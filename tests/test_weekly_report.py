"""周频报表出口 `app/weekly/report.py` 的守卫测试（2026-09-13）

为什么单测它：这个文件**覆盖率 0%**，而它是用户真正看到的东西 —— CLI 的 KPI 行、
**发到邮箱的健康告警/执行档看门狗告警**。它坏掉不会让交易出错，但会让"终端有没有在工作"
这件事**没人知道**（本项目最贵的一课就是"看着装好了其实不工作"）。
（袖套 KPI 那封邮件 2026-09-18 起已取消，改由 18:30 日终总结的「周频袖套」小节承载；
`kpi_lines` 仍是两边共用的呈现层，所以本文件继续守它。）

守五类（都是可观察行为，不测实现细节）：
  1. `kpi_lines` 该有的行都要有：资金/净值/现金/持仓/成交/计息、收益与回撤行、`note`、**停买告警行**；
  2. 空快照不许炸（`.get` 缺省）；
  3. 发信开关：`weekly.email=false` / `email.enabled=false` / 缺收件人 / 缺发件人 ⇒ **一律不发**；
  4. 真发时：三封邮件各自的标题要点 + 正文里**成交明细与原因列表必须 HTML 转义**（防注入/串版）；
  5. 健康告警的图标按 level 映射（dead→🔴、degrading→🟠、warning→🟡、其它→⚪）。

运行: python -m unittest tests.test_weekly_report -v
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from app.weekly import health as wk_health         # noqa: E402
from app.weekly import report as wk_report            # noqa: E402

SNAP = {"capital": 1_000_000.0, "inception": "20260914", "last_mark": "20260921",
        "nav": 1_012_345.0, "cash": 130_000.0, "n_positions": 98, "n_trades": 12,
        "interest": 123.45, "total_return": 0.0123, "ann_return": 0.18, "mdd": -0.07,
        "bench_ann": 0.08, "excess_pp": 10.0, "weeks": 3}


class TestKpiLines(unittest.TestCase):
    def test_core_line_has_every_counter(self):
        line = wk_report.kpi_lines(SNAP)[0]
        for token in ("袖套资金", "1,000,000", "起始 20260914", "最后盯市 20260921"):
            self.assertIn(token, line)
        second = wk_report.kpi_lines(SNAP)[1]
        for token in ("净值", "1,012,345", "现金", "130,000", "持仓 98 只", "累计成交 12 笔", "累计计息 ¥123.45"):
            self.assertIn(token, second)

    def test_return_and_drawdown_lines(self):
        lines = wk_report.kpi_lines(SNAP)
        self.assertTrue(any("累计收益 +1.23%" in x for x in lines))
        # 2026-09-21 同步: 文案改为"N 个自然周"并并排显示日点(此前"N 周"里的 N 其实是日点)
        self.assertTrue(any("年化 +18.00%" in x and "回撤 -7.00%" in x
                            and "基准年化 +8.00%" in x and "超额 +10.00pp" in x
                            and "已运行 3 个自然周" in x for x in lines))

    def test_note_and_halt_are_rendered(self):
        snap = dict(SNAP, note="净值点不足两期", halt={"active": True, "since": "20260910",
                                                  "reason": "健康度 dead"})
        lines = wk_report.kpi_lines(snap)
        self.assertTrue(any(x.startswith("注: 净值点不足两期") for x in lines))
        self.assertTrue(any("风控停买中" in x and "20260910" in x and "dead" in x
                            and "人工 resume" in x for x in lines))

    def test_empty_snapshot_does_not_crash(self):
        lines = wk_report.kpi_lines({})
        self.assertTrue(lines, "空快照也要给出至少一行(否则调用方拿不到任何输出)")
        self.assertNotIn("total_return", " ".join(lines))

    def test_halt_inactive_is_not_rendered(self):
        lines = wk_report.kpi_lines(dict(SNAP, halt={"active": False}))
        self.assertFalse(any("风控停买中" in x for x in lines))


class _MailBase(unittest.TestCase):
    """公共夹具：假的 config + 捕获 `_send_with_retry` 收到的邮件对象。"""

    def setUp(self):
        self.sent = []

        def _fake_send(cfg, msg, tag):
            self.sent.append((cfg, msg, tag))
            return True

        self._p = mock.patch.object(wk_report, "_send_with_retry", side_effect=_fake_send)
        self._p.start()
        self.addCleanup(self._p.stop)

    def _cfg(self, weekly_email=True, enabled=True, recipients=("a@b.c",), sender="s@b.c"):
        return {"weekly": {"email": weekly_email},
                "email": {"enabled": enabled, "recipients": list(recipients), "sender": sender,
                          "smtp_server": "smtp.example.com", "smtp_port": 465, "auth_code": "x"}}

    def _with_cfg(self, cfg):
        return mock.patch.object(wk_report.cfg_mod, "load_config", return_value=cfg)


class TestKpiEmailRemoved(unittest.TestCase):
    """袖套独立 KPI 邮件已取消（2026-09-18 邮件优化），读数改由日终总结承载。

    为什么留一条"它不存在"的测试而不是直接删掉测试：那封邮件的**内容不能丢**是这次
    改动的前提，`notify._sleeve_section` 的测试（tests/test_notify.py）钉的是"内容在"，
    这条钉的是"出口没有偷偷长回来" —— 两边各守一半，谁复发都会红。
    """

    def test_outlet_is_gone(self):
        self.assertFalse(hasattr(wk_report, "send_kpi_email"),
                         "袖套 KPI 邮件出口已于 2026-09-18 删除；要恢复请先想清楚"
                         "它和 18:30 日终总结的「周频袖套」小节为什么不能合并")

    def test_kpi_lines_still_available_for_cli_and_email(self):
        # `kpi_lines` 是 CLI(`run_weekly.py kpi`) 与日终总结袖套小节**共用**的呈现层
        self.assertTrue(callable(wk_report.kpi_lines))
        self.assertTrue(any("袖套资金" in x for x in wk_report.kpi_lines(SNAP)))


class TestSendHealthEmail(_MailBase):
    def test_level_icon_mapping(self):
        for level, icon in (("dead", "🔴"), ("degrading", "🟠"), ("warning", "🟡"), ("other", "⚪")):
            with self.subTest(level=level):
                self.sent.clear()
                with self._with_cfg(self._cfg()):
                    wk_report.send_health_email({"level": level, "reasons": ["换手异常"],
                                                 "rolling_ic": 0.01, "neg_streak": 7,
                                                 "turnover_est": 0.4, "deployed_ratio": 0.7})
                body = self.sent[0][1].get_payload(decode=True).decode("utf-8")
                self.assertIn(icon, body)
                self.assertIn("换手异常", body)

    def test_disabled_sends_nothing(self):
        with self._with_cfg(self._cfg(weekly_email=False)):
            self.assertFalse(wk_report.send_health_email({"level": "dead"}))
        self.assertEqual(self.sent, [])


class TestSendWatchdogEmail(_MailBase):
    def test_subject_says_the_execution_slot_did_not_run(self):
        item = {"detail": "账本里没有 20260914 的净值点", "fix": "检查调度与服务状态"}
        with self._with_cfg(self._cfg()):
            self.assertTrue(wk_report.send_watchdog_email(item))
        _cfg, msg, tag = self.sent[0]
        self.assertEqual(tag, "袖套看门狗")
        self.assertIn("执行档没跑", str(msg["Subject"]))
        body = msg.get_payload(decode=True).decode("utf-8")
        self.assertIn("账本里没有 20260914 的净值点", body)
        self.assertIn("检查调度与服务状态", body)

    def test_disabled_sends_nothing(self):
        with self._with_cfg(self._cfg(weekly_email=False)):
            self.assertFalse(wk_report.send_watchdog_email({"detail": "x"}))
        self.assertEqual(self.sent, [])


class TestHealthEmailEdges(unittest.TestCase):
    """2026-09-18 邮件优化：健康告警改边沿触发后，邮件本身要能表达"为什么又发/为什么不发"。

    边沿触发的最大风险是**读者误解沉默**："连着三天没收到告警" 既可能是"好了"，
    也可能是"还在坏但按规则不该重复发"。所以正文必须自己说清楚这一点。
    """

    _RES = {"level": "degrading", "reasons": ["换手 0.5 为回测的 2~3 倍"], "rolling_ic": -0.01,
            "neg_streak": 6, "turnover_est": 0.5, "deployed_ratio": 0.55,
            "frozen_ic": 0.0773, "ic_n": 12}

    def _body(self, kind):
        holder = []
        with mock.patch.object(wk_report.cfg_mod, "load_config",
                               return_value={"weekly": {"email": True},
                                             "email": {"enabled": True, "sender": "s@b.c",
                                                       "recipients": ["a@b.c"],
                                                       "smtp_server": "x", "smtp_port": 465,
                                                       "auth_code": "y"}}), \
                mock.patch.object(wk_report, "_send_with_retry",
                                  side_effect=lambda cfg, msg, tag: holder.append(msg) or True):
            self.assertTrue(wk_report.send_health_email(dict(self._RES), kind=kind))
        return holder[0], holder[0].get_payload(decode=True).decode("utf-8")

    def test_threshold_column_is_rendered_from_health_constants(self):
        _msg, body = self._body("alert")
        self.assertIn("判据 / 阈值", body)
        # 阈值来自 health.py 的常量，不手抄：改常量这里就会红
        self.assertIn(f"{2 * wk_health.BACKTEST_TURNOVER:.3f}", body)
        self.assertIn("0.0773", body)
        self.assertIn("≥ 0.60 健康", body)

    def test_silence_is_explained(self):
        _msg, body = self._body("alert")
        self.assertIn("没有新邮件不代表没在退化", body)

    def test_repeat_kind_says_it_is_not_a_new_degradation(self):
        _msg, body = self._body("repeat")
        self.assertIn("同一档重复提醒", body)
        self.assertIn(f"{wk_health.REPEAT_DAYS} 天", body)

    def test_recovered_kind_closes_the_loop(self):
        msg, body = self._body("recovered")
        self.assertIn("已恢复", body)
        # 2026-09-28 auto_resume: CLI 路径(无 auto_resume 键)走"人工 resume"分支;
        # 调度档路径(带键且已解除)则必须明说"已自动解除"—— 两条口径都要有 resume 字样。
        self.assertIn("resume", body, "恢复邮件必须交代停买去向(已自动解除/人工 resume)")
        self.assertNotIn("判据退化", body)
        self.assertIn("已恢复", str(msg["Subject"]))

    def test_recovered_email_announces_auto_resume_when_cleared(self):
        """调度档在发信前已跑 maybe_auto_resume ⇒ 恢复信必须说'已自动解除'，不许再说
        '不会自动解除'的假话（2026-09-28 改口径的守卫）。"""
        holder = []
        res = dict(self._RES, level="healthy", auto_resume={"changed": True, "saved": True})
        with mock.patch.object(wk_report.cfg_mod, "load_config",
                               return_value={"weekly": {"email": True},
                                             "email": {"enabled": True, "sender": "s@b.c",
                                                       "recipients": ["a@b.c"],
                                                       "smtp_server": "x", "smtp_port": 465,
                                                       "auth_code": "y"}}), \
                mock.patch.object(wk_report, "_send_with_retry",
                                  side_effect=lambda cfg, msg, tag: holder.append(msg) or True):
            self.assertTrue(wk_report.send_health_email(res, kind="recovered"))
        body = holder[0].get_payload(decode=True).decode("utf-8")
        self.assertIn("自动解除", body)
        self.assertNotIn("不会自动解除", body)

    def test_no_markdown_leaks_into_html(self):
        # 2026-09-18 实测：看门狗邮件标题里的 `**执行档没跑**` 以字面星号显示
        for kind in ("alert", "repeat", "recovered"):
            with self.subTest(kind=kind):
                self.assertNotIn("**", self._body(kind)[1])


class TestHealthThresholdRows(unittest.TestCase):
    """`health_threshold_rows` 是**纯函数**（2026-09-24 从 `send_health_email` 抽出）。

    守的是一条**呈现纪律**：换手那一行必须**并排显示样本量**。
    起因（07 号 #63）：账本只有 1 个卖出日时，`turnover_est=0.316` 会被读成
    "袖套长期换手偏高"，而真相是"**那一次调仓**换掉了 31/98 的名字"
    ⇒ 判据的**样本量前提不写出来，读者会照错的前提去做决策**。
    （抽成纯函数就是为了让这条纪律**可测** —— 埋在发信函数里测不了。）
    """

    def _turnover_label(self, res):
        rows = wk_report.health_threshold_rows(res)
        self.assertEqual(len(rows), 4, "阈值表应恒为 4 行")
        return [n for n, _, _ in rows if "换手" in n][0]

    def test_sample_size_shown_and_flagged_when_insufficient(self):
        label = self._turnover_label(
            {"ic_n": 12, "turnover_est": 0.3163, "turnover_n": 1, "turnover_min_days": 4})
        self.assertIn("n=1", label)
        self.assertIn("不判停买", label, "样本不足必须显式说明不会触发停买")

    def test_sample_size_shown_without_the_flag_when_sufficient(self):
        label = self._turnover_label(
            {"ic_n": 12, "turnover_est": 0.137, "turnover_n": 12, "turnover_min_days": 4})
        self.assertIn("n=12", label)
        self.assertNotIn("不判停买", label)

    def test_missing_sample_size_keeps_old_label(self):
        """旧读数/旧调用方没有 `turnover_n` ⇒ 标签退回原样，不炸（向后兼容）。"""
        label = self._turnover_label({"ic_n": 12, "turnover_est": 0.2})
        self.assertNotIn("样本", label)

    def test_threshold_text_still_comes_from_health_constants(self):
        """阈值文案仍必须来自 `health` 的常量（不许写死数字）。"""
        rows = wk_report.health_threshold_rows({"ic_n": 12})
        txt = " ".join(s for _, _, s in rows)
        self.assertIn(f"{wk_health.BACKTEST_TURNOVER}", txt)
        self.assertIn(f"{wk_health.FROZEN_IC}", txt)


if __name__ == "__main__":
    unittest.main()
