"""第四轮 · 三信号负向剔除器（预注册 plans/negative-excluder-round4-20260917.md）

合并拥挤分 = 0.5×z(图谱拥挤分) + 0.25×z(id60_late_ret_5d) + 0.25×z(id60_close_vs_dayvwap_5d)
θ 扫描 {1.0, 1.5, 2.0} → 剔除表 → 组合回测 → 两轴判决（死区不伤害 + 全期 ≥+1pp + 回撤）。

用法: python scripts/negative_excluder_r4.py [theta]   # 无参 = 跑全部三档 + 判决表
"""
import json
import sys
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

KG_SCORES = BASE / "data" / "kg" / "excluder_scores.jsonl"
MIN60 = BASE / "data" / "min60_derived" / "daily.parquet"
CTX = "20260912_limitquant"     # 对照臂（现役 pin）
CRIT = {"dead_floor": -0.005, "full_gain": 0.01, "mdd_worse_max": 0.01}


def _z(s: pd.Series) -> pd.Series:
    sd = s.std()
    return (s - s.mean()) / sd if sd and np.isfinite(sd) else s * 0.0


def build_crowd() -> dict:
    """{锚点日: {ts_code: crowd}} —— 三信号合并（全部只用 ≤锚点日数据）。"""
    # ① 图谱拥挤分（第三轮 LLM 打分；锚点=月度）
    kg = {}
    if KG_SCORES.exists():
        for l in KG_SCORES.open(encoding="utf-8"):
            r = json.loads(l)
            kg.setdefault(r["anchor"], {})[r["ts_code"]] = float(r["crowd"])
    # ② id60 两基列：日频 → 每月最后交易日取 5 日滚动值（锚点=月末，与月度候选对齐）
    d = pd.read_parquet(MIN60)
    d = d[(d["x2"].abs() <= 0.21) & (d["x3"].abs() <= 0.15)]
    late_ret = d.pivot(index="date", columns="ts_code", values="x2")
    cvw = d.pivot(index="date", columns="ts_code", values="x3")
    late_ret.index = pd.to_datetime(late_ret.index)
    cvw.index = pd.to_datetime(cvw.index)
    lr5 = late_ret.rolling(5).sum().resample("ME").last()
    cv5 = cvw.rolling(5).mean().resample("ME").last()

    anchors = sorted(set(lr5.dropna(how="all").index.strftime("%Y%m%d"))
                     | set(kg.keys()))
    out = {}
    for a in anchors:
        at = pd.Timestamp(a)
        z_kg = _z(pd.Series(kg.get(a, {})))
        z_lr = _z(lr5.loc[:at].iloc[-1])
        z_cv = _z(cv5.loc[:at].iloc[-1])
        codes = set(z_kg.dropna().index) | set(z_lr.dropna().index) | set(z_cv.dropna().index)
        crowd = {}
        for code in codes:
            v = (0.5 * float(z_kg.get(code, 0.0) or 0.0)
                 + 0.25 * float(z_lr.get(code, 0.0) or 0.0)
                 + 0.25 * float(z_cv.get(code, 0.0) or 0.0))
            if v == v:
                crowd[code] = round(v, 4)
        if crowd:
            out[a] = crowd
    return out


def run_arm(theta: int, crowd: dict) -> dict:
    conn = db.get_conn()
    try:
        ms = pd.read_sql("SELECT trade_date, ts_code, score, pred_ret FROM model_score "
                         "WHERE trade_date >= '20200601' ORDER BY trade_date, score DESC", conn)
    finally:
        conn.close()
    out_rows, kept, excluded = [], 0, 0
    for d, g in ms.groupby("trade_date"):
        cs = crowd.get(d)
        g = g.sort_values("score", ascending=False)
        if not cs:
            out_rows.append(g)
            continue
        hot = {c for c, v in cs.items() if v >= theta}
        keep_n, excl = 0, set()
        for _, row in g.iterrows():
            if row["ts_code"] in hot and keep_n < 100:
                excl.add(row["ts_code"])
                excluded += 1
                continue
            keep_n += 1
            if keep_n >= 100:
                break
        kept += keep_n
        out_rows.append(g[~g["ts_code"].isin(excl)])
    out = pd.concat(out_rows, ignore_index=True)
    tag = f"excl4_theta{str(theta).replace('.', '')}"
    st = f"model_score_{tag}"
    conn = db.get_conn()
    try:
        conn.execute(f"DROP TABLE IF EXISTS {st}")
        conn.execute(f"CREATE TABLE {st} (ts_code TEXT, trade_date TEXT, score REAL, "
                     "pred_ret REAL, PRIMARY KEY (ts_code, trade_date)) WITHOUT ROWID")
        conn.executemany(f"INSERT OR REPLACE INTO {st} VALUES (?,?,?,?)",
                         out[["ts_code", "trade_date", "score", "pred_ret"]].itertuples(index=False, name=None))
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
        m = E.run_backtest(score_table=st, run_tag=tag, start="20190722")
    finally:
        cfg_mod.load_config = _orig
    print(f"θ={theta}: 保留 {kept:,} 剔除 {excluded:,} → sys_ann {m['sys_ann_return']:+.2%} "
          f"mdd {m['sys_max_drawdown']:.2%} (run {m['run_date']})")
    return {"theta": theta, "run": m["run_date"], "sys_ann": m["sys_ann_return"],
            "sys_mdd": m["sys_max_drawdown"], "idle": m["avg_cash_idle"],
            "kept": kept, "excluded": excluded}


def seg_p1(run: str, idle: float, pool: pd.Series) -> float:
    conn = db.get_conn()
    try:
        d = pd.read_sql("SELECT trade_date, sys_nav FROM backtest_nav WHERE run_date=? ORDER BY trade_date",
                        conn, params=(run,))
    finally:
        conn.close()
    d["trade_date"] = pd.to_datetime(d["trade_date"])
    nav = d.set_index("trade_date")["sys_nav"]
    L = 1.0 - idle
    V = L * (pool.reindex(nav.index) / pool.reindex(nav.index).iloc[0]) + (1 - L)
    V = V / V.iloc[0]
    rs = nav.pct_change().dropna()
    mm = (rs.index >= "2020-09-01") & (rs.index <= "2023-12-31")
    return (1 + rs[mm]).prod() ** (12 / int(mm.sum())) - 1


def main() -> int:
    crowd = build_crowd()
    print(f"合并拥挤分覆盖 {len(crowd)} 个锚点月")
    arms = [run_arm(th, crowd) for th in (1.0, 1.5, 2.0)]
    # 判决
    conn = db.get_conn()
    try:
        pool = pd.read_sql("SELECT trade_date, close FROM pool_index", conn)
        m = pd.read_sql(f"SELECT run_date, metrics FROM backtest_summary WHERE run_date='{CTX}'", conn)
    finally:
        conn.close()
    pool["trade_date"] = pd.to_datetime(pool["trade_date"])
    pool = pool.set_index("trade_date")["close"].sort_index()
    ctl_m = json.loads(m["metrics"].iloc[0])
    ctl_p1 = seg_p1(CTX, ctl_m["avg_cash_idle"], pool)
    print(f"\n{'档':<6} {'死区Δ':>9} {'全期Δ':>9} {'回撤劣化':>9}  判决")
    verdicts = {}
    for a in arms:
        d1 = seg_p1(a["run"], a["idle"], pool) - ctl_p1
        dfull = a["sys_ann"] - ctl_m["sys_ann_return"]
        dmdd = a["sys_mdd"] - ctl_m["sys_max_drawdown"]
        passed = (d1 >= CRIT["dead_floor"] and dfull >= CRIT["full_gain"]
                  and dmdd <= CRIT["mdd_worse_max"])
        verdicts[str(a["theta"])] = {"d_dead": round(d1, 4), "d_full": round(dfull, 4),
                                     "d_mdd": round(dmdd, 4), "passed": bool(passed)}
        print(f"θ={a['theta']:<4} {100*d1:>+8.2f}pp {100*dfull:>+8.2f}pp {100*dmdd:>+8.2f}pp  "
              f"{'采纳' if passed else '不过'}")
    best = [v for v in verdicts.values() if v["passed"]]
    print("\n总判决:", ("采纳候选 = θ=" + max(
        (v for v in verdicts.items() if v[1]["passed"]), key=lambda kv: kv[1]["d_full"])[0]
        if best else "全不过 —— 负向剔除方向整体结清"))
    out = BASE / "data" / "kg" / "negative_excluder_r4_result.json"
    out.write_text(json.dumps({"criteria": CRIT, "ctl": CTX, "arms": arms,
                               "verdicts": verdicts,
                               "note": "采纳候选仍需组合层复验+重钉流程(另行拍板)"},
                              ensure_ascii=False, indent=1), encoding="utf-8")
    print("结果 →", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
