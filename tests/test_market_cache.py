"""行情面板缓存守卫（2026-09-13）

为什么加缓存：`load_market` 实测 **322~465 秒**（F50/F52/F55 各付一次），研究迭代与 19:30
打分保鲜档都被它卡住。缓存键必须是**内容指纹**（各行情表 MAX(trade_date)），不能是文件 mtime
——模拟盘/账本写的是同一个库，mtime 一变就作废等于没缓存。

守五条：
  1. 指纹：行情表最后交易日变化 ⇒ 指纹必须变（否则会读到过期价格面板）；
  2. 指纹：任一行情表缺失/查询失败 ⇒ 空串（**禁用缓存**，宁可慢也不读残留）；
  3. 往返：面板存了再读必须逐帧等价（两条基准腿 `bench`/`bench_tr` 都仍是 Series 且各是各的值）；
  4. 损坏：缺 `_meta.json` / 缺任一腿文件 / 形状不符 ⇒ 返回 None（走重建路径，绝不返回半截数据）；
  5. 清理：只保留最近 2 份缓存（不会无限长）。

运行: python -m unittest tests.test_market_cache -v
"""
import json
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

from app.factors import factor_engine as fe      # noqa: E402


def _parquet_engine_works() -> tuple:
    """跑一次**真往返**判断缓存能不能落盘，返回 (可否, 原因)。

    为什么不只 `import pyarrow`：2026-09-14 实测 —— venv 里 pyarrow 装上了（25.0.1），
    但 **Windows Smart App Control** 把 `.venv\\...\\pyarrow\\arrow.dll` 判成"未达签名要求"
    （CodeIntegrity 事件 3077/3033，同一份文件在 `D:\\python\\Lib\\site-packages` 下放行 ——
    逐路径声誉）。此时 `import pyarrow` 报 `ImportError: DLL load failed ... 应用程序控制策略
    已阻止此文件`，而 pandas 的报错信息退化成"pyarrow 未安装"，看起来像依赖没装。
    ⇒ 探测必须走**真实写入**，且把原始异常文本留下来（供跳过理由引用）。
    """
    tmp = Path(tempfile.mkdtemp(prefix="parquet_probe_"))
    try:
        pd.DataFrame({"a": [1.0]}).to_parquet(tmp / "p.parquet")
        return True, ""
    except Exception as e:                                   # 含 ImportError/OSError
        return False, f"{type(e).__name__}: {e}".splitlines()[0][:160]
    finally:
        try:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)
        except Exception:
            pass


_PARQUET_OK, _PARQUET_WHY = _parquet_engine_works()

#: 跳过理由：**不是**"没装依赖"这么简单。写明机器/策略/被拦路径，接手人才知道本机跑这个
#: 模块会少验几条、以及为什么。判据是"真写不进去"而非"import 不了"（见上面 docstring）。
_PARQUET_SKIP = ("parquet 引擎在本机当前解释器下不可用 ⇒ 缓存落盘路径无法验证"
                 f"（实测: {_PARQUET_WHY}）。本机为 Windows Smart App Control；"
                 "CI（ubuntu/windows runner 无此策略）与 D:\\python 侧正常执行本模块。")

_SCHEMA = """
CREATE TABLE daily (ts_code TEXT, trade_date TEXT, close REAL, PRIMARY KEY (ts_code, trade_date)) WITHOUT ROWID;
CREATE TABLE daily_basic (ts_code TEXT, trade_date TEXT, pe_ttm REAL, PRIMARY KEY (ts_code, trade_date)) WITHOUT ROWID;
CREATE TABLE adj_factor (ts_code TEXT, trade_date TEXT, adj_factor REAL, PRIMARY KEY (ts_code, trade_date)) WITHOUT ROWID;
CREATE TABLE index_daily (ts_code TEXT, trade_date TEXT, close REAL, PRIMARY KEY (ts_code, trade_date)) WITHOUT ROWID;
"""


def _panel(n_rows=6, n_cols=4) -> dict:
    # 用 list 构造（**不带 freq**）—— 与生产一致：`load_market` 的索引来自 SQL 透视后的
    # `pd.to_datetime(...)`，本身没有 freq；带 freq 的夹具会让往返比较出现假差异。
    idx = pd.DatetimeIndex(list(pd.date_range("2026-01-05", periods=n_rows, freq="W-FRI")))
    cols = [f"00000{i}.SZ" for i in range(n_cols)]
    out = {}
    for name in fe._CACHE_FRAMES:
        out[name] = pd.DataFrame(np.arange(n_rows * n_cols, dtype=np.float32).reshape(n_rows, n_cols),
                                 index=idx, columns=cols) + 1.0
    out["bench"] = pd.Series(np.linspace(1000, 1010, n_rows, dtype=np.float32), index=idx, name="bench")
    # 两条基准腿(2026-09-20 B1)：数值必须与 bench **不同** —— 相同的话往返会"互相顶包"
    # (bench_tr.parquet 没写、读时读到 bench 也能过)
    out["bench_tr"] = pd.Series(np.linspace(2000, 2010, n_rows, dtype=np.float32),
                                index=idx, name="bench_tr")
    return out


class TestFingerprint(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.path = tmp.name
        tmp.close()
        conn = sqlite3.connect(self.path)
        conn.executescript(_SCHEMA)
        conn.executemany("INSERT INTO daily VALUES ('600000.SH',?,10.0)",
                         [("20260910",), ("20260911",)])
        conn.execute("INSERT INTO daily_basic VALUES ('600000.SH','20260911',15.0)")
        conn.execute("INSERT INTO adj_factor VALUES ('600000.SH','20260911',1.0)")
        conn.execute("INSERT INTO index_daily VALUES ('000906.SH','20260911',5000.0)")
        conn.commit()
        conn.close()
        self.conn = sqlite3.connect(self.path)
        self.addCleanup(lambda: Path(self.path).unlink(missing_ok=True))
        self.addCleanup(self.conn.close)

    def test_fingerprint_is_stable_for_same_data(self):
        self.assertEqual(fe.market_fingerprint(self.conn, "20260901"),
                         fe.market_fingerprint(self.conn, "20260901"))

    def test_fingerprint_changes_when_new_trading_day_arrives(self):
        before = fe.market_fingerprint(self.conn, None)
        self.conn.execute("INSERT INTO daily VALUES ('600000.SH','20260914',10.5)")
        self.conn.commit()
        after = fe.market_fingerprint(self.conn, None)
        self.assertNotEqual(before, after, "日链写入新交易日后指纹必须变，否则会读到过期面板")

    def test_fingerprint_changes_with_since(self):
        self.assertNotEqual(fe.market_fingerprint(self.conn, None),
                            fe.market_fingerprint(self.conn, "20260901"))

    def test_fingerprint_changes_when_an_existing_day_is_corrected(self):
        """**原地重写已有日期**（残缺批次次日重拉）也要让缓存失效 —— 只看 MAX(date) 会漏掉它。

        2026-09-13 自查评审发现：日链对疑似残缺批会重拉并覆盖同一天行情，此时最大日期不变、
        数据已变。指纹把 `data_status.updated_at` 纳入后就抓得住这种修正。
        """
        self.conn.execute("CREATE TABLE data_status (name TEXT PRIMARY KEY, last_date TEXT, "
                          "updated_at TEXT, rows INTEGER)")
        self.conn.execute("INSERT INTO data_status VALUES ('daily','20260911','2026-09-11 19:36:31',2)")
        self.conn.commit()
        before = fe.market_fingerprint(self.conn, None)
        # 同一天被重拉覆盖：MAX(trade_date) 不变，只有 updated_at 变
        self.conn.execute("UPDATE data_status SET updated_at='2026-09-12 19:36:31' WHERE name='daily'")
        self.conn.commit()
        after = fe.market_fingerprint(self.conn, None)
        self.assertNotEqual(before, after,
                            "原地重写没有换 MAX(date) ⇒ 指纹必须靠 updated_at 才抓得住")

    def test_fingerprint_ignores_missing_data_status_table(self):
        """临时库没有 data_status 时不该禁用缓存（它是可选信号，四个价格表才是硬条件）。"""
        self.assertNotEqual(fe.market_fingerprint(self.conn, None), "")

    def test_missing_table_disables_cache(self):
        self.conn.execute("DROP TABLE adj_factor")
        self.conn.commit()
        self.assertEqual(fe.market_fingerprint(self.conn, None), "",
                         "表缺失时必须禁用缓存（宁可慢，不读残留）")


@unittest.skipUnless(_PARQUET_OK, _PARQUET_SKIP)
class TestCacheRoundtrip(unittest.TestCase):
    """往返/损坏/裁剪五条 —— **共同前提是面板能真的落盘**。

    ⚠️ 2026-09-14：这四条过去在"引擎不可用"时是**假绿** —— `_cache_save` 内部吞异常返回，
    `_cache_load` 于是返回 None；`test_missing_meta_returns_none` / `test_shape_mismatch`
    断言 `assertIsNone` 恰好"通过"，`test_only_two_entries_kept` 也因没有目录而 vacuously
    通过。现在显式跳过：**跳过数会进 `ci_run_tests.py` 的额度计数**，不会再冒充验证。
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="mkt_cache_"))
        self._p = mock.patch.object(fe, "MARKET_CACHE_DIR", self.tmp)
        self._p.start()
        self.addCleanup(self._p.stop)

    def tearDown(self):
        # 2026-09-24 补：原来只 `addCleanup(self._p.stop)` —— **临时目录从不删除**。
        # 实测代价：本机 `%TEMP%` 下累积了 **1431 个** `mkt_cache_*` 目录（每次跑全量 +4）。
        # 这是 `TestBackupFreshness` 的 docstring 早在 2026-09-19 就点名过的那一类缺陷
        # （"跑测试会把仓库/环境变脏"），只是当时只修了它自己那处。
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_save_then_load_is_identical(self):
        panel = _panel()
        fe._cache_save("abc123", panel)
        got = fe._cache_load("abc123", 0.0)
        self.assertIsNotNone(got)
        for name in fe._CACHE_FRAMES:
            pd.testing.assert_frame_equal(got[name], panel[name])
        self.assertIsInstance(got["bench"], pd.Series)
        pd.testing.assert_series_equal(got["bench"], panel["bench"])
        self.assertIsInstance(got["bench_tr"], pd.Series)
        pd.testing.assert_series_equal(got["bench_tr"], panel["bench_tr"])

    def test_missing_nav_leg_file_returns_none(self):
        """缺 `bench_tr.parquet` 必须**重建**，不能只把缺的那条腿补成 NaN。

        2026-09-20 B1：基准净值腿一旦缺失，回测的超额会静默变 NaN（或直接读到旧尺面板），
        两种都比"多花 5 分钟重建"坏。
        """
        fe._cache_save("abc123", _panel())
        (self.tmp / "abc123" / "bench_tr.parquet").unlink()
        self.assertIsNone(fe._cache_load("abc123", 0.0))

    def test_missing_meta_returns_none(self):
        panel = _panel()
        fe._cache_save("abc123", panel)
        (self.tmp / "abc123" / "_meta.json").unlink()
        self.assertIsNone(fe._cache_load("abc123", 0.0))

    def test_shape_mismatch_returns_none(self):
        panel = _panel()
        fe._cache_save("abc123", panel)
        meta = json.loads((self.tmp / "abc123" / "_meta.json").read_text(encoding="utf-8"))
        meta["shapes"]["close"] = [1, 1]                    # 伪造"形状不符"
        (self.tmp / "abc123" / "_meta.json").write_text(json.dumps(meta), encoding="utf-8")
        self.assertIsNone(fe._cache_load("abc123", 0.0),
                          "形状不符必须重建（半截文件比慢更危险）")

    def test_only_two_entries_kept(self):
        for i, key in enumerate(("k1", "k2", "k3")):
            fe._cache_save(key, _panel())
        # 用 mtime 排序，测试里连续写入可能同秒 ⇒ 至少不能超过 2 份
        dirs = [p for p in self.tmp.glob("*") if p.is_dir() and not p.name.startswith(".")]
        self.assertLessEqual(len(dirs), 2, f"缓存没有裁剪: {[p.name for p in dirs]}")


class TestCachePolicy(unittest.TestCase):
    """两条**与落盘无关**的策略断言 —— 不依赖 parquet 引擎，任何机器都该真跑。

    2026-09-14 从 `TestCacheRoundtrip` 里拆出来：它们原来会被"引擎不可用"的类级跳过一起
    带走，而它们验的是"什么窗口值得缓存 / 谁能写缓存"这两条**纯逻辑**，没必要搭环境顺风车。
    """

    def test_small_windows_are_not_cached(self):
        """窗口小的查询（如 plan 的 250 天窗口）不进缓存 —— 否则会挤掉 326 秒才建好的大缓存。"""
        self.assertTrue(fe._cache_worth(None), "全历史必须缓存")
        self.assertTrue(fe._cache_worth("20140101"), "周频口径（20140101 起）必须缓存")
        self.assertFalse(fe._cache_worth("20251201"), "250 天窗口不该进缓存")

    def test_only_prod_db_may_write_cache(self):
        """临时库/测试库**不许写**缓存（否则假面板会挤掉真缓存）。"""
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        self.assertFalse(fe._is_prod_db(conn), "内存库不该被判成生产库")
        real = sqlite3.connect(str(fe.db.DB_PATH))
        self.addCleanup(real.close)
        self.assertTrue(fe._is_prod_db(real), "生产库必须被认出来（否则缓存永远写不进去）")


class TestCacheWriteVisibility(unittest.TestCase):
    """写盘失败的**可观测性**（2026-09-24）。

    原来失败只有一行 `log.warning` ⇒ 2026-09-24 19:34 的
    `No type extension with name arrow.py_extension_type found` 沉睡了 2 小时。
    现在失败必须同时：① 落一份状态证据（供 `doctor` 体检项读）；② 发一封同日去重的运维告警。

    ⚠️ 失败用例**不需要 parquet 引擎**：`to_parquet` 被 patch 成抛异常，
    这正是"引擎/进程状态坏掉"的等价场景 —— 所以它们在任何机器上都真跑，不会被类级跳过带走。
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="mkt_cache_vis_"))
        self._p = mock.patch.object(fe, "MARKET_CACHE_DIR", self.tmp)
        self._p.start()
        self.addCleanup(self._p.stop)

    def tearDown(self):
        # 同上（2026-09-24）：本类初版也漏了清理 ⇒ 实测在 `%TEMP%` 留下 72 个
        # `mkt_cache_vis_*`。**新写的测试类必须自带 tearDown** —— 不能靠"反正别人也没删"。
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _save_failing(self, key="viskey0001"):
        """让 `_cache_save` 在第一次 `to_parquet` 就抛（等价于引擎/进程状态坏掉）。"""
        with mock.patch.object(pd.DataFrame, "to_parquet",
                               side_effect=RuntimeError(
                                   "No type extension with name arrow.py_extension_type found")):
            fe._cache_save(key, _panel())

    def test_failed_write_records_status(self):
        self._save_failing()
        st = fe.read_cache_status()
        self.assertFalse(st.get("ok"), "写入失败必须落 ok=False 的证据")
        self.assertIn("arrow.py_extension_type", st.get("error", ""))
        self.assertEqual(st.get("key"), "viskey0001")

    def test_failed_write_alerts_ops(self):
        with mock.patch("app.notify.send_ops_alert", return_value=True) as m:
            self._save_failing()
        self.assertEqual(m.call_count, 1, "写盘失败必须发一封运维告警（原来没人看得见）")
        self.assertIn("行情缓存写入失败", m.call_args[0][0])

    def test_same_day_failure_alerts_only_once(self):
        """同一天多次失败只发一封 —— 否则 20:30/21:30 的重试档会把告警变成噪声。"""
        with mock.patch("app.notify.send_ops_alert", return_value=True) as m:
            self._save_failing("viskey0001")
            self._save_failing("viskey0001")
            self._save_failing("viskey0002")
        self.assertEqual(m.call_count, 1, "同日第二次失败不该再发信")

    def test_next_day_failure_alerts_again(self):
        """跨日必须重发 —— 持续事故不能被"昨天发过"永久压住。"""
        with mock.patch("app.notify.send_ops_alert", return_value=True) as m:
            self._save_failing("viskey0001")
            fe._write_cache_status(alerted_day="19990101")     # 假装上次告警是很久以前
            self._save_failing("viskey0001")
        self.assertEqual(m.call_count, 2, "跨日失败必须重发")

    def test_alert_failure_does_not_break_the_run(self):
        """告警发不出去**绝不能**把跑数带崩（发信失败就不记 alerted_day ⇒ 下轮再试）。"""
        with mock.patch("app.notify.send_ops_alert", side_effect=OSError("smtp down")):
            self._save_failing()                                # 不许抛
        st = fe.read_cache_status()
        self.assertFalse(st.get("ok"))
        self.assertNotIn("alerted_day", st, "没发出去就不许记 alerted_day（否则永远不会再试）")

    @unittest.skipUnless(_PARQUET_OK, _PARQUET_SKIP)
    def test_successful_write_records_ok(self):
        fe._cache_save("visok0001", _panel())
        st = fe.read_cache_status()
        self.assertTrue(st.get("ok"), "成功也要落证据（否则 doctor 无法区分「没跑过」与「跑成了」）")
        self.assertIsNone(st.get("error"))


if __name__ == "__main__":
    unittest.main()
