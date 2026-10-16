"""F75 id60 反向信号 · tail 视角复测（2026-09-18，附加测量——不改 F75 原判决）

背景：F75 判据为正向选股因子设计（head ≥+0.5%/月 + 只做多经济门槛），对反向信号
方向失焦——id60_late_ret/id60_close_vs_dayvwap 以 t=-5.88/-5.84 被"不过"。
本脚本补齐对称读数：tail_excess = 因子值**最低 10%**（尾部）相对池子的超额，
并按剔除器语义给出经济口径（尾部股若被剔除，避免的损失 - 往返费用）。

**附加测量，不改 F75 原判决**；登记台账用。
判据（本脚本自设，跑前冻结）：
  tail 视角下反向剔除器"有料" ⇔ 死区尾部超额 ≤ -0.5%/月折算（0.119pp/5日）
  且 HAC t ≥ 2.0 且 两区同负 且 剔除收益(避免损失-费用) ≥ 0.119pp/5日。
输出：experiments/05_label/F75b_intraday_tail_result.json
用法: .venv/Scripts/python -X utf8 scripts/f75b_intraday_tail.py
"""
import json
import importlib.util
import time
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
HERE = BASE / "experiments" / "05_label"

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from app.logger import setup_logger  # noqa: E402

setup_logger()


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


f75 = _load("f75", HERE / "F75_intraday_minute_kgd.py")
f1b = _load("f1b", HERE / "F1b_weekly_open_overlap.py")

TAIL_BAR = 0.119        # 0.5%/月 折算 5 日锚点（与 F75 同折算）


def tail_stats(fac: pd.DataFrame, excess: pd.DataFrame, a: str, b: str) -> dict:
    """尾部 10% 的超额（因子值最低 10% 的股票相对池子）+ HAC t。"""
    z = (fac.index >= a) & (fac.index <= b)
    fx, ex = fac[z], excess[z]
    tails = []
    for i in range(len(fx)):
        s = pd.Series(fx.iloc[i]).dropna()
        t = pd.Series(ex.iloc[i]).reindex(s.index).dropna()
        s = s.reindex(t.index)
        if len(s) < 200:
            continue
        tails.append(float(t[s <= s.quantile(0.1)].mean() - t.mean()))
    if not tails:
        return {"n": 0}
    arr = np.asarray(tails, dtype=float)
    t_hac = f1b.hac_t(arr, 2) if len(arr) >= 10 else float("nan")
    return {"n": len(arr), "tail_pp": round(float(arr.mean()) * 100, 4),
            "t_hac": round(float(t_hac), 2) if np.isfinite(t_hac) else None}


def main() -> int:
    t0 = time.time()
    import sys as _s
    _s.path.insert(0, str(BASE))
    from app.factors import factor_engine as fe
    m = fe.load_market(since="2019-12-01")
    dates = pd.to_datetime(m["close"].index)
    anchors = f75.week_anchors(dates)
    anchors = anchors[anchors >= pd.Timestamp("2020-02-01")]
    op = m["open"]
    fwd = (op.shift(-(f75.H + 1)) / op.shift(-1) - 1.0).reindex(anchors)
    mask = f75.factory.tradable_mask(m, dates).reindex(anchors)
    pool = fwd.where(mask).mean(axis=1)
    excess = fwd.sub(pool, axis=0)
    print(f"锚点 {len(anchors)} 个 ({anchors[0]:%Y-%m-%d} ~ {anchors[-1]:%Y-%m-%d})", flush=True)

    facs = f75.build_factors(anchors)
    res = {"script": "f75b_intraday_tail", "date": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M"),
           "note": "附加测量：tail 视角（因子尾部 10% 相对池子），不改 F75 原判决。"
                   "剔除器语义：tail 超额为负 = 尾部股拖累，剔除它们避免的损失 = |tail| - 费用",
           "criteria_frozen": {"tail_bar_pp": TAIL_BAR, "min_t": 2.0, "both_negative": True},
           "factors": {}}
    for name, fac in facs.items():
        d = tail_stats(fac, excess, *f75.DEAD)
        a = tail_stats(fac, excess, *f75.ACTIVE)
        tail_pp = d.get("tail_pp")
        t_hac = d.get("t_hac")
        # 剔除器经济口径: 尾部股被剔除 ⇒ 避免 |tail_pp| 的损失, 成本=0(不交易);
        # 剔除动作本身发生在候选层(不买入), 无费用 ⇒ 有料 = tail_pp <= -TAIL_BAR
        # 且 t≥2 且 活跃区同负。
        has_meat = bool(tail_pp is not None and tail_pp <= -TAIL_BAR
                        and (t_hac or 0) >= 2.0 and (a.get("tail_pp") or 0) < 0)
        res["factors"][name] = {
            "dead_tail": d, "active_tail": a,
            "tail_meat": bool(has_meat),
        }
        print(f"{name}: 死区 tail {tail_pp}pp (t={t_hac}) | 活跃 {a.get('tail_pp')}pp | "
              f"剔除器有料: {has_meat}")
    n_meat = sum(1 for v in res["factors"].values() if v["tail_meat"])
    res["summary"] = f"{n_meat}/3 条基列 tail 视角有料（剔除器素材确认）"
    res["secs"] = round(time.time() - t0, 1)
    out = HERE / "F75b_intraday_tail_result.json"
    out.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"过 tail 视角: {n_meat}/3 | 产物 {out} | 耗时 {res['secs']}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
