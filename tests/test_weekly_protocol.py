"""纸面→小资金 放行协议的守卫（2026-09-13）

守三条：① 空账本只能过 G4（其余必须明确报 ✗，不许"四舍五入"）；
② 满 12 周 + 撮合达标 + 投放达标 + 恒等式成立 ⇒ 全过，且结论里**必须**带
"运行可信 ≠ alpha 已确认"的声明；③ 停买状态必须让 G4 变 ✗。
"""
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.weekly import protocol as pr      # noqa: E402

BASE = Path(__file__).resolve().parent.parent
LEDGER = BASE / "data" / "weekly" / "_test_protocol.json"


def _weeks(n: int) -> list:
    """最近 n 个周五(倒序到正序), 保证"最后一点在 14 天内"。"""
    today = pd.Timestamp.today().normalize()
    days = pd.date_range(end=today, periods=n, freq="W-FRI")
    return [d.strftime("%Y%m%d") for d in days]


class TestProtocol(unittest.TestCase):
    def setUp(self):
        if LEDGER.exists():
            LEDGER.unlink()

    def tearDown(self):
        if LEDGER.exists():
            LEDGER.unlink()

    def _write(self, state):
        LEDGER.write_text(json.dumps(state), encoding="utf-8")

    def test_empty_ledger_only_G4_passes(self):
        res = pr.status(ledger_path=LEDGER)
        self.assertEqual(res["n_pass"], 1)
        self.assertTrue(res["gates"]["G4_风控状态"]["ok"])
        for g in ("G1_连续运行", "G2_撮合口径", "G3_资金投放", "G5_记账完整"):
            self.assertFalse(res["gates"][g]["ok"], g)
        self.assertIn("未达放行", res["verdict"])
        self.assertTrue(pr.lines(res))

    def test_all_gates_pass_with_12_weeks_and_clean_fills(self):
        wk = _weeks(13)
        nav = 1_000_000.0
        hist, trades = [], []
        for i, d in enumerate(wk):
            trades.append({"date": d, "ts_code": "600000.SH", "side": "buy",
                           "shares": 1000, "price": 10.01, "fee": 5.1, "reason": "测试"})
            hist.append({"date": d, "nav": round(nav, 2), "invested": round(nav * 0.75, 2),
                         "cash": round(nav - 10_000.0, 2), "positions": {"600000.SH": 1000.0}})
        self._write({"capital": 1_000_000.0, "cash": nav - 10_000.0,
                     "positions": {"600000.SH": {"shares": 1000.0, "last_px": 10.0}},
                     "nav_history": hist, "trades": trades, "inception": wk[0],
                     "last_mark": wk[-1]})
        df = pd.DataFrame({"ts_code": ["600000.SH"], "trade_date": [wk[-1]], "open": [10.0]})
        with mock.patch("app.weekly.reconcile.pd.read_sql", return_value=df):
            res = pr.status(ledger_path=LEDGER)
        self.assertEqual(res["n_pass"], res["n_gates"], res["gates"])
        self.assertIn("alpha", res["alpha_disclaimer"])
        self.assertIn("运行可信", res["verdict"])

    def test_interest_bearing_ledger_passes_g5(self):
        """2026-09-14（07 #49）：**带利息的真实账本**必须能让 G5「记账完整」通过。

        改动前 `cash_identity` 漏利息项 ⇒ `ok` 从第 2 个点起恒为 False，而 G5 要求
        `ok 且 len(nav)>=2` —— 两个条件互斥 ⇒ **放行闸永远过不了**（终端的验收门自锁）。
        红检：去掉 gap 里的 interest ⇒ 本用例当场红。
        """
        wk = _weeks(3)
        hist = [{"date": wk[0], "nav": 1_000_000.0, "invested": 900_000.0,
                 "cash": 100_000.0, "interest": 0.0, "positions": {"600000.SH": 1000.0}},
                {"date": wk[1], "nav": 1_000_004.1, "invested": 900_000.0,
                 "cash": 100_000.0, "interest": 4.1, "positions": {"600000.SH": 1000.0}},
                {"date": wk[2], "nav": 1_000_008.2, "invested": 900_000.0,
                 "cash": 100_000.0, "interest": 8.2, "positions": {"600000.SH": 1000.0}}]
        self._write({"capital": 1_000_000.0, "cash": 100_000.0,
                     "positions": {"600000.SH": {"shares": 1000.0, "last_px": 900.0}},
                     "nav_history": hist, "trades": [], "inception": wk[0],
                     "last_mark": wk[-1], "interest": 8.2})
        res = pr.status(ledger_path=LEDGER)
        self.assertTrue(res["gates"]["G5_记账完整"]["ok"], res["gates"]["G5_记账完整"])

    def test_halt_fails_G4(self):
        self._write({"capital": 1e6, "cash": 1e6, "positions": {}, "nav_history": [],
                     "trades": [], "inception": None, "last_mark": None,
                     "halt": {"active": True, "since": "20261101", "reason": "健康度 dead"}})
        res = pr.status(ledger_path=LEDGER)
        self.assertFalse(res["gates"]["G4_风控状态"]["ok"])
        self.assertIn("停买中", res["gates"]["G4_风控状态"]["detail"])

    def test_zero_slippage_is_not_treated_as_missing(self):
        """执行残差**正好 0.0bps**（= 完全按口径成交，最好情况）不许被判成"缺失"。

        实测踩过的坑: 判据写成 `mean_bps or 99`, 而 `0.0` 是 falsy ⇒ 完美撮合反而判 ✗。
        2026-09-19 B3 之后被判的字段换成了 `resid_mean_bps` ⇒ 这条用例跟着换轴：
        买单成交价 = 开盘 ×1.001 ⇒ 偏离 +10bps ⇒ 残差 **正好 0.0**。
        """
        wk = _weeks(13)
        hist = [{"date": d, "nav": 1e6, "invested": 7.5e5, "cash": 9.9e5} for d in wk]
        trades = [{"date": wk[-1], "ts_code": "600000.SH", "side": "buy",
                   "shares": 1000, "price": round(10.0 * 1.001, 3), "fee": 5.0, "reason": "测试"}]
        self._write({"capital": 1e6, "cash": 9.9e5,
                     "positions": {"600000.SH": {"shares": 1000.0, "last_px": 10.0}},
                     "nav_history": hist, "trades": trades, "inception": wk[0],
                     "last_mark": wk[-1]})
        df = pd.DataFrame({"ts_code": ["600000.SH"], "trade_date": [wk[-1]], "open": [10.0]})
        with mock.patch("app.weekly.reconcile.pd.read_sql", return_value=df):
            res = pr.status(ledger_path=LEDGER)
        g2 = res["gates"]["G2_撮合口径"]
        self.assertIn("残差均值 0.0 bps", g2["detail"])     # 读数确实是 0.0，不是被兜底
        self.assertTrue(g2["ok"], g2)

    def test_G2_fails_when_adverse_fill_cancels_in_the_mean(self):
        """**本轮的分辨力证据**：两侧对称劣化 ⇒ 旧口径均值 0.0（判绿）、新口径残差 50（判红）。

        买 10.06(+60bps) / 卖 9.94(−60bps)：G2 原式 `abs(mean_bps) ≤ 20` 在这本账上**永远不可能红**
        （劣化反号对消）。换成"逆风为正"的残差后两侧都 +50bps ⇒ 必须 ✗。
        去掉 protocol.py 里的判据切换、或去掉 reconcile 的方向翻号，本用例都会红。
        """
        wk = _weeks(13)
        hist = [{"date": d, "nav": 1e6, "invested": 7.5e5, "cash": 9.9e5} for d in wk]
        trades = [{"date": wk[-1], "ts_code": "600000.SH", "side": "buy", "shares": 1000,
                   "price": 10.06, "fee": 5.0, "reason": "测试"},
                  {"date": wk[-1], "ts_code": "000001.SZ", "side": "sell", "shares": 1000,
                   "price": 9.94, "fee": 5.0, "reason": "测试"}]
        self._write({"capital": 1e6, "cash": 9.9e5,
                     "positions": {"600000.SH": {"shares": 1000.0, "last_px": 10.0}},
                     "nav_history": hist, "trades": trades, "inception": wk[0],
                     "last_mark": wk[-1]})
        df = pd.DataFrame({"ts_code": ["600000.SH", "000001.SZ"],
                           "trade_date": [wk[-1], wk[-1]], "open": [10.0, 10.0]})
        with mock.patch("app.weekly.reconcile.pd.read_sql", return_value=df):
            g2 = pr.status(ledger_path=LEDGER)["gates"]["G2_撮合口径"]
        self.assertFalse(g2["ok"], f"劣化被均值抵消后 G2 仍判绿 ⇒ 闸还是假的: {g2}")
        self.assertIn("残差均值 50", g2["detail"])
        self.assertIn("单笔最差偏离 60", g2["detail"])

    def test_identity_detail_shows_gap_not_no_nav(self):
        """恒等式成立时明细必须报"恒等式差 x", 不许退化成"无净值点"(实测踩到的键名坑)。"""
        wk = _weeks(13)
        hist = [{"date": d, "nav": 1e6, "invested": 7.5e5} for d in wk]
        self._write({"capital": 1e6, "cash": 1e6, "positions": {}, "nav_history": hist,
                     "trades": [], "inception": wk[0], "last_mark": wk[-1]})
        res = pr.status(ledger_path=LEDGER)
        self.assertIn("恒等式差", res["gates"]["G5_记账完整"]["detail"])

    def test_low_invested_fails_G3(self):
        wk = _weeks(13)
        hist = [{"date": d, "nav": 1e6, "invested": 5e5, "cash": 5e5} for d in wk]
        self._write({"capital": 1e6, "cash": 5e5, "positions": {}, "nav_history": hist,
                     "trades": [], "inception": wk[0], "last_mark": wk[-1]})
        res = pr.status(ledger_path=LEDGER)
        self.assertFalse(res["gates"]["G3_资金投放"]["ok"])
        self.assertIn("0.500", res["gates"]["G3_资金投放"]["detail"])


class TestB1SizeExposureAnnotation(unittest.TestCase):
    """B1 规模暴露守卫（D 案 ③-3）：**只标注，不动作**。

    为什么是标注而不是第六道闸：F67 实测剔微盘段会让留出集转负 ⇒ 规模暴露是超额的本体，
    但"要不要因此改配置"是**换 pin 级决策**，必须另立预注册。把它做成闸，
    就会出现"系统自己因为一个标注改了敞口"这种无人批准的行为变更。

    红检方向：让 `size_exposure_note` 返回 `flagged` 之外还改 gates ⇒
    `test_annotation_does_not_change_the_release_verdict` 当场红。
    """

    def setUp(self):
        if LEDGER.exists():
            LEDGER.unlink()

    def tearDown(self):
        if LEDGER.exists():
            LEDGER.unlink()

    def test_inside_one_sigma_is_normal(self):
        r = pr.size_exposure_note(0.5)
        self.assertTrue(r["available"])
        self.assertFalse(r["flagged"])
        self.assertIn("正常", r["note"])

    def test_beyond_one_sigma_is_flagged_but_says_no_action(self):
        r = pr.size_exposure_note(1.5)
        self.assertTrue(r["flagged"])
        self.assertIn("偏大盘", r["note"])
        self.assertIn("不动作", r["note"], "标注项必须写明它不会自己动")

    def test_negative_side_is_flagged_as_small_cap(self):
        r = pr.size_exposure_note(-1.2)
        self.assertTrue(r["flagged"])
        self.assertIn("偏小盘", r["note"])

    def test_boundary_is_one_sigma(self):
        self.assertFalse(pr.size_exposure_note(1.0)["flagged"], "恰好 1σ 不算越过")
        self.assertTrue(pr.size_exposure_note(1.01)["flagged"])
        self.assertEqual(pr.B1_SIGMA_ALERT, 1.0)

    def test_missing_reading_is_not_treated_as_normal(self):
        for v in (None, float("nan")):
            r = pr.size_exposure_note(v)
            self.assertFalse(r["available"], f"{v} 必须报无读数")
            self.assertFalse(r["flagged"])

    def test_annotation_does_not_change_the_release_verdict(self):
        """放行结论只由 G1~G5 决定：B1 标注再红也不许改变 `n_pass` / `verdict`。"""
        res = pr.status(ledger_path=LEDGER)
        self.assertIn("b1_size_exposure", res)
        self.assertEqual(res["n_gates"], 5, "B1 是标注项，不是第六道闸")
        self.assertNotIn("B1", res["gates"])
        self.assertIn("B1 规模暴露", "\n".join(pr.lines(res)))


if __name__ == "__main__":
    unittest.main()
