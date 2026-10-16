"""审查修复单测: 推分特征列序 / 方案资金兜底 / 监控复权缺失降级 (2026-09-04)

运行: python -m unittest tests.test_review_fixes -v
"""
import contextlib
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lightgbm as lgb
import numpy as np
import pandas as pd

from app import db
from app.models import daily_score as ds
from app.portfolio import builder
from app.portfolio import monitor

_SCHEMA = """
CREATE TABLE stock_basic (ts_code TEXT PRIMARY KEY, symbol TEXT, name TEXT, area TEXT,
    industry TEXT, market TEXT, list_date TEXT, delist_date TEXT);
CREATE TABLE daily (ts_code TEXT, trade_date TEXT, open REAL, high REAL, low REAL,
    close REAL, pre_close REAL, change REAL, pct_chg REAL, vol REAL, amount REAL);
CREATE TABLE factor (ts_code TEXT, trade_date TEXT, factor TEXT, value REAL,
    PRIMARY KEY (ts_code, trade_date, factor)) WITHOUT ROWID;
CREATE TABLE model_score (ts_code TEXT, trade_date TEXT, score REAL, pred_ret REAL,
    PRIMARY KEY (ts_code, trade_date)) WITHOUT ROWID;
CREATE TABLE daily_score (ts_code TEXT, trade_date TEXT, score REAL, pred_ret REAL,
    PRIMARY KEY (ts_code, trade_date)) WITHOUT ROWID;
CREATE TABLE plan (plan_date TEXT, plan_type TEXT, ts_code TEXT, name TEXT,
    weight REAL, shares INTEGER, industry TEXT, reason TEXT,
    PRIMARY KEY (plan_date, plan_type, ts_code)) WITHOUT ROWID;
CREATE TABLE regime (trade_date TEXT PRIMARY KEY, state TEXT, score REAL,
    drawdown REAL, vol REAL);
CREATE TABLE signal (signal_date TEXT, ts_code TEXT, name TEXT, action TEXT, reason TEXT,
    PRIMARY KEY (signal_date, ts_code, action)) WITHOUT ROWID;
CREATE TABLE adj_factor (ts_code TEXT, trade_date TEXT, adj_factor REAL);
CREATE TABLE daily_basic (ts_code TEXT, trade_date TEXT, total_mv REAL, circ_mv REAL);
CREATE TABLE repo_rate (trade_date TEXT PRIMARY KEY, overnight REAL, r7d REAL, r14d REAL);
CREATE TABLE trade_cal (cal_date TEXT PRIMARY KEY, is_open INTEGER);
CREATE TABLE paper_position (ts_code TEXT PRIMARY KEY, name TEXT, shares INTEGER,
                             avg_cost REAL, entry_adj REAL);
CREATE TABLE paper_trade (id INTEGER PRIMARY KEY AUTOINCREMENT, exec_date TEXT,
                          ts_code TEXT, name TEXT, side TEXT, shares INTEGER,
                          price REAL, cost REAL, amount REAL, reason TEXT);
"""

T = "20260904"


class _DBBase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.path = tmp.name
        tmp.close()
        conn = sqlite3.connect(self.path)
        conn.executescript(_SCHEMA)
        conn.commit()
        conn.close()
        self.patch_db = mock.patch.object(
            db, "get_conn", side_effect=lambda: sqlite3.connect(self.path))
        self.patch_db.start()

    def tearDown(self):
        self.patch_db.stop()
        os.unlink(self.path)

    def _c(self):
        return sqlite3.connect(self.path)

    def _exec(self, sql, args=()):
        conn = self._c()
        conn.execute(sql, args)
        conn.commit()
        conn.close()

    def _one(self, sql, args=()):
        conn = self._c()
        row = conn.execute(sql, args).fetchone()
        conn.close()
        return row

    def _rows(self, sql, args=()):
        conn = self._c()
        rows = conn.execute(sql, args).fetchall()
        conn.close()
        return rows


class TestScoreFeatureOrder(_DBBase):
    def test_predict_uses_model_feature_order(self):
        # mom_20/rev_5 的字母序 ≠ ALL_FACTORS 计算序; 旧代码按计算序喂列位置 → 预测错位
        # 样本/轮数足够多, 模型才对输入敏感(60行/20轮时两序预测会重合, 佐证失效)
        rng = np.random.default_rng(7)
        Xtr = pd.DataFrame({"mom_20": rng.normal(size=200), "rev_5": rng.normal(size=200)})
        y = 3.0 * Xtr["mom_20"] - 1.0 * Xtr["rev_5"] + rng.normal(scale=0.01, size=200)
        model = lgb.train({"objective": "regression", "verbose": -1},
                          lgb.Dataset(Xtr, y), num_boost_round=50)
        tmp_model = Path(self.path).with_suffix(".model")
        model.save_model(str(tmp_model))

        self._exec("INSERT INTO stock_basic (ts_code, name) VALUES ('600000.SH','浦发银行')")
        # 2026-09-12 P1-04 起: score_today 的市值/流动性过滤改为**缺失即报错**(不再静默跳过
        # 整层)。故夹具必须像生产一样同时给 amount 与 daily_basic.total_mv, 否则推分直接被拒。
        self._exec("INSERT INTO daily (ts_code, trade_date, close, vol, pct_chg, amount) "
                   "VALUES ('600000.SH',?,10.0,1000,0.5,1000.0)", (T,))
        self._exec("INSERT INTO daily_basic (ts_code, trade_date, total_mv) "
                   "VALUES ('600000.SH',?,100000.0)", (T,))
        # factor 行顺序无关紧要, 但列序由 reindex 决定
        self._exec("INSERT INTO factor (ts_code, trade_date, factor, value) VALUES ('600000.SH',?,'mom_20',1.0)", (T,))
        self._exec("INSERT INTO factor (ts_code, trade_date, factor, value) VALUES ('600000.SH',?,'rev_5',5.0)", (T,))
        with mock.patch.object(ds, "_model_files", lambda: [tmp_model]):
            ds.score_today(T)
        pred = self._one("SELECT pred_ret FROM daily_score WHERE ts_code='600000.SH'")[0]
        expected = model.predict(pd.DataFrame([{"mom_20": 1.0, "rev_5": 5.0}]))[0]
        self.assertAlmostEqual(pred, expected, places=6)
        # 佐证: 错误列序下的预测确实不同(测试本身有效)
        wrong = model.predict(pd.DataFrame([{"rev_5": 5.0, "mom_20": 1.0}]))[0]
        self.assertNotAlmostEqual(wrong, expected, places=6)


class TestBuilderCarryOver(_DBBase):
    def _cfg(self):
        return {"capital": 10000, "max_stocks": 5, "industry_max_weight": 0.25,
                "risk_profile": "aggressive", "single_side_cost": 0.003,
                "regime_equity_weight": {"aggressive": {"attack": 1.0, "neutral": 0.9,
                                                        "defense": 0.8}},
                "email": {}}

    def _seed(self):
        self._exec("INSERT INTO model_score VALUES ('600000.SH','20260831',95.0,0.02)")
        self._exec("INSERT INTO stock_basic (ts_code, name, industry) VALUES ('600000.SH','浦发银行','银行')")
        self._exec("INSERT INTO regime VALUES ('20260831','attack',1.0,NULL,NULL)")
        # 夹具须带 amount(R11-3 流动性域) —— 生产 daily 有该列, 缺了会把截面滤空
        self._exec("INSERT INTO daily (ts_code, trade_date, close, vol, pct_chg, amount) "
                   "VALUES ('600000.SH','20260831',10.0,1000,0.5,5e4)")
        # 2026-09-28 builder R11-3 fail-closed 起: daily_basic 表在而无行会拒绝建仓,
        # 夹具须像生产一样给 total_mv(与 daily_score P1-04 的夹具先例同款)
        self._exec("INSERT INTO daily_basic VALUES ('600000.SH','20260831',50.0,40.0)")
        self._exec("INSERT INTO plan VALUES ('20260731','stock','600000.SH','浦发银行',"
                   "0.4,400,'银行','模型打分 95.0 (银行)')")
        self._exec("INSERT INTO repo_rate VALUES ('20260831',1.5,1.6,1.7)")

    def _patches(self):
        return [mock.patch.object(builder.cfg_mod, "load_config", return_value=self._cfg()),
                mock.patch.object(builder, "allocate_lots",
                                  return_value={"n_eff": 0, "shares": {}, "weights": {},
                                                "cost": 0.0}),
                mock.patch.object(builder.funds, "score_etfs", return_value=pd.DataFrame()),
                mock.patch.object(builder.funds, "otc_fund_picks", return_value={}),
                mock.patch.object(builder.repo, "repo_advice",
                                  return_value={"desc": "x", "rate": 1.5})]

    def test_no_raise_carries_over_prev_stocks(self):
        self._seed()  # 上月方案在 20260731
        with contextlib.ExitStack() as st:
            for p in self._patches():
                st.enter_context(p)
            summary = builder.build_plan("20260831")
        self.assertEqual(len(summary["stocks"]), 1)
        self.assertEqual(summary["stocks"][0]["ts_code"], "600000.SH")
        self.assertIn("沿用", summary["stocks"][0]["reason"])
        self.assertIn("沿用上月", summary["warning"])
        row = self._one("SELECT shares FROM plan WHERE plan_type='stock' AND plan_date='20260831'")
        self.assertEqual(row[0], 400)

    def test_no_prev_plan_cash_only(self):
        self._exec("INSERT INTO model_score VALUES ('600000.SH','20260831',95.0,0.02)")
        self._exec("INSERT INTO stock_basic (ts_code, name, industry) VALUES ('600000.SH','浦发银行','银行')")
        self._exec("INSERT INTO regime VALUES ('20260831','attack',1.0,NULL,NULL)")
        self._exec("INSERT INTO daily (ts_code, trade_date, close, vol, pct_chg, amount) "
                   "VALUES ('600000.SH','20260831',10.0,1000,0.5,5e4)")
        self._exec("INSERT INTO daily_basic VALUES ('600000.SH','20260831',50.0,40.0)")
        self._exec("INSERT INTO repo_rate VALUES ('20260831',1.5,1.6,1.7)")
        with contextlib.ExitStack() as st:
            for p in self._patches():
                st.enter_context(p)
            summary = builder.build_plan("20260831")
        self.assertEqual(summary["stocks"], [])
        self.assertIn("仅现金/基金", summary["warning"])

    def test_r113_fail_closed_when_daily_basic_missing_rows(self):
        """2026-09-28 审查: daily_basic 表在而 plan_date 无行时, builder 原**静默降级**
        为"无过滤域"选股(推分域/建仓域分叉, 与 daily_score P1-04 同型) —— 现改 fail-closed。"""
        self._exec("INSERT INTO model_score VALUES ('600000.SH','20260831',95.0,0.02)")
        self._exec("INSERT INTO stock_basic (ts_code, name, industry) VALUES ('600000.SH','浦发银行','银行')")
        self._exec("INSERT INTO regime VALUES ('20260831','attack',1.0,NULL,NULL)")
        self._exec("INSERT INTO daily (ts_code, trade_date, close, vol, pct_chg) "
                   "VALUES ('600000.SH','20260831',10.0,1000,0.5)")
        # daily_basic 表存在(_DBBase 建表)但**不种行** ⇒ 必须拒绝建仓
        self._exec("INSERT INTO repo_rate VALUES ('20260831',1.5,1.6,1.7)")
        with contextlib.ExitStack() as st:
            for p in self._patches():
                st.enter_context(p)
            with self.assertRaises(RuntimeError):
                builder.build_plan("20260831")


class TestMonitorAdjMissing(_DBBase):
    def _cfg(self):
        return {"dynamic": {"enabled": True, "stop_loss": -0.15, "weak_pct": 40,
                            "swap_drawdown": -0.08, "candidate_top": 20,
                            "min_hold_days": 3, "weak_confirm_days": 2, "cooldown_days": 5},
                "risk_profile": "aggressive",
                "regime_equity_weight": {"aggressive": {"attack": 1.0}},
                "email": {}}

    def _seed(self):
        # 监控基准改为模拟盘真实持仓 (P2-4)
        self._exec("INSERT INTO paper_position VALUES ('600000.SH','浦发银行',100,10.0,NULL)")
        self._exec("INSERT INTO paper_trade (exec_date, ts_code, side) VALUES ('20260701','600000.SH','buy')")
        self._exec("INSERT INTO stock_basic (ts_code, name, industry) VALUES ('600000.SH','浦发银行','银行')")
        self._exec("INSERT INTO daily_score VALUES ('600000.SH',?,90.0,0.01)", (T,))
        self._exec("INSERT INTO daily (ts_code, trade_date, close, vol, pct_chg) "
                   "VALUES ('600000.SH','20260831',10.0,1000,0.0)")
        self._exec("INSERT INTO daily (ts_code, trade_date, close, vol, pct_chg) "
                   "VALUES ('600000.SH',?,5.0,1000,-1.0)", (T,))  # 原价口径回撤 -50%
        self._exec("INSERT INTO regime VALUES ('20260831','attack',1.0,NULL,NULL)")
        self._exec("INSERT INTO regime VALUES (?,?,1.0,NULL,NULL)", (T, "attack"))

    def test_adj_missing_downgrades_to_warn(self):
        # 当日复权因子缺失: 原价 -50% 不得触发止损, 降级告警
        self._seed()
        with mock.patch.object(monitor.cfg_mod, "load_config", return_value=self._cfg()):
            out = monitor.run_monitor(T)
        acts = {r["ts_code"]: r for r in out["items"]}
        self.assertIn("600000.SH", acts)
        self.assertEqual(acts["600000.SH"]["action"], "warn")
        self.assertIn("复权因子缺失", acts["600000.SH"]["reason"])
        self.assertNotIn("sell", {r["action"] for r in out["items"]})  # 不得触发止损
        self.assertTrue(out["has_action"])  # warn 属风险告警(会发邮件), 属预期

    def test_adj_present_triggers_stop_loss(self):
        # 对照组: 复权因子齐备时, -50% 应触发止损(实际买入成本基准)
        self._seed()
        self._exec("UPDATE paper_position SET entry_adj=1.0 WHERE ts_code='600000.SH'")
        self._exec("INSERT INTO adj_factor VALUES ('600000.SH',?,1.0)", (T,))
        with mock.patch.object(monitor.cfg_mod, "load_config", return_value=self._cfg()):
            out = monitor.run_monitor(T)
        acts = {r["ts_code"]: r for r in out["items"]}
        self.assertEqual(acts["600000.SH"]["action"], "sell")


class TestMonitorExplicitDateFallback(_DBBase):
    """2026-09-28 审查: 显式传 trade_date 时打分源原硬编码 daily_score —— 该日只有
    model_score 行(月末锚点补跑场景)会以"打分或持仓为空"**静默无产出**。
    修复后与自动路径对称: 先探日频, 无行回退月度表。"""

    def test_model_score_fallback_on_explicit_date(self):
        self._exec("INSERT INTO paper_position VALUES ('600000.SH','浦发银行',100,10.0,1.0)")
        self._exec("INSERT INTO paper_trade (exec_date, ts_code, side) VALUES ('20260701','600000.SH','buy')")
        self._exec("INSERT INTO stock_basic (ts_code, name, industry) VALUES ('600000.SH','浦发银行','银行')")
        self._exec("INSERT INTO model_score VALUES ('600000.SH',?,90.0,0.01)", (T,))
        self._exec("INSERT INTO daily (ts_code, trade_date, close, vol, pct_chg) "
                   "VALUES ('600000.SH',?,5.0,1000,-1.0)", (T,))  # 成本基准 -50%
        self._exec("INSERT INTO adj_factor VALUES ('600000.SH',?,1.0)", (T,))
        self._exec("INSERT INTO regime VALUES (?,?,1.0,NULL,NULL)", (T, "attack"))
        with mock.patch.object(monitor.cfg_mod, "load_config", return_value={
                "dynamic": {"enabled": True, "stop_loss": -0.15, "weak_pct": 40,
                            "swap_drawdown": -0.08, "candidate_top": 20,
                            "min_hold_days": 3, "weak_confirm_days": 2, "cooldown_days": 5},
                "risk_profile": "aggressive",
                "regime_equity_weight": {"aggressive": {"attack": 1.0}},
                "email": {}}):
            out = monitor.run_monitor(T)
        acts = {r["ts_code"]: r["action"] for r in out["items"]}
        self.assertEqual(acts.get("600000.SH"), "sell")  # 回退 model_score 后正常触发止损


class TestMonitorWeakGuard(_DBBase):
    """P2-3 回归: 弱势替换的持有期保护/连续确认/同股同动作冷却"""

    def _cfg(self):
        return {"dynamic": {"enabled": True, "stop_loss": -0.15, "weak_pct": 40,
                            "swap_drawdown": -0.08, "candidate_top": 5,
                            "min_hold_days": 3, "weak_confirm_days": 2, "cooldown_days": 5},
                "risk_profile": "aggressive",
                "regime_equity_weight": {"aggressive": {"attack": 1.0}},
                "email": {}}

    def _seed_held(self, entry_date="20260701"):
        self._exec("INSERT INTO paper_position VALUES ('600000.SH','浦发银行',100,10.0,1.0)")
        self._exec("INSERT INTO paper_trade (exec_date, ts_code, side) VALUES (?,?, 'buy')",
                   (entry_date, "600000.SH"))
        self._exec("INSERT INTO stock_basic (ts_code, name, industry) VALUES ('600000.SH','浦发银行','银行')")
        self._exec("INSERT INTO daily_score VALUES ('600000.SH',?,1.0,0.01)", (T,))  # 分位 1 < 40
        self._exec("INSERT INTO daily (ts_code, trade_date, close, vol, pct_chg) "
                   "VALUES ('600000.SH',?,9.1,1000,-1.0)", (T,))  # 较买入成本 -9%
        self._exec("INSERT INTO adj_factor VALUES ('600000.SH',?,1.0)", (T,))
        for d in ("20260831", "20260901", "20260902", "20260903", T):
            self._exec("INSERT INTO regime VALUES (?,?,1.0,NULL,NULL)", (d, "attack"))
        self._exec("INSERT INTO trade_cal VALUES ('20260831',1)")
        self._exec("INSERT INTO trade_cal VALUES ('20260901',1)")
        self._exec("INSERT INTO trade_cal VALUES ('20260902',1)")
        self._exec("INSERT INTO trade_cal VALUES ('20260903',1)")
        self._exec("INSERT INTO trade_cal VALUES (?,1)", (T,))

    def _run(self):
        with mock.patch.object(monitor.cfg_mod, "load_config", return_value=self._cfg()):
            return monitor.run_monitor(T)

    def _acts(self, out):
        return {r["ts_code"]: r["action"] for r in out["items"]}

    def test_fresh_buy_exempt_from_weak_swap(self):
        # 买入仅 2 个交易日(min_hold=3): 弱势+回撤也不卖, 不发观察
        self._seed_held(entry_date="20260902")
        out = self._run()
        self.assertNotIn("600000.SH", self._acts(out))

    def test_weak_first_day_only_watch(self):
        # 持有超保护期, 弱势+回撤达标但无前日确认 → 只观察不卖
        self._seed_held()
        out = self._run()
        self.assertEqual(self._acts(out).get("600000.SH"), "watch")

    def test_weak_confirmed_second_day_sells(self):
        # 前一日已有 watch → 连续确认通过 → sell_weak
        self._seed_held()
        self._exec("INSERT INTO signal VALUES ('20260903','600000.SH','浦发银行','watch','x')")
        out = self._run()
        self.assertEqual(self._acts(out).get("600000.SH"), "sell_weak")

    def test_cooldown_suppresses_repeat_weak_sell(self):
        # 2 个交易日前刚 sell_weak 过(冷却 5 天): 今天不再重复发
        self._seed_held()
        self._exec("INSERT INTO signal VALUES ('20260901','600000.SH','浦发银行','sell_weak','x')")
        self._exec("INSERT INTO signal VALUES ('20260903','600000.SH','浦发银行','watch','x')")
        out = self._run()
        self.assertNotIn("sell_weak", self._acts(out).values())

    def test_suspension_warn_cooldown(self):
        # 停牌告警同股 5 交易日内不重复(原天天刷邮件)
        self._seed_held()
        self._exec("UPDATE daily SET vol=0 WHERE ts_code='600000.SH'")
        self._exec("INSERT INTO signal VALUES ('20260902','600000.SH','浦发银行','warn','停牌')")
        out = self._run()
        self.assertNotIn("warn", self._acts(out).values())


if __name__ == "__main__":
    unittest.main()
