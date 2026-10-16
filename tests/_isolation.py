"""测试隔离的**唯一实现**：日志改道 + 邮件出口拦截（2026-09-14 夜，外部审查 P0-1 收口）。

为什么要把这段从 `tests/conftest.py` 搬出来：`conftest.py` 是 **pytest 专属**机制，
而 `scripts/ci_run_tests.py` 走的是 `unittest discover` —— 于是"按文档跑 CI 入口"时
隔离**根本没有建立**，测试异常直接写进生产 `logs/app.log`（2026-09-14 22:26 实测：
1216 用例跑完，app.log 增大 76KB、探针字符串落盘 1 次）。这是 07 号台账"测试副作用"
主题的**第 5 次形态复发**，根因不是谁忘了写隔离，而是**隔离只有一条 pytest 路径**。

⇒ 现在两个入口都 import 本模块，语义只有一份：任何"跑测试"的进程，先 `setup()`。

契约（改动前先读）：
- `setup()` 只做**进程内**改道 + 环境变量；不依赖 pytest 是否在跑；
- 子进程（`tests/test_logger_utf8.py` 那类）靠继承环境变量 `NEWECO_LOG_DIR` 生效；
- `setup()` 在 LOG_DIR 仍指向生产 `logs/` 时**直接抛异常**（fail-closed）——
  宁可当场红，也不要静默把测试异常混进事故取证台账。
"""
import logging
import os
import shutil
import sqlite3
import tempfile
import uuid
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
PROD_LOG_DIR = BASE / "logs"
WEEKLY_DIR = BASE / "data" / "weekly"


def _make_temp_log_dir() -> Path:
    """临时日志目录。

    pytest 下加**随机后缀**：`tmpdir` 基名固定（`neweco_test_logs_<user>`），同机并发跑
    两个测试会话（或 CI 与本地同时跑）会共用目录，`sessionfinish` 里一个会话把另一个
    会话的日志删掉。unittest 下由本模块负责清理，同样用独立目录。
    """
    return Path(tempfile.mkdtemp(prefix=f"neweco_test_logs_{uuid.uuid4().hex[:8]}_"))


#: 参照表：**只读内容**（日历、状态字段、股票列表），测试需要它们才跑得动，
#: 但任何测试都不该改它们。用 READ ONLY attach 从生产库复制，一键进临时库。
#: 生产库或表不存在时跳过（CI 上没有生产库，这些测试本来就该自己搭夹具）。
_REFERENCE_TABLES = ("trade_cal", "data_status", "stock_basic", "index_daily", "namechange")


def _seed_reference_data(db_file: Path) -> None:
    """把参照表内容复制进测试用临时库（**只读**打开源库，绝不写生产）。"""
    src = BASE / "data" / "neweco.db"
    if not src.exists():
        return
    try:
        # 2026-09-18 审查 Q-09：必须开 uri=True，否则下面 `ATTACH 'file:…?mode=ro'`
        # **一定**抛 OperationalError（非 URI 主连接不解析 URI 文件名，Windows 还会把
        # 冒号/反斜杠当路径）⇒ 必然落到 except 那条 fallback，用**读写**模式挂上 11G
        # 生产库。于是"prod_ro 只读"这个保护从来没生效过：误写 prod_ro 挡不住，
        # 而且测试会话会与在跑的服务共享生产库的锁面 / -wal / -shm。
        # 实测过：加 uri=True 之后写 prod_ro 会得到 `attempt to write a readonly database`。
        conn = sqlite3.connect(f"file:{db_file}", uri=True)
    except Exception:
        return
    try:
        try:
            conn.execute("ATTACH DATABASE ? AS prod_ro", (f"file:{src}?mode=ro",))
        except Exception:
            conn.execute("ATTACH DATABASE ? AS prod_ro", (str(src),))
        for t in _REFERENCE_TABLES:
            try:
                have = conn.execute(
                    "SELECT 1 FROM prod_ro.sqlite_master WHERE type='table' AND name=?",
                    (t,)).fetchone()
                if not have:
                    continue
                conn.execute(f"INSERT OR REPLACE INTO main.{t} SELECT * FROM prod_ro.{t}")
            except Exception:
                continue                                  # 单表失败不影响其它表
        conn.commit()
        try:
            conn.execute("DETACH DATABASE prod_ro")
        except Exception:
            pass
    finally:
        conn.close()


_TMP_LOGS: Path = _make_temp_log_dir()
os.environ["NEWECO_LOG_DIR"] = str(_TMP_LOGS)

#: 测试期间被拦下的"发送"记录 `(tag|None, subject|None)`；**不发信**
SENT_IN_TESTS: list = []


class _BlockedSMTP:
    """**唯一的网络出口哨兵**：顶替 `smtplib.SMTP_SSL`。

    为什么设在这里（2026-09-14 定稿）：全项目发信只剩一个出口 —— `app/notify.py:38` 的
    `with smtplib.SMTP_SSL(...)`。把哨兵放在**网络边界**，比替换 `_send_with_retry` /
    `send_ops_alert` 这类**内部函数**更稳，因为内部函数的替身会破坏"接管逻辑仍然真实"：
      ① `tests/test_notify_ops_alert.py` 要断言 `send_ops_alert` **真的**用了
         `_send_with_retry`（`test_enabled_uses_production_sender`）—— 替掉它这条就假红；
      ② 调度器的告警重发分支直连 `notify.send_ops_alert`，替掉 `_send_with_retry` 堵不住它
         （2026-09-14 22:26:54 真的漏出去一封「任务失败: 测试」，`notify_log` id=18 有据）。

    放在网络边界后：**发送一定失败**（返回 False、不真发信），但"谁调了谁、参数是什么"依旧
    是真的，测试可以照常断言接管逻辑。
    """

    def __init__(self, *a, **kw):
        SENT_IN_TESTS.append(("smtp", a[0] if a else None))
        raise OSError("测试隔离：SMTP 出口被 _isolation._BlockedSMTP 拦截（测试不许真发信）")


def setup(temp_log_dir: Path = None) -> Path:
    """建立隔离并**自证生效**：返回测试用的临时日志目录。

    幂等：重复调用只做一次改道（重复改道会把已写的日志丢在旧目录里，反而更难查）。
    """
    global _TMP_LOGS
    if temp_log_dir is not None:
        _TMP_LOGS = Path(temp_log_dir)
        os.environ["NEWECO_LOG_DIR"] = str(_TMP_LOGS)
    _TMP_LOGS.mkdir(parents=True, exist_ok=True)

    try:
        import app.config as cfg
        import app.logger as lg
    except Exception:                                    # 环境缺依赖时不影响收集
        return _TMP_LOGS
    cfg.LOG_DIR = _TMP_LOGS
    lg.LOG_DIR = _TMP_LOGS
    lg._initialized = False
    lg._handler = None
    if Path(cfg.LOG_DIR).resolve() == PROD_LOG_DIR.resolve():
        raise RuntimeError("测试日志改道失败：LOG_DIR 仍指向生产 logs/ —— 拒绝继续跑"
                           "（否则会把测试异常混进证据台账，见 07 号 #32）")

    # 已经挂上的文件 handler（可能指向生产 app.log）要摘掉
    root = logging.getLogger()
    for h in list(root.handlers):
        if isinstance(h, logging.FileHandler):
            root.removeHandler(h)
            h.close()

    # 邮件出口隔离：只堵**网络边界**（`smtplib.SMTP_SSL`），不替内部函数。详见 _BlockedSMTP。
    # 历史教训（07 号 #1/#2 + 2026-09-14 实测）：堵 `_send_with_retry` 会漏掉
    # ① 调度器在首选发送器抛错后的重发分支（直连 `notify.send_ops_alert` ⇒ notify_log id=18）、
    # ② `app/weekly/report.py` 那种"按名字绑定过"的模块级引用。
    try:
        import smtplib
        smtplib.SMTP_SSL = _BlockedSMTP
    except Exception:                                    # 环境缺依赖时不影响收集
        pass

    # 数据库隔离（2026-09-14 实测补的第二层）：**测试的写不许落到生产库**。
    #
    # 为什么"日志改了道、邮件也拦了"还不够：`scheduler._guarded` 在任务抛异常时会走
    # 真实告警路径 → `notify._send_with_retry` → `_log_send` → **写生产 `notify_log`**
    # （2026-09-14 实测：`tests/test_scheduler_lock.py` 与 test_scheduler_ops_alert 一起跑
    #  就增 1 行）。而 `notify_log` 是"什么时候发过什么"的证据台账 —— 和 app.log 一样，
    # 混进测试噪声等于抬高未来取证的代价（07 号 #1/#32 同一主题的第三层）。
    #
    # 做法：同目录建一个**空的临时库**（`init_db` 建出 SCHEMA 全部表），把 `db.DB_PATH`
    # 指过去。于是"有没有写生产库"由**路径**保证，不依赖谁记得 patch。
    # 只读生产库的测试（如 test_market_cache 的 `fe._is_prod_db`）在自己用例里 patch，
    # 不受影响；需要真实数据的测试必须自己搭夹具 —— 那本来也是更稳的写法。
    try:
        from app import db as db_mod
        db_file = _TMP_LOGS / "test.db"
        db_mod.DATA_DIR = _TMP_LOGS
        db_mod.DB_PATH = db_file
        db_mod.init_db()
        _seed_reference_data(db_file)
    except Exception:                                    # 环境缺依赖时不影响收集
        pass

    # 调度器锁隔离（2026-09-18 审查修）：`scheduler._LOCK_FILE = DATA_DIR/".task.lock"`
    # 指的是**生产** data/ 目录，而 `tests/test_scheduler_ops_alert.py` 端到端跑
    # `_retryable` 的正常路径 ⇒ 它去抢**真锁**。服务只要正处在定时档（18:00/18:05/
    # 19:30/19:45 + misfire 追补），锁被服务进程持有 ⇒ 那 5 条用例**必红**，
    # 报的却是"正常路径必须返回 True"——看起来像代码坏了，其实是撞上了在跑的活。
    # `test_scheduler_lock.py` 早就自己 patch 了这条；漏的是没 patch 的那些。
    # 判据由**路径**保证，不依赖谁记得写 patch（与上面 DB_PATH 同一手法）。
    try:
        from app import scheduler as sched_mod
        sched_mod._LOCK_FILE = _TMP_LOGS / ".task.lock"
    except Exception:
        pass
    return _TMP_LOGS


def seed_synthetic_market(db_file: Path = None, n_days: int = 400, n_stocks: int = 60) -> int:
    """给**测试库**灌一份最小的合成行情面板（日线/复权/基础指标/基准），返回交易日数。

    为什么需要它（2026-09-14 加）：数据库隔离之后测试库是**空的**，于是原本靠"生产缓存里
    恰好有面板"才通过的那类测试会掉进 `PITEngine` 的空索引（`IndexError: index 0 is out of
    bounds for axis 0 with size 0`）。给它们一份**确定性合成面板**，测试就同时满足两件事：
    ① 不依赖工作目录里有什么（今天的数据、昨天的缓存都不影响结论）；
    ② 走的是**真实**的 `load_market` → `PITEngine` 路径，不是把被测逻辑打桩掉。

    数据长什么样（够用即止，不追求仿真）：400 个交易日 × 60 只（含 600000.SH，另加几只
    688/300 让板块分支有覆盖），价格恒定、`vol>0`、`total_mv` 均匀 ⇒ 可交易域判定只受
    "上市满 60 日/停牌/ST"这些**被测规则**影响，不引入市值分位噪声。
    """
    db_file = Path(db_file or _TMP_LOGS / "test.db")
    conn = sqlite3.connect(str(db_file))
    try:
        days = [r[0] for r in conn.execute(
            "SELECT cal_date FROM trade_cal WHERE is_open=1 AND cal_date<='20260914' "
            "ORDER BY cal_date DESC LIMIT ?", (n_days,))]
        if len(days) < 70:                                   # 没有日历就没有参照，交给调用方
            return 0
        days = sorted(days)
        codes = ["600000.SH", "600519.SH", "000001.SZ", "300750.SZ", "688111.SH"]
        codes += [f"6000{i:02d}.SH" for i in range(5, 5 + max(0, n_stocks - len(codes)))]
        # 列序照 PRAGMA table_info(daily)：ts_code, trade_date, open, high, low, close,
        # pre_close, change, pct_chg, vol, amount
        rows_daily = [(c, d, 10.0, 10.0, 10.0, 10.0, 10.0, 0.0, 0.0, 1e6, 1e7)
                      for d in days for c in codes]
        conn.executemany(
            "INSERT OR REPLACE INTO daily (ts_code, trade_date, open, high, low, close, "
            "pre_close, change, pct_chg, vol, amount) VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows_daily)
        conn.executemany("INSERT OR REPLACE INTO adj_factor (ts_code, trade_date, adj_factor) "
                         "VALUES (?,?,1.0)", [(c, d) for d in days for c in codes])
        conn.executemany(
            "INSERT OR REPLACE INTO daily_basic (ts_code, trade_date, turnover_rate, pe_ttm, "
            "pb, dv_ttm, total_mv, circ_mv) VALUES (?,?,?,?,?,?,?,?)",
            [(c, d, 1.0, 15.0, 1.5, 2.0, 5e9, 4e9) for d in days for c in codes])
        conn.executemany("INSERT OR REPLACE INTO index_daily (ts_code, trade_date, close) "
                         "VALUES (?,?,4000.0)",
                         [("000906.SH", d) for d in days] + [("000300.SH", d) for d in days])
        conn.commit()
        return len(days)
    finally:
        conn.close()


def assert_active() -> None:
    """自证隔离已生效（CI 入口在跑测试前调用；不成立就当场红，不带病往下跑）。"""
    import app.config as cfg
    if Path(cfg.LOG_DIR).resolve() == PROD_LOG_DIR.resolve():
        raise RuntimeError(f"测试隔离未生效：app.config.LOG_DIR = {cfg.LOG_DIR}"
                           "（等于生产 logs/，跑测试会污染 app.log）")
    if os.environ.get("NEWECO_LOG_DIR") in (None, ""):
        raise RuntimeError("测试隔离未生效：环境变量 NEWECO_LOG_DIR 没设（子进程会写生产日志）")
    try:
        import smtplib
        if smtplib.SMTP_SSL is not _BlockedSMTP:
            raise RuntimeError("邮件出口隔离未生效：smtplib.SMTP_SSL 没被哨兵顶替"
                               "（测试会真的发出运维告警/方案邮件，见 07 号 #1/#2）")
    except RuntimeError:
        raise
    except Exception:                                    # 环境缺依赖时不影响收集
        pass
    try:
        from app import db as db_mod
        if Path(db_mod.DB_PATH).resolve() == (BASE / "data" / "neweco.db").resolve():
            raise RuntimeError("数据库隔离未生效：db.DB_PATH 仍指向生产库 "
                               "（测试写 notify_log 等证据表会污染台账）")
    except RuntimeError:
        raise
    except Exception:                                    # 环境缺依赖时不影响收集
        pass


def cleanup() -> None:
    """会话收尾：清临时日志目录 + `data/weekly/` 下的 `_test_*` 残渣。

    `_test_*` 残渣来自 `Ledger.save()` 的滚动 `.bak`，它会绕过各测试 tearDown 的 unlink。
    """
    if _TMP_LOGS is not None:
        shutil.rmtree(_TMP_LOGS, ignore_errors=True)
    for p in sorted(WEEKLY_DIR.glob("_test_*")):
        try:
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            else:
                p.unlink()
        except OSError:
            pass
