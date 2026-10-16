"""θ=2.0 拥挤剔除器 · 生产构型组合层复验（2026-09-18；断点 plans/resume-20260918-0600.md）

复验设计（与第四轮研究版的差异 = 这是"生产构型精确复验"）:
- 候选池不再用"打分 top100 近似"，而是逐锚点调用生产 `_top_candidates`
  （市值/流动性/ST/PIT 过滤 + 状态自适应只数，与方案构建共用同一实现）；
- 剔除注入：`_top_candidates` 产出后、`allocate_lots` 之前，剔除合并拥挤分 ≥2.0 的股
  （研究侧 θ=2.0 全轴过闸的同口径信号）；
- 回测：剔除后的月末锚点分数表 → `E.run_backtest`（隔离 run + 隔离表，不碰生产）。

判决口径（预注册 plans/negative-excluder-round4-20260917.md 沿用）:
死区不伤害(≥对照 -0.5pp) + 全期改善(≥+1pp) + 回撤不劣化(≤1pp)。
复验通过标准：全期改善在读数 ±1.5pp 邻域内（否则与研究侧结论不一致，存疑）。

用法: .venv/Scripts/python -X utf8 scripts/verify_excluder_production.py
"""
import json
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from app import config as cfg_mod  # noqa: E402
from app import db  # noqa: E402
from app.backtest import engine as E  # noqa: E402
from app.logger import setup_logger  # noqa: E402

setup_logger()

# 2026-09-28 审查注记: 对照臂**故意冻结**在 20260912_limitquant —— 本脚本是 excluder r6
# 预注册的复验器(该线已 REJECT 结清), 对照基准必须与当年判据同一读数, 不随重钉漂移;
# 若未来重开预注册, CTX_RUN 按新判据显式改。
CTX_RUN = "20260912_limitquant"
THETA = 2.0
CRIT = {"dead_floor": -0.005, "full_gain": 0.01, "mdd_worse_max": 0.01}
ST = "model_score_excl_verify"
TAG = "excl_verify"


def crowd_map() -> dict:
    """合并拥挤分（与研究侧 negative_excluder_r4.build_crowd 同一构造，冻结权重）。"""
    kg = {}
    p = BASE / "data" / "kg" / "excluder_scores.jsonl"
    if p.exists():
        for l in p.open(encoding="utf-8"):
            r = json.loads(l)
            kg.setdefault(r["anchor"], {})[r["ts_code"]] = float(r["crowd"])
    d = pd.read_parquet(BASE / "data" / "min60_derived" / "daily.parquet")
    d = d[(d["x2"].abs() <= 0.21) & (d["x3"].abs() <= 0.15)]
    lr = d.pivot(index="date", columns="ts_code", values="x2")
    cv = d.pivot(index="date", columns="ts_code", values="x3")
    lr.index = pd.to_datetime(lr.index)
    cv.index = pd.to_datetime(cv.index)
    lr5 = lr.rolling(5).sum().resample("ME").last()
    cv5 = cv.rolling(5).mean().resample("ME").last()

    def z(s):
        sd = s.std()
        return (s - s.mean()) / sd if sd and np.isfinite(sd) else s * 0.0

    out = {}
    for a in sorted(set(lr5.dropna(how="all").index.strftime("%Y%m%d")) | set(kg.keys())):
        at = pd.Timestamp(a)
        zk = z(pd.Series(kg.get(a, {})))
        zl = z(lr5.loc[:at].iloc[-1])
        zc = z(cv5.loc[:at].iloc[-1])
        m = {}
        for code in set(zk.dropna().index) | set(zl.dropna().index) | set(zc.dropna().index):
            v = (0.5 * float(zk.get(code, 0.0) or 0.0)
                 + 0.25 * float(zl.get(code, 0.0) or 0.0)
                 + 0.25 * float(zc.get(code, 0.0) or 0.0))
            if v == v:
                m[code] = round(v, 4)
        if m:
            out[a] = m
    return out


def main() -> int:
    from app.portfolio import builder as B
    t0 = time.time()
    cm = crowd_map()
    print(f"合并拥挤分覆盖 {len(cm)} 锚点月")

    # 重建剔除后的月末分数表：候选 = 生产 _top_candidates 输出 − 拥挤 ≥θ 的股
    # （plan_date 沿用 builder 同源：已完成月最后评分日）
    anchors = sorted(cm.keys())
    conn = db.get_conn()
    try:
        all_days = [r[0] for r in conn.execute(
            "SELECT DISTINCT trade_date FROM model_score ORDER BY trade_date").fetchall()]
    finally:
        conn.close()
    rows, kept, excluded = [], 0, 0
    for a in anchors:
        # 生产口径的月末评分日: 该月最后一个有评分的交易日
        month_days = [d for d in all_days if d[:6] == a[:6]]
        if not month_days:
            continue
        plan_date = month_days[-1]
        try:
            top, _state, _latest, _ld = B._top_candidates(plan_date)
        except Exception as e:
            print(f"  {a}: _top_candidates 失败 {type(e).__name__}, 跳过")
            continue
        if top.empty:
            continue
        hot = {c for c, v in cm.get(a, {}).items() if v >= THETA}
        top2 = top[~top.index.isin(hot)]
        excluded += len(top) - len(top2)
        kept += len(top2)
        for code, row in top2.iterrows():
            rows.append((code, plan_date, float(row["score"]), float(row.get("pred_ret") or 0.0)))

    out = pd.DataFrame(rows, columns=["ts_code", "trade_date", "score", "pred_ret"])
    print(f"剔除后: {len(out):,} 行（保留 {kept:,} / 剔除 {excluded:,} 股次）")
    conn = db.get_conn()
    try:
        conn.execute(f"DROP TABLE IF EXISTS {ST}")
        conn.execute(f"CREATE TABLE {ST} (ts_code TEXT, trade_date TEXT, score REAL, "
                     "pred_ret REAL, PRIMARY KEY (ts_code, trade_date)) WITHOUT ROWID")
        conn.executemany(f"INSERT OR REPLACE INTO {ST} VALUES (?,?,?,?)",
                         out.itertuples(index=False, name=None))
        conn.commit()
    finally:
        conn.close()

    _orig = cfg_mod.load_config

    def _cap(*a, **k):
        c = _orig(*a, **k)
        c["capital"] = 20000.0
        return c

    cfg_mod.load_config = _cap
    try:
        m = E.run_backtest(score_table=ST, run_tag=TAG, start="20190722")
    finally:
        cfg_mod.load_config = _orig
    print(f"复验回测完成 ({time.time()-t0:.0f}s): run={m['run_date']}")

    # 分段判决（对照 = 现役 pin run）
    conn = db.get_conn()   # 回测段后的连接已关, 判决段重开
    pool = pd.DataFrame(conn.execute("SELECT trade_date, close FROM pool_index").fetchall(),
                        columns=["trade_date", "close"])
    pool["trade_date"] = pd.to_datetime(pool["trade_date"])
    pool = pool.set_index("trade_date")["close"].sort_index()
    mm = pd.DataFrame(conn.execute(
        "SELECT run_date, metrics FROM backtest_summary WHERE run_date IN (?, ?)",
        (CTX_RUN, m["run_date"])).fetchall(), columns=["run_date", "metrics"])
    ms = {r.run_date: json.loads(r.metrics) for r in mm.itertuples()}
    # 2026-09-28 审查: 原 `TAG and m["run_date"]` 恒等于后者(TAG 为真值常量), 表达式
    # 看似"取 TAG 对应的 run"实为取本次回测 run —— 如实化, 防日后改 TAG 时踩坑。
    ctl, arm = ms[CTX_RUN], ms[m["run_date"]]

    def seg_p1(run, idle):
        c2 = db.get_conn()
        try:
            d = pd.DataFrame(c2.execute(
                "SELECT trade_date, sys_nav FROM backtest_nav WHERE run_date=? ORDER BY trade_date",
                (run,)).fetchall(), columns=["trade_date", "sys_nav"])
        finally:
            c2.close()
        d["trade_date"] = pd.to_datetime(d["trade_date"])
        nav = d.set_index("trade_date")["sys_nav"]
        L = 1.0 - idle
        V = L * (pool.reindex(nav.index) / pool.reindex(nav.index).iloc[0]) + (1 - L)
        V = V / V.iloc[0]
        rs = nav.pct_change().dropna()
        sel = (rs.index >= "2020-09-01") & (rs.index <= "2023-12-31")
        return (1 + rs[sel]).prod() ** (12 / int(sel.sum())) - 1

    d1 = seg_p1(m["run_date"], arm["avg_cash_idle"]) - seg_p1(CTX_RUN, ctl["avg_cash_idle"])
    dfull = arm["sys_ann_return"] - ctl["sys_ann_return"]
    dmdd = arm["sys_max_drawdown"] - ctl["sys_max_drawdown"]
    passed = (d1 >= CRIT["dead_floor"] and dfull >= CRIT["full_gain"]
              and dmdd <= CRIT["mdd_worse_max"])
    near = abs(dfull - 0.0364) <= 0.015
    print(f"\n===== 复验判决 =====")
    print(f"死区Δ {100*d1:+.2f}pp（不伤害 {'✓' if d1 >= CRIT['dead_floor'] else '✗'}）")
    print(f"全期Δ {100*dfull:+.2f}pp（改善 {'✓' if dfull >= CRIT['full_gain'] else '✗'}）")
    print(f"回撤劣化 {100*dmdd:+.2f}pp（{'✓' if dmdd <= CRIT['mdd_worse_max'] else '✗'}）")
    print(f"判据: {'采纳候选 ✅' if passed else '不过'} | 与研究侧邻域一致性: "
          f"{'✓ (差 {:+.2f}pp)'.format((dfull - 0.0364) * 100) if near else '✗ 偏离超 1.5pp'}")

    out_json = BASE / "data" / "kg" / "excluder_verify_result.json"
    out_json.write_text(json.dumps({
        "date": time.strftime("%Y-%m-%d %H:%M:%S"), "theta": THETA,
        "run": m["run_date"], "ctl": CTX_RUN,
        "kept": kept, "excluded": excluded,
        "d_dead": round(d1, 4), "d_full": round(dfull, 4), "d_mdd": round(dmdd, 4),
        "passed": bool(passed), "near_research": bool(near),
        "criteria": CRIT,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print("结果 →", out_json)
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
