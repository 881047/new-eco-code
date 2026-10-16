"""资金类/股东类因子单测 (2026-09-11 「芯片式异构架构」阶段 0b)

覆盖的三类坑都是**已经踩过并写进 docstring** 的, 测试的作用是防止后人"顺手简化"掉:

  1. **稀疏源必须 reindex 到完整交易日索引** —— 两融/大宗不是每日全市场都有行。
     不 reindex 时 `rolling(20)`/`shift(20)` 数的是「20 个观测」而不是「20 个交易日」,
     把 20 日变化算成 60 日变化, 且**不会报错**。
  2. **小分母清洗** —— margin.rzye 最小值是 0, 0.1% 分位仅 13 万元; holder_num 有
     11,052 行 < 100(最小为 1)。不清掉, 比率因子会被拉到 ±几十的量级。
  3. **PIT 对齐** —— 股东户数必须按 ann_date 而非 end_date 可见(季报在报告期结束后
     数周才披露), 且 ann_date 当天不可见(A 股财报多在收盘后披露)。

另有一条**不变量测试**: `include_research=False` 时不得返回任何 `hk_*` —— hk_hold 在
2024-08-19 覆盖度从 4042 只断崖跌到 793 只, 是「含义静默改变」的特征(方案 §3 不变量 #6)。

运行: ./.venv/Scripts/python.exe -m pytest tests/test_flow_factors.py -q
"""
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd

from app import db
from app.factors import flow_factors as ff
from app.factors import holder_factors as hf

DAYS = pd.bdate_range("2020-01-02", periods=40)


def _market(close=None, amount=None, circ_mv=None) -> dict:
    """对齐到 DAYS 的最小行情字典(compute_*_factors 只用到这三个键)"""
    idx, cols = DAYS, ["600000.SH", "000001.SZ"]
    def _mk(v):
        if isinstance(v, pd.DataFrame):
            return v.reindex(index=idx, columns=cols).astype(np.float32)
        return pd.DataFrame(v if v is not None else np.nan, index=idx, columns=cols,
                            dtype=np.float32)
    return {"close": _mk(close), "amount": _mk(amount), "circ_mv": _mk(circ_mv)}


class _DbBase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.path = tmp.name
        tmp.close()
        conn = sqlite3.connect(self.path)
        conn.executescript(db.SCHEMA)
        conn.commit()
        conn.close()
        self.patch = mock.patch.object(
            ff.db, "get_conn", side_effect=lambda: sqlite3.connect(self.path))
        self.patch.start()
        # holder_factors 与 flow_factors 是两个模块, 各自 import 了 db —— 都要指过来
        self.patch_h = mock.patch.object(
            hf.db, "get_conn", side_effect=lambda: sqlite3.connect(self.path))
        self.patch_h.start()

    def tearDown(self):
        self.patch_h.stop()
        self.patch.stop()
        Path(self.path).unlink(missing_ok=True)

    def _exec(self, sql, rows=None):
        """rows 为 None → 单条 DDL; 单行元组 → 一条 INSERT; 列表 → executemany。
        finally 关连接: 失败路径漏关会在 Windows 上锁住临时库文件(WinError 32)。"""
        conn = sqlite3.connect(self.path)
        try:
            if rows is None:
                conn.execute(sql)
            elif rows and isinstance(rows[0], (list, tuple)):
                conn.executemany(sql, rows)
            else:
                conn.execute(sql, rows)
            conn.commit()
        finally:
            conn.close()


# ---------------------------------------------------------------------------
class TestSparseReindex(_DbBase):
    """稀疏源 → 完整交易日索引。这是本模块最容易"被简化掉"的一处。"""

    def test_roll_window_counts_trading_days_not_observations(self):
        # 只在第 0 天与第 20 天各放一行两融数据(其余日期**无行**)
        d0, d20 = DAYS[0].strftime("%Y%m%d"), DAYS[20].strftime("%Y%m%d")
        self._exec("INSERT INTO margin (trade_date, ts_code, rzye, rqye, rzmre, rzche) "
                   "VALUES (?,?,?,?,?,?)",
                   [(d0, "600000.SH", 1e8, 1e7, 1e6, 1e6),
                    (d20, "600000.SH", 2e8, 1e7, 1e6, 1e6)])
        out = ff.compute_margin_factors(_market(), DAYS)
        got = out["margin_rzye_chg20"].loc[DAYS[20], "600000.SH"]
        # 正解 = 20 交易日变化 = 2e8/1e8 - 1 = 1.0
        # 若不 reindex: 该列只有 2 个观测, shift(20) 越界 → NaN, 且**不报错**
        self.assertAlmostEqual(float(got), 1.0, places=5)

    def test_no_row_is_nan_not_zero(self):
        # 非两融标的(无行)不是 0 而是**整列缺席** —— "不是两融标的" ≠ "融资余额为 0"。
        # reindex 只对齐日期, 不补股票: 没有 margin 行的股票根本不进因子矩阵。
        self._exec("INSERT INTO margin (trade_date, ts_code, rzye, rqye, rzmre, rzche) "
                   "VALUES (?,?,?,?,?,?)",
                   [(DAYS[0].strftime("%Y%m%d"), "600000.SH", 1e8, 1e7, 1e6, 1e6)])
        circ = pd.DataFrame(1e5, index=DAYS, columns=["600000.SH", "000001.SZ"])
        out = ff.compute_margin_factors(_market(circ_mv=circ), DAYS)
        # 列是「行情帧 ∪ 源帧」的并集, 所以非两融标的以**整列 NaN**出现(不是 0, 也不是缺席)。
        # 存储侧 store_factors 会 dropna, 所以稀疏不会撑大 factor 表。
        self.assertTrue(out["margin_rzye_ratio"]["000001.SZ"].isna().all(),
                        "非两融标的必须是 NaN —— 填 0 会被模型读成『融资余额为 0』")
        self.assertFalse(pd.isna(out["margin_rzye_ratio"].loc[DAYS[0], "600000.SH"]))


class TestPivotWindowLowerBound(_DbBase):
    """P2-19: `_pivot` 加 `trade_date>=dates[0]` 下界后必须与原全表读法**逐位相同**。

    这条不是性能测试, 是等价性测试 —— 优化本身是性能的, 但它有两条会静默改变下游读数的
    路子: ①行过滤若少算了窗口内该有的行; ②列集缩水(窗口内无行的退市/停牌股会整列消失,
    而面板宽度会通过 neutralize 的 groupby 分块影响每个截面 1~2 ulp)。
    """

    def setUp(self):
        super().setUp()
        cols = ["buy_sm_amount", "buy_md_amount", "buy_lg_amount", "buy_elg_amount",
                "sell_sm_amount", "sell_md_amount", "sell_lg_amount", "sell_elg_amount"]
        self.cols = cols
        d_out = (DAYS[0] - pd.Timedelta(days=1)).strftime("%Y%m%d")   # 窗口外(退市前的历史行)
        rows = [(d_out, "OLD.SH", *([1.0] * 8)),      # 只在窗口外有行 ⇒ 窗口内整列 NaN
                (d_out, "600000.SH", *([2.0] * 8))]
        for i, d in enumerate(DAYS):
            rows.append((d.strftime("%Y%m%d"), "600000.SH", *([float(i + 10)] * 8)))
            rows.append((d.strftime("%Y%m%d"), "000001.SZ", *([float(i + 20)] * 8)))
        self._exec("INSERT INTO moneyflow (trade_date, ts_code, buy_sm_amount, buy_md_amount,"
                   " buy_lg_amount, buy_elg_amount, sell_sm_amount, sell_md_amount,"
                   " sell_lg_amount, sell_elg_amount) VALUES (?,?,?,?,?,?,?,?,?,?)", rows)

    def _old_style_pivot(self):
        """修复前的实现: 无 WHERE 全表读入, 列集=全历史。"""
        conn = sqlite3.connect(self.path)
        try:
            q = ", ".join(["trade_date", "ts_code"] + self.cols)
            df = pd.read_sql(f"SELECT {q} FROM moneyflow", conn)
        finally:
            conn.close()
        out = {}
        for c in self.cols:
            pv = df.pivot(index="trade_date", columns="ts_code", values=c).sort_index()
            pv.index = pd.to_datetime(pv.index)
            out[c] = pv.reindex(index=DAYS).astype(np.float32)
        return out

    def _new_pivot(self):
        conn = sqlite3.connect(self.path)
        try:
            return ff._pivot(conn, "moneyflow", self.cols, DAYS)
        finally:
            conn.close()

    def test_bit_identical_to_full_table_read(self):
        old, new = self._old_style_pivot(), self._new_pivot()
        for c in self.cols:
            self.assertTrue(old[c].equals(new[c]),
                            f"{c} 与修复前不逐位相同 —— 性能优化改变了因子值")
        # 列集也必须一致: 只在窗口外有行的 OLD.SH 仍要以整列 NaN 出现
        self.assertIn("OLD.SH", new["buy_lg_amount"].columns,
                      "列集缩水了 —— 面板宽度变化会经由 neutralize 平移每个截面 1~2 ulp")
        self.assertTrue(new["buy_lg_amount"]["OLD.SH"].isna().all())
        self.assertEqual(float(new["buy_lg_amount"].loc[DAYS[-1], "600000.SH"]),
                         float(DAYS.size + 9))

    def test_lower_bound_reaches_the_sql(self):
        """佐证: 下界确实进了 SQL(否则本测试的前一条断言即使删掉 WHERE 也照样通过)。"""
        seen = []
        real = pd.read_sql

        def _spy(sql, con, *a, **kw):
            seen.append(sql)
            return real(sql, con, *a, **kw)
        with mock.patch.object(ff.pd, "read_sql", side_effect=_spy):
            self._new_pivot()
        hit = [s for s in seen if s.startswith("SELECT trade_date, ts_code")]
        self.assertTrue(hit, "没抓到取数 SQL, 测试空转")
        self.assertTrue(all("WHERE trade_date>=?" in s for s in hit), hit)

    def test_empty_dates_returns_empty_frames(self):
        conn = sqlite3.connect(self.path)
        try:
            out = ff._pivot(conn, "moneyflow", self.cols, pd.DatetimeIndex([]))
        finally:
            conn.close()
        self.assertEqual(out["buy_lg_amount"].shape, (0, 0))


class TestSmallDenominatorClean(unittest.TestCase):
    """小分母清洗 —— 实测把 margin_net_to_rzye 均值从 -3.51 拉回 -0.016"""

    def test_tiny_rzye_masked(self):
        idx = pd.DatetimeIndex(["20200102", "20200103"])
        rzye = pd.DataFrame({"A.SH": [130.0, 130.0], "B.SH": [1e8, 1e8]}, index=idx)
        amt = pd.DataFrame({"A.SH": [1e4, 1e4], "B.SH": [1e4, 1e4]}, index=idx)
        circ = pd.DataFrame({"A.SH": [1e5, 1e5], "B.SH": [1e5, 1e5]}, index=idx)
        m = {"amount": amt, "circ_mv": circ, "close": circ}
        with mock.patch.object(ff, "_pivot", return_value={
                "rzye": rzye, "rqye": rzye * 0.1, "rzmre": amt * 1e3, "rzche": amt * 0.5e3}):
            out = ff.compute_margin_factors(m, idx)
        # A 的余额 130 元 < 1e6 下界 → 全部比率置 NaN, 而不是算出上万倍的比率
        self.assertTrue(pd.isna(out["margin_net_to_rzye"].loc[idx[0], "A.SH"]))
        self.assertFalse(pd.isna(out["margin_net_to_rzye"].loc[idx[0], "B.SH"]))
        # 且清洗后量级正常(0.5 而不是几千)
        self.assertLess(abs(out["margin_net_to_rzye"].loc[idx[0], "B.SH"]), 10.0)


class TestMoneyflowUnitFree(_DbBase):
    """资金流比值用自身总买入做分母 → 不依赖上游万元约定"""

    def test_main_net_share(self):
        d = DAYS[0].strftime("%Y%m%d")
        # tot=100; 主力净 = (30+20)-(10+5) = 35 → mf_main_net = 0.35
        self._exec(
            "INSERT INTO moneyflow (trade_date, ts_code, buy_sm_amount, buy_md_amount,"
            " buy_lg_amount, buy_elg_amount, sell_sm_amount, sell_md_amount,"
            " sell_lg_amount, sell_elg_amount) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (d, "600000.SH", 25, 25, 30, 20, 25, 25, 10, 5))
        out = ff.compute_moneyflow_factors(_market(), DAYS)
        self.assertAlmostEqual(float(out["mf_main_net"].loc[DAYS[0], "600000.SH"]),
                               0.35, places=5)
        self.assertAlmostEqual(float(out["mf_elg_net"].loc[DAYS[0], "600000.SH"]),
                               0.15, places=5)


class TestBlockFactors(_DbBase):
    def test_discount_uses_close(self):
        d = DAYS[0].strftime("%Y%m%d")
        self._exec("INSERT INTO block_trade (trade_date, ts_code, n_trades, total_vol,"
                   " total_amount, avg_price) VALUES (?,?,?,?,?,?)",
                   (d, "600000.SH", 2, 100.0, 900.0, 9.0))
        # 不能用 _market()["close"] * 0 + 10.0 —— NaN * 0 仍是 NaN, 会得到全 NaN 的 close
        close = pd.DataFrame(10.0, index=DAYS, columns=["600000.SH", "000001.SZ"])
        out = ff.compute_block_factors(_market(close=close), DAYS)
        # 折价甩卖: 9/10 - 1 = -10%
        self.assertAlmostEqual(float(out["block_discount"].loc[DAYS[0], "600000.SH"]),
                               -0.10, places=5)


class TestResearchOnlyIsolation(_DbBase):
    """不变量 #6: hk_* 不得进入生产因子集"""

    def test_hk_excluded_by_default(self):
        self._exec("INSERT INTO hk_hold (trade_date, ts_code, vol, ratio) VALUES (?,?,?,?)",
                   (DAYS[0].strftime("%Y%m%d"), "600000.SH", 1e6, 3.5))
        self._exec("INSERT INTO moneyflow (trade_date, ts_code, buy_lg_amount,"
                   " sell_lg_amount, buy_sm_amount, sell_sm_amount, buy_md_amount,"
                   " sell_md_amount, buy_elg_amount, sell_elg_amount)"
                   " VALUES (?,?,?,?,?,?,?,?,?,?)",
                   (DAYS[0].strftime("%Y%m%d"), "600000.SH", 1, 1, 1, 1, 1, 1, 1, 1))
        m = _market()
        prod = ff.compute_flow_factors(m, DAYS, include_research=False)
        self.assertTrue(prod, "生产路径应产出资金类因子")
        self.assertFalse([k for k in prod if k.startswith(ff.RESEARCH_ONLY_PREFIX)],
                         "生产路径不得含 hk_* 因子(2024-08 覆盖度断崖)")
        research = ff.compute_flow_factors(m, DAYS, include_research=True)
        self.assertIn("hk_ratio", research)


class TestMissingTableNoCrash(unittest.TestCase):
    def test_returns_empty_when_tables_absent(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        path = tmp.name
        tmp.close()
        conn = sqlite3.connect(path)
        conn.executescript(db.SCHEMA)
        conn.execute("DROP TABLE margin")
        conn.execute("DROP TABLE moneyflow")
        conn.execute("DROP TABLE block_trade")
        conn.commit()
        conn.close()
        try:
            with mock.patch.object(ff.db, "get_conn",
                                   side_effect=lambda: sqlite3.connect(path)):
                out = ff.compute_flow_factors(_market(), DAYS)
            self.assertEqual(out, {})
        finally:
            Path(path).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
class TestHolderFactors(_DbBase):
    """股东户数: PIT 对齐 + 脏值下界 + 符号约定"""

    _DAYS = pd.bdate_range("2020-01-02", "2020-12-31")

    def _insert(self, rows):
        self._exec("INSERT INTO holder_num (ts_code, end_date, ann_date, holder_num) "
                   "VALUES (?,?,?,?)", rows)

    def test_pit_uses_ann_date_not_end_date(self):
        # 报告期 2020-03-31, 但 2020-04-25(周六)才披露 → 次一交易日 2020-04-27 才可见
        self._insert([("600000.SH", "20191231", "20200115", 10000.0),
                      ("600000.SH", "20200331", "20200425", 8000.0)])
        out = hf.compute_holder_factors(self._DAYS)
        s = out["holder_chg_1q"]["600000.SH"].dropna()
        self.assertTrue(len(s) > 0)
        self.assertGreaterEqual(s.index[0], pd.Timestamp("2020-04-27"),
                                "按 end_date(0331) 对齐 = 用了尚未公布的数据(前视)")
        # 户数 10000 → 8000 = 减少 = 筹码集中 → 因子为正(log 差分: -log(0.8) = +0.223)
        self.assertAlmostEqual(float(s.iloc[0]), 0.22314, places=4)

    def test_tiny_holder_num_dropped(self):
        # holder_num=1 是上游占位值(全表有 11,052 行 < 100) → 必须被下界清掉,
        # 否则 1 → 500 户会算出 +49,900% 把因子均值拉到 -48
        self._insert([("600000.SH", "20191231", "20200115", 1.0),
                      ("600000.SH", "20200331", "20200425", 500.0),
                      ("000001.SZ", "20191231", "20200115", 50000.0),
                      ("000001.SZ", "20200331", "20200425", 40000.0)])
        out = hf.compute_holder_factors(self._DAYS)
        # 600000 的两期都在下界之下 → 整列不出现在因子矩阵里
        self.assertNotIn("600000.SH", out["holder_chg_1q"].columns)
        # 000001 正常: 50000 → 40000 = +0.223
        self.assertAlmostEqual(
            float(out["holder_chg_1q"]["000001.SZ"].dropna().iloc[0]), 0.22314, places=4)

    def test_correction_keeps_latest_ann_date(self):
        # 同一报告期两次披露(更正) → 保留 ann_date 最新的一条
        self._insert([("600000.SH", "20191231", "20200115", 10000.0),
                      ("600000.SH", "20200331", "20200425", 8000.0),
                      ("600000.SH", "20200331", "20200820", 20000.0)])  # 更正
        out = hf.compute_holder_factors(self._DAYS)
        s = out["holder_chg_1q"]["600000.SH"].dropna()
        # 用更正后的 20000: log(20000/10000) 取负 = -0.693
        self.assertAlmostEqual(float(s.iloc[0]), -0.6931, places=4)


if __name__ == "__main__":
    unittest.main()
