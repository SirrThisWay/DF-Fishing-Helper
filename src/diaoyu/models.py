"""Domain model for the Delta Force (三角洲行动) fishing cost calculator.

Everything is expressed in *bag grid cells*:

* a **variant** (款式) of a fish occupies ``w x h`` cells and may be rotated;
* a **fish species** (鱼种) owns one or more variants — the same fish can come
  in several colours/styles, each with its **own size and price**;
* a container (胸挂 chest rig / 背包 backpack) is a grid of ``cols x rows``
  cells, optionally shaped by a mask;
* value is the variant's sale price in 哈夫币.

The variant level is where packing and costing happen, because size and price
live there. Examples straight from the price sheet:

    蓝宝石雷龙鱼 / 红         1x2  3800
    幼鲨        / 金色        1x4  1300
    幼鲨        / 红          1x5  2600
    铂金银龙鱼  / 红          1x2  3200
    铂金银龙鱼  / 红(隐藏款)   1x3  3200     <- same price, different size

The module is intentionally dependency-free so the solver can be unit tested
without a GUI.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

# --------------------------------------------------------------------------
# Rarity tiers (稀有度分档)
# --------------------------------------------------------------------------


#: 款式颜色 ↔ 稀有度色带 —— **这是稀有度的唯一真相**。
#:
#: 价目表里的 ``style`` 不是"同一种鱼的配色"，而是"同一种鱼在不同稀有度下的
#: 颜色"：白色/绿色/蓝色/紫色/金色/红。数据本身就能证明这一点 —— 每个鱼种的
#: 款式价格都随色带单调递增（罗非鱼 白20 < 绿45 < 蓝80；大西洋鲱 金1400 < 红2600）。
#: 所以分档**不需要**存字段、也**不需要**猜，直接从 style 读出来就行。
STYLE_TIERS: dict[str, str] = {
    "白色": "white",
    "绿色": "green",
    "蓝色": "blue",
    "紫色": "purple",
    "金色": "gold",
    "红":   "red",
    "红色": "red",
}

#: 不是颜色档的款式名，单独标记，不参与色带推导。
COLLECTOR_STYLE = "典藏"

#: 占位款式名：价格还没录，分档留着空。
PENDING_STYLES = frozenset({"待填", "", "默认"})


def tier_of_style(style: str) -> str:
    """Map a variant ``style`` to its rarity tier id (``"unknown"`` if unclear)."""
    s = (style or "").strip()
    if s == COLLECTOR_STYLE:
        return "collector"
    return STYLE_TIERS.get(s, "unknown")


@dataclass(frozen=True, slots=True)
class Tier:
    """One rarity band of the catch-picker.

    The game shows fish rarities as a colour ramp; the picker mirrors that so a
    player can find a fish by the colour they just saw on screen. ``id`` is what
    gets stored in JSON (stable, language-independent); ``label`` is what the UI
    shows; ``color`` is the swatch drawn behind the fish buttons.
    """

    id: str
    label: str
    color: str
    text: str = "#0d1117"      # readable text on top of ``color``


#: Lowest → highest. The order is the display order of the picker.
TIERS: tuple[Tier, ...] = (
    Tier("white",  "白",  "#e8edf2"),
    Tier("green",  "绿",  "#4fbf6a"),
    Tier("blue",   "蓝",  "#3f8fe0"),
    Tier("purple", "紫",  "#a468e8"),
    Tier("gold",   "金",  "#e8b53c"),
    Tier("red",    "红",  "#e04b4b", text="#ffffff"),
#: 兜底档：颜色认不出来、或者还没定档的鱼种进这里，UI 上单独一组，
#: 绝不静默丢弃。
Tier("unknown", "未分档", "#5a6470", text="#ffffff"),

#: 典藏款的落点。**典藏不是色带档位**（没有"典藏色"），所以它不参与
#: 白→红 的稀有度排序，但要有一个地方显示，否则那 2 个款式会掉进
#: "颜色认不出来"的告警里 —— 那是错的，典藏是明确的、有意义的标记。
Tier("collector", "典藏", "#f0d24a", text="#0d1117"),
)

TIER_INDEX: dict[str, Tier] = {t.id: t for t in TIERS}
TIER_ORDER: dict[str, int] = {t.id: i for i, t in enumerate(TIERS)}


def tier_of(raw: str | None) -> Tier:
    """Resolve a stored tier id, tolerating unknown/empty values."""
    return TIER_INDEX.get((raw or "").strip().lower(), TIER_INDEX["unknown"])


# --------------------------------------------------------------------------
# Fish variants (款式) — the packable unit
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FishVariant:
    """One style/colour of a fish — the unit that gets packed.

    ``species`` links back to the owning :class:`Fish`; ``name`` is the full
    display identifier used everywhere (``"幼鲨·金色"``), which keeps result
    dictionaries readable and unambiguous even when two species share a
    variant style name.
    """

    name: str              # full display id, e.g. "幼鲨·金色"
    species: str           # owning fish species, e.g. "幼鲨"
    style: str             # 款式, e.g. "金色" / "红" / "红(隐藏款)"
    size_w: int
    size_h: int
    price: int
    rarity: str = ""
    hidden: bool = False   # 隐藏款
    note: str = ""

    # -- derived ----------------------------------------------------------

    @property
    def tier_id(self) -> str:
        """稀有度分档 —— **由款式颜色直接推导**，不是另存的字段。

        这样"鱼的价格"和"鱼的稀有度"永远不可能互相矛盾：改颜色就等于改档位。
        """
        return tier_of_style(self.style)

    @property
    def tier_info(self) -> Tier:
        return tier_of(self.tier_id)

    @property
    def is_pending(self) -> bool:
        """款式还没录入（style 是「待填」之类的占位符）。"""
        return self.style in PENDING_STYLES

    @property
    def cells(self) -> int:
        """Footprint area in bag cells."""
        return self.size_w * self.size_h

    @property
    def size_label(self) -> str:
        return f"{self.size_w}X{self.size_h}"

    @property
    def value_per_cell(self) -> float:
        """Value density — the key ranking signal for what to pick up."""
        return self.price / self.cells if self.cells else 0.0

    @property
    def display(self) -> str:
        """Compact label for tables and the packing grid."""
        return self.name

    def orientations(self) -> tuple[tuple[int, int], ...]:
        """Distinct (w, h) footprints this variant can occupy in a bag."""
        if self.size_w == self.size_h:
            return ((self.size_w, self.size_h),)
        return ((self.size_w, self.size_h), (self.size_h, self.size_w))

    # -- serialisation ----------------------------------------------------

    def to_dict(self) -> dict:
        out: dict = {
            "style": self.style,
            "size": [self.size_w, self.size_h],
            "price": self.price,
        }
        if self.hidden:
            out["hidden"] = True
        if self.note:
            out["note"] = self.note
        return out

    @classmethod
    def from_dict(cls, raw: dict, species: str, rarity: str = "") -> "FishVariant":
        w, h = _parse_size(raw.get("size") or [1, 1])
        style = str(raw.get("style") or raw.get("name") or "默认")
        return cls(
            name=f"{species}·{style}",
            species=species,
            style=style,
            size_w=w,
            size_h=h,
            price=int(raw.get("price", 0)),
            rarity=rarity,
            hidden=bool(raw.get("hidden", False)),
            note=str(raw.get("note", "")),
        )


def _parse_size(size) -> tuple[int, int]:
    """Accept ``[1, 2]``, ``"1x2"`` or ``"1X2"`` and return ``(1, 2)``."""
    if isinstance(size, str):
        w, _, h = size.lower().replace("×", "x").partition("x")
        return int(w), int(h)
    return int(size[0]), int(size[1])


# --------------------------------------------------------------------------
# Fish species (鱼种)
# --------------------------------------------------------------------------


@dataclass(slots=True)
class Fish:
    """A fish species, owning one or more variants."""

    name: str
    variants: list[FishVariant] = field(default_factory=list)
    rarity: str = ""            # 原价目表上的文字标注（典藏 / 稀有 / 常见），保留不动
    note: str = ""
    #: 该鱼种有典藏款。**典藏不是色带档位**，所以单独一个布尔标记，
    #: 而不是塞进白/绿/蓝/紫/金/红里。
    collector: bool = False
    #: 可钓出的地区（水域）。空 = 见 ``regions_known``。
    regions: list[str] = field(default_factory=list)
    #: 该鱼种的产地是否**已经确认过**。
    #:
    #: 这个标记存在的理由：产地为空有两种完全不同的含义 ——
    #:   1. 「还没填」    -> 筛选时**必须**显示（不能因为资料缺失就把鱼藏起来）
    #:   2. 「确认没有」  -> 筛选时**必须**隐藏
    #: 只用 `regions == []` 无法区分两者，会把第 2 种当成第 1 种，
    #: 于是验证分布表时"每个水域能钓多少种"全部虚高（实测 16 -> 20）。
    regions_known: bool = False
    #: 推荐钓饵（这个鱼种在该水域建议用的饵）。
    bait: str = ""

    # -- accessors --------------------------------------------------------

    @property
    def variant_names(self) -> list[str]:
        return [v.name for v in self.variants]

    @property
    def variant_count(self) -> int:
        return len(self.variants)

    @property
    def prices(self) -> list[int]:
        return [v.price for v in self.variants]

    @property
    def price_range(self) -> tuple[int, int]:
        if not self.variants:
            return (0, 0)
        return (min(self.prices), max(self.prices))

    @property
    def best(self) -> FishVariant | None:
        """The highest value-density variant of this species."""
        return max(self.variants, key=lambda v: v.value_per_cell, default=None)

    # -- rarity / region / bait -------------------------------------------

    @property
    def tier_id(self) -> str:
        """该鱼种所在的分区 = 它**最稀有**的那个款式。

        同一种鱼在多个色带下都能钓到（罗非鱼 白/绿/蓝），所以鱼种本身没有单一
        稀有度；界面上按"最稀有的那一款"给它安排位置，款式自身的颜色仍由
        :attr:`FishVariant.tier_id` 决定。
        """
        tiers = [v.tier_id for v in self.variants
                 if v.tier_id not in ("unknown", "collector")]
        if not tiers:
            return "unknown"
        return max(tiers, key=lambda tid: TIER_ORDER.get(tid, 0))

    @property
    def tier_info(self) -> Tier:
        return tier_of(self.tier_id)

    @property
    def tier_ids(self) -> set[str]:
        """Every tier this species can be caught at."""
        return {v.tier_id for v in self.variants
                if v.tier_id not in ("unknown", "collector")}

    @property
    def priced_variants(self) -> list[FishVariant]:
        """Variants that can actually be sold — price 0 is a placeholder."""
        return [v for v in self.variants if v.price > 0]

    def in_region(self, region: str) -> bool:
        """Whether this species is catchable in ``region``.

        Two distinct cases, kept apart on purpose:

        * **产地未确认** (``regions_known`` 为假) —— 筛选时一律显示。
          不能因为资料没填就把一条鱼从清单里抹掉；宁可多显示，也不能让人
          以为自己钓到的鱼"不存在"。
        * **产地已确认** (``regions_known`` 为真) —— 严格按 `regions` 判断。
          此时空列表意味着"确认这里没有"，必须隐藏。
        """
        if not region or region == ALL_REGIONS:
            return True
        if not self.regions_known:
            return True
        return region in self.regions

    def variant(self, style: str) -> FishVariant | None:
        for v in self.variants:
            if v.style == style or v.name == style:
                return v
        return None

    def add_variant(self, variant: FishVariant) -> None:
        self.variants = [v for v in self.variants if v.name != variant.name]
        self.variants.append(variant)

    # -- serialisation ----------------------------------------------------

    def to_dict(self) -> dict:
        out: dict = {
            "name": self.name,
            "rarity": self.rarity,
            "variants": [v.to_dict() for v in self.variants],
        }
        if self.collector:
            out["collector"] = True
        out["regions"] = list(self.regions)
        out["regions_known"] = self.regions_known
        if self.bait:
            out["bait"] = self.bait
        if self.note:
            out["note"] = self.note
        return out

    @classmethod
    def from_dict(cls, raw: dict) -> "Fish":
        name = str(raw["name"])
        rarity = str(raw.get("rarity", ""))
        raw_variants = raw.get("variants")
        if raw_variants:
            variants = [FishVariant.from_dict(v, name, rarity) for v in raw_variants]
        else:
            # tolerate a flattened single-variant record
            variants = [FishVariant.from_dict(raw, name, rarity)]

        raw_regions = raw.get("regions")
        if isinstance(raw_regions, str):
            regions = [raw_regions] if raw_regions else []
        else:
            regions = [str(r) for r in (raw_regions or []) if str(r)]

        # collector 显式给了就用；没给就从款式里认（兼容老数据）
        if "collector" in raw:
            collector = bool(raw["collector"])
        else:
            collector = any(v.style == COLLECTOR_STYLE for v in variants)

        # regions_known 没写的话：**有**产地列表就推定已确认，空列表推定未确认。
        # 这条回退规则让老数据（只有 regions 数组）也能正确工作。
        if "regions_known" in raw:
            regions_known = bool(raw["regions_known"])
        else:
            regions_known = bool(regions)

        return cls(
            name=name, variants=variants, rarity=rarity,
            note=str(raw.get("note", "")),
            collector=collector,
            regions=regions,
            regions_known=regions_known,
            bait=str(raw.get("bait", "") or ""),
        )


# --------------------------------------------------------------------------
# Flattened catalogue — what the solver actually sees
# --------------------------------------------------------------------------


def flatten_variants(fish: Iterable[Fish]) -> list[FishVariant]:
    """Every variant of every species, as one flat list.

    ``Packer`` and the planner work on this, because size and price live at the
    variant level. ``FishVariant.species`` lets the UI group results back under
    the owning fish.
    """
    out: list[FishVariant] = []
    for f in fish:
        out.extend(f.variants)
    return out


def variant_lookup(fish: Iterable[Fish]) -> dict[str, FishVariant]:
    """``{variant full name: variant}`` for quick resolution."""
    return {v.name: v for v in flatten_variants(fish)}


#: Sentinel for "no region filter". Kept as a constant so the GUI, the loader
#: and the tests all spell it the same way.
ALL_REGIONS = ""


def fish_by_tier(fish: Iterable[Fish], region: str = ALL_REGIONS) -> dict[str, list[Fish]]:
    """Group species by tier id, in ``TIERS`` order, filtered by region.

    Species whose region list is empty are never filtered out — an unfilled
    column must not hide fish. Every tier id gets a list (possibly empty) so the
    UI can render a stable set of sections.
    """
    out: dict[str, list[Fish]] = {t.id: [] for t in TIERS}
    for f in fish:
        if not f.in_region(region):
            continue
        out[f.tier_id].append(f)
    for lst in out.values():
        lst.sort(key=lambda f: (-(f.best.price if f.best else 0), f.name))
    return out


def collector_fish(fish: Iterable[Fish]) -> list[Fish]:
    """Species that have a 典藏款 — the game's "collector's edition" catch."""
    return [f for f in fish if f.collector]


def known_regions(fish: Iterable[Fish]) -> list[str]:
    """Every region mentioned by any species, sorted for stable display."""
    seen = {r for f in fish for r in f.regions}
    return sorted(seen)


def best_bait(fish: Iterable[Fish], region: str = ALL_REGIONS,
              *, top: int = 3) -> list[tuple[str, int]]:
    """Recommended baits for a region, ranked by the value they can pull up.

    Each bait's score is the sum of every *priced* variant price of the species
    that list it — i.e. "if I fish this bait here, what is on the table". Returns
    ``[(bait, score), ...]``, highest first; species with no bait recorded are
    simply not represented.
    """
    scores: dict[str, int] = {}
    for f in fish:
        if not f.bait or not f.in_region(region):
            continue
        worth = sum(v.price for v in f.priced_variants)
        if worth <= 0:
            continue
        scores[f.bait] = scores.get(f.bait, 0) + worth
    ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    return ranked[:top] if top > 0 else ranked


# --------------------------------------------------------------------------
# Containers
# --------------------------------------------------------------------------


@dataclass(slots=True)
class Container:
    """A wearable container (胸挂 or 背包).

    ``mask`` is an optional row-major tuple of 0/1 (1 = usable cell) of length
    ``cols * rows``; when omitted the container is a plain rectangle.

    ``groups`` describes the container's **visual blocks** — in game a rig is
    shown as a handful of adjacent multi-cell regions (pouches), not a uniform
    grid, and each region renders as one solid block. Every group is
    ``(swatch_index, ((row, col), ...))``; the swatch is only a colour id, and
    the loader guarantees neighbouring groups never share one (a four-colour map
    colouring). Groups are **purely visual**: whether an item fits is decided by
    ``mask`` alone.
    """

    name: str
    kind: str              # "胸挂" | "背包"
    cols: int
    rows: int
    mask: tuple[int, ...] | None = None
    note: str = ""
    groups: tuple[tuple[int, tuple[tuple[int, int], ...]], ...] = ()
    #: 界面上显示的名字。``name`` 是数据源里的**权威名字**（可能被表格截断，
    #: 例如「穿山甲通用战…」），这里放补全后的可读名。为空则用 ``name``。
    #: 这样既不改动权威数据，界面上又不用看省略号。
    display_name: str = ""

    def __post_init__(self) -> None:
        if self.cols <= 0 or self.rows <= 0:
            raise ValueError(f"{self.name}: cols/rows must be positive")
        if self.mask is None:
            self.mask = tuple([1] * (self.cols * self.rows))
        elif len(self.mask) != self.cols * self.rows:
            raise ValueError(
                f"{self.name}: mask has {len(self.mask)} entries, "
                f"expected {self.cols * self.rows}"
            )

    @property
    def capacity(self) -> int:
        """Number of usable cells."""
        return sum(self.mask or ())

    @property
    def shown_name(self) -> str:
        """界面上用的名字：优先补全名，没有就用权威名。"""
        return self.display_name or self.name

    @property
    def label(self) -> str:
        return f"{self.shown_name} ({self.cols}x{self.rows}={self.capacity}格)"

    @property
    def name_is_truncated(self) -> bool:
        """数据源里的名字是否被截断（带省略号）。"""
        return "…" in self.name or "..." in self.name

    @property
    def is_rectangular(self) -> bool:
        return self.mask is None or all(self.mask)

    @property
    def group_of(self) -> dict[tuple[int, int], int]:
        """``{(row, col): group index}`` — used by the renderer."""
        return {cell: gi for gi, (_sw, cells) in enumerate(self.groups)
                for cell in cells}

    @property
    def swatch_of(self) -> dict[int, int]:
        """``{group index: swatch index}``."""
        return {gi: sw for gi, (sw, _cells) in enumerate(self.groups)}

    def to_dict(self) -> dict:
        out = {"name": self.name, "kind": self.kind,
               "cols": self.cols, "rows": self.rows}
        if self.display_name:
            out["display_name"] = self.display_name
        if not self.is_rectangular:
            out["mask"] = list(self.mask or ())
        if self.groups:
            out["groups"] = [{"cells": [list(c) for c in cells], "swatch": sw}
                             for sw, cells in self.groups]
        if self.note:
            out["note"] = self.note
        return out

    @classmethod
    def from_dict(cls, raw: dict) -> "Container":
        mask = raw.get("mask")
        groups: list[tuple[int, tuple[tuple[int, int], ...]]] = []
        for g in raw.get("groups") or []:
            cells = tuple((int(r), int(c)) for r, c in (g.get("cells") or []))
            if cells:
                groups.append((int(g.get("swatch", 0)), cells))
        return cls(
            name=str(raw["name"]),
            kind=str(raw.get("kind", "背包")),
            cols=int(raw["cols"]),
            rows=int(raw["rows"]),
            mask=tuple(int(v) for v in mask) if mask else None,
            note=str(raw.get("note", "")),
            groups=tuple(groups),
            display_name=str(raw.get("display_name", "") or ""),
        )


# --------------------------------------------------------------------------
# Consumables (成本项)
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Consumable:
    """A consumable item spent on a fishing run (饵/钩/线/损耗品…)."""

    name: str
    unit_price: int
    quantity: int = 1
    category: str = "其他"

    @property
    def cost(self) -> int:
        return self.unit_price * self.quantity

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "unit_price": self.unit_price,
            "quantity": self.quantity,
            "category": self.category,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "Consumable":
        return cls(
            name=str(raw["name"]),
            unit_price=int(raw.get("unit_price", 0)),
            quantity=max(0, int(raw.get("quantity", 1))),
            category=str(raw.get("category", "其他")),
        )


# --------------------------------------------------------------------------
# Run configuration
# --------------------------------------------------------------------------


@dataclass(slots=True)
class Loadout:
    """What the player is wearing / carrying for this run."""

    chest_rig: Container | None = None
    backpack: Container | None = None
    consumables: list[Consumable] = field(default_factory=list)
    travel_cost: int = 0          # 入场费/装备损耗 that is not a consumable

    @property
    def containers(self) -> list[Container]:
        return [c for c in (self.chest_rig, self.backpack) if c is not None]

    @property
    def consumable_cost(self) -> int:
        return sum(c.cost for c in self.consumables)

    @property
    def total_cost(self) -> int:
        """Total value that must be brought back for the run to break even."""
        return self.consumable_cost + self.travel_cost

    @property
    def capacity(self) -> int:
        return sum(c.capacity for c in self.containers)

    def add_consumable(self, item: Consumable) -> None:
        self.consumables = [c for c in self.consumables if c.name != item.name]
        if item.quantity > 0:
            self.consumables.append(item)


def total_capacity(containers: Iterable[Container]) -> int:
    return sum(c.capacity for c in containers)
