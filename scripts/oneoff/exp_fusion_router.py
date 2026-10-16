"""阶段 3: 融合器 —— 滚动路由(芯片类比里的 router / DVFS)

背景(2026-09-11): 第一个异构 die `model_score_die_flow` 过了 KGD 五项判据, 但**等权秩平均
融合当场证伪** —— 覆盖域对齐后融合把**活跃区 top-6 从 +3.17% 砸到 +0.76%/月**, 而活跃区
是模型全部收益的来源。机制: 等权让一个在该区更弱的伙伴(top6 −1.78%)把强者稀释掉。

⇒ 本脚本检验「**路由**」能否修好它。芯片类比: 等权 = 所有 die 常开; 路由 = 按**历史**
表现给 die 供电或断电(power gating), 权重只由**已实现**的信息决定。

=============================== 预注册(写于跑之前) ===============================
**信息纪律** —— 在第 t 个月定权重时, 只允许用**已实现**的读数: 模型 m 的
`e_m(t)` = 它在**截至 t-2 月**的所有月份上、在**共同覆盖域**上的 top6 超额均值。
(用 top6 不用 head: 策略买的就是 6 只; 用 t-2 不用 t-1: 月度截面的 21 日前向收益
要再过约一个月才完全实现, 留一期缓冲比精确贴边安全。)

**变体(全部零拟合参数, 写死在此)**:
  R0 等权        w_base = w_die = 1                    ← 对照(已知会砸活跃区, 只为并列显示)
  R1 电源门控    w_die = 1 当 e_die(t) > 0, 否则 0; w_base = 1
  R2 相对动量    w_die = 1 当 e_die(t) > e_base(t), 否则 0; w_base = 1

**覆盖域规则(参数无关)**: 某只股票在某模型上没有分数时, 该模型的权重**让给**有分数的
模型(即按「有意见的模型」做加权平均)。理由: die 平均覆盖 3808 只、基线 ~4055 只, 若取交集
会把**覆盖域劣势**混进结论 —— 而策略的候选池正比于覆盖域。这条规则让融合表的覆盖域
不低于基线, 使对照只反映「选股」不反映「能打多少只」。

**阶段 A 判据(便宜, 本脚本)**: 变体「有希望」⟺ **两个区段的 top6 都不劣于基线**
(DEAD_top6 ≥ 基线且 ACTIVE_top6 ≥ 基线)。任一区段变差 ⇒ 不跑阶段 B 回测, 直接记录负结果。
理由: 等权已经证明「一边改善一边砸穿」是可能的, 而活跃区是收益来源, 单边改善没有意义。

**阶段 B(仅对有希望的变体, 另跑)**: `engine.run_backtest` 的两轴判据(年化 + 回撤),
窗口 2019-07-22 起(与 pin `20260911_fullhist` 同窗), 且必须**两轴都不劣于现行**。

**不允许的事**: 看到阶段 A 结果后再加变体 / 调 e_m 的定义 / 改判据。要改, 开新的一轮并
在新脚本里写明这一轮为什么不够。

用法:
    ./.venv/Scripts/python.exe scripts/oneoff/exp_fusion_router.py
    ./.venv/Scripts/python.exe scripts/oneoff/exp_fusion_router.py --write-tables
"""
import argparse
import datetime
import io
import logging
import sys
from pathlib import Path

import numpy as np
import pandas as pd

if hasattr(sys.stdout, "buffer"):     # 中文 Windows 控制台是 GBK
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

BASE = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE))

from app import db                                       # noqa: E402
from app.factors import factor_engine as fe              # noqa: E402
from app.models import expert as ex                      # noqa: E402

BASE_T = "model_score"
DIE_T = "model_score_die_flow"
LOG = r"D:/claude-home/tmp/fusion_router.log"

SETTLE = 2        # 权重只能用 t-2 及更早的已实现读数
VARIANTS = ("R0", "R1", "R2")


def _setup_log():
    fmt = logging.Formatter("%(asctime)s %(message)s")
    fh = logging.FileHandler(LOG, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logging.basicConfig(level=logging.INFO, handlers=[fh, sh], force=True)


def _zone_stats(det: pd.DataFrame, log, prefix: str = "") -> dict:
    out = {}
    for z, (a, b) in (("DEAD", ex.DEAD), ("ACTIVE", ex.ACTIVE)):
        sub = det[(det.index >= a) & (det.index <= b)]
        h, t6 = ex._stat(sub["head_excess"]), ex._stat(sub["top6_excess"])
        out[z] = {"head": h["mean"], "t": h["t"], "top6": t6["mean"], "n": h["n"]}
        log.info("  %s%-7s head %+.4f (t=%+.2f, n=%d)   top6 %+.4f",
                 prefix, z, h["mean"], h["t"], h["n"], t6["mean"])
    return out


def _weights(variant: str, e_base: float, e_die: float) -> tuple:
    """返回 (w_base, w_die)。全部零拟合参数, 规则写死在预注册里。"""
    if variant == "R0":
        return 1.0, 1.0
    if variant == "R1":
        return 1.0, (1.0 if (e_die == e_die and e_die > 0) else 0.0)
    if variant == "R2":
        return 1.0, (1.0 if (e_die == e_die and e_base == e_base and e_die > e_base) else 0.0)
    raise ValueError(variant)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write-tables", action="store_true",
                    help="把融合分数落 `model_score_fused_<variant>` 表(阶段 B 回测用)")
    args = ap.parse_args()
    _setup_log()
    log = logging.getLogger("router")
    db.init_db()

    m = fe.load_market()
    dates = pd.to_datetime(m["close"].index)
    md_all = fe.month_end_dates(dates, today=datetime.date.today())
    fwd = ex._forward_return(m)

    base = ex.load_scores(BASE_T)
    die = ex.load_scores(DIE_T)
    if base.empty or die.empty:
        raise SystemExit("基线或 die 为空")
    common = pd.DatetimeIndex(base.index).intersection(pd.DatetimeIndex(die.index))
    md = md_all[(md_all >= common.min()) & (md_all <= common.max())]
    log.info("基线 %d 期 / die %d 期 / 共同评分月 %d 期 (%s ~ %s)",
             len(base), len(die), len(md), md[0].date(), md[-1].date())

    # ---- 两个模型各自的**已实现** top6 超额(共同覆盖域上, 同基准) ----
    # 这是路由器唯一的输入。restrict_to 保证两边在同一批股票上比, 否则会把覆盖域差异
    # 读成选股能力差异。
    hist = {}
    for tag, sc, other in (("base", base, die), ("die", die, base)):
        d = ex.daily_head_stats(sc, fwd, md, restrict_to=other)
        hist[tag] = d["top6_excess"].reindex(md)
    log.info("已实现 top6 超额: base 覆盖 %d 期 / die 覆盖 %d 期",
             int(hist["base"].notna().sum()), int(hist["die"].notna().sum()))

    # ---- 逐月路由 + 融合 ----
    cols = base.columns.union(die.columns)
    fused = {v: pd.DataFrame(index=md, columns=cols, dtype="float64") for v in VARIANTS}
    wlog = {v: [] for v in VARIANTS}
    for i, d in enumerate(md):
        cut = i - SETTLE                       # 只用 md[:cut] 的已实现读数
        e_b = float(hist["base"].iloc[:cut].mean()) if cut > 0 else np.nan
        e_d = float(hist["die"].iloc[:cut].mean()) if cut > 0 else np.nan
        rb = base.loc[d].rank(pct=True) if d in base.index else pd.Series(dtype=float)
        rd_ = die.loc[d].rank(pct=True) if d in die.index else pd.Series(dtype=float)
        for v in VARIANTS:
            wb, wd = _weights(v, e_b, e_d)
            # 权重让给「有意见的模型」: 缺失的一边不参与, 不把该股票的分拉向中点
            num = rb.reindex(cols).fillna(0) * wb + rd_.reindex(cols).fillna(0) * wd
            den = (rb.reindex(cols).notna() * wb + rd_.reindex(cols).notna() * wd)
            fused[v].loc[d] = (num / den.replace(0, np.nan))
            wlog[v].append((d, wb, wd, e_b, e_d))

    # ---- 阶段 A 判据 ----
    log.info("=" * 78)
    log.info("覆盖域: 基线 %d 只 / die %d 只 / 并集(融合表) %d 只, 逐月中位可比 %d 只",
             int(base.notna().sum(axis=1).median()),
             int(die.notna().sum(axis=1).median()),
             int(fused["R0"].notna().sum(axis=1).median()),
             int((base.notna() & die.notna()).sum(axis=1).median()))
    log.info("阶段 A: 覆盖域对齐后的分段读数 (判据 = 两区段 top6 都不劣于基线)")
    # 基线读数**限制在融合表的覆盖域**上算 —— 否则「融合能打更多股票」会混进对照,
    # 而那是覆盖域差异, 不是选股差异(expert.head_excess_at 的同一理由)。
    # 另报一份全coverage的, 供核对。
    log.info("[基线 %s, 全覆盖(仅参考)]", BASE_T)
    _zone_stats(ex.daily_head_stats(base, fwd, md), log)
    log.info("[基线 %s, 限制在融合表覆盖域 —— **判据用这一份**]", BASE_T)
    bs = _zone_stats(ex.daily_head_stats(base, fwd, md, restrict_to=fused["R0"]), log)

    results = {}
    for v in VARIANTS:
        det = ex.daily_head_stats(fused[v], fwd, md)
        log.info("[变体 %s]", v)
        st = _zone_stats(det, log)
        ok = all(st[z]["top6"] == st[z]["top6"] and st[z]["top6"] >= bs[z]["top6"]
                 for z in ("DEAD", "ACTIVE"))
        nd = sum(1 for _, _, wd, _, _ in wlog[v] if wd > 0)
        st["promising"] = bool(ok)
        st["die_on_months"] = nd
        results[v] = st
        log.info("  ⇒ 两区段 top6 都不劣于基线: **%s** (die 供电 %d/%d 个月)",
                 "是" if ok else "**否**", nd, len(md))

    log.info("=" * 78)
    log.info("路由开关轨迹(die 在哪些月份被断电):")
    for v in VARIANTS:
        off = [str(d.date()) for d, _, wd, _, _ in wlog[v] if wd == 0]
        if off:
            log.info("  %s 断电 %d 期: %s", v, len(off), ", ".join(off[:8]) + (" ..." if len(off) > 8 else ""))
        else:
            log.info("  %s 全程供电", v)

    if args.write_tables:
        for v in VARIANTS:
            t = f"model_score_fused_{v.lower()}"
            ex.ensure_score_table(t)
            f = fused[v].stack().rename("score").reset_index()
            f.columns = ["trade_date", "ts_code", "score"]
            f = f.dropna(subset=["score"])
            f["trade_date"] = pd.to_datetime(f["trade_date"]).dt.strftime("%Y%m%d")
            # pred_ret 引擎不读, 但 builder/api/monitor 会读 —— 沿用基线的值
            conn = db.get_conn()
            try:
                preds = pd.read_sql(
                    f"SELECT ts_code, trade_date, pred_ret FROM {BASE_T}", conn)
            finally:
                conn.close()
            f = f.merge(preds, on=["ts_code", "trade_date"], how="left")
            conn = db.get_conn()
            try:
                conn.execute(f"DELETE FROM {t}")
                f[["ts_code", "trade_date", "score", "pred_ret"]].to_sql(
                    t, conn, if_exists="append", index=False)
                conn.commit()
            finally:
                conn.close()
            log.info("已写 %s (%d 行)", t, len(f))

    log.info("阶段 A 结论: %s",
             "、".join(v for v in VARIANTS if results[v]["promising"]) or
             "**没有任何变体通过** —— 路由同样修不好, 阶段 3 的最简两形态(等权/门控)全灭")


if __name__ == "__main__":
    main()
