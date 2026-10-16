"""看板基线的取行守卫（2026-09-20，三补二十七 §④ 的根治）

坏法（原来真在的）：`config.backtest_run` 配了具名 pin，但那一行不在库里时，
`/api/overview` 与 `/api/nav` 会 **`ORDER BY run_date DESC LIMIT 1`** 兜底 —— 而生产库里
躺着几十行实验臂（`20260919_b1tr_*` / `*_b1real_*` / `abuw_*`），按字典序排在**最新**位置。
⇒ 任何一次手滑改 config 或误删 pin 行，看板基线就换成一个从没判决过的臂，**无人报警**。
（14 号三补二十七 §④ 实测：改前 `backtest_summary` 共 170 行，排在 `20260912_limitquant`
 之后的有 8 行，全是实验臂。）

修法：配了 pin 就**只认那一行**；缺失 ⇒ 空块 + `pin_missed=True`（宁红不假绿）。
`pin=="latest"` 仍取最新一行（那是字面意思，生产 config 从不用它）。

⚠️ 本文件**自带一份临时库**，不读生产库：跑测试时 `tests/_isolation.py` 会把 `db.DB_PATH`
   换到隔离副本（07 #53），依赖真库内容会让用例在不同机器/不同日子结果不同 —— 那正是
   P2-10 那条"CI 上稳定跳过"的同族毛病。
"""
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from app import config as cfg_mod          # noqa: E402
from app.web import api                    # noqa: E402

PIN = "20260920_trbench"
GHOST_PIN = "NO_SUCH_RUN_TAG_zzz"
NEWER_EXPERIMENT_ROWS = ("20260919_b1tr_prod_b", "20260919_b1real_prod_c",
                        "20260921_abg_arm_B")     # 字典序都排在 PIN **之后**
_SCHEMA = """
CREATE TABLE regime (trade_date TEXT, state TEXT, score REAL, drawdown REAL);
CREATE TABLE plan (plan_date TEXT);
CREATE TABLE daily_score (trade_date TEXT, ts_code TEXT, score REAL);
CREATE TABLE model_score (trade_date TEXT, ts_code TEXT, score REAL);
CREATE TABLE daily (trade_date TEXT, ts_code TEXT);
CREATE TABLE backtest_summary (run_date TEXT PRIMARY KEY, metrics TEXT);
CREATE TABLE backtest_nav (run_date TEXT, trade_date TEXT, nav REAL, sys_nav REAL,
                          bench_nav REAL);
"""


def _mk_db() -> str:
    """两份 run_date：真 pin + 若干**更新的实验臂**。兜底一发生就会拿到实验臂 ⇒ 可判。"""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    path = tmp.name
    tmp.close()
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA)
    for rd in (PIN,) + NEWER_EXPERIMENT_ROWS:
        conn.execute("INSERT INTO backtest_summary VALUES (?,?)",
                     (rd, json.dumps({"run_date": rd, "excess_ann": 0.1660,
                                      "periods": 85})))
        conn.execute("INSERT INTO backtest_nav VALUES (?,?,?,?,?)", (rd, "20260831", 1.2, 1.1, 1.0))
    conn.execute("INSERT INTO regime VALUES ('20260918','attack',0.8,-0.05)")
    conn.commit()
    conn.close()
    return path


class TestPinFallback(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.path = _mk_db()

    @classmethod
    def tearDownClass(cls):
        Path(cls.path).unlink(missing_ok=True)

    def setUp(self):
        self.p = mock.patch.object(api.db, "get_conn",
                                  side_effect=lambda: sqlite3.connect(self.path))
        self.p.start()
        self.addCleanup(self.p.stop)

    def test_fixture_is_not_vacuous(self):
        """正检查：临时库里**确实**存在比 pin 更新的行 ⇒ 旧实现的 DESC 兜底会拿到实验臂。

        没有这一条，"缺失时不返回内容"的断言可能只是因为库里没别的东西而空转
        （照三补二十七 A4 的教训：能让结果看着对、构造却是错的实现，只有正检查抓得住）。
        """
        conn = sqlite3.connect(self.path)
        newer = [r[0] for r in conn.execute(
            "SELECT run_date FROM backtest_summary WHERE run_date>? ORDER BY run_date DESC",
            (PIN,))]
        conn.close()        # noqa: SIM115(显式关闭即可)
        self.assertTrue(newer, f"库里没有比 {PIN} 更新的 run ⇒ 本守卫测不到任何东西")
        self.assertNotIn(PIN, newer)

    def test_missing_pin_never_falls_back_to_newest_row(self):
        with mock.patch.object(cfg_mod, "load_config", return_value={"backtest_run": GHOST_PIN}):
            out = api.overview()
        self.assertTrue(out["backtest_pin_missed"], "pin 缺失必须带标记（前端要提示）")
        self.assertEqual(out["backtest"], {},
                         f"pin 缺失却返回了内容 —— 八成是回退到了最新行 {NEWER_EXPERIMENT_ROWS[-1]}")

    def test_real_pin_is_returned_verbatim(self):
        with mock.patch.object(cfg_mod, "load_config", return_value={"backtest_run": PIN}):
            out = api.overview()
        self.assertFalse(out["backtest_pin_missed"])
        self.assertEqual(out["backtest"].get("run_date"), PIN)

    def test_latest_mode_still_returns_a_row(self):
        with mock.patch.object(cfg_mod, "load_config", return_value={"backtest_run": "latest"}):
            out = api.overview()
        self.assertFalse(out["backtest_pin_missed"], "latest 是字面意思，不该报缺失")
        self.assertEqual(out["backtest"].get("run_date"), NEWER_EXPERIMENT_ROWS[-1],
                         "latest 模式应当拿字典序最新的一行")

    def test_nav_endpoint_uses_the_same_rule(self):
        with mock.patch.object(cfg_mod, "load_config", return_value={"backtest_run": GHOST_PIN}):
            out = api.nav()
        self.assertTrue(out["pin_missed"])
        self.assertIsNone(out["run_date"], "nav 也不得回退到最新 run（那会把实验臂画成基线曲线）")
        self.assertEqual(out["rows"], [], "缺失 pin 时不该返回任何净值行")
        with mock.patch.object(cfg_mod, "load_config", return_value={"backtest_run": PIN}):
            ok = api.nav()
        self.assertEqual(ok["run_date"], PIN)
        self.assertTrue(ok["rows"], "命中 pin 时 nav 必须有曲线")


if __name__ == "__main__":
    unittest.main()
