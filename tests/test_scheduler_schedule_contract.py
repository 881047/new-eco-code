"""调度档位的**契约测试**（2026-09-14）

为什么：`app/scheduler.py` 的 **12 个档**（08:30 今日待办邮件 / 09:31 开盘执行 / 09:32 周频袖套 /
18:00 日链 / 18:05 周频晚间 / 18:30 日终汇总 / 19:30 打分保鲜 / **20:30 + 21:30 打分保鲜重试** /
19:45 周频健康 / 每月 1 日 18:33 月度 / 周日 09:15 备份）
是这台终端的**心跳**，而它们的时刻散在 900 行的注册代码里 —— 谁把 `18:05` 改成 `19:05`、
把 `day_of_week="mon-fri"` 漏掉、或把两个档的 id 写重，都**不会报错**，只会让某个动作悄悄不跑
（本项目对"静默不工作"的教训很多：07 号 #12 自愈没接线、#33/#34 看门狗两类缺陷）。

这里**静态**钉住注册表（AST 解析，不启动调度器 ⇒ 没有任何副作用、也不受当前时间影响）：
  1. 档位 id 集合与文档（03 号时段表）一致（多一个少一个都红）；
  2. 关键档位的 cron 字段（day_of_week / day / hour / minute）逐字一致；
  3. 每个档都带 `misfire_grace_time`（服务晚启动时的补跑窗口；漏了就等于"机器一停档就丢"）；
  4. 每个档抢锁失败都要**留痕**（裸 `_guarded` 会静默丢弃整档，见 A2）—— 例外须写进
     `_BARE_ALLOWED` 并注明兜底在哪；
  5. 那条例外名单不许过期。

运行: python -m pytest tests/test_scheduler_schedule_contract.py -q
"""
import ast
import sys
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

SRC = BASE / "app" / "scheduler.py"

# 档位 → (day_of_week, day, hour, minute)；None = 该参数不出现
EXPECTED = {
    # 2026-09-18 邮件优化：新增 08:30「今日待办」档 —— 挂单 09:31 就撮合，
    # "今天要不要我动手"必须在那之前送到；宽限只给 2h（错过开盘的待办比没有更误导）。
    "daily_brief":    ("mon-fri", None, 8, 30),   # 开盘前待办邮件
    # 2026-10-08 三模型选股交集早报: 08:35(用户"选股交集也发邮件/每日早上发"),
    # 错开今日待办; 非交易日不发(与 daily_brief 同源 trade_cal 门控)。
    "picks_email":    ("mon-fri", None, 8, 35),   # 三模型选股交集早报
    "open_exec":      ("mon-fri", None, 9, 31),    # 月频模拟盘开盘执行
    "weekly_sleeve":  ("mon-fri", None, 9, 32),    # 周频袖套执行档（锚点后首个交易日）
    "daily":          ("mon-fri", None, 18, 0),    # 数据更新 + 记账
    "weekly_evening": ("mon-fri", None, 18, 5),    # 周频晚间兜底档
    "daily_summary":  ("mon-fri", None, 18, 30),   # 日终汇总邮件
    "monthly":        (None, 1, 18, 33),           # 月度全流程（每月 1 日）
    "weekly_score":   ("mon-fri", None, 19, 30),   # 打分保鲜
    # 2026-09-14（07 #50）：日链偶尔晚于 19:30 完成（实测 09-11 是 19:41）⇒ 那一晚保鲜会
    # 看到旧锚点而跳过，而周一 09:32 只有一次机会；加两次重试（同一幂等函数）。
    "weekly_score_retry1": ("mon-fri", None, 20, 30),
    "weekly_score_retry2": ("mon-fri", None, 21, 30),
    "weekly_health":  ("mon-fri", None, 19, 45),   # 健康/风控/核对/归因/账本快照
    # 2026-10-05 全账户邮件: 三模型 7 纸面账户日报+按账户告警(读取失败/滞后/fterm 熔断线/
    # ECO-W2 熔断/月频盘总闸)。**不设 day_of_week**(=每日) —— fterm/ECO-W2 落账任务每日跑,
    # 周末账本不动时"数据滞后"体检项正好暴露"该跑没跑"。
    "accounts_mail":  (None, None, 21, 15),
    "backup":         ("sun", None, 9, 15),        # 每周备份
}


def _job_registrations() -> dict:
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    out = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if not (isinstance(fn, ast.Attribute) and fn.attr == "add_job"):
            continue
        kw = {k.arg: k.value for k in node.keywords if k.arg}
        if "id" not in kw or not isinstance(kw["id"], ast.Constant):
            continue
        vals = {}
        for name in ("day_of_week", "day", "hour", "minute"):
            v = kw.get(name)
            vals[name] = v.value if isinstance(v, ast.Constant) else None
        # 补跑窗口只要**存在**即可（源码里是表达式，如 5*3600+29*60 ⇒ 取不到常量值）
        vals["has_misfire"] = "misfire_grace_time" in kw
        vals["busy_wrapper"] = _busy_wrapper(node.args[0] if node.args else None)
        out[kw["id"].value] = vals
    return out


#: 抢锁失败仍能留痕/重排的注册工厂（`_retryable`/`_weekly_job` 内部都带 `on_busy`）
_STAGED = {"_retryable", "_weekly_job", "_monthly_slot"}

#: 有意不接重排的例外：档内自己已 `_stage(同名)` 留痕，再套一层 staged 会写出**同名**的
#: 第二行，而 `_stage_failed`/水位判定只看"该 stage 最新一行" ⇒ 外层那个恒为 ok 的顶层行
#: 会把内层 error 从台账里抹掉（2026-09-19 审查 A2 复核时确认，注释在注册处）。
_BARE_ALLOWED = {"open_exec": "09:31 开盘档：内部 _stage('open_exec') 已留痕，错过由 18:00 日链兜底"}


def _busy_wrapper(arg) -> str:
    """把 add_job 的第一个实参归成 'staged' / 'guarded_with_on_busy' / 'bare'。"""
    if arg is None:
        return "bare"
    # 既可能是调用 `_retryable(...)`，也可能是直接交函数名 `_monthly_slot`
    ids = set()
    for n in ast.walk(arg):
        if isinstance(n, ast.Call):
            if isinstance(n.func, ast.Name):
                ids.add(n.func.id)
        elif isinstance(n, ast.Name):
            ids.add(n.id)
    if ids & _STAGED:
        return "staged"
    for c in (n for n in ast.walk(arg) if isinstance(n, ast.Call)):
        if isinstance(c.func, ast.Name) and c.func.id == "_guarded":
            if any(k.arg == "on_busy" for k in c.keywords):
                return "guarded_with_on_busy"
    return "bare"


class TestSchedulerScheduleContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.jobs = _job_registrations()

    def test_job_id_set_matches_documented_slots(self):
        self.assertEqual(sorted(self.jobs), sorted(EXPECTED),
                         "档位集合与 03 号时段表不一致（新增/删除档位要同时改文档与本测试）")

    def test_cron_fields_are_unchanged(self):
        for jid, (dow, day, hour, minute) in EXPECTED.items():
            with self.subTest(job=jid):
                got = self.jobs.get(jid)
                self.assertIsNotNone(got, f"档位 {jid} 不见了")
                self.assertEqual(got.get("day_of_week"), dow, f"{jid} 的 day_of_week 漂了")
                self.assertEqual(got.get("day"), day, f"{jid} 的 day 漂了")
                self.assertEqual(got.get("hour"), hour, f"{jid} 的 hour 漂了")
                self.assertEqual(got.get("minute"), minute, f"{jid} 的 minute 漂了")

    def test_every_job_has_a_misfire_grace_window(self):
        for jid, vals in self.jobs.items():
            with self.subTest(job=jid):
                self.assertTrue(vals.get("has_misfire"),
                                f"{jid} 没有 misfire_grace_time ⇒ 服务晚启动时该档会被直接丢掉")

    def test_no_job_is_silently_dropped_on_lock_busy(self):
        """抢锁失败必须有留痕（2026-09-19 审查 A2）。

        `misfire_grace_time` 只兜"进程没起来"，兜不住"进程在但锁被另一档占着"：
        裸 `_guarded(on_busy=None)` 下 `_guarded` 直接返回 False，APScheduler 认为已执行 ⇒
        这一档今天**既没跑、台账里也没有任何一行**，事后无从发现。18:00 日链与唯一的周备份
        档就是这样被漏掉的（其余档 2026-09-14 P0-2 已接 `_retryable`）。
        """
        for jid, vals in self.jobs.items():
            if jid in _BARE_ALLOWED:
                continue
            with self.subTest(job=jid):
                self.assertNotEqual(
                    vals.get("busy_wrapper"), "bare",
                    f"{jid} 是裸 _guarded ⇒ 抢锁失败静默丢弃、无 pipeline_run 留痕；"
                    "要么走 _retryable/_weekly_job，要么把例外写进 _BARE_ALLOWED 并说明兜底在哪")

    def test_bare_allowlist_entries_are_still_bare(self):
        """例外名单不许**过期**：哪一天它被接上了重排，就该从名单里删掉。"""
        for jid in _BARE_ALLOWED:
            with self.subTest(job=jid):
                self.assertIn(jid, self.jobs, f"例外名单里的 {jid} 已不是注册档位")
                self.assertEqual(
                    self.jobs[jid].get("busy_wrapper"), "bare",
                    f"{jid} 已经接上留痕/重排 ⇒ 请从 _BARE_ALLOWED 删除")


if __name__ == "__main__":
    unittest.main()
