"""整手/资金约束下的组合分配: 月度方案与回测共用同一套机制 (P0-1/P0-2)

规则(2026-09-05 第二轮复查 N5 重写为贪心逐只分配):
- 每槽预算 = 资金/max_stocks(等权目标), 按 score 从高到低逐只买入整手
- 买不起 1 手的股票跳过, 预算自然留给后面的股票 —— 原"中位价估算只数再硬截断"会
  在头部便宜股多可买时无谓集中、在头部贵股多时无谓留现金
- 行业上限: 已投行业权重达到 eff_cap(自适应放宽 max(ind_cap, 2/max_stocks))后跳过该行业
- 可选 cost_rate: 计入单边成本, 保证成交端不会因费用超预算
返回 {"shares", "weights"(实际成本权重), "cost", "n_eff", "cash_left"}
"""
import numpy as np
import pandas as pd

from app.portfolio.board import lot_shares

LOT = 100  # A股一手(主板/创业板; 科创/北交规则见 board.lot_shares)


def allocate_lots(top: pd.DataFrame, equity_cash: float, max_stocks: int,
                  ind_cap: float = 1.0, cost_rate: float = 0.0,
                  floor_stocks: float = 2.0, rank_weighted: bool = False) -> dict:
    # rank_weighted(2026-10-02, 预注册 plans/rankweight-prereg-20261002.md 的 A/B 缝):
    # True 时第 r 名(传入顺序=分数降序)的 slot = equity × (K+1−r)/Σ(K+1−j),
    # K = min(6, len(pool));第 K 名之后 slot=0(跳过)。False(缺省) = 等权原路径
    # **逐位不变**(pin 安全)。被跳过名次的预算不重分配(现金闲置 = 真实代价, 由 A/B 捕获)。
    # 2026-09-06 第十轮 N10-21: slot_floor 死参数已删除(贪心算法不需要估算只数)
    # 2026-09-06 第十二轮 R11-2: 行业上限下界参数化(floor_stocks=2 历史现状)
    out = {"shares": {}, "weights": {}, "cost": 0.0, "n_eff": 0, "cash_left": equity_cash}
    if top is None or top.empty or equity_cash <= 0 or max_stocks <= 0:
        return out
    # 2026-09-05 第五轮复查 P5-1: 不再 top.head(max_stocks) 截断候选池 —— 调用方传入
    # 更大的候选池(如 nlargest(candidate_top))时, 被整手/行业上限挡掉的股票向下补位,
    # 由循环内 len(shares)>=max_stocks 封顶目标只数。注意: 补位机制经回测实证在 1 万
    # 本金下系统性恶化(回撤 -30.7%→-43%~-50%), 生产 config stock_candidate_top=0
    # 默认关闭, 资金做大后可重新评估
    pool = top.copy()
    pool = pool[pool["close"] > 0]
    if pool.empty:
        return out
    # 第三轮复查 M3: shares 的 key 统一为 ts_code —— 输入带 ts_code 列时(如 builder 的
    # read_sql RangeIndex)重建索引, 消除"key 类型随调用方变化"的隐式契约
    if "ts_code" in pool.columns:
        pool = pool.set_index("ts_code")
    has_ind = "industry" in pool.columns
    eff_cap = max(ind_cap, floor_stocks / max_stocks) if ind_cap < 1.0 else 1.0

    cash = float(equity_cash)
    slot = equity_cash / max_stocks
    rank_w = None
    K = min(6, len(pool))
    if rank_weighted:
        den = sum(K + 1 - j for j in range(1, K + 1))
        rank_w = {r: (K + 1 - r) / den for r in range(1, K + 1)}
    shares = {}
    ind_w = {}
    for r, (code, row) in enumerate(pool.iterrows(), start=1):
        if len(shares) >= max_stocks:
            break
        price = float(row["close"])
        if rank_w is not None:
            if r > K:
                continue                      # K 名之外 slot=0: 不买(冻结语义)
            budget = min(equity_cash * rank_w[r], cash)
        else:
            budget = min(slot, cash)
        # 可负担最大股数(floor(商+1e-9): 修正浮点毛刺, ε 加在商上而非分母上);
        # 板块整手规则(2026-09-07 全市场扩展): 主板/创业板 100 股整数倍, 科创板最低
        # 200 股后 1 股递增, 北交所最低 100 股后 1 股递增 —— board.lot_shares
        affordable = int(np.floor(budget / (price * (1 + cost_rate)) + 1e-9))
        sh = lot_shares(code, affordable)
        if sh <= 0:
            continue  # 买不起最低起买量: 贪心跳过, 预算留给后面的股票
        ind = None
        if has_ind and ind_cap < 1.0:
            ind = row["industry"] if pd.notna(row["industry"]) else "未分类"
            # 前瞻检查: 买入后该行业权重不得顶破 eff_cap(先算拟买再判定)
            prospect = ind_w.get(ind, 0.0) + sh * price / equity_cash
            if prospect > eff_cap + 1e-9:
                continue  # 该股会顶破行业上限: 跳过, 预算留给其他行业
        shares[code] = sh
        cash -= sh * price
        if ind is not None:
            ind_w[ind] = ind_w.get(ind, 0.0) + sh * price / equity_cash

    cost = float(sum(shares[c] * float(pool.loc[c, "close"]) for c in shares))
    if cost > 0:
        weights = {c: round(shares[c] * float(pool.loc[c, "close"]) / cost, 4) for c in shares}
    else:
        weights = {}
    return {"shares": shares, "weights": weights, "cost": round(cost, 2),
            "n_eff": len(shares), "cash_left": round(cash, 2)}
