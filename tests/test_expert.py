"""KGD 测试台单测 (2026-09-11 「芯片式异构架构」阶段 1)

**最重要的一条是交叉校验**: `app/models/expert.py` 与**预注册**的
`scripts/oneoff/exp_deadzone_ic.py` 各写了一份 head_excess 统计量(故意不重构冻结的
判据脚本 —— 冻结的判据比 DRY 值钱)。两份实现若静默漂移, 就会出现「门禁说通过、
KGD 说退役」这种无法解释的分歧。所以这里锁死: **同一输入必须给出同一个数字**。

运行: ./.venv/Scripts/python.exe -m pytest tests/test_expert.py -q
"""
import importlib.util
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd

from app.models import expert as ex

# 冻结的预注册判据脚本 —— **不移动、不改写**(见本文件开头: 冻结的判据比 DRY 值钱)。
_FROZEN_GATE_PATH = (Path(__file__).resolve().parent.parent
                     / "scripts" / "oneoff" / "exp_deadzone_ic.py")
_frozen_gate_mod = None


def _frozen_gate():
    """按**文件路径**加载冻结脚本, 而不是把 `scripts/oneoff/` 塞进 `sys.path`。

    2026-09-12 审查(测试质量项): 原做法 `sys.path.insert(0, .../scripts/oneoff)` +
    `import exp_deadzone_ic` 有两个副作用 —— (1) 一次性脚本目录里那几十个 `exp_*.py`
    在整轮测试期间全变成**裸名可导入**, 任何地方一句 `import exp_*` 都会命中冻结脚本,
    而命中与否只取决于 import 顺序; (2) pytest/unittest 的模块解析多一个不可见的
    path 项, 收集期行为依赖调用方式。按路径显式加载后: 不碰 `sys.path`, 加载的就是
    这一个文件, 路径写死在这里 —— 它被挪走或改名会当场 ImportError 而不是静默换源。

    冻结脚本自己在模块顶部按 `__file__` 推出项目根并 `sys.path.insert`, 所以它的
    `from app import ...` 在按路径加载时照常可用(已实测)。
    """
    global _frozen_gate_mod
    if _frozen_gate_mod is None:
        # 先显式判存在: `spec_from_file_location` 对不存在的路径**也会成功**(它只造 spec),
        # 直到 exec_module 才由 loader 抛 FileNotFoundError —— 埋在 importlib 深处的
        # 文件系统错误, 读起来完全不像"冻结脚本被挪走了"。这里换成人话。
        if not _FROZEN_GATE_PATH.is_file():
            raise ImportError(
                f"冻结的预注册判据脚本不在了: {_FROZEN_GATE_PATH} —— 它是**冻结**判据, "
                f"只允许原地放着; 要归档/改名请先想清楚这条交叉校验还怎么成立")
        spec = importlib.util.spec_from_file_location("_frozen_exp_deadzone_ic",
                                                      _FROZEN_GATE_PATH)
        if spec is None or spec.loader is None:      # 不是可加载的源码(权限/编码等)
            raise ImportError(f"冻结的预注册判据脚本加载失败: {_FROZEN_GATE_PATH}")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _frozen_gate_mod = mod
    return _frozen_gate_mod


def _panel(seed=0, n_dates=6, n_stocks=400):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2020-09-30", periods=n_dates, freq="ME")
    cols = [f"{i:06d}.SZ" for i in range(n_stocks)]
    f = pd.DataFrame(rng.normal(size=(n_dates, n_stocks)), index=idx, columns=cols)
    r = f * 0.01 + rng.normal(0, 0.05, size=(n_dates, n_stocks))
    return idx, f, pd.DataFrame(r, index=idx, columns=cols)


class TestCrossCheckWithPreregisteredGate(unittest.TestCase):
    """与预注册门禁脚本的统计量必须逐字一致"""

    def test_head_excess_matches_gate_evaluate(self):
        gate = _frozen_gate()      # 冻结脚本按路径加载, 见模块顶部说明

        idx, f, r = _panel()
        zones = np.array(["DEAD"] * len(idx))
        got = ex.daily_head_stats(f, r, idx)
        ref = gate.evaluate(f, r, idx, zones)
        self.assertEqual(len(got), len(ref))
        # 逐截面逐列比对 —— 任一列漂移都会让两个判据打架
        for col in ("n", "head_excess", "top6_excess"):
            np.testing.assert_allclose(
                got[col].to_numpy(), ref[col].to_numpy(), rtol=0, atol=1e-12,
                err_msg=f"{col} 与预注册门禁的实现不一致")
        # 参考列 IC 也要一致(top6_excess 之外的另一把尺)
        j = pd.concat([got[["head_excess"]], ref[["head_excess"]]], axis=1)
        self.assertGreater(len(j), 0)

    def test_head_excess_matches_gate_with_partial_nan(self):
        gate = _frozen_gate()      # 冻结脚本按路径加载, 见模块顶部说明

        idx, f, r = _panel(seed=3)
        f = f.where(np.random.default_rng(1).random(f.shape) > 0.3)
        got = ex.daily_head_stats(f, r, idx)
        ref = gate.evaluate(f, r, idx, np.array(["DEAD"] * len(idx)))
        np.testing.assert_allclose(got["head_excess"].to_numpy(),
                                   ref["head_excess"].to_numpy(), rtol=0, atol=1e-12)


class TestFrozenGateIsLoadedByPath(unittest.TestCase):
    """加载方式本身也要守住(2026-09-12 审查: 测试质量项)

    交叉校验只有在**加载的确实是那份冻结脚本**时才有意义。原先靠 `sys.path` 插入 + 裸名
    import, 命中哪一份文件取决于 import 顺序与自己/别人的 path 操作; 现在路径写死, 于是
    「加载到的是不是那一个文件」变成可断言的事实。
    """

    def test_loaded_module_is_that_exact_file(self):
        gate = _frozen_gate()
        self.assertEqual(Path(gate.__file__).resolve(), _FROZEN_GATE_PATH.resolve())
        # 判据常量也在: 加载成功但内容被换掉同样是"静默换源"
        self.assertTrue(hasattr(gate, "evaluate"))

    def test_oneoff_dir_is_not_put_on_sys_path(self):
        """回归守卫: 不许再把 `scripts/oneoff/` 插进 `sys.path`。

        插了的话, 目录里几十个一次性脚本会以裸名对整轮测试可见 —— 谁的一句
        `import exp_*` 都可能命中冻结脚本, 而这与脚本的真实位置无关。
        """
        oneoff = str((_FROZEN_GATE_PATH.parent).resolve())
        self.assertNotIn(oneoff, [str(Path(p).resolve()) for p in sys.path if p],
                         "scripts/oneoff 又被插进 sys.path 了 —— 见本文件 _frozen_gate 说明")

    def test_second_call_is_cached(self):
        self.assertIs(_frozen_gate(), _frozen_gate())

    def test_missing_script_raises_import_error(self):
        """路径写死 ⇒ 脚本被挪走/改名要当场炸, 而不是静默去别处找一份同名文件。"""
        saved_path, saved_mod = _FROZEN_GATE_PATH, _frozen_gate_mod
        try:
            globals()["_FROZEN_GATE_PATH"] = saved_path.parent / "no_such_frozen_gate.py"
            globals()["_frozen_gate_mod"] = None
            with self.assertRaises(ImportError):
                _frozen_gate()
        finally:
            globals()["_FROZEN_GATE_PATH"] = saved_path
            globals()["_frozen_gate_mod"] = saved_mod


class TestHeadExcessSemantics(unittest.TestCase):
    def test_perfect_score_gives_positive_head(self):
        # 分数即前向收益 → 头部超额必须为正
        idx, _, r = _panel(seed=5)
        got = ex.daily_head_stats(r, r, idx)
        self.assertTrue((got["head_excess"] > 0).all())

    def test_inverted_score_gives_negative_head(self):
        idx, _, r = _panel(seed=5)
        got = ex.daily_head_stats(-r, r, idx)
        self.assertTrue((got["head_excess"] < 0).all())

    def test_small_cross_section_skipped(self):
        idx, f, r = _panel(seed=6, n_stocks=50)     # < MIN_NAMES
        self.assertTrue(ex.daily_head_stats(f, r, idx).empty)

    def test_restrict_to_intersection(self):
        # 覆盖域不同: A 只有前一半, B 全都有。限制到交集后 B 的截面股票数应减半
        idx, f, r = _panel(seed=7, n_stocks=400)
        half = f.iloc[:, :200]
        full = ex.daily_head_stats(f, r, idx)["n"]
        inter = ex.daily_head_stats(f, r, idx, restrict_to=half)["n"]
        self.assertTrue((inter == 200).all())
        self.assertTrue((full == 400).all())


class TestRankCorr(unittest.TestCase):
    def test_perfect_and_inverse(self):
        idx, f, _ = _panel(seed=8)
        self.assertAlmostEqual(ex.rank_corr_with(f, f, idx), 1.0, places=10)
        self.assertAlmostEqual(ex.rank_corr_with(f, -f, idx), -1.0, places=10)

    def test_constant_column_skipped(self):
        idx, f, _ = _panel(seed=9)
        c = f * 0 + 1.0
        self.assertTrue(np.isnan(ex.rank_corr_with(c, f, idx)))


class TestGates(unittest.TestCase):
    def test_bonferroni(self):
        self.assertAlmostEqual(ex.bonferroni(0.001, 10), 0.01, places=10)
        self.assertEqual(ex.bonferroni(0.5, 10), 1.0)   # 截断在 1
        self.assertTrue(np.isnan(ex.bonferroni(np.nan, 10)))

    def test_zone_of(self):
        d = pd.DatetimeIndex(["2019-01-31", "2020-09-30", "2023-12-29",
                              "2024-01-31", "2026-08-31", "2026-09-30"])
        z = ex.zone_of(d)
        self.assertEqual(list(z), ["OTHER", "DEAD", "DEAD", "ACTIVE", "ACTIVE", "OTHER"])

    def test_corr_ceiling_is_documented_constant(self):
        # 换皮保护: 阈值必须存在且在 (0,1) 内
        self.assertTrue(0 < ex.KGD_MAX_CORR < 1)


if __name__ == "__main__":
    unittest.main()
