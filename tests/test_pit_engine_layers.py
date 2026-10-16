"""PIT 六层过滤的**逐层**守卫（2026-09-13）

为什么写它：`app/pit/engine.py` 是全项目后果最重的模块 —— 本项目**最贵的一次错误**
（回测买过生产买不到的 ST/退市/停牌股，旧基线年化被高估 28.78pp）就出在这里的同类逻辑。
它的 docstring 把口径写成六层表，本文件就**按那六层逐条钉**，外加两条文档里点名的、
最容易被人"优化"掉的细节：

  ① 市值/流动性那两层**即使阈值是 0 也照过滤**（`mv.quantile(0)` = 截面最小，
     所以 NaN 市值仍被剔）—— 写成"阈值 0 就跳过"会**静默放宽生产口径**；
  ② `first_trade = max(数据首现, list_date)`：北交所在精选层时期就有新三板数据，
     只按"数据首现"会把上市前的记录当成可交易 ⇒ 前视。

以及 `audit()` 的承诺：**逐层统计的最终剩余必须等于 `tradable_mask` 的实际通过数**
（否则"过滤可解释"就是假的）。

运行: python -m unittest tests.test_pit_engine_layers -v
"""
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from app import db                                  # noqa: E402
from app.pit.engine import PITEngine                # noqa: E402

DT = "20260731"          # 评价日（周五）
CODES = ["000001.SZ", "000002.SZ", "000003.SZ", "000004.SZ",
         "000005.SZ", "000006.SZ", "688001.SH", "830001.BJ",
         "830002.BJ"]
CFG = {"star_enabled": False, "stock_size_pctl": 0, "stock_min_amt20": 0}

# 北交两票的分工（`first_trade = max(数据首现, list_date)` 的两个方向）：
#   830001.BJ → list_date **晚于面板末日**（未来上市）：被 clip 到末日，任何面板内日期都不可交易
#   830002.BJ → list_date 在面板内、但**晚于它的首个数据日**（精选层时期就有数据）：必须锚 list_date
BSE_FUTURE = "830001.BJ"
BSE_PRELISTED_DATA = "830002.BJ"


def _market() -> dict:
    """合成面板：每只股票只在一层上"违规"（见各列注释），便于逐层断言。"""
    dates = pd.bdate_range("2025-06-02", "2026-07-31")
    n, k = len(dates), len(CODES)
    idx = pd.DatetimeIndex(list(dates))              # 不带 freq，与生产一致
    close = pd.DataFrame(1.0, index=idx, columns=CODES)
    vol = pd.DataFrame(1e6, index=idx, columns=CODES)
    amount = pd.DataFrame(2e7, index=idx, columns=CODES)
    mv = pd.DataFrame(5e9, index=idx, columns=CODES)
    d = pd.Timestamp(DT)
    vol.loc[d, "000002.SZ"] = 0.0                    # ② 当日停牌
    close.iloc[:5, CODES.index("000004.SZ")] = np.nan  # ④ 次新（数据首现也晚）
    mv.loc[d, "000005.SZ"] = np.nan                  # ⑤ 市值 NaN（阈值 0 也要剔）
    # ⑥ 近 20 日均额**整段** NaN（不是只缺几天）：`mean()` 默认 skipna=True，只缺
    # 几天时均值仍是非 NaN（停牌日不参与均值），那种情况下本该放行 —— 别把两个口径搞混
    amount.iloc[-25:, CODES.index("000006.SZ")] = np.nan
    # 北交所两票的数据都从面板头就有（精选层时期），上市日均晚于数据首现
    return {"close": close, "vol": vol, "amount": amount, "total_mv": mv}


class TestPitLayers(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.path = tmp.name
        tmp.close()
        conn = sqlite3.connect(self.path)
        conn.executescript(db.SCHEMA)
        list_dates = {"000001.SZ": "20100101", "000002.SZ": "20100101",
                      "000003.SZ": "20100101", "000004.SZ": "20260720",   # 次新
                      "000005.SZ": "20100101", "000006.SZ": "20100101",
                      "688001.SH": "20190722",
                      "830001.BJ": "20261115",                             # 北交：**未来**上市
                      "830002.BJ": "20260210"}                             # 北交：面板内上市
        conn.executemany("INSERT INTO stock_basic (ts_code, symbol, name, industry, list_date) "
                         "VALUES (?,?,?,?,?)",
                         [(c, c[:6], "普通股", "银行", list_dates[c]) for c in CODES])
        # ③ 时点名称：000003 从 2026-07-01 起戴帽 ST（此前是正常股 ⇒ 之前应可交易）
        conn.executemany("INSERT INTO namechange VALUES (?,?,?,?,?,?)",
                         [("000003.SZ", "正常股", "20100101", "20260630", "20100101", "其他"),
                          ("000003.SZ", "ST测试", "20260701", None, "20260630", "ST")])
        conn.commit()
        conn.close()
        self.conn = sqlite3.connect(self.path)
        self.addCleanup(lambda: Path(self.path).unlink(missing_ok=True))
        self.addCleanup(self.conn.close)
        self.engine = PITEngine(m=_market(), cfg=CFG, conn=self.conn)

    def _mask(self, dt=DT, **kw):
        return self.engine.tradable_mask(dt, **kw)

    def test_only_the_clean_stock_passes(self):
        m = self._mask()
        self.assertTrue(bool(m["000001.SZ"]), "干净股应当可交易")
        for bad, why in (("000002.SZ", "停牌"), ("000003.SZ", "ST"), ("000004.SZ", "次新"),
                         ("000005.SZ", "市值 NaN"), ("000006.SZ", "均额 NaN"),
                         ("688001.SH", "科创"), (BSE_PRELISTED_DATA, "北交")):
            with self.subTest(code=bad):
                self.assertFalse(bool(m[bad]), f"{bad} 因{why}不该可交易")

    def test_star_and_bse_open_up_with_switches(self):
        self.assertTrue(bool(self._mask(star_enabled=True)["688001.SH"]))
        # 开关只放开**板块**这一层，别的层照旧：用一只已满 60 日的北交票测才有效
        # （830001.BJ 是未来上市，放开板块也不可能可交易 —— 那是 ① 层的职责）
        self.assertTrue(bool(self._mask(allow_bse=True)[BSE_PRELISTED_DATA]))
        self.assertFalse(bool(self._mask()[BSE_PRELISTED_DATA]))

    def test_pit_name_lets_a_stock_trade_before_it_becomes_st(self):
        """时点名称：000003 在戴帽**之前**必须可交易 —— 用当前名称过滤就是前视+幸存者偏差。"""
        self.assertTrue(bool(self._mask("20260601")["000003.SZ"]))
        self.assertFalse(bool(self._mask("20260731")["000003.SZ"]))

    def test_zero_threshold_still_filters_nan_rows(self):
        """阈值 0 ≠ 关掉这一层：NaN 市值 / NaN 均额在 `>= quantile(0)` / `>= 0` 下仍是 False。"""
        m = self._mask()
        self.assertFalse(bool(m["000005.SZ"]), "市值 NaN 被 0 阈值放过了（口径静默放宽）")
        self.assertFalse(bool(m["000006.SZ"]), "均额 NaN 被 0 阈值放过了（口径静默放宽）")

    def test_first_trade_is_anchored_at_list_date_not_first_data(self):
        """北交所票在上市前就有新三板数据 ⇒ `first_trade` 必须锚定 list_date（防前视）。

        两个方向都要钉：面板内上市（值必须**恰好**是 list_date，不是数据首现日）、
        面板后才上市（clip 到末日 ⇒ 面板内任何日期都不可交易）。
        """
        ft = self.engine._first_trade[BSE_PRELISTED_DATA]
        self.assertEqual(pd.Timestamp(ft), pd.Timestamp("2026-02-10"),
                         f"first_trade={ft} 取了数据首现日 ⇒ 上市前数据被当成可交易（前视）")
        ft_future = self.engine._first_trade[BSE_FUTURE]
        last = pd.Timestamp(self.engine.dates[-1])
        self.assertGreaterEqual(pd.Timestamp(ft_future), min(pd.Timestamp("2026-11-15"), last),
                                f"first_trade={ft_future} 早于 list_date/面板末日 ⇒ 前视")
        self.assertFalse(bool(self._mask(str(last.date()))[BSE_FUTURE]),
                         "未来上市的票在面板内任何日期都不该可交易")

    def test_audit_final_count_matches_the_real_mask(self):
        """`audit()` 的承诺：逐层累计的最终剩余 == `tradable_mask` 实际通过数。"""
        a = self.engine.audit(DT)
        self.assertIn("剩余", a.columns)
        self.assertEqual(int(a["剩余"].iloc[-1]), int(self._mask().sum()),
                         "审计表与真实掩码不一致 ⇒ '过滤可解释'是假的")
        self.assertLessEqual(int(a["剩余"].iloc[-1]), int(a["剩余"].iloc[0]))
        # 每层剔除数不许为负
        self.assertTrue((a["本层剔除"] >= 0).all())

    def test_module_level_wrapper_needs_no_cache(self):
        """一次性脚本用的模块级入口走同一口径（每次自建引擎）。

        包装函数**不接受 conn**（它是给一次性脚本连生产库用的）⇒ 想隔离就得从
        `db.get_conn` 这一层注入。这同时钉住"包装内部真的重新读了库（list_date / 时点名称）"，
        而不是把面板自带的东西当口径。
        """
        from app.pit.engine import tradable_mask as mod_mask
        path = self.path
        with mock.patch("app.db.get_conn", side_effect=lambda: sqlite3.connect(path)):
            dates = pd.to_datetime(_market()["close"].index)
            s = mod_mask(_market(), dates, pd.Timestamp(DT), cfg=CFG)
        self.assertEqual(int(s.fillna(False).sum()), int(self._mask().sum()))


if __name__ == "__main__":
    unittest.main()
