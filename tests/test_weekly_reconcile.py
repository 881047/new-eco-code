"""成交核对的守卫（2026-09-13）

守三条：① 分类正确（已建仓/部分/未成交/方案外残留）；② 成交价偏离按 bps 算对，
取不到行情时降级成"无法核对"而不是抛异常；③ 资金恒等式判据（差 ≤1 元算对得上）。
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.weekly import reconcile as rc       # noqa: E402
from app.weekly.ledger import Ledger         # noqa: E402

BASE = Path(__file__).resolve().parent.parent


class TestPositionDiff(unittest.TestCase):
    PLAN = {"rows": [{"ts_code": "600000.SH", "shares": 1000},
                     {"ts_code": "000001.SZ", "shares": 500},
                     {"ts_code": "600519.SH", "shares": 100}]}

    def test_classification(self):
        pos = {"600000.SH": {"shares": 1000.0},      # 足额
               "000001.SZ": {"shares": 300.0}}       # 部分
        d = rc.diff_positions(self.PLAN, pos)
        self.assertEqual((d["n_target"], d["n_ok"], d["n_partial"], d["n_missing"]), (3, 1, 1, 1))
        states = {x["ts_code"]: x["state"] for x in d["deviations"]}
        self.assertEqual(states.get("000001.SZ"), "部分成交")
        self.assertEqual(states.get("600519.SH"), "未成交")
        self.assertNotIn("600000.SH", states)        # 足额的不进偏差清单

    def test_extra_holdings_are_reported(self):
        """方案外的残留（该卖没卖掉）必须单列 —— 否则持仓会悄悄漂移。"""
        d = rc.diff_positions(self.PLAN, {"000002.SZ": {"shares": 100.0}})
        self.assertEqual(d["n_extra"], 1)
        self.assertEqual(d["extra"], ["000002.SZ"])


class TestPriceDeviation(unittest.TestCase):
    TRADES = [{"ts_code": "600000.SH", "date": "20260914", "side": "buy", "price": 10.01},
              {"ts_code": "000001.SZ", "date": "20260914", "side": "sell", "price": 9.90}]

    def test_bps_against_open(self):
        df = pd.DataFrame({"ts_code": ["600000.SH", "000001.SZ"],
                           "trade_date": ["20260914", "20260914"], "open": [10.0, 10.0]})
        with mock.patch("app.weekly.reconcile.pd.read_sql", return_value=df) as rs:
            r = rc.price_deviation(self.TRADES)
        sql = rs.call_args.args[0]           # 必须按 (日期, 代码) 过滤, 不许扫全表
        self.assertIn("WHERE trade_date IN", sql)
        self.assertIn("ts_code IN", sql)
        self.assertEqual(r["n"], 2)
        self.assertAlmostEqual(r["mean_bps"], -45.0, places=1)   # (+10bps, −100bps)/2
        self.assertAlmostEqual(r["max_abs_bps"], 100.0, places=1)
        # 残差族(2026-09-19 B3): 先按方向翻成"逆风为正"(+10, +100)，再扣 10bps 假设滑点
        self.assertAlmostEqual(r["resid_mean_bps"], 45.0, places=1)
        self.assertAlmostEqual(r["assumed_slip_bps"], 10.0, places=1)

    def test_symmetric_adverse_fill_does_not_cancel(self):
        """**本轮的核心用例**：两侧同样劣化 50bps 时，旧口径恒绿、新口径必须红。

        买 10.06(+60bps) / 卖 9.94(−60bps) ⇒ 成交价对开盘的均值 = 0.0 ⇒
        旧判据 `abs(mean_bps) ≤ 20` 通过 —— 这正是 G2 永远不会红的原因：
        劣化在买卖之间**反号对消**。残差把方向统一成"逆风为正"后两侧都是 +50 ⇒ 判红。
        """
        trades = [{"ts_code": "600000.SH", "date": "20260914", "side": "buy", "price": 10.06},
                  {"ts_code": "000001.SZ", "date": "20260914", "side": "sell", "price": 9.94}]
        df = pd.DataFrame({"ts_code": ["600000.SH", "000001.SZ"],
                           "trade_date": ["20260914", "20260914"], "open": [10.0, 10.0]})
        with mock.patch("app.weekly.reconcile.pd.read_sql", return_value=df):
            r = rc.price_deviation(trades)
        self.assertAlmostEqual(r["mean_bps"], 0.0, places=1,
                               msg="旧口径应当仍然 ≈0（这条用例的意义就在于此）")
        self.assertAlmostEqual(r["resid_mean_bps"], 50.0, places=1)
        self.assertAlmostEqual(r["resid_p95_bps"], 50.0, places=1)
        # 阈值在 protocol，判据翻转由 test_weekly_protocol 钉；这里只钉"残差 ≠ 均值族"
        self.assertGreater(r["resid_mean_bps"], 20.0, "两侧对称劣化必须超 20bps 阈值")

    def test_clean_fill_resides_at_zero(self):
        """完全按口径执行（买 +10 / 卖 −10）⇒ 残差必须正好 0（新判据不会自己造假阳性）。"""
        trades = [{"ts_code": "600000.SH", "date": "20260914", "side": "buy", "price": 10.01},
                  {"ts_code": "000001.SZ", "date": "20260914", "side": "sell", "price": 9.99}]
        df = pd.DataFrame({"ts_code": ["600000.SH", "000001.SZ"],
                           "trade_date": ["20260914", "20260914"], "open": [10.0, 10.0]})
        with mock.patch("app.weekly.reconcile.pd.read_sql", return_value=df):
            r = rc.price_deviation(trades)
        self.assertAlmostEqual(r["resid_mean_bps"], 0.0, places=2)

    def test_assumed_slip_comes_from_config_not_a_literal(self):
        """假设滑点必须取自 `paper.slip` 唯一真源（原先硬编码 10.0 = 第二份真相）。"""
        df = pd.DataFrame({"ts_code": ["600000.SH"], "trade_date": ["20260914"], "open": [10.0]})
        with mock.patch("app.weekly.reconcile.pd.read_sql", return_value=df), \
                mock.patch("app.weekly.reconcile._assumed_slip", return_value=0.002):
            r = rc.price_deviation(
                [{"ts_code": "600000.SH", "date": "20260914", "side": "buy", "price": 10.02}])
        self.assertAlmostEqual(r["assumed_slip_bps"], 20.0, places=1)
        self.assertAlmostEqual(r["resid_mean_bps"], 0.0, places=1,
                               msg="config 改成 20bps 后按 20bps 执行就该是 0 残差")

    def test_lines_survive_a_legacy_payload(self):
        """`lines()` 不许因为缺残差字段就抛 KeyError —— 它跑在 scheduler 的**告警分支**里，
        一抛就把整条运维告警静默吞掉（2026-09-19 加残差时正是被这条抓出来的）。"""
        res = {"available": True, "plan": "plan_x.json", "anchor": "20260911",
               "positions": {"n_target": 0, "n_ok": 0, "n_partial": 0, "n_missing": 0,
                             "n_extra": 0, "deviations": [], "extra": []},
               "prices": {"n": 2, "mean_bps": 1.0, "median_bps": 1.0, "max_abs_bps": 2.0,
                          "assumed_slip_bps": 10.0},
               "identity": {"n": 0, "ok": True, "gap": 0.0, "cash": 0, "market_value": 0,
                            "nav": 0, "note": "x"}}
        out = rc.lines(res)
        self.assertTrue(any("均值" in l for l in out), f"旧 payload 应退回旧文案: {out}")

    def test_missing_market_data_degrades(self):
        with mock.patch("app.weekly.reconcile.pd.read_sql",
                        return_value=pd.DataFrame({"ts_code": [], "trade_date": [], "open": []})):
            r = rc.price_deviation(self.TRADES)
        self.assertEqual(r["n"], 0)
        self.assertIn("note", r)

    def test_no_trades(self):
        self.assertEqual(rc.price_deviation([])["n"], 0)


class TestCashIdentityAndReport(unittest.TestCase):
    def setUp(self):
        self.path = BASE / "data" / "weekly" / "_test_reconcile.json"
        if self.path.exists():
            self.path.unlink()

    def tearDown(self):
        if self.path.exists():
            self.path.unlink()

    def test_identity_ok_and_gap(self):
        led = Ledger(100_000.0, self.path)
        led.state["cash"] = 90_000.0
        led.state["positions"] = {"600000.SH": {"shares": 1000.0, "last_px": 10.0}}
        led.state["nav_history"] = [{"date": "20260914", "nav": 100_000.0}]
        self.assertTrue(rc.cash_identity(led)["ok"])
        led.state["nav_history"] = [{"date": "20260914", "nav": 99_000.0}]   # 差 1000
        self.assertFalse(rc.cash_identity(led)["ok"])

    def test_identity_includes_the_interest_term(self):
        """2026-09-14（07 #49）：恒等式必须含**累计利息**，否则 gap 恒等于 −利息。

        `Ledger.mark` 写的净值是 `现金 + 利息 + 市值`；原 `cash_identity` 少了利息项 ⇒
        (a) 第 2 个净值点起每天假 ✗、(b) `protocol` 的放行闸 G5「记账完整」**永远过不了**。
        红检：把 interest 项从 gap 里去掉 ⇒ 本用例当场红。
        """
        led = Ledger(100_000.0, self.path)
        led.state["cash"] = 90_000.0
        led.state["interest"] = 5.5
        led.state["positions"] = {"600000.SH": {"shares": 1000.0, "last_px": 10.0}}
        led.state["nav_history"] = [{"date": "20260915", "nav": 100_005.5,
                                     "interest": 5.5}]
        r = rc.cash_identity(led)
        self.assertTrue(r["ok"], f"含利息的健康账本必须 ✓（gap={r['gap']}）")
        self.assertAlmostEqual(r["gap"], 0.0, places=2)

    def test_identity_reports_n_so_the_alert_can_fire(self):
        """`n` 是调度器判"有没有净值点"的依据；原先**没有这个键** ⇒ 告警永远不触发（哑警报）。"""
        led = Ledger(100_000.0, self.path)
        led.state["nav_history"] = [{"date": "20260915", "nav": 100_000.0}]
        self.assertEqual(rc.cash_identity(led)["n"], 1)
        self.assertEqual(rc.cash_identity(Ledger(100_000.0, self.path))["n"], 0)  # 无点 ⇒ n=0

    def test_lines_print_the_interest_term(self):
        """打印的算式必须与真式逐项一致（现金 + 利息 + 市值 = 净值）。"""
        led = Ledger(100_000.0, self.path)
        led.state["nav_history"] = [{"date": "20260915", "nav": 100_000.0, "interest": 0.0}]
        led.save()
        res = rc.report(ledger_path=self.path)
        line = [x for x in rc.lines(res) if "资金恒等式" in x][0]
        self.assertIn("利息", line)

    def test_report_and_lines_never_raise(self):
        res = rc.report()
        self.assertIn("available", res)
        self.assertTrue(rc.lines(res))
        self.assertTrue(rc.lines({"available": False, "note": "x"}))       # 降级路径

    def test_report_with_temp_ledger(self):
        led = Ledger(1_000_000.0, self.path)
        led.save()
        res = rc.report(ledger_path=self.path)
        self.assertTrue(res.get("available"))
        self.assertEqual(res["positions"]["n_ok"], 0)                       # 空账本
        self.assertTrue(rc.lines(res))


if __name__ == "__main__":
    unittest.main()
