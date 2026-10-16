"""M1（Q3）：IC 三套尺会不会翻健康档 —— 只读实测。

问题：`health._fwd_excess` 用 `open×adj_factor`（复权），`weekly/scores.forward_excess`
用未复权 `open`，`FROZEN_IC` 是第三种来历 ⇒ 三处不同尺是已知事实（两处注释已就地登记），
但"差多少"**没被量过**（09-20 那次产物失踪后整段撤回，见 14 号三补二十九·补）。
本脚本量的是唯一要紧的那件事：**换尺会不会让 `health.evaluate()` 的判档结果变**
（判档会驱动 `risk.apply` 自动停买 ⇒ 翻档 = 有行为后果；不翻 = 只是读数口径注记）。

判据跑前冻结在 `plans/ledger-settlement-criteria-20260920.md` §M1：
  K = 逐锚点两尺各自的 (滚动 IC, 连续为负计数) 喂同一个 evaluate() 后**判档不同**的锚点数
  K = 0 ⇒ 关闭（维持现状 + 已有就地标注）；K ≥ 1 ⇒ 待跑第二步（统一尺须另立预注册）。
只读：`mode=ro` + `query_only`；不写任何表、不落任何生产产物。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app import db                                    # noqa: E402
from app.weekly import health, scores as wsc          # noqa: E402

HORIZON = 5              # 与 health._fwd_excess 的缺省一致（不自己另定一个）
WINDOW = 12              # 与 health.rolling_ic 的缺省一致
MIN_NAMES = 200          # 与 rolling_ic 的截面下限一致（不同下限 = 另一种尺）
OUT = ROOT / "experiments" / "b_stage_20260920" / "m1_ic_scale_flip.json"


def both_scales(conn, anchor: str):
    """同一批交易日、同一条 SQL 同时取两尺 ⇒ 差异只能来自 `adj_factor` 这一个因子。

    返回 (r_adj, r_raw)，均为 `ts_code` 索引、已 dropna 的收益序列。
    """
    cal = [r[0] for r in conn.execute(
        "SELECT DISTINCT trade_date FROM daily WHERE trade_date>=? ORDER BY trade_date LIMIT ?",
        (anchor, HORIZON + 2))]
    if len(cal) < HORIZON + 2:
        return None, None
    d0, d1 = cal[1], cal[HORIZON + 1]
    q = ("SELECT d.ts_code, d.open AS raw, d.open*a.adj_factor AS adj FROM daily d "
         "JOIN adj_factor a ON a.ts_code=d.ts_code AND a.trade_date=d.trade_date "
         "WHERE d.trade_date=?")
    a0 = pd.read_sql(q, conn, params=(d0,)).set_index("ts_code")
    a1 = pd.read_sql(q, conn, params=(d1,)).set_index("ts_code")
    out = {}
    for k in ("adj", "raw"):
        p0, p1 = a0[k], a1[k]
        common = p0.index.intersection(p1.index)
        r = (p1.reindex(common) / p0.reindex(common) - 1.0)
        out[k] = r.replace([np.inf, -np.inf], np.nan).dropna()
    return out["adj"], out["raw"]


def ic_of(s_row: pd.Series, r: pd.Series):
    """与 `health.rolling_ic` 同一条链：交集 → 下限 200 → 秩相关 → 非有限值不采信。"""
    if r is None or len(r) < MIN_NAMES:
        return None
    s = s_row.reindex(r.index).dropna()
    r = r.reindex(s.index)
    if len(s) < MIN_NAMES:
        return None
    v = float(s.rank().corr(r.rank()))
    return v if np.isfinite(v) else None


def rolling(vals, window=WINDOW):
    """滚动均值（时间升序，前 window-1 期给 None）+ 逐期"截至该期的连续为负"计数。"""
    out = []
    for i in range(len(vals)):
        w = [v for v in vals[max(0, i - window + 1):i + 1] if v is not None]
        out.append(sum(w) / len(w) if w else None)
    streaks = []
    for i in range(len(vals)):
        n = 0
        for j in range(i, -1, -1):
            v = vals[j]
            if v is None or v >= 0:
                break
            n += 1
        streaks.append(n)
    return out, streaks


def main() -> int:
    t0 = time.time()
    S, anchors, cols, names, path = wsc.latest()
    scored = [i for i in range(len(anchors)) if np.isfinite(S[i]).any()]
    # 现值（turnover / deployed）固定用线上这一次读数 ⇒ 两臂之间只有 IC 轴在动
    cur = health.current()
    turn, dep = cur.get("turnover_est", 0.0), cur.get("deployed_ratio", 0.0)
    conn = db.get_conn()
    try:
        conn.execute("PRAGMA query_only=ON")
        ic_adj, ic_raw, selfcheck, skipped = [], [], [], 0
        for n, i in enumerate(scored):
            a = anchors[i].strftime("%Y%m%d")
            row = pd.Series(S[i], index=cols)
            ra, rr = both_scales(conn, a)
            va = ic_of(row, ra)
            vr = ic_of(row, rr)
            if va is None and vr is None:
                skipped += 1
                continue
            ic_adj.append(va)
            ic_raw.append(vr)
            if len(ic_adj) % 40 == 0 and ra is not None:
                # M1-a 自证：本脚本复权臂与生产函数 health._fwd_excess 必须逐位一致
                prod = health._fwd_excess(conn, a, HORIZON)
                j = prod.index.intersection(ra.index)
                if len(j):
                    d = float((prod.reindex(j) - ra.reindex(j)).abs().max())
                    selfcheck.append({"anchor": a, "max_abs_diff_vs_production": d})
            if n % 50 == 0:
                print(f"  {len(ic_adj)}/{len(scored)} 锚点，{time.time()-t0:.0f}s", flush=True)
    finally:
        conn.close()

    def levels(vals):
        roll, streaks = rolling(vals)
        lv = []
        for k, v in enumerate(roll):
            r = health.evaluate(v, streaks[k], turn, dep)
            lv.append(r["level"])
        return roll, streaks, lv

    roll_a, st_a, lv_a = levels(ic_adj)
    roll_r, st_r, lv_r = levels(ic_raw)
    flips = [k for k in range(len(lv_a)) if lv_a[k] != lv_r[k]]
    rel = [abs(a - b) / abs(a) for a, b in zip(ic_adj, ic_raw)
           if a is not None and b is not None and abs(a) > 1e-9]
    # M1-c 负对照：把一臂钉在 0.5×FROZEN_IC 上（正好贴阈值）⇒ 判档差异必须能被抓住
    frozen = health.FROZEN_IC
    pin = [frozen * 0.5] * len(ic_adj)
    _, _, lv_pin = levels(pin)
    neg_control = len([k for k in range(len(lv_a)) if lv_pin[k] != lv_a[k]])

    res = {
        "criteria": "plans/ledger-settlement-criteria-20260920.md §M1（跑前冻结）",
        "question": "复权尺 vs 未复权尺的 IC，会不会让 health.evaluate() 翻判档",
        "verdict": None,
        "K_flips": len(flips),
        "n_anchors": len(ic_adj),
        "skipped_anchors": skipped,
        "score_archive": str(path),
        "frozen_ic": frozen,
        "levels_adj_counts": pd.Series(lv_a).value_counts().to_dict(),
        "levels_raw_counts": pd.Series(lv_r).value_counts().to_dict(),
        "flip_anchors": [(anchors[scored[k]].strftime("%Y%m%d"), lv_a[k], lv_r[k],
                          roll_a[k], roll_r[k]) for k in flips[:20]],
        "per_anchor_ic_rel_diff": {
            "mean": float(np.mean(rel)) if rel else None,
            "p95": float(np.quantile(rel, 0.95)) if rel else None,
            "max": float(np.max(rel)) if rel else None,
            "source": "本脚本逐锚点 |ic_adj-ic_raw|/|ic_adj|（不是引用 F73 的二手量级）"},
        "self_checks": {
            "M1a_same_scale_as_production": {
                "n_checked": len(selfcheck),
                "max_abs_diff": max((s["max_abs_diff_vs_production"] for s in selfcheck), default=None),
                "gate": "必须为 0.0（复权臂与 health._fwd_excess 同一把尺）"},
            "M1b_window_shift_changes_reading": True,   # 下面单独算
            "M1c_negative_control_pin_at_half_frozen": {
                "flips_caught": neg_control, "gate": "必须 ≥ 1，否则这条门是死的"},
            "M1e_min_names_and_horizon_match_production": {"MIN_NAMES": MIN_NAMES, "HORIZON": HORIZON,
                                                           "WINDOW": WINDOW}},
        "runtime_s": round(time.time() - t0, 1),
        "not_measured": [],
    }
    # M1-b 平移自检：窗口整体后移一期，读数必须变化
    roll_shift, _s, lv_shift = levels([None] + ic_adj[:-1])
    res["self_checks"]["M1b_window_shift_changes_reading"] = bool(lv_shift != lv_a)

    gates_ok = (res["self_checks"]["M1a_same_scale_as_production"]["max_abs_diff"] in (0.0, None)
                and res["self_checks"]["M1b_window_shift_changes_reading"]
                and neg_control >= 1)
    res["gates_pass"] = bool(gates_ok)
    res["verdict"] = ("关闭：两尺判档逐锚点一致（K=0）" if gates_ok and len(flips) == 0 else
                      ("待跑第二步：存在翻档（K≥1），统一尺须另立预注册" if gates_ok else
                       "本轮读数作废：实现自证门未过（见 self_checks）"))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(res, indent=2, ensure_ascii=False, default=str) + "\n", "utf-8")
    print(json.dumps({k: res[k] for k in ("verdict", "K_flips", "n_anchors", "gates_pass",
                                          "runtime_s")}, ensure_ascii=False))
    print(f"→ {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
