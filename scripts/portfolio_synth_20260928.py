"""组合层合成分析（2026-09-28）——"2 万本金如何配置"的量化答案。

问题（goal："超额有限"痛点）：月频车（full 窗系统 18.77%/年）+ 周频袖套缩容臂 + CB 双低
三条收益流，相关性多低？叠加后夏普/回撤改善多少？

方法（冻结在脚本头）：
- 统一周频对齐：每腿 NAV → 日频阶梯（exec 日生效、ffill）→ ISO 周末取样 → 周收益。
  各腿周收益因此都"赚在同一个日历周"，无错期噪声。
- 输入（全部为已落盘回测产物，只读）：
  1) 月频车：experiments/portfolio_synth_20260928/monthly_car_daily_exe_full.json
     （backtest_nav_daily run=20260928_exe_full，pin 第 8 次，2017-02~2026-08 日线）
  2) 袖套：experiments/sleeve_downsize_ab_20260928_series_<seed>_<ARM>.json
     （--arm 选臂，默认 B4_2w_top10；含 G0 校准守卫 ⇒ 与 F52 等价）
  3) CB 双低：D:/ECO-W2/data/cb_backtest_v2_series_20260928.json（2018-01~2026-09 周线）
- 组合配置（冻结，周度再平衡，腿间交易成本=0——腿是独立账户，费用已含在腿收益里）：
  P0 = 100% 月频车（现状）
  P0r = 100% 月频车 × 部署折扣 0.744（=0.65/0.873，36 补实测 2 万部署率 65% 对回测
        0.873 权益仓的折算——2 万现实口径）
  P1 = 50% 月频车 + 50% 袖套
  P2 = 40% 月频车 + 40% 袖套 + 20% CB
  P3 = 33/33/33
  P4 = 逆周波动率权（risk parity，仅用全样本波动率——描述性，非可执行）
- 报告：各腿与组合的 ann/vol/Sharpe(周)/MDD(周)/最差周 + 全样本与三分段相关矩阵
  （Pearson/Spearman）。
- 诚实性注记：CB 与袖套的收益流为回测口径（CB Broker 级含强赎近似/冻结处理；
  袖套缩容臂含整手约束与 10bp 滑点）；组合再平衡本身在 2 万下为同账户内调仓，
  月频车腿与袖套腿若在同一 A 股账户内运行，调仓费用已在各自腿内，腿间再平衡
  只是把新钱按比例分配，边际成本≈0 的假设在"每层独立账本"下成立。
- 本脚本零生产改动：只读 + 产物写 experiments/portfolio_synth_20260928/result.json。

用法：.venv\\Scripts\\python.exe -X utf8 scripts\\portfolio_synth_20260928.py [--arm B4_2w_top10]
"""
import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent.parent
EXP = BASE / "experiments"
MON_IN = EXP / "portfolio_synth_20260928" / "monthly_car_daily_exe_full.json"
CB_IN = Path(r"D:/ECO-W2/data/cb_backtest_v2_series_20260928.json")
DEPLOY_RATIO_2W = 0.65          # 36 补实测：2 万账户月频车部署率
BACKTEST_EQUITY_W = 0.873       # 回测恒定权益仓


def iso_week(ts: pd.Timestamp):
    y, w, _ = ts.isocalendar()
    return (y, w)


def week_nav_series(dates, navs):
    """日频阶梯 NAV：日期生效日起 ffill 到下个生效日。"""
    s = pd.Series(navs, index=pd.to_datetime(dates)).sort_index()
    bdays = pd.bdate_range(s.index[0], s.index[-1])
    daily = s.reindex(bdays.union(s.index)).ffill()
    return daily


def weekly_returns(daily: pd.Series) -> pd.Series:
    g = daily.groupby(daily.index.map(iso_week)).last()
    g = g.sort_index()
    return g.pct_change().dropna()


def stats(weekly: pd.Series, name: str) -> dict:
    ann = float((1 + weekly).prod() ** (52 / len(weekly)) - 1)
    vol = float(weekly.std() * np.sqrt(52))
    sharpe = float(weekly.mean() / (weekly.std() + 1e-12) * np.sqrt(52))
    nav = (1 + weekly).cumprod()
    mdd = float((nav / nav.cummax() - 1).min())
    return {"name": name, "n_weeks": int(len(weekly)), "ann_pct": round(100 * ann, 2),
            "vol_pct": round(100 * vol, 2), "sharpe": round(sharpe, 3),
            "mdd_pct": round(100 * mdd, 2),
            "worst_week_pct": round(100 * float(weekly.min()), 2)}


def week_monday(yw) -> pd.Timestamp:
    y, w = yw
    return pd.Timestamp.fromisocalendar(int(y), int(w), 1)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--arm", default="B4_2w_top10")
    ap.add_argument("--seed", default="seedA")
    a = ap.parse_args()

    mon = json.loads(MON_IN.read_text(encoding="utf-8"))
    mon_daily = week_nav_series([r["d"] for r in mon], [r["nav"] for r in mon])
    mon_sys_daily = week_nav_series([r["d"] for r in mon], [r["sys"] for r in mon])

    sleeve_path = EXP / f"sleeve_downsize_ab_20260928_series_{a.seed}_{a.arm}.json"
    sleeve = json.loads(sleeve_path.read_text(encoding="utf-8"))
    sl_daily = week_nav_series(
        [pd.Timestamp(int(r["date"]), unit="ms").strftime("%Y%m%d") for r in sleeve],
        [r["nav"] for r in sleeve])

    cbj = json.loads(CB_IN.read_text(encoding="utf-8"))["results"]["双低"]
    cb_daily = week_nav_series([r[0] for r in cbj["nav_series"]], [r[1] for r in cbj["nav_series"]])

    legs = {"monthly_car": weekly_returns(mon_daily),
            "monthly_car_2w_real": weekly_returns(mon_daily) * (DEPLOY_RATIO_2W / BACKTEST_EQUITY_W),
            "sleeve_" + a.arm: weekly_returns(sl_daily),
            "cb_dual_low": weekly_returns(cb_daily)}

    common = None
    for s in legs.values():
        common = s.index if common is None else common.intersection(s.index)
    common = common.sort_values()
    W = pd.DataFrame({k: v.reindex(common) for k, v in legs.items()}).dropna()

    res = {"inputs": {"monthly": str(MON_IN), "sleeve": str(sleeve_path), "cb": str(CB_IN),
                      "deploy_ratio_2w": DEPLOY_RATIO_2W, "backtest_equity_w": BACKTEST_EQUITY_W,
                      "arm": a.arm, "seed": a.seed},
           "overlap_weeks": int(len(W)),
           "overlap_window": [str(week_monday(W.index[0]).date()), str(week_monday(W.index[-1]).date())],
           "legs": {k: stats(W[k], k) for k in W.columns}}

    # 相关矩阵（全样本 + 三分段）
    segs = {"full": None,
            "2018-2020": ("2018-01-01", "2020-12-31"),
            "2021-2023": ("2021-01-01", "2023-12-31"),
            "2024-2026": ("2024-01-01", "2026-12-31")}
    res["corr"] = {}
    mondays = W.index.map(week_monday)
    for seg, rng in segs.items():
        sub = W if rng is None else W[(mondays >= rng[0]) & (mondays <= rng[1])]
        res["corr"][seg] = {"n": int(len(sub)),
                            "pearson": sub.corr(method="pearson").round(3).to_dict(),
                            "spearman": sub.corr(method="spearman").round(3).to_dict()}

    # 组合配置（冻结）
    cfgs = {        "P0_100pct_car": {"monthly_car": 1.0},
        "P0r_2w_real": {"monthly_car_2w_real": 1.0},
        "P1_car50_sleeve50": {"monthly_car": 0.5, "sleeve_" + a.arm: 0.5},
        "P1r_2w_real_car50_sleeve50": {"monthly_car_2w_real": 0.5, "sleeve_" + a.arm: 0.5},
        "P2_car40_sleeve40_cb20": {"monthly_car": 0.4, "sleeve_" + a.arm: 0.4, "cb_dual_low": 0.2},
        "P3_thirds": {"monthly_car": 1 / 3, "sleeve_" + a.arm: 1 / 3, "cb_dual_low": 1 / 3},
        "P2r_2w_real": {"monthly_car_2w_real": 0.4, "sleeve_" + a.arm: 0.4, "cb_dual_low": 0.2},
    }
    res["portfolios"] = {}
    for pname, wts in cfgs.items():
        port = sum(W[k] * w for k, w in wts.items())
        res["portfolios"][pname] = {"weights": {k: round(v, 4) for k, v in wts.items()},
                                    **stats(port, pname)}
    # 逆波动率权（描述性）
    inv = 1 / W.std()
    wts = (inv / inv.sum()).to_dict()
    port = sum(W[k] * w for k, w in wts.items())
    res["portfolios"]["P4_invvol_descriptive"] = {"weights": {k: round(v, 4) for k, v in wts.items()},
                                                  **stats(port, "P4")}

    out = EXP / "portfolio_synth_20260928" / "result.json"
    out.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({"overlap": [res["overlap_weeks"], res["overlap_window"]],
                      "legs": res["legs"],
                      "portfolios": res["portfolios"],
                      "corr_full_pearson": res["corr"]["full"]["pearson"]},
                     ensure_ascii=False, indent=1))
    print(f"写入 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
