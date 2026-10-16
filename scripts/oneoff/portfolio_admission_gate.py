# -*- coding: utf-8 -*-
"""组合层级准入判决器 —— `plans/portfolio-admission-gate-prereg-20260927.md` 的执行体。

门禁第三层（六判据门 → 单因子档 → **组合层档**）：给成对评分表（控制臂/候选臂，
各含 _sa/_sb 两套种子集变体）跑生产路径回测（E.run_backtest 生产默认参数，
单一变量 = 评分表；双窗 prod=eval_start 起 / full=全史），计算 Δ 读数并调用
`gate.verdict_portfolio` 机械判决（P1 收益 / P2 回撤 / P3 换手，判决词
PASS(进拍板) / REJECT(线结清) / INDETERMINATE(读数不可辨)）。人只做最终拍板。

用法（cwd 任意）:
  .venv/Scripts/python.exe -X utf8 scripts/oneoff/portfolio_admission_gate.py \
      --ctrl nfab_ctrl --cand nfab_trt --tag nf58
表名展开为 {ctrl,cand}_{sa,sb} 四张（须已在库，训练由 train_model.py 侧完成）。
只读生产 config；回测留痕 run_tag 前缀 padm_<tag>_（保留不自清）；
产物 experiments/padm_<tag>_<date>.json。
"""
import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app import config as cfg_mod
from app.alpha import gate
from app.backtest import engine as E

ROOT = Path(__file__).resolve().parents[2]
WINDOWS = ("prod", "full")


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def run_pair(table: str, eval_start) -> dict:
    """一张评分表 × 双窗 → 读数"""
    out = {}
    for w in WINDOWS:
        m = E.run_backtest(score_table=table, run_tag=f"padm_{table}_{w}",
                           start=(eval_start if w == "prod" else None))
        out[w] = {"sys_ann_return": float(m["sys_ann_return"]),
                  "sys_max_drawdown": float(m["sys_max_drawdown"]),
                  "avg_turnover": float(m["avg_turnover"]),
                  "periods": int(m["periods"])}
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ctrl", required=True, help="控制臂表名前缀（展开 _sa/_sb）")
    ap.add_argument("--cand", required=True, help="候选臂表名前缀（展开 _sa/_sb）")
    ap.add_argument("--cand-sa-table", default=None,
                    help="覆写候选 _sa 表名（种子噪声对照等构造性映射用）")
    ap.add_argument("--cand-sb-table", default=None,
                    help="覆写候选 _sb 表名（同上）")
    ap.add_argument("--tag", default=None, help="留痕/产物命名 tag（默认用 cand）")
    ap.add_argument("--ann-bar-prod", type=float, default=0.020)
    ap.add_argument("--ann-bar-full", type=float, default=0.010)
    ap.add_argument("--mdd-bar-prod", type=float, default=-0.020)
    ap.add_argument("--mdd-bar-full", type=float, default=-0.030)
    ap.add_argument("--turnover-bar", type=float, default=0.050)
    args = ap.parse_args()
    tag = args.tag or args.cand
    eval_start = cfg_mod.load_config().get("eval_start")

    tables = {}
    for arm, base in (("ctrl", args.ctrl), ("cand", args.cand)):
        for s in ("sa", "sb"):
            tables[f"{arm}_{s}"] = f"{base}_{s}"
    if args.cand_sa_table:
        tables["cand_sa"] = args.cand_sa_table
    if args.cand_sb_table:
        tables["cand_sb"] = args.cand_sb_table
    log(f"表: {tables} | eval_start={eval_start}")

    runs = {}
    t0 = time.time()
    for name, table in tables.items():
        runs[name] = run_pair(table, eval_start)
        log(f"{name}({table}) 完成({time.time()-t0:.0f}s): "
            f"prod sys_ann {runs[name]['prod']['sys_ann_return']:+.2%}, "
            f"full sys_ann {runs[name]['full']['sys_ann_return']:+.2%}")

    deltas = []
    for s in ("sa", "sb"):
        c, t = runs[f"ctrl_{s}"], runs[f"cand_{s}"]
        deltas.append({
            "seed_set": s,
            "sys_ann_prod": round(t["prod"]["sys_ann_return"] - c["prod"]["sys_ann_return"], 4),
            "sys_ann_full": round(t["full"]["sys_ann_return"] - c["full"]["sys_ann_return"], 4),
            "sys_mdd_prod": round(t["prod"]["sys_max_drawdown"] - c["prod"]["sys_max_drawdown"], 4),
            "sys_mdd_full": round(t["full"]["sys_max_drawdown"] - c["full"]["sys_max_drawdown"], 4),
            "turnover": round(t["prod"]["avg_turnover"] - c["prod"]["avg_turnover"], 4),
        })

    v = gate.verdict_portfolio(deltas, ann_bar_prod=args.ann_bar_prod,
                               ann_bar_full=args.ann_bar_full,
                               mdd_bar_prod=args.mdd_bar_prod,
                               mdd_bar_full=args.mdd_bar_full,
                               turnover_bar=args.turnover_bar)
    log(f"Δ 读数: {json.dumps(deltas, ensure_ascii=False)}")
    log(f"判决: {v['verdict']} | {v}")

    out = {"ran_at": datetime.now().isoformat(timespec="seconds"),
           "prereg": "plans/portfolio-admission-gate-prereg-20260927.md",
           "tag": tag, "eval_start": eval_start, "tables": tables,
           "runs": runs, "deltas": deltas,
           "thresholds": {"ann_bar_prod": args.ann_bar_prod,
                          "ann_bar_full": args.ann_bar_full,
                          "mdd_bar_prod": args.mdd_bar_prod,
                          "mdd_bar_full": args.mdd_bar_full,
                          "turnover_bar": args.turnover_bar},
           "verdict": v}
    out_path = ROOT / "experiments" / f"padm_{tag}_{datetime.now().strftime('%Y%m%d')}.json"
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str),
                        encoding="utf-8")
    log(f"产物: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
