"""Tkinter GUI for the 三角洲行动 钓鱼成本核算器.

Three panes, matching the calculation workflow:

* **left**   — 装备与消耗品：胸挂 / 背包 / 消耗品勾选与数量 → 回本线(总成本)
* **center** — 核算结果：单趟最高收益、能否回本、最少几条什么鱼回本、
               拾取推荐、装载网格可视化(按容器画格子并标注鱼)
* **right**  — 已钓上的鱼：先选鱼种、再选款式，加减数量 → 实时重算最优携带方案，
               并列出装不下、建议丢弃的鱼

The catalogue is two-level: a **fish species** (鱼种) owns one or more
**variants** (款式), and size/price live on the variant — the same fish in a
different colour can be a different size at a different price. So the catch
sidebar picks species first, then variant, and every result is keyed by the
variant's full name (``幼鲨·金色``).

Only ``tkinter`` from the stdlib is used. Planning runs on a worker thread so
the UI never blocks.
"""

from __future__ import annotations

import queue
import sys
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from .models import (
    ALL_REGIONS,
    TIER_INDEX,
    TIER_ORDER,
    TIERS,
    Consumable,
    Container,
    Fish,
    FishVariant,
    Loadout,
    best_bait,
    fish_by_tier,
    known_regions,
)
from .packing import PackResult
from .planner import RunPlan, caught_value, plan_run, report_catch
from .repository import (
    ConsumableTemplate,
    GameData,
    get_setting,
    save_consumables,
    save_containers,
    save_fish,
    set_setting,
)

# --------------------------------------------------------------------------
# Palette
# --------------------------------------------------------------------------

BG = "#12161d"
PANEL = "#1b212b"
PANEL_ALT = "#222a36"
FG = "#e6ebf2"
MUTED = "#8b98a9"
ACCENT = "#39a7ff"
GOOD = "#3ddc84"
WARN = "#ffb020"
BAD = "#ff5c5c"
GRID_LINE = "#2e3947"
EMPTY_CELL = "#171d26"

FISH_COLORS = [
    "#e0603c", "#e08a2c", "#d9b62c", "#a8c92c", "#5ec24a", "#35b58a",
    "#35a7b5", "#3d7fd6", "#6a63d6", "#9a56cf", "#c853b0", "#d64f76",
]

#: 地区下拉里代表"不筛选"的那一项。模型里的哨兵是空字符串（``ALL_REGIONS``），
#: 但空字符串在下拉里看起来像 bug，所以只在 UI 上换个说法。
ALL_REGIONS_LABEL = "全部水域"

#: 胸挂/背包下拉里代表"这个部位还没选"的选项。
#: 显示成一个能看懂的词，而不是空白 —— 空白看起来像界面坏了。
NOT_SELECTED = "（未选择）"


def _unselect(value: str) -> str:
    """Turn the display label back into the empty value the model expects."""
    return "" if value in ("", NOT_SELECTED) else value


def _color_for(index: int) -> str:
    return FISH_COLORS[index % len(FISH_COLORS)]


def _is_light(hex_color: str) -> bool:
    """Perceived-luminance test, used to pick readable ink on a swatch."""
    try:
        r = int(hex_color[1:3], 16)
        g = int(hex_color[3:5], 16)
        b = int(hex_color[5:7], 16)
    except (ValueError, IndexError):
        return False
    return (0.299 * r + 0.587 * g + 0.114 * b) > 150


# --------------------------------------------------------------------------
# High-DPI
# --------------------------------------------------------------------------
#
# 为什么必须处理：tkinter 默认是 **DPI 不感知** 的。在 125% / 150% 缩放的
# Windows 上，系统会把整个窗口**拉伸**成位图 —— 画布里明明是 30×30 的
# 正方形格子，拉到屏幕上就成了长方形，格数看着不对、鱼块也变形。
# 处理方式分两步：
#   1. 启动时声明 DPI 感知，让 Windows 别再拉伸我们（画面变清晰，坐标=物理像素）
#   2. 按缩放比例放大格子边长与字号，否则在高 DPI 屏上 UI 会小得看不清
_DPI_SCALE = 1.0


def _setup_dpi_awareness() -> None:
    """Tell Windows we handle DPI ourselves. Must run **before** Tk starts.

    ``SetProcessDpiAwarenessContext(-4)`` = PER_MONITOR_AWARE_V2 (Win10 1703+).
    Falls back through shcore then user32 for older systems; on non-Windows or
    on failure we simply stay DPI-unaware (the old stretched behaviour).
    """
    global _DPI_SCALE
    if sys.platform != "win32":
        return
    try:
        import ctypes

        try:
            ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        except (AttributeError, OSError):
            try:
                ctypes.windll.shcore.SetProcessDpiAwareness(2)
            except (AttributeError, OSError):
                ctypes.windll.user32.SetProcessDPIAware()
        # 取主屏 DPI 作为统一缩放系数
        try:
            dpi = ctypes.windll.user32.GetDpiForSystem()
        except (AttributeError, OSError):
            hdc = ctypes.windll.user32.GetDC(0)
            dpi = ctypes.windll.gdi32.GetDeviceCaps(hdc, 88)   # LOGPIXELSX
            ctypes.windll.user32.ReleaseDC(0, hdc)
        if dpi and dpi > 0:
            _DPI_SCALE = max(1.0, dpi / 96.0)
    except Exception:          # noqa: BLE001 - 感知失败不该拦住程序启动
        _DPI_SCALE = 1.0


def _px(value: float) -> int:
    """Scale a pixel value for the current display DPI."""
    return max(1, int(round(value * _DPI_SCALE)))


def dpi_scale() -> float:
    """Current display scale factor (1.0 = 96 DPI / 100%)."""
    return _DPI_SCALE


def _font(size: int, weight: str = "") -> tuple:
    """A YaHei UI font tuple with a DPI-scaled point size.

    Positive sizes in Tk are **points**, which Tk already scales by
    ``tk scaling``; that is why the fonts did not need this and the *grid* did.
    We still route every font through here so sizes stay consistent, and pass
    the same point sizes as before (no double scaling).
    """
    return ("Microsoft YaHei UI", size, weight) if weight else ("Microsoft YaHei UI", size)


#: 容器"组"的填充色 —— 只用于**画背景块**，不表示任何语义。
#: 相邻组会被四色算法保证取到不同下标，所以紧挨着的两块颜色一定不同，
#: 加上中间那道深色缝隙，分区一眼可辨。
_GROUP_FILLS = ("#1d2b3a", "#1e3328", "#332c1c", "#2b2338",
                "#3a2226", "#1c3233", "#332a1d", "#2a2440")
_GROUP_EDGE = ("#2f4a63", "#2f5c42", "#5c4a2a", "#4a3a63",
               "#63383e", "#2a5557", "#5c4c2a", "#463a6b")


#: 一个组里最多把鱼名写到多少行 —— 再多就不像"一整块"了。
_MAX_LABEL_LINES = 3


def _wrap_cjk(text: str, per_line: int) -> list[str]:
    """Greedy wrap for CJK (every char is one unit wide)."""
    per_line = max(1, per_line)
    return [text[i:i + per_line] for i in range(0, len(text), per_line)]


def _fish_label_for_block(species: str, w: int, h: int) -> tuple[str, int]:
    """Fit a fish name into a ``w × h`` block, in **as few lines as possible**.

    Returns ``(text, font_size)``.

    设计要点：
    * 名字在一个方块里**只出现一次**，居中显示 —— 不是每格写一个字。
      否则一条 2×5 的鱼会在十个格子里写十遍名字，完全没法读。
    * 优先**一行**装下；装不下才折行，最多 3 行，再装不下才截断。
    * 字号跟着格子数走，块越大字越大。
    """
    area = max(1, w * h)
    max_lines = 1 if h <= 1 else min(_MAX_LABEL_LINES, h)

    # 能容下多少字：按格子宽高估算，中文字约等于一个格子宽
    for lines in range(1, max_lines + 1):
        per_line = max(1, (len(species) + lines - 1) // lines)
        if per_line <= w and lines <= h:
            chunks = _wrap_cjk(species, per_line)
            if len(chunks) <= lines:
                size = 7 if min(w, per_line) <= 2 and area <= 2 else (
                    8 if area <= 4 else (9 if area <= 8 else 11))
                return "\n".join(chunks), size

    # 实在放不下：按能容纳的字数截断
    per_line = max(1, w)
    lines = max(1, min(max_lines, h))
    return "\n".join(_wrap_cjk(species, per_line)[:lines]), 7


def _fish_full_label(full_name: str, index: dict) -> str:
    """``蓝宝石雷龙鱼·红 1X2 3800`` — 图例与清单里用的完整标签。"""
    v = index.get(full_name)
    if v is None:
        return full_name
    return f"{full_name}  {v.size_label}  {v.price}"


def _split_regions(text: str) -> list[str]:
    """Parse "零号大坝、长弓溪谷" / "零号大坝,长弓溪谷" into a clean list."""
    if not text:
        return []
    for sep in ("、", "，", ",", ";", "；", "/", "|"):
        text = text.replace(sep, "\n")
    return [part.strip() for part in text.splitlines() if part.strip()]


#: 可选字段的缺失提醒会被折叠成一句，否则 31 个鱼种会刷满整个横幅。
_OPTIONAL_WARN_KEYS = ("未填产地", "未填推荐钓饵")


def _condense_warnings(warnings: list[str]) -> list[str]:
    """Collapse the "optional column not filled" family into one summary line.

    Everything else (占位符名称、款式待填价、容器待核对…) passes through intact,
    because those really do need the user's attention.
    """
    optional = [w for w in warnings if any(k in w for k in _OPTIONAL_WARN_KEYS)]
    rest = [w for w in warnings if w not in optional]
    if optional:
        parts = []
        for key, label in zip(_OPTIONAL_WARN_KEYS, ("产地", "推荐钓饵")):
            hit = next((w for w in optional if key in w), None)
            if hit is None:
                continue
            n = "".join(ch for ch in hit.split("有 ", 1)[-1] if ch.isdigit())
            parts.append(f"{label} {n or '?'} 个")
        rest.append(
            "资料待补：" + "、".join(parts)
            + "鱼种（在「编辑数据 → 产地 / 鱼饵 / 典藏」里补；不填也能用）"
        )
    return rest


# --------------------------------------------------------------------------
# First-run tutorial
# --------------------------------------------------------------------------

#: 新手教程正文。
#:
#: 两条硬约束（都是实际踩过的）：
#:   1. **不要写 Markdown**。tkinter 的 Text/Label 不会渲染 ``**粗体**``，
#:      星号会原样显示出来。强调请用【】或 —— 这种自带视觉重量的符号。
#:   2. **每行别超过约 32 个汉字**。文本框宽 64 半角字符，1 个汉字≈2 个半角，
#:      超长会被折行，而折行后缩进会丢，条目看起来就散了。
TUTORIAL_TITLE = "第一次用？30 秒看懂"
TUTORIAL_TEXT = """\
这个工具回答三个问题：
  这一趟最多能带回多少钱、能不能回本、
  回本最少要几条什么鱼。

① 左边：填你这一趟的配置
   • 勾选带的消耗品（饵料、渔具…）
   • 胸挂、背包必须自己选
     （程序不会替你预设）

② 右边：钓到鱼就点一下
   • 按颜色找鱼：白/绿/蓝/紫/金/红 就是稀有度
   • 鱼块上那排小色块 = 这鱼能钓到哪几档
   • 带「藏」角标 = 有典藏款

   ★ 左键点鱼块 = +1
     加的是「当前款」——鱼块上写着，如 红1X5
   ★ 右键点鱼块 = 换成别的稀有度/尺寸
     这才是关键操作！

   右键菜单里每一行是一档：
     金色 1X4 [金]
     红   1X5 [红]
     典藏 2X5 [典藏]
   点哪一行就 +1 那一条，而且之后左键
   都按这一档来加。选中那行前面有 ●。

   例：你钓到的是【红色 1X5 幼鲨】，
       就要右键选「红 1X5 [红]」；
       直接左键会记成金色 1X4（默认款），
       尺寸和稀有度都不是你那条。

   • 右键菜单里还能「−1」「清掉这一种」
   • 先在「水域」里选钓点，只列那儿能钓的鱼

③ 中间：看结果
   • 格子图每块鱼都写了名字
     左上角小字是尺寸（如 2×3）
   • 不同颜色的口袋互不相通
     一条鱼不能横跨两个口袋
   • 网格下方有图例：
     颜色 ↔ 鱼名 ↔ 稀有度 ↔ 条数 ↔ 单价

小技巧
   • 点格子可以「屏蔽」它（不参与装载）
     再点一次取消
   • 计算要一两秒时，中间会出现滚动进度条
     ——那是正在算，不是卡死
   • 改价目表/容器：左栏「编辑价目表 / 容器…」
   • 数据文件在哪：左栏「打开数据目录」

价格与容量数据请自己核对一遍再用它做投放决策。
"""


class TutorialDialog(tk.Toplevel):
    """首次启动弹出的操作说明。

    没勾「不再提示」的话，每次打开都要弹。
    勾了之后写进 settings.json，之后就不再打扰。
    """

    def __init__(self, master: tk.Misc, *, setting_key: str = "tutorial_dismissed",
                 base_dir=None) -> None:
        super().__init__(master)
        self.title("新手提示 — 三角洲钓鱼成本核算器")
        self.configure(bg=BG)
        self.resizable(True, True)
        self.setting_key = setting_key
        self.base_dir = base_dir
        self.dont_show = tk.BooleanVar(value=False)

        outer = ttk.Frame(self, style="Panel.TFrame", padding=14)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text=TUTORIAL_TITLE, style="Headline.TLabel").pack(anchor="w")
        ttk.Separator(outer).pack(fill="x", pady=(6, 8))

        body = tk.Text(outer, bg=PANEL_ALT, fg=FG, relief="flat", wrap="word",
                       font=_font(9), width=78, height=34)
        body.insert("1.0", TUTORIAL_TEXT)
        body.configure(state="disabled")
        body.pack(fill="both", expand=True)

        foot = ttk.Frame(outer, style="Panel.TFrame")
        foot.pack(fill="x", pady=(10, 0))
        ttk.Checkbutton(foot, text="不再提示", variable=self.dont_show).pack(side="left")
        ttk.Button(foot, text="开始使用", command=self._close).pack(side="right")

        self._center(master)
        self.transient(master)
        self.grab_set()
        self.bind("<Escape>", lambda _e: self._close())
        self.protocol("WM_DELETE_WINDOW", self._close)

    def _center(self, master: tk.Misc) -> None:
        self.update_idletasks()
        try:
            mx, my = master.winfo_rootx(), master.winfo_rooty()
            mw, mh = master.winfo_width(), master.winfo_height()
        except tk.TclError:
            return
        w, h = self.winfo_width(), self.winfo_height()
        self.geometry(f"+{mx + max(0, (mw - w) // 2)}+{my + max(0, (mh - h) // 3)}")

    def _close(self) -> None:
        if self.dont_show.get():
            set_setting(self.setting_key, True, self.base_dir)
        self.destroy()


def should_show_tutorial(base_dir=None, setting_key: str = "tutorial_dismissed") -> bool:
    """True unless the user ticked 「不再提示」 last time."""
    return not bool(get_setting(setting_key, False, base_dir))


# --------------------------------------------------------------------------
# Grid rendering
# --------------------------------------------------------------------------


class GridCanvas(tk.Canvas):
    """Renders one container's **actual cell layout** plus the fish placed in it.

    Because a container is a 0/1 mask rather than a solid rectangle, this draws
    exactly the usable cells — so an irregular rig (pockets split by a seam)
    looks irregular, which is the whole point: a 2x2 fish cannot straddle a
    seam, and the drawing has to make that obvious.

    Cells are **clickable**: clicking a usable cell toggles it between
    "available" and "屏蔽(locked out)". Locked cells are removed from the
    packing problem entirely, so the solver never allocates anything to them —
    useful when you deliberately want to keep part of a rig free (ammo, meds,
    a quest item).
    """

    LOCK_FILL = "#3a2630"
    LOCK_OUTLINE = "#8a4a5c"

    def __init__(self, master: tk.Misc, cell: int = 34, **kw) -> None:
        super().__init__(master, bg=PANEL, highlightthickness=0, **kw)
        self.cell = cell
        self._container: Container | None = None
        self._pad = 10
        self._top = 0
        self._ox = 10        # 网格左边界（= pad + 刻度留白），点击命中和绘制共用
        self._on_toggle = None
        self._disabled: set[int] = set()
        self.bind("<Button-1>", self._on_click)
        self.bind("<Button-3>", self._on_click)

    def bind_toggle(self, callback) -> None:
        """``callback(container_name, cell_index)`` on every cell click."""
        self._on_toggle = callback

    def _on_click(self, event) -> None:
        cont = self._container
        if cont is None or self._on_toggle is None:
            return
        c = self.cell
        # 注意减去刻度留白 ox：点命中的是"格子"，不是画布像素。
        # 少了这一项，点击会整体偏移半格到一格，屏蔽格子就会点错。
        x = int((self.canvasx(event.x) - self._ox) // c)
        y = int((self.canvasy(event.y) - self._top) // c)
        if not (0 <= x < cont.cols and 0 <= y < cont.rows):
            return
        idx = y * cont.cols + x
        mask = cont.mask or tuple([1] * (cont.cols * cont.rows))
        if not mask[idx]:
            return          # not a slot at all — nothing to toggle
        self._on_toggle(cont.name, idx)

    def render(
        self,
        container: Container | None,
        placements: list,
        color_of: dict[str, str],
        title: str = "",
        subtitle: str = "",
        disabled: set[int] | None = None,
        legend: list[tuple[str, str, int, int, int | None, str]] | None = None,
    ) -> None:
        """Draw one container: its **group blocks**, the fish on it, and a legend.

        关键设计（都是实际使用中逼出来的）：

        * **格子必须是正方形**。画布里固定 `c × c`，再配合启动时的 DPI 感知，
          125%/150% 缩放的屏幕上才不会把正方形拉成长方形。
        * **按"组"画整块，不是按格画**。游戏里胸挂/背包是一块块相邻的多格区域
          （口袋），不是均匀网格。每个组画成一个整体色块，组与组之间留深色
          分隔带 —— 这正是"黑色格子造就的视觉分割线"那种观感。
        * **鱼名在一个方块里只写一次并居中**。曾经是每格写一次、而且写的是
          颜色（`蓝宝石雷龙鱼·红` → `红`），2×5 的鱼会在十个格子里写十遍；
          现在是整块写一行名字，放不下才折行，最多 3 行。
        * 左上角小字标尺寸（如 `2×3`）—— 8 格(2×4) 与 10 格(2×5) 的大眼海鲢
          全靠它区分。
        * **每个格子都要看得见**。只画整块会让玩家分不清格与格的边界，放鱼时
          很容易放到隔壁行/列；所以组块内部**再画一层格子内框**，并加上
          行/列刻度数字。
        * **鱼块颜色 = 该款式的稀有度色**（由调用方传入，不再随机分配）。
        """
        self.delete("all")
        self._container = container
        self._disabled = set(disabled or ())
        if container is None:
            return

        c = self.cell
        pad = self._pad
        rows, cols = container.rows, container.cols
        mask = container.mask or tuple([1] * (cols * rows))
        w, h = cols * c, rows * c
        # 左边/上边留出刻度数字的位置
        ruler = max(_px(13), c // 2)
        ox = pad + ruler
        oy = pad + ruler

        header = 0
        if title:
            self.create_text(pad, 4, text=title, anchor="nw", fill=FG,
                             font=_font(10, "bold"))
            header += _px(18)
        if subtitle:
            self.create_text(pad, 4 + header, text=subtitle, anchor="nw", fill=MUTED,
                             font=_font(8))
            header += _px(15)

        top = oy + header
        self._top = top
        self._ox = ox

        # 深色底：格子不存在的位置就是游戏里的空档/隔板
        self.create_rectangle(ox, top, ox + w, top + h,
                              fill="#0b0f14", outline="")

        group_of = container.group_of
        swatch_of = container.swatch_of
        dashed = set(self._disabled)

        # -- 先给每个"组"画一整块，组与组之间留深色缝隙（视觉分割线）--
        gap = max(2, c // 9)
        rect_groups: dict[int, bool] = {}
        for gi, (_sw, cells) in enumerate(container.groups):
            rs = [r for r, _ in cells]
            cs = [cc for _, cc in cells]
            r0, r1, c0, c1 = min(rs), max(rs), min(cs), max(cs)
            rect = len(cells) == (r1 - r0 + 1) * (c1 - c0 + 1)
            rect_groups[gi] = rect
            if not rect:
                continue          # 非矩形组：下面逐格补
            sw = swatch_of.get(gi, 0)
            self.create_rectangle(
                ox + c0 * c + gap, top + r0 * c + gap,
                ox + (c1 + 1) * c - gap, top + (r1 + 1) * c - gap,
                fill=_GROUP_FILLS[sw % len(_GROUP_FILLS)],
                outline=_GROUP_EDGE[sw % len(_GROUP_EDGE)], width=2,
            )

        # -- 逐格：内框（看得见每一格）+ 不可用暗点 + 屏蔽叉 + 非矩形组补底色 --
        line_color = "#33465c"
        for y in range(rows):
            for x in range(cols):
                i = y * cols + x
                cx = ox + x * c + c / 2
                cy = top + y * c + c / 2
                if not mask[i]:
                    self.create_oval(cx - 1.5, cy - 1.5, cx + 1.5, cy + 1.5,
                                     fill="#1b2330", outline="")
                    continue
                gi = group_of.get((y, x))
                if gi is not None and not rect_groups.get(gi, True):
                    sw = swatch_of.get(gi, 0)
                    self.create_rectangle(
                        ox + x * c + gap, top + y * c + gap,
                        ox + (x + 1) * c - gap, top + (y + 1) * c - gap,
                        fill=_GROUP_FILLS[sw % len(_GROUP_FILLS)],
                        outline=_GROUP_EDGE[sw % len(_GROUP_EDGE)], width=1)
                # 每格一个内框：不画的话整块看不出格与格的边界，
                # 玩家放鱼时很容易放到隔壁行/列。
                self.create_rectangle(
                    ox + x * c + 1, top + y * c + 1,
                    ox + (x + 1) * c - 1, top + (y + 1) * c - 1,
                    outline=line_color, width=1)
                if i in dashed:
                    r = c * 0.18
                    self.create_line(cx - r, cy - r, cx + r, cy + r,
                                     fill=self.LOCK_OUTLINE, width=2)
                    self.create_line(cx + r, cy - r, cx - r, cy + r,
                                     fill=self.LOCK_OUTLINE, width=2)

        # -- 行/列刻度：按数字定位，不会数错行 --
        for x in range(cols):
            self.create_text(ox + x * c + c / 2, top - ruler / 2 - _px(1),
                             text=str(x + 1), fill=MUTED, font=_font(7))
        for y in range(rows):
            self.create_text(ox - ruler / 2, top + y * c + c / 2,
                             text=str(y + 1), fill=MUTED, font=_font(7))

        # -- 放置的鱼：一个方块 = 一个整体，名字只写一次并居中 --
        for p in placements:
            x0 = ox + p.x * c
            y0 = top + p.y * c
            x1 = x0 + p.w * c
            y1 = y0 + p.h * c
            color = color_of.get(p.fish_name, ACCENT)
            self.create_rectangle(x0 + 2, y0 + 2, x1 - 2, y1 - 2,
                                  fill=color, outline="#0d1117", width=2)

            species = p.fish_name.split("·")[0] if "·" in p.fish_name else p.fish_name
            label, size = _fish_label_for_block(species, p.w, p.h)
            ink = "#0d1117" if _is_light(color) else "#ffffff"
            self.create_text((x0 + x1) / 2, (y0 + y1) / 2, text=label,
                             fill=ink, justify="center",
                             font=_font(size, "bold"))
            if p.w * p.h >= 4:
                self.create_text(x0 + _px(4), y0 + _px(2), anchor="nw",
                                 text=f"{p.w}×{p.h}", fill=ink, font=_font(7))

        # -- 图例：色块 ↔ 鱼名 ↔ 稀有度 ↔ 条数 ↔ 占格 ↔ 单价 --
        legend_h = 0
        items = [it for it in (legend or []) if it[2] > 0]
        if items:
            ly = top + h + _px(8)
            self.create_text(pad, ly, anchor="nw", text="图例", fill=MUTED,
                             font=_font(8, "bold"))
            ly += _px(14)
            for item in items:
                color, name, count, cells, price = item[:5]
                tier = item[5] if len(item) > 5 else ""
                self.create_rectangle(pad, ly + 1, pad + _px(12), ly + _px(13),
                                      fill=color, outline="#0d1117")
                text = f"{name} ×{count}　{cells}格"
                if tier:
                    text += f"　[{tier}]"
                if price:
                    text += f"　单价{price}"
                self.create_text(pad + _px(18), ly + _px(7), anchor="w", text=text,
                                 fill=FG, font=_font(8))
                ly += _px(17)
            legend_h = ly - (top + h) - _px(8)

        total_h = h + top + pad + (_px(legend_h + 8) if items else 0)
        total_w = w + ox + pad
        self.configure(width=total_w, height=total_h,
                       scrollregion=(0, 0, total_w, total_h))

    def render_placeholder(self, text: str) -> None:
        self.delete("all")
        self._container = None
        self.create_text(_px(12), _px(12), text=text, anchor="nw", fill=MUTED,
                         font=_font(10))
        self.configure(width=_px(460), height=_px(90),
                       scrollregion=(0, 0, _px(460), _px(90)))


# --------------------------------------------------------------------------
# Left pane: loadout
# --------------------------------------------------------------------------


class LoadoutPane(ttk.Frame):
    """胸挂 / 背包 / 消耗品 选择，实时算出回本线。"""

    def __init__(self, master: tk.Misc, app: "CalculatorApp") -> None:
        super().__init__(master, style="Panel.TFrame", padding=10)
        self.app = app
        self._consumable_vars: dict[str, tk.BooleanVar] = {}
        self._qty_vars: dict[str, tk.IntVar] = {}
        self._price_vars: dict[str, tk.IntVar] = {}
        self._building = False

        ttk.Label(self, text="装备与消耗品", style="Title.TLabel").pack(anchor="w")
        ttk.Separator(self).pack(fill="x", pady=6)

        ttk.Label(self, text="胸挂", style="Sub.TLabel").pack(anchor="w")
        self.rig_var = tk.StringVar()
        self.rig_combo = ttk.Combobox(self, textvariable=self.rig_var, state="readonly")
        self.rig_combo.pack(fill="x", pady=(2, 8))
        self.rig_combo.bind("<<ComboboxSelected>>", lambda _e: self.app.schedule_replan())

        ttk.Label(self, text="背包", style="Sub.TLabel").pack(anchor="w")
        self.bag_var = tk.StringVar()
        self.bag_combo = ttk.Combobox(self, textvariable=self.bag_var, state="readonly")
        self.bag_combo.pack(fill="x", pady=(2, 8))
        self.bag_combo.bind("<<ComboboxSelected>>", lambda _e: self.app.schedule_replan())

        self.capacity_label = ttk.Label(self, text="总容量: -", style="Muted.TLabel")
        self.capacity_label.pack(anchor="w", pady=(0, 8))

        ttk.Separator(self).pack(fill="x", pady=6)
        ttk.Label(self, text="消耗品（计入成本）", style="Sub.TLabel").pack(anchor="w")
        self.cons_frame = ttk.Frame(self, style="Panel.TFrame")
        self.cons_frame.pack(fill="x", pady=4)

        ttk.Label(self, text="额外入场成本", style="Muted.TLabel").pack(anchor="w", pady=(6, 0))
        self.travel_var = tk.IntVar(value=0)
        travel = ttk.Entry(self, textvariable=self.travel_var, width=12)
        travel.pack(anchor="w")
        travel.bind("<KeyRelease>", lambda _e: self.app.schedule_replan())

        ttk.Separator(self).pack(fill="x", pady=8)
        self.cost_label = ttk.Label(self, text="回本线: 0", style="Cost.TLabel")
        self.cost_label.pack(anchor="w")

        ttk.Button(self, text="打开数据目录", command=self.app.open_data_dir).pack(
            fill="x", side="bottom", pady=(4, 0)
        )
        ttk.Button(self, text="重新载入 data/", command=self.app.reload_data).pack(
            fill="x", side="bottom", pady=(8, 0)
        )
        ttk.Button(self, text="编辑价目表 / 容器…", command=self.app.open_editor).pack(
            fill="x", side="bottom", pady=(4, 0)
        )

    # -- population --------------------------------------------------------

    def set_data(self, data: GameData) -> None:
        self._building = True
        try:
            # 下拉里显示 shown_name（补全名），但模型/查找仍用权威 name。
            rig_names = [NOT_SELECTED] + [c.shown_name for c in data.chest_rigs]
            bag_names = [NOT_SELECTED] + [c.shown_name for c in data.backpacks]
            self.rig_combo["values"] = rig_names
            self.bag_combo["values"] = bag_names
            # ⚠️ **不**替玩家预设装备。开局两个容器都是空选项，
            # 玩家必须自己选 —— 否则程序等于替他编了一套他没穿的装备，
            # 算出来的回本线/容量全是假的。列表里第一项是空串，
            # 显示为「（未选择）」。
            if self.rig_var.get() not in rig_names:
                self.rig_var.set(NOT_SELECTED)
            if self.bag_var.get() not in bag_names:
                self.bag_var.set(NOT_SELECTED)

            for child in self.cons_frame.winfo_children():
                child.destroy()
            self._consumable_vars.clear()
            self._qty_vars.clear()
            self._price_vars.clear()

            header = ttk.Frame(self.cons_frame, style="Panel.TFrame")
            header.pack(fill="x")
            ttk.Label(header, text="启用", style="Muted.TLabel", width=5).pack(side="left")
            ttk.Label(header, text="名称", style="Muted.TLabel", width=12).pack(side="left")
            ttk.Label(header, text="单价", style="Muted.TLabel", width=7).pack(side="left")
            ttk.Label(header, text="数量", style="Muted.TLabel", width=5).pack(side="left")

            for item in data.consumables:
                row = ttk.Frame(self.cons_frame, style="Panel.TFrame")
                row.pack(fill="x", pady=1)
                enabled = tk.BooleanVar(value=item.enabled)
                price = tk.IntVar(value=item.unit_price)
                qty = tk.IntVar(value=item.quantity)
                self._consumable_vars[item.name] = enabled
                self._price_vars[item.name] = price
                self._qty_vars[item.name] = qty

                ttk.Checkbutton(row, variable=enabled, width=4,
                                command=self.app.schedule_replan).pack(side="left")
                ttk.Label(row, text=item.name, width=12, style="Small.TLabel").pack(side="left")
                pe = ttk.Entry(row, textvariable=price, width=7)
                pe.pack(side="left")
                pe.bind("<KeyRelease>", lambda _e: self.app.schedule_replan())
                qe = ttk.Entry(row, textvariable=qty, width=5)
                qe.pack(side="left", padx=(4, 0))
                qe.bind("<KeyRelease>", lambda _e: self.app.schedule_replan())

            self.travel_var.set(data.travel_cost)
        finally:
            self._building = False

    # -- read out ----------------------------------------------------------

    def build_loadout(self, data: GameData) -> Loadout:
        rig = data.container_by_name(_unselect(self.rig_var.get()))
        bag = data.container_by_name(_unselect(self.bag_var.get()))
        lo = Loadout(
            chest_rig=self.app.effective_container(rig),
            backpack=self.app.effective_container(bag),
        )
        for item in data.consumables:
            var = self._consumable_vars.get(item.name)
            if var is None or not var.get():
                continue
            try:
                price = int(self._price_vars[item.name].get())
                qty = int(self._qty_vars[item.name].get())
            except (tk.TclError, ValueError):
                continue
            if price <= 0 or qty <= 0:
                continue
            lo.add_consumable(Consumable(item.name, price, qty, item.category))
        try:
            lo.travel_cost = max(0, int(self.travel_var.get()))
        except (tk.TclError, ValueError):
            lo.travel_cost = 0
        return lo

    def update_cost(self, loadout: Loadout) -> None:
        free = self.app.raw_capacity()
        locked = free - loadout.capacity
        text = f"总容量: {loadout.capacity} 格"
        if locked:
            text += f"（已屏蔽 {locked} 格，原 {free} 格）"
        self.capacity_label.configure(text=text)
        cost = loadout.total_cost
        detail = " + ".join(f"{c.name}×{c.quantity}" for c in loadout.consumables) or "无消耗品"
        self.cost_label.configure(text=f"回本线: {cost}   （{detail}）")


# --------------------------------------------------------------------------
# Right pane: caught fish
# --------------------------------------------------------------------------


class ScrollArea(ttk.Frame):
    """A vertically scrollable frame (tkinter has none built in).

    Mouse wheel is bound only while the pointer is inside, so the page does not
    steal wheel events from the rest of the app.
    """

    def __init__(self, master: tk.Misc, *, width: int = 300, height: int = 220) -> None:
        super().__init__(master, style="Panel.TFrame")
        self.canvas = tk.Canvas(self, bg=PANEL, highlightthickness=0,
                                width=width, height=height)
        self.bar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.bar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.bar.pack(side="right", fill="y")

        self.body = ttk.Frame(self.canvas, style="Panel.TFrame")
        self._win = self.canvas.create_window((0, 0), window=self.body, anchor="nw")

        self.body.bind("<Configure>", self._on_body)
        self.canvas.bind("<Configure>", self._on_canvas)
        self.canvas.bind("<Enter>", lambda _e: self.canvas.bind_all("<MouseWheel>", self._wheel))
        self.canvas.bind("<Leave>", lambda _e: self.canvas.unbind_all("<MouseWheel>"))

    def _on_body(self, _e=None) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas(self, event) -> None:
        self.canvas.itemconfigure(self._win, width=event.width)

    def _wheel(self, event) -> None:
        self.canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")

    def clear(self) -> None:
        for child in self.body.winfo_children():
            child.destroy()

    def to_top(self) -> None:
        self.canvas.yview_moveto(0.0)


class FishChip(tk.Frame):
    """One fish button: name + price, coloured by rarity tier.

    Left click  = +1 of the default variant (fast, no dialog).
    Right click = menu to pick a specific 款式 (白色/绿色/…/红/典藏).

    The chip is a **species**, not a variant, because that is what the player
    recognises ("我钓到一条罗非鱼"). Which rarity it *is* depends on the colour
    of that particular catch, so the chip shows one small swatch per variant it
    has — click the colour you actually caught via the right-click menu.
    """

    def __init__(self, master: tk.Misc, fish, pane: "CatchPane") -> None:
        tier = fish.tier_info
        super().__init__(master, bg=tier.color, highlightthickness=0)
        self.fish = fish
        self.pane = pane
        pane.register_chip(self, fish.name)

        label = fish.name if len(fish.name) <= 6 else fish.name[:5] + "…"
        self.name_lbl = tk.Label(self, text=label, bg=tier.color, fg=tier.text,
                                 font=("Microsoft YaHei UI", 8, "bold"))
        self.name_lbl.pack(side="left", padx=(5, 2), pady=2)

        # 这一款鱼能钓到的所有色带 —— 玩家一眼看到"这鱼有金有红"
        self.dots: list[tk.Label] = []
        for tid in sorted(fish.tier_ids, key=lambda t: TIER_ORDER.get(t, 99)):
            dot = tk.Label(self, text=" ", bg=TIER_INDEX[tid].color, width=1,
                           highlightthickness=1, highlightbackground="#0d1117")
            dot.pack(side="left", padx=0, pady=3)
            dot._swatch = True          # 见 _hover：色块不跟着变底色
            dot.bind("<Button-1>", self._on_left)
            dot.bind("<Button-3>", self._on_right)
            Tooltip(dot, lambda tt=TIER_INDEX[tid]: f"{tt.label}档款式")
            self.dots.append(dot)

        if fish.collector:
            badge = tk.Label(self, text="藏", bg="#f0d24a", fg="#0d1117",
                             font=("Microsoft YaHei UI", 7, "bold"), padx=2)
            badge.pack(side="left", padx=(2, 0))
            badge.bind("<Button-1>", self._on_left)
            badge.bind("<Button-3>", self._on_right)
            Tooltip(badge, lambda: "有典藏款（不是色带档位，单独标记）")

        # 当前左键会加哪一款 —— 直接写在鱼块上。
        # 不写的话玩家不知道左键加的是金还是红（很容易犯的错：
        # 右键选了红，左键却一直加金）。
        self.pick_lbl = tk.Label(self, text="", bg=tier.color, fg=tier.text,
                                 font=("Microsoft YaHei UI", 7))
        self.pick_lbl.pack(side="left", padx=(0, 2), pady=2)

        self.count_lbl = tk.Label(self, text="", bg=tier.color, fg=tier.text,
                                  font=("Microsoft YaHei UI", 8, "bold"))
        self.count_lbl.pack(side="left", padx=(3, 5), pady=2)

        for w in list(self.winfo_children()):
            if not isinstance(w, tk.Label):
                continue
            w.configure(cursor="hand2")          # 一眼看出可以点
            if not (w is self.count_lbl):
                w.bind("<Enter>", lambda _e: self._hover(True), add="+")
                w.bind("<Leave>", lambda _e: self._hover(False), add="+")
        self.configure(cursor="hand2")
        self.bind("<Enter>", lambda _e: self._hover(True), add="+")
        self.bind("<Leave>", lambda _e: self._hover(False), add="+")
        Tooltip(self, lambda: pane.chip_tip(fish))
        self.refresh()

    # -- interaction -------------------------------------------------------

    def _on_left(self, _event) -> None:
        v = self.pane.default_variant(self.fish)
        if v is None:
            self.pane.warn_no_price(self.fish)
            return
        self.pane.add_variant(v, 1)

    def _on_right(self, event) -> None:
        self.pane.open_variant_menu(self, self.fish, event)

    def _hover(self, on: bool) -> None:
        """Highlight the whole chip, but leave the rarity colour swatches alone."""
        color = PANEL_ALT if on else self.fish.tier_info.color
        self.configure(bg=color)
        for child in self.winfo_children():
            if isinstance(child, tk.Label) and not getattr(child, "_swatch", False):
                child.configure(bg=color)

    def refresh(self) -> None:
        n = self.pane.fish_total(self.fish)
        self.count_lbl.configure(text=f"{n}" if n else "")
        # 把"左键会加哪一款"写在鱼块上：色号 + 尺寸。
        # 玩家不用记、也不用悬停——一眼就知道点下去加的是金还是红。
        active = self.pane.default_variant(self.fish)
        if active is None:
            self.pick_lbl.configure(text="未填价")
            return
        self.pick_lbl.configure(text=f"{active.style}{active.size_label}")
        # 当前款的档位数字加粗显示（用色块本身的高亮边框提示）
        for dot, tid in zip(self.dots,
                            sorted(self.fish.tier_ids,
                                   key=lambda t: TIER_ORDER.get(t, 99))):
            on = tid == active.tier_id
            dot.configure(highlightthickness=2 if on else 1,
                          highlightbackground="#ffffff" if on else "#0d1117")


class Tooltip:
    """Hover label driven by a callable, so the text is always current."""

    def __init__(self, widget: tk.Misc, text_fn) -> None:
        self.widget = widget
        self.text_fn = text_fn
        self.tip: tk.Toplevel | None = None
        widget.bind("<Enter>", self._show, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<Button>", self._hide, add="+")

    def _show(self, _event=None) -> None:
        text = self.text_fn()
        if not text or self.tip is not None:
            return
        x = self.widget.winfo_rootx() + 12
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        self.tip = tk.Toplevel(self.widget)
        self.tip.wm_overrideredirect(True)
        self.tip.wm_geometry(f"+{x}+{y}")
        tk.Label(self.tip, text=text, bg="#0d1117", fg=FG, justify="left",
                 font=("Microsoft YaHei UI", 8), padx=6, pady=3,
                 highlightthickness=1, highlightbackground=GRID_LINE).pack()

    def _hide(self, _event=None) -> None:
        if self.tip is not None:
            self.tip.destroy()
            self.tip = None


class CatchPane(ttk.Frame):
    """右侧栏：按**稀有度分档**的鱼按钮清单，一键记录钓上来的鱼。

    设计意图：真实场景里人是按稀有度认鱼的 —— 白/绿/蓝/紫/金/红。
    所以选鱼不再是"鱼种下拉 → 款式下拉 → 加数量"这三步，而是**一眼看到一片
    彩色方块，点一下就 +1**。款式差异（尺寸/价格）通过右键菜单处理。

    这样求解器拿到的就是"这一趟实际钓上来的鱼"，而不是整个图鉴；
    组合、摆放、收益、成本全都围绕这份清单算。
    """

    #: 右栏宽度：够放 2~3 个鱼块，又不挤压中栏
    PANE_WIDTH = 330

    def __init__(self, master: tk.Misc, app: "CalculatorApp") -> None:
        super().__init__(master, style="Panel.TFrame", padding=10)
        self.app = app
        self.stock: dict[str, int] = {}
        self._data: GameData | None = None
        self._chips: dict[str, list[FishChip]] = {}
        #: 玩家用右键**显式选过**的款式：{鱼种名: 款式全名}。
        #: 有了它，左键才会一直加同一款；见 default_variant 的说明。
        self._picked_variant: dict[str, str] = {}
        self.region_var = tk.StringVar(value=ALL_REGIONS)
        self.bait_var = tk.StringVar(value="")
        self.tier_filter: set[str] = set()          # 空 = 全部显示

        ttk.Label(self, text="钓上来的鱼", style="Title.TLabel").pack(anchor="w")

        # 常驻操作提示。
        # 为什么值得占一行：**稀有度靠右键切**这件事不看说明很难自己发现，
        # 而左键默认加的那一款（最便宜的颜色）经常不是玩家手上的那条。
        # 这一行会随操作变成"左键将加：XX"，玩家一眼就知道点下去会记成什么。
        self.hint = tk.Label(self, text="", bg=PANEL_ALT, fg=FG, justify="left",
                             anchor="w", wraplength=self.PANE_WIDTH - 26,
                             font=_font(8), padx=6, pady=3)
        self.hint.pack(fill="x", pady=(4, 2))

        # -- 地区 + 鱼饵 ----------------------------------------------------
        filt = ttk.Frame(self, style="Panel.TFrame")
        filt.pack(fill="x", pady=(6, 0))
        ttk.Label(filt, text="水域", style="Sub.TLabel").pack(side="left")
        self.region_combo = ttk.Combobox(filt, textvariable=self.region_var,
                                         state="readonly", width=13,
                                         values=[ALL_REGIONS_LABEL])
        self.region_combo.pack(side="left", padx=(4, 0))
        self.region_combo.bind("<<ComboboxSelected>>", lambda _e: self._rebuild_picker())

        self.bait_label = ttk.Label(self, text="", style="Muted.TLabel",
                                    wraplength=self.PANE_WIDTH - 28, justify="left")
        self.bait_label.pack(anchor="w", pady=(3, 0))

        # 全部档位的图例（含 0 个的档）—— 见 _rebuild_picker 里的说明
        self.tier_summary = ttk.Label(self, text="", style="Small.TLabel",
                                      foreground=MUTED)
        self.tier_summary.pack(anchor="w", pady=(2, 0))

        # -- 稀有度快筛 ----------------------------------------------------
        chips = ttk.Frame(self, style="Panel.TFrame")
        chips.pack(fill="x", pady=(6, 2))
        ttk.Label(chips, text="只看", style="Small.TLabel").pack(side="left")
        self._tier_btns: dict[str, tk.Label] = {}
        for t in TIERS:
            if t.id in ("unknown", "collector"):
                continue          # 这两个不是色带档位，不放进"只看"筛选
            lbl = tk.Label(chips, text=t.label, bg=t.color, fg=t.text,
                           font=("Microsoft YaHei UI", 8, "bold"), padx=5, pady=1)
            lbl.pack(side="left", padx=1)
            lbl.bind("<Button-1>", lambda _e, tid=t.id: self._toggle_tier(tid))
            Tooltip(lbl, lambda tt=t: f"只看 / 隐藏「{tt.label}」档")
            self._tier_btns[t.id] = lbl

        ttk.Separator(self).pack(fill="x", pady=4)

        # -- 分区鱼块（可滚动） ---------------------------------------------
        self.picker = ScrollArea(self, width=self.PANE_WIDTH - 26, height=300)
        self.picker.pack(fill="both", expand=True)
        self._update_hint()

        ttk.Separator(self).pack(fill="x", pady=4)

        # -- 已钓清单 -------------------------------------------------------
        head = ttk.Frame(self, style="Panel.TFrame")
        head.pack(fill="x")
        ttk.Label(head, text="已记录", style="Sub.TLabel").pack(side="left")
        self.count_label = ttk.Label(head, text="", style="Small.TLabel")
        self.count_label.pack(side="right")

        self.caught = ScrollArea(self, width=self.PANE_WIDTH - 26, height=120)
        self.caught.pack(fill="x")

        self.totals = ttk.Label(self, text="", style="Cost.TLabel", justify="left")
        self.totals.pack(anchor="w", pady=(4, 0))
        self.status = ttk.Label(self, text="", style="Muted.TLabel", justify="left",
                                wraplength=self.PANE_WIDTH - 22)
        self.status.pack(anchor="w", pady=(2, 0))

        btns = ttk.Frame(self, style="Panel.TFrame")
        btns.pack(fill="x", side="bottom", pady=(6, 0))
        ttk.Button(btns, text="清空", command=self.clear).pack(side="left")
        ttk.Button(btns, text="全部+1", command=self.add_all_one).pack(
            side="left", padx=4, fill="x", expand=True)

    # -- chip registry -----------------------------------------------------

    def register_chip(self, chip: FishChip, species: str) -> None:
        self._chips.setdefault(species, []).append(chip)

    def fish_total(self, fish) -> int:
        return sum(self.stock.get(v.name, 0) for v in fish.variants)

    def default_variant(self, fish) -> FishVariant | None:
        """Which variant a plain **left-click** adds.

        优先级（实际使用逼出来的行为）：

        1. **玩家右键选过的那一款** —— 选了红的就一直是红的。
           曾经没有这一层：右键选红、再左键加，结果又变回金色（因为下面的
           规则 2 会挑"第一个有价的款式"，而幼鲨的款式顺序是 金色→红）。
           表现就是"不管我加多少条红色幼鲨，系统都当金色幼鲨规划"。
        2. 已钓清单里已有的那一款（同一个鱼种别混着加，容易看错）。
        3. 兜底：最便宜的那款（通常就是常见颜色）。
        """
        picked = self._picked_variant.get(fish.name)
        if picked is not None and any(v.name == picked for v in fish.variants):
            for v in fish.priced_variants:
                if v.name == picked:
                    return v

        present = [v for v in fish.priced_variants if self.stock.get(v.name, 0) > 0]
        if present:
            return present[0]

        priced = fish.priced_variants
        if not priced:
            return None
        return min(priced, key=lambda v: v.price)

    def chip_tip(self, fish) -> str:
        active = self.default_variant(fish)
        lines = [f"{fish.name}   （原表标注：{fish.rarity or '无'}）"]
        for v in fish.variants:
            mark = "  ← 左键加这一款" if (active is not None and v.name == active.name) else ""
            lines.append(f"  {v.style:<8} {v.size_label:<5} {v.price or '未填价'}"
                         f"   [{v.tier_info.label}] {v.value_per_cell:.1f}/格{mark}")
        lines.append(f"  产地：{'、'.join(fish.regions) if fish.regions else '未填'}")
        lines.append(f"  推荐钓饵：{fish.bait or '未填'}")
        lines.append("  左键 +1　右键 换款式/减一条")
        return "\n".join(lines)

    def warn_no_price(self, fish) -> None:
        messagebox.showinfo("没有价格", f"「{fish.name}」的款式都还没填价格，无法计入收益。")

    # -- catalogue ---------------------------------------------------------

    def set_data(self, data: GameData) -> None:
        self._data = data
        regions = known_regions(data.fish)
        options = [ALL_REGIONS_LABEL] + regions
        self.region_combo["values"] = options
        if self.region_var.get() not in options:
            self.region_var.set(ALL_REGIONS_LABEL)
        self._rebuild_picker()
        self._rebuild_caught()

    def current_region(self) -> str:
        raw = self.region_var.get()
        return ALL_REGIONS if raw in ("", ALL_REGIONS_LABEL) else raw

    def visible_fish(self) -> list:
        """Species that pass the region filter and the tier quick-filter."""
        if self._data is None:
            return []
        region = self.current_region()
        out = [f for f in self._data.fish if f.in_region(region)]
        if self.tier_filter:
            out = [f for f in out if f.tier_id in self.tier_filter]
        return out

    def visible_variants(self) -> list[FishVariant]:
        """Every priced variant of the currently visible species."""
        return [v for f in self.visible_fish() for v in f.priced_variants]

    def _toggle_tier(self, tier_id: str) -> None:
        if tier_id in self.tier_filter:
            self.tier_filter.discard(tier_id)
        else:
            self.tier_filter.add(tier_id)
        self._rebuild_picker()

    def _rebuild_picker(self) -> None:
        self.picker.clear()
        self._chips.clear()
        if self._data is None:
            return

        region = self.current_region()
        groups = fish_by_tier(self._data.fish, region)
        counts = {t.id: len(groups.get(t.id) or []) for t in TIERS}

        # 空档位不排版（省地方，也避免"点了没反应"的茫然）。
        # 但它们仍然要**被看见** —— 否则玩家不知道是"这个水域没这种鱼"还是
        # "程序坏了"。所以用一行彩色图例把全部档位连同条数摆出来。
        self.tier_summary.configure(
            text="  ".join(
                f"{t.label}{counts[t.id]}" for t in TIERS
                if t.id not in ("unknown", "collector") or counts[t.id]
            )
        )

        shown = 0
        for t in TIERS:
            members = groups.get(t.id) or []
            if not members:
                continue
            if self.tier_filter and t.id not in self.tier_filter:
                continue
            self._section(t, members)
            shown += 1
        if shown == 0:
            tk.Label(self.picker.body, text="（当前筛选下没有鱼）", bg=PANEL,
                     fg=MUTED, font=("Microsoft YaHei UI", 9)).pack(anchor="w", pady=6)

        # 鱼饵推荐 —— 只按当前水域里"能钓到的东西值多少钱"排序
        baits = best_bait(self._data.fish, region, top=3)
        if baits:
            top = "、".join(f"{name}（{score}）" for name, score in baits)
            self.bait_label.configure(text=f"🎣 建议鱼饵：{top}")
        else:
            self.bait_label.configure(
                text="🎣 建议鱼饵：暂无数据（在「编辑数据」里给鱼种填推荐钓饵）")

        # 快筛按钮的选中态
        for tid, lbl in self._tier_btns.items():
            t = TIER_INDEX[tid]
            dim = bool(self.tier_filter) and tid not in self.tier_filter
            lbl.configure(bg=PANEL_ALT if dim else t.color,
                          fg=MUTED if dim else t.text)

        self._refresh_counts()

    def _section(self, tier, members: list) -> None:
        header = tk.Frame(self.picker.body, bg=PANEL)
        header.pack(fill="x", pady=(5, 1))
        tk.Label(header, text=" ", bg=tier.color, width=2).pack(side="left", padx=(0, 4))
        tk.Label(header, text=f"{tier.label}　{len(members)}",
                 bg=PANEL, fg=MUTED, font=("Microsoft YaHei UI", 8, "bold")).pack(side="left")

        wrap = tk.Frame(self.picker.body, bg=PANEL)
        wrap.pack(fill="x")
        col = 0
        for fish in members:
            if col % 2 == 0:
                row = tk.Frame(wrap, bg=PANEL)
                row.pack(fill="x", pady=1)
            FishChip(row, fish, self).pack(side="left", padx=(0, 2))
            col += 1

    def open_variant_menu(self, chip: FishChip, fish, event) -> None:
        """右键菜单：选款式（并记住这一选择）、减一条、清掉这一种。"""
        menu = tk.Menu(self, tearoff=0)
        active = self.default_variant(fish)
        for v in fish.variants:
            label = f"{v.style}  {v.size_label}  {v.price or '未填价'}  [{v.tier_info.label}]"
            if v.hidden:
                label += "  隐藏"
            if active is not None and v.name == active.name:
                label = "● " + label          # 标出左键当前会加哪一款
            else:
                label = "　 " + label
            menu.add_command(
                label=label, state="normal" if v.price > 0 else "disabled",
                # pick_variant 会记住选择：之后左键一直加这一款
                command=lambda vv=v: self.pick_variant(fish, vv, 1))
        if self.stock:
            menu.add_separator()
        menu.add_command(label=f"−1（{fish.name}）",
                         command=lambda: self.add_species(fish, -1))
        menu.add_command(label=f"清掉「{fish.name}」",
                         command=lambda: self.remove_species(fish))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    # -- stock -------------------------------------------------------------

    def add_variant(self, v: FishVariant, n: int = 1) -> None:
        new = self.stock.get(v.name, 0) + n
        if new <= 0:
            self.stock.pop(v.name, None)
        else:
            self.stock[v.name] = new
        self._after_stock_change()

    def pick_variant(self, fish, v: FishVariant, n: int = 1) -> None:
        """右键菜单里"选款式并加一条" —— 顺便**记住**这个选择。

        记住是关键：右键选了红色，之后左键应该继续加红色。
        之前没记住，左键又按"第一个有价款式"来，于是红色永远被顶成金色 ——
        否则"不管选多少条红色幼鲨，系统都视作新增金色幼鲨"。
        """
        self._picked_variant[fish.name] = v.name
        self.add_variant(v, n)

    def add_species(self, fish, n: int) -> None:
        """点鱼块（左键）/ 右键 -1。加哪一款交给 :meth:`default_variant` 决定。"""
        if n < 0:
            present = [v for v in fish.variants if self.stock.get(v.name, 0) > 0]
            if present:
                # 扣掉数量最多的那一款，行为与"撤销"一致
                v = max(present, key=lambda vv: self.stock.get(vv.name, 0))
                self.add_variant(v, -1)
                return
            self._after_stock_change()
            return
        v = self.default_variant(fish)
        if v is None:
            self.warn_no_price(fish)
            return
        self.add_variant(v, 1)

    def remove_species(self, fish) -> None:
        for v in fish.variants:
            self.stock.pop(v.name, None)
        self._after_stock_change()

    def add_all_one(self) -> None:
        """One of every visible fish — handy for trying a whole region."""
        for fish in self.visible_fish():
            v = self.default_variant(fish)
            if v is not None:
                self.stock[v.name] = self.stock.get(v.name, 0) + 1
        self._after_stock_change()

    def bump(self, name: str, delta: int) -> None:
        new = self.stock.get(name, 0) + delta
        if new <= 0:
            self.stock.pop(name, None)
        else:
            self.stock[name] = new
        self._after_stock_change()

    def clear(self) -> None:
        self.stock.clear()
        self._after_stock_change()

    def _after_stock_change(self) -> None:
        self._rebuild_caught()
        self._refresh_counts()
        self._update_hint()
        self.app.schedule_replan()

    # -- 常驻操作提示 ------------------------------------------------------

    def _update_hint(self) -> None:
        """维护右栏那条常驻提示：左键现在会加什么 / 怎么换成别的稀有度。

        「稀有度靠右键切」这件事不看说明很难自己发现，而左键默认加的那一款
        （最便宜的颜色）经常不是玩家手上的那条 —— 所以这条提示既要说清规则，
        也要实时显示"现在点下去会记成哪一款"。
        """
        last: FishVariant | None = None
        if self._data is not None:
            for fish_name, variant_name in self._picked_variant.items():
                fish = next((f for f in self._data.fish if f.name == fish_name), None)
                if fish is None:
                    continue
                found = next((v for v in fish.priced_variants
                              if v.name == variant_name), None)
                if found is not None:
                    last = found          # 用最后一次选定的那款做样例

        if last is not None:
            head = (f"左键将加：{last.species}·{last.style} {last.size_label}"
                    f"  [{last.tier_info.label}]")
            tail = "要换别的稀有度/尺寸 → 右键点鱼块，选菜单里对应那一行"
        else:
            head = "左键点鱼块 = +1（加鱼块上写着的那一款）"
            tail = ("钓到的是【红色 / 典藏 / 其他尺寸】？先右键点鱼块，"
                    "在菜单里选对应那一行再点")
        self.hint.configure(text=f"{head}\n{tail}")

    def _refresh_counts(self) -> None:
        for chips in self._chips.values():
            for chip in chips:
                chip.refresh()
        n = sum(self.stock.values())
        self.count_label.configure(text=f"{len(self.stock)} 款 / {n} 条" if n else "还没有")

    def _rebuild_caught(self) -> None:
        self.caught.clear()
        if not self.stock:
            ttk.Label(self.caught.body, text="（点上面的鱼块记录已钓）",
                      style="Muted.TLabel").pack(anchor="w")
            return

        for name, n in sorted(self.stock.items(), key=lambda kv: -kv[1]):
            row = ttk.Frame(self.caught.body, style="Panel.TFrame")
            row.pack(fill="x", pady=1)
            ttk.Button(row, text="−", width=2,
                       command=lambda nm=name: self.bump(nm, -1)).pack(side="left")
            ttk.Label(row, text=f"{n}", width=3, style="Small.TLabel",
                      anchor="center").pack(side="left")
            ttk.Button(row, text="+", width=2,
                       command=lambda nm=name: self.bump(nm, 1)).pack(side="left")
            ttk.Label(row, text=name, width=17, style="Small.TLabel").pack(
                side="left", padx=(5, 0))

    # -- report ------------------------------------------------------------

    def update_report(self, plan: RunPlan, data: GameData, rep) -> None:
        total = caught_value(data.variants, self.stock)
        packed = rep.packed.value if rep and rep.packed else 0
        lines = [f"已钓价值: {total}",
                 f"可带走: {packed}"]
        if plan.target:
            lines.append(f"回本线: {plan.target}  "
                         f"({'已回本' if packed >= plan.target else '未回本'})")
        if total:
            lines.append(f"损失: {total - packed}")
        self.totals.configure(text="\n".join(lines))

        if rep and rep.unplaced:
            drop = "、".join(f"{k}×{v}" for k, v in sorted(rep.unplaced.items()))
            self.status.configure(text=f"装不下，建议丢弃/吃掉：{drop}")
        elif self.stock:
            self.status.configure(text="全部装得下 ✓")
        else:
            self.status.configure(text="")


# --------------------------------------------------------------------------
# Center pane: results
# --------------------------------------------------------------------------


class ResultPane(ttk.Frame):
    """核算结果 + 装载网格可视化。"""

    def __init__(self, master: tk.Misc, app: "CalculatorApp") -> None:
        super().__init__(master, style="Panel.TFrame", padding=10)
        self.app = app

        ttk.Label(self, text="核算结果", style="Title.TLabel").pack(anchor="w")
        ttk.Separator(self).pack(fill="x", pady=6)

        self.headline = ttk.Label(self, text="正在计算…", style="Headline.TLabel",
                                  justify="left", wraplength=640)
        self.headline.pack(anchor="w")

        self.detail = ttk.Label(self, text="", style="Small.TLabel", justify="left",
                                wraplength=640)
        self.detail.pack(anchor="w", pady=(6, 0))

        # 回本进度（确定值：最高收益 / 回本线）
        self.progress = ttk.Progressbar(self, mode="determinate", maximum=100)
        self.progress.pack(fill="x", pady=(8, 0))

        # 「正在计算」提示条。
        # 为什么必须有：精确装箱是指数搜索，超容量实例会跑好几秒。没有提示的话
        # 玩家分不清「程序在算」和「程序卡死」。用**不确定模式**的进度条
        # （来回滚动的那种）：搜索分支没有可用的"总进度"，
        # 假装一个百分比反而是骗人。
        self.busy = ttk.Frame(self, style="Panel.TFrame")
        self.busy_bar = ttk.Progressbar(self.busy, mode="indeterminate")
        self.busy_bar.pack(side="left", fill="x", expand=True)
        self.busy_label = ttk.Label(self.busy, text="", style="Muted.TLabel")
        self.busy_label.pack(side="left", padx=(8, 0))

        # ---- 已选装备的格子布局: 常驻显示, 实时随选择变化 -------------------
        ttk.Separator(self).pack(fill="x", pady=(10, 4))
        gear = ttk.Frame(self, style="Panel.TFrame")
        gear.pack(fill="x")
        ttk.Label(gear, text="已选装备的格子布局", style="Sub.TLabel").pack(side="left")
        self.gear_summary = ttk.Label(gear, text="", style="Muted.TLabel")
        self.gear_summary.pack(side="left", padx=(8, 0))
        ttk.Button(gear, text="清空屏蔽", width=10,
                   command=self.app.clear_disabled).pack(side="right")
        ttk.Label(gear, text="点击格子可屏蔽", style="Muted.TLabel").pack(
            side="right", padx=(0, 8))

        grid_wrap = ttk.Frame(self, style="Panel.TFrame")
        grid_wrap.pack(fill="both", expand=True, pady=(4, 0))
        self.grid_canvas = tk.Canvas(grid_wrap, bg=PANEL, highlightthickness=0)
        vbar = ttk.Scrollbar(grid_wrap, orient="vertical", command=self.grid_canvas.yview)
        hbar = ttk.Scrollbar(grid_wrap, orient="horizontal", command=self.grid_canvas.xview)
        self.grid_canvas.configure(yscrollcommand=vbar.set, xscrollcommand=hbar.set)
        self.grid_canvas.grid(row=0, column=0, sticky="nsew")
        vbar.grid(row=0, column=1, sticky="ns")
        hbar.grid(row=1, column=0, sticky="ew")
        grid_wrap.rowconfigure(0, weight=1)
        grid_wrap.columnconfigure(0, weight=1)
        self.grid_inner = self.grid_canvas

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=False, pady=(8, 0))
        self.nb = nb

        self.plan_tab = ttk.Frame(nb, style="Panel.TFrame", padding=6)
        nb.add(self.plan_tab, text="最优携带方案")
        self.plan_text = tk.Text(self.plan_tab, bg=PANEL_ALT, fg=FG, relief="flat",
                                 wrap="word", font=("Microsoft YaHei UI", 9), height=11)
        self.plan_text.pack(fill="both", expand=True)
        self.plan_text.configure(state="disabled")

        self.rec_tab = ttk.Frame(nb, style="Panel.TFrame", padding=6)
        nb.add(self.rec_tab, text="拾取推荐")
        self.rec_text = tk.Text(self.rec_tab, bg=PANEL_ALT, fg=FG, relief="flat",
                                wrap="word", font=("Microsoft YaHei UI", 9), height=11)
        self.rec_text.pack(fill="both", expand=True)
        self.rec_text.configure(state="disabled")

    def _set_text(self, widget: tk.Text, content: str) -> None:
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", content)
        widget.configure(state="disabled")

    def update_plan(self, plan: RunPlan, data: GameData, loadout: Loadout, rep) -> None:
        if plan.target <= 0:
            head = f"单趟最高收益 {plan.ceiling}"
            sub = "还没填消耗品成本，回本线为 0。填上饵料/渔具单价后即可推算回本。"
            color = FG
        elif plan.can_break_even:
            head = f"可以回本 — 最高收益 {plan.ceiling}，回本线 {plan.target}"
            # 措辞必须跟着 break_even_exact 走：没被证明就不能写"最少"。
            if not plan.break_even_feasible:
                tag = "回本方案（未通过装箱验证）"
            elif plan.break_even_exact:
                tag = "理论最少"
            else:
                tag = "当前找到的回本方案（未证明理论最少）"
            sub = (f"{tag}：{plan.break_even_count} 条鱼（合计 {plan.break_even_value}），"
                   f"余量 {plan.ceiling - plan.target}")
            color = GOOD
        else:
            head = f"本趟无法回本 — 还差 {plan.shortfall}"
            sub = (f"容器最多带走 {plan.ceiling}，回本线 {plan.target}。"
                   "换更大的背包/胸挂，或减少消耗品。")
            color = BAD
        self.headline.configure(text=head, foreground=color)
        self.detail.configure(text=sub)
        ratio = min(100.0, (plan.ceiling / plan.target * 100) if plan.target else 100.0)
        self.progress.configure(value=ratio)

        index = data.variant_index

        # -- plan tab ------------------------------------------------------
        lines = []
        lines.append(f"【容器】{loadout.capacity} 格  "
                     f"(胸挂 {loadout.chest_rig.capacity if loadout.chest_rig else 0} + "
                     f"背包 {loadout.backpack.capacity if loadout.backpack else 0})")
        lines.append(f"【最优方案】{plan.best_plan.summary()}")
        lines.append(f"  装填率 {plan.best_plan.fill_ratio * 100:.1f}%  "
                     f"空余 {plan.best_plan.free_cells} 格")
        lines.append("")
        counts = sorted(plan.best_plan.counts.items(), key=lambda kv: -kv[1])
        if counts:
            lines.append("  装载清单：")
            for name, n in counts:
                v = index.get(name)
                unit = v.price if v else 0
                size = v.size_label if v else "?"
                lines.append(f"    {name:<18} {size:<6} ×{n:<3} 单价 {unit:>6}  小计 {unit * n:>8}")
        else:
            lines.append("  （没有可用的鱼，请先填价目表与容器）")
        lines.append("")
        if plan.target > 0:
            lines.append("【回本所需的最少组合】")
            if plan.break_even_count:
                if plan.break_even_exact:
                    lines.append("    （(条数, 占格) 已由精确搜索证明为最优）")
                elif plan.break_even_feasible:
                    lines.append("    （未证明理论最少 —— 只保证这组装得下）")
                for v, n in plan.break_even:
                    lines.append(f"    {v.name:<18} {v.size_label:<6} ×{n:<3} "
                                 f"单价 {v.price:>6}  小计 {v.price * n:>8}")
                lines.append(f"    合计 {plan.break_even_value}  (回本线 {plan.target})")
            else:
                lines.append("    在当前容量下凑不出回本金额")
        else:
            lines.append("【回本所需的最少组合】")
            lines.append("    回本线为 0（还没填消耗品成本）。填上饵料/渔具单价后，")
            lines.append("    这里会给出「最少几条什么鱼就能回本」。")
        if rep and rep.stock:
            lines.append("")
            lines.append("【当前已钓上的鱼（按你的输入实时重算）】")
            lines.append(f"    已钓价值 {caught_value(data.variants, rep.stock)}，"
                         f"可带走 {rep.packed.value if rep.packed else 0}")
            if rep.unplaced:
                for name, n in sorted(rep.unplaced.items()):
                    lines.append(f"    装不下 {name} ×{n}")
        for w in plan.warnings:
            lines.append(f"⚠ {w}")
        self._set_text(self.plan_text, "\n".join(lines))

        # -- 已选装备的格子布局 + 放鱼结果 ---------------------------------
        # 网格常驻显示(不放在分页里), 这样一选装备就能立刻看到它的真实格位图;
        # 有钓获时同一个网格立刻变成装载方案。
        self.grid_inner.delete("all")
        containers = loadout.containers
        showing_catch = bool(rep and rep.packed and rep.packed.placements)
        placements = (rep.packed.placements if showing_catch else plan.best_plan.placements)

        # 鱼块颜色 = **该款式的稀有度色**（白/绿/蓝/紫/金/红）。
        #
        # ⚠️ 颜色必须覆盖**所有可能画到网格上的鱼**，不能只按"理论最优方案"的鱼
        # 建表。曾经的写法是 `for name in counts`（counts 来自 best_plan.counts），
        # 而网格在有钓获时画的是 `rep.packed.placements` —— 两条鱼的集合不一致时
        # 颜色就落到兜底色 ACCENT（蓝），表现就是"我钓的红鱼在包里是蓝的、
        # 看不出稀有度"。现在把两边来源合并，外加已钓清单全量兜底。
        color_of: dict[str, str] = {}
        tier_label_of: dict[str, str] = {}

        def _remember(name: str) -> None:
            if name in color_of and color_of[name] != ACCENT:
                return
            v = index.get(name)
            if v is not None:
                color_of[name] = v.tier_info.color
                tier_label_of[name] = v.tier_info.label
            else:
                color_of.setdefault(name, ACCENT)
                tier_label_of.setdefault(name, "?")

        for name, _n in counts:                      # 理论最优方案
            _remember(name)
        for p in placements:                         # 网格上真正会画的
            _remember(p.fish_name)
        for name in (rep.stock if rep else {}):      # 玩家已钓的全部（含装不下的）
            _remember(name)
        if rep and rep.unplaced:
            for name in sorted(rep.unplaced):
                # 装不下的：保留稀有度色，但图例标"装不下"。
                # 不要直接涂成红色 —— 那会丢掉"它是几档"这个信息。
                tier_label_of[name] = "装不下"
                color_of.setdefault(name, BAD)

        per: list[list] = [[] for _ in containers]
        for p in placements:
            if 0 <= p.container_index < len(per):
                per[p.container_index].append(p)

        placed_cells = sum(c.capacity for c in containers)
        locked = sum(len(self.app.disabled_cells.get(c.name, ())) for c in containers)
        self.gear_summary.configure(
            text=(f"{' + '.join(c.kind for c in containers)}  "
                  f"共 {placed_cells} 格（可用格）"
                  + (f"，已屏蔽 {locked} 格" if locked else "")
                  + (f"；当前方案占用 {sum(p.cells for p in placements)} 格"
                     if placements else "")
                  + (f"　[{'已钓获装载' if showing_catch else '理论最优方案'}]"
                     if placements else ""))
        )

        x_cursor = _px(10)
        cell = _px(30)          # 正方形格子的边长，按 DPI 放大
        for idx, cont in enumerate(containers):
            cv = GridCanvas(self.grid_inner, cell=cell)
            cv.bind_toggle(self.app.toggle_cell)
            locked_here = set(self.app.disabled_cells.get(cont.name, ()))
            here = per[idx]
            # 图例数据：颜色 / 鱼名 / 条数 / 占格 / 单价
            tally: dict[str, list[int]] = {}
            for p in here:
                row = tally.setdefault(p.fish_name, [0, 0])
                row[0] += 1
                row[1] += p.cells
            legend = [
                (color_of.get(name, ACCENT), name, n, cells,
                 index[name].price if name in index else None,
                 tier_label_of.get(name, ""))
                for name, (n, cells) in sorted(
                    tally.items(), key=lambda kv: -kv[1][1])
            ]
            used = sum(p.cells for p in here)
            cv.render(
                cont, here, color_of,
                title=f"{cont.kind}: {cont.shown_name}",
                subtitle=(f"{cont.cols}×{cont.rows} 网格 · 可用格 {cont.capacity}"
                          + (f" · 屏蔽 {len(locked_here)} 格" if locked_here else "")
                          + f" · 已放 {used} 格，空 {cont.capacity - used - len(locked_here)} 格"
                          + "　（点击格子可屏蔽/取消屏蔽）"),
                disabled=locked_here,
                legend=legend,
            )
            self.grid_inner.create_window(x_cursor, _px(8), anchor="nw", window=cv)
            x_cursor += max(cont.cols * cell + _px(40), _px(340)) + _px(30)

        if not containers:
            self.grid_inner.create_text(
                _px(16), _px(20), anchor="nw", fill=MUTED, font=_font(10),
                text="还没选择胸挂或背包 —— 在左侧选一个，这里会立刻显示它的格子布局。",
            )
        self.grid_inner.configure(scrollregion=self.grid_inner.bbox("all"))

        # -- recommendations tab -------------------------------------------
        rec = ["【优先拾取（按每格价值从高到低）】", ""]
        rec.append(f"  {'款式':<18}{'尺寸':<7}{'单价':>7}{'占格':>5}{'每格价值':>10}")
        for v, _s in plan.recommendations:
            flag = " 隐藏款" if v.hidden else ""
            rec.append(f"  {v.name:<18}{v.size_label:<7}{v.price:>7}{v.cells:>5}"
                       f"{v.value_per_cell:>10.1f}{flag}")
        if plan.avoid:
            rec.append("")
            rec.append("【不建议占格子（密度过低，先丢这些）】")
            for v in plan.avoid:
                rec.append(f"  {v.name:<18}{v.size_label:<7}{v.price:>7}{v.cells:>5}"
                           f"{v.value_per_cell:>10.1f}")
        self._set_text(self.rec_text, "\n".join(rec))


# --------------------------------------------------------------------------
# App
# --------------------------------------------------------------------------


class CalculatorApp(tk.Tk):
    """Main window. 数据全部来自 ``data/game.json``。"""

    def __init__(self) -> None:
        super().__init__()
        self.title("三角洲行动 · 钓鱼成本核算器")
        self.geometry("1500x900")
        self.minsize(1200, 720)
        self.configure(bg=BG)

        self._setup_style()
        self._build_menu()

        self.data: GameData = self._load()
        self.plan: RunPlan | None = None
        self.report = None
        # 玩家手动屏蔽的格子: {容器名: {格索引}}；这些格完全不参与装箱分配
        self.disabled_cells: dict[str, set[int]] = {}
        self._job: str | None = None
        self._queue: queue.Queue = queue.Queue()
        self._worker: threading.Thread | None = None

        self._build_layout()
        self._apply_data()
        self.after(50, self._drain_queue)
        self.schedule_replan()
        # 新手提示：没勾过「不再提示」就每次都弹。
        self.after(400, self.maybe_show_tutorial)

    def maybe_show_tutorial(self, *, force: bool = False) -> None:
        if not force and not should_show_tutorial(self.data_dir().parent):
            return
        TutorialDialog(self, base_dir=self.data_dir().parent)

    def _build_menu(self) -> None:
        """菜单栏 —— 正规软件该有的入口，也让玩家能主动重看教程。"""
        bar = tk.Menu(self)
        helpm = tk.Menu(bar, tearoff=0)
        helpm.add_command(label="新手提示…", accelerator="F1",
                          command=lambda: self.maybe_show_tutorial(force=True))
        helpm.add_separator()
        helpm.add_command(label="打开数据目录…", command=self.open_data_dir)
        helpm.add_command(label="打开使用说明（README）…", command=self.open_readme)
        helpm.add_separator()
        helpm.add_command(label="关于本软件…", command=self.show_about)
        bar.add_cascade(label="帮助", menu=helpm)
        self.configure(menu=bar)
        self.bind_all("<F1>", lambda _e: self.maybe_show_tutorial(force=True))

    def open_readme(self) -> None:
        """Open the shipped README.md with whatever the OS uses for .md / text."""
        import os
        import sys

        meipass = getattr(sys, "_MEIPASS", None)
        candidates = [
            self.data_dir().parent / "README.md",
            self.data_dir().parent / "使用说明.md",
            Path(meipass) / "README.md" if meipass else None,
            Path(__file__).resolve().parents[2] / "dist_readme.md",
        ]
        for cand in candidates:
            if cand and cand.is_file():
                try:
                    if sys.platform == "win32":
                        os.startfile(cand)  # noqa: S606
                    else:
                        import subprocess
                        subprocess.Popen(
                            ["open" if sys.platform == "darwin" else "xdg-open",
                             str(cand)])
                except OSError as exc:
                    messagebox.showinfo("使用说明", f"{cand}\n\n(打不开: {exc})")
                return
        messagebox.showinfo("使用说明", "没有找到 README.md，可在数据目录里查看。")

    def show_about(self) -> None:
        from . import __version__ as ver
        messagebox.showinfo(
            "关于",
            f"三角洲行动 · 钓鱼成本核算器\n"
            f"版本 {ver}\n\n"
            "功能：按胸挂/背包的真实格子布局，算出这一趟的最高收益、回本线、"
            "以及回本所需的最少鱼数与摆放方案。\n\n"
            "所有价格与容器数据来自本地 data\\ 目录，可自行编辑。\n"
            "本工具为玩家自制，与游戏官方无关。",
        )

    # -- 手动屏蔽格子 ------------------------------------------------------

    def toggle_cell(self, container_name: str, cell_index: int) -> None:
        """点一下格子 = 屏蔽/取消屏蔽；该格从此不参与装箱。"""
        cells = self.disabled_cells.setdefault(container_name, set())
        if cell_index in cells:
            cells.discard(cell_index)
        else:
            cells.add(cell_index)
        if not cells:
            self.disabled_cells.pop(container_name, None)
        self.schedule_replan()

    def effective_container(self, c: Container | None) -> Container | None:
        """容器原件 + 屏蔽层 -> 实际送去装箱的容器。"""
        if c is None:
            return None
        disabled = self.disabled_cells.get(c.name)
        if not disabled:
            return c
        base = c.mask or tuple([1] * (c.cols * c.rows))
        mask = tuple(0 if i in disabled else v for i, v in enumerate(base))
        # ⚠️ groups 和 display_name 都必须原样带上：
        #   * groups      承载"不同颜色的口袋不能互通"这条约束；
        #   * display_name 承载补全后的显示名（权威名可能被源表截断）。
        # 重建容器时漏掉任何一个都会静默退化。
        # （groups 丢了 -> 求解器又能跨口袋；显示名丢了 -> 屏蔽格子后
        # 标题变回「穿山甲通用战…」）。
        return Container(c.name, c.kind, c.cols, c.rows, mask=mask,
                         note=c.note, groups=c.groups,
                         display_name=c.display_name)

    def prune_disabled(self) -> None:
        """丢掉已经不适用的屏蔽记录（换了装备 / 重载数据）。"""
        valid = {c.name: c for c in self.data.chest_rigs + self.data.backpacks}
        for name in list(self.disabled_cells):
            cont = valid.get(name)
            if cont is None:
                del self.disabled_cells[name]
                continue
            base = cont.mask or tuple([1] * (cont.cols * cont.rows))
            self.disabled_cells[name] = {i for i in self.disabled_cells[name] if base[i]}
            if not self.disabled_cells[name]:
                del self.disabled_cells[name]

    def clear_disabled(self) -> None:
        self.disabled_cells.clear()
        self.schedule_replan()

    def raw_capacity(self) -> int:
        """未屏蔽前的总容量（用于提示"原有多少格"）。"""
        total = 0
        for c in (self.data.container_by_name(_unselect(self.left.rig_var.get())),
                  self.data.container_by_name(_unselect(self.left.bag_var.get()))):
            if c is not None:
                total += c.capacity
        return total


    # -- data --------------------------------------------------------------

    def data_dir(self):
        """可写数据目录 —— 冻结成 exe 后是「exe 旁边的 data\\」。

        绝不能用 ``Path(__file__)``：打包后它指向 PyInstaller 解包出来的
        只读临时目录，编辑器一保存就会被丢弃（下次启动还原成打包时的版本）。
        """
        from .repository import ensure_data_dir

        return ensure_data_dir()

    def _load(self) -> GameData:
        from .repository import load_game_data

        return load_game_data(self.data_dir())

    def _apply_data(self) -> None:
        self.left.set_data(self.data)
        self.right.set_data(self.data)
        warns = _condense_warnings(self.data.warnings)
        self.banner.configure(
            text=("⚠ " + " ｜ ".join(warns)) if warns else "",
            foreground=WARN if warns else MUTED,
        )

    def reload_data(self) -> None:
        try:
            self.data = self._load()
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("载入失败", str(exc))
            return
        self.prune_disabled()
        self._apply_data()
        self.schedule_replan()

    def open_data_dir(self) -> None:
        """Reveal the writable data folder in Explorer.

        打包成 exe 后数据目录不再在源码树里，玩家很难猜到自己该改哪个文件；
        这个按钮直接把它打开，并顺便告诉他编辑器保存的就是这些文件。
        """
        import os
        import subprocess
        import sys

        ddir = self.data_dir()
        try:
            if sys.platform == "win32":
                os.startfile(ddir)  # noqa: S606 - 打开自己给出的目录
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(ddir)])
            else:
                subprocess.Popen(["xdg-open", str(ddir)])
        except OSError as exc:
            messagebox.showinfo("数据目录", f"{ddir}\n\n(无法自动打开: {exc})")

    # -- chrome ------------------------------------------------------------

    def _setup_style(self) -> None:
        st = ttk.Style(self)
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        st.configure(".", background=PANEL, foreground=FG, font=("Microsoft YaHei UI", 9))
        st.configure("TFrame", background=PANEL)
        st.configure("Panel.TFrame", background=PANEL)
        st.configure("TLabel", background=PANEL, foreground=FG)
        st.configure("Title.TLabel", font=("Microsoft YaHei UI", 12, "bold"), foreground=FG)
        st.configure("Sub.TLabel", font=("Microsoft YaHei UI", 9, "bold"), foreground=MUTED)
        st.configure("Muted.TLabel", foreground=MUTED)
        st.configure("Small.TLabel", font=("Microsoft YaHei UI", 8))
        st.configure("Cost.TLabel", font=("Microsoft YaHei UI", 11, "bold"), foreground=WARN)
        st.configure("Headline.TLabel", font=("Microsoft YaHei UI", 14, "bold"))
        st.configure("TButton", background=PANEL_ALT, foreground=FG)
        st.map("TButton", background=[("active", ACCENT)])
        st.configure("TCombobox", fieldbackground=PANEL_ALT, background=PANEL_ALT, foreground=FG)
        st.configure("TCheckbutton", background=PANEL, foreground=FG)
        st.map("TCheckbutton", background=[("active", PANEL)])
        st.configure("TNotebook", background=PANEL, borderwidth=0)
        st.configure("TNotebook.Tab", background=PANEL_ALT, foreground=MUTED, padding=(10, 5))
        st.map("TNotebook.Tab", background=[("selected", PANEL)], foreground=[("selected", FG)])
        st.configure("Horizontal.TProgressbar", background=ACCENT, troughcolor=PANEL_ALT)
        st.configure("TEntry", fieldbackground=PANEL_ALT, foreground=FG)
        st.configure("Treeview", background=PANEL_ALT, fieldbackground=PANEL_ALT,
                     foreground=FG, rowheight=20)
        st.configure("Treeview.Heading", background=PANEL, foreground=MUTED)

    def _build_layout(self) -> None:
        outer = ttk.Frame(self, style="Panel.TFrame", padding=8)
        outer.pack(fill="both", expand=True)

        self.left = LoadoutPane(outer, self)
        self.left.pack(side="left", fill="y", padx=(0, 8))

        self.right = CatchPane(outer, self)
        self.right.pack(side="right", fill="y", padx=(8, 0))
        self.right.configure(width=260)

        self.center = ResultPane(outer, self)
        self.center.pack(side="left", fill="both", expand=True)

        self.banner = ttk.Label(outer, text="", style="Muted.TLabel", wraplength=1200,
                                justify="left")
        self.banner.pack(side="bottom", fill="x", pady=(6, 0))

    # -- planning ----------------------------------------------------------

    def schedule_replan(self) -> None:
        """Debounce input changes so typing does not spawn a solve per keystroke."""
        if self.left._building:
            return
        if self._job is not None:
            self.after_cancel(self._job)
        self._job = self.after(180, self.replan)

    # -- busy indicator ----------------------------------------------------
    #
    # 精确装箱是指数搜索：超容量的实例（比如背包塞了上百格）会老老实实跑几秒
    # 才返回。没有提示的时候玩家无法区分「程序在算」和「程序卡死」。
    # 这里用**不确定模式**的进度条 + 一句阶段文字；不编造百分比，
    # 因为分支限界搜索没有一个可用的"总任务量"。
    def set_busy(self, stage: str | None) -> None:
        if stage:
            self._busy_label = stage
            self.center.busy_label.configure(text=stage)
            if not self.center.busy.winfo_ismapped():
                self.center.busy.pack(fill="x", pady=(4, 0),
                                      before=self.center.progress)
            try:
                self.center.busy_bar.start(12)
            except tk.TclError:
                pass
        else:
            try:
                self.center.busy_bar.stop()
            except tk.TclError:
                pass
            self.center.busy.pack_forget()

    def _stage(self, text: str) -> None:
        """Called **from the worker thread** — must not touch tkinter at all.

        ⚠️ 这里曾经写成 ``self.after(0, ...)``，是个严重 bug：
        tkinter 不是线程安全的，从子线程调 ``after()`` 会抛
        ``RuntimeError: main thread is not in main loop``。
        那个异常在工作线程里被 except 吞掉，于是**每次计算都在第一步就崩**，
        ``self.plan`` 永远是 None，中栏只能继续显示上一次的残留内容 ——
        表现就是"我选的是 1x5 红幼鲨，背包里却显示 1x4 金幼鲨"。

        正确做法：只把消息塞进队列，由主线程的 ``_drain_queue`` 去更新界面。
        队列是线程安全的，这是唯一的跨线程通道。
        """
        self._queue.put(("stage", text, None, None, None))

    def replan(self) -> None:
        self._job = None
        loadout = self.left.build_loadout(self.data)
        self.left.update_cost(loadout)
        stock = dict(self.right.stock)
        data = self.data
        variants = list(data.variants)
        has_containers = bool(loadout.containers)

        def work() -> None:
            try:
                if has_containers:
                    self._stage("正在搜索最优携带方案…")
                plan = plan_run(loadout, variants, progress=self._stage)
                rep = (report_catch(loadout, variants, stock, progress=self._stage)
                       if stock else None)
                self._queue.put(("ok", plan, rep, loadout, data))
            except Exception as exc:  # noqa: BLE001
                self._queue.put(("err", exc, None, None, None))

        self._worker = threading.Thread(target=work, daemon=True)
        self._worker.start()

    def _drain_queue(self) -> None:
        try:
            while True:
                kind, plan, rep, loadout, data = self._queue.get_nowait()
                if kind == "stage":
                    # 工作线程只负责报告"在算哪一步"；界面更新一律在主线程做。
                    self.set_busy(plan)
                    continue
                if kind == "ok":
                    self.plan = plan
                    self.report = rep
                    self.center.update_plan(plan, data, loadout, rep)
                    self.right.update_report(plan, data, rep)
                else:
                    self.center.headline.configure(text=f"计算出错: {plan}", foreground=BAD)
                self.set_busy(None)
        except queue.Empty:
            pass
        self.after(80, self._drain_queue)

    # -- editor ------------------------------------------------------------

    def open_editor(self) -> None:
        DataEditor(self, self.data, on_saved=self.reload_data)


# --------------------------------------------------------------------------
# Data editor
# --------------------------------------------------------------------------


class DataEditor(tk.Toplevel):
    """Edit the price table (species → variants) and container capacities.

    Exists because the price sheet's fish names could not be OCR'd reliably —
    this is where they get fixed, without hand-editing JSON.
    """

    def __init__(self, master: CalculatorApp, data: GameData, on_saved) -> None:
        super().__init__(master)
        self.title("编辑数据 — 价目表 / 容器")
        self.geometry("1040x680")
        self.configure(bg=BG)
        self.data = data
        self.on_saved = on_saved
        self._species_idx: int | None = None

        outer = ttk.Frame(self, style="Panel.TFrame", padding=8)
        outer.pack(fill="both", expand=True)
        nb = ttk.Notebook(outer)
        nb.pack(fill="both", expand=True)

        # --- price tab: species | variants --------------------------------
        tab = ttk.Frame(nb, style="Panel.TFrame", padding=8)
        nb.add(tab, text="价目表（鱼种 / 款式）")
        ttk.Label(
            tab,
            text="先说清结构：鱼种 → 若干款式，每个款式各有自己的尺寸与价格"
                 "（例如 幼鲨·金色 1X4 1300、幼鲨·红 1X5 2600）。价格填 0 会被装箱忽略。",
            style="Muted.TLabel", wraplength=980, justify="left",
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 6))

        left = ttk.Frame(tab, style="Panel.TFrame")
        left.grid(row=1, column=0, sticky="nsew", padx=(0, 8))
        right = ttk.Frame(tab, style="Panel.TFrame")
        right.grid(row=1, column=1, sticky="nsew")
        tab.columnconfigure(0, weight=1)
        tab.columnconfigure(1, weight=2)
        tab.rowconfigure(1, weight=1)

        ttk.Label(left, text="鱼种", style="Sub.TLabel").pack(anchor="w")
        self.sp_tree = ttk.Treeview(left, columns=("name", "tier", "n"),
                                    show="headings", height=18)
        for c, t, w in (("name", "鱼种", 130), ("tier", "分档", 60), ("n", "款式数", 60)):
            self.sp_tree.heading(c, text=t)
            self.sp_tree.column(c, width=w, anchor="w")
        self.sp_tree.pack(fill="both", expand=True)
        self.sp_tree.bind("<<TreeviewSelect>>", self._on_species_select)
        self.sp_tree.bind("<Double-1>", lambda _e: self._edit_species())

        sb = ttk.Frame(left, style="Panel.TFrame")
        sb.pack(fill="x", pady=(6, 0))
        ttk.Button(sb, text="改名", command=self._edit_species).pack(side="left")
        ttk.Button(sb, text="新增鱼种", command=self._add_species).pack(side="left", padx=3)
        ttk.Button(sb, text="删除", command=self._del_species).pack(side="left")

        ttk.Label(right, text="款式（尺寸与价格）", style="Sub.TLabel").pack(anchor="w")
        self.var_tree = ttk.Treeview(right, columns=("style", "size", "price", "hidden"),
                                     show="headings", height=18)
        for c, t, w in (("style", "款式", 150), ("size", "尺寸", 70),
                        ("price", "价格", 80), ("hidden", "隐藏款", 60)):
            self.var_tree.heading(c, text=t)
            self.var_tree.column(c, width=w, anchor="w")
        self.var_tree.pack(fill="both", expand=True)
        self.var_tree.bind("<Double-1>", lambda _e: self._edit_variant())

        vb = ttk.Frame(right, style="Panel.TFrame")
        vb.pack(fill="x", pady=(6, 0))
        ttk.Button(vb, text="修改款式", command=self._edit_variant).pack(side="left")
        ttk.Button(vb, text="新增款式", command=self._add_variant).pack(side="left", padx=3)
        ttk.Button(vb, text="删除款式", command=self._del_variant).pack(side="left")
        ttk.Button(vb, text="保存价目表", command=self._save_fish).pack(side="right")

        # --- traits tab: 产地 / 鱼饵 / 典藏 ---------------------------------
        self._build_traits_tab(nb)

        # --- container tab -------------------------------------------------
        ctab = ttk.Frame(nb, style="Panel.TFrame", padding=8)
        nb.add(ctab, text="胸挂 / 背包")
        ttk.Label(
            ctab,
            text="按 support/胸挂.jpg 与 support/背包.jpg 的蓝色格子，填正确的宽(cols)与高(rows)。",
            style="Muted.TLabel",
        ).pack(anchor="w", pady=(0, 6))
        self.cont_tree = ttk.Treeview(ctab, columns=("kind", "name", "cols", "rows", "cap"),
                                      show="headings", height=16)
        for c, t, w in (("kind", "类型", 80), ("name", "名称", 260),
                        ("cols", "宽(cols)", 90), ("rows", "高(rows)", 90), ("cap", "容量格", 90)):
            self.cont_tree.heading(c, text=t)
            self.cont_tree.column(c, width=w, anchor="w")
        self.cont_tree.pack(fill="both", expand=True)
        self.cont_tree.bind("<Double-1>", lambda _e: self._edit_container())

        cbar = ttk.Frame(ctab, style="Panel.TFrame")
        cbar.pack(fill="x", pady=(6, 0))
        ttk.Button(cbar, text="修改选中", command=self._edit_container).pack(side="left")
        ttk.Button(cbar, text="新增胸挂",
                   command=lambda: self._add_container("胸挂")).pack(side="left", padx=4)
        ttk.Button(cbar, text="新增背包",
                   command=lambda: self._add_container("背包")).pack(side="left")
        ttk.Button(cbar, text="删除选中", command=self._del_container).pack(side="left", padx=4)
        ttk.Button(cbar, text="保存容器", command=self._save_containers).pack(side="right")

        self._reload_species()
        self._reload_containers()

    # -- lists -------------------------------------------------------------

    def _reload_species(self, keep: int | None = None) -> None:
        self.sp_tree.delete(*self.sp_tree.get_children())
        for i, f in enumerate(self.data.fish):
            self.sp_tree.insert("", "end", iid=str(i),
                                values=(f.name, f.tier_info.label, f.variant_count))
        target = keep if keep is not None else self._species_idx
        if target is not None and 0 <= target < len(self.data.fish):
            self.sp_tree.selection_set(str(target))
            self.sp_tree.see(str(target))
            self._species_idx = target
        elif self.data.fish:
            self.sp_tree.selection_set("0")
            self._species_idx = 0
        self._reload_variants()

    def _reload_variants(self) -> None:
        self.var_tree.delete(*self.var_tree.get_children())
        species = self._current_species()
        if species is None:
            return
        for i, v in enumerate(species.variants):
            self.var_tree.insert("", "end", iid=str(i),
                                 values=(v.style, v.size_label, v.price,
                                         "是" if v.hidden else ""))

    def _reload_containers(self) -> None:
        self.cont_tree.delete(*self.cont_tree.get_children())
        for i, c in enumerate(self.data.chest_rigs + self.data.backpacks):
            self.cont_tree.insert("", "end", iid=str(i),
                                  values=(c.kind, c.name, c.cols, c.rows, c.capacity))

    def _current_species(self) -> Fish | None:
        if self._species_idx is None or not (0 <= self._species_idx < len(self.data.fish)):
            return None
        return self.data.fish[self._species_idx]

    def _on_species_select(self, _event=None) -> None:
        sel = self.sp_tree.selection()
        if sel:
            self._species_idx = int(sel[0])
            self._reload_variants()

    # -- species ops -------------------------------------------------------

    def _edit_species(self) -> None:
        sp = self._current_species()
        if sp is None:
            return
        dlg = _SpeciesDialog(self, sp)
        self.wait_window(dlg)
        if dlg.result:
            sp.name, sp.rarity = dlg.result
            # keep variant names in sync with the renamed species
            sp.variants = [
                FishVariant(f"{sp.name}·{v.style}", sp.name, v.style,
                            v.size_w, v.size_h, v.price, sp.rarity, v.hidden, v.note)
                for v in sp.variants
            ]
            self.data.invalidate()
            self._reload_species()

    def _add_species(self) -> None:
        dlg = _SpeciesDialog(self, Fish("新鱼种", [], "常见"))
        self.wait_window(dlg)
        if dlg.result:
            name, rarity = dlg.result
            self.data.fish.append(Fish(name, [], rarity))
            self.data.invalidate()
            self._reload_species(keep=len(self.data.fish) - 1)

    def _del_species(self) -> None:
        idx = self._species_idx
        if idx is None or not (0 <= idx < len(self.data.fish)):
            return
        sp = self.data.fish[idx]
        if not messagebox.askyesno("确认", f"删除鱼种「{sp.name}」及其 {sp.variant_count} 个款式？"):
            return
        del self.data.fish[idx]
        self.data.invalidate()
        self._species_idx = max(0, idx - 1)
        self._reload_species()

    # -- variant ops -------------------------------------------------------

    def _selected_variant(self) -> FishVariant | None:
        sp = self._current_species()
        sel = self.var_tree.selection()
        if sp is None or not sel:
            return None
        i = int(sel[0])
        return sp.variants[i] if 0 <= i < len(sp.variants) else None

    def _edit_variant(self) -> None:
        sp = self._current_species()
        v = self._selected_variant()
        if sp is None or v is None:
            return
        dlg = _VariantDialog(self, v, sp.name, sp.rarity)
        self.wait_window(dlg)
        if dlg.result:
            i = sp.variants.index(v)
            sp.variants[i] = dlg.result
            self.data.invalidate()
            self._reload_species()

    def _add_variant(self) -> None:
        sp = self._current_species()
        if sp is None:
            messagebox.showinfo("提示", "请先在左边选一个鱼种。")
            return
        dlg = _VariantDialog(self, None, sp.name, sp.rarity)
        self.wait_window(dlg)
        if dlg.result:
            sp.add_variant(dlg.result)
            self.data.invalidate()
            self._reload_species()

    def _del_variant(self) -> None:
        sp = self._current_species()
        v = self._selected_variant()
        if sp is None or v is None:
            return
        sp.variants.remove(v)
        self.data.invalidate()
        self._reload_species()

    def _save_fish(self) -> None:
        self._commit_traits()
        try:
            path = save_fish(self.data.fish, self.data.data_dir)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("保存失败", str(exc))
            return
        messagebox.showinfo("已保存", f"价目表已保存\n{path}")
        self.on_saved()

    # -- traits: tier / regions / bait -------------------------------------

    def _commit_traits(self) -> None:
        """把「分档 / 产地 / 鱼饵」页里的编辑写回 `self.data.fish`。

        这一页用**行内编辑**（Treeview 双击单元格）而不是弹窗：31 行要逐条
        核对的场景下，弹窗一次只能改一个，太慢了。

        注意**没有 tier 列**：稀有度是从款式颜色推导出来的，不是存的字段。
        想改分档就去改款式的颜色（「价目表」页），这样两边永远不会打架。
        """
        if not hasattr(self, "trait_tree"):
            return
        for iid in self.trait_tree.get_children():
            try:
                i = int(iid)
            except ValueError:
                continue
            if not (0 <= i < len(self.data.fish)):
                continue
            fish = self.data.fish[i]
            _name, _tier, regions, bait, collector = self.trait_tree.item(iid, "values")
            fish.regions = _split_regions(regions)
            fish.bait = str(bait).strip()
            fish.collector = str(collector).strip() in ("✓", "是", "1", "true", "True")
        self.data.invalidate()

    def _build_traits_tab(self, nb: ttk.Notebook) -> None:
        tab = ttk.Frame(nb, style="Panel.TFrame", padding=8)
        nb.add(tab, text="产地 / 鱼饵 / 典藏")
        ttk.Label(
            tab,
            text="「分档」这一列是【从款式颜色推导】的（白色→绿→蓝→紫→金→红），"
                 "所以这里不能直接改；要改分档就去「价目表」页改款式颜色。\n"
                 "本页可改：产地（按水域筛选）、推荐钓饵、典藏标记。"
                 "三项都不参与装箱计算，缺了也能用（产地为空 = 任何水域都显示）。",
            style="Muted.TLabel", wraplength=980, justify="left",
        ).pack(anchor="w", pady=(0, 6))

        bar = ttk.Frame(tab, style="Panel.TFrame")
        bar.pack(fill="x", pady=(0, 6))
        ttk.Label(bar, text="批量产地（逗号 / 顿号分隔，留空=清除）：",
                  style="Small.TLabel").pack(side="left")
        self.region_bulk = ttk.Entry(bar, width=30)
        self.region_bulk.pack(side="left", padx=4)
        ttk.Button(bar, text="填入选中的行", command=self._bulk_regions).pack(side="left")
        ttk.Label(bar, text="　批量鱼饵：", style="Small.TLabel").pack(side="left")
        self.bait_bulk = ttk.Entry(bar, width=16)
        self.bait_bulk.pack(side="left", padx=4)
        ttk.Button(bar, text="填入选中的行", command=self._bulk_bait).pack(side="left")

        cols = ("name", "tier", "regions", "bait", "collector")
        self.trait_tree = ttk.Treeview(tab, columns=cols, show="headings", height=20)
        for c, t, w in (("name", "鱼种", 150), ("tier", "分档（推导）", 110),
                        ("regions", "产地（逗号分隔）", 300), ("bait", "推荐钓饵", 160),
                        ("collector", "典藏", 60)):
            self.trait_tree.heading(c, text=t)
            self.trait_tree.column(c, width=w, anchor="w")
        self.trait_tree.pack(fill="both", expand=True)
        self.trait_tree.bind("<Double-1>", self._begin_trait_edit)

        foot = ttk.Frame(tab, style="Panel.TFrame")
        foot.pack(fill="x", pady=(6, 0))
        ttk.Label(
            foot,
            text="提示：分档＝款式颜色，价格随色带单调递增（白 20~30 < 绿 40~60 < "
                 "蓝 80~240 < 紫 160~480 < 金 640~1900 < 红 2600~3800），"
                 "数据本身就是这么组织的。",
            style="Muted.TLabel", wraplength=720, justify="left",
        ).pack(side="left")
        ttk.Button(foot, text="保存全部", command=self._save_all).pack(side="right")

        self._reload_traits()

    def _reload_traits(self) -> None:
        self.trait_tree.delete(*self.trait_tree.get_children())
        for i, f in enumerate(self.data.fish):
            band = f.tier_info.label
            self.trait_tree.insert(
                "", "end", iid=str(i),
                values=(f.name, band, "、".join(f.regions), f.bait,
                        "✓" if f.collector else ""),
            )

    def _begin_trait_edit(self, event) -> None:
        """Inline editor for the editable columns (产地 / 钓饵 / 典藏)."""
        row = self.trait_tree.identify_row(event.y)
        col = self.trait_tree.identify_column(event.x)
        if not row or col not in ("#3", "#4", "#5"):
            return
        idx = int(col[1:]) - 1
        x, y, w, h = self.trait_tree.bbox(row, col)
        value = self.trait_tree.item(row, "values")[idx]

        if col == "#5":       # 典藏 -> 勾选式
            editor: tk.Widget = ttk.Combobox(
                self.trait_tree, state="readonly", width=4, values=["", "✓"])
            editor.set(value)
            editor.bind("<<ComboboxSelected>>",
                        lambda _e: self._commit_trait_edit(row, idx, editor.get()))
        else:
            editor = ttk.Entry(self.trait_tree, width=max(10, w // 9))
            editor.insert(0, value)
            editor.bind("<Return>",
                        lambda _e: self._commit_trait_edit(row, idx, editor.get()))
            editor.bind("<FocusOut>",
                        lambda _e: self._commit_trait_edit(row, idx, editor.get()))
        editor.place(x=x, y=y, width=w, height=h)
        editor.focus_set()

    def _commit_trait_edit(self, row: str, idx: int, value: str) -> None:
        values = list(self.trait_tree.item(row, "values"))
        values[idx] = value
        self.trait_tree.item(row, values=values)
        for child in self.trait_tree.winfo_children():
            if isinstance(child, (ttk.Entry, ttk.Combobox)):
                child.destroy()
        self._commit_traits()

    def _selected_trait_rows(self) -> list[str]:
        return list(self.trait_tree.selection())

    def _bulk_regions(self) -> None:
        rows = self._selected_trait_rows()
        if not rows:
            messagebox.showinfo("批量产地", "先在列表里选中若干行（Ctrl / Shift 多选）。")
            return
        text = self.region_bulk.get().strip()
        for row in rows:
            values = list(self.trait_tree.item(row, "values"))
            values[2] = text
            self.trait_tree.item(row, values=values)
        self._commit_traits()

    def _bulk_bait(self) -> None:
        rows = self._selected_trait_rows()
        if not rows:
            messagebox.showinfo("批量鱼饵", "先在列表里选中若干行（Ctrl / Shift 多选）。")
            return
        text = self.bait_bulk.get().strip()
        for row in rows:
            values = list(self.trait_tree.item(row, "values"))
            values[3] = text
            self.trait_tree.item(row, values=values)
        self._commit_traits()

    def _save_all(self) -> None:
        self._commit_traits()
        try:
            path = save_fish(self.data.fish, self.data.data_dir)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("保存失败", str(exc))
            return
        messagebox.showinfo("已保存", f"产地 / 鱼饵 / 典藏已保存\n{path}")
        self.on_saved()

    # -- container ops -----------------------------------------------------

    def _selected_container(self) -> int | None:
        sel = self.cont_tree.selection()
        return int(sel[0]) if sel else None

    def _all_containers(self) -> list[Container]:
        return self.data.chest_rigs + self.data.backpacks

    def _edit_container(self) -> None:
        idx = self._selected_container()
        if idx is None:
            return
        c = self._all_containers()[idx]
        dlg = _ContainerDialog(self, c)
        self.wait_window(dlg)
        if dlg.result:
            if c in self.data.chest_rigs:
                self.data.chest_rigs[self.data.chest_rigs.index(c)] = dlg.result
            else:
                self.data.backpacks[self.data.backpacks.index(c)] = dlg.result
            self._reload_containers()

    def _add_container(self, kind: str) -> None:
        dlg = _ContainerDialog(self, Container(f"新{kind}", kind, 4, 4))
        self.wait_window(dlg)
        if dlg.result:
            (self.data.chest_rigs if kind == "胸挂" else self.data.backpacks).append(dlg.result)
            self._reload_containers()

    def _del_container(self) -> None:
        idx = self._selected_container()
        if idx is None:
            return
        target = self._all_containers()[idx]
        if target in self.data.chest_rigs:
            self.data.chest_rigs.remove(target)
        else:
            self.data.backpacks.remove(target)
        self._reload_containers()

    def _save_containers(self) -> None:
        try:
            path = save_containers(self.data.chest_rigs, self.data.backpacks, self.data.data_dir)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("保存失败", str(exc))
            return
        messagebox.showinfo("已保存", f"容器已写入\n{path}")
        self.on_saved()


# --------------------------------------------------------------------------
# Small dialogs
# --------------------------------------------------------------------------


class _SpeciesDialog(tk.Toplevel):
    def __init__(self, master: tk.Misc, fish: Fish) -> None:
        super().__init__(master)
        self.title("编辑鱼种")
        self.configure(bg=PANEL)
        self.result: tuple[str, str] | None = None
        self.resizable(False, False)

        self.name_var = tk.StringVar(value=fish.name)
        self.rarity_var = tk.StringVar(value=fish.rarity)

        for i, (label, var) in enumerate((("鱼种名称", self.name_var),
                                          ("稀有度", self.rarity_var))):
            ttk.Label(self, text=label).grid(row=i, column=0, sticky="w", padx=8, pady=4)
            ttk.Entry(self, textvariable=var, width=26).grid(row=i, column=1, padx=8, pady=4)

        bar = ttk.Frame(self, style="Panel.TFrame")
        bar.grid(row=2, column=0, columnspan=2, pady=8)
        ttk.Button(bar, text="确定", command=self._ok).pack(side="left", padx=4)
        ttk.Button(bar, text="取消", command=self.destroy).pack(side="left")

    def _ok(self) -> None:
        name = self.name_var.get().strip()
        if not name:
            messagebox.showerror("格式错误", "鱼种名称不能为空。", parent=self)
            return
        self.result = (name, self.rarity_var.get().strip())
        self.destroy()


class _VariantDialog(tk.Toplevel):
    """Edit one 款式: its style/colour, size and price."""

    def __init__(self, master: tk.Misc, variant: FishVariant | None,
                 species: str, rarity: str) -> None:
        super().__init__(master)
        self.title(f"编辑款式 — {species}")
        self.configure(bg=PANEL)
        self.result: FishVariant | None = None
        self.resizable(False, False)
        self.species = species
        self.rarity = rarity

        self.style_var = tk.StringVar(value=variant.style if variant else "红")
        self.size_var = tk.StringVar(value=variant.size_label if variant else "1x2")
        self.price_var = tk.IntVar(value=variant.price if variant else 0)
        self.hidden_var = tk.BooleanVar(value=variant.hidden if variant else False)

        rows = (
            ("款式（颜色/样式）", self.style_var),
            ("尺寸（如 1x5）", self.size_var),
            ("价格", self.price_var),
        )
        for i, (label, var) in enumerate(rows):
            ttk.Label(self, text=label).grid(row=i, column=0, sticky="w", padx=8, pady=4)
            ttk.Entry(self, textvariable=var, width=26).grid(row=i, column=1, padx=8, pady=4)
        ttk.Checkbutton(self, text="隐藏款", variable=self.hidden_var).grid(
            row=len(rows), column=1, sticky="w", padx=8)

        ttk.Label(self, text=f"所属鱼种：{species}", style="Muted.TLabel").grid(
            row=len(rows) + 1, column=0, columnspan=2, sticky="w", padx=8)

        bar = ttk.Frame(self, style="Panel.TFrame")
        bar.grid(row=len(rows) + 2, column=0, columnspan=2, pady=8)
        ttk.Button(bar, text="确定", command=self._ok).pack(side="left", padx=4)
        ttk.Button(bar, text="取消", command=self.destroy).pack(side="left")

    def _ok(self) -> None:
        try:
            w, _, h = self.size_var.get().lower().replace("×", "x").partition("x")
            size_w, size_h = int(w), int(h)
            if size_w <= 0 or size_h <= 0:
                raise ValueError
            style = self.style_var.get().strip()
            if not style:
                raise ValueError("款式不能为空")
            price = int(self.price_var.get())
        except (ValueError, tk.TclError):
            messagebox.showerror("格式错误", "尺寸请填 1x5 这种格式，价格填整数。", parent=self)
            return
        self.result = FishVariant(
            name=f"{self.species}·{style}", species=self.species, style=style,
            size_w=size_w, size_h=size_h, price=price, rarity=self.rarity,
            hidden=bool(self.hidden_var.get()),
        )
        self.destroy()


class _ContainerDialog(tk.Toplevel):
    def __init__(self, master: tk.Misc, container: Container) -> None:
        super().__init__(master)
        self.title("编辑容器")
        self.configure(bg=PANEL)
        self.result: Container | None = None
        self.resizable(False, False)

        self.name_var = tk.StringVar(value=container.name)
        self.cols_var = tk.IntVar(value=container.cols)
        self.rows_var = tk.IntVar(value=container.rows)

        for i, (label, var) in enumerate((
            ("名称", self.name_var),
            ("宽 cols（列数）", self.cols_var),
            ("高 rows（行数）", self.rows_var),
        )):
            ttk.Label(self, text=label).grid(row=i, column=0, sticky="w", padx=8, pady=4)
            ttk.Entry(self, textvariable=var, width=24).grid(row=i, column=1, padx=8, pady=4)

        self.cap_label = ttk.Label(self, text="", style="Cost.TLabel")
        self.cap_label.grid(row=3, column=0, columnspan=2, pady=4)
        for var in (self.cols_var, self.rows_var):
            var.trace_add("write", lambda *_: self._update_cap())
        self._update_cap()

        bar = ttk.Frame(self, style="Panel.TFrame")
        bar.grid(row=4, column=0, columnspan=2, pady=8)
        ttk.Button(bar, text="确定", command=self._ok).pack(side="left", padx=4)
        ttk.Button(bar, text="取消", command=self.destroy).pack(side="left")

    def _update_cap(self) -> None:
        try:
            self.cap_label.configure(
                text=f"容量 = {int(self.cols_var.get()) * int(self.rows_var.get())} 格"
            )
        except (tk.TclError, ValueError):
            self.cap_label.configure(text="容量 = ?")

    def _ok(self) -> None:
        try:
            cols, rows = int(self.cols_var.get()), int(self.rows_var.get())
            if cols <= 0 or rows <= 0:
                raise ValueError
            name = self.name_var.get().strip() or "未命名容器"
            self.result = Container(name, "容器", cols, rows)
        except (ValueError, tk.TclError):
            messagebox.showerror("格式错误", "宽和高必须是正整数。", parent=self)
            return
        self.destroy()


def main() -> None:
    """Start the calculator with the real data files."""
    CalculatorApp().mainloop()


if __name__ == "__main__":
    main()
