# -*- coding: utf-8 -*-
"""E2E-DL 模块单测：手写反向传播的正确性是本模块能否成立的地基（plans/e2e-dl-prereg-20260929.md）。

无 torch 环境（Smart App Control 拦截，见预注册 §实现引擎变更）⇒ 梯度全靠手写，
唯一的防线是数值梯度对照（中心差分）——这条不绿，训练就是在喂随机噪声。
"""
import sys
from pathlib import Path

import numpy as np

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from app.models import e2e  # noqa: E402


def _sample(n=40, d=5, seed=3):
    rng = np.random.RandomState(seed)
    return rng.randn(n, d), rng.normal(0.02, 0.08, n)


class TestTopSoftmaxLoss:
    def test_gradient_matches_numerical(self):
        """解析梯度 vs 中心差分：相对误差必须 < 1e-6（手写反传的地基）。"""
        X, y = _sample()
        tau = float(y.std())
        net = e2e.MLP(X.shape[1], seed=7)

        def loss_at():
            s = net.forward(X)
            return e2e.top_softmax_loss_scores(y, s, tau)[0]

        s, cache = net.forward(X, cache=True)
        _, ds = e2e.top_softmax_loss_scores(y, s, tau)
        grads = net.backward(cache, ds)
        eps = 1e-6
        for name in ("W1", "b1", "W2", "b2", "W3", "b3"):
            p = net.params()[name]
            it = np.nditer(p, flags=["multi_index"])
            worst = 0.0
            while not it.finished:
                ix = it.multi_index
                old = p[ix]
                p[ix] = old + eps
                lp = loss_at()
                p[ix] = old - eps
                lm = loss_at()
                p[ix] = old
                num = (lp - lm) / (2 * eps)
                ana = grads[name][ix]
                denom = max(1.0, abs(num), abs(ana))
                worst = max(worst, abs(num - ana) / denom)
                it.iternext()
            assert worst < 1e-6, f"{name} 梯度相对误差 {worst:.2e} 超阈"

    def test_p_star_concentrates_on_true_head(self):
        """软 top 分布的锐度：正态截面下收益最大者的 p* 必须 ≫ 均匀值 1/N（τ=std 时）。

        （用正态截面测——真实标签是尖峰厚尾，linspace 均匀分布是熵最大的人为最坏例。）"""
        rng = np.random.RandomState(5)
        y = rng.normal(0, 0.08, 50)
        p_star = e2e.softmax(y / y.std())
        assert p_star.max() > 5.0 / 50, "τ=std 下最大收益者的概率质量应显著高于均匀"
        assert abs(p_star.sum() - 1.0) < 1e-9

    def test_loss_gradient_direction(self):
        """把分数向真实头部挪一步，损失必须下降（优化方向 sanity）。"""
        X, y = _sample()
        tau = float(y.std())
        net = e2e.MLP(X.shape[1], seed=7)
        s0 = net.forward(X)
        L0, ds = e2e.top_softmax_loss_scores(y, s0, tau)
        s1 = s0 - 0.1 * ds * tau
        L1 = e2e.top_softmax_loss_scores(y, s1, tau)[0]
        assert L1 < L0, "沿负梯度一步损失不降 ⇒ 梯度方向错误"


class TestGelu:
    def test_grad_matches_numerical(self):
        x = np.linspace(-3, 3, 41)
        eps = 1e-6
        num = (e2e.gelu(x + eps) - e2e.gelu(x - eps)) / (2 * eps)
        ana = e2e.gelu_grad(x)
        assert np.abs(num - ana).max() < 1e-6

    def test_gelu_values(self):
        assert abs(float(e2e.gelu(np.array(0.0)))) < 1e-12
        assert abs(float(e2e.gelu(np.array(1.0))) - 0.841192) < 1e-4


class TestAdamW:
    def test_decoupled_weight_decay(self):
        """wd 只作用于参数本体，不进动量（解耦语义）。"""
        net = e2e.MLP(3, seed=1)
        opt = e2e.AdamW(net.params(), lr=0.1, weight_decay=0.01)
        W0 = net.W1.copy()
        zero_grad = {k: np.zeros_like(v) for k, v in net.params().items()}
        opt.step(net.params(), zero_grad)
        assert np.allclose(net.W1, W0 * (1 - 0.1 * 0.01)), "零梯度下参数应只按 wd 收缩"
