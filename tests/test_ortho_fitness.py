# -*- coding: utf-8 -*-
"""H0 正交增量适应度单测（plans/h0-ortho-fitness-prereg-20261003.md §二判据③）。

只测纯函数 `_ortho_residual` 与构造器接线（缺省 head 不动 = 位级审计锚点）；
head 路径逐位不变由 scripts/b2_fitness_audit.py --verify 在真实装配上证明。
"""
import numpy as np
import pandas as pd
import pytest

from app.alpha import gate
from app.alpha.factory import GPProposer, _ortho_residual

RNG = np.random.default_rng(20261003)
# gate.MIN_NAMES=200 ⇒ 夹具截面 300 只（重叠阈值语义才有意义）
NAMES = 300
DATES = pd.to_datetime([f"2021-01-0{d}" for d in range(1, 5)])
CODES = [f"C{i:03d}" for i in range(NAMES)]


def _panel(dates, codes, scale=1.0):
    return pd.DataFrame(RNG.normal(scale=scale, size=(len(dates), len(codes))),
                        index=dates, columns=codes)


def _rank_corr(a_col, b_col):
    return a_col.rank(pct=True).corr(b_col.rank(pct=True))


def test_residual_removes_control_info():
    ctrl = _panel(DATES, CODES)
    noise = _panel(DATES, CODES)
    cand = ctrl + noise  # 候选 = 控制 + 独立噪声 ⇒ 残差应只剩噪声
    res = _ortho_residual(cand, {"ctrl": ctrl}, gate.MIN_NAMES)
    assert len(res) == len(DATES)
    raw_c = max(abs(_rank_corr(cand.loc[d], ctrl.loc[d])) for d in DATES)
    res_c = max(abs(_rank_corr(res.loc[d], ctrl.loc[d])) for d in DATES)
    assert raw_c > 0.5, f"fixture broken: raw corr {raw_c}"
    assert res_c < 0.35, f"residual still correlated with control: {res_c}"


def test_residual_keeps_orthogonal_component():
    ctrl = _panel(DATES, CODES)
    sig = _panel(DATES, CODES)  # 与 ctrl 独立的真信号
    cand = ctrl + 0.5 * sig
    res = _ortho_residual(cand, {"ctrl": ctrl}, gate.MIN_NAMES)
    keep = max(abs(_rank_corr(res.loc[d], sig.loc[d])) for d in DATES)
    drop = max(abs(_rank_corr(res.loc[d], ctrl.loc[d])) for d in DATES)
    assert keep > drop, f"orthogonal signal not preserved: keep {keep} vs drop {drop}"


def test_rows_without_controls_dropped():
    ctrl = _panel(DATES[:2], CODES)  # 控制只覆盖前两天
    cand = _panel(DATES, CODES)
    res = _ortho_residual(cand, {"ctrl": ctrl}, gate.MIN_NAMES)
    assert list(res.index) == list(DATES[:2])


def test_low_overlap_control_excluded_per_date():
    cand = _panel(DATES, CODES)
    # 唯一控制只有 3 只重叠股 ⇒ 每期都低于 MIN_NAMES ⇒ 全部日期丢弃(空面板, 不炸)
    ctrl = _panel(DATES, CODES[:3])
    res = _ortho_residual(cand, {"ctrl": ctrl}, gate.MIN_NAMES)
    assert len(res) == 0 and list(res.columns) == list(cand.columns)


def test_empty_controls_fail_fast():
    with pytest.raises(RuntimeError):
        _ortho_residual(_panel(DATES, CODES), {}, gate.MIN_NAMES)


def test_default_mode_is_head_and_mode_validated():
    gp = GPProposer(m={}, dead_dates=pd.DatetimeIndex([]))
    assert gp._fitness_mode == "head"
    assert gp._ortho_controls is None
    with pytest.raises(ValueError):
        GPProposer(m={}, dead_dates=pd.DatetimeIndex([]), fitness_mode="bogus")
