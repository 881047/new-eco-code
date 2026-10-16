# -*- coding: utf-8 -*-
r"""组合合成的锚点键更正核算 —— 07 号事故 #66 / 05 号四十二补的复现脚本。

背景：四十一补的组合合成把袖套回放行按执行日（周一）ISO 周做键，而该行收益的
归属期是锚点周五→执行周一的持有周 ⇒ 袖套相对车腿错位一周，ρ(车,袖套) 0.65 被压成
0.054。本脚本按**锚点键（执行日−3 天）**重算全部组合读数（42 补 §二数字的来源）。

只读；产物直接打印（数字已冻结登记在 42 补与 plans/twostream-paper-prereg-20260928.md）。
跑法：.venv\Scripts\python.exe -X utf8 scripts\oneoff\portfolio_synth_anchorkey_20260928.py
"""
import json
from pathlib import Path

import pandas as pd

BASE = Path(__file__).resolve().parents[2]


def week_nav(dates, navs):
    s = pd.Series(navs, index=pd.to_datetime(dates)).sort_index()
    b = pd.bdate_range(s.index[0], s.index[-1])
    return s.reindex(b.union(s.index)).ffill()


def iso(ts):
    y, w, _ = pd.Timestamp(ts).isocalendar()
    return f"{int(y)}-W{int(w):02d}"


def wk_by_key(daily):
    return daily.groupby(daily.index.map(iso)).last().sort_index().pct_change().dropna()


def stats(r, name):
    r = r.dropna()
    ann = float((1 + r).prod() ** (52 / len(r)) - 1)
    vol = float(r.std() * np.sqrt(52))
    sh = float(r.mean() / (r.std() + 1e-12) * np.sqrt(52))
    nav = (1 + r).cumprod()
    mdd = float((nav / nav.cummax() - 1).min())
    return (f"{name:12s} ann {100*ann:>6.2f}%  vol {100*vol:>5.2f}%  "
            f"Sharpe {sh:.3f}  MDD {100*mdd:>7.2f}%  worst {100*r.min():>6.2f}%")


import numpy as np  # noqa: E402

mon = json.loads((BASE / 'experiments/portfolio_synth_20260928/monthly_car_daily_exe_full.json').read_text())
car = wk_by_key(week_nav([r['d'] for r in mon], [r['nav'] for r in mon]))
carr = car * (0.65 / 0.873)   # 36 补实测 2 万部署率 65% / 回测权益仓 0.873
cbj = json.loads(Path(r'D:/ECO-W2/data/cb_backtest_v2_series_20260928.json').read_text())['results']['双低']
cb = wk_by_key(week_nav([r[0] for r in cbj['nav_series']], [r[1] for r in cbj['nav_series']]))

for tag, arm in [('seedA_B4_2w_top10', 'B4'), ('seedB_B4_2w_top10', 'B4sB'),
                 ('seedA_B5_2w_top6', 'B5')]:
    sl = json.loads((BASE / f'experiments/sleeve_downsize_ab_20260928_series_{tag}.json').read_text())
    idx = [iso(pd.Timestamp(int(r['date']), unit='ms') - pd.Timedelta(days=3)) for r in sl]
    sl_w = pd.Series([r['ret'] for r in sl], index=idx).groupby(level=0).first()
    W = pd.DataFrame({'car': car, 'carr': carr, 'sl': sl_w, 'cb': cb}).dropna(subset=['car', 'sl'])
    print(f'== {arm} == n={len(W)} {W.index[0]}..{W.index[-1]}')
    for nm, r in [('P0 car', W['car']), ('P0r', W['carr']),
                  ('P1 50/50', 0.5 * W['car'] + 0.5 * W['sl']),
                  ('P1r', 0.5 * W['carr'] + 0.5 * W['sl']),
                  ('P2r 40/40/20', 0.4 * W['carr'] + 0.4 * W['sl'] + 0.2 * W['cb']),
                  ('P3 thirds', (W['carr'] + W['sl'] + W['cb']) / 3)]:
        print('  ', stats(r, nm))
    print('   rho car-sl', round(W['car'].corr(W['sl']), 3),
          '| car-cb', round(W['car'].corr(W['cb']), 3),
          '| sl-cb', round(W['sl'].corr(W['cb']), 3))
    q = W['car'].quantile(0.05)
    tail = W[W['car'] <= q]
    print('   car-tail(5%) P1r mean', round(float((0.5 * tail['carr'] + 0.5 * tail['sl']).mean()), 4),
          'vs uncond', round(float((0.5 * W['carr'] + 0.5 * W['sl']).mean()), 4))
