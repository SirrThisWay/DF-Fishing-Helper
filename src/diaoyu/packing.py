"""Grid packing solver: fit caught fish into 胸挂 + 背包 for maximum sale value.

问题模型是 **二维背包（带利润的矩形装箱）**：

* 一个鱼**款式**占 ``w x h`` 格，可 90° 旋转；
* 每个容器（胸挂 / 背包）是**格位图（bitmask）**而不是矩形 —— 真实的胸挂有
  被白缝隔开的独立格，一件 2x2 的鱼跨不过白缝；
* 每个款式有数量上限（玩家实际钓到几条）；
* 最大化装进去的那些款式的售价总和。

对外提供两个精确入口
--------------------
1. :meth:`Packer.pack` —— **最大化价值**（本次钓获怎么装收益最高）。
2. :meth:`Packer.solve_break_even` —— **词典序最小化 (条数, 占格数)**，
   约束是 ``价值 >= 目标``（回本规划用）。

两者共用同一个 DFS；第二个只是多两个约束（``min_value`` / ``max_items``）
外加一个 ``lean`` 目标，**不是另一套求解器**。

算法 —— 对「填充序」做精确 DFS
-------------------------------
取**最左上仍空闲的格** ``a``（锚点）。在任意合法摆法中，``a`` 要么留空，
要么被**恰好一个**矩形覆盖，且该矩形左上角必在 ``a`` 的左上方。
因此穷举这两种情况即穷举全部合法摆法 —— 搜索**完备**，跑完即证明最优。
状态以 ``(region_index, occupied_mask, remaining_supply[, items_left])`` 记忆化。

搜索是 anytime 的：``node_budget`` / ``time_budget`` 是软上限，超出后返回当前
最优解，并把 ``termination`` 标为 ``TIME_LIMIT``（即**未证明最优**）。

改动前必读的三条设计决定
------------------------
1. **搜索直接产出坐标，而不是只产出件数。**
   早期版本只返回件数，再用「大件优先 + 首次适应」重摆一遍。那个重摆过程
   **没有完备性保证**，于是可能出现"报了一个最高价值，但给出的布局实现不了它"。
   现在 memo 里为每个状态记录**决策指针**（选了哪个形状、放在哪里），
   最终沿这条路径回放坐标 —— 布局就是搜索证明过的那个摆法。
   见 ``docs/PACKING_ALGORITHM.md`` §5。

2. **搜索内部刻意不做基于上界的剪枝。**
   曾经加过一次，结果**静默返回错误答案**（真最优 19880 被剪成 19380），
   却仍报 ``optimal=True`` —— 最危险的一类 bug。已撤销。
   重新引入的前提见 ``docs/PACKING_ALGORITHM.md`` §13.5。

3. **词典序用"一个放大目标"表达，而不是跑两遍搜索。**
   ``score = Σ(price·SCALE − cells)``，SCALE 取 2 的幂且远大于任何可能占格数，
   于是最大化 score ≡ 先最大化总价、总价相同时最小化占格。
   注意：**这个等价依赖 SCALE 足够大**，改动 SCALE 时必须重新论证。
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Sequence

from .models import Container, FishVariant


# --------------------------------------------------------------------------
# Termination status — the search stopped why?
# --------------------------------------------------------------------------


class Termination:
    """单值表达"搜索为何停止"。只有 ``OPTIMAL`` 才是"已证明最优"。"""

    OPTIMAL = "OPTIMAL"          # 搜索跑完：最优性得到证明
    TIME_LIMIT = "TIME_LIMIT"    # 触预算：拿到可行解，但不保证最优
    NO_SOLUTION = "NO_SOLUTION"  # 触预算且没有可行解
    INFEASIBLE = "INFEASIBLE"    # 完整搜索证明：约束下无可行解
    EMPTY = "EMPTY"              # 无事可做（没有容器或没有候选）


#: 每放置一件物品获得的基准分。作用是保证"放了东西"永远优于"什么都不放"，
#: 否则在负分目标（cells 模式 = −占格）下，空方案反而得分最高。
#: "件数多"并不会因此压过"占格少" —— 件数由 ``max_items`` 约束钉住，
#: 真正的排序仍由 −cells 决定。
BASE = 1 << 40


# --------------------------------------------------------------------------
# Results
# --------------------------------------------------------------------------


@dataclass(slots=True)
class Placement:
    """一个实例被放进某个容器的具体位置（容器局部格坐标）。"""

    container_index: int
    fish_name: str
    x: int
    y: int
    w: int
    h: int
    price: int
    shape_index: int = -1

    @property
    def cells(self) -> int:
        return self.w * self.h

    def to_dict(self) -> dict:
        return {
            "container": self.container_index,
            "fish": self.fish_name,
            "x": self.x, "y": self.y, "w": self.w, "h": self.h,
            "price": self.price,
        }


@dataclass(slots=True)
class PackResult:
    """一次装箱的结果。"""

    placements: list[Placement] = field(default_factory=list)
    value: int = 0
    used_cells: int = 0
    capacity: int = 0
    termination: str = Termination.EMPTY
    #: 是否满足调用方给的价值下界。``pack`` 不带下界时恒为 True。
    feasible: bool = True
    nodes: int = 0
    seconds: float = 0.0

    @property
    def optimal(self) -> bool:
        """仅当搜索跑完并证明最优时为 True。

        刻意做成**计算属性**而非存储字段：这样它不可能与 ``termination`` 不一致
        —— 那种不一致正是被撤销的那次剪枝 bug 得以隐藏的方式。
        """
        return self.termination == Termination.OPTIMAL

    @property
    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for p in self.placements:
            out[p.fish_name] = out.get(p.fish_name, 0) + 1
        return out

    @property
    def items(self) -> int:
        return len(self.placements)

    @property
    def free_cells(self) -> int:
        return max(0, self.capacity - self.used_cells)

    @property
    def fill_ratio(self) -> float:
        return self.used_cells / self.capacity if self.capacity else 0.0

    def per_container(self, n: int) -> list[list[Placement]]:
        out: list[list[Placement]] = [[] for _ in range(n)]
        for p in self.placements:
            if 0 <= p.container_index < n:
                out[p.container_index].append(p)
        return out

    def summary(self) -> str:
        return (
            f"value={self.value} items={self.items} "
            f"cells={self.used_cells}/{self.capacity} nodes={self.nodes} "
            f"({self.termination})"
        )


@dataclass(slots=True)
class BreakEvenResult:
    """回本方案的精确结果 —— **语义已收口**。

    ``exact`` 是这里最关键的字段：

    * ``exact=True``  —— 「理论最少就是 ``count`` 条」，这一点已被证明；
    * ``exact=False`` —— 只找到一个可行的 ``count`` 条方案，**未证明最少**。

    UI 必须依据 ``exact`` 决定措辞：
    只有 ``exact`` 为真才允许写"理论最少需要 X 条"，
    否则只能写"当前找到的回本方案：X 条（未证明理论最少）"。
    """

    requested: int = 0          # 目标价值（回本线）
    feasible: bool = False      # 是否存在满足约束的布局
    exact: bool = False         # count / used_cells 是否已被证明为最优
    count: int = 0              # 鱼的数量（exact 时为理论最少）
    used_cells: int = 0         # 占格数（exact 时为该条数下的最少）
    value: int = 0              # 该方案的总价值
    placements: list[Placement] = field(default_factory=list)
    lower_bound: int = 0        # 来自 DP 的条数下界（起点）
    termination: str = Termination.EMPTY
    nodes: int = 0
    seconds: float = 0.0

    @property
    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for p in self.placements:
            out[p.fish_name] = out.get(p.fish_name, 0) + 1
        return out


# --------------------------------------------------------------------------
# Internals
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Shape:
    """一个具体的（款式 × 朝向）落位形状。

    相对坐标以元组存储，而**不**预存位掩码：掩码含义取决于被平移进的网格行宽，
    所以同一个矩形在 2 格宽和 3 格宽的网格里掩码不同。
    """

    variant_index: int
    name: str
    w: int
    h: int
    price: int
    cells_xy: tuple[tuple[int, int], ...]
    cells: int
    density: float

    def place(self, shift: int, region_w: int) -> int:
        mask = 0
        for dx, dy in self.cells_xy:
            mask |= 1 << (shift + dy * region_w + dx)
        return mask

    def mask_at(self, x0: int, y0: int, region_w: int) -> int:
        """左上角在 ``(x0, y0)`` 时的掩码。调用方需保证 x0,y0 >= 0。

        显式拒绝负坐标是有意的：负的 x0 会让 ``place`` 出现 ``1 << 负数``，
        Python 直接抛 ``ValueError``。枚举处已用 max(0, ...) 夹住。
        """
        if x0 < 0 or y0 < 0:
            raise ValueError(f"negative placement origin ({x0},{y0})")
        return self.place(y0 * region_w + x0, region_w)


@dataclass(frozen=True, slots=True)
class _Region:
    """一个容器展平成 row-major 位掩码网格。

    ``group_rect`` 是**同色组的"矩形干净版"**（见 :func:`_clean_group_rect`）：
    游戏里胸挂/背包是若干块相邻多格区域（口袋），**不同颜色的组之间不通** ——
    一条鱼不能横跨两个口袋。于是试放一件物品时，除了"格子存在且没被占"，
    还必须"整件落在同一个组里"。

    值为 ``None`` 表示该容器没有可用的组信息，此时退回"只看 mask"的旧行为。
    """

    w: int
    h: int
    mask: int
    capacity: int
    group_rect: tuple[tuple[int, int, int, int, int], ...] | None = None


def _clean_group_rect(
    container: Container,
) -> tuple[tuple[int, int, int, int, int], ...] | None:
    """把容器的组整理成 ``(r0, c0, r1, c1, abs_mask)``，供试放时快速判定。

    做法与理由：

    1. 组本身已由解析器按"同色且**边相邻**"切分 —— 同色但不相邻 = 两个独立组。
    2. 若某组不是整矩形（例如 L 形），**不猜**它的真实形状，直接用它的
       **外接矩形 + 真实格子掩码**。这样只会放宽到"外接矩形内的可用格"，
       绝不会把组外的格子算进来；组内的空洞另有 mask 兜住。
    3. 组的外接矩形之间可能互相重叠（不规则组的常见情况）。物品必须
       同时落在所有相关组里，等价于取**交集**，所以判定时逐个组检查即可。

    ⚠️ ``abs_mask`` 必须是**容器绝对坐标**下的掩码（bit ``r * w + c``），
    因为要和 ``_Shape.mask_at`` 的产物直接按位比较。曾经这里存的是区域坐标
    （``(r-r0) * w + (c-c0)``），两者坐标系不同，判定必然出错。
    """
    groups = getattr(container, "groups", ())
    if not groups:
        return None

    w = container.cols
    mask = 0
    for i, ok in enumerate(container.mask or ()):
        if ok:
            mask |= 1 << i

    rects: list[tuple[int, int, int, int, int]] = []
    for _sw, cells in groups:
        rs, cs = [], []
        own = 0
        for r, c in cells:
            if 0 <= r < container.rows and 0 <= c < w and (mask >> (r * w + c)) & 1:
                rs.append(r)
                cs.append(c)
                own |= 1 << (r * w + c)
        if not rs:
            continue
        rects.append((min(rs), min(cs), max(rs), max(cs), own))
    return tuple(rects) if rects else None


def _shape_cells(w: int, h: int) -> tuple[tuple[int, int], ...]:
    return tuple((i % w, i // w) for i in range(w * h))


def _same_group(region: "_Region", mask_at: int) -> bool:
    """``mask_at``（本次落位的掩码）是否整个落在**同一个组**里。

    这是"不同颜色的组不能互通"这条规则的唯一落点：一条鱼横跨两个口袋
    （例如把 2×3 斜着塞进 4 列宽的背包、一半在左边口袋一半在右边）必须被拒。

    判定：``(mask_at & group_abs_mask) == mask_at`` —— 物品的每一格都在该组内。
    两者都是**容器绝对坐标**掩码，可以直接按位与（坐标系不一致会静默出错，
    见 ``_clean_group_rect`` 的注释）。

    容器没有组信息（``group_rect is None``）时恒为真 —— 退回"只看 mask"的旧行为。
    """
    if region.group_rect is None:
        return True
    for _r0, _c0, _r1, _c1, gmask in region.group_rect:
        if (mask_at & gmask) == mask_at:
            return True
    return False


class Packer:
    """在一组容器上做精确矩形装箱。

    参数
    ----
    containers:
        要填的胸挂 / 背包集合，按填充优先级排序（当前语义是**硬约束**：
        先填满前一个才动下一个，即不做跨容器的最优交换）。
    variants:
        可放置的全部鱼款式。**一个款式一条**，所以数量单位是款式（颜色/样式），
        不是鱼种 —— 同一鱼种的两个款式是两条独立记录。
        参数名沿用历史上的 ``fish``。
    rotate:
        是否允许 90° 旋转。
    node_budget / time_budget:
        软上限，超出后返回当前 incumbent。
    """

    def __init__(
        self,
        containers: Sequence[Container],
        variants: Sequence[FishVariant],
        *,
        rotate: bool = True,
        node_budget: int = 300_000,
        time_budget: float = 3.0,
    ) -> None:
        self.containers = list(containers)
        self.fish = list(variants)          # 每个款式是独立的库存单位
        self.rotate = rotate
        self.node_budget = node_budget
        self.time_budget = time_budget

        slot_names: list[str] = []
        slot_of: list[int] = []
        for item in self.fish:
            if item.name not in slot_names:
                slot_names.append(item.name)
            slot_of.append(slot_names.index(item.name))
        self._slot_of = slot_of
        self._slot_names = slot_names

        self.variants = self._build_variants()
        self._density_order = sorted(
            range(len(self.variants)), key=lambda i: -self.variants[i].density
        )
        self._bounds_cache: dict[int, list[int]] = {}

    # -- 上界（只用于快通道，**绝不**用于剪枝） ---------------------------

    def _bound_table(self, capacity: int) -> list[int]:
        """``UB[s]`` = 用 ``s`` 格最多能装出的价值（忽略形状、忽略数量）。

        标准**分数背包**松弛：按密度降序整件装填，最后一件允许分数。
        其它约束（几何、容器边界、库存）全部丢掉，松弛只会更松 ——
        因此是**真上界**。

        ⚠️ 不要擅自再加一个候选取 min，除非那个候选**也被证明是上界**。
        曾经的做法是 ``min(密度贪心, 只用某一种物品能装多少)``，而第二项是
        **下界**性质的值，于是 min 得到远低于真值的数（真值 7600 时给出 40），
        然后"剪枝"就把最优解剪掉了，还照样报 ``optimal=True``。
        详见 ``docs/PACKING_ALGORITHM.md`` §13。
        """
        if capacity in self._bounds_cache:
            return self._bounds_cache[capacity]

        order = self._density_order
        dens = [self.variants[i].density for i in order]
        maxd = [0.0] * (len(order) + 1)
        for k in range(len(order) - 1, -1, -1):
            maxd[k] = max(dens[k], maxd[k + 1])

        table = [0] * (capacity + 1)
        for s in range(1, capacity + 1):
            frac = 0
            left = s
            for k, vi in enumerate(order):
                if left <= 0:
                    break
                v = self.variants[vi]
                if v.cells <= left:
                    n = left // v.cells
                    frac += n * v.price
                    left -= n * v.cells
                if left > 0 and maxd[k] > 0:
                    frac += int(maxd[k] * left)      # 唯一的松弛项
                    left = 0
            table[s] = frac
        self._bounds_cache[capacity] = table
        return table

    def _fractional_bound(self, capacity: int) -> int:
        return self._bound_table(capacity)[capacity]

    # -- 构造 --------------------------------------------------------------

    def _build_variants(self) -> list[_Shape]:
        out: list[_Shape] = []
        seen: set[tuple[int, int, int]] = set()
        for fi, f in enumerate(self.fish):
            if f.cells <= 0 or f.price <= 0:
                continue
            for w, h in (f.orientations() if self.rotate else ((f.size_w, f.size_h),)):
                if (fi, w, h) in seen:
                    continue
                seen.add((fi, w, h))
                out.append(
                    _Shape(
                        variant_index=fi, name=f.name, w=w, h=h, price=f.price,
                        cells_xy=_shape_cells(w, h), cells=w * h,
                        density=f.price / (w * h),
                    )
                )
        return out

    @staticmethod
    def _region(c: Container) -> _Region:
        mask = 0
        for i, ok in enumerate(c.mask or ()):
            if ok:
                mask |= 1 << i
        return _Region(w=c.cols, h=c.rows, mask=mask, capacity=bin(mask).count("1"),
                       group_rect=_clean_group_rect(c))

    # -- 公开接口 1：最大化价值 -------------------------------------------

    def pack(
        self,
        stock: dict[str, int] | None = None,
        *,
        max_per_type: int | None = None,
        fast: bool = True,
    ) -> PackResult:
        """最大化价值。``stock`` 是 **款式名 -> 可用数量**（与界面产出的键一致）。"""
        # 快通道的前提：不限供给 且 贪心已达分数背包上界 => 贪心即最优
        if fast and stock is None:
            res = self._greedy_pack(max_per_type)
            if res is not None:
                return res

        out = self._search(stock, max_per_type=max_per_type, lean=False)
        return PackResult(
            placements=out.placements, value=out.value, used_cells=out.used_cells,
            capacity=out.capacity, termination=out.termination,
            nodes=out.nodes, seconds=out.seconds,
        )

    def _greedy_pack(self, max_per_type: int | None) -> PackResult | None:
        """不限供给时先试贪心；若已达上界就直接返回（已证明最优）。"""
        started = time.monotonic()
        regions = [self._region(c) for c in self.containers]
        total_capacity = sum(r.capacity for r in regions)
        if total_capacity == 0 or not self.variants:
            return None
        n_slot = len(self._slot_names)
        cap = max_per_type if max_per_type is not None else total_capacity
        rem0 = [0] * n_slot
        for fi, f in enumerate(self.fish):
            rem0[self._slot_of[fi]] = min(cap, total_capacity // max(1, f.cells))

        greedy = self._greedy_placements(regions, list(rem0))
        value = sum(p.price for p in greedy)
        if value >= self._fractional_bound(total_capacity):
            return PackResult(
                placements=greedy, value=value,
                used_cells=sum(p.cells for p in greedy),
                capacity=total_capacity, termination=Termination.OPTIMAL,
                nodes=0, seconds=time.monotonic() - started,
            )
        return None

    # -- 公开接口 2：回本的精确二级优化 ------------------------------------

    def solve_break_even(
        self,
        stock: dict[str, int] | None,
        target: int,
        *,
        lower_bound: int = 0,
        max_probe: int | None = None,
    ) -> BreakEvenResult:
        """**严格**求「价值 ≥ ``target`` 的最少条数方案，同条数下最少占格」。

        目标（审计正式定义）::

            minimize (fish_count, used_cells)
            subject to  total_value >= target
                        packable(layout)
                        stock constraints

        注意 ``total_value`` 是**约束而不是目标** —— 这正是它与
        :meth:`pack`（最大化价值）的区别。价值更高但占格更多的方案，
        在这里**不如**一个刚好达标的省格方案。

        实现 = 三遍精确搜索，全部走同一个 DFS：

        1. **最小条数**：对 ``n = 下界, 下界+1, …`` 判定
           「≤ n 件且价值 ≥ target 是否存在」。
           可行性对 n 单调，所以第一个可行的 n 即理论最少条数。
        2. **该条数下的最小占格**：固定 ``max_items = n``，用
           ``objective="cells"`` 最大化 ``Σ(−cells)``。因为 ``min_value`` 过滤后
           所有入选项都 ≥ target，"占格最少"就是正确的次级目标。
        3. 价值由**第 2 步选出的那个方案**决定，而不是独立再求一次最大值 ——
           否则可能报一个与布局不符的价值（这正是早期版本犯过的错）。

        性能关键：验证第 n 档时把每款式可用量夹到 ``min(库存, n)``。
        这不排除任何解（≤ n 件的方案里单款式用量不可能超过 n），
        却把数量向量压小，使搜索从"爆炸"变为可算。

        时间预算耗尽时返回 ``exact=False`` —— 此时 ``count`` 只是"找到的可行条数"，
        UI **不得**把它写成"理论最少"。
        """
        started = time.monotonic()
        if target <= 0:
            return BreakEvenResult(requested=target, feasible=True, exact=True,
                                   count=0, used_cells=0, value=0,
                                   termination=Termination.OPTIMAL)

        max_possible = self._max_items_available(stock)
        if max_possible <= 0:
            return BreakEvenResult(requested=target, feasible=False, exact=True,
                                   termination=Termination.INFEASIBLE)

        n = max(1, min(lower_bound or 1, max_possible))
        probe_limit = max_probe or max_possible
        nodes_total = 0
        budget_hit = False

        # ---- 第 1 步：最小条数 -------------------------------------------------
        while n <= min(max_possible, probe_limit):
            res = self._search(self._capped_stock(stock, n), lean=False,
                               min_value=target, max_items=n, objective="feasible")
            nodes_total += res.nodes
            if res.termination == Termination.TIME_LIMIT:
                budget_hit = True
                break
            if res.feasible:
                break
            n += 1

        if not budget_hit and (n > min(max_possible, probe_limit) or not res.feasible):
            return BreakEvenResult(
                requested=target, feasible=False, exact=True,
                lower_bound=lower_bound, termination=Termination.INFEASIBLE,
                nodes=nodes_total, seconds=time.monotonic() - started,
            )

        if budget_hit:
            # 触预算：退而求一个"能装下的可行方案"，如实标 exact=False
            fb = self._capped_stock(stock, max_possible)
            fallback = self._search(fb, lean=True, objective="cells",
                                    min_value=target, max_items=None)
            nodes_total += fallback.nodes
            if fallback.feasible:
                return BreakEvenResult(
                    requested=target, feasible=True, exact=False,
                    count=fallback.items, used_cells=fallback.used_cells,
                    value=fallback.value, placements=fallback.placements,
                    lower_bound=lower_bound, termination=Termination.TIME_LIMIT,
                    nodes=nodes_total, seconds=time.monotonic() - started,
                )
            return BreakEvenResult(
                requested=target, feasible=False, exact=False,
                lower_bound=lower_bound, termination=Termination.TIME_LIMIT,
                nodes=nodes_total, seconds=time.monotonic() - started,
            )

        # ---- 第 2 步：该条数下的最小占格 ---------------------------------------
        best_n = n
        lean = self._search(self._capped_stock(stock, best_n), lean=True,
                            objective="cells", min_value=target, max_items=best_n)
        nodes_total += lean.nodes

        # 搜索自称找到了**比下界还少件**的方案 —— 说明第 1 步的条数（时间预算内
        # 逐 n 探到的第一个可行值）并不是真下界。必须回头验证那个更小的 n，
        # 否则会把"证明不了的更少件"当结论报出去。
        if (lean.feasible and lean.termination != Termination.TIME_LIMIT
                and 0 < lean.items < best_n):
            probes = 0
            while lean.items < best_n and probes < 64:
                probes += 1
                best_n = lean.items
                retry = self._search(self._capped_stock(stock, best_n), lean=True,
                                     objective="cells", min_value=target,
                                     max_items=best_n)
                nodes_total += retry.nodes
                if not retry.feasible or retry.termination == Termination.TIME_LIMIT:
                    break
                lean = retry
                if lean.items >= best_n:
                    break

        exact = lean.termination != Termination.TIME_LIMIT
        if lean.feasible:
            return BreakEvenResult(
                requested=target, feasible=True, exact=exact,
                count=lean.items, used_cells=lean.used_cells, value=lean.value,
                placements=lean.placements, lower_bound=lower_bound,
                termination=(Termination.OPTIMAL if exact
                             else Termination.TIME_LIMIT),
                nodes=nodes_total, seconds=time.monotonic() - started,
            )
        # 第 2 步超时但第 1 步已确认存在解 -> 返回第 1 步的可行布局并标 exact=False
        return BreakEvenResult(
            requested=target, feasible=True, exact=False,
            count=res.items, used_cells=res.used_cells, value=res.value,
            placements=res.placements, lower_bound=lower_bound,
            termination=Termination.TIME_LIMIT,
            nodes=nodes_total, seconds=time.monotonic() - started,
        )

    def _capped_stock(self, stock: dict[str, int] | None, cap: int) -> dict[str, int]:
        """把每个款式的可用量夹到 ``cap``。

        用于"求最少 n 条"的验证：一个 ≤ cap 件的方案里单个款式用量不会超过 cap，
        所以夹住不排除任何解，却能把数量向量压小、显著缩小搜索空间。
        """
        return {
            name: (cap if stock is None else min(int(stock.get(name, 0)), cap))
            for name in self._slot_names
        }

    def _max_items_available(self, stock: dict[str, int] | None) -> int:
        """库存里的总件数（不限供给时用容器容量作上限，因为最小尺寸是 1x1）。"""
        if stock is not None:
            return sum(max(0, int(stock.get(n, 0))) for n in self._slot_names)
        total_capacity = sum(self._region(c).capacity for c in self.containers)
        return max(1, total_capacity)

    # -- 共用搜索 ----------------------------------------------------------

    def _search(
        self,
        stock: dict[str, int] | None,
        *,
        max_per_type: int | None = None,
        min_value: int = 0,
        max_items: int | None = None,
        lean: bool = False,
        objective: str = "value",
    ) -> PackResult:
        """共用的精确 DFS。``pack`` 与 ``solve_break_even`` 都走这里。

        ``min_value`` / ``max_items`` 是**硬约束**（不满足则该状态不可行）。

        ``objective`` 决定目标函数，这是**显式**的而不是靠某个放大系数隐式表达：

        * ``"value"``    —— 最大化总价值（``pack`` 用）。
        * ``"cells"``    —— 最大化 ``Σ(−cells)``，即**最小化占格**。
          配合 ``min_value`` 使用时，词典序 ``(价值约束, 占格)`` 才成立；
          单用它会得到"占地最少但不值钱"的方案，所以必须带上 ``min_value``。
        * ``"feasible"`` —— 只判定存在性：一旦够到 ``min_value`` 立刻返回。
          比求最优快得多，用于回本求解的第 1 步「≤n 件是否可行」。

        ``lean`` 是 ``objective="cells"`` 的旧写法，保留兼容；
        新代码请直接用 ``objective``。
        """
        if lean and objective == "value":
            objective = "cells"
        started = time.monotonic()
        regions = [self._region(c) for c in self.containers]
        total_capacity = sum(r.capacity for r in regions)
        if total_capacity == 0 or not self.variants:
            return PackResult(capacity=total_capacity, termination=Termination.EMPTY,
                              seconds=time.monotonic() - started)

        n_slot = len(self._slot_names)
        rem0 = [0] * n_slot
        for fi, f in enumerate(self.fish):
            slot = self._slot_of[fi]
            if stock is not None:
                rem0[slot] = max(0, int(stock.get(f.name, 0)))
            else:
                cap = max_per_type if max_per_type is not None else total_capacity
                rem0[slot] = min(cap, total_capacity // max(1, f.cells))

        # 开解：必须与搜索受**完全相同的约束**，否则贪心会用一个"装了很多件、
        # 但违反件数上限"的方案当 incumbent，随后被判不可行丢弃，搜索只能从零开始。
        # 同时排序规则也要跟着 objective 走：cells 模式按"每格占格"偏好小件，
        # 否则贪心可能给出一个违反 min_value 的方案而被丢弃（曾因此让回本求解
        # 在极小实例上也拿不到 incumbent）。
        if max_items is not None:
            rem0 = [min(c, max_items) for c in rem0]

        # 贪心开解。注意**它的耗时不计入搜索的时间预算**：预算是给"精确搜索"
        # 用的，开解是它自己的前置工作。曾经把 deadline 定在函数入口，于是
        # 在款式多、容器大的实例上贪心就把预算全吃光，搜索第一次展开节点就
        # 撞上过期的 deadline 立刻停摆 —— 表现为"搜索明明该找到解却返回空"、
        # 最终只能退回未证明的贪心解（exact 标记大面积失真）。
        greedy_started = time.monotonic()
        greedy = self._greedy_placements(regions, list(rem0),
                                         lean=(objective == "cells"),
                                         max_items=max_items)
        greedy_seconds = time.monotonic() - greedy_started
        greedy_value = sum(p.price for p in greedy)
        if min_value and greedy_value < min_value:
            greedy = []
            greedy_value = 0

        stats = {"nodes": 0, "stop": False}
        memo: dict[tuple, tuple[int, tuple | None]] = {}
        # 搜索预算从**贪心结束之后**开始计时。
        deadline = time.monotonic() + self.time_budget
        items_left0 = max_items if max_items is not None else -1   # -1 = 不限
        any_feasible = objective == "feasible"

        def shape_score(v: _Shape) -> int:
            # ⚠️ BASE 偏移**只对 cells 目标**合法。
            # cells 模式得分 = BASE − 占格，全为正，所以"放东西"必然优于"什么都不放"。
            # 绝不能给 value 目标加 BASE：那会让"多放一件低价值鱼"的得分高于
            # "少放一件高价值鱼"，直接破坏"最大化总价值"（实测把 150 的最优解
            # 算成了 140）。value 目标只用真实价格。
            if objective == "cells":
                return BASE - v.cells
            return v.price

        def total_score_of(ps: list[Placement]) -> int:
            """cells 模式的判分：``BASE*件数 - 占格``（越大越好）。

            只用于给**剪枝**提供一个"已验证可行"的 incumbent 分数。
            ⚠️ **绝不要**拿它去比较"搜索解 vs 贪心解"：两者件数不同，
            而这个分数里每件都带一个巨大的 ``BASE``，于是"多装几件"会
            凭空赢过"占格更少"，比较结果完全失真。搜索结束后的选择逻辑
            只按 objective 的真实目标（价值 / 占格 / 达标与否）判断。
            """
            if objective == "cells":
                return BASE * len(ps) - sum(p.cells for p in ps)
            return sum(p.price for p in ps)

        def value_bound(rem: tuple[int, ...], left: int, capacity: int) -> int:
            """剩余库存还能贡献的**价值上界**（忽略几何，只看空格数与件数）。

            用法仅限"硬约束是价值 ≥ min_value"的两个目标（``cells`` / ``feasible``）：
            若连这个宽松上界都够不到 ``min_value``，该状态必然不可行，可以整支剪掉。
            它**不参与** ``value`` 目标的最优性推理 —— 那里要的是最大价值本身，
            一个偏大的上界只能少剪枝，不会给出错答案，但绝不会被用来"证明最优"。

            cells 模式**必须**有这道剪枝：该目标只会往"占格最少"的方向走，
            会心安理得地挑一个 1x1 的便宜鱼把格子省下来，然后因为价值不达标
            而被整个丢掉。实测就是它让"1 件 2x3（3000 价值）"这种明明可行的
            实例报 INFEASIBLE，最终只能退回未证明的贪心解。
            """
            prices: list[int] = []
            for v in self.variants:
                n = rem[self._slot_of[v.variant_index]]
                if n <= 0 or v.cells > capacity:
                    continue
                take = n if left < 0 else min(n, left)
                prices.extend([v.price] * take)
            if not prices:
                return 0
            prices.sort(reverse=True)
            take = len(prices) if left < 0 else min(len(prices), left)
            return sum(prices[:take])

        # objective="cells" 的剪枝阈值：来自**已验证可行**的贪心 incumbent。
        # 得分越大越好；继续放置只会让 −占格 更负，但每件都带 +BASE，
        # 所以"这一支最终能达到的分数"就是当前 gained（后面什么都不再放）。
        #
        # ⚠️ 前提是"greedy 非空"。`greedy` 在构造后已经过 min_value 校验，
        # 不达标就被清空成 [] —— 也就是说**空的 greedy 等于"还没有任何达标
        # incumbent"**，此时根本没有可比的分数：若照旧拿一个"价值不达标但
        # 占格很少"的贪心当阈值，合法的达标分支会成片被剪掉（实测把
        # "1 件 2x3 / 3000 价值"的可行实例剪成了 INFEASIBLE）。
        incumbent_score = (total_score_of(greedy)
                           if (objective == "cells" and greedy) else None)

        def _better(v_a: int, s_a: int, c_a: int,
                    v_b: int, s_b: int, c_b: int) -> bool:
            """状态比较器 —— **满足硬约束者优先，其次才是 objective**。

            ``v_*`` = 累计价值，``s_*`` = objective 得分，``c_*`` = 累计占格。

            为什么不能直接比 ``s``：cells 目标的得分是 ``BASE − 占格``，
            跟价格无关。于是"得分最高"的路径**完全可以是一条不达标的路径**
            —— 用 1 件便宜小鱼只占 1 格，得分比"6 格的 3000 价值大鱼"还高。
            若直接比 s，父状态会把这条不达标路径当成 incumbent 留下，随后
            整个返回被判不可行（实测就是它让"1 件 2x3 / 3000 价值"这种
            明明可行的实例报 INFEASIBLE）。

            所以有硬约束时（``min_value > 0``）字典序是

                (是否达标)  然后  cells → 占格少者胜 / value → 得分高者胜

            没有硬约束时（``pack`` 走的就是这条）退回纯 objective 比较。
            平局一律保持原有 incumbent（`_better` 返回 False），避免无谓改动。
            """
            if min_value:
                a_ok = v_a >= min_value
                b_ok = v_b >= min_value
                if a_ok != b_ok:
                    return a_ok          # 只有达标的方案才是候选
                if a_ok and b_ok:
                    if c_a != c_b:
                        return c_a < c_b
                    return False         # 占格相同：保持先到者
                # 两条都不达标：比得分只是给搜索一个方向，不参与最终结论
                return s_a > s_b
            return s_a > s_b

        def solve(ri: int, occ: int, rem: tuple[int, ...], left: int,
                  gained: int = 0, value: int = 0):
            """返回 ``(该状态起的最优得分, 该状态的放置列表, 该路径的总价值)``。

            **直接产出坐标**，不做任何事后回放。这样"搜索证明的东西"与
            "返回的布局"是同一个对象，从根上消除了两者可能不一致的问题。

            第三个返回值（累计价值）是**必需的**，不能事后用价格和反推：
            cells 目标的得分写的是 ``BASE − 占格``，跟价格无关，于是"得分最高
            的那条路径"完全可能是一条**不达标**的路径（用 1 件便宜小鱼占 1 格，
            得分就很漂亮）。没有价值这个维度，父状态就无法判断自己手里的
            incumbent 到底达没达标，也就无法在"达标"这个硬约束下比较 ——
            实测正因如此，明明存在"1 件 2x3 / 3000 价值"的可行解，搜索却
            报 INFEASIBLE 并退回未证明的贪心解。
            """
            if ri >= len(regions):
                return gained, [], value

            # 早退 1：件数用尽 —— 不可能再放任何东西。
            # 否则剩余容器的每个空格都会被"留空"分支白走一遍，
            # 在 max_items 很小时（回本求解常见）会成倍放大状态数。
            if left == 0:
                return gained, [], value

            region = regions[ri]
            free = region.mask & ~occ
            free_n = bin(free).count("1")

            # 早退 2：剩余库存全都放不下当前空闲区的任一格，本区域到此为止。
            # 三个返回值必须与其它出口**同一量纲**（gained 就是"从那之后什么都
            # 不再放"的得分），否则各条路径的分数不可比。
            # 早退：本区域彻底满了，转到下一个容器。
            #
            # ⚠️ 这里**不能**再顺手加一条"剩余库存都放不下就收工"的检查。
            # 曾经写的是
            #     not any(rem[slot] > 0 and v.cells <= free_n for v in variants)
            # 看着挺像"库里没东西装得下就没什么可搜的了"，但它**是错的**：
            # 自由格是**离散**的，局部的空格数与能否放下一件并不等价。
            # 反例（真实数据里天天出现）：2x2 的胸挂放了一件 2x2 的鱼，还剩
            #   3 个自由格，仍有一条 1x1 的鱼能放进去 —— 但 "3 < 4" 会让整个
            # 分支在根上就被剪掉，于是搜索"完整跑完"却得出空布局。
            # 实测它让"胸挂 1 条 + 背包 2 条"这种最普通的实例只装出 1 条，
            # 而且照样报 OPTIMAL。
            #
            # 正确的收工条件由下面的锚点枚举给出：锚点是**最左上**的自由格，
            # 覆盖它的那些矩形本身就是全部候选；一个都放不下时自然什么都搜不到。
            if free_n == 0:
                # ⚠️ 这里必须**进入下一个容器**，而不是直接收工返回！
                # 容器是一个接一个填的（occ 是"当前容器内"的占用掩码），
                # 当前容器填满只代表"本容器结束"，后面还有胸挂/背包没装。
                # 直接 `return` 会让整个搜索在第一个容器填满时就停手 ——
                # 实测那正是"胸挂 1 条 / 背包 0 条"的来源。
                res = solve(ri + 1, 0, rem, left, gained, value)
                return res

            # 剪枝 A（价值下界不可达）：只对"价值 ≥ min_value 是硬约束"的目标生效。
            # 这纯粹是"少搜一点"的剪枝：整支不可行，返回什么都不影响正确性，
            # 只要求调用方不把这次返回当成"找到一个解"（它没有放置列表）。
            if min_value:
                if value + value_bound(rem, left, free_n) < min_value:
                    return 0, [], 0

            # 剪枝 B（仅 cells 模式）。
            #
            # cells 模式的得分是 −占格，**越大越好**，且继续放置只会让分数更负。
            # 所以这一支最终能达到的最好成绩就是"当前已得的 gained"（后面什么都不放）。
            # 于是可安全剪掉的条件是：gained <= incumbent_score（连不放都赢不过）。
            #
            # 注意**不能**写成 `gained <= incumbent_score` 以外的东西：
            # 曾经写成直接比较"本路径已得分数"与"全局最优分数"，
            # 那是错的 —— 本路径已得的 −4（=4 格）确实优于全局的 −5（=5 格），
            # 但继续放下去就再也改进不了，所以这一支整体无望。
            # 判据必须落在"最终最佳成绩 vs 全局最优"上。
            if incumbent_score is not None and gained <= incumbent_score:
                stats["pruned"] = stats.get("pruned", 0) + 1
                return 0, [], 0

            key = (objective, ri, occ, rem, left)
            cached = memo.get(key)
            if cached is not None:
                return cached[0], list(cached[1]), cached[2]

            if free == 0:
                res = solve(ri + 1, 0, rem, left, gained, value)
                memo[key] = (res[0], tuple(res[1]), res[2])
                return res

            if stats["stop"]:
                memo[key] = (0, (), 0)
                return 0, [], 0

            stats["nodes"] += 1
            if stats["nodes"] > self.node_budget or (
                stats["nodes"] % 2048 == 0 and time.monotonic() > deadline
            ):
                stats["stop"] = True

            free_cells = bin(free).count("1")
            anchor = (free & -free).bit_length() - 1
            ax, ay = anchor % region.w, anchor // region.w
            rmask, rw, rh = region.mask, region.w, region.h

            # 选项 1：锚点留空
            best_score, best_list, best_value = solve(
                ri, occ | (1 << anchor), rem, left, gained, value)
            best_cells = sum(p.cells for p in best_list)

            # 选项 2：用某个矩形覆盖锚点
            for vi, v in enumerate(self.variants):
                slot = self._slot_of[v.variant_index]
                if rem[slot] == 0 or v.cells > free_cells:
                    continue
                x_lo = max(0, ax - v.w + 1)
                y_lo = max(0, ay - v.h + 1)
                for x0 in range(x_lo, ax + 1):
                    if x0 + v.w > rw:
                        continue
                    for y0 in range(y_lo, ay + 1):
                        if y0 + v.h > rh:
                            continue
                        placed = v.mask_at(x0, y0, rw)
                        if placed & ~rmask or placed & occ:
                            continue
                        # 不同颜色的组不能互通：整件必须落在同一个口袋里
                        if not _same_group(region, placed):
                            continue
                        nrem = list(rem)
                        nrem[slot] -= 1
                        scored = shape_score(v)
                        sub, sub_list, sub_value = solve(
                            ri, occ | placed, tuple(nrem),
                            left - 1 if left > 0 else left,
                            gained + scored, value + v.price)
                        cand = scored + sub
                        # ⚠️ 累计价值**只取子状态返回的那一个**：子调用传进去的
                        # 就是 ``value + v.price``，再额外加一次就会把本件的价格
                        # 记两遍（实测让 900 变成 1800，直接判错达标与否）。
                        cand_value = sub_value
                        cand_cells = v.cells + sum(p.cells for p in sub_list)
                        if _better(cand_value, cand, cand_cells,
                                   best_value, best_score, best_cells):
                            best_score = cand
                            best_list = [Placement(
                                container_index=ri, fish_name=v.name,
                                x=x0, y=y0, w=v.w, h=v.h, price=v.price,
                                shape_index=vi,
                            )] + sub_list
                            best_value = cand_value
                            best_cells = cand_cells
                            # 存在性模式：够到 min_value 就立刻收工
                            if any_feasible and best_value >= min_value:
                                memo[key] = (best_score, tuple(best_list), best_value)
                                return best_score, best_list, best_value

            memo[key] = (best_score, tuple(best_list), best_value)
            return best_score, best_list, best_value

        total_score, search_placements, search_value = solve(
            0, 0, tuple(rem0), items_left0, 0, 0)

        placements = search_placements
        greedy_won = False
        s_value = search_value
        g_value = sum(p.price for p in greedy)

        # 搜索**跑完了**（stats["stop"] 为假）-> 它的结论在自己的量纲里就是答案。
        # 特别是 cells 目标 + min_value 时，返回的每一个解都已满足该条数下
        # 占格最少，"和贪心一样好"只可能是另一条同样最优的路径，不该因此
        # 把一个已证明的结论降级成"未证明"（这一点曾让 exact 标记大面积失真）。
        #
        # 只有在搜索没跑完（预算耗尽）时，才需要真刀真枪地比较谁更好：
        #   * 搜索没有可行解        -> 用贪心；
        #   * 贪心可达标 / 搜索不可达标 -> 用贪心；
        #   * 两边都达标            -> 先比价值（这是 value 目标的目标函数），
        #     价值相同再比件数（占格是"同条数内"的次级目标）。
        # ⚠️ 只比件数是不够的：贪心可能会用一个"少一件、但总价更低"的方案，
        # 那样等于主动把价值换掉 —— 实测让满仓实例从 1950 掉到 1390。
        # 注意贪心在构造时就已经过 min_value 校验：不达标就被清空成 []，
        # 所以"greedy 非空"即"greedy 可达标"。
        if greedy and stats["stop"]:
            if not search_placements:
                placements, greedy_won = greedy, True
            elif s_value < min_value <= g_value:
                placements, greedy_won = greedy, True
            elif s_value >= min_value and g_value >= min_value:
                if (g_value, -len(greedy)) > (s_value, -len(search_placements)):
                    placements, greedy_won = greedy, True

        value = sum(p.price for p in placements)
        used = sum(p.cells for p in placements)
        feasible = value >= min_value
        if not feasible and greedy:
            # 兜底：搜索给的方案没达 min_value（例如 cells 模式下找了省格但不值钱的解，
            # 或者它压根没找到任何达标布局）-> 用经过校验的可行 incumbent。
            placements, value = greedy, g_value
            used = sum(p.cells for p in placements)
            feasible = value >= min_value

        # 终止状态只看**预算是否耗尽**，而不是"最终用了谁的解"：
        #   * 预算耗尽（stats["stop"]）  -> 结论不可靠，只能说 LIMIT；
        #   * 搜索跑完（stats["stop"] 为假）-> 结论就是可信的。此时即便最终
        #     采用的是贪心解（两边一样好、或 cells 模式下搜索没找到任何达标
        #     布局而贪心找到了），"该约束下最优"这件事也已经被完整搜索证明过，
        #     不该降级成"未证明"。
        if stats["stop"]:
            term = Termination.TIME_LIMIT if feasible else Termination.NO_SOLUTION
        else:
            term = Termination.OPTIMAL if feasible else Termination.INFEASIBLE
        return PackResult(
            placements=placements, value=value, used_cells=used,
            capacity=total_capacity, termination=term, feasible=feasible,
            nodes=stats["nodes"], seconds=time.monotonic() - started,
        )

    # -- 贪心 --------------------------------------------------------------

    @staticmethod
    def _first_fit(region: _Region, v: _Shape, occ: int) -> tuple[int, int, int] | None:
        for y0 in range(region.h - v.h + 1):
            for x0 in range(region.w - v.w + 1):
                shift = y0 * region.w + x0
                placed = v.place(shift, region.w)
                if placed & ~region.mask or placed & occ:
                    continue
                # 贪心也必须守"不同组的格子不能互通"，否则它会给出一个
                # 搜索根本不接受的 incumbent，兜底解就成了非法布局。
                if not _same_group(region, placed):
                    continue
                return shift, x0, y0
        return None

    def _greedy_placements(self, regions, rem, *, lean: bool = False,
                           max_items: int | None = None) -> list[Placement]:
        """贪心开解 incumbent。

        做法：每一轮在**所有容器 × 所有款式 × 所有可放位置**里选一个最佳放置，
        就地放上去，重复到放不下为止。选择依据就是当前目标：
        ``value`` 目标用每格价值（密度），``cells`` 目标偏好省格的小件。

        ⚠️ 这里必须是**全局最佳优先**，不能写成"按款式顺序逐轮扫一遍"：
        后者一旦某个款式在当前位置放不下就跳过，很容易卡在一个很差的解上
        （实测曾出现"胸挂完全空着、背包只用一半"的荒谬结果）。
        搜索超时时最终返回的就是这个 incumbent，所以它的质量直接决定兜底效果。
        """
        placements: list[Placement] = []
        working = list(rem)
        left = max_items if max_items is not None else -1

        def score(v: _Shape) -> float:
            # 越大越优先。cells 目标偏好"每件占格少"，value 目标偏好"每格价值高"。
            return (-v.cells) if lean else v.density

        remaining = max(0, left) if left > 0 else None
        while remaining != 0:
            best: tuple[float, int, int, _Shape, int, int, int] | None = None
            for ri, region in enumerate(regions):
                occ = self._occ_of(region, placements, ri, region.w)
                for vi, v in enumerate(self.variants):
                    slot = self._slot_of[v.variant_index]
                    if working[slot] == 0:
                        continue
                    spot = self._first_fit(region, v, occ)
                    if spot is None:
                        continue
                    s = score(v)
                    # 平手时偏好大件（更容易把角落填满）
                    key = (s, v.cells)
                    if best is None or key > (best[0], best[3].cells):
                        best = (s, ri, vi, v, spot[0], spot[1], spot[2])
            if best is None:
                break
            _s, ri, vi, v, _shift, x0, y0 = best
            placements.append(Placement(
                container_index=ri, fish_name=v.name, x=x0, y=y0,
                w=v.w, h=v.h, price=v.price, shape_index=vi,
            ))
            working[self._slot_of[v.variant_index]] -= 1
            if remaining is not None:
                remaining -= 1
        return placements

    @staticmethod
    def _occ_of(region: _Region, placements: list[Placement], ri: int,
                _w: int) -> int:
        """累加某个容器里已放下的格子掩码。"""
        occ = 0
        for p in placements:
            if p.container_index != ri:
                continue
            for i in range(p.w * p.h):
                occ |= 1 << ((p.y + i // p.w) * region.w + (p.x + i % p.w))
        return occ


# --------------------------------------------------------------------------
# 便捷包装
# --------------------------------------------------------------------------


def pack_containers(
    containers: Sequence[Container],
    variants: Sequence[FishVariant],
    stock: dict[str, int] | None = None,
    **kwargs,
) -> PackResult:
    """把给定库存（或无限供给）装进 ``containers``。"""
    return Packer(containers, variants, **kwargs).pack(stock)


def plan_packing(
    containers: Sequence[Container],
    variants: Sequence[FishVariant],
    *,
    max_per_type: int | None = None,
    **kwargs,
) -> PackResult:
    """每个款式都充足时的最优方案。"""
    return Packer(containers, variants, **kwargs).pack(None, max_per_type=max_per_type)
