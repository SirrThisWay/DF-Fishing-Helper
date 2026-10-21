"""diaoyu —— 三角洲行动 · 钓鱼成本核算器。

按胸挂 / 背包的**真实格子布局**做精确二维装箱，回答三个问题：

1. 这一趟最多能带回多少钱（``plan_run().ceiling``）
2. 能不能回本、还差多少（``target`` / ``shortfall``）
3. 回本最少要几条什么鱼、怎么摆（``solve_break_even``，严格词典序最优）

模块划分：

* :mod:`diaoyu.models`      领域模型（鱼种/款式/容器/消耗品/稀有度色带）
* :mod:`diaoyu.packing`     精确装箱求解器（分支限界 + 口袋分组约束）
* :mod:`diaoyu.packing` 里的关键约束：**不同颜色的口袋互不相通**，
  一条鱼不能横跨两个口袋。
* :mod:`diaoyu.planner`     成本与回本规划（面向业务的那一层）
* :mod:`diaoyu.repository`  JSON 数据读写 + 界面偏好
* :mod:`diaoyu.gui`         tkinter 界面
"""

__version__ = "1.0.0"
__all__ = ["__version__"]
