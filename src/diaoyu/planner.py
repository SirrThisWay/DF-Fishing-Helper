"""Cost / break-even planner — 「这次钓鱼怎么才能回本」.

The catalogue is a flat list of **variants** (款式): same fish, different
colour/style, each with its own size and price. That is the level the planner
works at, because size and price both live there.

Given a loadout (消耗品 + 胸挂 + 背包) and the variant list, the planner answers
the three questions the calculator exists for:

1. **最多能带回多少钱** — the best achievable haul value with the current
   containers (unlimited supply), i.e. the ceiling for one run.
2. **能不能回本 / 还差多少** — compare that ceiling with the consumable cost.
3. **回本需要多少条什么鱼** — a minimum-cardinality multiset of variants whose
   total price reaches the cost and which still packs into the containers.

The third one is computed **rigorously and lexicographically**:

    minimize (fish_count, used_cells)   subject to   total_value >= target,
                                                    packable(layout)

by :meth:`Packer.solve_break_even` — an exact DFS that proves the answer, so
``break_even_exact`` can honestly be True. A shape-blind DP over value
(``_dp_min_combo``) still runs, but only to supply a **starting lower bound**
for the item count (``lower_bound``), which prunes the first phase; it is never
the source of the reported answer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Sequence

from .models import Container, FishVariant, Loadout
from .packing import Packer, PackResult

# Value DP is O(max_value * len(variants)); guard against absurd costs.
MAX_DP_VALUE = 2_000_000


@dataclass(slots=True)
class CatchReport:
    """Live state while the player adds the fish they have actually caught."""

    stock: dict[str, int] = field(default_factory=dict)
    packed: PackResult | None = None
    unplaced: dict[str, int] = field(default_factory=dict)

    @property
    def packed_value(self) -> int:
        return self.packed.value if self.packed else 0


@dataclass(slots=True)
class RunPlan:
    """The planner's full answer for one loadout."""

    target: int                                       # 回本线 = 消耗品总成本
    ceiling: int                                      # 单趟理论最高收益
    can_break_even: bool
    shortfall: int                                    # ceiling < target 时的差额
    break_even: list[tuple[FishVariant, int]]         # 回本组合（**已经求解器验证可装**）
    break_even_value: int
    break_even_count: int
    break_even_cells: int                             # 该组合实际占的格数
    break_even_feasible: bool                         # 组合是否通过装箱验证
    best_plan: PackResult                             # 最优携带方案(不限供给)
    recommendations: list[tuple[FishVariant, int]]    # 建议优先拾取(按价值密度)
    avoid: list[FishVariant]                          # 密度过低, 不建议占格子
    break_even_exact: bool = False                    # (条数, 占格) 是否已被证明最优
    warnings: list[str] = field(default_factory=list)

    @property
    def fill_ratio(self) -> float:
        return self.best_plan.fill_ratio

    def summary(self) -> str:
        if self.can_break_even:
            if not self.break_even_feasible:
                tail = "（未通过装箱验证）"
            elif self.break_even_exact:
                tail = "（已证明最少）"
            else:
                tail = "（未证明理论最少）"
            return (
                f"回本线 {self.target}，最高收益 {self.ceiling}，"
                f"最少 {self.break_even_count} 条可达 {self.break_even_value}"
                f"（占 {self.break_even_cells} 格）{tail}"
            )
        return f"回本线 {self.target}，最高收益 {self.ceiling}，还差 {self.shortfall}，本趟无法回本"


def _dp_min_combo(
    variants: Sequence[FishVariant], target: int
) -> tuple[int, int, list[tuple[FishVariant, int]]]:
    """Cheapest multiset of variants reaching ``target`` value.

    Returns ``(count, value, combo)``. Cost is minimised lexicographically as
    (number of fish, total cells) — you want to break even with as few casts as
    possible and leave bag space free.

    ``(count, cells)`` per value state, plus a parent pointer for the rebuild.
    """
    if target <= 0:
        return 0, 0, []

    pool = [v for v in variants if v.price > 0]
    if not pool:
        return 0, 0, []

    max_price = max(v.price for v in pool)
    limit = min(target + max_price, MAX_DP_VALUE)

    INF = (10**9, 10**9)
    best: list[tuple[int, int]] = [INF] * (limit + 1)
    parent: list[tuple[int, int] | None] = [None] * (limit + 1)
    best[0] = (0, 0)

    for v in range(limit + 1):
        cur = best[v]
        if cur == INF:
            continue
        cnt, cells = cur
        for fi, item in enumerate(pool):
            nv = v + item.price
            if nv > limit:
                continue
            cand = (cnt + 1, cells + item.cells)
            if cand < best[nv]:
                best[nv] = cand
                parent[nv] = (v, fi)

    # pick the cheapest state at or above the target
    winner: int | None = None
    for v in range(target, limit + 1):
        if best[v] == INF:
            continue
        if winner is None or best[v] < best[winner]:
            winner = v
    if winner is None:
        return 0, 0, []

    # rebuild
    counts: dict[str, int] = {}
    node = winner
    guard = 0
    while node is not None and node > 0 and guard < 10_000:
        guard += 1
        step = parent[node]
        if step is None:
            break
        prev, fi = step
        counts[pool[fi].name] = counts.get(pool[fi].name, 0) + 1
        node = prev

    combo = [(v, counts[v.name]) for v in pool if counts.get(v.name)]
    return best[winner][0], winner, combo


def _order_by_density(variants: Iterable[FishVariant]) -> list[FishVariant]:
    return sorted(variants, key=lambda v: (-v.value_per_cell, -v.price, v.cells))


def _verified_break_even(
    packer: Packer,
    dp_combo: list[tuple[FishVariant, int]],
    target: int,
    *,
    time_budget: float = 3.0,
) -> tuple[list[tuple[FishVariant, int]], int, int, bool]:
    """Turn the DP's suggested combination into one the solver confirms fits.

    Returns ``(combo, value, cells, feasible)`` where ``combo`` is the multiset
    that was actually **packed** (its composition may equal the DP's, or be a
    reduced version). A third-party audit found that reporting a DP combination
    without verifying geometry could show an impossible plan; this routine is
    the fix — nothing is returned as a break-even plan unless the packer
    produced the layout for it.

    Strategy: try the DP set; if it does not fully fit, drop the largest / lowest
    value-density item and retry, until it fits or nothing is left. Keeping the
    high-density items is what makes a reduced set still reach the target most
    often.
    """
    if not dp_combo or not packer.containers:
        return [], 0, 0, False

    stock = {v.name: n for v, n in dp_combo}
    # order of removal: worst value-per-cell first (and biggest first on ties)
    removal_order: list[str] = []
    for v, _n in sorted(
        dp_combo, key=lambda p: (p[0].value_per_cell, -p[0].cells)
    ):
        removal_order.extend([v.name] * stock[v.name])

    def pack_with(counts: dict[str, int]):
        return packer.pack(dict(counts), max_per_type=None)

    current = dict(stock)
    for _ in range(len(removal_order) + 1):
        wanted = sum(
            next(v for v, _ in dp_combo if v.name == n).price * c
            for n, c in current.items() if c
        )
        res = pack_with(current)
        if res.value >= min(target, wanted) and len(res.placements) > 0:
            got: dict[str, int] = {}
            for p in res.placements:
                got[p.fish_name] = got.get(p.fish_name, 0) + 1
            items = {v.name: v for v, _ in dp_combo}
            combo = [(items[n], c) for n, c in got.items() if n in items]
            combo.sort(key=lambda p: (-p[0].value_per_cell, p[0].name))
            return combo, res.value, res.used_cells, True

        # drop one item and retry
        dropped = False
        for name in removal_order:
            if current.get(name, 0) > 0:
                current[name] -= 1
                if current[name] == 0:
                    del current[name]
                dropped = True
                break
        if not dropped:
            break

    return [], 0, 0, False


def plan_run(
    loadout: Loadout,
    variants: Sequence[FishVariant],
    *,
    rotate: bool = True,
    time_budget: float = 3.0,
    node_budget: int = 300_000,
    avoid_ratio: float = 0.25,
    progress=None,
) -> RunPlan:
    """Build the full plan for one loadout.

    ``avoid_ratio`` marks variants whose value density is below this fraction of
    the best density as "not worth a slot".

    ``progress`` 是可选的 ``callable(stage: str)``，每一步开始时调用一次。
    界面用它来显示"现在在算什么"，从而把**正在计算**和**卡死**区分开 ——
    精确装箱是指数搜索，超容量实例跑几秒很正常，没有提示的话玩家会以为死机。
    """

    def _say(stage: str) -> None:
        if progress is not None:
            try:
                progress(stage)
            except Exception:  # noqa: BLE001 - 提示失败不该影响计算
                pass

    containers: list[Container] = loadout.containers
    warnings: list[str] = []
    if not containers:
        warnings.append("还没有选择胸挂或背包，无法计算携带方案")

    positive = [v for v in variants if v.price > 0]
    if not positive:
        warnings.append("价目表里还没有任何填好价格的款式，请先在「编辑价目表」里补全")

    packer = Packer(
        containers, positive,
        rotate=rotate, node_budget=node_budget, time_budget=time_budget,
    )
    # Step 1: ceiling — best value one run can carry out, unlimited supply.
    _say("正在搜索单趟最高收益…")
    best_plan = packer.pack(None) if containers else PackResult()
    ceiling = best_plan.value

    # Step 2: break-even comparison.
    target = loadout.total_cost
    can = ceiling >= target and target > 0
    shortfall = max(0, target - ceiling)

    # Step 3: break-even combination — **strict lexicographic optimum**.
    #
    # History: this used to be a value-DP that only respected *cell count*, then
    # asked the packer to verify it afterwards and merely emit a warning on
    # failure — so the UI could show a combination that cannot actually be
    # packed. A third-party audit flagged that as a correctness risk (the output
    # field's meaning was unreliable), which it is. The next fix verified the
    # DP set against the packer, but "fewest items" still came from a
    # shape-blind DP, so the count was not provably minimal.
    #
    # Now: ``Packer.solve_break_even`` solves the real problem exactly —
    #     minimize (fish_count, used_cells)  s.t.  value >= target, packable
    # The DP survives only as a *lower bound* for the item count
    # (``lower_bound``), which lets phase 1 skip sizes that provably cannot
    # reach the target. ``break_even_exact`` reports whether the optimum was
    # proven, and the UI/`summary()` must honour it in wording.
    #
    # If the exact solve cannot finish inside its budget it still returns a
    # *verified packable* layout (``feasible=True, exact=False``); only when it
    # finds nothing do we fall back to the audit-era DP+prune path.
    #
    # ``target == 0`` means no cost entered yet; we still compute a combo so the
    # UI can show "what a break-even haul looks like" instead of nothing.
    combo: list[tuple[FishVariant, int]] = []
    be_value = 0
    be_count = 0
    be_cells = 0
    be_feasible = False
    be_exact = False

    if containers:
        _say("正在计算回本所需的最少组合…")
        dp_count, dp_value, dp_combo = _dp_min_combo(variants, target)
        by_name = {v.name: v for v in positive}

        if target > 0:
            exact_res = packer.solve_break_even(
                None, target, lower_bound=dp_count or 1
            )
            if exact_res.feasible:
                counts: dict[str, int] = exact_res.counts
                combo = [(by_name[n], c) for n, c in counts.items() if n in by_name]
                combo.sort(key=lambda p: (-p[0].value_per_cell, p[0].name))
                be_value = exact_res.value
                be_cells = exact_res.used_cells
                be_count = exact_res.count
                be_feasible = True
                be_exact = exact_res.exact
                if not be_exact:
                    warnings.append(
                        "回本方案在时间预算内未证明为理论最少，仅保证可装下"
                    )

        if not be_feasible:
            combo, be_value, be_cells, be_feasible = _verified_break_even(
                packer, dp_combo, target, time_budget=time_budget
            )
            be_count = sum(n for _, n in combo)
            if not be_feasible:
                warnings.append(
                    "回本组合无法完全装入容器，已剔除装不下的部分（容器不足或鱼太大）"
                )
            elif dp_value and target > 0 and be_value < target:
                warnings.append(
                    "当前容量下凑不出回本金额，请换更大的背包/胸挂"
                )
    elif target > 0:
        warnings.append("在当前容器容量下无法凑出回本金额，请换更大的背包/胸挂")

    # Recommendations: what to grab first, and what to leave behind.
    _say("正在排序拾取优先级…")
    ranked = _order_by_density(positive)
    recommendations = [(v, 0) for v in ranked]
    best_density = ranked[0].value_per_cell if ranked else 0.0
    avoid = [v for v in ranked if best_density and v.value_per_cell < best_density * avoid_ratio]

    return RunPlan(
        target=target,
        ceiling=ceiling,
        can_break_even=can,
        shortfall=shortfall,
        break_even=combo,
        break_even_value=be_value,
        break_even_count=be_count,
        break_even_cells=be_cells,
        break_even_feasible=be_feasible,
        break_even_exact=be_exact,
        best_plan=best_plan,
        recommendations=recommendations,
        avoid=avoid,
        warnings=warnings,
    )


def report_catch(
    loadout: Loadout,
    variants: Sequence[FishVariant],
    stock: dict[str, int],
    *,
    rotate: bool = True,
    time_budget: float = 3.0,
    node_budget: int = 300_000,
    progress=None,
) -> CatchReport:
    """Re-plan for what was actually caught: what fits, what must be dropped.

    ``stock`` maps variant name -> how many were caught. The optimizer keeps the
    most valuable subset that fits, so ``unplaced`` is exactly the list to leave
    behind (or eat/toss).

    **Only the caught variants enter the solver.** A fish sitting in the
    catalogue but not in ``stock`` has supply 0, so it could never be placed
    anyway — handing it to the packer only makes the anchor loop skip it at
    every cell of every container.

    Measured effect on the real 69-cell loadout (59 catalogue variants → 5
    caught ones): *no* change in the achievable value and *no* noticeable node
    reduction — the cost is dominated by geometry (how many anchors a shape can
    cover), not by how many variants exist. It is kept because it makes the
    "计算的就是你钓上来的这批鱼" semantics explicit in code, and because it does
    cut the greedy pre-pass and the `value_bound` scan, which are O(variants)
    per step. Do not expect a speedup here.
    """
    containers = loadout.containers
    if not containers:
        return CatchReport(
            stock=dict(stock),
            packed=PackResult(),
            unplaced={k: v for k, v in stock.items() if v},
        )

    caught = {name: n for name, n in stock.items() if n > 0}
    if not caught:
        return CatchReport(stock=dict(stock), packed=PackResult(), unplaced={})

    pool = [v for v in variants if v.price > 0 and caught.get(v.name, 0) > 0]
    if not pool:
        # stock 里的键在目录里找不到（或全是未填价的款式）——如实报"全都没装下"
        return CatchReport(
            stock=dict(stock),
            packed=PackResult(),
            unplaced=dict(caught),
        )

    packer = Packer(
        containers, pool, rotate=rotate,
        node_budget=node_budget, time_budget=time_budget,
    )
    if progress is not None:
        try:
            progress("正在计算这批鱼怎么装最值…")
        except Exception:  # noqa: BLE001
            pass
    res = packer.pack(dict(caught))

    placed = res.counts
    unplaced = {
        name: n - placed.get(name, 0)
        for name, n in stock.items()
        if n - placed.get(name, 0) > 0
    }
    return CatchReport(stock=dict(stock), packed=res, unplaced=unplaced)


def caught_value(variants: Sequence[FishVariant], stock: dict[str, int]) -> int:
    """Total sale price of a set of caught variants."""
    prices = {v.name: v.price for v in variants}
    return sum(prices.get(name, 0) * n for name, n in stock.items())
