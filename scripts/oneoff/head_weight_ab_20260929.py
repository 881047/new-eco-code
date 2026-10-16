# -*- coding: utf-8 -*-
"""B1 多头端样本加权 A/B —— plans/head-weight-prereg-20260929.md 执行体。

臂：ctrl_A = 生产 model_score（不训，回测验证逐位复现 pin）；
B1_A = head_weight=3.0 × 生产种子组；ctrl_B / B1_B = 生产构型 / B1 × 第二种子组。
判据（预注册 §判据）：组合层档 P1 双种子集 Δsys_ann ≥+2.0pp 同向（P2 回撤 / P3 换手）；
0<Δ<2.0pp 同向 = INDETERMINATE（效应小于判定力）；异向/负 = REJECT。
隔离表 + archive_dir 显式（2026-09-11 归档护栏）。回测留痕 20260929_abhw_* 保留不自清。
"""
import io
import json
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("hw")

from ab_governor_overlay import _metrics_from  # noqa: E402
from app import db  # noqa: E402  (ab_governor_overlay 导入时已 init)
from app.backtest import engine as E  # noqa: E402
from app.models import expert, trainer  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "experiments" / "head_weight_ab_20260929.json"
PREREG = "plans/head-weight-prereg-20260929.md"
HEAD_W = 3.0
SEEDS_A = list(trainer.ENSEMBLE_SEEDS)
SEEDS_B = [7, 13, 99, 2024]
WIN = {"start": "20190731", "end": "20260831"}
PIN = {"ann": 0.3168, "mdd": -0.2363, "sys_ann": 0.2913, "sys_mdd": -0.1817}
PIN_TOL = 5e-4

ARMS = {
    # name: (score_table, archive_dir, seeds, head_weight, train)
    "ctrl_A": ("model_score", None, SEEDS_A, None, False),
    "B1_A": (f"model_score_hw{int(HEAD_W)}_a", f"data/models/hw{int(HEAD_W)}_a",
             SEEDS_A, HEAD_W, True),
    "ctrl_B": ("model_score_hw_base_b", "data/models/hw_base_b", SEEDS_B, None, True),
    "B1_B": (f"model_score_hw{int(HEAD_W)}_b", f"data/models/hw{int(HEAD_W)}_b",
             SEEDS_B, HEAD_W, True),
}


def main() -> int:
    t0 = time.time()
    log.info("种子集 A=%s B=%s head_weight=%.1f", SEEDS_A, SEEDS_B, HEAD_W)

    results = {}
    for name, (table, arch, seeds, hw, train) in ARMS.items():
        if train:
            import shutil
            expert.ensure_score_table(table)
            imp_table = table.replace("model_score", "model_importance")
            with db.conn_ctx() as conn:
                # importance 表用与 db.init_db 同款 DDL（trainer 写入不自建表）
                conn.execute(f"""CREATE TABLE IF NOT EXISTS {imp_table} (
                    train_date TEXT, factor TEXT, importance REAL,
                    PRIMARY KEY (train_date, factor)) WITHOUT ROWID""")
                conn.execute(f"DELETE FROM {table}")
                conn.execute(f"DELETE FROM {imp_table}")
                conn.commit()
            if Path(arch).exists():
                shutil.rmtree(arch)
            t1 = time.time()
            log.info("[%s] 全量重训开始 (seeds=%s, head_weight=%s)", name, seeds, hw)
            trainer.run(score_table=table,
                        importance_table=table.replace("model_score", "model_importance"),
                        archive_dir=ROOT / arch, full=True, backtest_start="2017-01-01",
                        num_boost_round=trainer.ENSEMBLE_ROUNDS, early_stopping_rounds=0,
                        ensemble_seeds=seeds, train_months=36, holdout_recent=6,
                        head_weight=hw)
            log.info("[%s] 重训完成 %.0fs", name, time.time() - t1)
        m = E.run_backtest(score_table=table, run_tag=f"20260929_abhw_{name}", **WIN)
        # 期度口径 = governor._metrics_from 整理后的键（pin 对账与 Δ 判决用这把尺）
        mf = _metrics_from(m["run_date"], m)
        results[name] = {"run_date": m["run_date"], "score_table": table,
                         "periods": int(mf.get("periods") or 0),
                         "ann": round(float(mf["ann"]), 4),
                         "mdd": round(float(mf["mdd"]), 4),
                         "sys_ann": round(float(mf["sys_ann"]), 4),
                         "sys_mdd": round(float(mf["sys_mdd"]), 4),
                         "sys_mdd_daily": round(float(mf["sys_mdd_daily"]), 4),
                         "turnover": mf.get("turnover")}
        log.info("[%s] ann %.4f mdd %.4f sys %.4f/%.4f/%.4f turn %s",
                 name, results[name]["ann"], results[name]["mdd"],
                 results[name]["sys_ann"], results[name]["sys_mdd"],
                 results[name]["sys_mdd_daily"], results[name]["turnover"])

    pin_check = {k: abs(results["ctrl_A"][k] - v) <= PIN_TOL for k, v in PIN.items()}
    dA = {k: round(results["B1_A"][k] - results["ctrl_A"][k], 4)
          for k in ("sys_ann", "sys_mdd")}
    dB = {k: round(results["B1_B"][k] - results["ctrl_B"][k], 4)
          for k in ("sys_ann", "sys_mdd")}
    same_dir = dA["sys_ann"] > 0 and dB["sys_ann"] > 0
    both_neg = dA["sys_ann"] < 0 and dB["sys_ann"] < 0
    p1_pass = same_dir and dA["sys_ann"] >= 0.02 and dB["sys_ann"] >= 0.02
    if p1_pass and dA["sys_mdd"] > -0.02 and dB["sys_mdd"] > -0.02:
        verdict = "PASS 构型改良候选（进拍板，采纳须重钉另议）"
    elif same_dir or both_neg:
        verdict = ("INDETERMINATE（双种子同向但效应小于判定力门槛 +2.0pp，如实结清）"
                   if same_dir else "REJECT 线结清（双种子同向为负）")
    else:
        verdict = "REJECT 线结清（双种子异向 = 噪声）"
    log.info("ΔA=%s ΔB=%s | pin 门 %s", dA, dB, all(pin_check.values()))
    log.info("判决: %s", verdict)

    out = {"ran_at": datetime.now().isoformat(timespec="seconds"), "prereg": PREREG,
           "seeds_A": SEEDS_A, "seeds_B": SEEDS_B, "head_weight": HEAD_W,
           "windows": WIN, "arms": results, "pin_check": pin_check,
           "deltas": {"A": dA, "B": dB}, "verdict": verdict,
           "runtime_sec": round(time.time() - t0, 1)}
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str),
                   encoding="utf-8")
    print(f"产物: {OUT}  ({out['runtime_sec']}s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
