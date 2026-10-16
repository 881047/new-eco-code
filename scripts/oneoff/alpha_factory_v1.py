"""Alpha Factory v1 —— 芯片式架构的因子产生层首跑(2026-09-11)

对应用户框架图的 `Alpha Factory → Alpha Pool → Devil/Critic → OOS验证 → Alpha Bank`。
本脚本只做前三段里的产生与验证, **不碰打分链路、不碰回测引擎**。

## 预注册(写在跑之前, 看到任何结果之前)

**候选池 = 158 个**(`zoos.qlib158()`, microsoft/qlib Alpha158 的表达式移植, Apache-2.0)。
**Bonferroni 分母 = 实际测过的候选数** —— 不许跑完再挑几个"重点看"。

**判据 = `app/alpha/gate.py` 的五条**(四条与冻结的阶段 0c 门禁逐字相同, 第五条 top6
同号是**新增的更严条件**):

| # | 判据 | 阈值 |
|---|---|---|
| 1 | 死区 head 与活跃区同号 | 符号一致 |
| 2 | 死区 \|head\| | ≥ 0.5%/月 |
| 3 | 死区 t | ≥ 2.0 |
| 4 | Bonferroni p_adj | < 0.05 |
| 5 | ⭐ 死区 top6 | ≥ 0(**新增, 更严**) |

**产出 = 全部 158 条的读数入 `alpha_bank` 表**(不论过没过), 状态 CANDIDATE / POOLED。

## 出口声明(必须在跑之前说清, 否则结果会被误用)

**门禁通过 = POOLED = 有资格进组合层回测, 不等于可用。**
本项目的判决只能是**组合层两轴**(系统年化 + 系统回撤不劣于同窗基线),
见 `scripts/oneoff/exp_die_stage_b.py` 的预注册。本脚本**不产出任何"该用哪个因子"的结论**。

## 已知局限(写在跑之前, 不做事后辩解)

1. **Alpha158 全部是价量派生** ⇒ 与存量 31 因子**信息源重合**。本项目的中心论点是
   「数据异构 > 算法异构」, 所以这 158 个**不是**异构口粮。它们的作用是: ①给门禁一个
   够大的样本以检验分辨率; ②提供一个公开、被文献检验过的**标尺**。
2. 判据建在**月度截面**上, 39 期死区 ⇒ 单条 alpha 的检出下限不高, 只能靠 Bonferroni
   控制"测得多必然挑出好的"这一效应。
3. **不做事后筛选**: 跑完只看 `alpha_bank` 表, 不改 `gate.py`, 不重跑挑窗口。

## 第二轮: `--zoo flow`(2026-09-11 晚, 在 qlib158 那轮**之后**才写下的预注册)

`qlib158` 是量价派生 ⇒ 与本项目存量 31 因子**同信息源**, 它的用途是当**标尺**。
真正要问的问题是「**数据异构**到底有没有用」, 那要用资金类因子回答 ——
候选池 = `app/factors/flow_factors.py` **已经声明过的**全部非研究因子
(`compute_flow_factors(include_research=False)`), **原样** 15 个:

| 家族 | 候选 | 个数 |
|---|---|---|
| 融资融券 | `margin_rzye_ratio` / `margin_rqye_ratio` / `margin_buy_share` / `margin_net_share` / `margin_rzye_chg20` / `margin_net_to_rzye` | 6 |
| 个股资金流 | `mf_main_net` / `mf_elg_net` / `mf_lg_net` / `mf_sm_net` / `mf_main_net20` / `mf_main_sm_div` | 6 |
| 大宗交易 | `block_discount` / `block_cnt20` / `block_avg_size` | 3 |

**不加任何变换、不做任何搜索** —— 池子就是模块已经产出的因子本身。理由: 一旦"顺手再加个
20 日均值/差分", 就是在**看到读数之后**扩张搜索空间, 分母就再也说不清了。
Bonferroni 分母 = 15(本轮的候选数); **上一轮 158 条的分母仍是 158, 不合并、不重算**。

**PIT 说明(为什么同日资金数据可用)**: 回测执行价 = **顺延日收盘**(`engine.py:197`),
即用 t 日数据在 t+1 日成交 ⇒ 交易所在 t 日盘后发布的融资/资金流数据**是可以拿到的**,
不构成前视。`hk_*` 两个因子按不变量 #6 **排除**(2024-08 覆盖度断崖)。

用法:
    ./.venv/Scripts/python.exe scripts/oneoff/alpha_factory_v1.py            # 全量 158 (qlib158)
    ./.venv/Scripts/python.exe scripts/oneoff/alpha_factory_v1.py --zoo flow # 15 个资金类因子
    ./.venv/Scripts/python.exe scripts/oneoff/alpha_factory_v1.py --smoke    # 每个算子各 1 窗
    ./.venv/Scripts/python.exe scripts/oneoff/alpha_factory_v1.py --resume   # 跳过已登记且有值面板的
日志: D:/claude-home/tmp/alpha_factory.log
"""
import argparse
import io
import logging
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

# ⚠️ 必须先留住原 wrapper 的引用: io.TextIOWrapper(sys.stdout.buffer, ...) 之后,
# 旧 TextIOWrapper 一旦被 GC 就会**关掉底层 buffer**(症状=日志文件正常, 控制台全是
# "I/O operation on closed file")。2026-09-11 在 exp_die_weekly.py 上踩过。
if hasattr(sys.stdout, "buffer"):
    _ORIG_STDOUT = sys.stdout
    sys.stdout = io.TextIOWrapper(_ORIG_STDOUT.buffer, encoding="utf-8", errors="replace")

BASE = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE))

from app import db                                        # noqa: E402
from app.alpha import bank, expr as E, gate, zoos         # noqa: E402
from app.factors import factor_engine as fe               # noqa: E402
from app.factors import flow_factors as ff                # noqa: E402

LOG_PATH = r"D:/claude-home/tmp/alpha_factory.log"
EVAL_SINCE = "2019-01-01"     # 与冻结的阶段 0c 门禁同口径
WARMUP_DAYS = 400             # 最长滚动窗 60 交易日, 400 自然日远超所需

log = logging.getLogger("alpha_factory")


def _setup_log():
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(message)s")
    fh = logging.FileHandler(LOG_PATH, encoding="utf-8")
    fh.setFormatter(fmt)
    log.addHandler(fh)
    log.addHandler(logging.StreamHandler(sys.stdout))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--zoo", default="qlib158", choices=["qlib158", "flow"],
                    help="候选来源: qlib158(量价标尺) / flow(资金类, 异构口粮)")
    ap.add_argument("--smoke", action="store_true", help="每个算子只取 1 个窗口(冒烟)")
    ap.add_argument("--resume", action="store_true", help="跳过已登记且有值面板的 alpha")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()
    _setup_log()
    db.init_db()
    bank.ensure_table()

    cands = [] if args.zoo == "flow" else zoos.all_candidates()
    if args.smoke:
        keep, seen = [], set()
        for n, e, meta in cands:
            base = n.rstrip("0123456789")
            if base not in seen:
                seen.add(base)
                keep.append((n, e, meta))
        cands = keep
    if args.limit:
        cands = cands[:args.limit]

    mkt_since = (pd.Timestamp(EVAL_SINCE) - pd.Timedelta(days=WARMUP_DAYS)).strftime("%Y%m%d")
    t0 = time.time()
    m = fe.load_market(since=mkt_since)
    dates_all = pd.to_datetime(m["close"].index)
    log.info("行情面板 %d 天 × %d 只 (%.0fs)", len(dates_all), m["close"].shape[1],
             time.time() - t0)

    # 资金类候选不由表达式产生, 而是直接取自 flow_factors 的日频面板 ⇒ 先建池再打印横幅
    precomputed = {}
    if args.zoo == "flow":
        for nm, wide in sorted(ff.compute_flow_factors(
                m, dates_all, include_research=False).items()):
            precomputed[nm] = wide
            cands.append((nm, None, {
                "info_source": "flow", "source": "app/factors/flow_factors.py",
                "note": "资金类因子(原样, 无变换; hk_* 按不变量#6 排除)"}))
    n_tests = len(cands)
    if not n_tests:
        log.error("候选池为空 —— 检查 --zoo 与数据表")
        return

    log.info("=" * 78)
    log.info("Alpha Factory v1 | 候选来源 %s | 候选 %d 个 | 评估窗 %s 起 | Bonferroni 分母 = %d",
             args.zoo, n_tests, EVAL_SINCE, n_tests)
    log.info("门禁: 同号=%s | |head|>=%.3f | t>=%.1f | p_adj<%.2f | **top6>=0(新增)**",
             gate.GATE_REQUIRE_SAME_SIGN, gate.GATE_MIN_HEAD, gate.GATE_MIN_T,
             gate.GATE_ALPHA)
    log.info("=" * 78)

    panel = E.build_panel(m)
    eng = E.Engine(panel)
    md = fe.month_end_dates(dates_all)
    md = md[md >= pd.Timestamp(EVAL_SINCE)]
    zones = gate.zone_of(md)
    log.info("月末截面 %d 个 (%s ~ %s) | DEAD %d / ACTIVE %d / OTHER %d",
             len(md), md[0].date(), md[-1].date(),
             int((zones == "DEAD").sum()), int((zones == "ACTIVE").sum()),
             int((zones == "OTHER").sum()))

    fwd = gate.forward_return(m, gate.HORIZON)
    conn = db.get_conn()
    ind = fe.industry_map(conn)
    conn.close()

    recs, t_start = [], time.time()
    for i, (name, expr, meta) in enumerate(cands, 1):
        if args.resume and bank.has_values(name) and bank.get(name):
            log.info("[%3d/%3d] %-10s 跳过(已登记)", i, n_tests, name)
            continue
        t1 = time.time()
        try:
            if expr is None:                      # 资金类: 面板已算好, 只需对齐+中性化
                raw = precomputed[name].reindex(index=dates_all)
            else:
                raw = eng.eval(expr)
            neu = fe.neutralize(raw, ind)
        except Exception:
            log.exception("[%3d/%3d] %-10s 求值失败", i, n_tests, name)
            continue
        if isinstance(neu, pd.Series):
            neu = neu.to_frame()
        wide = neu.loc[neu.index.isin(md)]
        det = gate.evaluate(neu, fwd, md, zones)
        if det.empty:
            log.warning("[%3d/%3d] %-10s 无有效截面", i, n_tests, name)
            continue
        rec = gate.summarize(name, det, n_tests, extra={
            "avg_names": float(det["n"].mean()),
            "expr": expr if expr is not None else
                    f"flow_factors.compute_flow_factors()[{name!r}]",
            "zoo": args.zoo, "name": name,
            "info_source": meta.get("info_source"), "source": meta.get("source"),
            "pit_version": "pit_v1_%s" % EVAL_SINCE,
            "note": meta.get("note"),
        })
        bank.save_values(name, wide)
        bank.upsert(rec)
        recs.append(rec)
        log.info("[%3d/%3d] %-10s DEAD head %+.4f (t=%+.2f) top6 %+.4f | ACTIVE head %+.4f "
                 "| p_adj %.4f | %s | %.1fs",
                 i, n_tests, name, rec["DEAD_head"], rec["DEAD_head_t"], rec["DEAD_top6"],
                 rec["ACTIVE_head"], rec["p_adj"],
                 "PASS" if rec["PASS"] else "否", time.time() - t1)

    df = bank.list_alphas()
    passed = df[df["PASS"] == 1]
    log.info("=" * 78)
    log.info("完成 %d/%d 条 | 用时 %.0fs | 门禁通过 %d 条",
             len(recs), n_tests, time.time() - t_start, len(passed))
    if len(passed):
        for _, r in passed.sort_values("DEAD_head_t", ascending=False).iterrows():
            log.info("  PASS %-10s DEAD head %+.4f t=%+.2f top6 %+.4f | ACTIVE head %+.4f "
                     "| 逐年 %s", r["alpha_id"], r["DEAD_head"], r["DEAD_head_t"],
                     r["DEAD_top6"], r["ACTIVE_head"],
                     {k: round(v["head"], 4) for k, v in
                      __import__("json").loads(r["yearly"]).items()})
    log.info("⚠️ PASS = POOLED = **有资格进组合层回测**, 不等于可用。判决只能是组合层两轴")
    log.info("=" * 78)


if __name__ == "__main__":
    main()
