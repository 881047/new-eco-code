# -*- coding: utf-8 -*-
"""chase 建仓追高剔除闸单元测试 —— 单一实现 `app/portfolio/chase.py`。

口径冻结于 plans/risk-overlay / chase6-fullhist / chase6-round3 三份预注册
（05 号台账 七补/九补）: chase_z = max(z(mom5), z(devvwap)), θ=2.0, 剔除不补位。
这里钉的是**机制**（z 公式 / max 组合 / 无前视 / 开关语义 / builder 接线）,
生产读数由 pin 重钉钉住。
"""
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd

from app import db as app_db
from app.portfolio import builder
from app.portfolio.chase import ChaseGate, enabled, theta_of


def _tiny_m(n_days=30, n_codes=10, ramp_code=None, seed=5, future_days=0):
    """小面板: n_codes 只票、量价随机游走; ramp_code 指定的票在锚点前 5 个交易日
    逐日抬升(追高形态)。future_days>0 时在锚点之后再补 N 天狂变行情
    (用于无前视测试 —— 锚点读数不得因此改变)。锚点 = 第 n_days-1 天。"""
    rng = np.random.default_rng(seed)
    n_total = n_days + future_days
    idx = pd.bdate_range("2022-01-03", periods=n_total)
    cols = [f"{600000 + i}.SH" for i in range(n_codes)]
    close = pd.DataFrame(
        10.0 + rng.normal(0, 0.05, size=(n_total, n_codes)).cumsum(axis=0),
        index=idx, columns=cols)
    if ramp_code is not None:
        factors = np.array([1.08 ** k for k in range(1, 6)])
        close.loc[idx[n_days - 5: n_days], ramp_code] = \
            close.loc[idx[n_days - 5: n_days], ramp_code].to_numpy() * factors
        if future_days:
            close.loc[idx[n_days:], ramp_code] = close.loc[idx[n_days:], ramp_code] * 1.5
    m = {k: pd.DataFrame(1.0, index=idx, columns=cols)
         for k in ("open", "high", "low", "pre_close", "pb", "pe_ttm",
                   "turnover", "total_mv", "circ_mv", "adj")}
    m["close"] = close
    m["vol"] = pd.DataFrame(1e5, index=idx, columns=cols)
    m["amount"] = pd.DataFrame(1e6, index=idx, columns=cols)
    if future_days:
        m["close"].loc[idx[n_days:]] = m["close"].loc[idx[n_days:]] * 3.0
    return m


def _scores(m):
    return pd.Series(1.0, index=list(m["close"].columns))


def _normal_code(m):
    """挑一只时点名非 ST/退 的票(测试库名称链含真实数据, 600003 是 ST东北高)。"""
    from app.models.names import NameLookup
    nl = NameLookup()
    dstr = m["close"].index[-1].strftime("%Y%m%d")
    for c in m["close"].columns:
        if not nl.is_st(c, dstr):
            return c
    raise AssertionError("测试面板里没有非 ST 码")


class TestChaseGate(unittest.TestCase):
    ANCHOR = 29  # 0 基; 30 天面板的最后一天

    def test_high_momentum_excluded_others_not(self):
        ramp = _normal_code(_tiny_m())
        m = _tiny_m(ramp_code=ramp)
        d = m["close"].index[self.ANCHOR]
        excl = ChaseGate(m).exclusions(d, _scores(m), theta=2.0)
        self.assertIn(ramp, excl)
        others = set(m["close"].columns) - {ramp}
        self.assertEqual(excl & others, set())

    def test_theta_higher_fewer_exclusions(self):
        ramp = _normal_code(_tiny_m())
        m = _tiny_m(ramp_code=ramp)
        d = m["close"].index[self.ANCHOR]
        e2 = ChaseGate(m).exclusions(d, _scores(m), theta=2.0)
        e99 = ChaseGate(m).exclusions(d, _scores(m), theta=99.0)
        self.assertIn(ramp, e2)
        self.assertEqual(e99, set())

    def test_no_lookahead(self):
        """锚点之后加 10 天狂变行情 ⇒ 锚点剔除集逐位不变(无前视)。"""
        ramp = _normal_code(_tiny_m())
        m0 = _tiny_m(ramp_code=ramp)
        e0 = ChaseGate(m0).exclusions(m0["close"].index[self.ANCHOR],
                                      _scores(m0), theta=2.0)
        m1 = _tiny_m(ramp_code=ramp, future_days=10)
        e1 = ChaseGate(m1).exclusions(m1["close"].index[self.ANCHOR],
                                      _scores(m1), theta=2.0)
        self.assertEqual(e0, e1)

    def test_config_semantics(self):
        self.assertFalse(enabled({}))                       # 缺省关(测试/新装机路径)
        self.assertFalse(enabled({"chase": {}}))
        self.assertTrue(enabled({"chase": {"enabled": True}}))
        self.assertEqual(theta_of({}), 2.0)                 # theta 缺省 = 冻结值
        self.assertEqual(theta_of({"chase": {"theta": 2.5}}), 2.5)

    def test_short_panel_no_wraparound(self):
        """2026-09-28 审查: 锚点在面板前 5 行时 iloc[pos-5] 负索引回绕到面板**末行**
        (未来数据)。生产全历史面板永不触发, 但任何短面板/早期锚点调用方都会中招 ⇒
        钉住: 锚点不足 5 日历史 ⇒ 无信号可算, chase_z 为空、剔除集为空
        (fail-open, 与 S5"锚点不足不否决"同取向)。"""
        m = _tiny_m(n_days=30, n_codes=5, seed=5)
        early = m["close"].index[3]        # pos=3 < 5
        cz = ChaseGate(m).chase_z(early, _scores(m))
        self.assertEqual(len(cz), 0)
        excl = ChaseGate(m).exclusions(early, _scores(m), theta=2.0)
        self.assertEqual(excl, set())


_BUILDER_SCHEMA = """
CREATE TABLE stock_basic (ts_code TEXT, name TEXT, industry TEXT);
CREATE TABLE namechange (ts_code TEXT, start_date TEXT, end_date TEXT, name TEXT);
CREATE TABLE model_score (ts_code TEXT, trade_date TEXT, score REAL, pred_ret REAL);
CREATE TABLE daily (ts_code TEXT, trade_date TEXT, close REAL, pre_close REAL,
                    vol REAL, pct_chg REAL);
CREATE TABLE regime (trade_date TEXT, state TEXT);
CREATE TABLE repo_rate (trade_date TEXT, overnight REAL, r7d REAL, r14d REAL);
CREATE TABLE plan (plan_date TEXT, plan_type TEXT, ts_code TEXT, name TEXT,
                   weight REAL, shares INTEGER, industry TEXT, reason TEXT);
"""


class TestBuilderChaseWiring(unittest.TestCase):
    """builder 侧 chase 接线回归(2026-09-28 审查 P0 修复)。

    缺陷史: 原实现在 `set_index("ts_code")` **之前**用 `top.index.isin(_excl)` 过滤,
    而 top.index 是 read_sql 的 RangeIndex 整数、_excl 是 ts_code 字符串集合 ⇒ 恒不匹配
    ⇒ builder 侧 chase **静默空操作**(回测引擎侧按 ts_code 列过滤, 一直正确; 测试只测
    ChaseGate 本体, 全绿掩盖了生产接线失效)。chase 2026-09-26 采纳, 首个走该路径的
    月链是 2026-10-01 —— 修复赶在其之前。"""

    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.path = tmp.name
        tmp.close()
        conn = sqlite3.connect(self.path)
        conn.executescript(_BUILDER_SCHEMA)
        conn.execute("INSERT INTO stock_basic VALUES "
                     "('600000.SH','高分股','i0'),('600001.SH','对照股','i1')")
        conn.execute("INSERT INTO model_score VALUES "
                     "('600000.SH','20260831',99.0,0.01),('600001.SH','20260831',95.0,0.01)")
        conn.execute("INSERT INTO daily VALUES "
                     "('600000.SH','20260831',10.0,10.0,1e6,0.5),"
                     "('600001.SH','20260831',10.0,10.0,1e6,0.5)")
        conn.execute("INSERT INTO regime VALUES ('20260831','neutral')")
        conn.execute("INSERT INTO repo_rate VALUES ('20260831',1.5,1.6,1.7)")
        conn.commit()
        conn.close()
        self.patch_db = mock.patch.object(
            app_db, "get_conn", side_effect=lambda: sqlite3.connect(self.path))
        self.patch_db.start()
        self.patch_cfg = mock.patch.object(
            builder.cfg_mod, "load_config",
            return_value={"capital": 10000, "risk_profile": "aggressive",
                          "max_stocks": 6, "state_stocks": {"attack": 6, "neutral": 6, "defense": 6},
                          "stock_candidate_top": 0, "industry_max_weight": 0.25,
                          "single_side_cost": 0.003,
                          "chase": {"enabled": True, "theta": 2.0},
                          "regime_equity_weight": {"aggressive": {"attack": 1.0, "neutral": 1.0,
                                                                  "defense": 1.0}}})
        self.patch_cfg.start()

    def tearDown(self):
        self.patch_db.stop()
        self.patch_cfg.stop()
        os.unlink(self.path)

    def _run(self, excluded_codes):
        captured = {}

        def fake_alloc(top, equity_cash, max_stocks, **kw):
            captured["codes"] = sorted(top.index)
            return {"shares": {}, "weights": {}, "cost": 0.0, "n_eff": 0, "cash_left": equity_cash}

        gate = mock.MagicMock()
        gate.exclusions.return_value = set(excluded_codes)
        with mock.patch.object(builder, "allocate_lots", side_effect=fake_alloc), \
             mock.patch("app.portfolio.chase.ChaseGate", return_value=gate), \
             mock.patch("app.factors.factor_engine.load_market", return_value={}), \
             mock.patch.object(builder.funds, "score_etfs", return_value=pd.DataFrame()), \
             mock.patch.object(builder.funds, "otc_fund_picks", return_value={}), \
             mock.patch.object(builder.repo, "repo_advice",
                               return_value={"desc": "x", "rate": 1.5}):
            builder.build_plan("20260831")
        return captured

    def test_excluded_candidate_is_removed_from_plan(self):
        captured = self._run({"600000.SH"})
        self.assertEqual(captured["codes"], ["600001.SH"])

    def test_gate_not_consulted_when_disabled(self):
        cfg = self.patch_cfg
        # enabled=False ⇒ 不构造闸、不剔除
        self.patch_cfg.stop()
        self.patch_cfg = mock.patch.object(
            builder.cfg_mod, "load_config",
            return_value={"capital": 10000, "risk_profile": "aggressive",
                          "max_stocks": 6, "state_stocks": {"attack": 6, "neutral": 6, "defense": 6},
                          "stock_candidate_top": 0, "industry_max_weight": 0.25,
                          "single_side_cost": 0.003,
                          "regime_equity_weight": {"aggressive": {"attack": 1.0, "neutral": 1.0,
                                                                  "defense": 1.0}}})
        self.patch_cfg.start()
        captured = self._run({"600000.SH"})
        self.assertEqual(captured["codes"], ["600000.SH", "600001.SH"])


_E2E_SCHEMA = """
CREATE TABLE stock_basic (ts_code TEXT, name TEXT, industry TEXT, list_date TEXT);
CREATE TABLE namechange (ts_code TEXT, start_date TEXT, end_date TEXT, name TEXT);
CREATE TABLE model_score (ts_code TEXT, trade_date TEXT, score REAL, pred_ret REAL);
CREATE TABLE daily (ts_code TEXT, trade_date TEXT, close REAL, pre_close REAL,
                    vol REAL, pct_chg REAL);
CREATE TABLE regime (trade_date TEXT, state TEXT);
CREATE TABLE plan (plan_date TEXT, plan_type TEXT, ts_code TEXT, name TEXT,
                   weight REAL, shares INTEGER, industry TEXT, reason TEXT);
"""


class TestChaseEndToEnd(unittest.TestCase):
    """真闸端到端契约(2026-09-29 劣势升级): 上面的 TestBuilderChaseWiring 把
    ChaseGate **整体 mock** 了 —— 它只证明 builder 会应用闸**返回的集合**，但 P0
    事故的同类风险(builder→闸的**输入契约**错位: 传错列/传错截面/域查空 ⇒ 真闸
    剔除集静默为空)它抓不住。这里不打桩闸: 真 ChaseGate(真 PIT 域+时点名) →
    真 allocate_lots → 真写 plan 表，打穿"打分→剔除→方案"全链；并用 chase 关闭的
    对照臂证明 ramp 股**本会**进方案(排除"测试天然绿"的假阳性)。
    生产 load_market 是全历史面板；测试注入 30 天合成面板(与 _tiny_m 同构，
    锚点 = 2026-08-31)，闸本体/PIT/名称链/整手分配全部走真实现。"""

    CODES = [f"{600000 + i}.SH" for i in range(10)]
    RAMP = "600000.SH"

    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.path = tmp.name
        tmp.close()
        conn = sqlite3.connect(self.path)
        conn.executescript(_E2E_SCHEMA)
        rows = []
        for i, c in enumerate(self.CODES):
            name = "高分股" if c == self.RAMP else f"普通股{i}"
            rows.append(f"('{c}','{name}','ind{i}','20150101')")
        conn.execute("INSERT INTO stock_basic VALUES " + ",".join(rows))
        # 打分: ramp 最高(必进 top6), 其余递减 ⇒ top6 = 600000..600005
        for i, c in enumerate(self.CODES):
            conn.execute("INSERT INTO model_score VALUES (?,?,?,?)",
                         (c, "20260831", 99.0 - i, 0.01))
        for c in self.CODES:
            conn.execute("INSERT INTO daily VALUES (?,?,?,?,?,?)",
                         (c, "20260831", 10.0, 10.0, 1e6, 0.5))
        conn.execute("INSERT INTO regime VALUES ('20260831','neutral')")
        conn.commit()
        conn.close()
        self.patch_db = mock.patch.object(
            app_db, "get_conn", side_effect=lambda: sqlite3.connect(self.path))
        self.patch_db.start()
        m = _tiny_m(n_days=30, n_codes=10, ramp_code=self.RAMP)
        shift = pd.Timestamp("2026-08-31") - m["close"].index[-1]
        self.panel = {k: df.set_axis(df.index + shift) for k, df in m.items()}
        self.patch_mkt = mock.patch("app.factors.factor_engine.load_market",
                                    return_value=self.panel)
        self.patch_mkt.start()
        self.patch_cfg = None

    def tearDown(self):
        self.patch_mkt.stop()
        self.patch_db.stop()
        if self.patch_cfg is not None:
            self.patch_cfg.stop()
        os.unlink(self.path)

    def _cfg(self, chase_enabled: bool) -> dict:
        cfg = {"capital": 20000, "risk_profile": "neutral",
               "max_stocks": 6,
               "state_stocks": {"attack": 6, "neutral": 6, "defense": 6},
               "stock_candidate_top": 0, "industry_max_weight": 0.25,
               "single_side_cost": 0.003,
               "regime_equity_weight": {"aggressive": {"attack": 1.0, "neutral": 1.0,
                                                       "defense": 1.0}}}
        if chase_enabled:
            cfg["chase"] = {"enabled": True, "theta": 2.0}
        return cfg

    def _run(self, chase_enabled: bool):
        self.patch_cfg = mock.patch.object(
            builder.cfg_mod, "load_config", return_value=self._cfg(chase_enabled))
        self.patch_cfg.start()
        try:
            with mock.patch.object(builder.funds, "score_etfs",
                                   return_value=pd.DataFrame()), \
                    mock.patch.object(builder.funds, "otc_fund_picks", return_value={}), \
                    mock.patch.object(builder.repo, "repo_advice",
                                      return_value={"desc": "x", "rate": 1.5}):
                summary = builder.build_plan("20260831")
            conn = sqlite3.connect(self.path)
            try:
                rows = [r[0] for r in conn.execute(
                    "SELECT ts_code FROM plan WHERE plan_type='stock' "
                    "AND plan_date='20260831'").fetchall()]
            finally:
                conn.close()
            return summary, sorted(rows)
        finally:
            self.patch_cfg.stop()
            self.patch_cfg = None

    def test_real_gate_removes_chased_stock_from_plan(self):
        """真闸启用: ramp 股(近5日抬升, z>θ)不得出现在方案表, 且**不补位** ——
        持仓名从 6 缩到 5, 而不是从 rank7-10 补一只进来。"""
        summary, codes = self._run(chase_enabled=True)
        self.assertNotIn(self.RAMP, codes, "追高股必须被真闸剔出方案")
        self.assertEqual(summary["stock_actual"], 5, "剔除不补位 ⇒ 6 缩 5")
        self.assertEqual(codes, sorted(self.CODES[1:6]))

    def test_control_without_chase_ramp_would_be_bought(self):
        """对照臂(chase 关): 同一份数据下 ramp 股本会进方案且持仓名=6 ——
        证明上一条测试的剔除真来自闸, 不是数据天然不含它。"""
        summary, codes = self._run(chase_enabled=False)
        self.assertIn(self.RAMP, codes)
        self.assertEqual(summary["stock_actual"], 6)


class TestVwapUnitCrossReference(unittest.TestCase):
    """vwap 量纲假设单一真源钉(2026-09-29 劣势升级): `×10`(Tushare amount 千元 /
    vol 手 ⇒ 元/股)这份假设有两个使用点 —— expr.py 的 $vwap(单日)与 chase.py 的
    vwap5(5 日聚合)。换数据源时若只改一处, 另一处会**静默按旧量纲算**(devvwap/
    vwap 相关因子整体错量级且无报错)。这里钉住: ①两处互引对方路径; ②×10 换算
    因子仍在各自公式里 —— 任何人改动任一处的量纲, 这条测试会红并把他引向另一处。"""

    def test_unit_conversion_documented_in_both(self):
        base = Path(__file__).resolve().parent.parent
        chase_src = (base / "app" / "portfolio" / "chase.py").read_text(encoding="utf-8")
        expr_src = (base / "app" / "alpha" / "expr.py").read_text(encoding="utf-8")
        self.assertIn("app/alpha/expr.py", chase_src,
                      "chase.py 必须互引 expr.py 的同源量纲假设")
        self.assertIn("app/portfolio/chase.py", expr_src,
                      "expr.py 必须互引 chase.py 的同源量纲假设")
        self.assertIn("10.0 * amt5", chase_src, "chase vwap5 的 ×10 换算被改动?")
        self.assertIn('m["amount"] * 10.0', expr_src, "expr $vwap 的 ×10 换算被改动?")


if __name__ == "__main__":
    unittest.main()
