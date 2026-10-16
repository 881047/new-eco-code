"""两腿基准的接线守卫（2026-09-20 B1 采纳轮 S4）

金标准夹具与 `config.yaml` 解耦（`_FIXED_CFG` 自己钉死两把键）⇒ 换报告尺对那份快照**隐形**
（三补二十六 §④ 早已预警）。所以新键的接线必须由本文件单独钉：

  ① 两条腿各随各的键，且互不回落；
  ② 只换净值腿 ⇒ **只有** `bench_ann/excess_ann/win_rate`（和 nav 表的 `bench_nav` 列）变，
     其余全部逐位不变（= "这条尺只改报出来的数、不改决策"的机器版本）；
  ③ 价量因子（含 `beta_250`）在两种尺下逐位相同 —— 防止 `bench_tr` 串进特征腿；
  ④ 行情面板缓存指纹必须随净值键变化（摘掉 `bench_tr=` 那一项就红）；
  ⑤ 只该用价格指数的四个消费方（regime 择时 / style 风格 / 模拟盘基准列 / 邮件文案）
     不许出现 `benchmark_nav_code`（"顺手统一" = 悄悄换策略输入）；
  ⑥ 净值键配错（表里没这个码）⇒ 全 NaN 的**显式**空腿，而不是崩或静默换尺。

②③④⑥ 都带了"这条测试不是空的"的正向断言（照三补二十七 A4 的教训：能让结果看着对、
构造却是错的实现，只有正检查抓得住）。
"""
import os
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
sys.path.insert(0, str(BASE / "tests"))

from app import config as cfg_mod                                   # noqa: E402
from app import db                                                  # noqa: E402
from app.backtest import engine as bt_engine                        # noqa: E402
from app.factors import factor_engine as fe                         # noqa: E402
from test_backtest_golden import _FIXED_CFG, _SCHEMA, _universe     # noqa: E402

PR, TR = "000906.SH", "H00906.CSI"
# 允许随尺变化的字段；其余必须逐位相同
REPORT_ONLY = {"bench_ann", "excess_ann", "win_rate"}
_PATH = None
_DFS = None


def _universe_two_legs() -> dict:
    """golden 宇宙 + 第二条基准腿：TR = PR × (1+0.01%)^k。
    逐日**复利**而非恒定加数 ⇒ 每期的抬高量随当月天数变化 ⇒ `win_rate` 才可能变；
    若两腿只差一个常数，②的"确实变了"会退化成只测年化。"""
    dfs = _universe()
    px = dfs["index_daily"].copy()          # _universe 里就是 000906.SH
    k = np.arange(len(px), dtype=np.float64)
    tr = px.copy()
    tr["ts_code"] = TR
    tr["close"] = px["close"].to_numpy(dtype=np.float64) * np.power(1.0001, k)
    dfs["index_daily"] = pd.concat([px, tr], ignore_index=True)
    return dfs


def _make_db(dfs: dict) -> str:
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    path = tmp.name
    tmp.close()
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA)
    for name, df in dfs.items():
        df.to_sql(name, conn, if_exists="append", index=False)
    conn.commit()
    conn.close()
    return path


def _cfg(benchmark: str, nav: str) -> dict:
    cfg = dict(_FIXED_CFG)
    cfg["benchmark"] = benchmark
    cfg["benchmark_nav_code"] = nav
    return cfg


def _panel(path: str, benchmark: str, nav: str) -> dict:
    with mock.patch.object(cfg_mod, "load_config", return_value=_cfg(benchmark, nav)), \
         mock.patch.object(db, "get_conn", side_effect=lambda: sqlite3.connect(path)):
        return fe.load_market(use_cache=False)      # 临时库不参与缓存，也别去碰生产缓存


def _backtest(path: str, benchmark: str, nav: str, tag: str) -> tuple:
    with mock.patch.object(cfg_mod, "load_config", return_value=_cfg(benchmark, nav)), \
         mock.patch.object(db, "get_conn", side_effect=lambda: sqlite3.connect(path)):
        metrics = bt_engine.run_backtest(run_tag=tag)
    conn = sqlite3.connect(path)
    try:
        # 必须按 run_date 过滤：两臂的 nav 行都落在同一张临时表里（引擎按 tag 各写一份），
        # 不过滤会把两臂的行叠成 58 行 —— 那正是金标准测试单跑一次时看不见形状问题的地方
        nav_df = pd.read_sql("SELECT trade_date, nav, sys_nav, bench_nav FROM backtest_nav "
                             "WHERE run_date=? ORDER BY trade_date",
                             conn, params=(metrics["run_date"],))
    finally:
        conn.close()
    return metrics, nav_df


class TestBenchLegWiring(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        global _PATH, _DFS
        if _PATH is None:
            _DFS = _universe_two_legs()
            _PATH = _make_db(_DFS)

    @classmethod
    def tearDownClass(cls):
        # 临时库整个丢掉 ⇒ 不留在 tests/ 或 data/ 里冒充证据
        global _PATH
        if _PATH and os.path.exists(_PATH):
            os.unlink(_PATH)
        _PATH = None

    # ---------------- ① 两腿各随各的键 ----------------
    def test_each_leg_follows_its_own_key(self):
        px = _DFS["index_daily"]
        pr = px[px.ts_code == PR].set_index("trade_date")["close"].astype(np.float32)
        tr = px[px.ts_code == TR].set_index("trade_date")["close"].astype(np.float32)

        m = _panel(_PATH, PR, TR)
        got_pr, got_tr = m["bench"], m["bench_tr"]
        last = got_pr.index[-1].strftime("%Y%m%d")
        self.assertEqual(got_pr.iloc[-1], pr.loc[last], "bench 腿取错了码")
        self.assertEqual(got_tr.iloc[-1], tr.loc[last], "bench_tr 腿取错了码")
        self.assertFalse(np.allclose(got_pr.to_numpy(), got_tr.to_numpy()),
                         "两腿数值相同 ⇒ 本文件所有对比都是空的")

        sw = _panel(_PATH, TR, PR)        # 互换两把键 ⇒ 两条腿必须跟着互换
        pd.testing.assert_series_equal(sw["bench"], m["bench_tr"], check_names=False)
        pd.testing.assert_series_equal(sw["bench_tr"], m["bench"], check_names=False)

    # ---------------- ② 只换净值腿 ⇒ 只有报告字段变 ----------------
    def test_switching_nav_leg_moves_only_report_fields(self):
        mA, navA = _backtest(_PATH, PR, PR, "wire_price")   # 旧尺：两腿都是价格指数
        mB, navB = _backtest(_PATH, PR, TR, "wire_tr")      # 新尺：只换净值腿

        # 两臂用了不同 run_tag ⇒ run_date 必然不同（它不是指标）；其余字段才是要比对的
        keys = {k for k in mA if k != "run_date"}
        moved = {k for k in keys if mA.get(k) != mB.get(k)}
        self.assertLessEqual(moved - REPORT_ONLY, set(),
                             f"越界的字段被换尺动到了: {sorted(moved - REPORT_ONLY)}")
        self.assertEqual(moved, REPORT_ONLY,
                         f"报告字段没全变（多半是回测没真的走 bench_tr）: {sorted(moved)}")
        pd.testing.assert_frame_equal(navA[["trade_date", "nav", "sys_nav"]],
                                      navB[["trade_date", "nav", "sys_nav"]])
        a = navA["bench_nav"].to_numpy(dtype=np.float64)
        b = navB["bench_nav"].to_numpy(dtype=np.float64)
        self.assertFalse(np.allclose(a, b), "nav 表的 bench_nav 列没变 ⇒ 报告腿没换")

    # ---------------- ③ 因子腿不许串味 ----------------
    def test_price_factors_identical_under_both_scales(self):
        fA = fe.compute_price_factors(_panel(_PATH, PR, PR))
        fB = fe.compute_price_factors(_panel(_PATH, PR, TR))
        self.assertEqual(set(fA), set(fB), "因子集形状变了")
        self.assertGreaterEqual(len(fA), 20, f"因子数不对: {len(fA)}")
        for k in sorted(fA):
            pd.testing.assert_frame_equal(fA[k], fB[k], check_exact=True, obj=f"因子 {k}")

    # ---------------- ④ 缓存指纹随净值键变化 ----------------
    def test_market_fingerprint_follows_nav_code(self):
        def fp(nav):
            with mock.patch.object(cfg_mod, "load_config", return_value=_cfg(PR, nav)), \
                 mock.patch.object(db, "get_conn", side_effect=lambda: sqlite3.connect(_PATH)):
                with db.conn_ctx() as conn:
                    return fe.market_fingerprint(conn, None)

        self.assertNotEqual(fp(TR), fp(PR),
                            "指纹没把净值腿算进去 ⇒ 换尺不作废缓存，旧面板会静默供应旧基准腿")
        self.assertEqual(fp(TR), fp(TR), "同一份配置指纹不稳定 ⇒ 缓存永远命中不了")

    # ---------------- ⑤ 只用价格指数的四个消费方 ----------------
    def test_price_index_only_consumers_never_read_the_nav_key(self):
        for rel in ("app/models/regime.py", "app/models/style.py",
                    "app/paper/engine.py", "app/notify.py"):
            txt = (BASE / rel).read_text(encoding="utf-8")
            self.assertIn("benchmark_code()", txt, f"{rel} 不再读 benchmark_code() —— 基准源漂了")
            self.assertNotIn("benchmark_nav_code", txt,
                             f"{rel} 改成读净值腿了 = 换策略输入/换模拟盘与邮件的尺（预注册 §5）")

    # ---------------- ⑥ 配错码 ⇒ 显式空腿，不崩也不静默换尺 ----------------
    def test_unknown_nav_code_gives_explicit_empty_leg(self):
        m = _panel(_PATH, PR, "ZZZ999.NOTHING")
        self.assertTrue(m["bench_tr"].isna().all(), "空腿没被识别 ⇒ 后面会拿 NaN 当价格算")
        self.assertTrue(m["bench"].notna().any(), "特征腿被连累了（只该净值腿受害）")


if __name__ == "__main__":
    unittest.main()
