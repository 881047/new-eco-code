"""周频两个档（09:32 开盘档 / 18:05 晚间档）测试的公共夹具（2026-09-13）

两档的测试需求完全一样：**临时库 + 临时方案目录 + 临时账本 + 注入成交价/行情 + 不发邮件**。
抽在这里，避免两份复制粘贴的 fixture 各自漂移（复制粘贴的夹具迟早只改一份）。

约定：
- 生产库/生产账本/生产方案目录一律**不碰**（本机 11GB 库、data/weekly 下的真方案都不读）；
- 需要"真实成交价函数"的用例自己 patch `ledger._exec_price` / `quote_client.get_quotes`；
- 子类只需实现自己的断言，不要重写 setUp。
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

from app.weekly import plan as wk_plan            # noqa: E402
from app.weekly import ledger as wk_ledger        # noqa: E402
from app.weekly import pipeline as wk_pipeline    # noqa: E402

SCHEMA = """
CREATE TABLE stock_basic (ts_code TEXT PRIMARY KEY, symbol TEXT, name TEXT, area TEXT,
    industry TEXT, market TEXT, list_date TEXT, delist_date TEXT);
CREATE TABLE namechange (ts_code TEXT, name TEXT, start_date TEXT, end_date TEXT,
    ann_date TEXT, change_reason TEXT, PRIMARY KEY (ts_code, start_date)) WITHOUT ROWID;
CREATE TABLE daily (ts_code TEXT, trade_date TEXT, open REAL, high REAL, low REAL,
    close REAL, pre_close REAL, change REAL, pct_chg REAL, vol REAL, amount REAL,
    PRIMARY KEY (ts_code, trade_date)) WITHOUT ROWID;
CREATE TABLE adj_factor (ts_code TEXT, trade_date TEXT, adj_factor REAL,
    PRIMARY KEY (ts_code, trade_date)) WITHOUT ROWID;
CREATE TABLE repo_rate (trade_date TEXT PRIMARY KEY, overnight REAL);
"""
TODAY = "20260914"          # 周一
ANCHOR = "20260911"         # 上周五


class WeeklySlotTestCase(unittest.TestCase):
    """两个档共用的夹具；子类直接用 self.plan_dir / self.ledger / self.cfg。"""

    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = tmp.name
        tmp.close()
        conn = sqlite3.connect(self.db_path)
        conn.executescript(SCHEMA)
        conn.execute("INSERT INTO stock_basic (ts_code, name) VALUES ('600000.SH','浦发银行')")
        conn.commit()
        conn.close()
        # ledger.execute_plan 自己取 db.get_conn() ⇒ 指到临时库，别碰生产 11GB 库
        self._pdb = mock.patch.object(wk_ledger.db, "get_conn",
                                      side_effect=lambda: sqlite3.connect(self.db_path))
        self._pdb.start()
        self.addCleanup(self._pdb.stop)
        self.addCleanup(lambda: Path(self.db_path).unlink(missing_ok=True))

        self.plan_dir = Path(tempfile.mkdtemp(prefix="wk_plan_"))
        self._pdir = mock.patch.object(wk_plan, "PLAN_DIR", self.plan_dir)
        self._pdir.start()
        self.addCleanup(self._pdir.stop)
        # 2026-09-24 补：原来只有上面那行 `addCleanup(self._pdir.stop)` —— **只还原 mock、
        # 不删目录** ⇒ 本机 `%TEMP%` 累积了 **6186 个** `wk_plan_*`（约 11 MB）。
        # 这是本仓"跑测试把环境搞脏"缺陷族的**最大一处**（07 号 #64）。
        # 正确写法在 `test_weekly_pipeline.py` 里早就有（`wk_ensure_plan_` / `wk_typed_`），
        # 本文件是抄漏。纪律：**setUp 里 mkdtemp 了几个，tearDown 就必须 rmtree 几个。**
        import shutil
        self.addCleanup(lambda: shutil.rmtree(self.plan_dir, ignore_errors=True))

        self.ledger = self.plan_dir / "ledger_test.json"
        # 2026-09-14 外部复核 D1：**必须**把 `ensure_plan` 的补训路径打桩。
        # 三条既有用例注入的建造失败消息里含"存档"⇒ 会走 `run_score_slot(..., train_fn=None)`
        # ⇒ 用**生产** DB/存档、且在"存档落后于锚点"时真的跑 6~8 分钟训练并写生产 npz
        # （今天恰好因为 `scores_20260911.npz` 已覆盖锚点而秒退）。夹具统一打桩 ⇒ 从此不可能。
        self.train_calls = []
        self._ptrain = mock.patch.object(
            wk_pipeline, "run_score_slot",
            side_effect=lambda **kw: self.train_calls.append(kw) or {"skipped": "stub"})
        self._ptrain.start()
        self.addCleanup(self._ptrain.stop)
        # 2026-09-18 邮件优化: 袖套 KPI 邮件出口已删（读数并入 18:30 日终总结的袖套小节），
        # 执行档不再有任何发信 ⇒ 原来这里对 `report.send_kpi_email` 的打桩整段作废。

        self._pprice = mock.patch("app.weekly.ledger._exec_price", return_value=10.0)
        self._pprice.start()
        self.addCleanup(self._pprice.stop)
        self.cfg = {"weekly": {"enabled": True}}

    # ---------------------------------------------------------------- 工具
    def write_plan(self, rows, capital=100_000.0, anchor=ANCHOR):
        (self.plan_dir / f"plan_{anchor}.json").write_text(
            json.dumps({"anchor": anchor, "capital": capital, "rows": rows}),
            encoding="utf-8")

    def halt_ledger(self, capital=100_000.0, reason="健康度 dead"):
        self.ledger.write_text(json.dumps({
            "capital": capital, "cash": capital, "positions": {}, "nav_history": [],
            "trades": [], "inception": ANCHOR, "last_mark": None,
            "halt": {"active": True, "since": "20260910", "reason": reason}}),
            encoding="utf-8")

    def ledger_state(self):
        return json.loads(self.ledger.read_text(encoding="utf-8"))
