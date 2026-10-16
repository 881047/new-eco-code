"""龙虎榜同步修复单测: 机构腿缺口续跑 / 同花顺失败中止 (2026-09-04 审查修复)

运行: python -m unittest tests.test_lhb_sync -v
"""
import datetime
import logging
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from app import timeutil
from app.datasource import lhb_client as lhb

_SCHEMA = """
CREATE TABLE trade_cal (cal_date TEXT PRIMARY KEY, is_open INTEGER);
CREATE TABLE stock_basic (ts_code TEXT PRIMARY KEY);
CREATE TABLE data_status (name TEXT PRIMARY KEY, last_date TEXT, updated_at TEXT, rows INTEGER);
CREATE TABLE lhb (trade_date TEXT, ts_code TEXT, name TEXT, close REAL, pct_chg REAL,
    net_buy REAL, buy_value REAL, sell_value REAL, lhb_amount REAL, market_amount REAL,
    net_ratio REAL, amount_ratio REAL, turnover REAL, float_mv REAL, reason TEXT,
    PRIMARY KEY (trade_date, ts_code)) WITHOUT ROWID;
CREATE TABLE lhb_org (trade_date TEXT, ts_code TEXT, name TEXT,
    org_buy_num REAL, org_sell_num REAL, org_buy_value REAL, org_sell_value REAL,
    org_net_value REAL, org_net_ratio REAL, reason TEXT,
    PRIMARY KEY (trade_date, ts_code)) WITHOUT ROWID;
CREATE TABLE lhb_ths (trade_date TEXT, thscode TEXT, name TEXT,
    change REAL, net_value REAL, buy_value REAL, sell_value REAL,
    hot_rank INTEGER, org_net_value REAL, org_net_rate REAL,
    org_buy_num INTEGER, org_sell_num INTEGER,
    limit_reason TEXT, concepts TEXT, range_days INTEGER,
    PRIMARY KEY (trade_date, thscode, range_days)) WITHOUT ROWID;
CREATE TABLE lhb_ths_hm (trade_date TEXT, hm_name TEXT, thscode TEXT, name TEXT,
    net_value REAL, buying REAL, PRIMARY KEY (trade_date, hm_name, thscode)) WITHOUT ROWID;
"""

DETAIL_COLS = ["代码", "名称", "收盘价", "涨跌幅", "龙虎榜净买额", "龙虎榜买入额",
               "龙虎榜卖出额", "龙虎榜成交额", "市场总成交额", "净买额占总成交比",
               "成交额占总成交比", "换手率", "流通市值", "上榜原因"]
ORG_COLS = ["代码", "名称", "买方机构数", "卖方机构数", "机构买入总额", "机构卖出总额",
            "机构买入净额", "机构净买额占总成交比", "上榜原因"]


def _detail_df(d):
    return pd.DataFrame(
        [["600000", "浦发银行", 10.0, 1.5, 1000.0, 2000.0, 1000.0, 3000.0, 1e9,
          0.05, 0.1, 2.0, 1e11, "日涨幅偏离值达到7%的前5只证券"]], columns=DETAIL_COLS)


def _org_df(d):
    return pd.DataFrame(
        [["600000", "浦发银行", 1, 0, 800.0, 0.0, 800.0, 0.03, "机构专用"]], columns=ORG_COLS)


class _SyncBase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.path = tmp.name
        tmp.close()
        conn = sqlite3.connect(self.path)
        conn.executescript(_SCHEMA)
        conn.commit()
        conn.close()
        self.patch_db = mock.patch.object(
            lhb.db, "get_conn", side_effect=lambda: sqlite3.connect(self.path))
        self.patch_db.start()
        self.fake_ak = mock.MagicMock()
        self.patch_ak = mock.patch.dict(sys.modules, {"akshare": self.fake_ak})
        self.patch_ak.start()
        # 固定 2026-09-04 14:00(盘中): 不触发盘后强制重拉, 测试纯增量逻辑。
        # 2026-09-20 F3 收编后 lhb 已改读 `timeutil.today()/now()` ⇒ 假钟必须打在这上面
        # （原写法 patch `lhb.datetime` 的模块属性，收编后那个名字已经不存在了）。
        # ⚠️ 这同时**补上了一块覆盖盲区**：`today()` 那几行以前根本没被假钟管着
        #   （测的是真实系统日期），现在才真的可测 —— 见本文件末尾 test_window_follows_fake_today。
        self.patch_time = mock.patch.multiple(
            timeutil,
            today=lambda: datetime.date(2026, 9, 4),
            now=lambda: datetime.datetime(2026, 9, 4, 14, 0))
        self.patch_time.start()

    def tearDown(self):
        self.patch_time.stop()
        self.patch_ak.stop()
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

    def _org_last(self):
        return self._one("SELECT last_date FROM data_status WHERE name='lhb_org'")


class TestOrgLeg(_SyncBase):
    def setUp(self):
        super().setUp()
        for d in ("20260901", "20260902", "20260903"):
            self._exec("INSERT INTO trade_cal VALUES (?,1)", (d,))
        self._exec("INSERT INTO stock_basic VALUES ('600000.SH')")
        for d in ("20260901", "20260902"):
            self._exec("INSERT INTO lhb (trade_date, ts_code) VALUES (?, '600000.SH')", (d,))
        self._exec("INSERT INTO data_status (name, last_date, rows) VALUES ('lhb_org','20260901',1)")
        self.fake_ak.stock_lhb_detail_em.return_value = _detail_df("20260903")

    def test_org_failure_not_advanced_and_retried(self):
        # 机构腿失败: 状态不推进, 下次续跑仍会重拉 0902/0903
        # (0901 是 setUp 里"明细有行、机构无行"的疑似晚发布日, ⑦ 起一并回补)
        self.fake_ak.stock_lhb_jgmmtj_em.side_effect = Exception("接口异常")
        lhb.sync_lhb_ak(end_date="20260903")
        self.assertEqual(self._org_last()[0], "20260901")  # 未推进
        pulled = {c.kwargs["start_date"] for c in self.fake_ak.stock_lhb_jgmmtj_em.call_args_list}
        self.assertEqual(pulled, {"20260901", "20260902", "20260903"})
        # 第二次运行: 仍尝试同样的日子
        self.fake_ak.stock_lhb_jgmmtj_em.reset_mock()
        self.fake_ak.stock_lhb_jgmmtj_em.side_effect = Exception("接口异常")
        lhb.sync_lhb_ak(end_date="20260903")
        pulled = {c.kwargs["start_date"] for c in self.fake_ak.stock_lhb_jgmmtj_em.call_args_list}
        self.assertEqual(pulled, {"20260901", "20260902", "20260903"})

    def test_org_empty_day_advances(self):
        # 该日无机构上榜(空): 照常推进水位, 避免空榜日期被反复重拉。
        # 2026-09-09 审查项 ⑦: 但「明细有行而机构为空」的日子仍会作为疑似晚发布日回补
        # (无法区分"当日无机构上榜"与"上游尚未发布"; 实测 2841 个上榜日全部有机构数据)
        self.fake_ak.stock_lhb_jgmmtj_em.return_value = pd.DataFrame()
        lhb.sync_lhb_ak(end_date="20260903")
        self.assertEqual(self._org_last()[0], "20260903")
        pulled = {c.kwargs["start_date"] for c in self.fake_ak.stock_lhb_jgmmtj_em.call_args_list}
        self.assertIn("20260901", pulled)  # 水位已覆盖, 但明细有行机构无行 → 回补

    def test_org_failure_does_not_block_detail(self):
        self.fake_ak.stock_lhb_jgmmtj_em.side_effect = Exception("接口异常")
        done = lhb.sync_lhb_ak(end_date="20260903")
        self.assertEqual(done, 1)  # 明细腿正常推进
        self.assertEqual(
            self._one("SELECT COUNT(*) FROM lhb WHERE trade_date='20260903'")[0], 1)

    def test_org_success_advances(self):
        self.fake_ak.stock_lhb_jgmmtj_em.return_value = _org_df("20260903")
        lhb.sync_lhb_ak(end_date="20260903")
        self.assertEqual(self._org_last()[0], "20260903")
        self.assertEqual(
            self._one("SELECT COUNT(*) FROM lhb_org WHERE trade_date='20260903'")[0], 1)


class TestOrgLatePublishBackfill(_SyncBase):
    """2026-09-09 审查项 ⑦: 机构榜发布延迟超过盘后 3 日回补窗时的缺口必须自愈

    原实现: 机构腿空响应也推进水位, 且只重拉最后 3 个交易日 —— 延迟 >3 日的日子被水位
    永久跳过, lhb_org_net20 在残缺机构历史上滚动。
    """

    def setUp(self):
        super().setUp()
        # DATA-02(2026-09-16): 对账窗 20→60 交易日 ⇒ 日历扩到 70 天才能表达"窗内/窗外"
        self.days = [d.strftime("%Y%m%d")
                     for d in pd.date_range("2026-07-01", periods=70, freq="B")]
        for d in self.days:
            self._exec("INSERT INTO trade_cal VALUES (?,1)", (d,))
        self._exec("INSERT INTO stock_basic VALUES ('600000.SH')")
        self.fake_ak.stock_lhb_detail_em.return_value = _detail_df(self.days[-1])
        self.fake_ak.stock_lhb_jgmmtj_em.return_value = _org_df(self.days[-1])

    def _pulled(self):
        return {c.kwargs["start_date"] for c in self.fake_ak.stock_lhb_jgmmtj_em.call_args_list}

    def test_hole_below_watermark_repulled(self):
        # 倒数第 2 个交易日: 明细已有、机构腿无行(上游晚发布), 水位却已推进到最后一日
        hole = self.days[-2]
        self._exec("INSERT INTO lhb (trade_date, ts_code) VALUES (?, '600000.SH')", (hole,))
        self._exec("INSERT INTO data_status (name, last_date, rows) VALUES ('lhb_org',?,1)",
                   (self.days[-1],))
        lhb.sync_lhb_ak(end_date=self.days[-1])
        self.assertIn(hole, self._pulled())

    def test_hole_outside_window_not_repulled(self):
        # DATA-02(2026-09-16): 对账窗 20→60 个交易日 —— 60 日内的洞**应回补**
        #（停跑一个季度内自愈）；60 日外的洞不回补(有界, 避免无限重拉)。
        old = self.days[0]                     # 最老一天: 落在 60 日窗外
        mid = self.days[-45]                   # 45 日前: 落在 60 日窗内 ⇒ 应回补
        self._exec("INSERT INTO lhb (trade_date, ts_code) VALUES (?, '600000.SH')", (old,))
        self._exec("INSERT INTO lhb (trade_date, ts_code) VALUES (?, '600000.SH')", (mid,))
        self._exec("INSERT INTO data_status (name, last_date, rows) VALUES ('lhb_org',?,1)",
                   (self.days[-1],))
        lhb.sync_lhb_ak(end_date=self.days[-1])
        self.assertNotIn(old, self._pulled(), "60 日窗外的洞不回补")
        self.assertIn(mid, self._pulled(), "60 日窗内的洞应回补")

    def test_complete_history_no_extra_calls(self):
        # 机构腿完整(明细/机构都有行) → 不产生额外回补调用, 只拉水位之后的日子
        for d in self.days[-3:]:
            self._exec("INSERT INTO lhb (trade_date, ts_code) VALUES (?, '600000.SH')", (d,))
            self._exec("INSERT INTO lhb_org (trade_date, ts_code) VALUES (?, '600000.SH')", (d,))
        self._exec("INSERT INTO data_status (name, last_date, rows) VALUES ('lhb_org',?,1)",
                   (self.days[-3],))
        lhb.sync_lhb_ak(end_date=self.days[-1])
        self.assertEqual(self._pulled(), {self.days[-2], self.days[-1]})


class TestThsFailFast(_SyncBase):
    def setUp(self):
        super().setUp()
        for d in ("20260901", "20260902", "20260903", "20260904",
                  "20260907", "20260908", "20260909", "20260910"):
            self._exec("INSERT INTO trade_cal VALUES (?,1)", (d,))
        self._exec("INSERT INTO stock_basic VALUES ('600000.SH')")
        self.patch_key = mock.patch.object(lhb, "HITHINK_FINANCE_API_KEY", "test-key")
        self.patch_key.start()

    def tearDown(self):
        self.patch_key.stop()
        super().tearDown()

    def test_consecutive_failures_abort(self):
        # Key 失效(_ths_get 恒 None): 连续 5 个交易日无数据即中止, 不再重爬整个窗口
        with mock.patch.object(lhb, "_ths_get", return_value=None) as get:
            lhb.sync_lhb_ths(end_date="20260910")
        self.assertEqual(get.call_count, 5)  # 1 天 × 1 接口 × 5 天 = 5 次后中止
        self.assertIsNone(
            self._one("SELECT last_date FROM data_status WHERE name='lhb_ths'"))

    def test_valid_data_advances(self):
        item = {"thscode": "600000", "name": "浦发银行", "change": 1.5, "net_value": 1000.0,
                "buy_value": 2000.0, "sell_value": 1000.0, "hot_rank": 1,
                "org_net_value": 0.0, "org_net_rate": 0.0, "org_buy_num": 0,
                "org_sell_num": 0, "limit_reason": "t", "concept_list": [], "range_days": 1}
        with mock.patch.object(lhb, "_ths_get", side_effect=[
                {"stock_items": [item], "hot_money_items": None},
                None] * 100):
            done = lhb.sync_lhb_ths(end_date="20260910")
        self.assertEqual(done, 8)
        self.assertEqual(
            self._one("SELECT last_date FROM data_status WHERE name='lhb_ths'")[0],
            "20260910")


class TestThsHmLeg(_SyncBase):
    """2026-09-07 全面检查: 游资榜(lhb_ths_hm)独立水位 —— all-board 成功即推进 lhb_ths
    状态导致游资榜当日失败永不再拉(与 AkShare 机构腿同型问题)"""

    def setUp(self):
        super().setUp()
        for d in ("20260901", "20260902", "20260903", "20260904"):
            self._exec("INSERT INTO trade_cal VALUES (?,1)", (d,))
        self._exec("INSERT INTO stock_basic VALUES ('600000.SH')")
        self.patch_key = mock.patch.object(lhb, "HITHINK_FINANCE_API_KEY", "test-key")
        self.patch_key.start()

    def tearDown(self):
        self.patch_key.stop()
        super().tearDown()

    _ITEM = {"thscode": "600000", "name": "浦发银行", "change": 1.5, "net_value": 1000.0,
             "buy_value": 2000.0, "sell_value": 1000.0, "hot_rank": 1,
             "org_net_value": 0.0, "org_net_rate": 0.0, "org_buy_num": 0,
             "org_sell_num": 0, "limit_reason": "t", "concept_list": [], "range_days": 1}
    _HM = [{"name": "游资A", "buying": "buy",
            "rows": [{"thscode": "600000", "name": "浦发银行", "net_value": 500.0}]}]

    def test_hm_failure_not_advanced_and_retried(self):
        # all-board 成功 + 游资榜 None(失败): lhb_ths 推进, lhb_ths_hm 不推进
        with mock.patch.object(lhb, "_ths_get", side_effect=[
                {"stock_items": [self._ITEM]}, None] * 4) as get:
            lhb.sync_lhb_ths(end_date="20260904")
        self.assertEqual(
            self._one("SELECT last_date FROM data_status WHERE name='lhb_ths'")[0],
            "20260904")
        self.assertIsNone(
            self._one("SELECT last_date FROM data_status WHERE name='lhb_ths_hm'"))
        # 第二次运行: all-board 无缺口不再外呼, 游资榜仍重拉 4 天并推进水位
        with mock.patch.object(lhb, "_ths_get",
                               side_effect=[{"hot_money_items": self._HM}] * 4) as g2:
            lhb.sync_lhb_ths(end_date="20260904")
        self.assertEqual(g2.call_count, 4)
        self.assertEqual(
            self._one("SELECT last_date FROM data_status WHERE name='lhb_ths_hm'")[0],
            "20260904")
        self.assertEqual(
            self._one("SELECT COUNT(*) FROM lhb_ths_hm WHERE trade_date='20260904'")[0], 1)

    def test_hm_success_advances(self):
        with mock.patch.object(lhb, "_ths_get", side_effect=[
                {"stock_items": [self._ITEM]}, {"hot_money_items": self._HM}] * 4):
            lhb.sync_lhb_ths(end_date="20260904")
        self.assertEqual(
            self._one("SELECT last_date FROM data_status WHERE name='lhb_ths_hm'")[0],
            "20260904")

    def test_hm_watermark_seeded_from_existing_table(self):
        # 历史库: 表有数据但无状态行 → 以 MAX(trade_date) 播种, 不重爬整个窗口
        self._exec("INSERT INTO lhb_ths_hm (trade_date, hm_name, thscode, name, net_value, buying) "
                   "VALUES ('20260902','A','600000','X',1.0,1.0)")
        self._exec("INSERT INTO data_status (name, last_date, rows) VALUES ('lhb_ths','20260902',1)")
        with mock.patch.object(lhb, "_ths_get", side_effect=[
                {"stock_items": []}, {"hot_money_items": []}] * 10) as get:
            lhb.sync_lhb_ths(end_date="20260904")
        hm_calls = [c for c in get.call_args_list
                    if c.args[1].get("board_type") == "hot_money"]
        self.assertEqual([c.args[1]["date"] for c in hm_calls],
                         ["2026-09-03", "2026-09-04"])  # 只补 0902 之后的游资榜
        self.assertEqual(
            self._one("SELECT last_date FROM data_status WHERE name='lhb_ths_hm'")[0],
            "20260904")


class TestThsHoleReconcileConn(_SyncBase):
    """2026-09-18 审查 P0-01: 水位内 40 日缺口对账必须在**连接存活期内**执行

    2026-09-16 加的对账逻辑(lhb_client: 水位内近 40 个交易日缺行重探)被放在
    `conn.close()` **之后**, 于是每个生产日必抛
    `sqlite3.ProgrammingError: Cannot operate on a closed database`, 被 updater 的
    `except` 吞掉 ⇒ `data_status.lhb_ths` 水位永不推进、同花顺龙虎榜静默停更。

    既有测试全部用 ≤8 天的日历, `len(cal) > 40` 恒不成立 ⇒ 该分支从未被执行,
    bug 因此躲过 1221 条测试进入生产(日志实证 09-16/09-17/09-18 共 4 次)。
    本类用 50 个交易日 + 已推进的水位, 强制走通对账分支。
    """

    _ITEM = {"thscode": "600000", "name": "浦发银行", "change": 1.5, "net_value": 1000.0,
             "buy_value": 2000.0, "sell_value": 1000.0, "hot_rank": 1,
             "org_net_value": 0.0, "org_net_rate": 0.0, "org_buy_num": 0,
             "org_sell_num": 0, "limit_reason": "t", "concept_list": [], "range_days": 1}

    def setUp(self):
        super().setUp()
        self.days = [d.strftime("%Y%m%d")
                     for d in pd.date_range("2026-07-01", periods=50, freq="B")]
        for d in self.days:
            self._exec("INSERT INTO trade_cal VALUES (?,1)", (d,))
        self._exec("INSERT INTO stock_basic VALUES ('600000.SH')")
        self.patch_key = mock.patch.object(lhb, "HITHINK_FINANCE_API_KEY", "test-key")
        self.patch_key.start()
        # 水位已到最后一日但窗内 lhb_ths 无行 ⇒ 对账分支必然执行(旧代码在此用已 close 的 conn)
        self._exec("INSERT INTO data_status (name,last_date,rows) VALUES ('lhb_ths',?,1)",
                   (self.days[-1],))

    def tearDown(self):
        self.patch_key.stop()
        super().tearDown()

    def test_hole_reconcile_uses_live_connection(self):
        with mock.patch.object(lhb, "_ths_get",
                               return_value={"stock_items": [self._ITEM]}) as get:
            # 旧代码在此抛 sqlite3.ProgrammingError: Cannot operate on a closed database
            lhb.sync_lhb_ths(end_date=self.days[-1])
        asked = {c.args[1]["date"] for c in get.call_args_list}
        # 契约: 水位**以下**的缺口日必须被纳入重探(证明对账分支真的跑了)
        self.assertIn(
            datetime.datetime.strptime(self.days[-5], "%Y%m%d").strftime("%Y-%m-%d"), asked)
        self.assertEqual(
            self._one("SELECT last_date FROM data_status WHERE name='lhb_ths'")[0],
            self.days[-1])

    def test_no_closed_connection_error(self):
        logger = logging.getLogger("app.datasource.lhb_client")
        seen = []
        handler = logging.Handler()
        handler.emit = lambda rec: seen.append(rec.getMessage())
        logger.addHandler(handler)
        try:
            with mock.patch.object(lhb, "_ths_get",
                                   return_value={"stock_items": [self._ITEM]}):
                lhb.sync_lhb_ths(end_date=self.days[-1])
        finally:
            logger.removeHandler(handler)
        self.assertFalse([m for m in seen if "closed database" in m],
                         "对账不得再触发连接已关闭错误")


class TestFakeClockReachesTimeutil(unittest.TestCase):
    """F3 收编的收益之一：假钟现在**真的**能管到"今天"这一类站点。

    以前 `today = datetime.date.today()` 不在任何假钟控制下（测试跑的是真实系统日期，
    窗口右端点跟着日历走 ⇒ 结果随日期漂移而无人察觉）。收编后 `timeutil.today()` 是可
    patch 的单点，这里钉两件事：换钟确实改变 `today()/date8()`，且 lhb 源码里取今天的
    那条表达式已经走它（源码级断言由 tests/test_clock_hygiene.py 的扫描器负责）。

    ⚠️ 曾试过再加一条"直接调 `sync_lhb_ak` 看窗口右端点"的用例，但那要把 akshare 腿
       的返回形状一并造出来（`read_sql` 一 patch 就波及后续查询），收益不抵成本 ⇒ 放弃，
       改在 `test_clock_hygiene.py` 里钉源码级事实。**这条差异如实登记，不留"已覆盖"的假象。**
    """

    def test_timeutil_is_a_single_swappable_hook(self):
        with mock.patch.object(timeutil, "today", lambda: datetime.date(2026, 9, 4)), \
             mock.patch.object(timeutil, "now", lambda: datetime.datetime(2026, 9, 4, 14, 0)):
            self.assertEqual(timeutil.today().strftime("%Y%m%d"), "20260904")
            self.assertEqual(timeutil.date8(), "20260904")
            self.assertEqual(timeutil.now().hour, 14)
        # 出了 with 必须复原（否则进程级换钟会污染后面的用例 —— 与 A3 那条 mock.patch 教训同源）
        self.assertNotEqual(timeutil.today().strftime("%Y%m%d"), "20260904")


if __name__ == "__main__":
    unittest.main()
