"""周频归因模块守卫（2026-09-13，规划 §6.1-5）

归因最容易出的两类错：**恒等式不守恒**（差额被悄悄藏进"残差"）与**研究/生产两套口径**
（本项目已经踩过三次）。文件把这两件事钉死，另外守住「账本为空/点不足时不许抛异常、不许写盘」。

运行: .venv\\Scripts\\python.exe -X utf8 -m pytest tests/test_weekly_attrib.py -q
"""
import hashlib
import importlib.util
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from app.weekly import attrib  # noqa: E402
from app.weekly import blocks as wk_blocks  # noqa: E402
from app.weekly.ledger import Ledger  # noqa: E402

# 核心风格里 mom_60 要 60 个交易日、vol_20/mom_20 要 20 个 ⇒ 锚点必须落在有足够历史的区间
N_DATES, N_CODES, N_HOLD = 120, 260, 30


def _market(seed: int = 7):
    """合成行情面板（字段与 fe.load_market 一致），只给归因用得到的那几个。"""
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2026-06-01", periods=N_DATES)
    codes = [f"{i:06d}.SZ" for i in range(1, N_CODES + 1)]
    close = pd.DataFrame(10 * np.exp(np.cumsum(rng.normal(0, 0.02, (N_DATES, N_CODES)), axis=0)),
                         index=dates, columns=codes)
    pe = np.where(rng.random(close.shape) < 0.1, np.nan, rng.lognormal(3, 0.6, close.shape))
    dv = np.where(rng.random(close.shape) < 0.3, np.nan, rng.random(close.shape))
    return {"close": close, "open": close * (1 + rng.normal(0, 0.003, close.shape)),
            "adj": pd.DataFrame(1.0, index=dates, columns=codes),
            "total_mv": pd.DataFrame(rng.lognormal(10, 1, close.shape), index=dates, columns=codes),
            "pe_ttm": pd.DataFrame(pe, index=dates, columns=codes),
            "pb": pd.DataFrame(rng.lognormal(0.5, 0.5, close.shape), index=dates, columns=codes),
            "dv_ttm": pd.DataFrame(dv, index=dates, columns=codes),
            "turnover": pd.DataFrame(rng.lognormal(0, 0.5, close.shape), index=dates, columns=codes)}


class _AllTradable:
    """鸭子类型的 PIT：归因只需要 tradable_matrix。"""

    def tradable_matrix(self, anchors, columns=None, **kw):
        return pd.DataFrame(True, index=pd.to_datetime(anchors), columns=list(columns))


class TestAttributionIdentity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = _market()
        dates = pd.to_datetime(cls.m["close"].index)
        codes = list(cls.m["close"].columns)
        cls.anchors = dates[70::5][:6]               # 6 个净值点 ⇒ 5 期
        cls.points = []
        for d in cls.anchors:
            pos = {c: 100.0 for c in codes[:N_HOLD]}
            px = cls.m["close"].loc[d]
            invested = float(sum(100.0 * px[c] for c in pos))
            cash = 10_000.0
            cls.points.append({"date": d.strftime("%Y%m%d"), "nav": invested + cash,
                               "invested": invested, "cash": cash, "positions": pos})
        with mock.patch.object(attrib, "sw_industry_map_pit",
                               return_value=pd.DataFrame(index=cls.anchors)):
            cls.res = attrib.attribute_periods(cls.points, cls.m, _AllTradable())
        cls.summary = attrib.summarize(cls.res)

    def test_no_period_silently_dropped(self):
        self.assertEqual(self.res["skipped"], 0)
        self.assertEqual(len(self.res["periods"]), len(self.points) - 1)

    def test_identity_holds_every_period(self):
        for r in self.res["periods"]:
            total = r["style_total"] + r["industry_total"] + r["missing_total"] + r["specific"]
            self.assertAlmostEqual(total, r["port_excess"], places=12)
            self.assertAlmostEqual(r["other"], r["impl_gap"] - r["cash_drag"], places=12)

    def test_summary_identity_and_shapes(self):
        s = self.summary
        self.assertEqual(s["n"], len(self.points) - 1)
        self.assertLess(abs(s["identity_check_pp"]), 0.05)
        self.assertIn("size", s["style"])
        # 投入率必须等于各期 (持仓市值/净值) 的均值（区间起点口径）
        exp = float(np.mean([p["invested"] / p["nav"] for p in self.points[:-1]]))
        self.assertAlmostEqual(s["invested"], round(exp, 4), places=4)
        # 现金拖累的定义必须逐期与 (1−投入率)×持仓收益 反向（持仓涨 ⇒ 现金拖累为负）
        for r in self.res["periods"]:
            self.assertAlmostEqual(r["cash_drag"], -(1 - r["invested"]) * r["port_ret"],
                                   places=6)      # invested 按 4 位小数入账 ⇒ 容差取 1e-6


class TestBlockAttributionIdentity(unittest.TestCase):
    """**块级归因恒等式**（D 案 ③-2 + 验收 §四）：|Σ块贡献 − 持仓超额| < 1e-6，逐期成立。

    为什么必须钉死：把不属于任何块的那部分（行业 / 缺失指示 / 选股残差）悄悄塞进
    "残差"里，块贡献之和就会看起来等于超额 —— 那是一次**会计魔术**，不是分解。
    所以本口径显式留一个 `未归类` 桶，恒等式要求 **7 项**之和 = 持仓超额。

    B6 恒为 0 是**本口径的已知边界**（事件类不在 Fama-MacBeth 风格集内），
    不是"它没有贡献" —— 这条注释与断言一起钉住，防止后人把它读成事实。
    """

    @classmethod
    def setUpClass(cls):
        m = _market(seed=11)
        dates = pd.to_datetime(m["close"].index)
        codes = list(m["close"].columns)
        cls.anchors = dates[70::5][:6]
        cls.points = []
        for d in cls.anchors:
            pos = {c: 100.0 for c in codes[:N_HOLD]}
            px = m["close"].loc[d]
            invested = float(sum(100.0 * px[c] for c in pos))
            cls.points.append({"date": d.strftime("%Y%m%d"), "nav": invested + 10_000.0,
                               "invested": invested, "cash": 10_000.0, "positions": pos})
        with mock.patch.object(attrib, "sw_industry_map_pit",
                               return_value=pd.DataFrame(index=cls.anchors)):
            cls.res = attrib.attribute_periods(cls.points, m, _AllTradable())
        cls.summary = attrib.summarize(cls.res)

    def test_identity_holds_every_period(self):
        self.assertTrue(self.res["periods"], "夹具必须真的产出期数，否则恒等式测了个空")
        for r in self.res["periods"]:
            self.assertLess(abs(r["block_total"] - r["port_excess"]), 1e-6)
            named = sum(v for k, v in r["blocks"].items() if k != wk_blocks.UNATTRIBUTED)
            # Σ(B1..B6) 必须**恰好**等于风格那部分（风格列全部有归属 ⇒ 不许凭空多出/少掉）
            self.assertAlmostEqual(named, r["style_total"], places=12)

    def test_summary_identity_and_extremes(self):
        s = self.summary
        self.assertIsNotNone(s["block_identity_max_dev"])
        self.assertLess(s["block_identity_max_dev"], 1e-6)
        self.assertEqual(sorted(s["blocks"]),
                         sorted(tuple(wk_blocks.BLOCK_KEYS) + (wk_blocks.UNATTRIBUTED,)))
        self.assertEqual(s["blocks"]["B6"], 0.0, "B6 在本口径恒 0（无对应风格列）")
        self.assertIn(s["top_block"]["block"], wk_blocks.BLOCK_KEYS)
        self.assertIn(s["worst_block"]["block"], wk_blocks.BLOCK_KEYS)
        self.assertGreaterEqual(s["top_block"]["pp"], s["worst_block"]["pp"])

    def test_size_exposure_is_reported_for_the_b1_guard(self):
        """B1 暴露守卫（`protocol.size_exposure_note`）的**输入**必须来自归因，不许另立一套。"""
        for r in self.res["periods"]:
            self.assertIsNotNone(r["size_z"], "每期都要有规模暴露读数")
        self.assertIsNotNone(self.summary["size_z_mean"])

    def test_lines_report_blocks_and_the_b6_caveat(self):
        # `summarize` 只出读数层，"available" 由 `current()` 补；lines 的人读分支看这个键
        txt = "\n".join(attrib.lines(dict(self.summary, available=True)))
        self.assertIn("块贡献", txt)
        self.assertIn("未归类", txt)
        self.assertIn("B6", txt, "B6 恒 0 这件事必须在人读的那一行里说清楚")
        self.assertIn("最大块贡献", txt)


class TestResearchProductionParity(unittest.TestCase):
    def test_style_panels_match_research(self):
        """生产与研究必须用**同一份**风格定义（否则两边读数不可比）。"""
        p = BASE / "experiments" / "05_label" / "F24_attribution.py"
        spec = importlib.util.spec_from_file_location("f24_for_parity", p)
        mod = importlib.util.module_from_spec(spec)
        sys.modules["f24_for_parity"] = mod
        spec.loader.exec_module(mod)
        m = _market()
        dates = pd.to_datetime(m["close"].index)
        cols = list(m["close"].columns)
        a, b = attrib.style_panels(m, dates, cols), mod.style_panels(m, dates, cols)
        self.assertEqual(sorted(a), sorted(b))
        for k in a:
            pd.testing.assert_frame_equal(a[k], b[k].astype("float64"))


class TestGuards(unittest.TestCase):
    def setUp(self):
        self.path = BASE / "data" / "weekly" / "_test_attrib_ledger.json"

    def tearDown(self):
        if self.path.exists():
            self.path.unlink()

    def _write(self, state):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(state), encoding="utf-8")

    def test_empty_ledger_is_not_an_error(self):
        Ledger(100_000.0, self.path).save()
        before = hashlib.sha256(self.path.read_bytes()).hexdigest()
        out = attrib.current(ledger_path=self.path)
        self.assertFalse(out["available"])
        self.assertIn("note", out)
        self.assertEqual(before, hashlib.sha256(self.path.read_bytes()).hexdigest(),
                         "归因是只读的: 看一眼不许改账本")

    def test_single_point_is_not_an_error(self):
        self._write({"capital": 1e6, "cash": 1e6, "positions": {}, "trades": [],
                     "inception": "20260914", "last_mark": "20260914",
                     "nav_history": [{"date": "20260914", "nav": 1e6, "invested": 0.0}]})
        out = attrib.current(ledger_path=self.path)
        self.assertFalse(out["available"])
        self.assertEqual(out["n_points"], 1)

    def test_lines_never_raise(self):
        self.assertTrue(attrib.lines({"available": False, "note": "x"}))
        self.assertTrue(attrib.lines({"available": True, "window": ["a", "b"], "n": 1,
                                      "invested": 0.7, "skipped": 0, "note": "样本少"}))

    def test_old_format_points_are_counted_not_silent(self):
        """旧格式净值点(没有 positions 快照)必须被**计数上报**，不能静默丢。"""
        points = [{"date": "20260914", "nav": 1e6, "invested": 7e5},
                  {"date": "20260921", "nav": 1.01e6, "invested": 7e5}]
        res = attrib.attribute_periods(points, _market(), _AllTradable())
        self.assertEqual(res["periods"], [])
        self.assertEqual(res["skipped"], 1)


class TestLedgerRecordsPositionsSnapshot(unittest.TestCase):
    """账本净值点必须带持仓快照 —— 否则生产归因永远无米下锅。"""

    def test_mark_point_has_positions(self):
        path = BASE / "data" / "weekly" / "_test_attrib_snap.json"
        try:
            led = Ledger(100_000.0, path)
            led.state["cash"] = 90_000.0
            led.state["positions"] = {"600000.SH": {"shares": 1000.0, "cost": 10.0,
                                                    "last_px": 10.0}}
            px = pd.DataFrame({"ts_code": ["600000.SH"], "close": [10.0], "adj_factor": [1.0]})
            with mock.patch("app.weekly.ledger.pd.read_sql", return_value=px):
                led.mark("20260914")
            pt = led.state["nav_history"][-1]
            self.assertEqual(pt["positions"], {"600000.SH": 1000.0})
            self.assertAlmostEqual(pt["nav"], 90_000.0 + 10_000.0, places=2)
            self.assertEqual(pt["cash"], 90_000.0)
        finally:
            if path.exists():
                path.unlink()


if __name__ == "__main__":
    unittest.main()
