"""归因(2026-09-09): 回测年化从 38.51% 掉到 9.61%(ab0) 的主因 —— ST/退 PIT 过滤

背景: 0908 晚在 engine.py:361-364 加了与 builder 同口径的 PIT ST/退 候选过滤, 回测
年化 41.51%(含四舍五入修复) → 9.73%。本脚本量化该过滤的两个通道:

  通道 A(幻想 alpha, 合法剔除): 过滤掉的 ST/退 股在上月的实际收益 —— 这些是生产
      builder 永不买入的标的, 旧基线把它们当持仓, 属虚高。
  通道 B(无补位偏差, 需核对): engine 先 nlargest(n) 再过滤(过滤后 < n 只, 不再补),
     builder 先过滤再 nlargest(n)(始终 n 只) —— 同一规则两种实现, engine 比生产更保守。
     本脚本统计每月被"少买"的只数及补位标的的实际收益。

同时给出 ①(财务披露日顺延) 月度差值的显著性检验(nav 层面)。

运行: PYTHONPATH=".venv/Lib/site-packages" python scripts/oneoff/attrib_st_filter.py
"""
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import numpy as np
import pandas as pd

from app import db
from app.factors.factor_engine import load_market
from app.models.names import NameLookup

START, END = "20170126", "20260831"
STATE_STOCKS = {"attack": 6, "neutral": 5, "defense": 4}


def main():
    m = load_market()
    dates = list(m["close"].index)
    ac = m["close"] * m["adj"]
    nl = NameLookup()

    src = sys.argv[1] if len(sys.argv) > 1 else "live"
    conn = db.get_conn()
    if src == "bk":
        conn.execute("ATTACH DATABASE 'data/backup/neweco_20260909.db' AS bk")
        tbl = "bk.model_score"
        print("### 评分来源: 冷备 neweco_20260909.db(0909 18:15, 旧模型) ###")
    else:
        tbl = "model_score"
        print("### 评分来源: 生产 model_score(当前 ab2/fix0909 模型) ###")
    scores = pd.read_sql(f"SELECT ts_code, trade_date, score FROM {tbl}", conn)
    scores = scores[~scores["ts_code"].str.startswith("688")]  # star_enabled=false
    regime = pd.read_sql("SELECT trade_date, state FROM regime", conn)
    me_dates = set(pd.read_sql(
        "SELECT MAX(cal_date) AS d FROM trade_cal WHERE is_open=1 GROUP BY substr(cal_date,1,6)",
        conn)["d"])
    conn.close()
    state_s = regime.set_index("trade_date")["state"]
    sd = [d for d in sorted(scores["trade_date"].unique()) if d in me_dates
          and START <= d <= END]
    date_pos = {d: i for i, d in enumerate(dates)}

    def next_pos(d):
        return min(date_pos[pd.Timestamp(d)] + 1, len(dates) - 1)

    def month_ret(code, d, d1):
        """评分日 d 后首个交易日买入 → d1 后首个交易日卖出 的复权收益(engine 口径近似)"""
        i0, i1 = next_pos(d), next_pos(d1)
        a0, a1 = ac.iloc[i0].get(code), ac.iloc[i1].get(code)
        if pd.isna(a0) or pd.isna(a1) or a0 <= 0:
            return None
        return float(a1 / a0 - 1)

    def is_bad(code: str, d: str) -> bool:
        nm = nl.name_at(code, d)
        return bool(nm and ("ST" in nm or "退" in nm))

    rows = []
    for i in range(len(sd) - 1):
        d, d1 = sd[i], sd[i + 1]
        st = state_s.get(str(d), "neutral")
        n = int(STATE_STOCKS.get(st, 5))
        sub = scores[scores["trade_date"] == d]
        pool = sub.nlargest(n, "score")                     # engine: 先取 top-n
        is_bad_s = pool["ts_code"].map(lambda c: is_bad(c, str(d)))
        dropped = pool[is_bad_s]
        kept_engine = pool[~is_bad_s]
        # builder: 先过滤再 top-n
        clean = sub[~sub["ts_code"].map(lambda c: is_bad(c, str(d)))]
        top_builder = clean.nlargest(n, "score")
        backfill = top_builder[~top_builder["ts_code"].isin(kept_engine["ts_code"])]
        rows.append({
            "date": d, "state": st, "n": n,
            "n_engine": len(kept_engine), "n_builder": len(top_builder),
            "dropped": list(dropped["ts_code"]), "backfill": list(backfill["ts_code"]),
            "dropped_ret": [month_ret(c, d, d1) for c in dropped["ts_code"]],
            "backfill_ret": [month_ret(c, d, d1) for c in backfill["ts_code"]],
            "kept_ret": [month_ret(c, d, d1) for c in kept_engine["ts_code"]],
        })

    df = pd.DataFrame(rows)
    hit = df[df["dropped"].map(len) > 0]
    print(f"评估期 {sd[0]}~{sd[-1]} 共 {len(df)} 个月")
    print(f"ST/退 命中月数: {len(hit)} / {len(df)}  ({len(hit)/len(df):.1%})")
    print(f"被过滤的 ST/退 持仓次数: {sum(len(x) for x in df['dropped'])} 只次")
    print(f"其中 engine 少买(builder 会补位)的只次: {sum(len(x) for x in df['backfill'])}")
    print(f"平均实际持仓: engine {df['n_engine'].mean():.2f} 只 vs builder {df['n_builder'].mean():.2f} 只")
    print()
    drop_all = [r for lst in df["dropped_ret"] for r in lst if r is not None]
    fill_all = [r for lst in df["backfill_ret"] for r in lst if r is not None]
    kept_all = [r for lst in df["kept_ret"] for r in lst if r is not None]
    print("通道 A —— 被剔除的 ST/退 标的月收益(幻想 alpha):")
    if drop_all:
        print(f"  样本 {len(drop_all)} 只次 | 均值 {np.mean(drop_all):+.2%} | 中位 {np.median(drop_all):+.2%} "
              f"| 胜率 {(np.array(drop_all)>0).mean():.1%}")
    print("通道 B —— 补位标的月收益(engine 因不补位而错过的):")
    if fill_all:
        print(f"  样本 {len(fill_all)} 只次 | 均值 {np.mean(fill_all):+.2%} | 中位 {np.median(fill_all):+.2%} "
              f"| 胜率 {(np.array(fill_all)>0).mean():.1%}")
    print("对照 —— 实际持有标的月收益:")
    print(f"  样本 {len(kept_all)} 只次 | 均值 {np.mean(kept_all):+.2%} | 中位 {np.median(kept_all):+.2%} "
          f"| 胜率 {(np.array(kept_all)>0).mean():.1%}")
    print()
    print("按年分布(命中月数 / 被剔只次 / 被剔标的均值收益):")
    df["year"] = df["date"].str[:4]
    for y, g in df.groupby("year"):
        n_hit = int((g["dropped"].map(len) > 0).sum())
        rets = [r for lst in g["dropped_ret"] for r in lst if r is not None]
        print(f"  {y}: 命中 {n_hit:2d}/{len(g):2d} 月, 剔除 {len(rets):2d} 只次, "
              f"均值 {np.mean(rets):+.2%}" if rets else f"  {y}: 命中 {n_hit:2d}/{len(g):2d} 月")
    print()
    print("命中明细:")
    for _, r in hit.iterrows():
        dr = [f"{c}({(v if v is not None else float('nan')):+.1%})"
              for c, v in zip(r["dropped"], r["dropped_ret"])]
        bf = [f"{c}({(v if v is not None else float('nan')):+.1%})"
              for c, v in zip(r["backfill"], r["backfill_ret"])]
        print(f"  {r['date']} {r['state']:>7} n={r['n']} 剔:{' '.join(dr)}"
              + (f" | 补位:{' '.join(bf)}" if bf else ""))

    # ---------- ① 的显著性检验(nav 层面) ----------
    print()
    print("=== ① 财务披露日顺延 的月度差值显著性 ===")
    conn = db.get_conn()
    a = pd.read_sql("SELECT trade_date, nav FROM backtest_nav WHERE run_date='20260909_ab0_ctrl' "
                    "ORDER BY trade_date", conn).set_index("trade_date")["nav"]
    b = pd.read_sql("SELECT trade_date, nav FROM backtest_nav WHERE run_date='20260909_ab1_ann' "
                    "ORDER BY trade_date", conn).set_index("trade_date")["nav"]
    conn.close()
    ra, rb = a.pct_change().dropna(), b.pct_change().dropna()
    ra.iloc[0] = a.iloc[0] / 10000.0 - 1
    rb.iloc[0] = b.iloc[0] / 10000.0 - 1
    dlog = np.log1p(rb) - np.log1p(ra)
    n = len(dlog)
    tot = dlog.sum()
    se = dlog.std(ddof=1) / math.sqrt(n)
    print(f"  月数 {n} | 累计 log 差 {tot:+.4f} (= 终值比 {math.exp(tot):.4f})")
    print(f"  月均差 {dlog.mean():+.4%} | 标准差 {dlog.std(ddof=1):.2%} | 标准误 {se:.4%} | t = {dlog.mean()/se:+.2f}")
    rng = np.random.default_rng(42)
    boot = np.array([rng.choice(dlog, n, replace=True).sum() for _ in range(20000)])
    print(f"  自助法(20000 次重排): 累计差的 5%/50%/95% 分位 = "
          f"{np.percentile(boot,5):+.3f} / {np.percentile(boot,50):+.3f} / {np.percentile(boot,95):+.3f}")
    print(f"  → P(累计差 < 0) = {(boot<0).mean():.3f}  (若接近 0.5 则 ① 的方向性是噪声)")


if __name__ == "__main__":
    main()
