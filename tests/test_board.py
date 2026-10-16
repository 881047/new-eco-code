"""板块交易规则单测 (2026-09-07 全市场扩展)

运行: python -m unittest tests.test_board -v
覆盖: board_of / limit_ratio(含 ST 规则) / no_limit_trading_days / lot_shares,
以及执行引擎 _exec_price 与 sizing 的板块化行为。
"""
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from app.portfolio import board
from app.paper import engine
from app.portfolio.sizing import allocate_lots


class TestBoardOf(unittest.TestCase):
    def test_board_of(self):
        cases = {
            "600000.SH": "main", "000001.SZ": "main", "002123.SZ": "main",
            "001979.SZ": "main", "003001.SZ": "main",
            "300750.SZ": "chinext", "688111.SH": "star",
            # 689xxx = 科创板 CDR, 与 688 同规则 —— 2026-09-12 D06/D07 验收补:
            # 原只认 688 ⇒ 689009.SH 落到 "other"(10%), 而交易所实测它是 20%
            # (前 5 日无限制)。它是唯一进了 model_score 的科创板代码 ⇒ 真影响回测。
            "689009.SH": "star",
            "830799.BJ": "bse", "920000.BJ": "bse", "430047.BJ": "bse",
            # 2026-09-28 预注册修正(plans/metric-fix-batch-prereg-20260928.md §三):
            # 900xxx = **沪市 B 股**(±10%/100 股整数倍), 原被 ("4","8","9") 一把抓进
            # bse(±30%/最低 100 股后 1 股递增) —— 封板判定线被抬高 3 倍。实测无 9 开头
            # 进分 ⇒ Δ=0, 纯潜伏修正; 920 前置后北交所新段行为不变。
            "900901.SH": "other",
        }
        for code, want in cases.items():
            self.assertEqual(board.board_of(code), want, code)

    def test_sh_b_share_limit_and_lots(self):
        """900 沪B: 限幅走 other 档 ±10%, 整手 100 股整数倍(非 bse 的 1 股递增)。"""
        self.assertEqual(board.limit_ratio("900901.SH", False, "20260901", "沪B测试"), 0.10)
        self.assertEqual(board.lot_shares("900901.SH", 350), 300)   # multiple-100, 非 min-100
        self.assertEqual(board.lot_shares("920001.BJ", 350), 350)   # bse: min-100 后 1 股递增


class TestLimitRatio(unittest.TestCase):
    def test_limits(self):
        self.assertEqual(board.limit_ratio("600000.SH"), 0.10)
        # 主板 ST: **2026-07-06 起放宽到 10%**(见 test_main_st_reform_20260706)。
        # 本行在 2026-09-12 前断言 0.05 —— 那是已失效的旧规则, 不是回归。
        self.assertEqual(board.limit_ratio("600000.SH", True), 0.10)
        self.assertEqual(board.limit_ratio("300750.SZ"), 0.20)
        self.assertEqual(board.limit_ratio("300750.SZ", True), 0.20)   # 创业板 ST 不收窄
        self.assertEqual(board.limit_ratio("688111.SH", True), 0.20)   # 科创板 ST 不收窄
        self.assertEqual(board.limit_ratio("830799.BJ"), 0.30)
        self.assertEqual(board.limit_ratio("830799.BJ", True), 0.30)   # 北交所 ST 不收窄

    def test_main_st_reform_20260706(self):
        """主板风险警示股涨跌幅 5%→10% —— 交易所 `stk_limit` 定档的边界。

        2026-09-12 D06/D07 验收实测: 5% 档最后一天 **20260703**(周五), 10% 档首日
        **20260706**(周一), 142~173 只全量同步切换、无过渡日、无残留; 创业板/科创板
        ST 全程 20% 未变。原实现把"主板 ST 恒为 5%"写死 ⇒ 7,142 格判错。
        """
        # 边界两侧
        self.assertEqual(board.limit_ratio("600000.SH", True, "20260703"), 0.05)
        self.assertEqual(board.limit_ratio("600000.SH", True, "20260706"), 0.10)
        self.assertEqual(board.limit_ratio("600000.SH", True, "20260911"), 0.10)
        # 非 ST 主板全程 10%, 不受此改革影响
        for d in ("20170103", "20260703", "20260706", "20260911"):
            self.assertEqual(board.limit_ratio("600000.SH", False, d), 0.10)
        # 创业/科创/北交 ST 全程不变
        self.assertEqual(board.limit_ratio("300750.SZ", True, "20260703"), 0.20)
        self.assertEqual(board.limit_ratio("300750.SZ", True, "20260706"), 0.20)
        self.assertEqual(board.limit_ratio("830799.BJ", True, "20260706"), 0.30)

    def test_s_share_by_name(self):
        """S股(未完成股权分置改革)±5% 由**当日名称**决定, 与板块无关。

        `600182.SH` S佳通是全市场仅存的未股改 S股, 交易所 `stk_limit` 实测
        **1,789 格全部 ±5%**, 无一年例外。
        """
        self.assertEqual(board.limit_ratio("600182.SH", False, "20260911", "S佳通"), 0.05)
        self.assertEqual(board.limit_ratio("600182.SH", False, "20260911"), 0.10)  # 不传名 = 板块档
        # "S" 前缀但含 ST 的(SST 类)= 风险警示, 不适用 S股档 ⇒ 走主板 ST 档
        self.assertEqual(board.limit_ratio("600182.SH", True, "20200103", "SST佳通"), 0.05)
        self.assertEqual(board.limit_ratio("600182.SH", True, "20260911", "SST佳通"), 0.10)

    def test_delist_period_follows_board_band(self):
        """退市整理期("退")**没有独立档位** —— 它走板块常态档。

        2026-09-12 D06/D07 验收: 先按"退 ⇒ 一律 ±10%"改, 当日即被 `stk_limit` 实测
        推翻(该改动把 2022 的无法归因从 0 推到 112、2023 推到 154)。按(板块×年)
        分组后真相是: 主板 0.10 / 科创板 0.20 / 北交所 0.30 / 创业板改革后 0.20
        (2021+ 的 28 只无一例外)、改革前 0.10。主板退股名为"XX退"不含 ST ⇒
        `is_st=False` ⇒ 本来就走 `_LIMIT["main"]`, 不需要任何特殊分支。
        """
        self.assertEqual(board.limit_ratio("600001.SH", False, "20220601", "某某退"), 0.10)
        self.assertEqual(board.limit_ratio("688001.SH", False, "20230601", "某某退"), 0.20)
        self.assertEqual(board.limit_ratio("830799.BJ", False, "20220601", "某某退"), 0.30)
        self.assertEqual(board.limit_ratio("300023.SZ", False, "20220609", "宝德退"), 0.20)
        self.assertEqual(board.limit_ratio("300023.SZ", False, "20180102", "某某退"), 0.10)
        # 名称含 ST **且**含"退"的: 走风险警示档(ST 优先于"退"的板块档)
        self.assertEqual(board.limit_ratio("600001.SH", True, "20200103", "ST某某退"), 0.05)
        self.assertEqual(board.limit_ratio("600001.SH", True, "20260911", "ST某某退"), 0.10)

    def test_chinext_delist_legacy_carryover(self):
        """创业板退市整理期**新老划断**: 改革前已进入整理期的存量股沿用 ±10%。

        `300216.SZ` 千山退: 整理期 20200805~20200915 **跨过**改革日 20200824,
        交易所 `stk_limit` 实测全程 lu/pc=1.1000(20200915: pc=0.19 → 涨停价 0.21,
        而 0.19×1.20=0.23 ⇒ 按改革后规则判会错)。改革**后**才进入整理期的则是 20%。
        """
        legacy = lambda code, d: "千山退" if d >= "20200805" else "千山制药"
        self.assertEqual(
            board.limit_ratio("300216.SZ", False, "20200824", "千山退", legacy), 0.10)
        self.assertEqual(
            board.limit_ratio("300216.SZ", False, "20200915", "千山退", legacy), 0.10)
        # 不传 name_of ⇒ 退化为改革后规则(已知且被登记的近似, 只影响 2020 年约 25 格)
        self.assertEqual(board.limit_ratio("300216.SZ", False, "20200824", "千山退"), 0.20)
        # 改革后才进入整理期(改革日当天名字不含"退") ⇒ 20%
        fresh = lambda code, d: "宝德股份" if d <= "20200824" else "宝德退"
        self.assertEqual(
            board.limit_ratio("300023.SZ", False, "20220609", "宝德退", fresh), 0.20)

        def _boom(code, d):
            raise AssertionError("name_of 不应对非退市格子调用")

        # name_of 只在"退"字格子上被调用 —— 普通格子传了也不查(性能前提)
        self.assertEqual(
            board.limit_ratio("300750.SZ", False, "20220609", "宁德时代", _boom), 0.20)
        self.assertEqual(
            board.limit_ratio("300750.SZ", False, "20180102", "宁德时代", _boom), 0.10)

    def test_chinext_reform_20200824(self):
        """创业板注册制改革(2020-08-24)前后限幅不同 —— 这是**限幅规则唯一一次变更**。

        缺陷原状: `limit_ratio` 没有日期参数, 把今天的 20% 套到 2017 年的 300xxx 上。
        后果与「旧基线买生产买不到的 ST/退市股」同类但方向相反: 涨停价被算高一倍幅度
        ⇒ 当天封死 +10% 涨停(生产买不到)的创业板票在回测里照样成交。
        交易所 `stk_limit` 对照: 2017~2019 年创业板 99.9~100% 是 10% 档、0% 是 20% 档。
        """
        # 改革前: 10%, ST 5%
        self.assertEqual(board.limit_ratio("300750.SZ", False, "20170103"), 0.10)
        self.assertEqual(board.limit_ratio("300750.SZ", True, "20170103"), 0.05)
        self.assertEqual(board.limit_ratio("301001.SZ", False, "20200821"), 0.10)
        # 改革当日即生效
        self.assertEqual(board.limit_ratio("300750.SZ", False, "20200824"), 0.20)
        self.assertEqual(board.limit_ratio("300750.SZ", True, "20200824"), 0.20)
        # 改革后不变
        self.assertEqual(board.limit_ratio("300750.SZ", False, "20260911"), 0.20)
        # 日期写法两种都认(引擎传 YYYYMMDD, 外部可能传 YYYY-MM-DD)
        self.assertEqual(board.limit_ratio("300750.SZ", False, "2017-01-03"), 0.10)

    def test_other_boards_are_date_invariant(self):
        """只有**创业板**(20200824)与**主板 ST**(20260706)两个档位随日期变 ——
        其余组合传不传日期必须**逐位相同**, 否则就是改多了。"""
        for code, st in (("688111.SH", False), ("688111.SH", True),
                         ("830799.BJ", False), ("830799.BJ", True),
                         ("002001.SZ", False)):
            base = board.limit_ratio(code, st)
            for d in ("20130101", "20170103", "20200823", "20200824", "20260706",
                      "20260911"):
                self.assertEqual(board.limit_ratio(code, st, d), base, (code, st, d))
        # 主板**非 ST** 也全程 10%(改革只动 ST 那一档)
        for d in ("20130101", "20200824", "20260706", "20260911"):
            self.assertEqual(board.limit_ratio("600000.SH", False, d), 0.10, d)

    def test_none_date_keeps_current_rules(self):
        """`dstr=None` = 现行规则 ⇒ 模拟盘/看板/下单这些只看今天的路径逐位不变。"""
        self.assertEqual(board.limit_ratio("300750.SZ"), 0.20)
        self.assertEqual(board.limit_ratio("300750.SZ", True), 0.20)
        self.assertEqual(board.limit_ratio_hist("20170103")("300750.SZ"), 0.10)
        self.assertEqual(board.limit_ratio_hist("20170103")("600000.SH", True), 0.05)

    def test_unparseable_date_is_conservative(self):
        """解析不了的日期走**历史档**(10%)而不是现行档(20%) —— 保守方向:
        限幅取小则涨停价取低, 更容易判「买不进」, 只会少成交不会多成交。"""
        self.assertEqual(board.limit_ratio("300750.SZ", False, "garbage"), 0.10)

    def test_round2_is_exact_decimal_half_up(self):
        """`round2` 必须等于**十进制精确半进位**, 而不是「差不多」。

        bug 原状(2026-09-12 修): `17.15 × 1.1` 在 float64 里是 `18.864999999999998`,
        ×100 后 `floor(+0.5)` 落到 1886 ⇒ 18.86, 而交易所公布 18.87。
        实测 2 位小数价格 × 四档限幅的 16 万种组合里 **0.83% 差 1 分**。
        这是 D06/D07 逐格比对残差分歧的主要来源。
        """
        from decimal import Decimal, ROUND_HALF_UP
        import random

        def exact(p, r):
            return float(Decimal(repr(round(p * (1 + r), 10)))
                         .quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

        # 钉死的边界样例(不是随机撞上的)
        self.assertEqual(board.round2(17.15 * 1.1), 18.87)
        self.assertEqual(board.round2(17.15 * 0.9), 15.44)
        self.assertEqual(board.round2(3.685), 3.69)
        # 全量对照
        random.seed(7)
        for _ in range(4000):
            p = round(random.uniform(1, 300), 2)
            for r in (0.05, 0.10, 0.20, 0.30):
                for s in (1, -1):
                    self.assertEqual(board.round2(p * (1 + r * s)), exact(p, r * s),
                                     (p, r, s))
        self.assertTrue(pd.isna(board.round2(float("nan"))), "NaN 必须透传")

    def test_no_limit_days(self):
        self.assertEqual(board.no_limit_trading_days("600000.SH"), 5)
        self.assertEqual(board.no_limit_trading_days("300750.SZ"), 5)
        self.assertEqual(board.no_limit_trading_days("688111.SH"), 5)
        self.assertEqual(board.no_limit_trading_days("830799.BJ"), 1)


class TestLotShares(unittest.TestCase):
    def test_multiple_boards(self):
        # 主板/创业板: 100 股整数倍
        self.assertEqual(board.lot_shares("600000.SH", 550), 500)
        self.assertEqual(board.lot_shares("300750.SZ", 299), 200)
        self.assertEqual(board.lot_shares("600000.SH", 50), 0)
        # 科创板: 最低 200, 其后 1 股递增
        self.assertEqual(board.lot_shares("688111.SH", 150), 0)
        self.assertEqual(board.lot_shares("688111.SH", 200), 200)
        self.assertEqual(board.lot_shares("688111.SH", 233), 233)
        # 北交所: 最低 100, 其后 1 股递增
        self.assertEqual(board.lot_shares("830799.BJ", 80), 0)
        self.assertEqual(board.lot_shares("830799.BJ", 100), 100)
        self.assertEqual(board.lot_shares("830799.BJ", 123), 123)


class TestSizingBoard(unittest.TestCase):
    def test_star_min_200_and_bse_1share_steps(self):
        top = pd.DataFrame({
            "close": [10.0, 10.0],
            "score": [0.9, 0.8],
        }, index=["688111.SH", "830799.BJ"])
        # 资金 5000, 2 槽 → 每槽 2500: 科创 2500/10=250 股(200起,1股递增); 北交 250 股
        out = allocate_lots(top, 5000, 2, cost_rate=0.0)
        self.assertEqual(out["shares"]["688111.SH"], 250)
        self.assertEqual(out["shares"]["830799.BJ"], 250)
        # 资金 3000 → 每槽 1500: 科创 150 股 < 200 起买 → 跳过; 北交 150 股
        out2 = allocate_lots(top, 3000, 2, cost_rate=0.0)
        self.assertNotIn("688111.SH", out2["shares"])
        self.assertEqual(out2["shares"]["830799.BJ"], 150)


_SCHEMA = """
CREATE TABLE paper_account (id INTEGER PRIMARY KEY, cash REAL, created_at TEXT,
                            last_applied_plan TEXT, initial_capital REAL);
CREATE TABLE paper_order (id INTEGER PRIMARY KEY AUTOINCREMENT, order_date TEXT,
                          ts_code TEXT, name TEXT, side TEXT, shares INTEGER,
                          reason TEXT, status TEXT DEFAULT 'pending', first_attempt TEXT,
                          created_at TEXT);
CREATE TABLE paper_position (ts_code TEXT PRIMARY KEY, name TEXT, shares INTEGER,
                             avg_cost REAL, entry_adj REAL);
CREATE TABLE paper_trade (id INTEGER PRIMARY KEY AUTOINCREMENT, exec_date TEXT,
                          ts_code TEXT, name TEXT, side TEXT, shares INTEGER,
                          price REAL, cost REAL, amount REAL, reason TEXT);
CREATE TABLE paper_nav (trade_date TEXT PRIMARY KEY, nav REAL, cash REAL,
                        market_value REAL, bench_nav REAL);
CREATE TABLE paper_interest (trade_date TEXT PRIMARY KEY, amount REAL);
CREATE TABLE repo_rate (trade_date TEXT PRIMARY KEY, overnight REAL, r7d REAL, r14d REAL);
CREATE TABLE trade_cal (cal_date TEXT PRIMARY KEY, is_open INTEGER);
CREATE TABLE stock_basic (ts_code TEXT PRIMARY KEY, list_date TEXT);
CREATE TABLE daily (ts_code TEXT, trade_date TEXT, open REAL, pre_close REAL, vol REAL, close REAL);
CREATE TABLE adj_factor (ts_code TEXT, trade_date TEXT, adj_factor REAL);
CREATE TABLE plan (plan_date TEXT, plan_type TEXT, ts_code TEXT, name TEXT,
                   weight REAL, shares INTEGER, industry TEXT, reason TEXT);
"""


class _ExecBase(unittest.TestCase):
    """撮合价/涨跌停板的夹具。

    ⚠️ 2026-09-22（结项审计复核）：本类与 `tests/_paper_base.py::_ExecBase` **同名但不同物**，
    是**有意**的 —— 两者看着像重复，实际是三处实质差异，合并会改变断言所依赖的口径：
      1. **建库内容**：本类不插 `paper_account`；`_paper_base` 那份要插（100,000 起始现金）
      2. **config 桩**：本类只桩 `single_side_cost/paper.slip/capital`；
         `_paper_base` 那份还显式钉住**旧扁平 0.3% 费用**（它整个文件的现金算术都按此口径写）
      3. **辅助方法**：本类只有 `_exec`；`_paper_base` 那份另有 `_one`/`_open_days` 等
     ⇒ 结论：**不是技术债，不要"顺手合并"**。审计曾把它列为重复代码，复核后判定为两个夹具。
    """
    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.path = tmp.name
        tmp.close()
        conn = sqlite3.connect(self.path)
        conn.executescript(_SCHEMA)
        conn.commit()
        conn.close()
        self.patch_conn = mock.patch.object(engine.db, "get_conn",
                                            side_effect=lambda: sqlite3.connect(self.path))
        self.patch_cfg = mock.patch.object(
            engine.cfg_mod, "load_config",
            return_value={"single_side_cost": 0.003, "paper": {"slip": 0.001},
                          "capital": 10000})
        self.patch_conn.start()
        self.patch_cfg.start()

    def tearDown(self):
        self.patch_cfg.stop()
        self.patch_conn.stop()
        os.unlink(self.path)

    def _exec(self, sql, args=()):
        conn = sqlite3.connect(self.path)
        conn.execute(sql, args)
        conn.commit()
        conn.close()


class TestExecPriceBoard(_ExecBase):
    def _insert_daily(self, code, d, o, pre, vol=100.0):
        self._exec("INSERT INTO daily (ts_code, trade_date, open, pre_close, vol, close) "
                   "VALUES (?,?,?,?,?,?)", (code, d, o, pre, vol, o))

    def test_missing_pre_close_is_visible(self):
        """2026-09-14：缺 `pre_close` ⇒ 涨跌停闸门被整段跳过（方向偏乐观）。

        实库有 193 行这样的数据（每只票在库内的首行）。**行为不变**（改它会与回测引擎
        分叉、动 pin），但必须留下告警 —— 否则"一字板也被放行"只会静默发生。
        红检：删掉那条 warning ⇒ 本用例红。
        """
        self._insert_daily("832317.BJ", "20260904", 12.0, None)
        conn = sqlite3.connect(self.path)
        try:
            with self.assertLogs("app.paper.engine", level="WARNING") as cm:
                px = engine._exec_price(conn, "832317.BJ", "20260904", "buy")
        finally:
            conn.close()
        self.assertIsNotNone(px, "行为不变：仍按放行处理")
        self.assertTrue(any("pre_close" in m for m in cm.output), cm.output)

    def test_chinext_18pct_open_is_buyable(self):
        # 创业板 +18% 开盘: 未到 +20% 涨停 → 可成交(旧 10% 口径会误判一字板)
        self._insert_daily("300750.SZ", "20260907", 11.8, 10.0)
        conn = sqlite3.connect(self.path)
        px = engine._exec_price(conn, "300750.SZ", "20260907", "buy")
        conn.close()
        self.assertIsNotNone(px)
        self.assertAlmostEqual(px, round(11.8 * 1.001, 3))

    def test_chinext_limit_up_blocked(self):
        # 创业板 +20% 一字板: 买不进
        self._insert_daily("300750.SZ", "20260907", 12.0, 10.0)
        conn = sqlite3.connect(self.path)
        px = engine._exec_price(conn, "300750.SZ", "20260907", "buy")
        conn.close()
        self.assertIsNone(px)

    def test_bse_25pct_open_is_buyable(self):
        # 北交所 +25%: 未到 +30% 涨停 → 可成交(旧 ±20% 脏数据防护会误挡)
        self._insert_daily("830799.BJ", "20260907", 12.5, 10.0)
        conn = sqlite3.connect(self.path)
        px = engine._exec_price(conn, "830799.BJ", "20260907", "buy")
        conn.close()
        self.assertIsNotNone(px)

    def test_star_st_limit_stays_20pct(self):
        # 科创板 ST 股: 限幅仍 20%(不收窄 5%)
        self._insert_daily("688111.SH", "20260907", 11.0, 10.0)
        conn = sqlite3.connect(self.path)
        px = engine._exec_price(conn, "688111.SH", "20260907", "buy",
                                name="ST某某")
        conn.close()
        self.assertIsNotNone(px)  # +10% < +20% 可买; 旧口径 ST→5% 会误判涨停

    def test_ipo_no_limit_window(self):
        # 主板新股上市第 3 个交易日: 无涨跌幅限制, +25% 开盘可成交
        self._exec("INSERT INTO stock_basic (ts_code, list_date) VALUES ('600999.SH','20260903')")
        for i, d in enumerate(("20260903", "20260904", "20260907"), 1):
            self._exec("INSERT INTO trade_cal VALUES (?,1)", (d,))
        self._insert_daily("600999.SH", "20260907", 12.5, 10.0)
        conn = sqlite3.connect(self.path)
        px = engine._exec_price(conn, "600999.SH", "20260907", "buy")
        conn.close()
        # 2026-09-13 强化(独立审查 §4.2)：原来只 assertIsNotNone，而注释描述的是
        # **"无涨跌停限制 ⇒ 高价也能成交"** 这个具体性质 ⇒ 断言强度要与注释匹配：
        # 成交价 = 今开 × (1 + 滑点)，且它**确实越过**了 ±10% 限幅(否则这条测试没测到窗口)。
        from app.paper.engine import _slip
        self.assertAlmostEqual(px, round(12.5 * (1 + _slip()), 3), places=3)
        self.assertGreater(px, 10.0 * 1.10, "12.5/10.0 = +25% 必须被放行才说明处在无限制窗口")

    def test_ipo_after_window_back_to_limit(self):
        # 上市第 6 个交易日(窗口后): 恢复 ±10% 限幅, +12% 开盘(一字板)买不进
        self._exec("INSERT INTO stock_basic (ts_code, list_date) VALUES ('600999.SH','20260901')")
        for d in ("20260901", "20260902", "20260903", "20260904", "20260907", "20260908"):
            self._exec("INSERT INTO trade_cal VALUES (?,1)", (d,))
        self._insert_daily("600999.SH", "20260908", 11.2, 10.0)
        conn = sqlite3.connect(self.path)
        px = engine._exec_price(conn, "600999.SH", "20260908", "buy")
        conn.close()
        self.assertIsNone(px)


class TestBseLimitPrice(unittest.TestCase):
    """北交所限价取整规则接线(2026-10-05): 上轨 floor、下轨 ceil(向 pre_close 收)。

    锚点 = 交易所 `stk_limit` 真实公布值(920002/920003.BJ @20260914), 与
    scripts/oneoff/diag_stklimit_window_check.py 的逐格验证同源 —— half-up 在
    这四个锚点上全部差 1 分, floor/ceil 全部位级一致。
    """

    def test_bse_floor_ceil_vs_halfup(self):
        # 920002.BJ 20260914: pc=48.86, 公布 63.51/34.21 —— half-up 给 63.52/34.20
        self.assertEqual(board.limit_price(48.86, 0.30, "bse"), 63.51)
        self.assertEqual(board.limit_price(48.86, -0.30, "bse"), 34.21)
        # 920003.BJ 20260914: pc=15.13, 公布 19.66/10.60 —— half-up 给 19.67/10.59
        self.assertEqual(board.limit_price(15.13, 0.30, "bse"), 19.66)
        self.assertEqual(board.limit_price(15.13, -0.30, "bse"), 10.60)

    def test_bse_integer_cents_same_as_halfup(self):
        # 整分位上两条规则同值(守卫 1e-9 不得把整数分位推过界)
        self.assertEqual(board.limit_price(10.00, 0.30, "bse"), 13.00)
        self.assertEqual(board.limit_price(10.00, -0.30, "bse"), 7.00)

    def test_non_bse_boards_bit_unchanged(self):
        # 沪深规则缺省/显式传板都走 round2 半进位 —— 既有口径逐位不变(不换 pin)
        self.assertEqual(board.limit_price(15.13, 0.30), 19.67)
        self.assertEqual(board.limit_price(15.13, 0.30, "main"), 19.67)
        self.assertEqual(board.limit_price(15.13, -0.30, "main"), 10.59)

    def test_bse_nan_passthrough(self):
        import math
        self.assertTrue(math.isnan(board.limit_price(float("nan"), 0.30, "bse")))


if __name__ == "__main__":
    unittest.main()
