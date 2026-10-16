"""邮件函数回归测试: 变量遮蔽 P0 修复 (2026-09-05)

事故: send_signal_email/send_paper_email 内局部变量 html 遮蔽顶部 import html 模块,
esc = html.escape 抛 UnboundLocalError —— 邮件启用时成交/信号邮件必崩(日志 9/5 00:34)。
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import notify


def _cfg():
    return {"email": {"enabled": True, "smtp_server": "smtp.qq.com", "smtp_port": 465,
                      "sender": "a@qq.com", "auth_code": "x", "recipients": ["b@qq.com"]}}


class TestNotifyEmails(unittest.TestCase):
    def test_signal_email_builds_without_shadowing(self):
        sig = {"trade_date": "20260904", "state": "neutral", "items": [
            {"action": "sell", "name": "测试股", "ts_code": "600000.SH", "reason": "止损"}]}
        with mock.patch.object(notify.cfg_mod, "load_config", return_value=_cfg()), \
                mock.patch.object(notify, "_send_with_retry", return_value=True) as send, \
                mock.patch.object(notify, "_lhb_block", return_value=""):
            self.assertTrue(notify.send_signal_email(sig))
        self.assertEqual(send.call_count, 1)  # 未崩且正常走发送

    def test_paper_email_builds_without_shadowing(self):
        res = {"trade_date": "20260904", "trades": [
            {"side": "buy", "name": "测试股", "ts_code": "600000.SH", "shares": 100,
             "price": 10.0, "reason": "月度方案买入"}]}
        with mock.patch.object(notify.cfg_mod, "load_config", return_value=_cfg()), \
                mock.patch.object(notify, "_send_with_retry", return_value=True) as send:
            self.assertTrue(notify.send_paper_email(res))
        self.assertEqual(send.call_count, 1)

    def test_plan_email_still_works(self):
        summary = {"plan_date": "20260831", "state": "defense", "equity_weight": 0.8,
                   "capital": 10000, "data_date": "20260904",
                   "stocks": [], "stock_cost": 0, "cash": 8000,
                   "repo": {"amount": 8000, "desc": "x", "rate": 1.5},
                   "etfs": [], "otc": {}, "warning": None}
        # 2026-09-10: 方案邮件新增风格块(读库) —— 与本文件其余测试同法 patch 掉保持纯单测
        with mock.patch.object(notify.cfg_mod, "load_config", return_value=_cfg()), \
                mock.patch.object(notify, "_style_block", return_value=""), \
                mock.patch.object(notify, "_send_with_retry", return_value=True) as send:
            self.assertTrue(notify.send_plan_email(summary))
        self.assertEqual(send.call_count, 1)

    def test_disabled_email_returns_false_without_crash(self):
        cfg = {"email": {"enabled": False, "recipients": [], "sender": ""}}
        with mock.patch.object(notify.cfg_mod, "load_config", return_value=cfg):
            self.assertFalse(notify.send_signal_email({"trade_date": "x", "items": []}))

    def test_daily_summary_email_builds(self):
        s = {"trade_date": "20260907", "nav": 10004.0, "cash": 8996.0,
             "market_value": 1000.0, "interest": 8.0, "bench_nav": 5080.0,
             "positions": [{"name": "测试股", "ts_code": "600000.SH", "shares": 100,
                            "avg_cost": 10.0, "close": 10.2, "pnl": 71.0, "pnl_pct": 7.1}],
             "trades": [{"name": "测试股", "ts_code": "600000.SH", "side": "buy",
                         "shares": 100, "price": 10.01, "reason": "月度方案买入"}]}
        with mock.patch.object(notify.cfg_mod, "load_config", return_value=_cfg()), \
                mock.patch.object(notify, "_send_with_retry", return_value=True) as send:
            self.assertTrue(notify.send_daily_summary_email(s))
        self.assertEqual(send.call_count, 1)

    def test_daily_summary_email_empty_positions_and_no_bench(self):
        # 空仓/无成交/基准缺失(None)也要能构建发送
        s = {"trade_date": "20260907", "nav": 10000.0, "cash": 10000.0,
             "market_value": 0.0, "interest": 0.0, "bench_nav": None,
             "positions": [], "trades": []}
        with mock.patch.object(notify.cfg_mod, "load_config", return_value=_cfg()), \
                mock.patch.object(notify, "_send_with_retry", return_value=True) as send:
            self.assertTrue(notify.send_daily_summary_email(s))
        self.assertEqual(send.call_count, 1)


class TestStyleBlock(unittest.TestCase):
    """风格块(2026-09-10): 描述性读数, 不是预警 —— 数据缺失必须静默降级, 不能让邮件发不出"""

    _S = {"available": True, "as_of": "20260910", "rel_20": 0.0372, "rel_60": 0.0857,
          "rel_250": 0.0187, "bias": "顺风", "pool_state": "neutral", "pool_score": 3.0,
          "pool_drawdown": -0.0992, "bench_state": "defense", "diverged": True}

    def test_renders_readings(self):
        with mock.patch("app.models.style.load_style", return_value=self._S):
            b = notify._style_block()
        self.assertIn("20260910", b)
        self.assertIn("+8.6%", b)          # 近60日相对
        self.assertIn("顺风", b)
        self.assertIn("-9.9%", b)          # 池子自身回撤

    def test_divergence_flagged(self):
        with mock.patch("app.models.style.load_style",
                        return_value={**self._S, "diverged": False}):
            self.assertNotIn("b45309", notify._style_block())
        with mock.patch("app.models.style.load_style", return_value=self._S):
            self.assertIn("b45309", notify._style_block())

    def test_unavailable_and_errors_degrade_to_empty(self):
        # 池子表未建 / 数据不足 → 空串(邮件照发, 不出现半截块)
        for ret in ({"available": False, "reason": "池子指数为空"}, None):
            with mock.patch("app.models.style.load_style", return_value=ret):
                self.assertEqual(notify._style_block(), "")
        with mock.patch("app.models.style.load_style", side_effect=RuntimeError("db down")):
            self.assertEqual(notify._style_block(), "")   # 异常被吞, 不冒泡到发信路径

    def test_fields_escaped(self):
        evil = {**self._S, "bias": "<script>x</script>"}
        with mock.patch("app.models.style.load_style", return_value=evil):
            b = notify._style_block()
        self.assertNotIn("<script>", b)
        self.assertIn("&lt;script&gt;", b)


# ---------------------------------------------------------------------------
# 2026-09-18 邮件优化：统一信封 + 三轴（时间/次数/内容）的内容契约守卫
# ---------------------------------------------------------------------------
class _Capture(unittest.TestCase):
    """公共夹具：把 `_send_with_retry` 换成"只捕获不发送"，返回 (msg, body, tag)。

    守卫**绝不真发**：跑一次测试就发一封邮件的话，没人敢常跑测试。
    真发只在人工验收那一步做（用户已同意验收发两封）。
    """

    def _run(self, fn, *a, **kw):
        cap = []

        def fake(cfg, msg, tag):
            cap.append((msg, tag))
            return True
        with mock.patch.object(notify, "_send_with_retry", side_effect=fake):
            ok = fn(*a, **kw)
        self.assertTrue(ok, "邮件函数在配置齐全时必须返回 True")
        self.assertEqual(len(cap), 1, f"每次调用只能发一封, 实际 {len(cap)}")
        msg, tag = cap[0]
        body = msg.get_payload(decode=True).decode("utf-8")
        return msg, body, tag

    def _cfg(self):
        return mock.patch.object(notify.cfg_mod, "load_config", return_value=_cfg())


def _no_db_sections():
    """日终总结里四个"会读库"的小节统一打桩，让本文件的测试保持纯单测。"""
    return [mock.patch.object(notify, "_signal_section", return_value=""),
            mock.patch.object(notify, "_sleeve_section", return_value=""),
            mock.patch.object(notify, "_ops_section", return_value=""),
            mock.patch.object(notify, "_lhb_block", return_value="")]


SUMMARY = {"trade_date": "20260918", "nav": 20201.62, "cash": 8724.79,
           "market_value": 11475.0, "interest": 1.83, "bench_nav": 5071.61,
           "prev": {"trade_date": "20260917", "nav": 20039.26, "cash": 8724.79,
                    "market_value": 11313.0, "bench_nav": 5007.17},
           "initial": 20000.0, "bench0": 5008.36,
           "positions": [{"name": "甲", "ts_code": "600000.SH", "shares": 100,
                          "avg_cost": 10.0, "close": 10.2, "pnl": 20.0, "pnl_pct": 2.0}],
           "trades": [{"name": "甲", "ts_code": "600000.SH", "side": "buy",
                       "shares": 100, "price": 10.0, "cost": 5.0, "reason": "月度方案买入"}]}


class TestEnvelopeContract(_Capture):
    """**每封**邮件都必须满足的最低可读性契约。

    邮件是在手机上读、而看板深链**只在本机可达**（手机上必然打不开）⇒
    正文必须自包含、第一眼必须有结论、必须有明确的"要不要我动手"。
    新加邮件出口时：把它的 body 交给 `_check` 即可复用这五条。
    """

    def _check(self, body, tag):
        self.assertIn("需要你做什么", body, f"{tag}: 没有动作行 = 读的人要自己判断今天要不要动手")
        self.assertIn("不构成投资建议", body, f"{tag}: 缺免责声明")
        self.assertIn("仅本机可达", body, f"{tag}: 深链必须标注手机上打不开")
        self.assertNotIn("**", body, f"{tag}: markdown 星号在 HTML 里不渲染, 只会字面出现")
        self.assertNotIn("&lt;b&gt;", body, f"{tag}: 双重转义 —— 调用方刻意放的标签被吃掉了")

    def test_daily_summary(self):
        patches = _no_db_sections()
        with self._cfg(), patches[0], patches[1], patches[2], patches[3]:
            _m, body, tag = self._run(notify.send_daily_summary_email, SUMMARY)
        self.assertEqual(tag, "模拟盘日终汇总", "调度器开机补跑按这个 tag 判同日是否已发过")
        self._check(body, "日终总结")
        self.assertIn("当日 +162.36", body, "当日盈亏要带正号: 手机上第一眼就要能判方向")
        self.assertIn("累计 +1.01%", body)
        self.assertIn("超额 -0.25pp", body)
        self.assertIn("20260917", body, "Δ昨日的参照日要写出来, 否则不知道在跟哪天比")
        self.assertIn("口径", body)

    def test_plan_email(self):
        summary = {"plan_date": "20260831", "state": "defense", "equity_weight": 0.8,
                   "capital": 10000, "data_date": "20260904",
                   "stocks": [], "stock_cost": 0, "cash": 8000,
                   "repo": {"amount": 8000, "desc": "x", "rate": 1.5},
                   "etfs": [], "otc": {}, "warning": None}
        with self._cfg(), mock.patch.object(notify, "_style_block", return_value=""):
            _m, body, tag = self._run(notify.send_plan_email, summary)
        self.assertEqual(tag, "调仓方案")
        self._check(body, "月度方案")
        self.assertIn("样本外", body, "纪律 §4: 留出集已一次性开封, 回测读数只具方向性")
        self.assertIn("防守", body, "市场状态必须给中文, 不能把 defense 印进正文")

    def test_signal_email_lists_only_real_actions(self):
        sig = {"trade_date": "20260904", "state": "neutral", "items": [
            {"action": "sell", "name": "该卖", "ts_code": "600000.SH", "reason": "止损"},
            {"action": "buy_candidate", "name": "候选甲", "ts_code": "000001.SZ",
             "reason": "打分进前 10%"},
            {"action": "watch", "name": "观察乙", "ts_code": "000002.SZ",
             "reason": "跌出前 20%"}]}
        with self._cfg(), mock.patch.object(notify, "_lhb_block", return_value=""):
            _m, body, tag = self._run(notify.send_signal_email, sig)
        self.assertEqual(tag, "动态信号")
        self._check(body, "动态信号")
        self.assertIn("该卖", body)
        self.assertNotIn("候选甲", body, "观察/候选类不进即时邮件(否则 1 条真动作要配一屏噪声)")
        self.assertIn("另有 2 条", body, "被过滤掉的数量要说出来, 不能让人以为今天只有 1 条信号")

    def test_signal_email_tone_follows_the_action_kind(self):
        """减仓/候选 ⇒ 橙（要安排），止损/风险 ⇒ 红（今天就得处理）。"""
        for act, color in (("sell", "#c0392b"), ("warn", "#c0392b"), ("reduce", "#b45309")):
            with self.subTest(action=act):
                sig = {"trade_date": "20260904", "state": "neutral",
                       "items": [{"action": act, "name": "甲", "ts_code": "600000.SH",
                                  "reason": "x"}]}
                with self._cfg(), mock.patch.object(notify, "_lhb_block", return_value=""):
                    _m, body, _tag = self._run(notify.send_signal_email, sig)
                self.assertIn(color, body)

    def test_paper_trade_email(self):
        res = {"trade_date": "20260904", "trades": [
            {"side": "buy", "name": "甲", "ts_code": "600000.SH", "shares": 100,
             "price": 10.0, "reason": "月度方案买入"}]}
        with self._cfg():
            _m, body, tag = self._run(notify.send_paper_email, res)
        self.assertEqual(tag, "模拟盘")
        self._check(body, "成交邮件")
        self.assertIn("开盘档成交 1 笔", body)

    def test_daily_brief_email(self):
        brief = {"today": "20260918", "is_open": True, "pending": [], "signal": None,
                 "daily_last": "20260917", "open_days_late": 0, "mail_fail_24h": 0,
                 "ops_7d": 0, "paused": False, "halt": {}, "halt_unknown": False}
        with self._cfg():
            _m, body, tag = self._run(notify.send_daily_brief_email, brief)
        self.assertEqual(tag, "今日待办")
        self._check(body, "今日待办")
        self.assertIn("无挂单", str(_m["Subject"]), "主题行要能直接看出今天有没有活")


class TestDailySummaryShape(_Capture):
    """日终总结的**形状**：截断、空态、缺参照点都不能变成缺陷或谎言。"""

    def _send(self, s):
        patches = _no_db_sections()
        with self._cfg(), patches[0], patches[1], patches[2], patches[3]:
            return self._run(notify.send_daily_summary_email, s)

    def test_positions_truncated_at_ten_with_fold_row(self):
        pos = [{"name": f"股{i:02d}", "ts_code": f"6{i:05d}.SH", "shares": 100,
                "avg_cost": 10.0, "close": 10.0 + i / 100, "pnl": i * 10.0,
                "pnl_pct": i / 10} for i in range(1, 16)]
        _m, body, _tag = self._send({**SUMMARY, "positions": pos})
        self.assertEqual(body.count("<tr>"), 12,
                         "1 行表头 + 10 行持仓 + 1 行折叠汇总 —— 手机一屏读得完")
        self.assertIn("其余 5 只合计", body)
        # |浮盈| 从大到小 ⇒ 股15…股06 上表，股05…股01 折进汇总行
        self.assertIn("股15", body)
        self.assertNotIn("股05", body)

    def test_sorted_by_absolute_pnl_not_input_order(self):
        pos = [{"name": "小", "ts_code": "600000.SH", "shares": 1, "avg_cost": 10.0,
                "close": 10.05, "pnl": 5.0, "pnl_pct": 0.5},
               {"name": "大", "ts_code": "600001.SH", "shares": 1, "avg_cost": 10.0,
                "close": 9.0, "pnl": -100.0, "pnl_pct": -10.0}]
        _m, body, _tag = self._send({**SUMMARY, "positions": pos})
        self.assertLess(body.index("大"), body.index("小"),
                        "要按 |浮盈| 排, 亏得最多的那只不能排在赚得最多的后面")

    def test_missing_reference_points_do_not_lie(self):
        """没有 prev/initial/bench0 ⇒ 结论退回绝对值，**不许**编一个 0% 出来。

        只看结论行/主题（正文的口径块本来就要解释"超额"是怎么算的，那里出现字面
        "超额"是正常的口径说明，不是读数）。
        """
        m, body, _tag = self._send({k: v for k, v in SUMMARY.items()
                                    if k not in ("prev", "initial", "bench0")})
        verdict = body.split("需要你做什么")[0]
        self.assertIn("缺参照点", verdict)
        self.assertNotIn("超额", verdict)
        self.assertNotIn("pp", str(m["Subject"]))
        self.assertIn("缺参照点", str(m["Subject"]), "主题里也不能留一个看起来像读数的百分号")

    def test_empty_account_renders_empty_state_text(self):
        _m, body, _tag = self._send({**SUMMARY, "positions": [], "trades": []})
        self.assertIn("空仓", body)
        self.assertIn("当日无成交", body)

    def test_data_fields_are_escaped(self):
        evil = {**SUMMARY,
                "positions": [{"name": "<script>x</script>", "ts_code": "600000.SH",
                               "shares": 1, "avg_cost": 1.0, "close": 1.0,
                               "pnl": 0.0, "pnl_pct": 0.0}]}
        _m, body, _tag = self._send(evil)
        self.assertNotIn("<script>", body)
        self.assertIn("&lt;script&gt;", body)


class TestSectionsCarryCancelledEmails(unittest.TestCase):
    """被取消/被合并的那几封，内容必须**在日终总结里看得见** —— 否则"降噪"= "隐瞒"。"""

    def test_sleeve_section_carries_the_cancelled_kpi_email(self):
        cfg = {"email": {"enabled": True}, "weekly": {"enabled": True}}
        fake_led = mock.MagicMock()
        fake_led.state = {"trades": [
            {"date": "20260914", "ts_code": "600000.SH", "side": "buy", "shares": 100,
             "price": 10.0, "fee": 5.0},
            {"date": "20260914", "ts_code": "000001.SZ", "side": "sell", "shares": 50,
             "price": 9.0, "fee": 1.2}]}
        fake_led.snapshot.return_value = {"capital": 1e6, "nav": 1_012_345.0, "cash": 1e5,
                                         "n_positions": 98, "n_trades": 2, "interest": 21.32,
                                         "inception": "20260914", "last_mark": "20260914"}
        with mock.patch.object(notify.cfg_mod, "load_config", return_value=cfg), \
                mock.patch("app.weekly.ledger.Ledger.load", return_value=fake_led), \
                mock.patch("app.weekly.plan.sleeve_capital", return_value=1e6):
            html_out = notify._sleeve_section("20260914")
        for token in ("周频袖套", "袖套资金 1,000,000", "净值 1,012,345", "持仓 98 只",
                      "上次执行 <b>20260914</b>", "：2 笔（买 1 ／ 卖 1）",
                      "5.00", "1.20"):
            self.assertIn(token, html_out)
        self.assertNotIn("<b>600000.SH</b>", html_out)

    def test_sleeve_section_silent_when_weekly_disabled(self):
        cfg = {"weekly": {"enabled": False}}
        with mock.patch.object(notify.cfg_mod, "load_config", return_value=cfg):
            self.assertEqual(notify._sleeve_section("20260914"), "")

    def test_ops_section_shows_merged_self_heals(self):
        """同日合并掉的自愈必须在「今日运维」里数得出来（台账由看门狗写）。"""
        import json
        from app import config as cfg_mod
        day = "20260918"
        f = Path(cfg_mod.LOG_DIR) / "service_heal.json"
        f.write_text(json.dumps({"days": {day: {"ok": 4, "fail": 1, "mailed": 1}}}),
                     encoding="utf-8")
        self.addCleanup(f.unlink, missing_ok=True)
        out = notify._ops_section(day)
        self.assertIn("今天自愈拉起 <b>4</b> 次", out)
        self.assertIn("其中 <b>3</b> 次被同日合并", out)
        self.assertIn("拉起失败 <b>1</b> 次", out)
        self.assertIn("这不是邮件问题", out, "一天死三次 ⇒ 必须点名根因不在邮件")

    def test_ops_section_survives_missing_ledger(self):
        from app import config as cfg_mod
        (Path(cfg_mod.LOG_DIR) / "service_heal.json").unlink(missing_ok=True)
        out = notify._ops_section("19990101")     # 那天什么都没有 ⇒ 空态文案, 不抛
        self.assertIn("没有任何邮件收发记录", out)


class TestBriefSleeveExecutionLine(_Capture):
    """晨间待办里的「袖套今天动不动」—— 判据归 `pipeline`，邮件只转述，不自己算 weekday。"""

    _B = {"today": "20260921", "is_open": True, "pending": [], "signal": None,
          "daily_last": "20260918", "open_days_late": 0, "mail_fail_24h": 0, "ops_7d": 0,
          "paused": False, "halt": {}, "halt_unknown": False}

    def _body(self, **kw):
        return self._email(**kw)[1]

    def _email(self, **kw):
        with mock.patch.object(notify.cfg_mod, "load_config", return_value=_cfg()):
            return self._run(notify.send_daily_brief_email, {**self._B, **kw})

    def test_ready_plan_is_announced_with_count(self):
        body = self._body(sleeve_exec={"anchor": "20260918", "ready": True, "n_stocks": 98})
        self.assertIn("袖套执行日", body)
        self.assertIn("20260918", body)
        self.assertIn("98", body)

    def test_missing_plan_turns_the_verdict_into_a_warning(self):
        body = self._body(sleeve_exec={"anchor": "20260918", "ready": False, "n_stocks": None})
        self.assertIn("方案还没就绪", body)
        self.assertIn("run_weekly.py doctor", body, "处置建议要给一条**真的存在**的命令")
        self.assertIn("#b45309", body, "这条是 warn 档, 不能画成绿色结论")

    def test_unknown_predicate_does_not_claim_a_negative(self):
        """判据取不到 ⇒ 宁可不写这行, 也不能断言"今天不是执行日"。"""
        body = self._body(sleeve_exec_unknown=True)
        self.assertNotIn("今天不是袖套执行日", body)

    def test_ordinary_day_says_not_execution_day(self):
        self.assertIn("今天不是袖套执行日", self._body())

    def test_sleeve_readings_live_only_in_the_evening_email(self):
        """用户 2026-09-18 定：**袖套读数只在晚间账户总结出现一次**。

        早上这封只回答"今天动不动、方案在不在"。同组数字一天出现两遍，
        迟早会出现两遍不一致（净值取数时刻不同、口径改一处漏一处）。
        """
        body = self._body(sleeve_exec={"anchor": "20260918", "ready": True, "n_stocks": 98},
                          sleeve_next_exec="20260928")
        # 只钉"带数值的读数"，不钉字段名 —— 指引那句本来就要提到这些词
        for token in ("1,040,082", "133,967", "+3.05%", "最后盯市", "上次调仓", "净值 <b>"):
            self.assertNotIn(token, body, f"袖套读数不该出现在早上: {token}")
        self.assertIn("在晚间「今日市场与账户总结」里报", body,
                      "不留指引 = 读的人会以为整条线没人管")

    def test_risk_halt_still_shows_in_the_morning(self):
        """风控停买是"读数"之外唯一留在早上的袖套事实：它改变今天 09:32 那档的行为。"""
        body = self._body(halt={"active": True, "since": "20260919", "reason": "健康度 dead"})
        self.assertIn("停买", body)
        m, _b, _t = self._email(halt={"active": True, "since": "20260919", "reason": "健康度 dead"})
        self.assertIn("⛔ 需人工处理", str(m["Subject"]))

    def test_missing_ledger_says_unknown_not_not_halted(self):
        """账本读不到 ⇒ 风控只能写"未知"，不许渲染成"未停买"。"""
        body = self._body(halt_unknown=True)
        self.assertIn("未知（账本还没建）", body)
        self.assertNotIn("未停买", body)


    def test_warn_and_bad_go_into_the_subject(self):
        """手机上只有主题能被扫到 ⇒ 非 ok 的结论不许被"无挂单"三个字盖掉。

        实测周一就是这个情形：月频没挂单、但袖套方案没就绪 ⇒ 旧主题写"无挂单"，
        正文里唯一的待办事项在收件箱列表上完全看不见。
        """
        m, _b, _t = self._email(sleeve_exec={"anchor": "20260918", "ready": False,
                                             "n_stocks": None})
        subj = str(m["Subject"])
        self.assertIn("⚠", subj)
        self.assertIn("方案还没就绪", subj)
        self.assertNotIn("无挂单", subj)
        m2, _b2, _t2 = self._email(paused=True)
        self.assertIn("⛔ 需人工处理", str(m2["Subject"]))
        m3, _b3, _t3 = self._email()              # 一切正常 ⇒ 主题回到"有没有挂单"
        self.assertIn("无挂单，链路正常", str(m3["Subject"]))


class TestBriefSleeveData(unittest.TestCase):
    """`build_daily_brief()` 的袖套取数：全程只读，且绝不因为袖套读不到而抛。"""

    def test_brief_data_has_no_sleeve_snapshot(self):
        """组装层同样不许带袖套读数 —— 只在呈现层删干净是不够的（下次加回来最方便的路径
        就是"数据早就取好了"）。"""
        from app import notify as n
        out = n.build_daily_brief("20260921")     # 周一 = 袖套执行日
        self.assertNotIn("sleeve", out)
        # 执行日判据与 `pipeline` 同源：周一该报"今天是执行日"，且不再算"下次"
        self.assertTrue(out.get("sleeve_exec"))
        self.assertFalse(out.get("sleeve_next_exec"))
        self.assertIn("halt", out)                 # 风控留在早上（它改变今天的成交行为）

    def test_non_execution_day_reports_the_next_one(self):
        from app import notify as n
        out = n.build_daily_brief("20260922")     # 周二：不调仓
        self.assertFalse(out.get("sleeve_exec"))
        self.assertEqual(out.get("sleeve_next_exec"), "20260928")

    def test_reading_the_brief_does_not_touch_the_ledger(self):
        import hashlib
        from app.weekly.ledger import DEFAULT_PATH
        before = hashlib.sha256(DEFAULT_PATH.read_bytes()).hexdigest()
        notify.build_daily_brief("20260921")
        self.assertEqual(before, hashlib.sha256(DEFAULT_PATH.read_bytes()).hexdigest(),
                         "晨间邮件是旁观者 —— 读一次账本不许改动它")


# ---------------------------------------------------------------------------
# 2026-10-06 邮件内容二轮优化（用户"仍然不满意"后逐封渲染成品找出的毛病）：
# 多问题并列 / 候选降噪 / 成交聚合 / 零动作防狼来了 / 挂单金额感
# ---------------------------------------------------------------------------
class TestMailContent20261006(_Capture):
    def test_signal_email_zero_action_is_green_not_alarmist(self):
        """防御性兜底：真动作集为空时不得套"需要处理"的橙/红结论（0 动作=绿色 ok 档）。"""
        sig = {"trade_date": "20260904", "state": "neutral",
               "items": [{"action": "watch", "name": "观察乙", "ts_code": "000002.SZ",
                          "reason": "x"}]}
        with self._cfg(), mock.patch.object(notify, "_lhb_block", return_value=""):
            _m, body, _tag = self._run(notify.send_signal_email, sig)
        self.assertIn("今天没有需要动手的动作", body)
        self.assertIn("#1f7a45", body, "零动作必须是绿色 ok 档，橙色=狼来了")
        self.assertNotIn("需要今天/明天处理", body)
        self.assertIn("另有 1 条", body, "被过滤数量照旧要说明")

    def test_summary_signal_section_lists_counts_not_noise(self):
        """日终「今日信号」：候选/观察只给分类计数，不再逐行刷屏（21 行候选曾占大半封）。"""
        rows = [{"action": "sell", "name": "该卖", "ts_code": "600000.SH", "reason": "止损"}]
        rows += [{"action": "buy_candidate", "name": f"候选{i:02d}",
                  "ts_code": f"00000{i:02d}.SZ", "reason": "新进前20"} for i in range(21)]
        with mock.patch.object(notify, "_rows", return_value=rows), \
             mock.patch.object(notify.db, "get_conn"), \
             mock.patch.object(notify, "_lhb_block", return_value=""):
            html = notify._signal_section("20261006")
        self.assertIn("候选买入 21 条", html)
        self.assertIn("次日真动作 <b>1</b> 条", html)
        self.assertIn("该卖", html, "真动作行保留")
        self.assertNotIn("候选05", html, "候选明细不得逐行渲染")
        self.assertNotIn("另有", html, "旧的'另有 N 条未列出'句式随逐行清单一起退役")

    def test_brief_lists_all_issues_and_est_amount(self):
        """多问题并存全部列出（原 elif 链互相遮蔽）+ 挂单约金额列。"""
        brief = {"today": "20261006", "is_open": True,
                 "pending": [{"order_date": "20260930", "ts_code": "000001.SZ",
                              "name": "平安银行", "side": "buy", "shares": 300,
                              "reason": "月度方案买入", "est_amt": 3504}],
                 "signal": None, "daily_last": "20260930", "open_days_late": 3,
                 "mail_fail_24h": 0, "ops_7d": 0, "paused": True,
                 "halt": {"active": True, "since": "20260901", "reason": "回撤"},
                 "halt_unknown": False,
                 "sleeve_exec": {"anchor": "20260930", "ready": False, "n_stocks": None},
                 "accounts_actions": None}
        with self._cfg():
            _m, body, _tag = self._run(notify.send_daily_brief_email, brief)
        self.assertIn("交易总闸暂停", body, "verdict = 最严重的一条")
        self.assertIn("今日其他注意项", body)
        self.assertIn("风控停买中", body, "第二问题也必须出现")
        self.assertIn("还没就绪", body, "第三问题也必须出现")
        self.assertIn("落后 3 个开市日", body, "第四问题也必须出现")
        self.assertIn("约 3,504", body, "挂单金额列给'这次调仓动多少钱'的量级感")
        self.assertIn("按最新日线收盘价估算", body, "约金额必须带口径注")

    def test_paper_email_has_trade_aggregates(self):
        """成交邮件补聚合行：动了多少钱/几个标的，不只一张逐笔表。"""
        res = {"trade_date": "20260930", "trades": [
            {"side": "buy", "name": "甲", "ts_code": "000001.SZ", "shares": 300,
             "price": 10.0, "reason": "r"},
            {"side": "sell", "name": "乙", "ts_code": "000002.SZ", "shares": 200,
             "price": 10.0, "reason": "r"}]}
        with self._cfg():
            _m, body, _tag = self._run(notify.send_paper_email, res)
        self.assertIn("净额 1,000", body)
        self.assertIn("涉及标的 2 只", body)
        self.assertIn("18:30 日终汇总", body, "费用口径指向日终，不算两套账")


if __name__ == "__main__":
    unittest.main()
