"""风控自动动作的守卫（规划 §6.1-4：阈值触发时自动降仓或停止）

守五条:
  1. 只有 degrading/dead 触发（healthy/warning/unknown 不触发）；
  2. 开关 `weekly.risk_halt.enabled=false` ⇒ 只报警不动作；
  3. 触发写进账本且**幂等**（重复 apply 不重复记）；
  4. **解除**（2026-09-28 改）：auto_resume 开启时恢复 healthy 自动解除（滞回，
     warning 不解除）；关闭时仍要人工 resume —— 两种口径都被钉死；
  5. 停买态下 execute_plan **只卖不买**（已有持仓照方案卖出、一笔新仓都不开）。
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.weekly import risk as wk_risk       # noqa: E402
from app.weekly.ledger import Ledger         # noqa: E402


class TestHaltDecision(unittest.TestCase):
    def test_only_degrading_and_dead_trigger(self):
        cfg = {}
        self.assertFalse(wk_risk.decide({"level": "healthy", "neg_streak": 0}, cfg)["halt"])
        self.assertFalse(wk_risk.decide({"level": "warning", "neg_streak": 3}, cfg)["halt"])
        self.assertFalse(wk_risk.decide({"level": "unknown"}, cfg)["halt"])
        self.assertTrue(wk_risk.decide({"level": "degrading", "neg_streak": 6}, cfg)["halt"])
        self.assertTrue(wk_risk.decide({"level": "dead", "neg_streak": 12}, cfg)["halt"])

    def test_switch_off_means_alarm_only(self):
        cfg = {"weekly": {"risk_halt": {"enabled": False}}}
        self.assertFalse(wk_risk.decide({"level": "dead", "neg_streak": 12}, cfg)["halt"])

    def test_coupling_with_health_thresholds(self):
        """跨模块契约（2026-09-14）：**risk 只读 health 的 `level`**，而
        "连续为负 ≥6 个锚点就停买"这条规则，是靠 `health.evaluate` 把 `neg_streak` 映射成
        `degrading` 实现的 —— 改 health 的阈值会**静默**改风控触发线（触发即自动停买、
        且不自动解除，要人工 resume）。所以这里用**真 evaluate**（不是手写字典）把两端钉住。
        红检：把 health 的 6 改成 7 ⇒ 本用例当场红。
        """
        from app.weekly import health as wk_health
        h5 = wk_health.evaluate(wk_health.FROZEN_IC, 5, 0.10, 0.75)
        self.assertEqual(h5["level"], "healthy")
        self.assertFalse(wk_risk.decide(h5, {})["halt"], "5 个负锚点不许触发停买")
        h6 = wk_health.evaluate(wk_health.FROZEN_IC, 6, 0.10, 0.75)
        self.assertEqual(h6["level"], "degrading")
        self.assertTrue(wk_risk.decide(h6, {})["halt"], "6 个负锚点必须触发停买")
        h12 = wk_health.evaluate(wk_health.FROZEN_IC, 12, 0.10, 0.75)
        self.assertEqual(h12["level"], "dead")
        self.assertTrue(wk_risk.decide(h12, {})["halt"])


class TestHaltLedger(unittest.TestCase):
    def setUp(self):
        self.path = Path(__file__).resolve().parent.parent / "data" / "weekly" / "_test_risk.json"
        if self.path.exists():
            self.path.unlink()
        self.led = Ledger(100_000.0, self.path)

    def tearDown(self):
        if self.path.exists():
            self.path.unlink()

    def test_apply_is_idempotent_and_not_auto_cleared(self):
        dec = {"halt": True, "reason": "健康度 dead"}
        first = wk_risk.apply(self.led, dec, "20261101")
        self.assertTrue(first["changed"])
        self.assertIsNotNone(wk_risk.is_halted(self.led))
        second = wk_risk.apply(self.led, dec, "20261108")
        self.assertFalse(second["changed"], "重复触发不应重复记账")
        self.assertEqual(self.led.state["halt"]["since"], "20261101")
        # 健康恢复也不许自动解除(§6.1-3 不允许降级/人工放行)
        back = wk_risk.apply(self.led, {"halt": False, "reason": "健康度 healthy"}, "20261115")
        self.assertFalse(back["changed"])
        self.assertIsNotNone(wk_risk.is_halted(self.led))
        # 人工解除才行
        cleared = wk_risk.clear(self.led, "20261116", "人工确认信号已恢复")
        self.assertFalse(cleared["active"])
        self.assertIsNone(wk_risk.is_halted(self.led))

    def test_clear_on_non_halted_does_not_create_ledger(self):
        """2026-09-28 毒账本守卫: 未停买时 clear() 必须短路不落账 —— 原实现在账本文件
        不存在的场景(run_weekly resume 的 Ledger.load(0.0))会写出 capital=0 的毒账本,
        此后执行档永远按 0 资金运行 ⇒ 袖套永久空仓。"""
        fresh = Ledger(0.0, self.path)           # 模拟无账本时的 load(0.0)
        self.assertIsNone(wk_risk.is_halted(fresh))
        out = wk_risk.clear(fresh, "20261116", "演练")
        self.assertFalse(out["changed"])
        self.assertFalse(self.path.exists(), "未停买的 clear 不得凭空创建账本文件")


class TestAutoResume(unittest.TestCase):
    """自动解除（2026-09-28 用户拍板"改成自动解除"）的守卫，四条：

    a. auto_resume 开 + 停买中 + healthy ⇒ 解除**落账**，halt 记录带 auto=True 留痕；
    b. 滞回：触发要 degrading/dead，解除只认 healthy —— warning/degrading/unknown 都不解；
    c. 开关关（DEFAULT_CONFIG 缺省）⇒ 永不自动解除（旧"只人工"口径不静默漂移）；
    d. 未停买时纯 no-op、不落账（与 clear 的毒账本守卫同源）。
    """

    def setUp(self):
        self.path = Path(__file__).resolve().parent.parent / "data" / "weekly" / "_test_risk3.json"
        if self.path.exists():
            self.path.unlink()
        self.led = Ledger(100_000.0, self.path)
        self.led.state["halt"] = {"active": True, "since": "20261101",
                                  "reason": "健康度 degrading(连续 6 个锚点滚动 IC<0)"}
        self.assertTrue(wk_risk.is_halted(self.led))

    def tearDown(self):
        if self.path.exists():
            self.path.unlink()

    def test_releases_on_healthy_and_leaves_audit(self):
        cfg = {"weekly": {"risk_halt": {"enabled": True, "auto_resume": True}}}
        out = wk_risk.maybe_auto_resume(self.led, {"level": "healthy"}, cfg, "20261108")
        self.assertTrue(out["changed"])
        self.assertTrue(out.get("saved"), "解除必须真的落账")
        self.assertIsNone(wk_risk.is_halted(self.led))
        rec = self.led.state["halt"]
        self.assertFalse(rec["active"])
        self.assertEqual(rec["cleared"], "20261108")
        self.assertTrue(rec.get("auto"), "自动解除必须在账本留痕, 与人工 resume 可区分")
        self.assertIn("自动解除", rec["reason"])

    def test_hysteresis_only_healthy_releases(self):
        cfg = {"weekly": {"risk_halt": {"auto_resume": True}}}
        for level in ("warning", "degrading", "dead", "unknown", None):
            out = wk_risk.maybe_auto_resume(self.led, {"level": level}, cfg, "20261108")
            self.assertFalse(out["changed"], f"{level} 不许解除停买(滞回)")
            self.assertIsNotNone(wk_risk.is_halted(self.led))

    def test_switch_off_keeps_manual_only(self):
        out = wk_risk.maybe_auto_resume(self.led, {"level": "healthy"}, {}, "20261108")
        self.assertFalse(out["changed"], "缺省(未开 auto_resume)必须维持只人工口径")
        self.assertIsNotNone(wk_risk.is_halted(self.led))

    def test_noop_when_not_halted(self):
        cfg = {"weekly": {"risk_halt": {"auto_resume": True}}}
        self.led.state["halt"] = {"active": False}
        out = wk_risk.maybe_auto_resume(self.led, {"level": "healthy"}, cfg, "20261108")
        self.assertFalse(out["changed"])
        self.assertFalse(self.path.exists(), "未停买时不得凭空创建账本文件")


class TestHaltedPlanTradesSellOnly(unittest.TestCase):
    def setUp(self):
        self.path = Path(__file__).resolve().parent.parent / "data" / "weekly" / "_test_risk2.json"
        if self.path.exists():
            self.path.unlink()
        self.led = Ledger(100_000.0, self.path)
        # 手上: A 在目标里(留存)、B 不在目标里(该卖)
        self.led.state["cash"] = 20_000.0        # 卖出后要够钱买 600002(否则是"现金不足顺延"而非停买)
        self.led.state["positions"] = {
            "600000.SH": {"shares": 1000.0, "cost": 10.0, "last_px": 10.0},
            "000001.SZ": {"shares": 1000.0, "cost": 10.0, "last_px": 10.0}}
        self.plan = {"rows": [{"ts_code": "600000.SH", "shares": 1000},
                              {"ts_code": "600002.SH", "shares": 1000}]}
        self.prices = {"600000.SH": 10.0, "000001.SZ": 10.0, "600002.SH": 10.0}

    def tearDown(self):
        if self.path.exists():
            self.path.unlink()

    def _fill(self, code, date, side):
        return self.prices.get(code)

    def test_allow_buys_false_sells_but_never_buys(self):
        done = self.led.execute_plan(self.plan, "20261102", fill_fn=self._fill, allow_buys=False)
        sides = [d["side"] for d in done]
        self.assertIn("sell", sides, "调出的持仓必须照方案卖出")
        self.assertNotIn("buy", sides, "停买态下一笔新仓都不许开")
        self.assertNotIn("600002.SH", self.led.state["positions"])
        self.assertIn("600000.SH", self.led.state["positions"], "留存持仓不动")

    def test_default_still_buys(self):
        done = self.led.execute_plan(self.plan, "20261102", fill_fn=self._fill)
        self.assertIn("buy", [d["side"] for d in done])


if __name__ == "__main__":
    unittest.main()
