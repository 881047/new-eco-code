"""袖套 KPI 的基准腿接线（2026-09-20 B1 采纳轮 S5）

`ledger.snapshot(benchmark=True)` 的 `excess_pp` 是用户目标线"周频超额 ≥ +10pp/年"看的那个
数（`api.py:327` 直送看板）。NAV 腿按 `adj_factor` 缩放股数 = 全收益，而基准腿以前直接
`cfg.get("benchmark","000906.SH")` 取价格指数 ⇒ 同一个 ~2.2pp/年 的偏差，占目标的 1/5~1/4。

⚠️ 这条**线上今天不可观测**：`snapshot()` 有"不足 8 周不报年化/超额"的守卫（实测 weeks=4、
`excess_pp=None`）。所以换尺只能在合成的 12 周账本上验，别在看板上找它。
"""
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

import pandas as pd        # noqa: F401  (与既有测试保持一致：Ledger 内部依赖 pandas 已加载)

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from app import config as cfg_mod              # noqa: E402
from app.weekly.ledger import Ledger           # noqa: E402

PR, TR = "000906.SH", "H00906.CSI"


def _index_db(rows):
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    path = tmp.name
    tmp.close()
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE index_daily (ts_code TEXT, trade_date TEXT, close REAL)")
    conn.executemany("INSERT INTO index_daily VALUES (?,?,?)", rows)
    conn.commit()
    conn.close()
    return path


class TestLedgerBenchLeg(unittest.TestCase):
    def setUp(self):
        # 12 个周度净值点（>8 周，越过样本守卫），每点 +1% ⇒ 年化是个正数
        start = date(2026, 7, 6)
        hist = [{"date": (start + timedelta(weeks=i)).strftime("%Y%m%d"),
                 "nav": 1_000_000 * (1.01 ** i)} for i in range(12)]
        fd, name = tempfile.mkstemp(suffix=".json")
        os.close(fd)                       # 不关掉这个 fd，Windows 下 tearDown 的 unlink 会 EBUSY
        jf = Path(name)
        self.addCleanup(jf.unlink, missing_ok=True)
        jf.write_text(json.dumps({"capital": 1_000_000, "inception": "20260706",
                                  "last_mark": hist[-1]["date"], "positions": {},
                                  "cash": 0.0, "trades": [], "interest": 0.0,
                                  "interest_upto": None, "halt": None,
                                  "nav_history": hist}), encoding="utf-8")
        # 必须走 load()：`Ledger(capital, path=...)` 在 state=None 时**新建空状态**，
        # 不会去读盘上那份 JSON（第一版测试就这么拿到空账本，note="还没有净值点"）。
        self.led = Ledger.load(1_000_000, path=jf)
        self.assertEqual(len(self.led.state["nav_history"]), 12)
        # PR 走平（年化 0），TR 每天 +0.02%（年化明显为正）⇒ 两把尺必须给出不同的 excess_pp
        days = [(start + timedelta(days=k)).strftime("%Y%m%d") for k in range(120)]
        rows = [(PR, d, 5000.0) for d in days]
        rows += [(TR, d, 5000.0 * (1.0002 ** k)) for k, d in enumerate(days)]
        self.db = _index_db(rows)
        self.addCleanup(lambda: Path(self.db).unlink(missing_ok=True))

    def _snap(self, nav_code):
        def _cfg():
            return {"benchmark": PR, "benchmark_nav_code": nav_code}
        with mock.patch.object(cfg_mod, "load_config", side_effect=_cfg), \
             mock.patch("app.weekly.ledger.db.get_conn",
                        side_effect=lambda: sqlite3.connect(self.db)):
            return self.led.snapshot(benchmark=True)

    def test_excess_pp_follows_the_nav_leg_not_the_price_leg(self):
        price = self._snap(PR)          # 缺省腿=价格指数 ⇒ 基准年化 0 ⇒ 超额=自身年化
        tr = self._snap(TR)             # 新尺 ⇒ 基准年化 > 0 ⇒ 超额被如实下修
        self.assertNotIn("note", price, f"样本守卫没被越过: {price}")
        self.assertEqual(price["bench_ann"], 0.0)
        self.assertGreater(tr["bench_ann"], 0.0,
                           "换到全收益腿后基准年化仍是 0 ⇒ ledger 没真的读 benchmark_nav_code")
        self.assertLess(tr["excess_pp"], price["excess_pp"],
                        "excess_pp 没被下修 ⇒ 袖套 KPI 还挂在价格指数上")
        yrs = 121 / 365.25
        want = round(price["ann_return"] - tr["bench_ann"], 4)
        self.assertAlmostEqual(tr["excess_pp"] / 100, want, places=4,
                               msg="excess_pp 不等于 自身年化 − 基准年化 ⇒ 算错了差值")

    def test_ledger_no_longer_invents_its_own_default(self):
        """第三份"000906.SH 自造缺省"必须消失（口径只有一个事实源，见 14 号纪律）。"""
        src = (BASE / "app" / "weekly" / "ledger.py").read_text(encoding="utf-8")
        self.assertNotIn('get("benchmark", "000906.SH")', src)
        self.assertIn("benchmark_nav_code()", src)


if __name__ == "__main__":
    unittest.main()
