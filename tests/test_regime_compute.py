"""市场状态 `compute_regime()` 的守卫测试（2026-09-13）

为什么单测它：它决定**月频系统的建议仓位**（attack/neutral/defense → `regime_equity_weight`），
也是看板上那块"月频建议仓位"的来源；而 `compute_regime` 主体此前**零覆盖**（覆盖率 48%）。
更要紧的是：它的注释里写着两条**明确承诺**，必须被测试钉住 ——

  ① **R18-1**：估值接口挂了也要"**价格层永远照算照写**"（转熊时不降仓比翻口径更危险）；
  ② **R19-3**：`regime_use_valuation=false` 时**不许白构造 TushareClient**（省配额）。

以及两条边界（都是真实事故换来的）：
  ③ 指数数据为空 ⇒ 返回 `{}`（不炸）；
  ④ 基准指数史不足 250 日 ⇒ 返回 `{}` 且**不写库**（原实现 `iloc` IndexError 崩溃且反复重试）。

做法：临时库（真 `db.SCHEMA`）+ 假 TushareClient（不联网、不耗配额）+ 合成指数序列。

运行: python -m unittest tests.test_regime_compute -v
"""
import contextlib
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from app import db                              # noqa: E402
from app.models import regime as rg             # noqa: E402


class _FakeTs:
    """假 Tushare 客户端：可配置"接口不可用"，并提供合成 PE 序列。"""

    def __init__(self, available=True, pe=None, explode=False):
        if explode:
            raise AssertionError("不该构造 TushareClient（regime_use_valuation=false）")
        self.available_flag = available
        self.pe = pe
        self.calls = []

    def available(self, api, **kw):
        self.calls.append(("available", api))
        return self.available_flag

    def call(self, api, **kw):
        self.calls.append(("call", api))
        return pd.DataFrame() if self.pe is None else self.pe


def _index_frame(days: int, start="2024-01-02") -> pd.DataFrame:
    idx = pd.bdate_range(start, periods=days)
    px = 4000 + np.cumsum(np.random.default_rng(7).normal(0, 12, days))
    return pd.DataFrame({"trade_date": idx.strftime("%Y%m%d"), "close": px})


class TestComputeRegime(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.path = tmp.name
        tmp.close()
        conn = sqlite3.connect(self.path)
        conn.executescript(db.SCHEMA)               # 真 schema（表结构不会与生产漂移）
        conn.commit()
        conn.close()

        @contextlib.contextmanager
        def _ctx():
            c = sqlite3.connect(self.path)
            try:
                yield c
                c.commit()
            finally:
                c.close()
        self._pdb = mock.patch.object(rg.db, "conn_ctx", _ctx)
        self._pdb.start()
        self.addCleanup(self._pdb.stop)
        self.addCleanup(lambda: Path(self.path).unlink(missing_ok=True))
        self.cfg = {"regime_use_valuation": True, "regime_style_guard": {}}
        # regime.py 在函数体内 `from app import config as cfg_mod` ⇒ 只能 patch 模块本体
        self._pcfg = mock.patch("app.config.load_config", side_effect=lambda *a, **k: self.cfg)
        self._pcfg.start()
        self.addCleanup(self._pcfg.stop)

    def _insert_index(self, days: int):
        conn = sqlite3.connect(self.path)
        df = _index_frame(days)
        df.insert(0, "ts_code", "000906.SH")
        df.to_sql("index_daily", conn, if_exists="append", index=False)
        conn.commit()
        conn.close()

    def _rows(self, sql):
        conn = sqlite3.connect(self.path)
        try:
            return conn.execute(sql).fetchall()
        finally:
            conn.close()

    def test_empty_index_returns_empty_dict(self):
        out = rg.compute_regime()
        self.assertEqual(out, {})
        self.assertEqual(self._rows("SELECT COUNT(*) FROM regime")[0][0], 0)

    def test_short_history_returns_empty_and_writes_nothing(self):
        """基准指数史不足 250 日 ⇒ 返回 {}（原实现 iloc IndexError 崩溃且反复重试）。"""
        self._insert_index(120)
        with mock.patch.object(rg, "TushareClient", lambda: _FakeTs(available=False)):
            out = rg.compute_regime()
        self.assertEqual(out, {})
        self.assertEqual(self._rows("SELECT COUNT(*) FROM regime")[0][0], 0)

    def test_normal_path_writes_regime_and_valuation_status(self):
        self._insert_index(600)
        pe = _index_frame(600).rename(columns={"close": "pe_ttm"})
        with mock.patch.object(rg, "TushareClient", lambda: _FakeTs(True, pe)):
            out = rg.compute_regime()
        self.assertIn("last_state", out)
        self.assertIn(out["last_state"], ("attack", "neutral", "defense"))
        self.assertIsInstance(out["score"], float)
        n_regime = self._rows("SELECT COUNT(*) FROM regime")[0][0]
        self.assertGreater(n_regime, 250, "价格层必须落库")
        status = dict(self._rows("SELECT name, last_date FROM data_status"))
        self.assertIn("regime", status)
        self.assertIn("regime_valuation", status)
        self.assertNotEqual(status["regime_valuation"], "degraded",
                            "估值正常时不该标成 degraded")
        self.assertGreater(self._rows("SELECT COUNT(*) FROM valuation_pe")[0][0], 250,
                           "拉到 PE 后应落 valuation_pe 表")

    def test_price_layer_updates_even_when_valuation_is_down(self):
        """R18-1 承诺：估值接口挂了，**价格层照算照写**，并标 degraded 供状态页告警。"""
        self._insert_index(600)
        pe = _index_frame(600).rename(columns={"close": "pe_ttm"})
        conn = sqlite3.connect(self.path)
        pe.to_sql("valuation_pe", conn, if_exists="append", index=False)
        conn.commit()
        conn.close()
        with mock.patch.object(rg, "TushareClient", lambda: _FakeTs(available=False)):
            out = rg.compute_regime()
        self.assertIn("last_state", out)
        self.assertGreater(self._rows("SELECT COUNT(*) FROM regime")[0][0], 250,
                           "估值层不可用时价格层也必须照写（R18-1）")
        status = dict(self._rows("SELECT name, last_date FROM data_status"))
        self.assertEqual(status.get("regime_valuation"), "degraded",
                         "降级必须留痕到 data_status，否则状态页看不见")

    def test_valuation_disabled_never_builds_client(self):
        """R19-3 承诺：关估值层时不构造 TushareClient（`explode=True` 会抛错）。"""
        self._insert_index(600)
        self.cfg = {"regime_use_valuation": False, "regime_style_guard": {}}
        with mock.patch.object(rg, "TushareClient", lambda: _FakeTs(explode=True)):
            out = rg.compute_regime()
        self.assertIn("last_state", out)
        self.assertGreater(self._rows("SELECT COUNT(*) FROM regime")[0][0], 250)

    def test_no_valuation_history_falls_back_to_price_only(self):
        """估值接口不可用 **且** 库里没有任何历史 PE ⇒ 按纯价格体系算，并标 degraded_nohistory。

        与上一条（有历史 ⇒ ffill 降级）区分开：这两种降级的含义不同，状态页必须分得清。
        """
        self._insert_index(600)
        with mock.patch.object(rg, "TushareClient", lambda: _FakeTs(available=False)):
            out = rg.compute_regime()
        self.assertIn("last_state", out)
        self.assertGreater(self._rows("SELECT COUNT(*) FROM regime")[0][0], 250,
                           "没有估值历史也必须照出价格层状态")
        status = dict(self._rows("SELECT name, last_date FROM data_status"))
        self.assertEqual(status.get("regime_valuation"), "degraded_nohistory")


if __name__ == "__main__":
    unittest.main()
