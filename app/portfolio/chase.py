"""建仓追高剔除闸（chase gate, 2026-09-26 用户拍板采纳）—— **单一实现**。

## 这是什么

月度建仓名单的**计划时**叠加: 对最终 top-n 候选中「近 5 日追高最凶」的股票**剔除、
不补位**（少买 ⇒ 现金留存）。信号与阈值全部来自三轮预注册 A/B 的冻结口径
（`plans/risk-overlay-prereg-20260926.md` → `chase6-fullhist-prereg-20260926.md` →
`chase6-round3-prereg-20260926.md`, 均过冻结判据; 判决与证据链见 05 号台账 七补/九补）:

- `mom5`    = adjClose(d) / adjClose(d−5) − 1（近 5 交易日复权动量, 含 d）
- `devvwap` = close(d) / vwap5 − 1, vwap5 = 10×Σamount / Σvol（近 5 日含 d;
  tushare amount 千元 / vol 手 ⇒ 元/股, 与 close 同量纲）
- 截面 z（均值 / std ddof=1）over「当期锚点 model_score ∩ PIT 可交易域(tradable_mask)
  内**信号可算**」的股票; `chase_z = max(z_mom5, z_devvwap)`（任一轴极端即追高）
- 剔除集 = {code | chase_z > theta}, theta 冻结 **2.0**（剂量曲线 θ=1.5/2.0/2.5 全史
  +0.80/+1.47/+1.10pp/年 见九补）

## 无前视

mom5/devvwap/vwap5 只用锚点日 d **当日及以前**的行情（评分在 d 收盘后、执行在其后,
与生产执行时序一致）。历史锚点同样只读当时截面。

## 为什么是类、为什么吃整个面板

PITEngine 构造要载入名称链/首现日（秒级）⇒ 回测 85 期、方案构建各**构造一次**复用。
行情面板 m 由调用方传入（engine 与 builder 都是 `load_market()` 全历史 ⇒ 信号与
A/B 臂**逐位同数**——这是重钉后 pin 能复现 A/B 读数的前提）。builder 侧按月构建、
cache 命中时面板加载为秒级。
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from app.pit.engine import PITEngine

log = logging.getLogger(__name__)

THETA_DEFAULT = 2.0


def enabled(cfg: dict) -> bool:
    """config `chase.enabled`（缺省 False —— 生产由 config.yaml 显式开启;
    测试/新装机的 DEFAULT_CONFIG 路径保持旧行为, golden 回归不受影响）。"""
    return bool((cfg or {}).get("chase", {}).get("enabled", False))


def theta_of(cfg: dict) -> float:
    return float((cfg or {}).get("chase", {}).get("theta", THETA_DEFAULT))


class ChaseGate:
    """单次运行构造一次; `exclusions()` 按锚点问询。"""

    def __init__(self, m: dict, cfg: dict = None):
        self.m = m
        self.pit = PITEngine(m=m, cfg=cfg)
        self._dates = pd.DatetimeIndex(m["close"].index)
        self._close = m["close"]
        self._adj = m["adj"]
        self._amount = m["amount"]
        self._vol = m["vol"]

    def chase_z(self, anchor, scores: pd.Series) -> pd.Series:
        """当期锚点的 chase_z。scores = 当期锚点**全量**打分（index=ts_code）。"""
        d = pd.Timestamp(anchor)
        pos = self._dates.get_loc(d)
        if pos < 5:
            # 2026-09-28 审查: 原实现 pos<5 时 iloc[pos-5] 负索引**回绕到面板末行**
            # (未来数据)、iloc[pos-4:pos+1] 空切片。生产全历史面板永不触发, 但任何
            # 短面板/窗口化调用方都会中招 ⇒ 锚点前历史不足 5 日 = 信号不可算,
            # 返回空(fail-open 不剔除, 与 S5"锚点不足不否决"同取向)。
            return pd.Series(dtype="float64")
        uni = self.pit.tradable_mask(d)
        codes = [c for c in scores.index if uni.get(c, False)]
        o_adj = (self._close.iloc[pos] * self._adj.iloc[pos]).astype(float)
        o_adj0 = (self._close.iloc[pos - 5] * self._adj.iloc[pos - 5]).astype(float)
        mom5 = (o_adj / o_adj0 - 1.0).reindex(codes)
        c_raw = self._close.iloc[pos].astype(float)
        amt5 = self._amount.iloc[pos - 4: pos + 1].sum().astype(float)
        vol5 = self._vol.iloc[pos - 4: pos + 1].sum().astype(float)
        # ×10 = Tushare `daily` 量纲换算(amount 千元 / vol 手 ⇒ 元/股) —— 与
        # app/alpha/expr.py 的 $vwap 现算是**同一份量纲假设**的两个使用点：
        # 换数据源(单位变化)必须两处同步, 静态钉在 tests/test_chase.py::TestVwapUnitCrossReference
        vwap5 = 10.0 * amt5 / vol5.replace(0.0, np.nan)
        devvwap = (c_raw / vwap5.astype(float) - 1.0).reindex(codes)
        z1 = (mom5 - mom5.mean()) / mom5.std(ddof=1)
        z2 = (devvwap - devvwap.mean()) / devvwap.std(ddof=1)
        return pd.concat([z1, z2], axis=1).max(axis=1).dropna()   # max(z1, z2)

    def exclusions(self, anchor, scores: pd.Series,
                   theta: float = THETA_DEFAULT) -> set:
        cz = self.chase_z(anchor, scores)
        return set(cz[cz > float(theta)].index)
