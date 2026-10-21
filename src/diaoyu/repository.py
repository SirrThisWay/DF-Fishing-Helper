"""JSON-backed data access for the calculator.

All game data lives in **one** file, ``data/game.json``, so a player can correct
prices/容量 without touching code — which matters here because parts of the
price sheet are still pending manual verification.

Schema
------
A **fish species** owns one or more **variants** (款式). Size and price live on
the variant, because the same fish comes in several colours with different
sizes and prices::

    data/game.json
    {
      "travel_cost": 0,
      "fish": [
        {
          "name": "幼鲨",
          "variants": [
            {"style": "金色", "size": [1, 4], "price": 1300},
            {"style": "红",   "size": [1, 5], "price": 2600}
          ]
        },
        {
          "name": "铂金银龙鱼",
          "variants": [
            {"style": "红",         "size": [1, 2], "price": 3200},
            {"style": "红(隐藏款)",  "size": [1, 3], "price": 3200, "hidden": true}
          ]
        }
      ],
      "chest_rigs": [...],
      "backpacks":  [...],
      "consumables": [{"name": "荧光拟饵", "unit_price": 13, "quantity": 1}]
    }

稀有度(tier) 不写进文件：它由款式的 ``style`` 颜色推导(见 models.STYLE_TIERS)，
文件里只保留 ``style``，避免两处数据打架。

A flattened record ``{name, size, price}`` inside ``fish`` is still accepted for
convenience and becomes a single-variant species.
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .models import (
    Consumable,
    Container,
    Fish,
    FishVariant,
    flatten_variants,
    variant_lookup,
)


def _is_frozen() -> bool:
    """True when running from a packaged build (Nuitka **or** PyInstaller)."""
    return bool(getattr(sys, "frozen", False)) or hasattr(sys, "_MEIPASS") \
        or "__compiled__" in globals()


def _exe_dir() -> Path:
    """Directory containing the shipped executable."""
    return Path(sys.executable).resolve().parent


def resolve_data_dir() -> Path:
    """Find the directory the JSON data lives in — and can be **written** to.

    Priority:

    1. ``DIAOYU_DATA_DIR`` env var — explicit override, wins over everything.
    2. **Packaged build** — a ``data/`` folder next to the .exe.
       Both packagers put a copy there (Nuitka via ``--include-data-dir``,
       PyInstaller via the spec), and that copy is *also* the writable location:
       a green build keeps everything in its own folder, so edits land beside
       the exe and survive restarts.
    3. **Running from source** — ``<repo>/data``, two levels above this file
       (``src/diaoyu/repository.py``).

    The return value is not guaranteed to exist; callers create it on save.
    """
    override = os.environ.get("DIAOYU_DATA_DIR")
    if override:
        return Path(override).expanduser()

    if _is_frozen():
        return _exe_dir() / "data"

    return Path(__file__).resolve().parents[2] / "data"


def bundled_data_dir() -> Path:
    """Where the read-only data shipped *with the program* sits.

    各打包方式的布局不同：

    * **Nuitka** —— ``--include-data-dir`` 把数据放在 **exe 旁边**，
      和可写目录是同一个地方，所以播种步骤自然变成 no-op。
    * **PyInstaller** —— 数据在 ``sys._MEIPASS``（只读临时目录），
      需要复制一份到 exe 旁边才能编辑保存。
    * **源码运行** —— 直接就是仓库里的 ``data/``，同样是 no-op。
    """
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass) / "data"
    if _is_frozen():
        return _exe_dir() / "data"
    return Path(__file__).resolve().parents[2] / "data"


def ensure_data_dir(target: Path | None = None) -> Path:
    """Make sure the writable data dir exists, seeding it from the bundle.

    Returns the directory to use. Existing files are never overwritten, so the
    user's edits survive upgrades.
    """
    target = Path(target) if target else resolve_data_dir()
    target.mkdir(parents=True, exist_ok=True)

    source = bundled_data_dir()
    if source.resolve() == target.resolve() or not source.is_dir():
        return target

    for src in sorted(source.glob("*.json")):
        dst = target / src.name
        if not dst.exists():
            try:
                dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
            except OSError:
                pass          # 只读环境：至少不让启动失败
    return target


DEFAULT_DATA_DIR = resolve_data_dir()


# --------------------------------------------------------------------------
# 界面偏好（settings.json）
# --------------------------------------------------------------------------
#
# 和「游戏数据」分开放：data/ 是玩家要编辑的价目表，settings.json 是程序
# 自己的状态（新手提示是否已关闭等）。混在一起会让玩家以为那是数据。
# 位置同样在 exe 旁边（冻结后）/ 仓库根（源码运行时）。

SETTINGS_NAME = "settings.json"


def settings_path(base: Path | None = None) -> Path:
    base = Path(base) if base else DEFAULT_DATA_DIR.parent
    return base / SETTINGS_NAME


def load_settings(base: Path | None = None) -> dict:
    """Read UI preferences; a missing or broken file just means "defaults"."""
    path = settings_path(base)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_settings(values: dict, base: Path | None = None) -> Path | None:
    """Persist UI preferences. Never raises — a failed save must not break the app."""
    path = settings_path(base)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(values, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
        return path
    except OSError:
        return None


def get_setting(key: str, default=None, base: Path | None = None):
    return load_settings(base).get(key, default)


def set_setting(key: str, value, base: Path | None = None) -> None:
    values = load_settings(base)
    values[key] = value
    save_settings(values, base)


class DataError(Exception):
    """Raised when a data file is missing or malformed."""


@dataclass(slots=True)
class ConsumableTemplate:
    """A selectable consumable row in the loadout editor.

    ``note`` 只是数据备注(例如「价格由整图坐标推断」)，程序不用它算钱,
    但保存时必须原样写回 —— 免得编辑一次就把疑点记录抹掉。
    """

    name: str
    unit_price: int
    quantity: int = 1
    category: str = "其他"
    enabled: bool = False
    note: str = ""

    @property
    def cost(self) -> int:
        return self.unit_price * self.quantity

    def to_consumable(self) -> Consumable:
        return Consumable(
            name=self.name, unit_price=self.unit_price,
            quantity=self.quantity, category=self.category,
        )

    def to_dict(self) -> dict:
        out = {
            "name": self.name, "unit_price": self.unit_price,
            "quantity": self.quantity, "category": self.category,
            "enabled": self.enabled,
        }
        if self.note:
            out["note"] = self.note
        return out


@dataclass(slots=True)
class GameData:
    """Everything the calculator needs, loaded from disk."""

    fish: list[Fish]
    chest_rigs: list[Container]
    backpacks: list[Container]
    consumables: list[ConsumableTemplate]
    travel_cost: int = 0
    data_dir: Path = DEFAULT_DATA_DIR
    warnings: list[str] = field(default_factory=list)
    _variants: list[FishVariant] | None = field(default=None, repr=False)
    _index: dict[str, FishVariant] | None = field(default=None, repr=False)

    # -- catalogue views --------------------------------------------------

    @property
    def variants(self) -> list[FishVariant]:
        """Every variant of every species — the solver's view of the catalogue."""
        if self._variants is None:
            self._variants = flatten_variants(self.fish)
        return self._variants

    @property
    def variant_index(self) -> dict[str, FishVariant]:
        """``{variant full name: variant}``."""
        if self._index is None:
            self._index = variant_lookup(self.fish)
        return self._index

    def invalidate(self) -> None:
        """Drop cached catalogue views after the editor mutates ``fish``."""
        self._variants = None
        self._index = None

    def species_of(self, variant_name: str) -> str:
        v = self.variant_index.get(variant_name)
        return v.species if v else variant_name

    def variants_of(self, species: str) -> list[FishVariant]:
        for f in self.fish:
            if f.name == species:
                return list(f.variants)
        return []

    def container_by_name(self, name: str) -> Container | None:
        """按名字找容器 —— 权威名（``name``）和显示名（``shown_name``）都认。

        界面的下拉用的是 ``shown_name``（补全名，避免省略号），
        而数据/屏蔽记录用的是权威 ``name``，两种都要能查回来。
        """
        for c in self.chest_rigs + self.backpacks:
            if c.name == name or c.shown_name == name:
                return c
        return None

    find_container = container_by_name


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------
#
# 全部游戏数据放在**一个** ``data/game.json`` 里（fish / chest_rigs /
# backpacks / consumables / travel_cost）。
#
# 为什么是单文件：实测读三个文件 0.549 ms、读一个 0.379 ms，只省 0.17 ms ——
# 而一次求解要几秒，这点差异完全在噪声里。合并的真正理由是**管理**：
# 备份、分发、编辑、看 diff 都只要盯一个文件。
#
# 早期版本是三个独立文件（fish.json / containers.json / consumables.json），
# 现在统一到 game.json；下面每个 load_* 都可以接受"已经读好的整份数据"，
# 避免重复读盘。

GAME_DATA_FILE = "game.json"


def _read_json(path: Path) -> dict:
    if not path.exists():
        raise DataError(f"缺少数据文件: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise DataError(f"{path.name} 不是合法 JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise DataError(f"{path.name} 顶层必须是对象")
    return raw


def load_game_file(data_dir: Path | str | None = None) -> dict:
    """读入整份 ``game.json``（一次读盘，各 load_* 共用）。"""
    ddir = Path(data_dir) if data_dir else DEFAULT_DATA_DIR
    return _read_json(ddir / GAME_DATA_FILE)


def load_fish(
    path: Path | None = None, *, raw: dict | None = None
) -> tuple[list[Fish], list[str]]:
    """解析鱼种。``raw`` 已给出时不再读盘。"""
    raw = raw if raw is not None else _read_json(
        path or (DEFAULT_DATA_DIR / GAME_DATA_FILE))
    items = raw.get("fish") or []
    fish: list[Fish] = []
    warnings: list[str] = []
    for entry in items:
        try:
            species = Fish.from_dict(entry)
        except (KeyError, ValueError, TypeError) as exc:
            warnings.append(f"鱼种记录被跳过 {entry!r}: {exc}")
            continue
        if not species.variants:
            warnings.append(f"「{species.name}」没有任何款式，已跳过")
            continue
        fish.append(species)

    if not fish:
        raise DataError("game.json 里没有任何有效的鱼")

    placeholders = [f.name for f in fish if f.name.startswith("鱼") and f.name[1:].isdigit()]
    if placeholders:
        warnings.append(
            f"有 {len(placeholders)} 个鱼种名称仍是占位符({placeholders[0]} 等)，"
            "请按价目表截图核对"
        )
    no_style = [v.name for f in fish for v in f.variants if v.style == "默认"]
    if no_style:
        warnings.append(
            f"有 {len(no_style)} 个款式未标注颜色/样式（显示为「·默认」），建议补全"
        )

    # 稀有度分档**由款式颜色推导**，所以这里只提醒"颜色认不出来"的情况，
    # 以及两个可选字段（产地 / 推荐钓饵）的缺失。
    unknown_style = [v.name for f in fish for v in f.variants
                     if v.tier_id == "unknown" and not v.is_pending]
    if unknown_style:
        warnings.append(
            f"有 {len(unknown_style)} 个款式的颜色不在色带里"
            f"（{'、'.join(unknown_style[:3])} 等），无法判断稀有度分档；"
            "请改成 白色/绿色/蓝色/紫色/金色/红"
        )
    pending = [v.name for f in fish for v in f.variants if v.is_pending]
    if pending:
        warnings.append(
            f"有 {len(pending)} 个款式还没填价格与颜色（显示为「待填」），"
            "装箱时会被忽略"
        )
    no_region = [f.name for f in fish if not f.regions]
    if no_region:
        warnings.append(
            f"有 {len(no_region)} 个鱼种未填产地（地区筛选时一律显示），"
            "填了之后才能按水域过滤"
        )
    no_bait = [f.name for f in fish if not f.bait]
    if no_bait:
        warnings.append(
            f"有 {len(no_bait)} 个鱼种未填推荐钓饵，鱼饵推荐会不完整"
        )
    return fish, warnings


def load_containers(
    path: Path | None = None, *, raw: dict | None = None
) -> tuple[list[Container], list[Container], list[str]]:
    raw = raw if raw is not None else _read_json(
        path or (DEFAULT_DATA_DIR / GAME_DATA_FILE))
    warnings: list[str] = []
    rigs = _load_container_list(raw.get("chest_rigs"), "胸挂", warnings)
    bags = _load_container_list(raw.get("backpacks"), "背包", warnings)
    if not rigs and not bags:
        raise DataError("game.json 里没有任何容器")
    if any("占位" in c.note or "核对" in c.name for c in rigs + bags):
        warnings.append("容器容量仍是占位值，请按胸挂/背包截图核对 cols/rows")
    return rigs, bags, warnings


def _load_container_list(items: list | None, kind: str, warnings: list[str]) -> list[Container]:
    out: list[Container] = []
    for entry in items or []:
        try:
            out.append(Container.from_dict(entry))
        except (KeyError, ValueError, TypeError) as exc:
            warnings.append(f"{kind}记录被跳过 {entry!r}: {exc}")
    return out


def load_consumables(
    path: Path | None = None, *, raw: dict | None = None
) -> tuple[list[ConsumableTemplate], int, list[str]]:
    raw = raw if raw is not None else _read_json(
        path or (DEFAULT_DATA_DIR / GAME_DATA_FILE))
    out: list[ConsumableTemplate] = []
    warnings: list[str] = []
    for entry in raw.get("consumables") or raw.get("items") or []:
        try:
            out.append(
                ConsumableTemplate(
                    name=str(entry["name"]),
                    unit_price=int(entry.get("unit_price", 0)),
                    quantity=max(1, int(entry.get("quantity", 1))),
                    category=str(entry.get("category", "其他")),
                    enabled=bool(entry.get("enabled", False)),
                    note=str(entry.get("note", "") or ""),
                )
            )
        except (KeyError, ValueError, TypeError) as exc:
            warnings.append(f"消耗品记录被跳过 {entry!r}: {exc}")
    return out, int(raw.get("travel_cost", 0) or 0), warnings


def load_game_data(data_dir: Path | str | None = None) -> GameData:
    """读入全部游戏数据（只读一次盘）。"""
    ddir = Path(data_dir) if data_dir else DEFAULT_DATA_DIR
    raw = load_game_file(ddir)
    fish, w1 = load_fish(raw=raw)
    rigs, bags, w2 = load_containers(raw=raw)
    consumables, travel, w3 = load_consumables(raw=raw)
    return GameData(
        fish=fish, chest_rigs=rigs, backpacks=bags,
        consumables=consumables, travel_cost=travel,
        data_dir=ddir, warnings=w1 + w2 + w3,
    )


# --------------------------------------------------------------------------
# Saving (used by the built-in data editor)
# --------------------------------------------------------------------------
#
# 全部数据都在 game.json 一个文件里。编辑器只改自己那一段, 其余段落从磁盘
# 原样读回来再写出去 —— 这样保存价目表不会顺手清掉容器容量。

GAME_README = [
    "三角洲行动 钓鱼成本核算器 —— 全部游戏数据。",
    "fish: 价目表。每个鱼种拥有若干「款式」, 尺寸与价格挂在款式上。",
    "      size 是占格 [宽, 高], 例如 [1,4] 表示 1x4 格; hidden: true 表示隐藏款。",
    "      稀有度由 style(款式颜色) 自动推出, 不要再写 tier 字段。",
    "chest_rigs / backpacks: 胸挂与背包。cols=宽(列数), rows=高(行数)。",
    "      mask 表示缺角, groups 表示互不互通的格子分组(颜色)。",
    "consumables: 消耗品(成本项)。总成本 = Σ unit_price*quantity + travel_cost。",
    "本文件由程序内的编辑界面保存, 也可手工编辑; 段落缺失时会自动补默认值。",
]


#: 纯数字数组（容器 mask、口袋格子坐标）。``indent`` 会把 28 个 0/1 排成 28 行，
#: 整个文件从 75 KB 涨到 110 KB，人也没法读 —— 这些数组压回一行。
_NUMERIC_LIST = re.compile(r"\[\s*([\d\s,\-]+?)\s*\]")


def dump_game_json(payload: dict) -> str:
    """把整份数据序列化成「人能读、diff 也看得懂」的 JSON 文本。"""
    text = json.dumps(payload, ensure_ascii=False, indent=2)

    def _compact(m: re.Match[str]) -> str:
        items = [t.strip() for t in m.group(1).split(",") if t.strip()]
        return "[" + ", ".join(items) + "]"

    return _NUMERIC_LIST.sub(_compact, text) + "\n"


def _merge_game_file(
    data_dir: Path | str | None = None, **sections: object
) -> Path:
    """把 ``sections`` 合并进 game.json 并写回, 其余段落保持磁盘原样。"""
    ddir = Path(data_dir) if data_dir else DEFAULT_DATA_DIR
    ddir.mkdir(parents=True, exist_ok=True)
    path = ddir / GAME_DATA_FILE
    try:
        payload = load_game_file(ddir) if path.exists() else {}
    except (DataError, json.JSONDecodeError):
        payload = {}
    payload.setdefault("_README", list(GAME_README))
    for key, value in sections.items():
        payload[key] = value
    path.write_text(dump_game_json(payload), encoding="utf-8")
    return path


def save_fish(fish: list[Fish], data_dir: Path | str | None = None) -> Path:
    return _merge_game_file(data_dir, fish=[f.to_dict() for f in fish])


def save_containers(
    rigs: list[Container], bags: list[Container], data_dir: Path | str | None = None
) -> Path:
    return _merge_game_file(
        data_dir,
        chest_rigs=[c.to_dict() for c in rigs],
        backpacks=[c.to_dict() for c in bags],
    )


def save_consumables(
    items: list[ConsumableTemplate], travel_cost: int, data_dir: Path | str | None = None
) -> Path:
    return _merge_game_file(
        data_dir,
        travel_cost=int(travel_cost),
        consumables=[i.to_dict() for i in items],
    )
