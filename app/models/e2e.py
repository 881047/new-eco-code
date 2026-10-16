# -*- coding: utf-8 -*-
"""E2E-DL：端到端组合层训练（第 13 轮"改模型"，plans/e2e-dl-prereg-20260929.md）。

范式：损失 = 组合层目标本身（top-softmax 交叉熵，可微 top-k 构件族的标准实现），
绕开"IC 与 top-6 收益反相关"（发现②，5 次本仓复现）的代理脱节 —— 前 12 轮全部
在"预测-再优化"范式内，本轮是范式变更。

实现引擎：**numpy 手写两层 MLP + 解析梯度**（torch 轮子被 Smart App Control 拦截
WinError 4551，见预注册 §实现引擎变更）。参数 ~3.3k（26→64→32→1 GELU），与
LightGBM 50 轮×31 叶同级容量 —— 容量不作为差异变量（tabular 数据量约束的应对）。

口粮与协议与生产逐字同构：26 因子（factor 表 − factor_exclusions 消费侧口径）、
`trainer._train_window`（36 月滚动）、`trainer.forward_return`（delist_last_price=True
生产标签口径）+ 行业中性化、`PITEngine.tradable_mask`（训练域 = 选股域）。
不写生产表（隔离表 + 无归档文件，同 seq_die 结构性护栏）。
"""
from __future__ import annotations

import dataclasses
import logging
from dataclasses import dataclass

import numpy as np
import pandas as pd

from app import db

log = logging.getLogger(__name__)

_GELU_C = np.sqrt(2.0 / np.pi)


def gelu(x: np.ndarray) -> np.ndarray:
    """tanh 近似 GELU（Hendrycks & Gimpel 2016 的标准近似）。"""
    return 0.5 * x * (1.0 + np.tanh(_GELU_C * (x + 0.044715 * x ** 3)))


def gelu_grad(x: np.ndarray) -> np.ndarray:
    """gelu 的解析导数（tanh 近似）。"""
    inner = _GELU_C * (x + 0.044715 * x ** 3)
    t = np.tanh(inner)
    sech2 = 1.0 - t * t
    return 0.5 * (1.0 + t) + 0.5 * x * sech2 * _GELU_C * (1.0 + 3 * 0.044715 * x ** 2)


def softmax(x: np.ndarray, axis: int = -1) -> np.ndarray:
    x = x - x.max(axis=axis, keepdims=True)
    e = np.exp(x)
    return e / e.sum(axis=axis, keepdims=True)


@dataclass(frozen=True)
class E2EConfig:
    """**预注册**: 架构与训练超参全部冻结在这里（跑前冻结，事后不得增删）。"""
    d_hidden: int = 64
    d_hidden2: int = 32
    lr: float = 1e-3
    weight_decay: float = 1e-4
    epochs: int = 100            # 上限；早停 patience 10（holdout 同款损失）
    patience: int = 10
    train_months: int = 36       # 与生产同口径（不是调参对象）
    holdout_recent: int = 6      # 与生产同口径
    horizon: int = 21            # 与生产同口径
    tau_mult: float = 1.0        # τ_i = tau_mult × std(y_当期截面)（预注册 §损失）
    seed: int = 42


class MLP:
    """26→64→32→1 GELU，numpy 手写前向/反向（~3.3k 参数）。"""

    def __init__(self, d_in: int, seed: int):
        rng = np.random.RandomState(seed)
        self.W1 = (rng.randn(d_in, 64) * np.sqrt(2.0 / d_in)).astype("float64")
        self.b1 = np.zeros(64)
        self.W2 = (rng.randn(64, 32) * np.sqrt(2.0 / 64)).astype("float64")
        self.b2 = np.zeros(32)
        self.W3 = (rng.randn(32) * np.sqrt(1.0 / 32)).astype("float64")
        self.b3 = np.zeros(1)

    def params(self) -> dict:
        return {"W1": self.W1, "b1": self.b1, "W2": self.W2, "b2": self.b2,
                "W3": self.W3, "b3": self.b3}

    def forward(self, X: np.ndarray, cache: bool = False):
        h1pre = X @ self.W1 + self.b1
        h1 = gelu(h1pre)
        h2pre = h1 @ self.W2 + self.b2
        h2 = gelu(h2pre)
        s = h2 @ self.W3 + self.b3
        if cache:
            return s, (X, h1pre, h1, h2pre, h2)
        return s

    def backward(self, cache, ds: np.ndarray) -> dict:
        """ds = dL/ds（截面内已聚合）。返回各参数梯度 dict。"""
        X, h1pre, h1, h2pre, h2 = cache
        gW3 = h2.T @ ds
        gb3 = np.atleast_1d(ds.sum())
        dh2 = np.outer(ds, self.W3) if ds.ndim == 1 else ds @ self.W3.T
        dh2 = dh2 * gelu_grad(h2pre)
        gW2 = h1.T @ dh2
        gb2 = dh2.sum(axis=0)
        dh1 = dh2 @ self.W2.T * gelu_grad(h1pre)
        gW1 = X.T @ dh1
        gb1 = dh1.sum(axis=0)
        return {"W1": gW1, "b1": gb1, "W2": gW2, "b2": gb2, "W3": gW3, "b3": gb3}


def top_softmax_loss_scores(y: np.ndarray, s: np.ndarray, tau: float):
    """按月截面的 top-softmax 交叉熵。

    p* = softmax(y/τ)（真实收益的软 top 分布），p = softmax(s/τ)，
    L = −Σ p*·log p。返回 (L, dL/ds)——dL/ds_i = (p_i − p*_i)/τ（解析）。
    """
    p_star = softmax(y / tau)
    m = s.max() / tau
    logp = s / tau - (m + np.log(np.exp(s / tau - m).sum()))
    L = -float((p_star * logp).sum())
    ds = (np.exp(logp) - p_star) / tau
    return L, ds


class AdamW:
    """解耦权重衰减（Loshchilov & Hutter 2019）。"""

    def __init__(self, params: dict, lr: float, weight_decay: float):
        self.lr, self.wd = lr, weight_decay
        self.m = {k: np.zeros_like(v) for k, v in params.items()}
        self.v = {k: np.zeros_like(v) for k, v in params.items()}
        self.t = 0
        self.b1, self.b2, self.eps = 0.9, 0.999, 1e-8

    def step(self, params: dict, grads: dict):
        self.t += 1
        for k in params:
            g = grads[k]
            self.m[k] = self.b1 * self.m[k] + (1 - self.b1) * g
            self.v[k] = self.b2 * self.v[k] + (1 - self.b2) * g * g
            mh = self.m[k] / (1 - self.b1 ** self.t)
            vh = self.v[k] / (1 - self.b2 ** self.t)
            params[k] -= self.lr * (mh / (np.sqrt(vh) + self.eps) + self.wd * params[k])


def _train_one(samples: list, cfg: E2EConfig, seed: int, d_in: int) -> MLP:
    """samples = [(X_i, y_i)]（逐月截面）。损失逐月算（截面内 softmax），梯度累加。

    早停：最后 holdout_recent 个月为验证集（同款损失），patience=cfg.patience。
    """
    net = MLP(d_in, seed)
    opt = AdamW(net.params(), cfg.lr, cfg.weight_decay)
    n_hold = min(cfg.holdout_recent, max(0, len(samples) - 12))
    tr, va = samples[:-n_hold] if n_hold else samples, samples[-n_hold:] if n_hold else []
    best_va, best_state, bad = np.inf, None, 0
    rng = np.random.RandomState(seed)
    for ep in range(cfg.epochs):
        order = rng.permutation(len(tr))
        for i in order:
            X, y = tr[i]
            tau = float(y.std()) * cfg.tau_mult or 1.0
            s, cache = net.forward(X, cache=True)
            L, ds = top_softmax_loss_scores(y, s, tau)
            opt.step(net.params(), net.backward(cache, ds))
        if va:
            vl = float(np.mean([top_softmax_loss_scores(
                y, net.forward(X), float(y.std()) * cfg.tau_mult or 1.0)[0]
                for X, y in va]))
            if vl < best_va - 1e-6:
                best_va, bad = vl, 0
                best_state = {k: v.copy() for k, v in net.params().items()}
            else:
                bad += 1
                if bad >= cfg.patience:
                    break
    if best_state:
        for k, v in best_state.items():
            setattr(net, k, v)
    net.trained_epochs = ep + 1
    return net


def _prep截面(X: np.ndarray) -> np.ndarray:
    """当期截面标准化（NaN 感知）：nanmean/nanstd z-score 后 NaN→0（截面中性）。

    factor 表特征有真缺失（新股/缺指标）——LightGBM 原生支持 NaN 而 MLP 必须
    显式处理。0 = 截面中性语义（seq_die._clean 先例：'仍缺的记 0'）。
    行剔除不在本函数内（调用方用 `np.isfinite(X).any(axis=1)` 显式剔除全 NaN 行，
    保证 codes 对齐）。
    """
    with np.errstate(invalid="ignore"):
        mu = np.nanmean(X, axis=0)
        sd = np.nanstd(X, axis=0)
    sd = np.where(np.isfinite(sd) & (sd > 1e-12), sd, 1.0)
    Z = (X - mu) / sd
    return np.where(np.isfinite(Z), Z, 0.0)


def run_e2e(table: str = None, seeds: list = None, cfg: E2EConfig = None,
            start: str = "2017-01-01", end: str = None, max_months: int = None,
            since: str = "20130101", skip_existing: bool = False) -> dict:
    """walk-forward 训练 E2E-MLP 并落隔离表（4 种子分数秩平均，同生产 ens 语义）。

    skip_existing=True 时跳过表里已有 trade_date 的期（逐期落库 + 断点续跑，
    2019-09 OpenBLAS 死锁事故后的重启语义）。
    """
    cfg = cfg or E2EConfig()
    seeds = seeds or [cfg.seed, cfg.seed + 1, cfg.seed + 2, cfg.seed + 3]
    table = table or "model_score_e2e_ts1"
    from app import config as cfg_mod
    from app.models import trainer
    from app.models.expert import ensure_score_table
    from app.pit.engine import PITEngine

    ensure_score_table(table)
    excl = set(cfg_mod.load_config().get("factor_exclusions") or [])
    wide = trainer._apply_exclusions(trainer._load_factors_wide(), excl)
    factor_names = sorted(wide.columns.get_level_values("factor").unique())
    log.info("E2E 因子 %d 个（生产 26 因子口径）", len(factor_names))

    m = trainer.load_market()
    dates = pd.to_datetime(m["close"].index)
    md_all = month_end_all(dates)
    ac = m["close"] * m["adj"]
    fut = trainer.forward_return(ac, cfg.horizon)
    labels_wide = fut.loc[md_all]
    with db.conn_ctx() as conn:
        inds = trainer.industry_map(conn)
    inds = inds.reindex(labels_wide.columns)
    labels_wide = labels_wide - labels_wide.T.groupby(inds, sort=False).transform("mean").T
    pit = PITEngine(m)

    # 因子面板：trade_date → DataFrame(股 × 因子)（生产消费侧 26 因子）
    panels = {}
    for d in md_all:
        if d not in wide.index:
            continue
        Xd = wide.loc[d].unstack("factor").reindex(columns=factor_names)
        panels[d] = Xd

    months = [d for d in md_all if d >= pd.Timestamp(start)
              and d in panels and (end is None or d <= end)]
    if skip_existing:
        with db.conn_ctx() as conn:
            have = {r[0] for r in conn.execute(
                f"SELECT DISTINCT trade_date FROM {table}").fetchall()}
        have = {pd.Timestamp(x) if not isinstance(x, pd.Timestamp) else x for x in have}
        n0 = len(months)
        months = [d for d in months if d.strftime("%Y%m%d") not in
                  {x.strftime("%Y%m%d") for x in have}]
        log.info("断点续跑: 表内已有 %d 期, 跳过后剩 %d/%d 期", n0 - len(months), len(months), n0)
    if max_months:
        months = months[:max_months]
    log.info("E2E 评分月 %d 期: %s → %s (seeds=%s)", len(months),
             months[0].date() if months else "-", months[-1].date() if months else "-", seeds)

    rows = []
    for k, d in enumerate(months):
        i = int(np.where(md_all == d)[0][0])
        window = trainer._train_window(md_all, i, cfg.train_months)
        window = [w for w in window if w in labels_wide.index and w in panels]
        if len(window) < 12:
            log.warning("%s 训练窗口仅 %d 期, 跳过", d.date(), len(window))
            continue
        samples = []
        for w in window:
            ok = pit.tradable_mask(w, allow_bse=False)
            lab = labels_wide.loc[w].reindex(panels[w].index)
            okv = ok.reindex(panels[w].index).fillna(False).astype(bool).to_numpy()
            good = lab.notna().to_numpy() & okv
            if good.sum() < 50:
                continue
            Xraw = panels[w].to_numpy(dtype="float64")[good]
            keep = np.isfinite(Xraw).any(axis=1)          # 全 NaN 行（无任何因子值）剔除
            if keep.sum() < 50:
                continue
            samples.append((_prep截面(Xraw[keep]),
                            lab.to_numpy(dtype="float64")[good][keep]))
        if len(samples) < 12:
            log.warning("%s 可用训练月不足 (%d), 跳过", d.date(), len(samples))
            continue
        # 评分截面（当期，可打分域）
        ok_d = pit.tradable_mask(d, allow_bse=False)
        Xd = panels[d]
        okv = ok_d.reindex(Xd.index).fillna(False).astype(bool).to_numpy()
        Xraw = Xd.to_numpy(dtype="float64")[okv]
        keep = np.isfinite(Xraw).any(axis=1)
        if keep.sum() < 50:
            log.warning("%s 评分截面剔除全 NaN 行后不足 (%d), 跳过", d.date(), int(keep.sum()))
            continue
        codes_kept = Xd.index[okv][keep]
        Xd_mat = _prep截面(Xraw[keep])

        preds = []
        for sd_ in seeds:
            net = _train_one(samples, cfg, sd_, d_in=len(factor_names))
            preds.append(net.forward(Xd_mat))
        # 分数秩平均（跨种子；同生产 ens 语义）；pred_ret 存原始分数均值
        R = np.mean([pd.Series(p).rank(pct=True).to_numpy() for p in preds], axis=0)
        raw = np.mean(preds, axis=0)
        sub = pd.DataFrame({"ts_code": codes_kept, "trade_date": d.strftime("%Y%m%d"),
                            "score": R, "pred_ret": raw})
        # **逐期落库**（幂等 DELETE+INSERT）——整臂循环中途崩溃不丢已完成期
        # （2019-09 期 OpenBLAS 死锁事故后改为逐期写，2026-09-29）
        with db.conn_ctx() as conn:
            conn.execute(f"DELETE FROM {table} WHERE trade_date=?", (d.strftime("%Y%m%d"),))
            db.upsert_df(conn, sub, table)
            conn.commit()
        rows.append(sub)
        log.info("  %s: %d 训练月 × %d 种子 → 打分 %d 只", d.date(), len(samples), len(seeds), len(codes_kept))

    if not rows:
        log.error("没有任何分数产出")
        return {"table": table, "n_months": 0, "n_rows": 0}
    df = pd.concat(rows, ignore_index=True)
    log.info("写入 %s 合计: %d 行 / %d 期", table, len(df), df["trade_date"].nunique())
    return {"table": table, "n_rows": int(len(df)), "n_months": int(df["trade_date"].nunique()),
            "config": dataclasses.asdict(cfg), "seeds": seeds}


def month_end_all(dates: pd.DatetimeIndex) -> pd.DatetimeIndex:
    from app.factors.factor_engine import month_end_dates
    import app.timeutil as timeutil
    md = month_end_dates(dates, today=timeutil.today())
    return md[md >= pd.Timestamp("2014-01-01")]


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run_e2e(max_months=3)
