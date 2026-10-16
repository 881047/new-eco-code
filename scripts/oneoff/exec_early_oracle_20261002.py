# -*- coding: utf-8 -*-
"""执行时点前移 oracle 上界探针（plans/exec-early-prereg-20261002.md，跑判决前冻结）。
只读零写库。量"组合敞口从 t0+1 开盘前移到 t0 收盘"的隔夜漂移上界。"""
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import numpy as np
import pandas as pd

from app import db
from app.factors.factor_engine import load_market, month_end_dates

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "experiments" / "exec_early_oracle_20261002.json"
TOPN = 6
WINDOWS = {"prod": "20190731", "full": "20170126"}


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def main() -> int:
    m = load_market()
    ca = (m["close"] * m["adj"]).astype(float)
    oa = (m["open"] * m["adj"]).astype(float)
    td = pd.DatetimeIndex(m["close"].index)
    # 个股隔夜漂移面板: open_adj(t+1)/close_adj(t) − 1
    gap = oa.shift(-1) / ca - 1.0
    ew_mkt = gap.mean(axis=1)                      # 全市场 EW 隔夜基准(无 bench 开盘, 注记)

    with db.conn_ctx() as conn:
        ms = pd.read_sql("SELECT trade_date, ts_code, score FROM model_score", conn)
    ms["d"] = pd.to_datetime(ms["trade_date"], format="%Y%m%d")

    md = month_end_dates(td)
    out = {"asof": datetime.now().isoformat(timespec="seconds"),
           "criteria_file": "plans/exec-early-prereg-20261002.md",
           "windows": {}}
    prev_picks = None
    churn_rows = []
    for wname, start in WINDOWS.items():
        rows = []
        for d in [x for x in md if pd.Timestamp(start) <= x <= td[-2]]:
            i = td.get_loc(d)
            g = ms[ms["d"] == d].dropna(subset=["score"])
            if len(g) < 50 or i + 1 >= len(td):
                continue
            top6 = g.nlargest(TOPN, "score")["ts_code"].tolist()
            gi = gap.iloc[i].reindex(top6)
            gm = ew_mkt.iloc[i]
            if gi.notna().sum() < 4:
                prev_picks = set(top6)
                continue
            port = float(gi.mean())
            mkt = float(gm) if pd.notna(gm) else np.nan
            rows.append({"date": d.strftime("%Y%m%d"),
                         "port_gap": round(port, 5), "mkt_gap": round(mkt, 5),
                         "excess": round(port - mkt, 5)})
            if prev_picks is not None and wname == "prod":
                entering = [c for c in top6 if c not in prev_picks]
                exiting = [c for c in prev_picks if c not in top6]
                ge = gap.iloc[i].reindex(entering).dropna()
                gx = gap.iloc[i].reindex(exiting).dropna()
                if len(ge) and len(gx):
                    churn_rows.append(float(ge.mean() - gx.mean()))
            prev_picks = set(top6)
        dfm = pd.DataFrame(rows)
        s = dfm["excess"].dropna()
        mean, sd = float(s.mean()), float(s.std(ddof=1))
        t = mean / sd * (len(s) ** 0.5) if sd > 0 else float("nan")
        churn_mean = float(np.mean(churn_rows)) if churn_rows and wname == "prod" else None
        out["windows"][wname] = {
            "n_months": int(len(s)),
            "port_gap_monthly_mean": round(float(dfm["port_gap"].mean()), 5),
            "mkt_gap_monthly_mean": round(float(dfm["mkt_gap"].mean()), 5),
            "excess_monthly_mean": round(mean, 5),
            "excess_ann": round(mean * 12, 4),
            "t": round(t, 2),
            "win_rate": round(float((s > 0).mean()), 4),
            "churn_net_gap_monthly": round(churn_mean, 5) if churn_mean is not None else None,
        }
        log(f"[{wname}] 组合隔夜 {dfm['port_gap'].mean():+.4f}/月 vs 市场 "
            f"{dfm['mkt_gap'].mean():+.4f}/月 ⇒ 超额 {mean:+.4f}/月 "
            f"(年化 {mean*12:+.2%}, t={t:.2f}, 胜率 {(s>0).mean():.0%})"
            + (f" | 换手净效应 {churn_mean:+.4f}/月" if churn_mean is not None else ""))

    # 冻结判据(prod 主判)
    w = out["windows"]["prod"]
    ex = w["excess_monthly_mean"]
    if ex >= 0.0010:
        verdict = "GO"
    elif ex < 0.0005:
        verdict = "NO-GO"
    else:
        verdict = "INDETERMINATE"
    cn = w.get("churn_net_gap_monthly")
    if verdict == "GO" and cn is not None and cn < 0 and abs(cn) >= 0.5 * ex:
        verdict = "INDETERMINATE(换手净效应侵蚀条款)"
    out["verdict"] = verdict
    out["criteria"] = {"GO_ge_0.10pct_month": None, "verdict": verdict}
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str),
                   encoding="utf-8")
    log(f"判决: {verdict}")
    log(f"产物: {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
