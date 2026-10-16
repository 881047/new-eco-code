"""账户级回撤治理(决策函数)的守卫测试（2026-09-25）

守五组:
  1. 纯函数滞回(`governor.hysteresis`): 触发/解除/再触发/边界/不足两点/脏净值剔除/
     新峰值重置参照 —— 判定必须是净值历史的纯函数(可逐位复算, 无持久状态);
  2. 配置段解析(`governor.band`): 缺省**启用**(fail-safe)/显式停用/阈值错配必须抛;
  3. 月盘 `apply_plan`: 谨慎态 ⇒ 买单(补仓)不挂、卖出/减仓照常; 浅回撤自动放行;
     显式停用后深回撤也放行;
  4. 月盘 `apply_signals`: 谨慎态 ⇒ add/候选替换买入全停; defense 状态 ⇒ 候选只看不動
     (config dynamic 段登记过的已知项在执行侧的落法); 卖出一概不经过两闸;
  5. 袖套执行档: 谨慎态 ⇒ allow_buys=False(零买入), 判定副本随账落盘;
     健康档在触发⇄解除翻转时发一条运维告警。

运行: python -m pytest tests/test_governor.py -q
"""
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from app.portfolio import governor as gov          # noqa: E402
from app.paper import engine                       # noqa: E402
from tests._paper_base import _ExecBase            # noqa: E402
from tests._weekly_slot_base import ANCHOR, TODAY, WeeklySlotTestCase  # noqa: E402


class TestHysteresis(unittest.TestCase):
    def test_rising_series_never_trips(self):
        out = gov.hysteresis([100.0, 101.0, 99.5, 103.0])
        self.assertFalse(out["cautious"])

    def test_trips_at_trip_line_and_reports_since(self):
        out = gov.hysteresis([100.0, 90.0, 80.0], ["20260901", "20260902", "20260903"])
        self.assertTrue(out["cautious"])
        self.assertEqual(out["since"], "20260903")     # 首次跌破 -20% 的那天
        self.assertAlmostEqual(out["dd"], -0.20, places=6)

    def test_stays_tripped_inside_the_band(self):
        # 100 → 80(触发) → 85 → 88: 回撤 -15%/-12%, 未过解除线(-10%) ⇒ 仍在谨慎态
        out = gov.hysteresis([100.0, 80.0, 85.0, 88.0])
        self.assertTrue(out["cautious"])

    def test_resets_at_reset_line(self):
        # 100 → 80 → 90: 回撤恰好 -10% ≥ 解除线 ⇒ 自动解除
        out = gov.hysteresis([100.0, 80.0, 90.0])
        self.assertFalse(out["cautious"])
        self.assertIsNone(out["since"])

    def test_retrips_after_reset_with_fresh_since(self):
        # 100 → 80(触发) → 91(解除) → 79(再触发): since 更新为再触发日
        out = gov.hysteresis([100.0, 80.0, 91.0, 79.0],
                             ["20260901", "20260902", "20260903", "20260904"])
        self.assertTrue(out["cautious"])
        self.assertEqual(out["since"], "20260904")

    def test_new_peak_resets_the_reference(self):
        # 100 → 80 → 110(新高) → 90: 相对**新峰值**回撤 -18.2% ⇒ 从未触发
        out = gov.hysteresis([100.0, 80.0, 110.0, 90.0])
        self.assertFalse(out["cautious"])
        self.assertAlmostEqual(out["peak"], 110.0, places=6)

    def test_fewer_than_two_points_does_not_gate(self):
        for navs in ([], [100.0]):
            out = gov.hysteresis(navs)
            self.assertFalse(out["cautious"])
            self.assertIn("不判定", out["reason"])

    def test_dirty_points_are_dropped(self):
        # None / NaN / 非正净值剔除后剩 3 点: 100 → 80(触发) → 60(仍触发)
        out = gov.hysteresis([100.0, None, float("nan"), 80.0, -5.0, 60.0])
        self.assertTrue(out["cautious"])
        self.assertEqual(out["n_points"], 3)


class TestBand(unittest.TestCase):
    def test_defaults_are_enabled_fail_safe(self):
        enabled, trip, reset = gov.band({})
        self.assertTrue(enabled)                       # 缺省启用, 与 weekly.risk_halt 同取向
        self.assertEqual((trip, reset), (-0.20, -0.10))

    def test_disabled_section(self):
        self.assertFalse(gov.band({"enabled": False})[0])
        out = gov.decide([("20260901", 100.0), ("20260902", 1.0)], {"enabled": False})
        self.assertFalse(out["cautious"])
        self.assertEqual(out["reason"], "governor.disabled")

    def test_bad_thresholds_raise(self):
        with self.assertRaises(ValueError):
            gov.band({"dd_trip": 0.1})                 # 触发线必须为负
        with self.assertRaises(ValueError):
            gov.band({"dd_trip": -0.10, "dd_reset": -0.20})   # 解除带必须在触发带上方

    def test_production_defaults_follow_the_ab_verdict(self):
        """2026-09-25 A/B 判决的钉子: 月盘停用(清仓陷阱, -12.76pp/年 且 MDD 恶化)、
        袖套启用(-3.14pp/年 保费换 MDD 改善 13.75pp)。改这两个缺省 = 推翻 A/B 判决,
        必须带新证据(ab_governor_overlay.py 重跑)并在 commit 里写明 Δ。"""
        from app.config import DEFAULT_CONFIG
        self.assertFalse(DEFAULT_CONFIG["paper"]["governor"]["enabled"],
                         "月盘回撤治理已被 A/B 判否, 缺省必须停用")
        self.assertTrue(DEFAULT_CONFIG["weekly"]["governor"]["enabled"],
                        "袖套回撤治理已被 A/B 采纳, 缺省必须启用")


# ---------------------------------------------------------------- 月盘接线
class TestMonthlyPlanGate(_ExecBase):
    """apply_plan: 谨慎态 ⇒ 补仓买单不挂, 卖出/减仓照常。"""

    def _seed(self, navs):
        for d, v in navs:
            self._exec("INSERT INTO paper_nav VALUES (?,?,?,?,?)", (d, v, 0.0, 0.0, None))
        self._exec("INSERT INTO paper_position VALUES ('600000.SH','A',1000,10.0,NULL)")
        self._exec("INSERT INTO paper_position VALUES ('000001.SZ','B',1000,10.0,NULL)")
        for code, shares in (("600000.SH", 2000), ("000001.SZ", 500), ("600002.SH", 1000)):
            self._exec("INSERT INTO plan VALUES ('20260914','stock',?,NULL,NULL,?,NULL,'t')",
                       (code, shares))

    def test_deep_drawdown_blocks_buys_but_not_sells(self):
        self._seed([("20260910", 100000.0), ("20260911", 74000.0)])   # 回撤 -26%
        n = engine.apply_plan("20260914")
        buys = self._one("SELECT COUNT(*) FROM paper_order WHERE side='buy'")[0]
        sells = self._one("SELECT COUNT(*) FROM paper_order WHERE side='sell'")[0]
        self.assertEqual(buys, 0, "谨慎态下一笔补仓买单都不许挂")
        self.assertEqual(sells, 1, "减仓卖出(000001.SZ 500 股)照常")
        self.assertEqual(n, 1)
        reason = self._one("SELECT reason FROM paper_order WHERE side='sell'")[0]
        self.assertEqual(reason, "月度方案减仓")

    def test_shallow_drawdown_still_buys(self):
        self._seed([("20260910", 100000.0), ("20260911", 99000.0)])   # -1%
        n = engine.apply_plan("20260914")
        buys = self._one("SELECT COUNT(*) FROM paper_order WHERE side='buy'")[0]
        self.assertEqual(n, 3)
        self.assertEqual(buys, 2)

    def test_disabled_governor_ignores_drawdown(self):
        self._seed([("20260910", 100000.0), ("20260911", 74000.0)])
        with mock.patch.object(
                engine.cfg_mod, "load_config",
                return_value={"single_side_cost": 0.003, "capital": 10000,
                              "paper": {"slip": 0.001, "governor": {"enabled": False}},
                              "fees": {"commission_rate": 0.003, "min_commission": 0.0,
                                       "stamp_tax": 0.0, "stamp_tax_before": 0.0,
                                       "transfer_fee": 0.0}}):
            n = engine.apply_plan("20260914")
        self.assertEqual(n, 3, "显式停用后深回撤也不闸")


class TestMonthlySignalGate(_ExecBase):
    """apply_signals 双闸: 治理器谨慎态 / defense 状态; 卖出路径一概不经过。"""

    def setUp(self):
        super().setUp()
        # 2026-09-30 起 signal 表进了 _paper_base._SCHEMA(apply_plan 查动态层卖出锁),
        # 这里改为幂等建表
        self._exec("CREATE TABLE IF NOT EXISTS signal (signal_date TEXT, ts_code TEXT, name TEXT, "
                   "action TEXT, reason TEXT)")
        self._exec("CREATE TABLE regime (trade_date TEXT PRIMARY KEY, state TEXT)")
        self._exec("UPDATE paper_account SET cash=100000.0 WHERE id=1")
        self._exec("INSERT INTO paper_position VALUES ('600000.SH','A',1000,10.0,NULL)")
        self._daily("600002.SH", "20260914", 10.0, 9.9)

    def _signal(self, action, code, name="TEST"):
        self._exec("INSERT INTO signal VALUES ('20260914',?,?,?, 't')", (code, name, action))

    def _regime(self, state):
        self._exec("INSERT INTO regime VALUES ('20260914',?)", (state,))

    def _navs(self, navs):
        for d, v in navs:
            self._exec("INSERT INTO paper_nav VALUES (?,?,?,?,?)", (d, v, 0.0, 0.0, None))

    def _sides(self):
        conn = self._c()
        try:
            return sorted(r[0] for r in
                          conn.execute("SELECT side FROM paper_order").fetchall())
        finally:
            conn.close()

    def test_defense_blocks_candidate_buy_but_not_sell(self):
        self._signal("sell", "600000.SH")
        self._signal("buy_candidate", "600002.SH")
        self._regime("defense")
        n = engine.apply_signals("20260914")
        self.assertEqual(n, 1)
        self.assertEqual(self._sides(), ["sell"], "defense 下候选只看不動: 卖出挂单、替换买入不做")

    def test_attack_allows_candidate_replacement_buy(self):
        self._signal("sell", "600000.SH")
        self._signal("buy_candidate", "600002.SH")
        self._regime("attack")
        n = engine.apply_signals("20260914")
        self.assertEqual(n, 2)
        self.assertEqual(self._sides(), ["buy", "sell"])

    def test_governor_blocks_all_buys_even_in_attack(self):
        self._navs([("20260910", 100000.0), ("20260911", 74000.0)])
        self._signal("sell", "600000.SH")
        self._signal("add", "600003.SH")
        self._signal("buy_candidate", "600002.SH")
        self._regime("attack")
        self._daily("600003.SH", "20260914", 10.0, 9.9)
        n = engine.apply_signals("20260914")
        self.assertEqual(self._sides(), ["sell"], "谨慎态下 add/候选替换买入全停")
        self.assertEqual(n, 1)

    def test_missing_regime_table_degrades_to_no_gate(self):
        self._exec("DROP TABLE regime")
        self._signal("sell", "600000.SH")
        self._signal("buy_candidate", "600002.SH")
        n = engine.apply_signals("20260914")
        self.assertEqual(self._sides(), ["buy", "sell"], "regime 表缺失只降级候选闸, 不阻断主流程")
        self.assertEqual(n, 2)


# ---------------------------------------------------------------- 袖套接线
class TestWeeklySlotGovernor(WeeklySlotTestCase):
    def _ledger_with_navs(self, navs):
        self.ledger.write_text(json.dumps({
            "capital": 100_000.0, "cash": 100_000.0, "positions": {},
            "nav_history": [{"date": d, "nav": v} for d, v in navs],
            "trades": [], "inception": navs[0][0], "last_mark": navs[-1][0]}),
            encoding="utf-8")

    def _run(self, days=None):
        with mock.patch("app.datasource.quote_client.get_quotes",
                        return_value={"600000.SH": {"open": 10.0, "prev_close": 10.0}}):
            return self._open(days or [TODAY, ANCHOR])

    def _open(self, days):
        from app.weekly import pipeline as wk_pipe
        return wk_pipe.run_open_slot(trade_date=TODAY, cfg=self.cfg,
                                     ledger_path=self.ledger,
                                     open_days_fn=lambda _t: days)

    def test_deep_drawdown_blocks_buys(self):
        self.write_plan([{"ts_code": "600000.SH", "shares": 1000}])
        self._ledger_with_navs([("20260901", 100_000.0), ("20260911", 74_000.0)])
        out = self._run()
        self.assertEqual(out["trades"], 0, "谨慎态下周度重建仓零买入")
        self.assertTrue(out["governor"], "返回摘要必须带治理 reason")
        st = self.ledger_state()
        self.assertTrue(st["governor"]["cautious"], "判定副本随 execute_plan 的 save() 落盘")

    def test_shallow_drawdown_still_buys(self):
        self.write_plan([{"ts_code": "600000.SH", "shares": 1000}])
        self._ledger_with_navs([("20260901", 100_000.0), ("20260911", 99_000.0)])
        out = self._run()
        self.assertEqual(out["trades"], 1)
        self.assertIsNone(out["governor"])

    def test_exits_continue_while_cautious(self):
        """谨慎态语义的核心: 停的是**开新仓**, 退出(调出)照旧 —— 敞口只降不升。"""
        self.write_plan([{"ts_code": "600000.SH", "shares": 1000},
                         {"ts_code": "600002.SH", "shares": 1000}])
        self.ledger.write_text(json.dumps({
            "capital": 100_000.0, "cash": 20_000.0,
            "positions": {"600003.SH": {"shares": 1000.0, "cost": 10.0, "last_px": 10.0}},
            "nav_history": [{"date": "20260901", "nav": 100_000.0},
                            {"date": "20260911", "nav": 74_000.0}],
            "trades": [], "inception": "20260901", "last_mark": "20260911"}),
            encoding="utf-8")
        with mock.patch("app.datasource.quote_client.get_quotes",
                        return_value={"600000.SH": {"open": 10.0, "prev_close": 10.0},
                                      "600002.SH": {"open": 10.0, "prev_close": 10.0},
                                      "600003.SH": {"open": 10.0, "prev_close": 10.0}}):
            out = self._open([TODAY, ANCHOR])
        st = self.ledger_state()
        buys = [t for t in st["trades"] if t["side"] == "buy"]
        sells = [t for t in st["trades"] if t["side"] == "sell"]
        self.assertEqual(buys, [], "谨慎态零买入")
        self.assertEqual([s["ts_code"] for s in sells], ["600003.SH"], "调出卖出照常")


class TestHealthSlotGovernorAlert(unittest.TestCase):
    """健康档: 谨慎态触发⇄解除翻转时落痕并告警(触发方向)。"""

    def _run_slot(self, led, alerts):
        from app import config as cfg_mod
        from app import scheduler
        with mock.patch.object(cfg_mod, "load_config",
                               return_value={"weekly": {"enabled": True}}), \
                mock.patch("app.weekly.health.current",
                           return_value={"level": "healthy"}), \
                mock.patch("app.weekly.risk.apply", return_value={}), \
                mock.patch("app.weekly.risk.decide", return_value={}), \
                mock.patch("app.weekly.ledger.Ledger.load", return_value=led), \
                mock.patch("app.weekly.ledger.backup_snapshots", return_value="x.json"), \
                mock.patch("app.weekly.doctor.check_exec_ran",
                           return_value={"ok": True, "detail": "非执行日"}), \
                mock.patch("app.weekly.reconcile.report", return_value={"available": False}), \
                mock.patch("app.weekly.attrib.refresh", return_value=None), \
                mock.patch.object(scheduler, "_ops_alert_once",
                                  side_effect=lambda k, t, b: alerts.append(k) or True):
            return scheduler._weekly_health_slot()

    def _fake_ledger(self, navs, prev_cautious=False):
        led = mock.Mock()
        led.state = {"governor": {"cautious": prev_cautious},
                     "nav_history": [{"date": d, "nav": v} for d, v in navs]}
        led.save.return_value = True
        return led

    def test_trip_transition_fires_one_alert(self):
        alerts = []
        led = self._fake_ledger([("20260910", 100_000.0), ("20260911", 74_000.0)])
        res = self._run_slot(led, alerts)
        self.assertEqual(alerts, ["weekly_governor_cautious"])
        self.assertTrue(res["governor"]["cautious"])
        self.assertTrue(led.state["governor"]["cautious"], "留痕副本被更新")
        led.save.assert_called()

    def test_no_alert_without_transition(self):
        alerts = []
        led = self._fake_ledger([("20260910", 100_000.0), ("20260911", 99_000.0)])
        res = self._run_slot(led, alerts)
        self.assertEqual(alerts, [])
        self.assertFalse(res["governor"]["cautious"])

    def test_mock_ledger_without_nav_history_does_not_break_the_slot(self):
        """test_weekly_slot_snapshot 用裸 Mock 账本 —— 治理块必须被 except 兜住,
        不带走健康读数(该档还有风控/看门狗/快照等动作)。"""
        alerts = []
        led = mock.Mock()          # state.get(...) 返回 Mock, nav_history 不可迭代
        res = self._run_slot(led, alerts)
        self.assertEqual(res.get("level"), "healthy")
        self.assertEqual(alerts, [])


if __name__ == "__main__":
    unittest.main()
