# -*- coding: utf-8 -*-
"""vol 缩放层季度复跑驱动 —— `plans/volscale-overlay-prereg-20260927.md` §二的到期复查。

与 09-27 首跑(`scripts/oneoff/volscale_overlay_20260927.py`)的唯一差异:
  **NAV 源动态取自 config.yaml `backtest_run` 现役 pin**(prod=tag, full=tag+"_full"),
  而不是硬编码 20260927_excl5 —— 换 pin 后旧实验硬编码不可复用(2026-10-01 L3 教训)。
判据逐字同源(P1 MDD 改善≥5pp 一窗达标另一窗≤2pp 劣化 / P2 年化非劣≥−1.5pp /
P3 覆盖率 15-60%), overlay/metrics 直接 import oneoff 冻结实现, 只读零写库。

调度: NEWECO_volscale_recheck(2026-12-27 19:00 一次性, wscript 静默)。
判决处置: PASS ⇒ 提请生产接线预注册拍板; INDETERMINATE ⇒ 再延一季度;
REJECT ⇒ 线结清。登记 = 下一会话读产物写台账。
"""
import importlib.util
import json
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
ROOT = Path(__file__).resolve().parents[1]
ONEOFF = ROOT / "scripts" / "oneoff" / "volscale_overlay_20260927.py"

spec = importlib.util.spec_from_file_location("volscale_overlay_ref", ONEOFF)
_ref = importlib.util.module_from_spec(spec)
spec.loader.exec_module(_ref)          # 只取冻结函数 overlay/metrics 与判据常数
overlay, metrics = _ref.overlay, _ref.metrics
RF = _ref.RF


def current_pin_runs() -> dict:
    """config.yaml 现役 pin → {prod, full} run 名(运行时读, 防硬编码过期)。"""
    from app.config import load_config
    cfg = load_config()
    tag = cfg.get("backtest_run")
    if not tag or tag == "latest":
        raise RuntimeError(f"config backtest_run={tag!r} 不是具体 pin tag —— 拒绝猜测")
    return {"prod": str(tag), "full": str(tag) + "_full"}


def main() -> int:
    t0 = time.time()
    runs = current_pin_runs()
    today = datetime.now().strftime("%Y%m%d")
    out_path = ROOT / "experiments" / f"volscale_recheck_{today}.json"

    conn = sqlite3.connect(f"{(ROOT / 'data' / 'neweco.db').as_uri()}?mode=ro", uri=True)
    out_runs, checks_all = {}, {}
    for wname, run_date in runs.items():
        rows = conn.execute("SELECT COUNT(*) FROM backtest_nav_daily WHERE run_date=?",
                            (run_date,)).fetchone()
        if not rows or rows[0] == 0:
            raise RuntimeError(f"现役 pin 的 NAV run 不存在: {run_date} —— "
                               f"config 与留痕不一致, 拒绝跑(fail-closed)")
    for wname, run_date in runs.items():
        rows = conn.execute("SELECT trade_date, nav FROM backtest_nav_daily "
                            "WHERE run_date=? ORDER BY trade_date", (run_date,)).fetchall()
        import pandas as pd
        nav = pd.Series([r[1] for r in rows],
                        index=pd.to_datetime([r[0] for r in rows], format="%Y%m%d"))
        base_ret = nav.pct_change().dropna()
        r_ov, _w_by_day, w_periods = overlay(base_ret)
        m_base, m_ov = metrics(base_ret), metrics(r_ov)
        ov_periods = [p for p in w_periods if p["w"] < 1.0]
        cover = len(ov_periods) / max(1, len(w_periods))
        mdd_impr = m_ov["mdd"] - m_base["mdd"]
        ann_gap = m_ov["ann_return"] - m_base["ann_return"]
        out_runs[wname] = {
            "run_date": run_date, "base": m_base, "overlay": m_ov,
            "mdd_improvement": round(mdd_impr, 4), "ann_gap": round(ann_gap, 4),
            "w_coverage": round(cover, 3),
            "avg_w_when_active": round(float(np_mean([p["w"] for p in ov_periods]))
                                       if ov_periods else 1.0, 3),
            "n_periods": len(w_periods), "window_days": int(len(base_ret)),
            "w_series_sample": w_periods[-12:]}
        checks_all[wname] = {
            "p1_mdd_improve_ge_5pp": bool(mdd_impr >= 0.05),
            "p2_ann_non_inferior": bool(ann_gap >= -0.015),
            "p3_coverage_ok": bool(0.15 <= cover <= 0.60)}
        print(f"  {wname}({run_date}): base ann {m_base['ann_return']:+.4f} "
              f"mdd {m_base['mdd']:+.4f} | overlay ann {m_ov['ann_return']:+.4f} "
              f"mdd {m_ov['mdd']:+.4f} | MDD改善 {mdd_impr:+.4f} 覆盖 {cover:.0%}",
              flush=True)
    conn.close()

    p1 = bool((checks_all["prod"]["p1_mdd_improve_ge_5pp"]
               or checks_all["full"]["p1_mdd_improve_ge_5pp"])
              and all((checks_all[w]["p1_mdd_improve_ge_5pp"]
                       or out_runs[w]["mdd_improvement"] >= -0.02) for w in checks_all))
    p2 = all(c["p2_ann_non_inferior"] for c in checks_all.values())
    p3 = all(c["p3_coverage_ok"] for c in checks_all.values())
    if p1 and p2 and p3:
        verdict = "PASS"
    elif (not p2) or all(out_runs[w]["mdd_improvement"] < 0.02 for w in out_runs):
        verdict = "REJECT"
    else:
        verdict = "INDETERMINATE"
    print(f"判决: {verdict} (P1={p1} P2={p2} P3={p3})", flush=True)

    payload = {
        "ran_at": datetime.now().isoformat(timespec="seconds"),
        "kind": "volscale quarterly recheck (判据逐字同源 plans/volscale-overlay-prereg-20260927.md §二)",
        "pin_runs": runs, "runs": out_runs, "checks": checks_all,
        "checks_summary": {"p1": p1, "p2": p2, "p3": p3},
        "verdict": {"PASS": "PASS（提请生产接线预注册拍板）",
                    "REJECT": "REJECT（线结清）",
                    "INDETERMINATE": "INDETERMINATE"}[verdict],
        "runtime_sec": round(time.time() - t0, 1),
    }
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=1, default=str),
                        encoding="utf-8")
    print(f"产物: {out_path}  ({time.time() - t0:.0f}s)", flush=True)
    return 0


def np_mean(xs):
    import numpy as np
    return float(np.mean(xs)) if xs else 1.0


if __name__ == "__main__":
    sys.exit(main())
