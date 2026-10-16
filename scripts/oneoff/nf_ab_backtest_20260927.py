# -*- coding: utf-8 -*-
"""新因子构型组合层 A/B —— `plans/nf-fullretrain-prereg-20260926.md` §五 的执行体。

四张已落库评分表(nfab_ctrl_sa/sb、nfab_trt_sa/sb, 见 fullretrain_nf_ab_20260926.py)
× 生产路径回测(E.run_backtest 生产默认, 单一变量 = 评分表) × 双窗
(prod = eval_start 起 / full = 全史)。判据见预注册 §五:
Δsys_ann(prod) 双种子集同向且均值 ≥ +1.0pp/年; Δsys_mdd 劣化 < 2pp(两窗均值)。
产物 experiments/nf_ab_backtest_20260927.json; run 留痕 tag 前缀 nfab_(保留不自清)。
"""
import json
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app import config as cfg_mod
from app.backtest import engine as E

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "experiments" / "nf_ab_backtest_20260927.json"

TABLES = ["nfab_ctrl_sa", "nfab_ctrl_sb", "nfab_trt_sa", "nfab_trt_sb"]
ARMS = {"sa": ("nfab_ctrl_sa", "nfab_trt_sa"), "sb": ("nfab_ctrl_sb", "nfab_trt_sb")}


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def main() -> int:
    cfg = cfg_mod.load_config()
    eval_start = cfg.get("eval_start")
    windows = [("prod", eval_start), ("full", None)]
    log(f"eval_start={eval_start}, 表 {TABLES}, 窗 {[w[0] for w in windows]}")

    out = {"ran_at": datetime.now().isoformat(timespec="seconds"),
           "eval_start": eval_start, "runs": {}}
    t0 = time.time()
    for table in TABLES:
        for wname, start in windows:
            tag = f"nfab_{table}_{wname}"
            m = E.run_backtest(score_table=table, run_tag=tag, start=start)
            out["runs"][f"{table}_{wname}"] = {
                "run_date": m["run_date"], "periods": m.get("periods"),
                "ann_return": round(m.get("ann_return", float("nan")), 4),
                "max_drawdown": round(m.get("max_drawdown", float("nan")), 4),
                "sharpe": round(m.get("sharpe", float("nan")), 3),
                "sys_ann_return": round(m.get("sys_ann_return", float("nan")), 4),
                "sys_max_drawdown": round(m.get("sys_max_drawdown", float("nan")), 4),
                "sys_mdd_daily": round(m.get("sys_mdd_daily", float("nan")), 4),
                "ic_mean": round(m.get("ic_mean", float("nan")), 4),
                "icir": round(m.get("icir", float("nan")), 3),
                "avg_turnover": round(m.get("avg_turnover", float("nan")), 4),
                "avg_cash_idle": round(m.get("avg_cash_idle", float("nan")), 4),
                "avg_stocks": m.get("avg_stocks"),
            }
            log(f"{tag} 完成({time.time()-t0:.0f}s): sys_ann {m.get('sys_ann_return'):+.2%}, "
                f"sys_mdd {m.get('sys_max_drawdown'):+.2%}, 换手 {m.get('avg_turnover'):.2%}, "
                f"{m.get('periods')} 期")

    # ---------- 判读(预注册 §五) ----------
    deltas = {}
    for sname, (c_tab, t_tab) in ARMS.items():
        d = {}
        for w in ("prod", "full"):
            c, t = out["runs"][f"{c_tab}_{w}"], out["runs"][f"{t_tab}_{w}"]
            d[f"sys_ann_{w}"] = round(t["sys_ann_return"] - c["sys_ann_return"], 4)
            d[f"sys_mdd_{w}"] = round(t["sys_max_drawdown"] - c["sys_max_drawdown"], 4)
        deltas[sname] = d
    same_dir = all(d["sys_ann_prod"] > 0 for d in deltas.values())
    mean_d_ann = float(sum(d["sys_ann_prod"] for d in deltas.values()) / 2)
    mean_d_mdd = float(sum(d["sys_mdd_prod"] for d in deltas.values()) / 2)
    mean_d_mdd_full = float(sum(d["sys_mdd_full"] for d in deltas.values()) / 2)
    c1 = bool(same_dir and mean_d_ann >= 0.010)
    c2 = bool(mean_d_mdd > -0.02 and mean_d_mdd_full > -0.02)
    out["deltas"] = deltas
    out["verdict"] = {
        "delta_sys_ann_prod": {s: deltas[s]["sys_ann_prod"] for s in deltas},
        "mean_delta_sys_ann_prod_pp": round(mean_d_ann * 100, 2),
        "mean_delta_sys_mdd_prod_pp": round(mean_d_mdd * 100, 2),
        "mean_delta_sys_mdd_full_pp": round(mean_d_mdd_full * 100, 2),
        "c1_双种子集同向且≥+1pp/年": c1,
        "c2_回撤轴劣化<2pp": c2,
        "ADOPT_PACKAGE": bool(c1 and c2),
    }
    log(f"判据: {out['verdict']}")
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str),
                   encoding="utf-8")
    log(f"产物: {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
