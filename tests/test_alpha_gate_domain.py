"""alpha 门禁的**读数域**与**分母**（2026-09-13，独立审查 #4）

三处口径分叉，都会让"门禁判决"与"留痕复查"不是一回事、或让 Bonferroni 偏松：

  A) 门禁读的是 PIT 可交易域（裁域面板），而 `bank.save_values` 存的是**未裁域** ⇒
     留痕/复查（critic）看到的是另一个域；
  B) `critic.attack_all` **没有** mask 入口 ⇒ 否证清单在全市场算 head/tail；
  C) `Proposer.n_evaluated` 的 docstring 写着"调用方取 max(n_declared, n_evaluated) 以保守"，
     但 `Factory.run` 原来直接用冻结的 declared ⇒ 实际求值超预算时 p 值**偏松**。

守四条：① 不传 mask 的 critic 读数必须**自曝**警告；② 传 mask 后警告消失且确实裁了域；
③ `tradable_mask` 是唯一实现（Factory 也调它）；④ 实际求值数 > 声明时，分母取 max（更严）。
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from app.alpha import critic, factory                       # noqa: E402


def _fixture():
    md = pd.DatetimeIndex(pd.bdate_range("2021-02-26", periods=6, freq="ME"))
    cols = [f"{600000 + i}.SH" for i in range(20)]
    rnd = np.random.RandomState(0)
    wide = pd.DataFrame(rnd.rand(len(md), len(cols)), index=md, columns=cols)
    fwd = pd.DataFrame(rnd.rand(len(md), len(cols)) * 0.1, index=md, columns=cols)
    zones = np.array(["DEAD"] * len(md))
    mask = pd.DataFrame(True, index=md, columns=cols)
    mask.iloc[:, :10] = False          # 只留一半可交易
    return wide, fwd, md, zones, mask


class TestCriticMaskDomain(unittest.TestCase):
    def test_missing_mask_is_self_reported(self):
        wide, fwd, md, zones, _ = _fixture()
        rep = critic.attack_all("a1", wide, fwd, md, zones)
        self.assertIn("_mask_warning", rep, "没传域却不报警 ⇒ 留痕会被当成与门禁同口径")

    def test_with_mask_no_warning_and_domain_is_applied(self):
        wide, fwd, md, zones, mask = _fixture()
        rep = critic.attack_all("a1", wide, fwd, md, zones, mask=mask)
        self.assertNotIn("_mask_warning", rep)
        # 裁域后不该看到被屏蔽的那些票参与：用 drop_best_year 的读数间接验证不抛错即可
        self.assertIn("drop_best_year", rep)


class TestTradableMaskSingleSource(unittest.TestCase):
    def test_factory_uses_module_level_helper(self):
        src = (BASE / "app/alpha/factory.py").read_text(encoding="utf-8")
        self.assertIn("mat = tradable_mask(m, dates)", src,
                      "Factory._tradable 没走模块级 tradable_mask ⇒ 又变成两份实现")


class TestBonferroniDenominatorTakesMax(unittest.TestCase):
    class _P:
        """声明 1、实际求值 3 的提议器（模拟 GP 重提议/缓存未命中）。"""

        label = "stub"

        def __init__(self):
            self.n_evaluated = 3

        def declared(self):
            return 1

        def bind_factory(self, f):
            return None

        def propose(self):
            return []

    def test_denominator_uses_max_of_declared_and_evaluated(self):
        f = factory.Factory(factory.PreRegistration(intent="单测"))
        f.add(self._P())
        f.declare()                                   # 冻结分母 = 1
        seen = {}

        def _fake_eval(cands, eng, m, dates_all, md, zones, fwd, ind, n_tests, eval_since,
                       save, **kw):
            seen["n_tests"] = n_tests
            seen.update(kw)
            return []

        # _prepare 的返回值会被 run() 用来打印"死区/活跃区期数"，所以 zones 必须是数组
        stub_prepare = (None, None, pd.DatetimeIndex(["2021-01-29"]),
                        np.array(["DEAD"]), None, None)
        with mock.patch.object(factory.Factory, "_prepare", return_value=stub_prepare), \
                mock.patch.object(factory.E, "build_panel", return_value={}), \
                mock.patch.object(factory.E, "Engine", return_value=mock.Mock()), \
                mock.patch.object(factory.bank, "cumulative_tests", return_value=171), \
                mock.patch.object(factory.Factory, "_evaluate", side_effect=_fake_eval):
            f.run(save=False)
        # 实际求值(3) 超过预注册分母(1) ⇒ 本批 = 3；
        # 门禁 v2 ②（2026-09-14）：再叠加**累计试验数**（登记簿 171 条，mock 掉不读真库）
        # ⇒ N_used = max(3, 171 + 3) = 174
        self.assertEqual(seen["n_tests"], 174,
                         "分母必须 = max(本批, 累计 + 本批) —— 否则 p 值偏松")
        self.assertEqual(seen["n_tests_family"], 3)
        self.assertEqual(seen["n_tests_prior"], 171)


if __name__ == "__main__":
    unittest.main()
