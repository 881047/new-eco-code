"""晚间兜底档 `run_evening_slot` 的守卫测试（2026-09-13）

为什么单测它：它是**晚间 18:05** 那条档（用当日已入库日线开盘价补齐开盘档没成交的部分），
2026-09-14 首次运行；而在此之前 `app/weekly/pipeline.py` 里它**零覆盖**（只有 `run_open_slot`
与几个纯函数被覆盖）。它写的是**生产账本**，任何一条路径错都会直接落到钱上。

守八条：
  1. `weekly.enabled=false` ⇒ 在读日历之前就返回（零副作用）；
  2. 今天不是开市日 ⇒ 跳过；
  3. 本周还没有已完结锚点（days 全落在同一 ISO 周）⇒ 跳过、不建仓；
  4. 锚点存在但没有方案文件 ⇒ **先尝试现建**（2026-09-14 起，07 #50），**建不出来才跳过**，
     且跳过时不许建账本；现建成功则按当日开盘价成交（as-if 记账）；
  5. 正常路径：按方案差额成交（本测试用注入的固定价），返回 anchor/trades；
  6. **幂等**：同一天再跑一次 ⇒ 差额为 0、不产生重复成交；
  7. 账本已触发**风控停买** ⇒ 一笔不买（`allow_buys=False`），并把 halted 传回调用方。

全部用临时库 + 临时账本 + 注入 open_days/成交价，**不碰生产文件也不联网**。

运行: python -m unittest tests.test_weekly_evening_slot -v
"""
import sys
from pathlib import Path
from unittest import mock

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from app.weekly import pipeline as wk_pipe                  # noqa: E402
from app.weekly import ledger as wk_ledger                  # noqa: E402
from tests._weekly_slot_base import ANCHOR, TODAY, WeeklySlotTestCase   # noqa: E402


class TestEveningSlot(WeeklySlotTestCase):

    def _run(self, days, today=TODAY, cfg=None, build_plan_fn=None, train_fn=None):
        return wk_pipe.run_evening_slot(trade_date=today, cfg=cfg or self.cfg,
                                        ledger_path=self.ledger,
                                        open_days_fn=lambda _t: days,
                                        build_plan_fn=build_plan_fn, train_fn=train_fn)

    # ---------------------------------------------------------------- 跳过类
    def test_disabled_returns_before_any_io(self):
        out = self._run([TODAY, ANCHOR], cfg={"weekly": {"enabled": False}})
        self.assertEqual(out, {"skipped": "weekly.enabled=false"})
        self.assertFalse(self.ledger.exists(), "停用时不该创建账本")

    def test_non_open_day_is_skipped(self):
        out = self._run(["20260915", ANCHOR])
        self.assertIn("不是开市日", out["skipped"])

    def test_no_finished_anchor_yet_is_skipped(self):
        out = self._run([TODAY, "20260915".replace("15", "14")])
        self.assertIn("本周尚无锚点", out["skipped"])

    def test_missing_plan_and_failed_build_is_skipped(self):
        """2026-09-14（07 #50）改写：缺方案时**先尝试现建**，建不出来才跳过。

        原断言是"锚点存在但没有方案文件 ⇒ 跳过（不许凭分数现建仓）"；新版把"现建"这一步
        纳入（否则 09:32 一档失败就整周不可恢复）。这条用例保留原断言的精神：
        **建不出来 ⇒ 跳过，且不许碰账本**。注入的建造器模拟"没有打分存档"。
        """
        def _boom(**_kw):
            raise RuntimeError("打分存档未覆盖该锚点")
        out = self._run([TODAY, ANCHOR], build_plan_fn=_boom)
        self.assertIn("无方案", out["skipped"])
        self.assertIn("打分存档", out["skipped"])
        self.assertFalse(self.ledger.exists(), "建不出方案时不许建账本")

    def test_missing_plan_is_built_inline_then_executed(self):
        """链式自愈（07 #50）：方案缺失 + 打得开分 ⇒ 晚间档**自己建**并按当日开盘价成交。

        校验的是"服务周一白天不在线、18:05 才起来"那条路径：锚点严格早于本周、输入 ≤ 锚点，
        故非前视；按当日开盘价入账属 **as-if 记账**（与 18:05 档原语义一致）。
        """
        def _build(asof=None, capital=None, **_kw):
            self.write_plan([{"ts_code": "600000.SH", "shares": 1000}], anchor=asof)
        out = self._run([TODAY, ANCHOR], build_plan_fn=_build)
        self.assertEqual(out["anchor"], ANCHOR)
        self.assertEqual(out["trades"], 1)
        st = self.ledger_state()
        self.assertEqual(st["positions"]["600000.SH"]["shares"], 1000)

    # ---------------------------------------------------------------- 正常路径
    def test_executes_plan_and_reports(self):
        self.write_plan([{"ts_code": "600000.SH", "shares": 1000}])
        out = self._run([TODAY, ANCHOR])
        self.assertEqual(out["anchor"], ANCHOR)
        self.assertEqual(out["trades"], 1)
        self.assertEqual(out["halted"], False)
        st = self.ledger_state()
        self.assertEqual(st["positions"]["600000.SH"]["shares"], 1000)
        self.assertAlmostEqual(st["cash"], 100_000.0 - 10_000.0 - 5.1, places=2)  # 含 5 元最低佣金

    def test_second_run_same_day_is_idempotent(self):
        self.write_plan([{"ts_code": "600000.SH", "shares": 1000}])
        self._run([TODAY, ANCHOR])
        again = self._run([TODAY, ANCHOR])
        self.assertEqual(again["trades"], 0, "同一天重跑不该产生重复成交")
        st = self.ledger_state()
        self.assertEqual(len(st["trades"]), 1)

    # ---------------------------------------------------------------- 风控停买
    def test_halted_ledger_blocks_buys(self):
        self.write_plan([{"ts_code": "600000.SH", "shares": 1000}])
        self.halt_ledger()
        out = self._run([TODAY, ANCHOR])
        self.assertEqual(out["trades"], 0, "停买状态下不许买入")
        self.assertTrue(out["halted"])
        self.assertIn("dead", out["halt_reason"])

    # ---------------------------------------------------------------- 空占位（07 #34 的现场）
    def test_empty_marker_does_not_block_evening_fill(self):
        """**2026-09-14 生产现场复刻**：账本里已有"当日空占位净值点"（0 成交 / 0 持仓）时，
        18:05 晚间兜底档**必须照常补齐**（幂等判据只看 `trades`，不看净值点）。
        这条是首个纸面周期的兜底路径：09:32 若没跑成，就靠它按**当日开盘价**成交。"""
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
        self.assertEqual(out["trades"], 1, "空占位把晚间兜底档挡掉了 ⇒ 首个周期会空仓")

    def test_missing_price_leaves_an_empty_marker(self):
        """**把危险行为钉住**：当日行情还没入库时跑晚间档 ⇒ 一笔没成**却照样落净值点**
        （`execute_plan` 的口径：成交 0 笔也 mark）。这就是 07 #34 的成因，因此：
        ① 调度器的开机补跑加了 **18:05 闸**（盘前不再跑它）；② 医生的看门狗认"空占位"并判 ✗。
        哪天有人把这两道守卫拆了，这条测试会提醒他代价是什么。"""
        self.write_plan([{"ts_code": "600000.SH", "shares": 1000}])
        with mock.patch("app.weekly.ledger._exec_price", return_value=None):
            out = self._run([TODAY, ANCHOR])
        self.assertEqual(out["trades"], 0)
        st = self.ledger_state()
        self.assertEqual(st["trades"], [])
        self.assertEqual(st["positions"], {})
        self.assertEqual(st["last_plan"]["applied_on"], TODAY)
        self.assertEqual(st["last_plan"]["n_trades"], 0)
        self.assertTrue(any(p["date"] == TODAY for p in st["nav_history"]),
                        "空占位净值点是 07 #34 的成因：它在，看门狗的证据就可能被伪造")


if __name__ == "__main__":
    unittest.main()
