"""开盘执行档 `run_open_slot` 的守卫测试（2026-09-13）

为什么单测它：这是**每工作日 09:32** 那条档 —— 首个纸面周期（2026-09-14）就是它第一次真跑。
它此前只有"读日历/跳过"那几行被覆盖，**真正的成交核心（出方案 → 读实时行情 → 落账本）零覆盖**，
而它写的是生产账本、用的还是实时行情分支（`use_quotes=True`）。

守十条：
  1. `weekly.enabled=false` ⇒ 读日历前返回；
  2. 今天不是开市日 ⇒ 跳过；3. 日历不足两天 ⇒ 跳过；
  4. 今天与上一开市日同属一个 ISO 周 ⇒ 非执行日（周频决策点只在周首日执行）；
  5. 锚点无方案 ⇒ **现算**（注入 build_plan_fn 写文件）后继续执行；
  6. 现算抛异常 ⇒ 返回 `{"error": ...}`，**绝不向上抛**（不许炸断月频链）；
  7. 现算"成功"但没写出文件 ⇒ 同样报 error（不带着空方案去下单）；
  8. 有方案 ⇒ 用**实时行情今开价 ± 滑点**成交（与晚间档的日线价区分开）；
  9. 同日重跑 ⇒ `已执行过(幂等)`；
  10. 账本已停买 ⇒ 一笔不买。

全部用临时库 + 临时账本 + 注入 open_days/行情，**不碰生产文件也不联网**。

运行: python -m unittest tests.test_weekly_open_slot -v
"""
import json
import sys
from pathlib import Path
from unittest import mock

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from app.weekly import pipeline as wk_pipe                  # noqa: E402
from app.weekly import ledger as wk_ledger                  # noqa: E402
from tests._weekly_slot_base import ANCHOR, TODAY, WeeklySlotTestCase   # noqa: E402

QUOTES = {"600000.SH": {"open": 10.0, "prev_close": 10.0}}


class TestOpenSlot(WeeklySlotTestCase):
    def _run(self, days, today=TODAY, cfg=None, build_plan_fn=None, quotes=QUOTES):
        with mock.patch("app.datasource.quote_client.get_quotes", return_value=quotes):
            return wk_pipe.run_open_slot(trade_date=today, cfg=cfg or self.cfg,
                                         ledger_path=self.ledger,
                                         open_days_fn=lambda _t: days,
                                         build_plan_fn=build_plan_fn)

    # ---------------------------------------------------------------- 跳过类
    def test_disabled_returns_before_any_io(self):
        out = self._run([TODAY, ANCHOR], cfg={"weekly": {"enabled": False}})
        self.assertEqual(out, {"skipped": "weekly.enabled=false"})
        self.assertFalse(self.ledger.exists())

    def test_non_open_day_is_skipped(self):
        out = self._run([ANCHOR, "20260910"])
        self.assertIn("不是开市日", out["skipped"])

    def test_short_calendar_is_skipped(self):
        out = self._run([TODAY])
        self.assertIn("日历不足两天", out["skipped"])

    def test_same_iso_week_is_not_an_execution_day(self):
        """今天(周二)与上一开市日(周一)同属一个 ISO 周 ⇒ 不是周频执行日。"""
        out = self._run(["20260915", "20260914", ANCHOR], today="20260915")
        self.assertIn("非执行日", out["skipped"])

    # ---------------------------------------------------------------- 出方案
    def test_missing_plan_is_built_then_executed(self):
        def _build(asof=None, capital=None):        # 模拟 wk_plan.build 写方案
            self.assertEqual(asof, ANCHOR)
            self.write_plan([{"ts_code": "600000.SH", "shares": 1000}], capital=capital)
            return {"n_target": 1}

        out = self._run([TODAY, ANCHOR], build_plan_fn=_build)
        self.assertEqual(out["trades"], 1)
        self.assertEqual(out["anchor"], ANCHOR)

    def test_build_failure_is_reported_not_raised(self):
        def _boom(**_kw):
            raise RuntimeError("打分存档未覆盖该锚点")

        out = self._run([TODAY, ANCHOR], build_plan_fn=_boom)
        self.assertIn("error", out)
        self.assertIn("出方案失败", out["error"])

    def test_build_writes_nothing_still_reports_error(self):
        out = self._run([TODAY, ANCHOR], build_plan_fn=lambda **_kw: None)
        self.assertIn("出方案后仍无文件", out["error"])

    # ---------------------------------------------------------------- 成交
    def test_executes_at_realtime_open_with_slip(self):
        """开盘档走 `use_quotes=True`：成交价 = 今开×(1+滑点) = 10.01（晚间档是日线价 10.0）。"""
        self.write_plan([{"ts_code": "600000.SH", "shares": 1000}])
        out = self._run([TODAY, ANCHOR])
        self.assertEqual(out["trades"], 1)
        st = self.ledger_state()
        self.assertAlmostEqual(st["trades"][0]["price"], 10.01, places=3)
        self.assertEqual(st["positions"]["600000.SH"]["shares"], 1000)

    def test_second_run_same_day_is_skipped_as_done(self):
        self.write_plan([{"ts_code": "600000.SH", "shares": 1000}])
        self._run([TODAY, ANCHOR])
        again = self._run([TODAY, ANCHOR])
        self.assertIn("已执行过", again["skipped"])
        self.assertEqual(len(self.ledger_state()["trades"]), 1)

    def test_halted_ledger_blocks_buys(self):
        self.write_plan([{"ts_code": "600000.SH", "shares": 1000}])
        self.halt_ledger()
        out = self._run([TODAY, ANCHOR])
        self.assertEqual(out["trades"], 0)
        self.assertTrue(out["halted"])

    # ---------------------------------------------------------------- 空占位（07 #34 的现场）
    def test_empty_marker_from_premarket_catchup_does_not_block_execution(self):
        """**2026-09-14 生产现场复刻**：盘前补跑在账本里留下"当日空占位净值点"
        （0 成交 / 0 持仓），09:32 档**必须照常成交** —— 幂等判据 `_already_done` 只看 `trades`，
        **不看净值点**。这条要是红了，就意味着首个纸面周期会"看着已执行、实际一股没买"
        （医生看门狗在修好空占位判据之前也会跟着报 ✓，见 07 号 #34）。"""
        self.write_plan([{"ts_code": "600000.SH", "shares": 1000}])
        led = wk_ledger.Ledger.load(100_000.0, self.ledger)
        led.state["nav_history"] = [{"date": TODAY, "nav": 100_000.0, "invested": 0.0,
                                     "cash": 100_000.0, "interest": 0.0, "positions": {}}]
        led.state["last_plan"] = {"anchor": ANCHOR, "applied_on": TODAY,
                                  "n_rows": 1, "n_trades": 0}
        led.state["inception"] = TODAY
        led.state["last_mark"] = TODAY
        led.save()
        out = self._run([TODAY, ANCHOR])
        self.assertEqual(out["trades"], 1, "空占位净值点把 09:32 档挡掉了 ⇒ 首个周期会空仓")
        st = self.ledger_state()
        self.assertEqual(len(st["trades"]), 1)
        self.assertIn("600000.SH", st["positions"])


class TestEmptyPlanGuard(WeeklySlotTestCase):
    """2026-09-28 审查 P1: 空目标方案不得清空全部持仓(生产入口闸)。

    锚点日行情整日缺失时 plan.build 已在源头 raise 拒绝出空方案; 这里钉执行路径的
    兜底闸 —— 历史坏方案/手工方案 rows=[] 时, 原行为会把**全部持仓**按当日价
    "按规则"清空(卖出不受 risk_halt/governor 拦截), 台账全绿无从察觉。"""

    def _run(self, days, today=TODAY, cfg=None, build_plan_fn=None, quotes=QUOTES):
        with mock.patch("app.datasource.quote_client.get_quotes", return_value=quotes):
            return wk_pipe.run_open_slot(trade_date=today, cfg=cfg or self.cfg,
                                         ledger_path=self.ledger,
                                         open_days_fn=lambda _t: days,
                                         build_plan_fn=build_plan_fn)

    def test_empty_plan_with_positions_is_refused(self):
        self.ledger.write_text(json.dumps({
            "capital": 100_000.0, "cash": 20_000.0,
            "positions": {"600000.SH": {"shares": 1000.0, "cost": 10.0, "last_px": 10.0,
                                        "bought": "20260907"}},
            "nav_history": [], "trades": [], "inception": "20260907", "last_mark": None}),
            encoding="utf-8")
        self.write_plan([])                      # rows=[] 的坏方案
        out = self._run([TODAY, ANCHOR])
        self.assertIn("error", out)
        self.assertIn("空目标集", out["error"])
        st = self.ledger_state()
        self.assertIn("600000.SH", st["positions"], "持仓不得被空方案清掉")
        self.assertEqual(st.get("trades"), [])


if __name__ == "__main__":
    unittest.main()
