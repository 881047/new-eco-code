"""诊断 ②(2026-09-09): 训练集/推分截面里「全 NaN 特征」样本的真实规模

背景: pandas 3.0 起 `DataFrame.stack()` 默认不再丢 NaN 行(pandas 2.x 默认丢),
`trainer._stack` 因此会把 (窗口日期 × 全部股票) 的笛卡尔积都堆出来, 其中不交易的
(stock, date) 组合是全 NaN 行。审查项 ② 的诉求: 这些全 NaN 样本不得进入训练集。

本脚本复现 trainer 的 X/y/mask 构造, 逐期统计:
  X 行数(stack 后的笛卡尔积) / mask 通过行数 / X[mask] 全 NaN 行数 / 高缺失(>50%)行数
另统计推分截面 X_d 的全 NaN 行数。

运行: PYTHONPATH=".venv/Lib/site-packages" python scripts/oneoff/diag_allnan_samples.py
"""
import datetime
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import numpy as np
import pandas as pd

from app.factors.factor_engine import load_market, month_end_dates
from app.models.trainer import HORIZON, _load_factors_wide, _stack, forward_return


def main():
    m = load_market()
    dates = pd.to_datetime(m["close"].index)
    md_all = month_end_dates(dates, today=datetime.date.today())
    md_all = md_all[md_all >= pd.Timestamp("2014-01-01")]
    wide = _load_factors_wide()
    factor_names = sorted(wide.columns.get_level_values("factor").unique())
    ac = m["close"] * m["adj"]
    fut = forward_return(ac, HORIZON)
    labels_wide = fut.loc[md_all]
    vol = m["vol"]

    # 默认取最近 12 期评分月(生产增量训练关注的正是近期); 也可传月份参数抽查历史
    if len(sys.argv) > 1:
        want = [pd.Timestamp(a) for a in sys.argv[1:]]
        targets = md_all[md_all.isin(want)]
        if targets.empty:
            raise SystemExit(f"指定月份不在月末截面内: {sys.argv[1:]}")
    else:
        targets = md_all[-12:]
    print(f"因子数 {len(factor_names)} | 宽表 {wide.shape} | 评估期 {targets[0].date()}~{targets[-1].date()}")
    print(f"{'预测月':>10} {'窗口':>4} {'X行数':>8} {'mask行数':>9} "
          f"{'全NaN入训':>10} {'高缺失>50%':>10} {'X_d行数':>8} {'X_d全NaN':>9}")
    tot = {"allnan": 0, "high": 0, "mask": 0, "x": 0}
    for d in targets:
        i = md_all.get_loc(d)
        window = [w for w in md_all[max(0, i - 36): i - 1] if w in wide.index]
        if len(window) < 12:
            continue
        X = _stack(wide, factor_names, window)
        y = labels_wide.loc[window].stack().reindex(X.index)
        mask = y.notna()
        for wd in window:
            ok = (vol.loc[wd] > 0)
            mask.loc[wd] = mask.loc[wd] & ok.reindex(mask.loc[wd].index).fillna(False)
        n_all_nan = int(X[mask].isna().all(axis=1).sum())
        n_high = int((X[mask].isna().mean(axis=1) > 0.5).sum())
        Xd = pd.concat([wide.xs(f, axis=1, level="factor").loc[d].rename(f)
                        for f in factor_names], axis=1)
        okd = (vol.loc[d] > 0).reindex(Xd.index).fillna(False)
        Xd = Xd[okd]
        print(f"{str(d.date()):>10} {len(window):>4} {len(X):>8} {int(mask.sum()):>9} "
              f"{n_all_nan:>10} {n_high:>10} {len(Xd):>8} "
              f"{int(Xd.isna().all(axis=1).sum()):>9}")
        tot["allnan"] += n_all_nan
        tot["high"] += n_high
        tot["mask"] += int(mask.sum())
        tot["x"] += len(X)
    print(f"\n合计: X {tot['x']} 行 / 入训 {tot['mask']} 样本 / 全 NaN 入训 {tot['allnan']} "
          f"({tot['allnan'] / max(1, tot['mask']):.4%}) / 高缺失 {tot['high']}")


if __name__ == "__main__":
    main()
