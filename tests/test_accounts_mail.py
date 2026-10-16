"""全账户邮件层（app/accounts_mail.py）的测试。

mock 口径与 tests/test_notify.py 一致：patch `cfg_mod.load_config` 给配置、
patch `_send_with_retry` 给发送结果 —— **从不碰真 smtplib**；notify_log 同日去重
的 DB 交互用假 conn 桩掉（绝不读写生产库）。
"""
import unittest
from unittest import mock

from app import accounts_mail as am
from app import model_picks as mp
from app.all_accounts import build_all


def _acc(id="fterm-w2", name="Final 2万档", model="Final", ok=True, **over):
    base = {"id": id, "name": name, "model": model, "ok": ok, "capital": 20000.0,
            "equity": 20000.0, "cash": 20000.0, "pos_value": 0.0, "pct": 0.0,
            "dd_pct": 0.0, "days": 1, "curve": [["2026-09-30", 0.0]],
            "positions": [], "trades": [], "extra": {}, "asof": "2026-09-30",
            "updated": "2026-09-30", "stale": False, "error": None}
    base.update(over)
    return base


def _payload(accounts):
    ok_n = sum(1 for a in accounts if a["ok"])
    by_model = {}
    for a in accounts:
        if a["ok"]:
            m = by_model.setdefault(a["model"], {"equity": 0.0, "n": 0})
            m["equity"] += a["equity"] or 0.0
            m["n"] += 1
    return {"generated_at": "2026-10-05T21:15:00", "accounts": accounts,
            "totals": {"equity": sum(a["equity"] or 0 for a in accounts),
                       "n_ok": ok_n, "n_total": len(accounts), "by_model": by_model}}


def _cfg(enabled=True, paused=False):
    return {"email": {"enabled": enabled, "smtp_server": "smtp.test", "smtp_port": 465,
                      "sender": "s@test", "auth_code": "x",
                      "recipients": ["r@test"]},
            "paper": {"paused": paused}}


class TestAssess(unittest.TestCase):
    def test_read_failed_is_bad(self):
        alerts = am.assess(_payload([_acc(ok=False, error="file missing")]))
        hit = [a for a in alerts if a["code"] == "read_failed"]
        self.assertEqual(len(hit), 1)
        self.assertEqual(hit[0]["level"], "bad")
        self.assertIn("file missing", hit[0]["message"])

    def test_stale_is_warn(self):
        alerts = am.assess(_payload([_acc(id="ecow2-k6", name="k6", model="W2",
                                          stale=True)]))
        self.assertTrue(any(a["code"] == "stale" and a["level"] == "warn"
                            for a in alerts), f"应报数据滞后: {alerts}")

    def test_fterm_halt_breach_and_alerts_and_heartbeat(self):
        a = _acc(extra={"halt_line": 17600.0, "heartbeat": None,
                        "alerts": [{"ts": "2026-10-05T20:05", "level": "warn",
                                    "code": "X", "message": "弃单: 涨停"}]})
        a["equity"] = 17500.0
        alerts = am.assess(_payload([a]))
        codes = {x["code"]: x["level"] for x in alerts}
        self.assertEqual(codes.get("halt_line_breach"), "bad")
        self.assertEqual(codes.get("fterm_alerts"), "warn")
        self.assertEqual(codes.get("no_heartbeat"), "warn")

    def test_t3_without_heartbeat_is_not_an_alert(self):
        """心跳只写在 w2 轨的 state 库（runner._run_dlv2 的 db0.heartbeat 覆盖双轨），
        t3 库没有心跳是设计如此 —— 对它体检会每天误报（2026-10-05 首跑实测）。"""
        a = _acc(id="fterm-t3", name="Final T3 100万验证档", equity=1000000.0,
                 extra={"halt_line": 880000.0, "heartbeat": None, "alerts": []})
        self.assertEqual(am.assess(_payload([a])), [])

    def test_ecow2_circuit_layers(self):
        a = _acc(id="ecow2-k6", name="k6", model="W2",
                 extra={"circuit": {"halted": True, "derisked": True,
                                    "cautious": True, "peak_nav": None, "c1_at": None}})
        alerts = am.assess(_payload([a]))
        codes = {x["code"]: x["level"] for x in alerts}
        self.assertEqual(codes.get("circuit_halted"), "bad")
        self.assertEqual(codes.get("circuit_derisked"), "warn")
        self.assertEqual(codes.get("circuit_cautious"), "warn")

    @mock.patch.object(am.cfg_mod, "load_config", side_effect=lambda: _cfg(paused=True))
    def test_monthly_paused(self, _m):
        a = _acc(id="neweco-monthly", name="NEW ECO 月频盘", model="NEW ECO")
        alerts = am.assess(_payload([a]))
        self.assertTrue(any(x["code"] == "paper_paused" for x in alerts), str(alerts))

    def test_clean_account_has_no_alerts(self):
        a = _acc(extra={"halt_line": 17600.0, "heartbeat": "2026-10-05T20:05 x",
                        "alerts": [], "circuit": {"halted": False}})
        a["equity"] = 20000.0
        self.assertEqual(am.assess(_payload([a])), [])

    def test_real_payload_assess_runs(self):
        alerts = am.assess(build_all(force=True))
        for x in alerts:
            for key in ("account_id", "account_name", "model", "level", "code", "message"):
                self.assertIn(key, x)

    # ---- 2026-10-06 内容优化：告警必须自带"意味着什么/影响/处置" -------------------
    def test_every_alert_code_has_explanation(self):
        """用户要求：块级报警要说清楚意味着什么。每个告警码都必须有非空的
        title/mean/impact/act，且 title 是中文人话（= code 说明 KB 漏登记）。"""
        a_fterm = _acc(extra={"halt_line": 17600.0, "heartbeat": None,
                              "alerts": [{"ts": "t", "level": "warn",
                                          "code": "fill_blocked", "message": "弃单"}]})
        a_fterm["equity"] = 17500.0            # 压到熔断线下方 ⇒ halt_line_breach 触发
        a_eco = _acc(id="ecow2-k6", name="k6", model="W2",
                     extra={"circuit": {"halted": True, "derisked": True,
                                        "cautious": True, "peak_nav": None, "c1_at": None}})
        payload = _payload([_acc(id="ecow2-k8", name="k8", model="W2",
                                 ok=False, error="x"),
                            _acc(id="neweco-monthly", name="月频盘", model="NEW ECO",
                                 stale=True),
                            _acc(id="neweco-weekly", name="袖套", model="NEW ECO",
                                 stale=True),
                            a_fterm, a_eco])
        with mock.patch.object(am.cfg_mod, "load_config",
                               side_effect=lambda: _cfg(paused=True)):
            alerts = am.assess(payload)
        codes = {x["code"] for x in alerts}
        self.assertTrue({"read_failed", "stale", "paper_paused", "halt_line_breach",
                         "no_heartbeat", "fterm_alerts", "circuit_halted",
                         "circuit_derisked", "circuit_cautious"} <= codes, str(codes))
        for x in alerts:
            for key in ("title", "mean", "impact", "act"):
                self.assertTrue(str(x.get(key) or "").strip(),
                                f"{x['code']} 缺 {key}")
            self.assertNotEqual(x["title"], x["code"],
                                f"{x['code']} 的 title 还是裸代码 ⇒ _ALERT_KB 漏登记")
            self.assertNotIn("（体检项", x["mean"], "未登记占位泄漏进了告警")

    def test_fterm_alert_message_translates_latest_code(self):
        a = _acc(extra={"heartbeat": "h",
                        "alerts": [{"ts": "2026-10-06T20:05", "level": "warn",
                                    "code": "fill_blocked", "message": "1 单弃单"}]})
        alerts = am.assess(_payload([a]))
        hit = [x for x in alerts if x["code"] == "fterm_alerts"][0]
        self.assertIn("封板弃单", hit["message"], "原始码必须在邮件层翻译成人话")
        self.assertIn("市场约束", hit["mean"])


class TestBuildDailyReport(unittest.TestCase):
    def test_report_contains_all_accounts_and_bad_tone(self):
        accounts = [_acc(id="ecow2-k6", name="k6 盘", model="W2", ok=False,
                         error="账本缺失"),
                    _acc(id="fterm-w2")]
        payload = _payload(accounts)
        rep = am.build_daily_report(payload, am.assess(payload))
        self.assertIn("全账户日报", rep["subject"])
        self.assertEqual(rep["tone"], "bad")
        for name in ("k6 盘", "Final 2万档"):
            self.assertIn(name, rep["body"])
        self.assertIn("✗ 读取失败", rep["body"])

    def test_report_ok_tone_when_clean(self):
        accounts = [_acc(id="fterm-w2", extra={"halt_line": 17600.0,
                                               "heartbeat": "h", "alerts": []})]
        rep = am.build_daily_report(_payload(accounts))
        self.assertEqual(rep["tone"], "ok")
        self.assertIn("✓ 正常", rep["body"])

    # ---- 2026-10-06 内容优化 -------------------------------------------------------
    def test_report_alert_block_explains_meaning(self):
        """日报告警块：表格用中文体检项，表下逐条给 意味着/影响/建议。"""
        a = _acc(id="ecow2-k6", name="k6", model="W2",
                 extra={"circuit": {"halted": True, "derisked": False,
                                    "cautious": False, "peak_nav": None, "c1_at": None}})
        payload = _payload([a])
        rep = am.build_daily_report(payload, am.assess(payload))
        self.assertIn("熔断（停交易层）", rep["body"], "体检项列必须是中文人话")
        self.assertIn("影响：", rep["body"])
        self.assertIn("建议：", rep["body"])
        self.assertIn("A 层硬停机", rep["body"], "解释必须来自 _ALERT_KB 而非裸 message")
        self.assertIn("建议处置", rep["body"], "bad 档的动作行（随信封进正文）要指向逐条解释")

    def test_report_counts_monthly_trades(self):
        """修复：月频盘走 09:31 即时邮件被排除在成交邮件之外是对的，但日报的
        「今日成交」列与 verdict 计数曾把月频也排除 ⇒ 月频当天成交了显示 0 = 假象。"""
        accounts = [_acc(id="neweco-monthly", name="月频盘", model="NEW ECO",
                         trades=[{"date": "20261005", "code": "000001.SZ",
                                  "side": "buy", "shares": 100, "price": 10.0}]),
                    _acc(id="fterm-w2")]
        payload = _payload(accounts)
        rep = am.build_daily_report(payload, am.assess(payload), today8="20261005")
        self.assertIn("今日成交 1 笔", rep["subject"])
        # 月频行的成交列显示 1（截断匹配 ">1</td>"）
        self.assertIn(">1</td>", rep["body"])


class _FakeCursor:
    """fetchone() 语义对齐 sqlite3：返回元组。"""
    def __init__(self, n):
        self._n = n

    def fetchone(self):
        return (self._n,)


class _FakeConn:
    def __init__(self, sent_count):
        self._n = sent_count

    def execute(self, *a, **k):
        return _FakeCursor(self._n)

    def commit(self):
        pass


class _FakeDbCtx:
    def __init__(self, sent_count):
        self._n = sent_count

    def __enter__(self):
        return _FakeConn(self._n)

    def __exit__(self, *a):
        return False


class TestSendDailyReport(unittest.TestCase):
    @mock.patch.object(am.cfg_mod, "load_config", side_effect=lambda: _cfg(enabled=False))
    def test_disabled_sends_nothing(self, _m):
        with mock.patch.object(am, "_send_with_retry") as sender:
            self.assertFalse(am.send_daily_report(_payload([_acc()])))
            sender.assert_not_called()

    @mock.patch.object(am.cfg_mod, "load_config", side_effect=lambda: _cfg())
    @mock.patch.object(am, "db")
    def test_send_ok_and_same_day_dedup(self, fake_db, _m):
        fake_db.conn_ctx = lambda: _FakeDbCtx(0)
        with mock.patch.object(am, "_send_with_retry", return_value=True) as sender:
            self.assertTrue(am.send_daily_report(_payload([_acc()])))
            self.assertEqual(sender.call_count, 1)
        # 同日已发过(notify_log 有 ok=1) ⇒ 第二次不发（开机补跑的双重保险）
        fake_db.conn_ctx = lambda: _FakeDbCtx(1)
        with mock.patch.object(am, "_send_with_retry") as sender:
            self.assertFalse(am.send_daily_report(_payload([_acc()])))
            sender.assert_not_called()


class TestSlot(unittest.TestCase):
    def setUp(self):
        # 模块级去重字典跨用例残留（同 key 同日）⇒ 每用例清空，与 _TRADE_SENT 同纪律
        am._ALERTED.clear()

    @mock.patch.object(am.cfg_mod, "load_config", side_effect=lambda: _cfg(enabled=False))
    def test_slot_disabled_returns_dict_without_raise(self, _m):
        out = am.run_accounts_mail_slot()
        self.assertFalse(out["report_sent"])
        self.assertEqual(out["alerts_sent"], 0)
        self.assertIn("n_alerts", out)

    @mock.patch.object(am.cfg_mod, "load_config", side_effect=lambda: _cfg())
    @mock.patch.object(am, "db")
    def test_slot_sends_alert_then_report(self, fake_db, _m):
        fake_db.conn_ctx = lambda: _FakeDbCtx(0)
        bad = _acc(id="ecow2-k6", name="k6", model="W2",
                   extra={"circuit": {"halted": True, "derisked": False,
                                      "cautious": False, "peak_nav": None, "c1_at": None}})
        payload = _payload([bad])
        with mock.patch.object(am, "assess", return_value=[
                {"account_id": "ecow2-k6", "account_name": "k6", "model": "W2",
                 "level": "bad", "code": "circuit_halted", "message": "熔断"}]) as m_a, \
             mock.patch.object(am, "build_all", return_value=payload), \
             mock.patch.object(am, "send_ops_alert", return_value=True) as m_alert, \
             mock.patch.object(am, "_send_with_retry", return_value=True):
            out = am.run_accounts_mail_slot()
        self.assertEqual(out["alerts_sent"], 1)
        self.assertTrue(out["report_sent"])
        m_alert.assert_called_once()
        self.assertIn("circuit_halted", m_alert.call_args[0][0])
        # 体检函数确实被走过（不是测试桩短路了数据层）
        m_a.assert_called_once()

    @mock.patch.object(am.cfg_mod, "load_config", side_effect=lambda: _cfg())
    @mock.patch.object(am, "db")
    def test_alert_dedup_survives_restart(self, fake_db, _m):
        """2026-10-13 修复回归：自愈重启清空 _ALERTED ⇒ model_meta 落库标记兜底，
        同 key 同日不重发（内存去重对重启无防护，当晚实测重发三组）。"""
        fake_db.get_meta.return_value = am.time.strftime("%Y-%m-%d")
        with mock.patch.object(am, "send_ops_alert", return_value=True) as m_alert:
            self.assertFalse(am._account_alert_once("acct_x_circuit_halted", "t", "b"))
            m_alert.assert_not_called()

    @mock.patch.object(am.cfg_mod, "load_config", side_effect=lambda: _cfg())
    @mock.patch.object(am, "db")
    def test_alert_db_failure_fails_open(self, fake_db, _m):
        """DB 不可用 ⇒ 按未发过处理照常发送（fail-open），不因去重层故障丢告警。"""
        fake_db.conn_ctx.side_effect = RuntimeError("db down")
        with mock.patch.object(am, "send_ops_alert", return_value=True) as m_alert:
            self.assertTrue(am._account_alert_once("acct_x_circuit_halted", "t", "b"))
            m_alert.assert_called_once()

    def test_alert_email_body_has_meaning_and_disposition(self):
        """告警邮件正文必须含 意味着什么/影响/处置 三段（2026-10-06 内容优化）。"""
        am._ALERTED.clear()
        bad = _acc(id="ecow2-k6", name="k6", model="W2",
                   extra={"circuit": {"halted": True, "derisked": False,
                                      "cautious": False, "peak_nav": None, "c1_at": None}})
        payload = _payload([bad])
        with mock.patch.object(am.cfg_mod, "load_config",
                               side_effect=lambda: _cfg()), \
             mock.patch.object(am, "build_all", return_value=payload), \
             mock.patch.object(am, "send_ops_alert", return_value=True) as m_alert, \
             mock.patch.object(am, "_send_with_retry", return_value=True), \
             mock.patch.object(am, "db"):
            out = am.run_accounts_mail_slot()
        self.assertEqual(out["alerts_sent"], 1)
        body = m_alert.call_args[0][1]
        self.assertIn("意味着什么：", body)
        self.assertIn("对账户的影响：", body)
        self.assertIn("建议处置：", body)
        self.assertIn("A 层硬停机", body, "解释来自 _ALERT_KB")
        self.assertIn("circuit_halted", m_alert.call_args[0][0], "标题保留体检码便于检索")


class TestCollectTodayTrades(unittest.TestCase):
    def test_today_filter_and_exclusions(self):
        today8 = "20261005"
        t_today = {"date": "20261005", "code": "300621.SZ", "side": "buy",
                   "shares": 100, "price": 10.0}
        t_yesterday = {"date": "20261004", "code": "000001.SZ", "side": "sell",
                       "shares": 100, "price": 9.0}
        t_norm = {"date": "2026-10-05", "code": "000002.SZ", "side": "buy",
                  "shares": 200, "price": 5.0}
        accounts = [
            _acc(id="fterm-w2", trades=[t_today, t_yesterday]),
            _acc(id="ecow2-k6", name="k6", model="W2", trades=[dict(t_norm)]),
            # 月频已有 09:31 即时邮件 ⇒ 不进成交邮件
            _acc(id="neweco-monthly", name="月频盘", model="NEW ECO",
                 trades=[dict(t_today)]),
            _acc(id="fterm-t3", name="t3", trades=[]),
        ]
        out = am.collect_today_trades(_payload(accounts), today8)
        self.assertEqual(set(out), {"fterm-w2", "ecow2-k6"})
        self.assertEqual(len(out["fterm-w2"]), 1)
        self.assertEqual(out["fterm-w2"][0]["code"], "300621.SZ")

    def test_empty_when_no_trades(self):
        self.assertEqual(am.collect_today_trades(_payload([_acc(trades=[])]), "20261005"), {})


class TestSendTradeEmails(unittest.TestCase):
    def setUp(self):
        # 模块级去重字典跨测试残留 ⇒ 每用例清空（否则前一个用例的 key 吃掉本用例的发送）
        am._TRADE_SENT.clear()

    def _payload_with_trade(self):
        return _payload([_acc(id="fterm-w2",
                              trades=[{"date": "20261005", "code": "300621.SZ",
                                       "side": "buy", "shares": 400, "price": 7.11,
                                       "amount": 2844.0, "reason": "目标调入"}])])

    @mock.patch.object(am.cfg_mod, "load_config", side_effect=lambda: _cfg(enabled=False))
    @mock.patch.object(am, "db")
    def test_disabled_sends_nothing(self, fake_db, _m):
        with mock.patch.object(am, "_send_with_retry") as sender:
            self.assertEqual(am.send_trade_emails(self._payload_with_trade()), 0)
            sender.assert_not_called()

    @mock.patch.object(am.cfg_mod, "load_config", side_effect=lambda: _cfg())
    @mock.patch.object(am, "db")
    def test_one_email_per_account_with_name_in_subject(self, fake_db, _m):
        captured = []

        def fake_send(cfg, msg, tag):
            captured.append((str(msg["Subject"]),
                             msg.get_payload(decode=True).decode("utf-8"), tag))
            return True

        with mock.patch.object(am, "_send_with_retry", side_effect=fake_send):
            n = am.send_trade_emails(self._payload_with_trade(), "20261005")
        self.assertEqual(n, 1)
        self.assertEqual(len(captured), 1)
        subject, body, tag = captured[0]
        self.assertEqual(tag, am.TRADE_TAG)
        self.assertIn("Final 2万档", subject)
        self.assertIn("今日成交 1 笔（买 1 / 卖 0）", body)
        self.assertIn("300621.SZ", body)
        self.assertIn("目标调入", body)

    @mock.patch.object(am.cfg_mod, "load_config", side_effect=lambda: _cfg())
    @mock.patch.object(am, "db")
    def test_same_day_dedup(self, fake_db, _m):
        with mock.patch.object(am, "_send_with_retry", return_value=True) as sender:
            self.assertEqual(am.send_trade_emails(self._payload_with_trade(), "20261005"), 1)
            self.assertEqual(am.send_trade_emails(self._payload_with_trade(), "20261005"), 0)
            self.assertEqual(sender.call_count, 1, "同日同账户只发一次")

    @mock.patch.object(am.cfg_mod, "load_config", side_effect=lambda: _cfg())
    @mock.patch.object(am, "db")
    def test_no_trades_no_email(self, fake_db, _m):
        with mock.patch.object(am, "_send_with_retry") as sender:
            self.assertEqual(am.send_trade_emails(_payload([_acc(trades=[])]), "20261005"), 0)
            sender.assert_not_called()

    @mock.patch.object(am.cfg_mod, "load_config", side_effect=lambda: _cfg())
    @mock.patch.object(am, "db")
    def test_dedup_survives_restart_via_db_marker(self, fake_db, _m):
        """2026-10-13 修复回归：服务自愈重启清空内存去重 ⇒ 落库标记(model_meta)兜底，
        同日同账户不重发（当晚实测 22:52/23:05/23:25 三次自愈补跑各重发一组）。"""
        fake_db.get_meta.return_value = "20261005"
        with mock.patch.object(am, "_send_with_retry") as sender:
            self.assertEqual(am.send_trade_emails(self._payload_with_trade(), "20261005"), 0)
            sender.assert_not_called()

    @mock.patch.object(am.cfg_mod, "load_config", side_effect=lambda: _cfg())
    @mock.patch.object(am, "db")
    def test_db_failure_fails_open(self, fake_db, _m):
        """model_meta 不可用时退化为纯内存去重：宁可至多重发，不允许丢信。"""
        fake_db.conn_ctx.side_effect = RuntimeError("db down")
        with mock.patch.object(am, "_send_with_retry", return_value=True) as sender:
            self.assertEqual(am.send_trade_emails(self._payload_with_trade(), "20261005"), 1)


class TestMorningActions(unittest.TestCase):
    def test_monthly_weekly_excluded(self):
        accounts = [_acc(id="neweco-monthly", name="月频盘", model="NEW ECO"),
                    _acc(id="neweco-weekly", name="袖套", model="NEW ECO")]
        self.assertEqual(am.morning_actions(_payload(accounts), "20261005"), [])

    def test_fterm_pending_orders(self):
        a = _acc(extra={"orders": [{"code": "300621.SZ", "side": "buy", "reason": "目标调入"}],
                        "heartbeat": "h", "alerts": []})
        out = am.morning_actions(_payload([a]), "20261005")
        self.assertEqual(len(out), 1)
        self.assertIn("1 笔挂单", out[0]["text"])
        self.assertIn("目标调入", out[0]["text"])

    def test_fterm_no_pending_no_item(self):
        a = _acc(extra={"orders": [], "heartbeat": "h", "alerts": []})
        self.assertEqual(am.morning_actions(_payload([a]), "20261005"), [])

    def test_ecow2_exec_day_is_warn(self):
        a = _acc(id="ecow2-k6", name="k6", model="W2",
                 extra={"periods": [{"anchor": "20260924"}]})
        with mock.patch.object(am, "_ecow2_next_exec",
                               return_value={"anchor": "20261009", "t1": "20261005"}):
            out = am.morning_actions(_payload([a]), "20261005")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["level"], "warn")
        self.assertIn("今日为执行日", out[0]["text"])

    def test_ecow2_next_exec_day_is_info(self):
        a = _acc(id="ecow2-k6", name="k6", model="W2",
                 extra={"periods": [{"anchor": "20260924"}]})
        with mock.patch.object(am, "_ecow2_next_exec",
                               return_value={"anchor": "20261009", "t1": "20261012"}):
            out = am.morning_actions(_payload([a]), "20261005")
        self.assertEqual(out[0]["level"], "ok")
        self.assertIn("下期执行日 20261012", out[0]["text"])

    def test_read_failed_is_bad(self):
        out = am.morning_actions(_payload([_acc(ok=False, error="x")]), "20261005")
        self.assertEqual(out[0]["level"], "bad")

    @mock.patch.object(am, "build_all")
    def test_real_payload_runs(self, m_build):
        m_build.return_value = build_all(force=True)
        out = am.morning_actions(build_all(force=True), "20261005")
        for x in out:
            for key in ("account_id", "account_name", "level", "text"):
                self.assertIn(key, x)
            self.assertNotIn("¥", x["text"], "早间正文不出现净值读数（既有测试红线）")


class TestDailyReportGrouped(unittest.TestCase):
    def test_report_grouped_by_model_with_trades(self):
        accounts = [
            _acc(id="neweco-monthly", name="月频盘", model="NEW ECO"),
            _acc(id="fterm-w2", trades=[{"date": "20261005", "code": "300621.SZ",
                                         "side": "buy", "shares": 400, "price": 7.11,
                                         "amount": 2844.0}]),
            _acc(id="ecow2-k6", name="k6", model="W2"),
        ]
        payload = _payload(accounts)
        rep = am.build_daily_report(payload, am.assess(payload), today8="20261005")
        for model in ("NEW ECO", "Final", "W2"):
            self.assertIn(f"{model}（", rep["body"])
            self.assertIn(am.MODEL_NOTE[model][:10], rep["body"])
        self.assertIn("今日成交 1 笔", rep["subject"])
        # Final 有成交、W2 无成交（今日成交列显示 0）
        self.assertIn(">1</td>", rep["body"])

    def test_report_ok_without_trades(self):
        rep = am.build_daily_report(_payload([_acc()]), [], today8="20261005")
        self.assertIn("今日无成交", rep["subject"])
        self.assertEqual(rep["tone"], "ok")


class TestBriefAccountsSection(unittest.TestCase):
    """08:30 今日待办新增「其他账户」节的渲染契约：有键出节、缺键不出。"""

    def _send(self, brief):
        from app import notify
        captured = {}

        def fake_send(cfg, msg, tag):
            captured["subject"] = str(msg["Subject"])
            captured["body"] = msg.get_payload(decode=True).decode("utf-8")
            captured["tag"] = tag
            return True

        with mock.patch.object(notify.cfg_mod, "load_config", return_value=_cfg()), \
             mock.patch.object(notify, "_send_with_retry", side_effect=fake_send):
            ok = notify.send_daily_brief_email(brief)
        return ok, captured

    _MINIMAL = {"today": "20261005", "is_open": True, "pending": [], "signal": None,
                "daily_last": "20261004", "open_days_late": 0, "mail_fail_24h": 0,
                "ops_7d": 0, "paused": False, "halt": {}, "halt_unknown": False}

    def test_section_rendered_when_key_present(self):
        brief = {**self._MINIMAL,
                 "accounts_actions": [{"account_id": "fterm-w2",
                                       "account_name": "Final 2万档",
                                       "model": "Final", "level": "ok",
                                       "text": "今晚 20:05 档将按今日开盘价撮合 1 笔挂单"}]}
        ok, captured = self._send(brief)
        self.assertTrue(ok)
        self.assertIn("其他账户（Final / W2）", captured["body"])
        self.assertIn("Final 2万档", captured["body"])
        self.assertIn("撮合 1 笔挂单", captured["body"])

    def test_section_absent_when_key_missing(self):
        ok, captured = self._send(dict(self._MINIMAL))
        self.assertTrue(ok)
        self.assertNotIn("其他账户（Final / W2）", captured["body"])


if __name__ == "__main__":
    unittest.main()


class TestPicksEmail(unittest.TestCase):
    """三模型选股交集早报（2026-10-08）：组装纯函数 + 发送去重，从不碰真 smtplib。"""

    @staticmethod
    def _picks_payload(n_triple=1, ok_all=True):
        def picks(codes):
            return [{"rank": i + 1, "code": c, "name": None} for i, c in enumerate(codes)]
        ok = {"ok": True, "n": 3, "picks": picks(["A", "B", "C"]), "error": None}
        out = {"generated_at": "2026-10-09T08:35:00", "top_n": 100,
               "models": {"NEW ECO": dict(ok, asof="2026-09-30"),
                          "Final": dict(ok, asof="2026-10-08"),
                          "W2": dict(ok, asof="2026-09-24")}}
        if not ok_all:
            out["models"]["Final"] = {"ok": False, "n": 0, "picks": [], "error": "EMA 未重建"}
        inter = mp._intersect(out["models"])
        if ok_all:
            if n_triple == 0:
                # 强制空三重：把 fterm 的 picks 与另两家错开
                out["models"]["Final"]["picks"] = picks(["D", "E", "F"])
            elif n_triple == 1:
                # 恰一只三重交集（A 在三家榜内）
                out["models"]["Final"]["picks"] = picks(["A", "D", "E"])
            inter = mp._intersect(out["models"])
        out["intersection"] = inter
        return out

    @mock.patch.object(am, "time", autospec=True)
    @mock.patch("app.model_picks.build_picks")
    def test_build_triple_positive(self, m_picks, m_time):
        m_time.strftime.return_value = "2026-10-09 08:35"
        m_picks.return_value = self._picks_payload(n_triple=1)
        rep = am.build_picks_email()
        self.assertIn("三模型共同入选 1 只", rep["subject"])
        self.assertEqual(rep["tone"], "ok")
        self.assertIn("强共识", rep["body"])   # action 由 _envelope 编进正文
        self.assertIn("<table", rep["body"])            # 三重交集表真的渲染了
        self.assertNotIn("榜缺席", rep["body"])

    @mock.patch.object(am, "time", autospec=True)
    @mock.patch("app.model_picks.build_picks")
    def test_build_triple_zero_is_ok_not_warn(self, m_picks, m_time):
        """空交集是结构性正常读数：tone 必须 ok，文案不得渲染成异常。"""
        m_time.strftime.return_value = "2026-10-09 08:35"
        m_picks.return_value = self._picks_payload(n_triple=0)
        rep = am.build_picks_email()
        self.assertIn("今日无三模型共同入选", rep["subject"])
        self.assertEqual(rep["tone"], "ok")
        self.assertIn("结构性正常读数", rep["body"])

    @mock.patch.object(am, "time", autospec=True)
    @mock.patch("app.model_picks.build_picks")
    def test_build_missing_model_named(self, m_picks, m_time):
        """单模型榜缺席：如实点名 + 交集只按可用榜计（warn，但两两交集仍给出）。"""
        m_time.strftime.return_value = "2026-10-09 08:35"
        m_picks.return_value = self._picks_payload(ok_all=False)
        rep = am.build_picks_email()
        self.assertIn("可用榜 2/3", rep["subject"])
        self.assertIn("EMA 未重建", rep["body"])
        self.assertIn("NEW ECO ∩ W2", rep["body"])
        self.assertEqual(rep["tone"], "warn")

    @mock.patch.object(am, "_send_with_retry", return_value=True)
    @mock.patch.object(am.cfg_mod, "load_config", return_value=_cfg())
    @mock.patch("app.model_picks.build_picks")
    def test_send_dedup_same_day(self, m_picks, m_cfg, m_send):
        """同日去重：notify_log 已有当日 ok=1 记录 → 不重复发送（与日报同款契约）。"""
        m_picks.return_value = self._picks_payload(n_triple=1)
        sent_rows = {"n": 1}
        conn = mock.MagicMock()
        conn.execute.return_value.fetchone.side_effect = lambda: (sent_rows["n"],)
        ctx = mock.MagicMock()
        ctx.__enter__ = mock.MagicMock(return_value=conn)
        ctx.__exit__ = mock.MagicMock(return_value=False)
        with mock.patch.object(am.db, "conn_ctx", return_value=ctx):
            self.assertFalse(am.send_picks_email())
        m_send.assert_not_called()
        sent_rows["n"] = 0
        with mock.patch.object(am.db, "conn_ctx", return_value=ctx):
            self.assertTrue(am.send_picks_email())
        m_send.assert_called_once()
        self.assertEqual(m_send.call_args[0][2], am.PICKS_TAG)   # tag 契约：开机补跑去重键


class TestMetaMarkerRoundTrip(unittest.TestCase):
    """2026-10-15 回归钉：去重标记必须**真正落库**。

    修的是"静默回滚"bug：`db.conn_ctx` 关闭连接不提交事务 ⇒ Python sqlite3 在 close
    时把 INSERT **静默回滚**（无异常/无日志）⇒ 重启后去重失效、全部告警/成交邮件重发
    （2026-10-15 晚实测 21:15/21:23/21:24/21:40 四组重发）。原 restart 测试把 `db`
    整体 mock 掉 —— 测的是逻辑流而非落库 wiring（"测试绿、功能死"又一实例）；本钉用
    **真库文件**走真实 conn_ctx，去掉 `conn.commit()` 必红（变异验证过）。
    """

    def test_marker_survives_new_connection(self):
        import sqlite3 as _sq
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as td:
            dbfile = Path(td) / "t.db"
            con = _sq.connect(dbfile)
            con.execute("CREATE TABLE model_meta (key TEXT PRIMARY KEY,"
                        " value TEXT, updated_at TEXT)")
            con.commit()
            con.close()
            old_path, old_dir = am.db.DB_PATH, am.db.DATA_DIR
            am.db.DB_PATH = dbfile
            am.db.DATA_DIR = Path(td)
            try:
                am._meta_marker_write("mail_alert:pin", "2026-10-15")
                # 新连接读（模拟服务重启后的新进程）—— 不回滚才算真落库
                con2 = _sq.connect(dbfile)
                row = con2.execute(
                    "SELECT value FROM model_meta WHERE key='mail_alert:pin'"
                ).fetchone()
                con2.close()
                self.assertEqual(row, ("2026-10-15",),
                                 "标记被静默回滚（close 未提交）⇒ 重启后去重失效")
                self.assertTrue(am._meta_marker_sent("mail_alert:pin", "2026-10-15"))
            finally:
                am.db.DB_PATH = old_path
                am.db.DATA_DIR = old_dir
