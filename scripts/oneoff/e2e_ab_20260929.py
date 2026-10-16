# -*- coding: utf-8 -*-
"""E2E-DL 双种子集 A/B —— plans/e2e-dl-prereg-20260929.md 执行体。

臂：E2E_A（种子组 A=[42,43,44,45]）vs 生产 model_score（不训）；
E2E_B（种子组 B=[7,13,99,2024]）vs ctrl_B = model_score_hw_base_b（B1 轮已跑的
种子组 B 生产 LightGBM 隔离表，同款"跨种子世界"对照——F76b 纪律：双种子集配对）。
判据（预注册 §判据）：P1 双种子集 Δsys_ann ≥ +2.0pp 同向；P2 回撤劣化 <2pp；
P3 换手 ≤ +5pp（E2E 病理警示针对性判据）；0<Δ<2pp 同向 = INDETERMINATE。
回测留痕 20260929_abe2e_* 保留不自清。
"""
import io
import json
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# 必须在 numpy import 前设：OpenBLAS/MKL 多线程偶发死锁（2019-09 期事故，
# CPU 时间 45s 只涨 0.06s = 0.13% 利用率空转）——本负载矩阵小，单线程几乎无损失
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("e2e")

from ab_governor_overlay import _metrics_from  # noqa: E402
from app.backtest import engine as E  # noqa: E402
from app.models import e2e  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "experiments" / "e2e_ab_20260929.json"
PREREG = "plans/e2e-dl-prereg-20260929.md"
WIN = {"start": "20190731", "end": "20260831"}
SEEDS_A = [42, 43, 44, 45]
SEEDS_B = [7, 13, 99, 2024]
PIN = {"ann": 0.3168, "mdd": -0.2363, "sys_ann": 0.2913, "sys_mdd": -0.1817}
PIN_TOL = 5e-4


def main() -> int:
    t0 = time.time()
    arms = {}
    for tag, seeds, table in (("E2E_A", SEEDS_A, "model_score_e2e_ts1_a"),
                              ("E2E_B", SEEDS_B, "model_score_e2e_ts1_b")):
        t1 = time.time()
        log.info("[%s] walk-forward 训练开始 (seeds=%s)", tag, seeds)
        e2e.run_e2e(table=table, seeds=seeds, skip_existing=True)
        log.info("[%s] 训练完成 %.0fs", tag, time.time() - t1)

    def bt(table: str, tag: str) -> dict:
        m = E.run_backtest(score_table=table, run_tag=f"20260929_abe2e_{tag}", **WIN)
        mf = _metrics_from(m["run_date"], m)
        out = {"run_date": m["run_date"], "score_table": table,
               "periods": int(mf.get("periods") or 0),
               "ann": round(float(mf["ann"]), 4), "mdd": round(float(mf["mdd"]), 4),
               "sys_ann": round(float(mf["sys_ann"]), 4),
               "sys_mdd": round(float(mf["sys_mdd"]), 4),
               "sys_mdd_daily": round(float(mf["sys_mdd_daily"]), 4),
               "turnover": mf.get("turnover")}
        log.info("[%s] ann %.4f mdd %.4f sys %.4f/%.4f turn %s", tag, out["ann"],
                 out["mdd"], out["sys_ann"], out["sys_mdd"], out["turnover"])
        return out

    arms["prod_A"] = bt("model_score", "prodA")          # 有效性门：逐位复现 pin
    arms["E2E_A"] = bt("model_score_e2e_ts1_a", "E2E_A")
    arms["prod_B"] = bt("model_score_hw_base_b", "prodB")  # B1 轮 ctrl_B 复用
    arms["E2E_B"] = bt("model_score_e2e_ts1_b", "E2E_B")

    pin_check = {k: abs(arms["prod_A"][k] - v) <= PIN_TOL for k, v in PIN.items()}
    dA = round(arms["E2E_A"]["sys_ann"] - arms["prod_A"]["sys_ann"], 4)
    dB = round(arms["E2E_B"]["sys_ann"] - arms["prod_B"]["sys_ann"], 4)
    dAm = round(arms["E2E_A"]["sys_mdd"] - arms["prod_A"]["sys_mdd"], 4)
    dBm = round(arms["E2E_B"]["sys_mdd"] - arms["prod_B"]["sys_mdd"], 4)
    turnA = (arms["E2E_A"]["turnover"] or 0) - (arms["prod_A"]["turnover"] or 0)
    same_dir = dA > 0 and dB > 0
    both_neg = dA < 0 and dB < 0
    p1 = same_dir and dA >= 0.02 and dB >= 0.02
    p2 = dAm > -0.02 and dBm > -0.02
    p3 = turnA <= 0.05
    if p1 and p2 and p3:
        verdict = "PASS 构型改良候选（进拍板，采纳须重钉另议）"
    elif same_dir:
        verdict = "INDETERMINATE（双种子同向但效应小于判定力门槛 +2.0pp，如实结清）"
    elif both_neg:
        verdict = "REJECT 线结清（双种子同向为负）"
    else:
        verdict = "REJECT 线结清（双种子异向 = 噪声）"
    log.info("ΔA=%+.4f ΔB=%+.4f | mdd Δ %+.4f/%+.4f | turn Δ %+.4f | pin 门 %s",
             dA, dB, dAm, dBm, turnA, all(pin_check.values()))
    log.info("判决: %s", verdict)

    out = {"ran_at": datetime.now().isoformat(timespec="seconds"), "prereg": PREREG,
           "seeds_A": SEEDS_A, "seeds_B": SEEDS_B, "windows": WIN,
           "arms": arms, "pin_check": pin_check,
           "deltas": {"A": dA, "B": dB, "A_mdd": dAm, "B_mdd": dBm, "turn": turnA},
           "P1": bool(p1), "P2": bool(p2), "P3": bool(p3),
           "verdict": verdict, "runtime_sec": round(time.time() - t0, 1)}
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str),
                   encoding="utf-8")
    print(f"产物: {OUT}  ({out['runtime_sec']}s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
