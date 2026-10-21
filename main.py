"""启动三角洲钓鱼成本核算器。

用法:
    python main.py              # 启动图形界面
    python main.py --check      # 只做数据自检并打印报告, 不打开窗口

全部游戏数据都在 ``data/game.json`` 一个文件里。

打包成 exe 后用法完全一致；此时数据目录是 **exe 旁边的 data\\**（可从
「帮助 → 打开数据目录」跳过去），首次运行会自动从内置副本播种。
"""

from __future__ import annotations

import sys
from pathlib import Path


def _src_dir() -> Path | None:
    """Where the ``diaoyu`` package lives, or ``None`` when it is baked in.

    * 源码运行：``<repo>/src/diaoyu``
    * Nuitka 打包：模块已编译进 exe，不需要路径
    * PyInstaller 打包：模块在 ``sys._MEIPASS`` 里，sys.path 已包含
    """
    if getattr(sys, "frozen", False) or hasattr(sys, "_MEIPASS"):
        return None
    here = Path(__file__).resolve().parent
    if (here / "src" / "diaoyu").is_dir():
        return here / "src"
    return None


def _bootstrap() -> None:
    """Make ``import diaoyu`` work from source.

    ⚠️ 这段必须在**模块顶层**执行（下面立刻调用），不能只放在 ``main()`` 里。
    原因：Nuitka 是在**编译期**按静态分析去找要编译的包的 —— 它从入口脚本
    出发扫描 ``import diaoyu...``。如果 ``sys.path`` 到运行时才补，
    编译期看不到包，产物里就没有 diaoyu 模块，启动直接
    ``ModuleNotFoundError: No module named 'diaoyu'``（实际踩过）。
    在顶层补好 sys.path，Nuitka 顺着就能把整个包编译进去。
    """
    src = _src_dir()
    if src is not None:
        path = str(src)
        if path not in sys.path:
            sys.path.insert(0, path)


_bootstrap()


def _data_dir():
    """The writable data directory (seeds it from the bundle on first run)."""
    from diaoyu.repository import ensure_data_dir

    return ensure_data_dir()


def run_check() -> int:
    """Validate the data file and print a readable report."""
    from diaoyu.models import Loadout
    from diaoyu.planner import plan_run
    from diaoyu.repository import load_game_data

    data_dir = _data_dir()
    out: list[str] = []
    try:
        data = load_game_data(data_dir)
        fish, rigs, bags = data.fish, data.chest_rigs, data.backpacks
        consumables, travel = data.consumables, data.travel_cost
        warnings = data.warnings
    except Exception as exc:  # noqa: BLE001
        print(f"数据载入失败: {exc}")
        return 1

    out.append(f"数据目录: {data_dir}")
    out.append(f"鱼种    : {len(fish)}")
    out.append(f"款式    : {sum(f.variant_count for f in fish)}")
    out.append(f"胸挂    : {len(rigs)}")
    out.append(f"背包    : {len(bags)}")
    out.append(f"消耗品  : {len(consumables)}  固定成本 {travel}")
    out.append("")

    if warnings:
        out.append("警告:")
        for w in warnings:
            out.append(f"  - {w}")
        out.append("")

    variants = [v for f in fish for v in f.variants if v.price > 0]
    out.append(f"{'鱼种':<14}{'款式':<14}{'尺寸':<7}{'价格':>7}{'占格':>5}{'每格价值':>10}")
    for v in sorted(variants, key=lambda x: -x.value_per_cell):
        flag = " [隐藏]" if v.hidden else ""
        out.append(f"{v.species:<14}{v.style:<14}{v.size_label:<7}{v.price:>7}"
                   f"{v.cells:>5}{v.value_per_cell:>10.1f}{flag}")
    if not variants:
        out.append("  （还没有任何填好价格的款式 —— 请在「编辑价目表」里补全）")
    out.append("")

    # -- 稀有度分档（由款式颜色推导）+ 产地 / 鱼饵 覆盖情况 -----------------
    from diaoyu.models import TIERS, best_bait, fish_by_tier, known_regions

    groups = fish_by_tier(fish)
    out.append("稀有度分档（由款式颜色推导：白色→绿→蓝→紫→金→红，典藏为独立标记）")
    for t in TIERS:
        members = groups.get(t.id) or []
        if not members:
            continue
        names = "、".join(f.name for f in members)
        out.append(f"  {t.label:<4}{len(members):>3} 个  {names}")
    out.append(f"  典藏款鱼种: {[f.name for f in fish if f.collector] or '（无）'}")
    regions = known_regions(fish)
    out.append(f"  已填产地的水域: {regions or '（还没填 —— 地区筛选会显示全部鱼）'}")
    baits = best_bait(fish)
    out.append(f"  建议鱼饵: {[(n, s) for n, s in baits] or '（还没填 —— 鱼饵推荐为空）'}")
    out.append("")

    rig = rigs[0] if rigs else None
    bag = bags[0] if bags else None
    lo = Loadout(chest_rig=rig, backpack=bag)
    plan = plan_run(lo, variants)
    out.append(f"示例组合: 胸挂 {rig.name if rig else '-'} + 背包 {bag.name if bag else '-'}")
    out.append(f"  总容量    : {lo.capacity} 格")
    out.append(f"  最高收益  : {plan.ceiling}")
    out.append(f"  回本线    : {plan.target}")
    counts = sorted(plan.best_plan.counts.items(), key=lambda kv: -kv[1])
    out.append(f"  装载清单  : {counts}")
    out.append(f"  装填率    : {plan.best_plan.fill_ratio * 100:.1f}%")
    out.append("")
    out.append("运行 python main.py 打开图形界面。")

    text = "\n".join(out)
    # 报告写在**可写**位置：打包后是 exe 旁边的 output\，源码运行时是仓库根的 .work\。
    # （绝不能用 sys._MEIPASS：那是只读临时目录，--check 会因为写不进去而失败。）
    base = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) \
        else Path(__file__).resolve().parent
    report_name = "selfcheck.txt"
    report = base / "output" / report_name
    try:
        report.parent.mkdir(exist_ok=True)
        report.write_text(text + "\n", encoding="utf-8")
    except OSError:
        report = None
    try:
        print(text)
        if report:
            print(f"(报告已写入 {report})")
    except UnicodeEncodeError:
        print(f"(控制台编码不支持中文, 报告已写入 {report})")
    return 0


def main() -> int:
    if "--check" in sys.argv:
        return run_check()

    from diaoyu.gui import CalculatorApp, _setup_dpi_awareness, dpi_scale

    # ⚠️ 必须在建立任何 Tk 窗口**之前**声明 DPI 感知 —— Windows 只在进程
    # 启动阶段接受这个声明。不做的话，在 125%/150% 缩放的屏幕上系统会把窗口
    # 整体拉伸成位图，画布里 30x30 的正方形格子看起来就是长方形。
    _setup_dpi_awareness()

    app = CalculatorApp()
    if dpi_scale() > 1.0:
        app.geometry(f"{int(1400 * dpi_scale())}x{int(900 * dpi_scale())}")
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
