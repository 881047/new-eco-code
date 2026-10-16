"""tests/test_llm_monthly_review.py — P3 LLM 月度复盘的纯函数单测。

覆盖：月度事实单构建（窗口过滤/键格式/NAV 计算）/ 机械异常检测（止损买回-光韵达
事故类、pending 陈旧、失败告警、场外买入、回撤越线）/ 引用校验（幻觉计零）。
不联网、不调 LLM、不写 data/llm/。
"""
import importlib.util
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

_spec = importlib.util.spec_from_file_location(
    "llm_review", BASE / "scripts" / "llm_monthly_review.py")
ob = importlib.util.module_from_spec(_spec)
sys.modules["llm_review"] = ob
_spec.loader.exec_module(ob)

_SCHEMA = """
CREATE TABLE plan (plan_date TEXT, plan_type TEXT, ts_code TEXT, name TEXT,
    weight REAL, shares INTEGER, reason TEXT, industry TEXT, fund_type TEXT);
CREATE TABLE paper_order (id INTEGER PRIMARY KEY, order_date TEXT, ts_code TEXT,
    name TEXT, side TEXT, shares INTEGER, reason TEXT, status TEXT, first_attempt TEXT,
    created_at TEXT);
CREATE TABLE paper_trade (id INTEGER PRIMARY KEY, exec_date TEXT, ts_code TEXT,
    name TEXT, side TEXT, shares INTEGER, price REAL, cost REAL, amount REAL,
    reason TEXT);
CREATE TABLE signal (signal_date TEXT, ts_code TEXT, name TEXT, action TEXT, reason TEXT,
    PRIMARY KEY (signal_date, ts_code, action)) WITHOUT ROWID;
CREATE TABLE notify_log (id INTEGER PRIMARY KEY, created_at TEXT, tag TEXT,
    ok INTEGER, error TEXT);
CREATE TABLE pipeline_run (id INTEGER PRIMARY KEY, started_at TEXT, ended_at TEXT,
    stage TEXT, run_date TEXT, status TEXT, rows INTEGER, error TEXT);
CREATE TABLE paper_nav (trade_date TEXT PRIMARY KEY, nav REAL, cash REAL,
    market_value REAL, bench_nav REAL);
CREATE TABLE paper_position (ts_code TEXT, name TEXT, shares INTEGER,
    avg_cost REAL, entry_adj REAL);
CREATE TABLE paper_account (id INTEGER, cash REAL, created_at TEXT,
    last_applied_plan TEXT, initial_capital REAL);
"""


class _Base(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript(_SCHEMA)

    def tearDown(self):
        self.conn.close()

    def seed(self):
        c = self.conn
        c.executemany("INSERT INTO plan VALUES (?,?,?,?,?,?,?,?,?)", [
            ("20260930", "stock", "002245", "光韵达", 0.16, 300, "月度方案买入", "电子", None),
            ("20260831", "stock", "600000", "浦发银行", 0.16, 500, "月度方案买入", "银行", None),
        ])
        c.executemany("INSERT INTO paper_order VALUES (?,?,?,?,?,?,?,?,?,?)", [
            (1, "20260905", "002245", "光韵达", "buy", 300, "月度方案买入", "done", "20260905", None),
            (2, "20260928", "002245", "光韵达", "sell", 300, "止损: 较买入成本 -15.5% (阈值 -15%)", "done", "20260928", None),
            (3, "20260929", "002245", "光韵达", "buy", 200, "月度方案买入", "pending", "20260929", None),
            (4, "20260901", "510300", "沪深300ETF", "buy", 100, "月度方案买入", "pending", "20260901", None),
        ])
        c.executemany("INSERT INTO paper_trade VALUES (?,?,?,?,?,?,?,?,?,?)", [
            (1, "20260905", "002245", "光韵达", "buy", 300, 10.5, 5.0, 3150, "月度方案买入"),
            (2, "20260928", "002245", "光韵达", "sell", 300, 8.9, 5.0, -2670, "止损: 较买入成本 -15.5% (阈值 -15%)"),
            (3, "20260929", "002245", "光韵达", "buy", 200, 10.2, 5.0, 2040, "月度方案买入"),
            (4, "20260915", "601111", "中国国航", "buy", 100, 7.0, 5.0, 700, "手动试仓"),
        ])
        c.executemany("INSERT INTO signal VALUES (?,?,?,?,?)", [
            ("20260928", "002245", "光韵达", "sell", "止损触发"),
            ("20260929", "002245", "光韵达", "buy_candidate", "候选替换"),
            ("20260910", "600000", "浦发银行", "watch", "观察"),
        ])
        c.executemany("INSERT INTO notify_log VALUES (?,?,?,?,?)", [
            (1, "2026-09-05 18:01:00", "模拟盘日终汇总", 1, None),
            (2, "2026-09-28 09:40:00", "运维告警", 0, "SMTP auth failed"),
        ])
        c.executemany("INSERT INTO pipeline_run VALUES (?,?,?,?,?,?,?,?)", [
            (1, "2026-09-30 18:33:01", None, "monthly", "20260930", "ok", 6, None),
            (2, "2026-09-15 09:40:01", None, "monitor", "20260915", "error", 0, "quote timeout"),
        ])
        c.executemany("INSERT INTO paper_nav VALUES (?,?,?,?,?)", [
            ("20260901", 20000.0, 5000.0, 15000.0, 1000.0),
            ("20260915", 19000.0, 5000.0, 14000.0, 995.0),
            ("20260930", 19600.0, 4600.0, 15000.0, 1002.0),
        ])
        c.executemany("INSERT INTO paper_position VALUES (?,?,?,?,?)", [
            ("002245", "光韵达", 200, 10.2, 1.0),
        ])
        c.execute("INSERT INTO paper_account VALUES (1, 4600.0, NULL, '20260930', 20000.0)")
        c.commit()

    def facts(self):
        self.seed()
        return ob.build_facts(self.conn, "2026-09")


class TestBuildFacts(_Base):
    def test_month_window_filters(self):
        f = self.facts()
        self.assertEqual([p["plan_date"] for p in f["plan"]], ["20260930"],
                         "8 月方案不进 9 月事实单")
        self.assertEqual(len(f["orders"]), 4)
        self.assertEqual(len(f["trades"]), 4)
        self.assertEqual(f["signal_summary"], {"buy_candidate": 1, "sell": 1, "watch": 1},
                         "summary 含全量计数；watch 只是不进逐条明细")
        self.assertEqual(len(f["signals"]), 2, "逐条明细排除 watch")

    def test_keys_are_referencable(self):
        f = self.facts()
        self.assertIn("order:2", ob.fact_keys(f))
        self.assertIn("plan:20260930:002245", ob.fact_keys(f))
        self.assertIn("notify:2", ob.fact_keys(f))
        self.assertIn("nav:20260930", ob.fact_keys(f))

    def test_nav_metrics(self):
        nav = self.facts()["nav"]
        self.assertAlmostEqual(nav["ret"], -0.02, places=6)
        self.assertAlmostEqual(nav["mdd"], -0.05, places=6)          # 20000→19000
        self.assertAlmostEqual(nav["bench_ret"], 0.002, places=6)


class TestMechanicalChecks(_Base):
    def checks(self):
        return {c["check"]: c for c in ob.mechanical_checks(self.facts())}

    def test_stop_loss_rebuy_catches_guangyunda_class(self):
        c = self.checks()
        self.assertFalse(c["stop_loss_fired"]["ok"], "止损卖出应被抓到")
        self.assertFalse(c["stop_loss_rebuy"]["ok"], "止损后同票再买 = 光韵达事故类，必须命中")
        self.assertTrue(any("trade:2" in x for x in c["stop_loss_rebuy"]["citations"]))

    def test_pending_and_stale(self):
        c = self.checks()
        self.assertFalse(c["pending_orders"]["ok"], "4 张 pending 应报告")
        self.assertFalse(c["stale_pending"]["ok"], "9-01 挂的 pending 已超 5 天 = 陈旧")
        self.assertTrue(any("order:4" in x for x in c["stale_pending"]["citations"]))

    def test_failed_alerts_and_pipeline(self):
        c = self.checks()
        self.assertFalse(c["failed_alerts"]["ok"])
        self.assertFalse(c["pipeline_errors"]["ok"])
        self.assertTrue(any("pipeline:2" in x for x in c["pipeline_errors"]["citations"]))

    def test_off_plan_buy_flagged_but_plan_buy_not(self):
        c = self.checks()
        self.assertFalse(c["off_plan_buys"]["ok"], "'手动试仓' 是场外买入")
        self.assertTrue(any("trade:4" in x for x in c["off_plan_buys"]["citations"]))

    def test_nav_thresholds(self):
        c = self.checks()
        self.assertTrue(c["nav_mdd"]["ok"], "-5% 未越 -8% 线")
        self.assertTrue(c["underperformance"]["ok"], "-1.8pp 未越 -3pp 线")

    def test_clean_month_all_ok(self):
        # 全删 9 月交易/告警/错误后应全绿
        self.seed()
        c = self.conn
        c.execute("DELETE FROM paper_trade")
        c.execute("DELETE FROM notify_log WHERE ok=0")
        c.execute("DELETE FROM pipeline_run WHERE status='error'")
        c.execute("UPDATE paper_order SET status='done'")
        c.execute("UPDATE paper_order SET first_attempt='20260920'")
        c.commit()
        out = ob.mechanical_checks(ob.build_facts(c, "2026-09"))
        self.assertTrue(all(x["ok"] for x in out),
                        f"干净月应全绿: {[x['check'] for x in out if not x['ok']]}")


class TestVerifyFindings(_Base):
    def test_hallucinated_citation_dropped(self):
        f = self.facts()
        findings = [
            {"severity": "high", "claim": "a", "citations": ["order:2"], "hypothesis": "h"},
            {"severity": "mid", "claim": "b", "citations": ["order:999"], "hypothesis": ""},
            {"severity": "low", "claim": "c", "citations": [], "hypothesis": ""},
        ]
        kept, dropped = ob.verify_findings(f, findings)
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["claim"], "a")
        self.assertEqual(len(dropped), 2)
        self.assertIn("引用不存在", dropped[0]["reason"])

    def test_every_check_citation_resolves(self):
        f = self.facts()
        keys = ob.fact_keys(f)
        for chk in ob.mechanical_checks(f):
            for cite in chk["citations"]:
                self.assertIn(cite, keys, f"{chk['check']} 引用了不存在的事实键 {cite}")


if __name__ == "__main__":
    unittest.main()
