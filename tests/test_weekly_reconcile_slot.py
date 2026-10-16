"""19:45 档里的**成交核对接线**（2026-09-13）

`reconcile`（方案意图 vs 账本实际 vs 当日开盘价）此前只有 CLI，没人自动跑 —— 成交那天
"想买的没买到 / 成交价离谱 / 现金对不上"只能靠人敲命令才看得见。接进健康档后必须守住两条：

① **只对结构性问题告警**（资金恒等式不成立、成交价均值偏离 >50bps）；
   缺仓/方案外残留是一字板与停牌的**正常顺延**，报了就成狼来了 —— 那是"报警疲劳"的开始。
② 核对本身坏掉**不许**把健康档带崩（它只是附加信息）。
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from app import scheduler  # noqa: E402


def _rec(identity_ok=True, mean_bps=3.0, resid_bps=3.0, n_trades=10, missing=0,
         legacy=False):
    """`legacy=True` 造一份**没有残差字段**的旧核对结果（验证回退分支不静默吞掉告警）。"""
    prices = {"n": n_trades, "mean_bps": mean_bps, "median_bps": mean_bps,
              "max_abs_bps": abs(mean_bps) * 2, "assumed_slip_bps": 10.0}
    if not legacy:
        # 2026-09-19 审查 B3: 判据看的是残差(先按方向统一成逆风为正，再扣假设滑点)
        prices.update({"resid_mean_bps": resid_bps, "resid_p95_bps": abs(resid_bps) * 1.2})
    return {
        "available": True, "plan": "plan_20260911.json", "anchor": "20260911",
        "positions": {"n_target": 94, "n_ok": 94 - missing, "n_partial": 0,
                      "n_missing": missing, "n_extra": 0, "deviations": [], "extra": []},
        "prices": prices,
        "identity": {"n": 1, "cash": 1.0, "market_value": 2.0, "nav": 3.0,
                     "gap": 0.0 if identity_ok else 123.4, "ok": identity_ok},
    }


class TestReconcileInHealthSlot(unittest.TestCase):
    def _run(self, rec, report_raises=False):
        cfg = {"weekly": {"enabled": True, "email": False}}
        rep = mock.Mock(side_effect=RuntimeError("核对炸了")) if report_raises else \
            mock.Mock(return_value=rec)
        # 2026-09-18 审查 Q-15：核对告警从直调 `send_ops_alert` 改走 `_ops_alert_once`
        # （同 key 当天只发一次）—— 于是本类 3 条"某种坏法应当报警"的用例互相被去重，
        # 只有第一条能发出。这里按 `test_scheduler_ops_alert._AlertCase` 的既有做法
        # 每条用例前清标记：各条断言的强度不变（仍然要求"这个坏法 ⇒ 一次告警"），
        # 只是不再共享当天那个键。去重本身由
        # `test_scheduler_ops_alert.py` 专门钉，不在这里重复测。
        scheduler._ALERTED.clear()
        self.addCleanup(scheduler._ALERTED.clear)
        # 2026-09-22（D 案接线）：块级 IC 刷新同属"重活"，也要打桩 —— 它在**测试库**
        # （空 `factor` 表）上算出来的"0 个锚点"若真的去写盘，会把生产缓存覆盖成假读数。
        # 双重保险：缓存路径本身也按 `db.DATA_DIR` 现算（见 `health._block_cache_path`）。
        with mock.patch("app.config.load_config", return_value=cfg), \
                mock.patch("app.weekly.health.current", return_value={"level": "healthy"}), \
                mock.patch("app.weekly.risk.decide", return_value={}), \
                mock.patch("app.weekly.risk.apply", return_value={}), \
                mock.patch("app.weekly.ledger.Ledger.load", return_value=mock.Mock()), \
                mock.patch("app.weekly.doctor.check_exec_ran",
                           return_value={"ok": True, "detail": ""}), \
                mock.patch("app.weekly.attrib.refresh", return_value=None), \
                mock.patch("app.weekly.health.block_ic_refresh",
                           return_value={"available": False}), \
                mock.patch("app.weekly.reconcile.report", rep), \
                mock.patch("app.notify.send_ops_alert",
                           return_value=True) as p_alert:
            out = scheduler._weekly_health_slot()
        return out, p_alert

    def test_clean_reconcile_sends_no_alert(self):
        out, p_alert = self._run(_rec())
        p_alert.assert_not_called()
        self.assertTrue(out["reconcile"]["ok"])

    def test_cash_identity_failure_alerts(self):
        out, p_alert = self._run(_rec(identity_ok=False))
        self.assertFalse(out["reconcile"]["ok"])
        p_alert.assert_called_once()
        self.assertIn("周频成交核对异常", p_alert.call_args.args[0])
        self.assertIn("资金恒等式", p_alert.call_args.args[1])

    def test_real_identity_payload_can_alert(self):
        """红检（2026-09-14，07 #49）：本条用**真产出**（`reconcile.cash_identity`），不手写 `n`。

        改动前 `cash_identity` 不返回 `n` ⇒ 调度器的 `idt.get("n")` 恒为 None ⇒
        "资金恒等式不成立"这句告警**从来不可能触发**（而另一条测试手写了 `"n": 1`，
        所以一直绿着）。本条把"生产者→调度器"整条链钉住。
        """
        from app.weekly import reconcile as rc
        from app.weekly.ledger import Ledger
        led = Ledger(1_000.0)
        led.state["nav_history"] = [{"date": "20260911", "nav": 1_100.0, "invested": 0.0,
                                     "cash": 1_000.0, "interest": 0.0, "positions": {}}]
        ident = rc.cash_identity(led)                 # 真产出：现金 1000 vs 净值 1100
        self.assertFalse(ident["ok"], "夹具本身必须是坏的恒等式")
        rec = _rec()
        rec["identity"] = ident
        out, p_alert = self._run(rec)
        self.assertFalse(out["reconcile"]["ok"])
        p_alert.assert_called_once()

    def test_price_deviation_alerts(self):
        out, p_alert = self._run(_rec(resid_bps=184.0))
        self.assertFalse(out["reconcile"]["ok"])
        p_alert.assert_called_once()
        self.assertIn("184", p_alert.call_args.args[1])

    def test_adverse_fill_alerts_even_when_the_mean_is_zero(self):
        """**自动化路径的同一条 B3 证据**：均值被买卖抵消成 0，残差 184bps 仍必须告警。

        原实现判 `abs(mean_bps) > 50` ⇒ 这种账本**永远不会响**。
        """
        out, p_alert = self._run(_rec(mean_bps=0.0, resid_bps=184.0))
        self.assertFalse(out["reconcile"]["ok"], "残差 184bps 却判好 ⇒ 判据还挂在均值上")
        p_alert.assert_called_once()

    def test_legacy_payload_without_resid_still_alerts(self):
        """核对结果里没有残差字段（旧版本/外部 payload）⇒ 回退旧口径，**不许静默不告警**。"""
        out, p_alert = self._run(_rec(mean_bps=184.0, legacy=True))
        self.assertFalse(out["reconcile"]["ok"])
        p_alert.assert_called_once()
        self.assertIn("旧口径", p_alert.call_args.args[1])

    def test_missing_positions_do_not_alert(self):
        """一字板/停牌顺延是设计内行为 —— 报它就是狼来了。"""
        out, p_alert = self._run(_rec(missing=17))
        p_alert.assert_not_called()
        self.assertTrue(out["reconcile"]["ok"])

    def test_reconcile_crash_does_not_break_the_slot(self):
        out, p_alert = self._run(None, report_raises=True)
        p_alert.assert_not_called()
        self.assertEqual(out.get("level"), "healthy")


if __name__ == "__main__":
    unittest.main()
