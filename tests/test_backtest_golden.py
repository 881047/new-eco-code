"""回测黄金快照回归 (2026-09-06 第九轮复查 N9-6)

缩微宇宙(固定种子 12 股 × 30 个月业务日行情)上跑完整 run_backtest, 把 nav 序列与
关键指标锁进 tests/data/golden_backtest.json。引擎数值被有意/无意改动时此测试报警,
防止 158 个组件级用例全绿而回测数字静默漂移。

快照更新(引擎逻辑有意变更后, 先人工核对新数字再更新):
    REGEN_GOLDEN=1 .venv/Scripts/python.exe -m pytest tests/test_backtest_golden.py -q
该运行**不会**覆盖快照: 它把当前数值写成 `golden_backtest.json.new`(旁路文件)并让
用例**失败**(2026-09-12 审查: 原实现直接覆盖 + skipTest ⇒ 一次运行就能悄悄重钉金标准,
而且"重钉了"只表现为一个"跳过", 看起来像没跑)。流程是: 跑一次 → diff `*.json` 与
`*.json.new` → 确认差异就是本次变更的预期结果 → 手动改名并提交(重钉 = 一次可追溯的提交)。
若是意外漂移, 别动快照, 去查引擎。
"""
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.backtest import engine as bt_engine

SNAP = Path(__file__).resolve().parent / "data" / "golden_backtest.json"
SNAP_STOP = Path(__file__).resolve().parent / "data" / "golden_backtest_stop.json"
# R15-9(2026-09-06): 60 股 —— 月度截面 >50 行, IC(score vs fut) 非空可锁死
_CODES = [f"6000{i:02d}.SH" for i in range(60)]

# 固定配置: 快照与真实 config.yaml 解耦(用户改配置不误伤此测试)
# 2026-09-20 B1: 两条基准腿的键都**显式**钉成夹具里唯一存在的那个码 —— 夹具只合成
# `000906.SH` 一列，任一把键落到线上缺省(H00906.CSI)就会得到空腿、把快照改成 NaN。
# 生产尺的接线由 tests/test_bench_leg_wiring.py 负责，不靠这份夹具。
_FIXED_CFG = {
    "capital": 10000, "risk_profile": "aggressive", "max_stocks": 6,
    "state_stocks": {"attack": 6, "neutral": 5, "defense": 4},
    "stock_candidate_top": 0, "industry_max_weight": 0.25,
    "single_side_cost": 0.003, "paper": {"slip": 0.001},
    "benchmark": "000906.SH", "benchmark_nav_code": "000906.SH",
    "regime_equity_weight": {
        "aggressive": {"attack": 1.0, "neutral": 0.9, "defense": 0.8},
    },
}
_METRIC_KEYS = ["ann_return", "excess_ann", "max_drawdown", "sharpe", "vol_ann",
                "win_rate", "avg_turnover", "ic_mean", "sys_ann_return",
                "sys_max_drawdown", "avg_stocks", "avg_cash_idle"]

_SCHEMA = """
CREATE TABLE daily (ts_code TEXT, trade_date TEXT, close REAL, pre_close REAL,
                    open REAL, high REAL, low REAL, vol REAL, amount REAL, pct_chg REAL);
CREATE TABLE daily_basic (ts_code TEXT, trade_date TEXT, turnover_rate REAL,
                          pe_ttm REAL, pb REAL, dv_ttm REAL, total_mv REAL,
                          circ_mv REAL);
CREATE TABLE adj_factor (ts_code TEXT, trade_date TEXT, adj_factor REAL);
CREATE TABLE index_daily (ts_code TEXT, trade_date TEXT, close REAL);
CREATE TABLE model_score (ts_code TEXT, trade_date TEXT, score REAL, pred_ret REAL);
CREATE TABLE stock_basic (ts_code TEXT, name TEXT, industry TEXT);
CREATE TABLE regime (trade_date TEXT, state TEXT);
CREATE TABLE repo_rate (trade_date TEXT, overnight REAL, r7d REAL, r14d REAL);
CREATE TABLE trade_cal (cal_date TEXT PRIMARY KEY, is_open INTEGER);
CREATE TABLE namechange (ts_code TEXT, start_date TEXT, end_date TEXT, name TEXT);
CREATE TABLE backtest_summary (run_date TEXT PRIMARY KEY, metrics TEXT);
CREATE TABLE backtest_nav (run_date TEXT, trade_date TEXT, nav REAL, sys_nav REAL,
                           bench_nav REAL);
CREATE TABLE backtest_nav_daily (run_date TEXT, trade_date TEXT, nav REAL,
                                 sys_nav REAL, bench_nav REAL);
"""


def _universe() -> dict:
    """固定种子缩微宇宙: 60 股 × 业务日 2017-01-03~2019-06-28 (30 个评分月)。
    R15-9(2026-09-06): 评分与未来 21 日收益构造正相关(有梯度) → 月度截面 60 行
    超过引擎 IC 门槛(>50), ic_mean 非空并锁进快照"""
    rng = np.random.default_rng(42)
    days = pd.bdate_range("2017-01-03", "2019-06-28")
    dstr = days.strftime("%Y%m%d")
    me = days[days.is_month_end]
    rows = []
    for i, code in enumerate(_CODES):
        p0 = 5.0 + 55.0 * (i / 59.0)          # 5~60 元: 覆盖整手买得起/买不起两类
        rets = rng.normal(0.0, 0.02, len(days))
        closes = p0 * np.cumprod(1 + rets)
        pre = np.concatenate([[p0], closes[:-1]])
        opens = closes * rng.uniform(0.98, 1.02, len(days))
        for k, dt in enumerate(dstr):
            rows.append((code, dt, closes[k], pre[k], opens[k],
                         max(opens[k], closes[k]) * 1.01,
                         min(opens[k], closes[k]) * 0.99,
                         1e6, closes[k] * 1e6,
                         (closes[k] / pre[k] - 1) * 100))
    daily = pd.DataFrame(rows, columns=["ts_code", "trade_date", "close", "pre_close",
                                        "open", "high", "low", "vol", "amount", "pct_chg"])
    # circ_mv 于 2026-09-11 加入(阶段 0b): load_market 现在透视该列 —— 夹具必须与生产
    # schema 同形, 否则 SELECT 直接 OperationalError。取 total_mv 的 60%(流通盘占比),
    # 且**非零**(下游因子以它为分母, 0 会被 replace(0, nan) 变成缺失)。
    basic_rows = [(c, dt, 2.0, 15.0, 2.0, 1.0, 1e10, 6e9) for c in _CODES for dt in dstr]
    daily_basic = pd.DataFrame(basic_rows, columns=[
        "ts_code", "trade_date", "turnover_rate", "pe_ttm", "pb", "dv_ttm", "total_mv",
        "circ_mv"])
    adj_factor = pd.DataFrame(
        [(c, dt, 1.0) for c in _CODES for dt in dstr],
        columns=["ts_code", "trade_date", "adj_factor"])
    bench_p = 5000.0 * np.cumprod(1 + rng.normal(0.0, 0.01, len(days)))
    index_daily = pd.DataFrame(
        {"ts_code": "000906.SH", "trade_date": dstr, "close": bench_p})
    # 未来21日收益(与引擎 IC 同口径): 评分与其正相关, 让 IC 有梯度且稳定
    tmp = daily.set_index("trade_date").groupby("ts_code")["close"].transform(
        lambda s: s.shift(-21) / s - 1)
    daily["fut21"] = tmp.reset_index(drop=True)
    score_rows = []
    for m, d in enumerate(me):
        d_s = d.strftime("%Y%m%d")
        fut = daily[daily.trade_date == d_s].set_index("ts_code")["fut21"]
        z = (fut - fut.mean()) / fut.std()
        noise = rng.normal(0.0, 0.5, len(_CODES))
        for i, code in enumerate(_CODES):
            sc = 90.0 - 4.0 * (i % 60) / 59.0 * 20.0 + 3.0 * z.get(code, 0.0) + noise[i]
            score_rows.append((code, d_s, sc, fut.get(code, 0.0)))
    model_score = pd.DataFrame(score_rows, columns=["ts_code", "trade_date", "score", "pred_ret"])
    # 2026-09-08 审查: 股票名从 "TST00"(含 ST 子串)改为 "N00" —— 引擎新增 ST/退 PIT
    # 候选过滤后, 原夹具名会被全部过滤(零候选); 改名也把限幅口径从 ST 5% 还原为
    # 主板 10%(快照已随 REGEN 重新生成)
    stock_basic = pd.DataFrame(
        {"ts_code": _CODES, "name": [f"N{i:02d}" for i in range(60)],
         "industry": [f"i{i % 6}" for i in range(60)]})
    states = ["attack", "neutral", "defense"]
    regime = pd.DataFrame({"trade_date": me.strftime("%Y%m%d"),
                           "state": [states[m % 3] for m in range(len(me))]})
    repo_rate = pd.DataFrame({"trade_date": me.strftime("%Y%m%d"),
                              "overnight": 1.5, "r7d": 1.6, "r14d": 1.7})
    trade_cal = pd.DataFrame({"cal_date": dstr, "is_open": 1})
    namechange = pd.DataFrame(columns=["ts_code", "start_date", "end_date", "name"])
    return {"daily": daily.drop(columns=["fut21"]), "daily_basic": daily_basic,
            "adj_factor": adj_factor, "index_daily": index_daily,
            "model_score": model_score, "stock_basic": stock_basic, "regime": regime,
            "repo_rate": repo_rate, "trade_cal": trade_cal, "namechange": namechange}


class TestBacktestGolden(unittest.TestCase):
    def _run(self, stop_loss=None):
        """跑一次完整 run_backtest 并返回 {metrics, nav}(快照键子集)"""
        dfs = _universe()
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        path = tmp.name
        tmp.close()
        conn = sqlite3.connect(path)
        conn.executescript(_SCHEMA)
        for name, df in dfs.items():
            df.to_sql(name, conn, if_exists="append", index=False)
        conn.commit()
        conn.close()
        try:
            with mock.patch.object(bt_engine.db, "get_conn",
                                   side_effect=lambda: sqlite3.connect(path)), \
                 mock.patch.object(bt_engine.cfg_mod, "load_config",
                                   return_value=_FIXED_CFG):
                metrics = bt_engine.run_backtest(run_tag="golden", stop_loss=stop_loss)
            conn = sqlite3.connect(path)
            nav = pd.read_sql(
                "SELECT trade_date, nav, sys_nav, bench_nav FROM backtest_nav "
                "ORDER BY trade_date", conn).to_dict("records")
            # 2026-09-28 预注册判据(plans/metric-fix-batch-prereg-20260928.md §二③):
            # 日频序列日期必须唯一 —— 锚点日曾出现"近似行+精确行"双记, 内存指标与
            # 落库表不可互审、日频回撤被虚高峰值人为放大; 此断言防锯齿回归。
            _dup = conn.execute(
                "SELECT COUNT(*) FROM (SELECT trade_date FROM backtest_nav_daily "
                "GROUP BY trade_date HAVING COUNT(*) > 1)").fetchone()[0]
            self.assertEqual(_dup, 0, "backtest_nav_daily 出现重复日期(月末锚点日锯齿回归)")
            conn.close()
        finally:
            os.unlink(path)
        keys = _METRIC_KEYS + ["ic_mean", "icir"]
        return {"metrics": {k: metrics[k] for k in keys}, "nav": nav}

    def _check(self, got, snap: Path, name: str):
        if os.environ.get("REGEN_GOLDEN") == "1":
            # 2026-09-12 审查(测试质量项): 原实现**直接覆盖**快照再 skipTest ⇒ 一次带环境
            # 变量的运行就能悄悄重钉金标准, 而"重钉了"这件事只以"跳过"的形式出现(看起来
            # 像没跑)。改为: 当前数值写 `.new` 旁路文件 + **本用例失败** —— 人对照 diff、
            # 确认这就是本次有意变更的预期数字之后, 自己把 .new 挪过去提交。
            # 一条纪律: **金标准只能被人重钉, 不能被一次运行顺手重钉**(与项目「改口径要
            # 预注册 + 组合层双轴判定」同源)。
            new = snap.with_suffix(snap.suffix + ".new")
            new.parent.mkdir(parents=True, exist_ok=True)
            new.write_text(json.dumps(got, ensure_ascii=False, indent=1),
                           encoding="utf-8")
            self.fail(
                f"REGEN_GOLDEN=1: {name} 的当前数值已写入 {new.name} —— **快照 "
                f"{snap.name} 没有被覆盖**。请人工 diff 两份文件: 若差异确是有意变更的"
                f"预期结果, 再把 {new.name} 改名成 {snap.name} 并提交(重钉即一次可追溯的"
                f"提交); 若是意外漂移, 别动快照, 去查引擎。")
        self.assertTrue(snap.exists(), f"{name} 快照不存在: 先 REGEN_GOLDEN=1 生成")
        exp = json.loads(snap.read_text(encoding="utf-8"))
        self.assertEqual(len(got["nav"]), len(exp["nav"]), f"{name} nav 期数漂移")
        for g, e in zip(got["nav"], exp["nav"]):
            self.assertEqual(g["trade_date"], e["trade_date"])
            for col in ("nav", "sys_nav", "bench_nav"):
                self.assertAlmostEqual(g[col], e[col], places=9,
                                       msg=f"{name} {e['trade_date']} {col} 漂移")
        for k in got["metrics"]:
            self.assertAlmostEqual(got["metrics"][k], exp["metrics"][k], places=6,
                                   msg=f"{name} 指标 {k} 漂移")

    def test_nav_and_metrics_match_snapshot(self):
        self._check(self._run(), SNAP, "默认路径")

    def test_stop_loss_snapshot(self):
        # R15-1(2026-09-06): 止损分支快照 —— 该分支曾把年化打到 -25.76%/回撤 -96.31%,
        # 与默认快照一起锁死逐日判定/entry 口径/现金补偿
        got = self._run(stop_loss=-0.15)
        self._check(got, SNAP_STOP, "止损路径")
        # 止损开启不得改变 IC(同评分同持仓池), 且 nav 序列必须与默认路径不同(证明分支生效)
        off = json.loads(SNAP.read_text(encoding="utf-8"))
        self.assertEqual(got["metrics"]["ic_mean"], off["metrics"]["ic_mean"])
        self.assertNotEqual(got["nav"][-1]["nav"], off["nav"][-1]["nav"])


class TestRegenGoldenIsNotSilent(unittest.TestCase):
    """`REGEN_GOLDEN=1` 只能产出**候选**快照, 不能重钉金标准 (2026-09-12 审查: 测试质量项)。

    这条守卫守的是"那一次运行到底做了什么": 覆盖 + 跳过 = 金标准悄悄变成当前值, 而且
    看起来像"没跑"; 换成"写旁路文件 + 失败"之后, 悄悄重钉这件事在结构上就不可能发生。
    """

    @staticmethod
    def _cand(ann=1.0, nav=1.0):
        """与 `_run()` 同形的候选结果(三个 nav 列一个都不能少 —— 比对循环按列取)"""
        return {"metrics": {"ann_return": ann},
                "nav": [{"trade_date": "20170103", "nav": nav,
                         "sys_nav": nav, "bench_nav": nav}]}

    def test_regen_writes_new_file_and_fails_never_overwrites(self):
        got = self._cand()
        with tempfile.TemporaryDirectory() as td:
            snap = Path(td) / "golden_backtest.json"
            snap.write_text('{"metrics": {"ann_return": 0.0}, "nav": []}',
                            encoding="utf-8")
            before = snap.read_text(encoding="utf-8")
            helper = unittest.TestCase()          # 只借 _check 用到的 self.fail
            with mock.patch.dict(os.environ, {"REGEN_GOLDEN": "1"}):
                with self.assertRaises(AssertionError) as cm:
                    TestBacktestGolden._check(helper, got, snap, "单元测试桩")
            # 1) 金标准一个字节都没动
            self.assertEqual(snap.read_text(encoding="utf-8"), before,
                             "REGEN_GOLDEN=1 把快照覆盖了 —— 金标准必须由人重钉")
            # 2) 当前数值确实落盘了(否则这次运行白跑, 人也没法 diff)
            new = snap.with_suffix(snap.suffix + ".new")
            self.assertTrue(new.exists(), f"没写出候选文件 {new.name}")
            self.assertEqual(json.loads(new.read_text(encoding="utf-8")), got)
            # 3) 报错要说清下一步(改名 + 提交), 不然只留一个红色的 fail 让人猜
            self.assertIn(".new", str(cm.exception))
            self.assertIn("diff", str(cm.exception))

    def test_regen_env_var_value_must_be_exactly_one(self):
        """只认 `=1`; `REGEN_GOLDEN=0` 这类写法按**正常比对**走(不做隐式真值解析)"""
        got = self._cand(nav=0.0)
        with tempfile.TemporaryDirectory() as td:
            snap = Path(td) / "golden_backtest.json"
            snap.write_text(json.dumps(got), encoding="utf-8")
            helper = unittest.TestCase()
            with mock.patch.dict(os.environ, {"REGEN_GOLDEN": "0"}):
                TestBacktestGolden._check(helper, got, snap, "单元测试桩")   # 不抛
            self.assertFalse((Path(td) / "golden_backtest.json.new").exists())


if __name__ == "__main__":
    unittest.main()
