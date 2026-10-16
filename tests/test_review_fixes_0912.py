"""2026-09-12 全项目审查修复单测(核实后逐项补的红-绿锁)

运行: python -m unittest tests.test_review_fixes_0912 -v

本轮的核实结论: 39 项里 30 项属实 / 7 项部分属实 / 2 项误判(P1-10 nav 复利口径、
P2-23 start.bat tokens=1,* 不截断空格路径 —— 两者均已实测证伪, 不改代码, 见注释)。
本文件只锁「已确认 + 已修」的行为, 每条注明对应的审查编号。
"""
import copy
import datetime
import logging
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd

from app import config as cfg_mod
from app import db
from app import scheduler
from app.web import api


class TestAuthCodeGoesToEnvNotYaml(unittest.TestCase):
    """P0-06: 设置页保存的邮箱授权码必须落 .env, 不得写进受 git 跟踪的 config.yaml。

    原实现 `e["auth_code"] = ac` → 明文存进 config.yaml(已纳入 git 跟踪)⇒ 一次
    `git add -A` 就把授权码永久留在仓库历史里。tests/test_config.py 的守卫只检查
    **提交的文件内容**, 拦不住「用户在设置页里填一次」这条真正会踩的路径。
    """

    CFG = {"capital": 10000, "max_stocks": 12, "industry_max_weight": 0.25,
           "single_side_cost": 0.003, "risk_profile": "aggressive",
           "email": {"enabled": True, "smtp_server": "smtp.qq.com", "smtp_port": 465,
                     "sender": "a@qq.com", "recipients": ["b@qq.com"], "auth_code": ""}}

    def _call(self, body, cfg=None):
        cfg = dict(cfg or self.CFG)
        cfg["email"] = dict(cfg["email"])
        with mock.patch.object(api.cfg_mod, "load_config", return_value=cfg), \
                mock.patch.object(api.cfg_mod, "save_config") as p_save, \
                mock.patch.object(api.cfg_mod, "set_env_secret") as p_env:
            api.post_settings(body)
        return p_save, p_env

    def test_new_code_written_to_env_not_yaml(self):
        p_save, p_env = self._call({"email": {"auth_code": "NEWSECRET123"}})
        p_env.assert_called_once_with("EMAIL_AUTH_CODE", "NEWSECRET123")
        saved = p_save.call_args[0][0]
        self.assertEqual(saved["email"]["auth_code"], "",
                         "授权码被写回 config.yaml —— 该文件受 git 跟踪")

    def test_masked_placeholder_does_not_touch_env(self):
        # 界面未改动授权码时回传 "******", 不得把掩码本身存成密钥
        p_save, p_env = self._call({"email": {"auth_code": "******"}})
        p_env.assert_not_called()
        self.assertEqual(p_save.call_args[0][0]["email"]["auth_code"], "")

    def test_legacy_plaintext_code_migrated_out_of_yaml(self):
        # 旧版本写进 config.yaml 的明文码: 借这次保存搬到 .env, 值不变、位置变
        cfg = dict(self.CFG)
        cfg["email"] = dict(cfg["email"], auth_code="OLDSECRET")
        p_save, p_env = self._call({"email": {"recipients": ["c@qq.com"]}}, cfg)
        p_env.assert_called_once_with("EMAIL_AUTH_CODE", "OLDSECRET")
        self.assertEqual(p_save.call_args[0][0]["email"]["auth_code"], "")

    def test_legacy_plaintext_migrated_even_when_body_has_no_email(self):
        """P0-06 **补洞**(2026-09-12, 由 test_review_fixes_0905 里被改写成"真注入哨兵值"
        的用例发现): 原实现把这段清洗写在 `if "email" in body` 的里面 ⇒ "进了设置页、但
        没动邮箱那一段"这条最常见的路径上, 明文码原地留在受 git 跟踪的 config.yaml 里 ——
        而这条路径的暴露窗口最长: 文件里已经有明文码, 用户还以为点一下保存就干净了。
        """
        cfg = dict(self.CFG)
        cfg["email"] = dict(cfg["email"], auth_code="OLDSECRET")
        p_save, p_env = self._call({"capital": 20000}, cfg)     # body 里**没有** email
        p_env.assert_called_once_with("EMAIL_AUTH_CODE", "OLDSECRET")
        self.assertEqual(p_save.call_args[0][0]["email"]["auth_code"], "",
                         "授权码留在 config.yaml 里了(该文件受 git 跟踪)")
        self.assertEqual(p_save.call_args[0][0]["capital"], 20000, "正常字段没保存")

    def test_other_email_fields_still_saved(self):
        # 修复不能顺手把正常字段也吞掉
        p_save, _ = self._call({"email": {"auth_code": "X1", "smtp_port": 587,
                                          "recipients": ["d@qq.com"]}})
        saved = p_save.call_args[0][0]["email"]
        self.assertEqual(saved["smtp_port"], 587)
        self.assertEqual(saved["recipients"], ["d@qq.com"])


class TestSetEnvSecret(unittest.TestCase):
    """config.set_env_secret: 只改目标行, 其余(含注释)原样保留, 原子写。"""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.env = Path(self.dir.name) / ".env"
        self.patch = mock.patch.object(cfg_mod, "ENV_FILE", self.env)
        self.patch.start()
        self._old = os.environ.pop("EMAIL_AUTH_CODE", None)

    def tearDown(self):
        self.patch.stop()
        self.dir.cleanup()
        # set_env_secret 会写进程环境: 必须无条件还原, 否则泄漏给后续用例
        os.environ.pop("EMAIL_AUTH_CODE", None)
        if self._old is not None:
            os.environ["EMAIL_AUTH_CODE"] = self._old

    def test_replaces_existing_line_keeps_others(self):
        self.env.write_text("# 注释\nTUSHARE_TOKEN=abc\nEMAIL_AUTH_CODE=old\nX=1\n",
                            encoding="utf-8")
        cfg_mod.set_env_secret("EMAIL_AUTH_CODE", "new")
        self.assertEqual(self.env.read_text(encoding="utf-8"),
                         "# 注释\nTUSHARE_TOKEN=abc\nEMAIL_AUTH_CODE=new\nX=1\n")

    def test_appends_when_absent(self):
        self.env.write_text("TUSHARE_TOKEN=abc\n", encoding="utf-8")
        cfg_mod.set_env_secret("EMAIL_AUTH_CODE", "new")
        self.assertEqual(self.env.read_text(encoding="utf-8"),
                         "TUSHARE_TOKEN=abc\nEMAIL_AUTH_CODE=new\n")

    def test_creates_file_when_missing(self):
        self.assertFalse(self.env.exists())
        cfg_mod.set_env_secret("EMAIL_AUTH_CODE", "new")
        self.assertEqual(self.env.read_text(encoding="utf-8"), "EMAIL_AUTH_CODE=new\n")

    def test_does_not_touch_similarly_named_keys(self):
        # 行首锚定: EMAIL_AUTH_CODE_EXTRA= 与注释里的同名文本都不许被改
        self.env.write_text("EMAIL_AUTH_CODE_EXTRA=keep\n# EMAIL_AUTH_CODE=cmt\n",
                            encoding="utf-8")
        cfg_mod.set_env_secret("EMAIL_AUTH_CODE", "new")
        self.assertEqual(self.env.read_text(encoding="utf-8"),
                         "EMAIL_AUTH_CODE_EXTRA=keep\n# EMAIL_AUTH_CODE=cmt\n"
                         "EMAIL_AUTH_CODE=new\n")

    def test_env_var_updated_for_running_process(self):
        cfg_mod.set_env_secret("EMAIL_AUTH_CODE", "new")
        self.assertEqual(os.getenv("EMAIL_AUTH_CODE"), "new")


class TestPaperPausedKeyAlwaysPresent(unittest.TestCase):
    """P0-07: paper.paused 三个消费点都是 .get(..., False)(fail-open), 而该键原先
    不在 DEFAULT_CONFIG ⇒ 换机/回滚/手删后「暂停交易」静默失效。

    本项**只补显式键, 不改方向**: 「缺键=未暂停」是 tests/test_trading_pause.py
    明确锁定过的既有语义, 翻默认值是产品决策, 不属本次修复。
    """

    def test_default_config_has_explicit_paused_key(self):
        self.assertIn("paused", cfg_mod.DEFAULT_CONFIG["paper"])
        self.assertIs(cfg_mod.DEFAULT_CONFIG["paper"]["paused"], False)

    def test_paused_key_survives_merge_of_old_config(self):
        # 旧 config.yaml 没有 paper.paused → 合并后依然拿得到显式 False
        with tempfile.NamedTemporaryFile(suffix=".yaml", delete=False) as f:
            f.write(b"paper:\n  slip: 0.001\n")
            tmp = f.name
        try:
            with mock.patch.object(cfg_mod, "CONFIG_FILE", Path(tmp)):
                cfg_mod._cfg_cache["key"] = None
                cfg_mod._cfg_cache["merged"] = None
                cfg = cfg_mod.load_config(inject_env=False)
            self.assertIs(cfg["paper"]["paused"], False)
        finally:
            os.unlink(tmp)
            cfg_mod._cfg_cache["key"] = None
            cfg_mod._cfg_cache["merged"] = None

    def test_paper_engine_reads_the_key(self):
        # 消费点契约: trading_paused() 读的就是 paper.paused
        from app.paper import engine as paper_engine
        with mock.patch.object(cfg_mod, "load_config",
                               return_value={"paper": {"paused": True}}):
            self.assertTrue(paper_engine.trading_paused())
        with mock.patch.object(cfg_mod, "load_config",
                               return_value={"paper": {"paused": False}}):
            self.assertFalse(paper_engine.trading_paused())


class TestPitTradableMatrix(unittest.TestCase):
    """P0-05: 门禁必须裁到 PIT 可交易域 —— 原先全库无一处调用 PIT 裁域
    (`filter_panel` 是死代码, 模块 docstring 却写着"上游 PITEngine 提供可交易域")。
    """

    def _engine(self):
        from app.pit import engine as pit_mod
        # 名字必须打成桩: 真实 NameLookup 会读线上库, 而 000005/000007 这些代码在
        # 现实里是带「退」的退市股 —— 不隔离就会把线上数据当成测试输入
        names = mock.Mock()
        names.name_at.side_effect = lambda code, dt: "测试股"
        # 同时屏蔽 stock_basic: 否则 list_date 会按真实库把某些代码判成"未来上市"
        # (clip 到日历末尾 = 不可交易), 测试输入就又不纯了
        with mock.patch.object(pit_mod, "NameLookup", return_value=names), \
                mock.patch.object(pit_mod.db, "table_exists", return_value=False):
            dates = pd.bdate_range("2024-01-01", periods=90)
            cols = ["0000%02d.SZ" % i for i in range(10)]
            vol = pd.DataFrame(1.0, index=dates, columns=cols)
            vol.iloc[70:, 0] = 0.0      # 第 0 只: 最近 20 天全是 0 成交 ⇒ 流动性层剔
            close = pd.DataFrame(10.0, index=dates, columns=cols)
            m = {"close": close, "vol": vol, "amount": vol * 1e6,
                 "total_mv": close * 1e5, "adj": close / close.iloc[0]}
            cfg = {"star_enabled": True, "stock_size_pctl": 0.0, "stock_min_amt20": 0.0}
            return pit_mod.PITEngine(m=m, cfg=cfg), dates, cols

    def test_matrix_shape_and_unknown_dates_are_false(self):
        eng, dates, cols = self._engine()
        want = pd.DatetimeIndex([dates[80], pd.Timestamp("1990-01-02")])
        mat = eng.tradable_matrix(want)
        self.assertEqual(list(mat.index), list(want))
        self.assertEqual(list(mat.columns), cols)
        self.assertEqual(mat.dtypes.unique().tolist(), [bool])
        self.assertFalse(mat.loc[pd.Timestamp("1990-01-02")].any(),
                         "不在交易日历的日期必须整行不可交易")

    def test_mask_actually_removes_names(self):
        eng, dates, cols = self._engine()
        mat = eng.tradable_matrix([dates[-1]])
        self.assertEqual(int(mat.to_numpy().sum()), len(cols) - 1,
                         "近 20 日无成交的那只应当被剔掉, 其余保留")
        self.assertFalse(bool(mat.loc[dates[-1], cols[0]]))

    def test_filter_panel_now_uses_the_same_domain(self):
        eng, dates, cols = self._engine()
        panel = pd.DataFrame(1.0, index=[dates[-1]], columns=cols)
        out = eng.filter_panel(panel)
        self.assertTrue(pd.isna(out.loc[dates[-1], cols[0]]))
        self.assertEqual(float(out.loc[dates[-1], cols[1]]), 1.0)

    def test_factory_masks_both_paths(self):
        # 静态守卫: 门禁与适应度两处都必须经过裁域, 不能再出现裸 gate.evaluate
        import inspect
        from app.alpha import factory as F
        src = inspect.getsource(F.Factory._evaluate)
        self.assertIn("_masked_panel(", src)
        self.assertIn("_tradable(", src)
        fit = inspect.getsource(F.GPProposer._fitness)   # 适应度在提议器上, 不在 Factory
        self.assertIn("_masked_panel(", fit)
        self.assertNotIn("gate.evaluate(neu,", fit + src)


class TestAlphaBankRoundTrip(unittest.TestCase):
    """P1-09: `upsert(get(aid))` 这条真实路径(alpha/pipeline.py:233)不得把 yearly
    二次编码, 也不得把 created 刷掉。

    `get()` 读出来的 yearly 已是 TEXT, 再 dumps 一遍会变成 `"{\\"2020\\": ...}"` ——
    证据表的归因读数就烂在库里了。
    """

    def setUp(self):
        # 与 tests/test_alpha.py::TestAlphaBank 同一套隔离手法(临时目录换 DB_PATH)
        from app import db as db_mod
        self.tmp = tempfile.TemporaryDirectory()
        self._db, db_mod.DB_PATH = db_mod.DB_PATH, Path(self.tmp.name) / "t.db"

    def tearDown(self):
        from app import db as db_mod
        db_mod.DB_PATH = self._db
        self.tmp.cleanup()

    def _rec(self, **kw):
        r = {"alpha_id": "a1", "PASS": True, "yearly": {"2020": 0.12, "2021": -0.03},
             "c_same_sign": 1, "c_head_size": 0.01, "c_t": 3.2, "c_p_adj": 0.001,
             "c_top6": 0.008, "note": "n0"}
        r.update(kw)
        return r

    def test_double_upsert_keeps_yearly_single_encoded(self):
        from app.alpha import bank
        bank.upsert(self._rec())
        bank.upsert(bank.get("a1"), status="VALIDATED")     # 流水线走的就是这条
        got = bank.get("a1")["yearly"]
        self.assertNotIn('\\"', got, "yearly 被二次编码了")
        import json as _json
        self.assertEqual(_json.loads(got), {"2020": 0.12, "2021": -0.03})

    def test_created_not_overwritten_by_update(self):
        from app.alpha import bank
        bank.upsert(self._rec())
        first = bank.get("a1")["created"]
        with mock.patch.object(bank, "_now", return_value="2099-01-01 00:00:00"):
            bank.upsert(bank.get("a1"), status="VALIDATED")
        self.assertEqual(bank.get("a1")["created"], first,
                         "created 被状态回写覆盖了 —— 首次登记时间丢失")
        self.assertEqual(bank.get("a1")["updated"], "2099-01-01 00:00:00")

    def test_status_and_note_still_updated(self):
        from app.alpha import bank
        bank.upsert(self._rec())
        bank.upsert(bank.get("a1"), status="VALIDATED", note="改过")
        row = bank.get("a1")
        self.assertEqual(row["status"], "VALIDATED")
        self.assertEqual(row["note"], "改过")


class TestKgdBaselineFailClosed(unittest.TestCase):
    """P2-10: 要求了基线对比却没比成(读表异常/基线为空)时, 去相关门禁必须判否。

    原实现让 corr 停在 NaN, 而 corr_ok = (corr != corr) or ... 把 NaN 当宽松分支
    ⇒ 「读不到基线」反而让**换皮判据自动通过**。PASS 决定了 alpha/bank 的
    POOLED/CANDIDATE, 静默放宽等于把噪声放进池子。
    """

    N = 400

    def _fixture(self, seed=7):
        """造一份**统计判据全过**的合成面板, 好让 PASS 只由基线门禁决定。"""
        from app.factors.factor_engine import month_end_dates
        from app.models import expert
        idx = pd.bdate_range("2020-09-01", "2026-08-31")
        md = month_end_dates(idx)
        rng = np.random.default_rng(seed)
        cols = ["%06d.SZ" % i for i in range(self.N)]
        sc = pd.DataFrame(rng.normal(size=(len(md), self.N)), index=md, columns=cols)
        # 前向收益与分数同向 ⇒ 死区/活跃区头部超额都显著为正(判据全过)
        fwd = pd.DataFrame(0.0, index=idx, columns=cols)
        for d in md:
            rank = sc.loc[d].rank(pct=True).to_numpy()
            fwd.loc[d] = 0.02 * rank - 0.01 + rng.normal(0, 0.01, self.N)
        market = {"close": pd.DataFrame(1.0, index=idx, columns=cols[:1])}
        market["adj"] = market["close"].copy()
        return expert, market, sc, fwd

    def _run(self, baseline, sc, fwd, market, side_effect=None, empty=False):
        from app.models import expert
        calls = []

        def fake_load(table, *a, **kw):
            calls.append(table)
            if table == "die_test":
                return sc
            if side_effect is not None:
                raise side_effect
            if empty:
                return pd.DataFrame()
            # 与分数无关的另一套排序 ⇒ 低秩相关, 不是换皮
            rng2 = np.random.default_rng(99)
            return pd.DataFrame(rng2.normal(size=sc.shape), index=sc.index,
                                columns=sc.columns)

        with mock.patch.object(expert, "load_scores", side_effect=fake_load), \
                mock.patch.object(expert, "_forward_return", return_value=fwd):
            return expert.kgd("die_test", baseline=baseline, n_tests=1, market=market)

    def test_statistics_pass_without_baseline(self):
        # 先证明这份合成面板本身是"能过"的 —— 否则下面几条否决可能是别的原因
        expert, market, sc, fwd = self._fixture()
        rec = self._run(None, sc, fwd, market)
        self.assertTrue(rec["PASS"], f"统计判据没过, 测不到门禁: {rec}")
        self.assertTrue(rec["corr_ok"])

    def test_baseline_read_error_blocks_pass(self):
        expert, market, sc, fwd = self._fixture()
        rec = self._run("model_score", sc, fwd, market,
                        side_effect=RuntimeError("表不存在"))
        self.assertFalse(rec["PASS"], "基线读不到时门禁竟然放行")
        self.assertFalse(rec["corr_ok"])
        self.assertIn("基线对比未完成", rec.get("reason", ""))

    def test_empty_baseline_table_blocks_pass(self):
        expert, market, sc, fwd = self._fixture()
        rec = self._run("model_score", sc, fwd, market, empty=True)
        self.assertFalse(rec["PASS"], "基线表为空时门禁竟然放行")
        self.assertIn("基线对比未完成", rec.get("reason", ""))

    def test_low_corr_baseline_still_passes(self):
        expert, market, sc, fwd = self._fixture()
        rec = self._run("model_score", sc, fwd, market)
        self.assertLess(abs(rec["corr_with_baseline"]), 0.95)
        self.assertTrue(rec["PASS"], "正常基线(低相关)不该被判否")

    def test_repackaged_baseline_blocked(self):
        # 基线就是自己 ⇒ 秩相关 1.0 ⇒ 换皮, 必须拦(修复不得把这个方向也弄丢)
        from app.models import expert
        _, market, sc, fwd = self._fixture()
        with mock.patch.object(expert, "load_scores", return_value=sc), \
                mock.patch.object(expert, "_forward_return", return_value=fwd):
            rec = expert.kgd("die_test", baseline="model_score", n_tests=1, market=market)
        self.assertAlmostEqual(rec["corr_with_baseline"], 1.0, places=6)
        self.assertFalse(rec["PASS"])


class TestMonthlyLockDeferral(unittest.TestCase):
    """P1-06: 月链抢不到锁必须留痕 + 退避重排, 不能静默丢一整月。

    原始缺陷: monthly cron(每月1日18:33)只比日链(18:00)晚 33 分钟, 日链轻易更长
    ⇒ _guarded 抢锁失败只 log.info, APScheduler 认为已执行、不重试 ⇒ 当月方案/
    训练/回测全缺, 只有重启走 _catch_up 能救。
    """

    REASON = "本进程已有任务在运行"

    def _fire(self, attempt, sched=..., reason=REASON):
        """跑一次 _monthly_slot 并在补丁生效期间**真正调用** on_busy 回调
        (回调若在 with 之外调用, 打到的是真实 _record_skip/scheduler)。"""
        recorded, jobs = [], []
        if sched is ...:
            sched = mock.Mock()
            sched.add_job.side_effect = lambda fn, trigger, **kw: jobs.append((trigger, kw))
        with mock.patch.object(scheduler, "_guarded", return_value=False) as p_g, \
                mock.patch.object(scheduler, "_record_skip",
                                  side_effect=lambda s, r: recorded.append((s, r))), \
                mock.patch.object(scheduler, "_scheduler", sched), \
                mock.patch.object(scheduler.log, "error") as p_err:
            scheduler._monthly_slot(attempt)
            on_busy = p_g.call_args.kwargs.get("on_busy")
            if on_busy is not None:
                on_busy(reason)
        return p_g, recorded, jobs, p_err

    def test_busy_is_recorded_and_rescheduled(self):
        p_g, recorded, jobs, _ = self._fire(0)
        # 走了 on_busy 钩子(而不是裸 _guarded)
        self.assertIsNotNone(p_g.call_args.kwargs.get("on_busy"))
        self.assertEqual(recorded, [("monthly", self.REASON)])
        self.assertEqual(len(jobs), 1)
        trigger, kw = jobs[0]
        self.assertEqual(trigger, "date")
        self.assertEqual(kw["id"], "monthly_retry_0")

    def test_backoff_schedule_is_30_60_120(self):
        self.assertEqual(scheduler._MONTHLY_RETRY_MIN, (30, 60, 120))
        for i, delay in enumerate(scheduler._MONTHLY_RETRY_MIN):
            _, _, jobs, _ = self._fire(i)
            _, kw = jobs[0]
            delta = kw["run_date"] - datetime.datetime.now(scheduler._TZ)
            self.assertAlmostEqual(delta.total_seconds() / 60, delay, delta=2)

    def test_gives_up_after_last_retry_with_error(self):
        _, recorded, jobs, p_err = self._fire(len(scheduler._MONTHLY_RETRY_MIN))
        self.assertEqual(jobs, [], "退避用尽后不应再重排")
        self.assertTrue(p_err.called, "退避用尽必须喊 ERROR, 不能又静默")
        self.assertEqual(recorded, [("monthly", self.REASON)])

    def test_missing_scheduler_does_not_crash(self):
        # 调度器不可用(退出中)时只记日志, 不得抛
        self._fire(0, sched=None, reason="其他进程正在执行")

    def test_on_busy_exception_does_not_break_guarded(self):
        # 回调自身出错不得让 _guarded 抛出去(那会连带丢掉锁释放路径)
        with mock.patch.object(scheduler, "_acquire_file_lock", return_value=False):
            ok = scheduler._guarded(lambda: None, "测试",
                                    on_busy=lambda r: (_ for _ in ()).throw(RuntimeError("x")))
        self.assertFalse(ok)

    def test_no_callback_means_old_behavior(self):
        with mock.patch.object(scheduler, "_acquire_file_lock", return_value=False):
            self.assertFalse(scheduler._guarded(lambda: None, "测试"))


class TestMonthlyRegimeGuard(unittest.TestCase):
    """P1-07: 月链的 pool_index/compute_regime 必须与日链同口径兜底。"""

    def test_monthly_wraps_regime_in_try_except(self):
        import inspect
        src = inspect.getsource(scheduler.monthly_pipeline)
        self.assertIn("市场状态计算失败(不影响训练与选股链)", src)
        self.assertNotIn('\n    _stage(compute_regime, "regime")', src,
                         "月链 compute_regime 又变回裸调用了")


class TestAnnDateAlignmentGuard(unittest.TestCase):
    """P1-01: 空/非法 ann_date 不得被对齐成日历首个交易日。

    原实现 pos = cal.searchsorted(...): "" 小于任何 "YYYYMMDD" ⇒ 排最前 ⇒
    该财报自 2013 年首个交易日全程可见(最严重前视, 且静默)。
    """

    CAL = ["20130104", "20130107", "20240102", "20240103"]

    def test_empty_ann_date_becomes_none_not_first_day(self):
        from app.factors import factor_engine as fe
        s = pd.Series(["", "20240102"])
        out = fe.shift_to_next_trading_day(s, self.CAL)
        self.assertIsNone(out.iloc[0], "空 ann_date 被当成日历首日 = 2013 年起前视")
        self.assertEqual(out.iloc[1], "20240103")

    def test_non_numeric_and_nan_are_dropped(self):
        from app.factors import factor_engine as fe
        s = pd.Series(["nan", "None", "2024-01-02", np.nan, "20240103"])
        out = fe.shift_to_next_trading_day(s, self.CAL)
        self.assertEqual(list(out[:4]), [None] * 4)
        # 末行日期合法但已是日历最后一天: 无「次一交易日」⇒ None(既有语义)
        self.assertIsNone(out.iloc[4])

    def test_valid_dates_unchanged(self):
        from app.factors import factor_engine as fe
        s = pd.Series(["20130104", "20130105", "20240101"])
        out = fe.shift_to_next_trading_day(s, self.CAL)
        self.assertEqual(list(out), ["20130107", "20130107", "20240102"])

    def test_empty_calendar_returns_all_none(self):
        from app.factors import factor_engine as fe
        out = fe.shift_to_next_trading_day(pd.Series(["20240102"]), [])
        self.assertIsNone(out.iloc[0])


class TestConfigContract(unittest.TestCase):
    """P2-16 / P2-17: 配置要有契约校验; 保存不得抹掉 config.yaml 的注释。

    config.yaml 是基线口径的唯一事实源, 而设置页保存会重写它 —— 原实现用 safe_dump
    整份重写, 35 行口径注释(以及"训练起点 vs 评估起点"那段血泪史)会在用户点一次保存后
    消失。同时 load_config 对数值/枚举零校验, `risk_profile: aggresive` 会一路走到
    builder 深处才炸, 或者更坏 —— 静默回退到别的档位权重。
    """

    YAML_WITH_COMMENTS = (
        "# ---- 基线口径(不得丢失) ----\n"
        "# capital: 2 万纸面账户, 用户 2026-09-10 拍板\n"
        "capital: 20000\n"
        "# 风险档位: aggressive 的权益权重是恒定 0.873\n"
        "risk_profile: aggressive\n"
        "max_stocks: 6\n"
    )

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg_file = Path(self.tmp.name) / "config.yaml"
        self._cache = dict(cfg_mod._cfg_cache)
        self.addCleanup(self._restore)
        self.p = mock.patch.object(cfg_mod, "CONFIG_FILE", self.cfg_file)
        self.p.start()
        self.addCleanup(self.p.stop)

    def _restore(self):
        cfg_mod._cfg_cache.clear()
        cfg_mod._cfg_cache.update(self._cache)

    def _write(self, text):
        self.cfg_file.write_text(text, encoding="utf-8")

    # ---- P2-16: 契约校验 ------------------------------------------------
    def test_live_default_config_passes(self):
        """默认模板本身必须合法 —— 否则每次 load_config 都会刷错误日志"""
        self.assertEqual(cfg_mod.validate_config(copy.deepcopy(cfg_mod.DEFAULT_CONFIG)), [])

    def test_catches_bad_enum_and_ranges(self):
        cases = {
            "risk_profile": "aggresive",
            "capital": -1,
            "max_stocks": 0,
            "industry_max_weight": 1.5,
            "single_side_cost": 5,
            "eval_start": "2019-07-22",
            "data_start": 20190722,
            "benchmark": "",
        }
        for k, bad in cases.items():
            cfg = copy.deepcopy(cfg_mod.DEFAULT_CONFIG)
            cfg[k] = bad
            with self.subTest(key=k):
                self.assertTrue(cfg_mod.validate_config(cfg), f"{k}={bad!r} 没被拦下")

    def test_catches_broken_sub_dicts(self):
        cfg = copy.deepcopy(cfg_mod.DEFAULT_CONFIG)
        cfg["state_stocks"] = {"attack": 6, "neutral": 6}          # 缺 defense
        cfg["fees"] = "便宜"                                        # 整块被顶掉
        cfg["paper"] = None
        probs = cfg_mod.validate_config(cfg)
        self.assertTrue(any("state_stocks.defense" in x for x in probs), probs)
        self.assertTrue(any("fees" in x for x in probs), probs)
        self.assertTrue(any("paper" in x for x in probs), probs)

    def test_save_config_rejects_invalid(self):
        cfg = copy.deepcopy(cfg_mod.DEFAULT_CONFIG)
        cfg["risk_profile"] = "aggresive"
        with self.assertRaises(ValueError) as cm:
            cfg_mod.save_config(cfg)
        self.assertIn("risk_profile", str(cm.exception))
        self.assertFalse(self.cfg_file.exists(), "校验没过却把文件写了")

    def test_load_config_logs_problems_without_raising(self):
        """读侧只告警不抛: 一个手改错字不该让整个服务/月链起不来"""
        self._write("capital: 20000\nrisk_profile: aggresive\n")
        with self.assertLogs("app.config", level="ERROR") as cm:
            cfg = cfg_mod.load_config()
        self.assertEqual(cfg["risk_profile"], "aggresive", "读侧不该悄悄改枚举值")
        self.assertTrue(any("risk_profile" in m for m in cm.output), cm.output)

    def test_load_config_repairs_wrong_types(self):
        self._write('capital: "两万"\npaper: 关闭\ndynamic: [1,2]\n')
        with self.assertLogs("app.config", level="ERROR"):
            cfg = cfg_mod.load_config()
        self.assertIsInstance(cfg["capital"], (int, float))
        self.assertIsInstance(cfg["paper"], dict)
        self.assertIsInstance(cfg["dynamic"], dict)

    def test_repair_does_not_put_shared_defaults_into_cfg(self):
        """还原默认时必须深拷贝 —— 否则调用方一改就污染 DEFAULT_CONFIG(全进程共用)"""
        self._write("paper: 关闭\n")
        cfg = cfg_mod.load_config()
        cfg["paper"]["slip"] = 999
        self.assertNotEqual(cfg_mod.DEFAULT_CONFIG["paper"]["slip"], 999)

    # ---- P2-17: 保存不得抹注释 -------------------------------------------
    # ⚠️ 下面三条"写回"用例一律 `load_config(inject_env=False)`: 这是**唯一**被允许接
    # `save_config` 的读法(生产里 api.post_settings 就是这么读的)。默认的 inject_env=True
    # 会把 .env 里的授权码注进内存 cfg, 再写回就等于把密钥搬进受 git 跟踪的 config.yaml ——
    # 见 test_save_refuses_env_injected_auth_code 与 config.save_config 的硬拒。
    def test_save_preserves_comments(self):
        self._write(self.YAML_WITH_COMMENTS)
        cfg = cfg_mod.load_config(inject_env=False)
        cfg["capital"] = 50000
        cfg_mod.save_config(cfg)
        txt = self.cfg_file.read_text(encoding="utf-8")
        self.assertIn("# ---- 基线口径(不得丢失) ----", txt)
        self.assertIn("# capital: 2 万纸面账户, 用户 2026-09-10 拍板", txt)
        self.assertIn("# 风险档位: aggressive 的权益权重是恒定 0.873", txt)
        self.assertIn("capital: 50000", txt)
        self.assertIn("risk_profile: aggressive", txt)

    def test_save_does_not_materialize_untouched_defaults(self):
        """文件里没写过、值又等于默认的键, 不该被设置页一次性写进去"""
        self._write("capital: 20000\n")
        cfg = cfg_mod.load_config(inject_env=False)
        cfg_mod.save_config(cfg)
        txt = self.cfg_file.read_text(encoding="utf-8")
        self.assertIn("capital: 20000", txt)
        self.assertNotIn("hysteresis_buffer", txt, "未动过的默认键被实体化了")
        self.assertNotIn("regime_equity_weight", txt, "未动过的默认键被实体化了")

    def test_save_roundtrip_keeps_effective_config_identical(self):
        """写回再读, 合并结果必须逐键相同(不能因为跳过默认键而丢配置)"""
        self._write(self.YAML_WITH_COMMENTS)
        before = cfg_mod.load_config(inject_env=False)
        cfg_mod.save_config(before)
        after = cfg_mod.load_config(inject_env=False)
        self.assertEqual(before, after)

    def test_save_refuses_env_injected_auth_code(self):
        """**load(inject_env=True) → save 这条回路必须被硬拒**(2026-09-12 P0-06 补洞)。

        这条回路是最容易写出来、后果最重的一种: 读的时候把 .env 的码注进来(默认行为!),
        写的时候原样落盘 ⇒ 密钥进了受 git 跟踪的文件, 而调用方全程毫无察觉。三条断言:
        拒绝、报错说得清怎么办、以及**没写任何文件**。
        """
        self._write(self.YAML_WITH_COMMENTS)
        with mock.patch.dict(os.environ, {"EMAIL_AUTH_CODE": "ENV_INJECTED_9f3a"}, clear=False):
            cfg = cfg_mod.load_config()                     # inject_env 默认就是 True
            self.assertEqual(cfg["email"]["auth_code"], "ENV_INJECTED_9f3a", "前提没成立")
            with self.assertRaises(ValueError) as cm:
                cfg_mod.save_config(cfg)
        msg = str(cm.exception)
        self.assertIn("set_env_secret", msg)
        self.assertIn("inject_env=False", msg)
        # 拒绝了 ⇒ 文件必须**一个字节都没动**(本用例先写了 YAML_WITH_COMMENTS)
        after = self.cfg_file.read_text(encoding="utf-8")
        self.assertEqual(after, self.YAML_WITH_COMMENTS, "拒绝了却还是把文件改了")
        self.assertNotIn("ENV_INJECTED_9f3a", after)

    def test_settings_endpoint_maps_validation_error_to_400(self):
        self._write(self.YAML_WITH_COMMENTS)
        with mock.patch.object(cfg_mod, "save_config",
                               side_effect=ValueError("配置校验未通过: risk_profile 不在 ...")):
            with self.assertRaises(api.HTTPException) as cm:
                api.post_settings({"capital": 20000})
        self.assertEqual(cm.exception.status_code, 400)


class TestFileLockAtomicity(unittest.TestCase):
    """P2-15: 锁文件必须**先有完整内容, 再对外可见**。

    原写法 `os.open(O_CREAT|O_EXCL)` 只让"创建"原子, 内容随后才 write —— 在 open 与 write
    之间, 锁文件是个**空文件**; 而 `_lock_stale()` 恰好把空文件判为陈旧(它的 except 分支
    就是为这种半成品写的), 于是第二个进程会名正言顺地把活锁搬走 ⇒ 双方同时持锁,
    正是这把锁要防的"多实例并发写乱账"。修法: 先写自己的临时文件, 再 `os.link` 原子挂上。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.lock = Path(self.tmp.name) / ".task.lock"
        self.p = mock.patch.object(scheduler, "_LOCK_FILE", self.lock)
        self.p.start()
        self.addCleanup(self.p.stop)
        self.addCleanup(lambda: scheduler._release_file_lock())

    def test_token_is_complete_at_the_moment_it_becomes_visible(self):
        """核心断言: os.link 被调用的那一刻, 内容已经写好了"""
        seen = {}
        real_link = os.link

        def spy(src, dst):
            seen["src"] = Path(src).read_text(encoding="utf-8")
            seen["visible_before"] = self.lock.exists()
            return real_link(src, dst)

        with mock.patch.object(scheduler.os, "link", spy):
            self.assertTrue(scheduler._acquire_file_lock())
        self.assertEqual(seen["src"], scheduler._lock_token(),
                         "链接时临时文件内容不完整 ⇒ 空文件窗口仍在")
        self.assertFalse(seen["visible_before"])
        self.assertEqual(self.lock.read_text(encoding="utf-8"), scheduler._lock_token())

    def test_no_temp_file_left_behind(self):
        self.assertTrue(scheduler._acquire_file_lock())
        self.assertEqual(list(Path(self.tmp.name).glob("*.tmp.*")), [])

    def test_second_acquire_fails_while_held(self):
        self.assertTrue(scheduler._acquire_file_lock())
        self.assertFalse(scheduler._acquire_file_lock(), "活锁被抢了")
        self.assertEqual(self.lock.read_text(encoding="utf-8"), scheduler._lock_token())

    def test_primary_path_does_not_use_the_legacy_racy_acquire(self):
        with mock.patch.object(scheduler, "_acquire_file_lock_legacy") as legacy:
            self.assertTrue(scheduler._acquire_file_lock())
        legacy.assert_not_called()

    def test_falls_back_when_hardlink_unsupported(self):
        """exFAT/网络盘不支持硬链接时必须还能拿到锁(退回旧写法并告警)"""
        with mock.patch.object(scheduler.os, "link",
                               side_effect=OSError(1, "Operation not permitted")):
            self.assertTrue(scheduler._acquire_file_lock())
        self.assertEqual(self.lock.read_text(encoding="utf-8"), scheduler._lock_token())

    def test_stale_lock_can_still_be_taken_over(self):
        """陈旧锁(持有者已死)仍要能被接管 —— 新写法不能把这条路堵死"""
        self.lock.write_text("999999\n0", encoding="utf-8")   # 注定不存在的 pid
        self.assertTrue(scheduler._acquire_file_lock())
        self.assertEqual(self.lock.read_text(encoding="utf-8"), scheduler._lock_token())

    def test_empty_lock_file_still_counts_as_stale(self):
        """这条锁的是"为什么原来会出事": 空文件被判陈旧 —— 保留该判断(兼容旧残留),
        但**新代码不再产生**空锁文件(上面第一条测的就是这个)"""
        self.lock.write_text("", encoding="utf-8")
        self.assertTrue(scheduler._lock_stale())

    def test_release_only_removes_own_lock(self):
        self.lock.write_text("999999\n0", encoding="utf-8")
        scheduler._release_file_lock()
        self.assertTrue(self.lock.exists(), "把别人的锁删了 ⇒ 互斥失效")

    def test_real_process_serialization(self):
        """真·双进程: 一个持锁时另一个必须拿不到, 交还后必须拿得到。

        注意这里让子进程**正常释放**后再断言拿走锁。不在这里测"进程被杀后接管":
        Windows 上父进程仍握着子进程句柄时, 已终止的进程对 `OpenProcess` 依然"存在",
        `_lock_stale` 会(按既定取舍"宁可锁滞留也不偷活锁")判为有效 —— 那是僵尸句柄的
        假象, 不是产品缺陷。"已死进程的锁能接管"由 test_stale_lock_can_still_be_taken_over
        用不存在的 pid 覆盖。
        """
        holder = subprocess.Popen(
            [sys.executable, "-c",
             "import sys, time; sys.path.insert(0, sys.argv[1]);\n"
             "from app import scheduler; from pathlib import Path;\n"
             "scheduler._LOCK_FILE = Path(sys.argv[2]);\n"
             "print('ACQ' if scheduler._acquire_file_lock() else 'BUSY', flush=True);\n"
             "time.sleep(int(sys.argv[3]));\n"
             "scheduler._release_file_lock()",
             str(ROOT), str(self.lock), "2"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(holder.kill)
        first = holder.stdout.readline().decode().strip()
        self.assertEqual(first, "ACQ", "第一个进程就没拿到锁")
        self.assertFalse(scheduler._acquire_file_lock(), "另一进程持锁时本进程也拿到了")
        _out, err = holder.communicate(timeout=60)
        self.assertEqual(holder.returncode, 0, err.decode("utf-8", "replace"))
        self.assertTrue(scheduler._acquire_file_lock(), "对方交还后本进程拿不到锁")


class _SpyConn:
    """记录 `close()` 是否被调用的连接代理。

    sqlite3.Connection 是 C 类型, 挂不上实例属性, 所以包一层; 其余属性透传。
    判定"关没关"用两把尺子: ①`closed` 标志; ②真连接关掉后再 execute 必抛 ProgrammingError。
    """

    def __init__(self, real):
        self._real = real
        self.closed = False

    def __getattr__(self, name):
        return getattr(self._real, name)

    def close(self):
        self.closed = True
        self._real.close()


def _really_closed(conn) -> bool:
    try:
        conn.execute("SELECT 1")
        return False
    except sqlite3.ProgrammingError:
        return True


class TestDbConnNoLeak(unittest.TestCase):
    """P2-12: 数据库连接在异常路径也不得泄漏。

    长驻服务里漏一个连接 = 漏一个文件句柄; `init_db` 里漏的更狠 —— 迁移中途失败会留下
    **持写锁**的连接, 其他进程全部 `database is locked`。
    """

    def test_conn_ctx_closes_on_normal_exit(self):
        with db.conn_ctx() as conn:
            conn.execute("SELECT 1")
        self.assertTrue(_really_closed(conn))

    def test_conn_ctx_closes_on_exception(self):
        holder = {}
        with self.assertRaises(ValueError):
            with db.conn_ctx() as conn:
                holder["c"] = conn
                raise ValueError("业务代码炸了")
        self.assertTrue(_really_closed(holder["c"]))

    def test_init_db_closes_even_when_migration_fails(self):
        """迁移抛错时必须放手 —— 否则留一个持写锁的连接卡死其他进程"""
        seen = []
        real = db.get_conn

        def spy():
            c = _SpyConn(real())
            seen.append(c)
            return c

        with mock.patch.object(db, "get_conn", spy), \
                mock.patch.object(db, "_init_db", side_effect=RuntimeError("迁移失败")):
            with self.assertRaises(RuntimeError):
                db.init_db()
        self.assertTrue(seen, "没走到 get_conn, 这条测试是空转的")
        self.assertTrue(all(c.closed for c in seen), "迁移失败后连接没关")
        self.assertTrue(all(_really_closed(c) for c in seen), "标志说关了但真连接还开着")

    def test_notify_log_send_closes_when_insert_fails(self):
        from app import notify
        seen = []
        real = db.get_conn          # 必须在打补丁前抓, 否则 spy 里的 db.get_conn 就是它自己

        def spy():
            c = _SpyConn(real())
            c.execute = mock.Mock(side_effect=sqlite3.OperationalError("表没了"))
            seen.append(c)
            return c

        with mock.patch.object(db, "get_conn", spy):
            notify._log_send("test", True)      # 内部已 try/except, 不该外抛
        self.assertTrue(seen, "没走到 get_conn, 这条测试是空转的")
        self.assertTrue(all(c.closed for c in seen), "写入失败后连接没关")

    def test_monitor_closes_conn_when_body_raises(self):
        from app.portfolio import monitor
        seen = []
        real = db.get_conn          # 同上: 先抓再打补丁

        def spy():
            c = _SpyConn(real())
            seen.append(c)
            return c

        cfg = {"dynamic": {"enabled": True}, "risk_profile": "balanced"}
        with mock.patch.object(db, "get_conn", spy), \
                mock.patch.object(monitor.cfg_mod, "load_config", return_value=cfg), \
                mock.patch.object(monitor.pd, "read_sql",
                                  side_effect=RuntimeError("查询炸了")):
            with self.assertRaises(RuntimeError):
                monitor.run_monitor("20240102")
        self.assertTrue(seen, "没走到 get_conn, 这条测试是空转的")
        self.assertTrue(all(c.closed for c in seen), "run_monitor 异常路径漏了连接")


class TestLoggerRootAndRotation(unittest.TestCase):
    """P2-13 / P2-14: 日志 handler 必须挂在**根**上; 轮转失败不得丢记录。

    两个都是"静默失效"类缺陷: 前者让全项目日志一条都不落盘且不报错; 后者在 Windows
    多进程下让某个进程**从此永久**失去文件日志 —— 而 app.log 是证据台账的一部分。
    """

    def setUp(self):
        from app import logger as logger_mod
        self.mod = logger_mod
        self._saved = (logger_mod._handler, logger_mod._initialized,
                       list(logging.getLogger().handlers),
                       logging.getLogger().level)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._restore)

    def _restore(self):
        h, init, handlers, lvl = self._saved
        root = logging.getLogger()
        for x in list(root.handlers):
            root.removeHandler(x)
        for x in handlers:
            root.addHandler(x)
        root.setLevel(lvl)
        # Windows 上文件被 handler 开着就删不掉临时目录 —— 先关掉本轮造的 handler
        if self.mod._handler is not None and self.mod._handler is not h:
            try:
                self.mod._handler.close()
            except Exception:
                pass
        self.mod._handler, self.mod._initialized = h, init
        self.tmp.cleanup()

    def _fresh(self):
        """在临时目录里强制重建 handler(不碰生产的 logs/app.log)"""
        self.mod._handler = None
        self.mod._initialized = False
        return mock.patch.object(self.mod, "LOG_DIR", Path(self.tmp.name))

    # ------------------------------------------------------------------
    def test_handler_attached_to_root_not_to_named_child(self):
        with self._fresh():
            root = self.mod.setup_logger("app.weird.child")
        self.assertIs(root, logging.getLogger(), "应返回根 logger")
        self.assertIn(self.mod._handler, logging.getLogger().handlers)
        self.assertEqual(logging.getLogger("app.weird.child").handlers, [],
                         "handler 挂到子 logger 上 ⇒ 根 logger 空着, 别人全不落盘(P2-14)")

    def test_child_logger_records_reach_the_file(self):
        """端到端: 模块里惯用的 getLogger(__name__) 必须真的写进 app.log"""
        with self._fresh():
            self.mod.setup_logger("app.weird.child")
            logging.getLogger("app.some.module").warning("P2-14 探针-落盘")
        txt = (Path(self.tmp.name) / "app.log").read_text(encoding="utf-8")
        self.assertIn("P2-14 探针-落盘", txt)

    def test_detached_handler_is_reattached(self):
        """handler 被人从根上摘掉后, 再次 setup_logger 应挂回去(不留静默空转)"""
        with self._fresh():
            self.mod.setup_logger()
            logging.getLogger().removeHandler(self.mod._handler)
            self.mod.setup_logger()
            self.assertIn(self.mod._handler, logging.getLogger().handlers)

    def test_logger_uses_concurrent_handler_when_available(self):
        with self._fresh():
            self.mod.setup_logger()
        self.assertEqual(self.mod.handler_kind(), "ConcurrentRotatingFileHandler",
                         "concurrent-log-handler 已装却没用上 ⇒ 多进程轮转仍无互斥")

    def test_falls_back_to_safe_handler_without_the_package(self):
        with self._fresh(), mock.patch.dict(sys.modules,
                                            {"concurrent_log_handler": None}):
            self.mod.setup_logger()
        self.assertEqual(self.mod.handler_kind(), "_SafeRotatingFileHandler")

    def test_rotation_failure_does_not_drop_records(self):
        """P2-13 的核心断言: 轮转改名失败(PermissionError)后记录**仍在**。

        旧实现 `RotatingFileHandler` 在这种情况下会把此后**每一条**记录都丢掉:
        doRollover 先把 stream 置 None 再抛错, 下次 emit 重开文件、大小仍超限
        ⇒ shouldRollover 恒为真 ⇒ 每次都走进同一个异常。
        """
        path = Path(self.tmp.name) / "rot.log"
        h = self.mod._SafeRotatingFileHandler(path, maxBytes=200, backupCount=3,
                                              encoding="utf-8")
        lgr = logging.getLogger("test.rot.%d" % id(self))
        lgr.addHandler(h)
        lgr.setLevel(logging.INFO)
        lgr.propagate = False
        self.addCleanup(lambda: (lgr.removeHandler(h), h.close()))
        with mock.patch.object(h, "doRollover",
                               side_effect=PermissionError("被别的进程占着")):
            for i in range(20):
                lgr.info("轮转失败期记录 %02d", i)
        txt = path.read_text(encoding="utf-8")
        for i in range(20):
            self.assertIn("轮转失败期记录 %02d" % i, txt,
                          "第 %d 条在轮转失败后丢了" % i)

    def test_stock_rotating_handler_also_loses_records(self):
        """对照臂: 证明上面那条不是白测的 —— 上游 `RotatingFileHandler` 在同样条件下丢光。

        没有这条, `test_rotation_failure_does_not_drop_records` 可能只是因为 mock
        没生效而恒真。
        """
        from logging.handlers import RotatingFileHandler
        path = Path(self.tmp.name) / "stock.log"
        h = RotatingFileHandler(path, maxBytes=200, backupCount=3, encoding="utf-8")
        lgr = logging.getLogger("test.stockrot.%d" % id(self))
        lgr.addHandler(h)
        lgr.setLevel(logging.INFO)
        lgr.propagate = False
        self.addCleanup(lambda: (lgr.removeHandler(h), h.close()))
        with mock.patch.object(h, "doRollover",
                               side_effect=PermissionError("被别的进程占着")), \
                mock.patch.object(h, "handleError"):      # 静音 stderr 的 "Logging error"
            for i in range(20):
                lgr.info("上游 handler 记录 %02d", i)
        txt = path.read_text(encoding="utf-8")
        self.assertNotIn("上游 handler 记录 19", txt,
                         "上游居然不丢? 那 P2-13 的故障链描述需要重核")

    def test_two_processes_can_rotate_the_same_file(self):
        """真·双进程: 两个进程并发写同一个文件并触发轮转, 记录一条都不能少。

        这是 P2-13 的现实场景(生产服务常开 app.log, 脚本同时在写)。必须用子进程,
        同进程里两个 handler 不构成"多进程"。走 `_make_handler` = 生产实际选中的那版。
        """
        path = Path(self.tmp.name) / "mp.log"
        worker = (
            "import logging, sys\n"
            "from app.logger import _make_handler\n"
            "p, tag, n = sys.argv[1], sys.argv[2], int(sys.argv[3])\n"
            "h = _make_handler(p)\n"
            "h.maxBytes = 1500; h.backupCount = 6\n"
            "lg = logging.getLogger('mp'); lg.addHandler(h); lg.setLevel(logging.INFO)\n"
            "lg.propagate = False\n"
            "for i in range(n): lg.info('%s-%03d-' + 'x' * 40, tag, i)\n"
            "h.close()\n"
        )
        procs = [subprocess.Popen([sys.executable, "-c", worker, str(path), tag, "25"],
                                  cwd=str(ROOT), stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE) for tag in ("A", "B")]
        for p in procs:
            _out, err = p.communicate(timeout=120)
            self.assertEqual(p.returncode, 0, err.decode("utf-8", "replace"))
        parts = sorted(path.parent.glob("mp.log*"))
        self.assertGreater(len(parts), 1,
                           "只有 mp.log 没有轮转备份 ⇒ 轮转压根没触发, 这条测试是空转的")
        txt = "".join(f.read_text(encoding="utf-8", errors="replace") for f in parts)
        for tag in ("A", "B"):
            for i in range(25):
                self.assertIn("%s-%03d-" % (tag, i), txt,
                              "%s 的第 %d 条在并发轮转中丢了" % (tag, i))


# ---------------------------------------------------------------------------
# P1-05: 集成模型整批换位(meta 当指针, 杜绝"看起来完整其实是混合"的批次)
# ---------------------------------------------------------------------------

class _FakeModel:
    """最小模型替身: 只实现换批用到的 save_model, 内容=标签便于事后核对批次。"""

    def __init__(self, tag, boom=False):
        self.tag = tag
        self.boom = boom

    def save_model(self, path):
        Path(path).write_text(self.tag, encoding="utf-8")
        if self.boom:
            raise KeyboardInterrupt("模拟写 staging 中途被强杀")


class _OsShim:
    """只拦 os.replace, 其余直通真实 os(不污染全局 os 模块)。"""

    def __init__(self, ok_calls):
        self.left = ok_calls

    def __getattr__(self, k):
        return getattr(os, k)

    def replace(self, a, b):
        if self.left <= 0:
            raise KeyboardInterrupt("模拟换批 replace 中途被强杀")
        self.left -= 1
        return os.replace(a, b)


class TestEnsembleBatchSwap(unittest.TestCase):
    """P1-05: 换批只允许「旧整批 / pending / 新整批」三种可观察状态。

    原实现逐个种子覆盖 lgb_latest_s*.txt, meta 在最后才写 —— 中途崩掉会留下
    「2 新 + 2 旧」的混合集成, 而 _model_files() 只核对存在性, 照样拿去推分。
    """

    def setUp(self):
        from app.models import trainer
        self.trainer = trainer
        from app.models import daily_score
        self.ds = daily_score
        self.tmp = tempfile.TemporaryDirectory()
        self.edir = Path(self.tmp.name) / "ens"
        self.edir.mkdir()
        self.old_ens = self.ds.ENS_DIR
        self.ds.ENS_DIR = self.edir          # _model_files 只看 ENS_DIR
        self.old_single = self.ds.MODEL_FILE

    def tearDown(self):
        self.ds.ENS_DIR = self.old_ens
        self.ds.MODEL_FILE = self.old_single
        self.tmp.cleanup()

    def _install_old_batch(self, seeds=(7, 42, 123, 2024)):
        for s in seeds:
            (self.edir / f"lgb_latest_s{s}.txt").write_text(f"OLD-{s}", encoding="utf-8")
        (self.edir / "lgb_latest_meta.txt").write_text(
            ",".join(str(s) for s in seeds) + "\nready", encoding="utf-8")

    def _meta(self):
        return (self.edir / "lgb_latest_meta.txt").read_text(encoding="utf-8")

    def _contents(self):
        return {p.name: p.read_text(encoding="utf-8")
                for p in sorted(self.edir.glob("lgb_latest_s*.txt"))}

    def _staging(self):
        return [p.name for p in self.edir.glob(".staging_*")]

    def test_happy_path_installs_whole_new_batch(self):
        self._install_old_batch()
        batch = [(7, _FakeModel("NEW-7")), (42, _FakeModel("NEW-42")),
                 (123, _FakeModel("NEW-123")), (2024, _FakeModel("NEW-2024"))]
        self.trainer._swap_latest_ensemble(self.edir, batch)
        self.assertEqual(self._contents(), {
            "lgb_latest_s7.txt": "NEW-7", "lgb_latest_s42.txt": "NEW-42",
            "lgb_latest_s123.txt": "NEW-123", "lgb_latest_s2024.txt": "NEW-2024"})
        self.assertTrue(self._meta().endswith("\nready"), self._meta())
        self.assertEqual(self._staging(), [], "staging 目录没清干净")
        # 消费者看到的是完整新批
        self.assertEqual([p.name for p in self.ds._model_files()],
                         ["lgb_latest_s7.txt", "lgb_latest_s42.txt",
                          "lgb_latest_s123.txt", "lgb_latest_s2024.txt"])

    def test_stale_orphan_seed_file_is_removed(self):
        # 换种子重训: 上一代的 s999 不再被 meta 引用, 留着会污染 glob 与人工排查
        self._install_old_batch()
        (self.edir / "lgb_latest_s999.txt").write_text("STALE", encoding="utf-8")
        self.trainer._swap_latest_ensemble(self.edir, [(7, _FakeModel("NEW-7")),
                                                      (42, _FakeModel("NEW-42"))])
        self.assertFalse((self.edir / "lgb_latest_s999.txt").exists())

    def test_crash_before_pending_keeps_old_batch_intact(self):
        """staging 阶段就崩 ⇒ meta 还没动, 盘上仍是旧整批(消费者无感)。"""
        self._install_old_batch()
        before = self._contents()
        with self.assertRaises(KeyboardInterrupt):
            self.trainer._swap_latest_ensemble(
                self.edir, [(7, _FakeModel("NEW-7", boom=True)), (42, _FakeModel("NEW-42"))])
        self.assertEqual(self._contents(), before, "崩在 staging 却改动了生产文件")
        self.assertTrue(self._meta().endswith("\nready"))
        self.assertEqual(len(self.ds._model_files()), 4, "旧批仍应被当作完整集成")

    def test_crash_mid_replace_is_pending_and_consumer_falls_back(self):
        """replace 中途崩 ⇒ meta=pending, 盘上是混合批 —— 消费者必须回退单模型。

        这是本条修复的核心: **混合批在盘上真实存在**, 只是靠 meta 指针让消费者看不见它。
        """
        self._install_old_batch()
        with mock.patch.object(self.trainer, "os", _OsShim(ok_calls=1)):
            with self.assertRaises(KeyboardInterrupt):
                self.trainer._swap_latest_ensemble(
                    self.edir, [(7, _FakeModel("NEW-7")), (42, _FakeModel("NEW-42"))])
        got = self._contents()
        self.assertEqual(got["lgb_latest_s7.txt"], "NEW-7")
        self.assertEqual(got["lgb_latest_s42.txt"], "OLD-42")
        self.assertIn("pending", self._meta().splitlines()[1])
        self.ds.MODEL_FILE = self.edir / "lgb_latest.txt"      # 单模型回退目标(不必真存在)
        with self.assertLogs("app.models.daily_score", level="ERROR") as cm:
            files = self.ds._model_files()
        self.assertEqual(files, [self.ds.MODEL_FILE], "pending 期间不得使用混合集成")
        self.assertIn("pending", "".join(cm.output))

    def test_pending_meta_wins_even_when_all_files_present(self):
        """四个文件都在、看起来"完整", 但 meta=pending ⇒ 仍回退(指针优先于存在性)。"""
        self._install_old_batch()
        (self.edir / "lgb_latest_meta.txt").write_text("7,42,123,2024\npending", encoding="utf-8")
        self.ds.MODEL_FILE = self.edir / "lgb_latest.txt"
        self.assertEqual(self.ds._model_files(), [self.ds.MODEL_FILE])

    def test_pending_written_before_any_replace(self):
        """顺序不变量: 第一次 replace 之前 meta 必须已经是 pending。"""
        self._install_old_batch()
        seen = []
        shim = _OsShim(ok_calls=99)
        real_replace = os.replace

        def _spy(a, b):
            seen.append(self._meta())
            return real_replace(a, b)
        with mock.patch.object(self.trainer, "os", shim):
            with mock.patch.object(shim, "replace", side_effect=_spy):
                self.trainer._swap_latest_ensemble(
                    self.edir, [(7, _FakeModel("NEW-7")), (42, _FakeModel("NEW-42"))])
        self.assertTrue(seen, "一次 replace 都没发生, 测试空转")
        self.assertTrue(all("pending" in s for s in seen),
                        "有 replace 发生在 meta 标记 pending 之前 ⇒ 存在混合批暴露窗口")


# ---------------------------------------------------------------------------
# P2-09: 增量训练前的因子集指纹核对(防同一张 score 表混入两代特征)
# ---------------------------------------------------------------------------

class TestFactorGenerationGuard(unittest.TestCase):
    """P2-09: 加减因子后若走增量, 历史月是旧口径的分、新月是新口径, 同表跨口径且无标记。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._db, db.DB_PATH = db.DB_PATH, Path(self.tmp.name) / "t.db"
        db.init_db()
        from app.models import trainer
        self.trainer = trainer

    def tearDown(self):
        db.DB_PATH = self._db
        self.tmp.cleanup()

    def _fp(self, table="model_score"):
        with db.conn_ctx() as conn:
            return db.get_meta(conn, f"factor_fp:{table}")

    def _gen(self, names, full=False, table="model_score"):
        """照 `trainer.run()` 的两步式契约走一遍(A5)：先**校验**，训练跑通后**登记**。
        测试里"训练"是假的，所以校验一过就立刻登记 —— 与生产顺序等价。"""
        if self.trainer._check_factor_generation(table, names, full):
            self.trainer._record_factor_generation(table, names)

    def test_first_run_bootstraps_with_warning(self):
        with self.assertLogs("app.models.trainer", level="WARNING") as cm:
            pending = self.trainer._check_factor_generation("model_score", ["a", "b"],
                                                            full=False)
        self.assertTrue(pending, "首次运行必须登记(校验返回 True 让调用方在跑通后落指纹)")
        self.assertIsNone(self._fp(), "A5: 校验阶段不许先把指纹落下去")
        self._gen(["a", "b"])
        self.assertIsNotNone(self._fp(), "首次运行必须落指纹, 否则后续无从核对")
        self.assertIn("首次", "".join(cm.output))

    def test_same_set_is_silent(self):
        self._gen(["a", "b"])
        self._gen(["a", "b"])                          # 不抛
        # 2026-09-13 强化(独立审查 §4.2)：原来只验"不抛" ⇒ 被守对象改坏了也全绿。
        # 现在验**副作用**：落盘的指纹必须就是这份因子集的指纹。
        self.assertEqual(self._fp(), self.trainer._factor_fingerprint(["a", "b"]))

    def test_order_does_not_matter(self):
        # 因子集是集合: 顺序变化不该产生假失配(否则任何重排都逼人全量重训)
        self._gen(["a", "b", "c"])
        fp1 = self._fp()
        self._gen(["c", "a", "b"])
        self.assertEqual(self._fp(), fp1, "顺序变化不该改动已落盘指纹")
        self.assertEqual(fp1, self.trainer._factor_fingerprint(["a", "b", "c"]))

    def test_added_factor_refuses_incremental_and_names_it(self):
        self._gen(["a", "b"])
        with self.assertRaises(RuntimeError) as cm:
            self.trainer._check_factor_generation("model_score", ["a", "b", "new_f"],
                                                  full=False)
        msg = str(cm.exception)
        self.assertIn("new_f", msg, "错误信息必须点名新增因子, 否则当班无法处置")
        self.assertIn("full", msg)

    def test_removed_factor_refuses_incremental(self):
        self._gen(["a", "b", "gone"])
        with self.assertRaises(RuntimeError) as cm:
            self.trainer._check_factor_generation("model_score", ["a", "b"], full=False)
        self.assertIn("gone", str(cm.exception))

    def test_full_true_refreshes_fingerprint(self):
        self._gen(["a", "b"])
        fp_old = self._fp()
        self._gen(["a", "b", "c"], full=True)          # 全量重训跑通 → 登记
        self._gen(["a", "b", "c"])                     # 已刷新
        # 2026-09-13 强化：全量重训必须**真的**把指纹换掉，否则"已刷新"这句是空口
        fp_new = self._fp()
        self.assertNotEqual(fp_new, fp_old, "full=True 没有刷新指纹")
        self.assertEqual(fp_new, self.trainer._factor_fingerprint(["a", "b", "c"]))

    def test_crashed_full_retrain_leaves_guard_armed(self):
        """A5 回归：全量重训**中途**崩掉时指纹必须仍是旧代 —— 否则下一次增量静默混代。

        这正是 P2-09 这个守卫存在的全部意义：原实现在开跑前就 set_meta，训练死在第 40 期
        时守卫已"对齐"，增量路径不再拦，同表混两代且无标记。
        """
        self._gen(["a", "b"])
        fp_old = self._fp()
        pending = self.trainer._check_factor_generation("model_score", ["a", "b", "c"],
                                                        full=True)
        self.assertTrue(pending, "全量重训需要跑通后登记")
        # —— 此处模拟"训练在登记之前抛了"（生产里是 OOM/强杀/断电，登记那段根本不执行）
        self.assertEqual(self._fp(), fp_old, "训练没跑通就把指纹刷新了 ⇒ 守卫被自己骗开")
        with self.assertRaises(RuntimeError) as cm:
            self.trainer._check_factor_generation("model_score", ["a", "b", "c"], full=False)
        self.assertIn("c", str(cm.exception), "失配必须仍被拦住，并点名那个新因子")

    def test_score_tables_are_independent(self):
        # model_score 与 daily_score 各训各的口径, 指纹不得互相污染
        self._gen(["a"], table="model_score")
        self._gen(["a", "b"], table="daily_score")            # 不抛
        # 2026-09-13 强化：这条测试的名字就是"互不污染" ⇒ 必须断言两张表各自的指纹
        self.assertEqual(self._fp("model_score"), self.trainer._factor_fingerprint(["a"]))
        self.assertEqual(self._fp("daily_score"), self.trainer._factor_fingerprint(["a", "b"]))
        self.assertNotEqual(self._fp("model_score"), self._fp("daily_score"))


# ---------------------------------------------------------------------------
# P1-04: 日频推分 fail-closed(缺数据/缺因子一律拒绝推分, 不再静默换口径)
# ---------------------------------------------------------------------------

_DS_SCHEMA_0912 = """
CREATE TABLE stock_basic (ts_code TEXT PRIMARY KEY, symbol TEXT, name TEXT, area TEXT,
    industry TEXT, market TEXT, list_date TEXT, delist_date TEXT);
CREATE TABLE daily (ts_code TEXT, trade_date TEXT, open REAL, high REAL, low REAL,
    close REAL, pre_close REAL, change REAL, pct_chg REAL, vol REAL, amount REAL);
CREATE TABLE factor (ts_code TEXT, trade_date TEXT, factor TEXT, value REAL,
    PRIMARY KEY (ts_code, trade_date, factor)) WITHOUT ROWID;
CREATE TABLE daily_score (ts_code TEXT, trade_date TEXT, score REAL, pred_ret REAL,
    PRIMARY KEY (ts_code, trade_date)) WITHOUT ROWID;
CREATE TABLE daily_basic (ts_code TEXT, trade_date TEXT, total_mv REAL, circ_mv REAL);
"""


class TestDailyScoreFailClosed(unittest.TestCase):
    """P1-04: 推分截面宁可当天不出分(可见、可处置), 也不静默换一种口径选股。"""

    T = "20260904"
    CODES = ["600000.SH", "600001.SH"]

    def setUp(self):
        from app.models import daily_score
        self.ds = daily_score
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.path = self.tmp.name
        self.tmp.close()
        self.patch_db = mock.patch.object(
            daily_score.db, "get_conn", side_effect=lambda: sqlite3.connect(self.path))
        self.patch_db.start()
        # 两因子模型: mom_20 + rev_5(字母序 ≠ 计算序, 顺带覆盖 P1-04 的列对齐路径)
        import lightgbm as lgb
        self.model = lgb.train(
            {"objective": "regression", "verbose": -1},
            lgb.Dataset(pd.DataFrame({"mom_20": [1.0, 2.0, 3.0], "rev_5": [3.0, 2.0, 1.0]}),
                        pd.Series([1.0, 2.0, 3.0])), num_boost_round=5)
        self.model_path = Path(self.path).with_suffix(".model")
        self.model.save_model(str(self.model_path))

    def tearDown(self):
        self.patch_db.stop()
        os.unlink(self.path)
        os.unlink(self.model_path)

    def _build(self, factors=("mom_20", "rev_5"), with_mv=True, amount=1000.0):
        conn = sqlite3.connect(self.path)
        conn.executescript(_DS_SCHEMA_0912)
        for c in self.CODES:
            conn.execute("INSERT INTO stock_basic (ts_code, name, list_date) VALUES (?,?,?)",
                         (c, c, "19910403"))
            conn.execute("INSERT INTO daily (ts_code, trade_date, close, pre_close, vol,"
                         " pct_chg, amount) VALUES (?,?,10.0,10.0,1000,0.5,?)",
                         (c, self.T, amount))
            if with_mv:
                conn.execute("INSERT INTO daily_basic (ts_code, trade_date, total_mv) "
                             "VALUES (?,?,100000.0)", (c, self.T))
            for f in factors:
                conn.execute("INSERT INTO factor (ts_code, trade_date, factor, value) "
                             "VALUES (?,?,?,1.0)", (c, self.T, f))
        conn.commit()
        conn.close()

    def _score(self):
        with mock.patch.object(self.ds, "_model_files", lambda: [self.model_path]):
            return self.ds.score_today(self.T)

    def test_control_all_data_present_scores_normally(self):
        self._build()
        self.assertEqual(self._score()["n"], len(self.CODES))   # 佐证: 夹具本身能正常推分

    def test_missing_factor_column_refuses_and_names_it(self):
        """旧路径对缺列 reindex → 全 NaN → LightGBM 照常吐近似常数分, 全程无报错。"""
        self._build(factors=("mom_20",))          # rev_5 缺失
        # 佐证: 全 NaN 列不会让 predict 报错(这就是静默失真的机制)
        xm = pd.DataFrame({"mom_20": [1.0, 2.0]}).reindex(columns=["mom_20", "rev_5"])
        self.assertTrue(np.isnan(xm["rev_5"]).all())
        self.assertEqual(len(self.model.predict(xm)), 2, "全 NaN 列本该照常出分(故必须显式拦)")
        with self.assertRaises(RuntimeError) as cm:
            self._score()
        self.assertIn("rev_5", str(cm.exception))
        self.assertNotIn("daily_score", self._stored(), "拒绝推分后不得留下半截结果")

    def test_missing_daily_basic_refuses(self):
        self._build(with_mv=False)
        with self.assertRaises(RuntimeError) as cm:
            self._score()
        self.assertIn("市值", str(cm.exception))

    def test_missing_amount_refuses(self):
        """daily_basic 有行、成交额全 NULL ⇒ 结果集非空但 amt20 全 NaN。

        只看 `df.empty` 会漏掉这一形态: NaN >= x 恒为 False ⇒ 过滤静默淘汰整个截面,
        最后死在 LightGBM 的 "Input data must be 2 dimensional and non empty" 里,
        报错完全看不出是数据问题(核实修复时实测踩到, 故补 `_all_missing`)。
        """
        self._build(amount=None)
        with self.assertRaises(RuntimeError) as cm:
            self._score()
        self.assertIn("市值", str(cm.exception))
        self.assertNotIn("non empty", str(cm.exception), "不该再漏到 LightGBM 内部报错")

    def test_filtered_to_empty_cross_section_refuses(self):
        """数据全有、但过滤口径把整个截面剔光 ⇒ 显式报"截面为空", 不是 LightGBM 的
        "Input data must be 2 dimensional and non empty"。"""
        self._build()
        with mock.patch("app.config.load_config",
                        return_value={"stock_min_amt20": 1e9}):
            with self.assertRaises(RuntimeError) as cm:
                self._score()
        msg = str(cm.exception)
        self.assertIn("截面为空", msg)
        self.assertIn("过滤前", msg)

    def _stored(self):
        conn = sqlite3.connect(self.path)
        rows = conn.execute("SELECT ts_code FROM daily_score").fetchall()
        conn.close()
        return rows


# ---------------------------------------------------------------------------
# P2-21: 日频因子的中性化按因子整块算(逐日等价) + recent 空守卫
# ---------------------------------------------------------------------------

class TestNeutralizeStaysPerDay(unittest.TestCase):
    """P2-21 的**否证锁**: 日频中性化必须保持逐日调用, 不得改成「整块算一次」。

    审查建议把 30×37 次 neutralize 合并成 37 次。试过, 不能保证逐位相同:
    行数从 1 改成 6 就会让同一截面平移 ~1e-8(3000 列纯随机面板 6/6 行不同;
    含一个 NaN 或 inf 格则 5/6 行不同), 机制是 `clip` 对多行帧上转 float64 造成的
    精度漂移 —— 详见 factor_engine.neutralize 的 docstring 实测记录。
    生产面板当前两法恰好逐位相同, 但那是数据形状碰巧, 不是不变量。

    这条测试锁的是**决定**而非代码风格: 谁再想做这个"顺手优化",
    会先红在这里, 并被指向 docstring 里的反例(而不是靠 review 记忆)。
    """

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.path = self.tmp.name
        self.tmp.close()
        conn = sqlite3.connect(self.path)
        conn.executescript(db.SCHEMA)
        conn.execute("INSERT INTO daily (ts_code, trade_date, close) VALUES ('000001.SZ','20260911',10.0)")
        # 两只股票必须同行业: 单票行业经行业去均值后整行为 0, z-score 除零 → 整行 NaN → 被 dropna 掉
        conn.execute("INSERT INTO stock_basic (ts_code, name, industry) VALUES ('000001.SZ','平安银行','银行')")
        conn.execute("INSERT INTO stock_basic (ts_code, name, industry) VALUES ('600000.SH','浦发银行','银行')")
        conn.commit()
        conn.close()
        from app.factors import daily_engine
        self.de = daily_engine
        self.patch_db = mock.patch.object(
            daily_engine.db, "get_conn", side_effect=lambda: sqlite3.connect(self.path))
        self.patch_db.start()

    def tearDown(self):
        self.patch_db.stop()
        os.unlink(self.path)

    def test_one_neutralize_call_per_day_per_factor(self):
        idx = pd.DatetimeIndex(["2026-09-09", "2026-09-10", "2026-09-11"])
        cols = ["000001.SZ", "600000.SH"]
        m = {"close": pd.DataFrame(1.0, index=idx, columns=cols),
             "amount": pd.DataFrame(1.0, index=idx, columns=cols),
             "circ_mv": pd.DataFrame(1.0, index=idx, columns=cols)}
        # 值必须有区分度: 常数截面经 neutralize 的 z-score 除零保护后整行 NaN, 会被 dropna 掉
        vals = np.arange(1.0, 1.0 + len(idx) * len(cols)).reshape(len(idx), len(cols))
        panels = {"mom_20": pd.DataFrame(vals, index=idx, columns=cols),
                  "vol_20": pd.DataFrame(vals * 2.0, index=idx, columns=cols)}
        calls = []
        real = self.de.neutralize

        def _spy(df, ind, *a, **kw):
            calls.append(df.shape)
            return real(df, ind, *a, **kw)
        with mock.patch.object(self.de, "load_market", return_value=m), \
             mock.patch.object(self.de, "compute_price_factors", return_value=panels), \
             mock.patch.object(self.de, "compute_lhb_factors", return_value={}), \
             mock.patch.object(self.de, "neutralize", side_effect=_spy):
            n = self.de.compute_daily("20260911", keep_days=3)
        self.assertGreater(n, 0)
        self.assertTrue(calls, "一次中性化都没调用, 测试空转")
        self.assertEqual(len(calls), 3 * 2, f"应逐日逐因子调用 3×2 次, 实际 {len(calls)}")
        self.assertTrue(all(shape[0] == 1 for shape in calls),
                        f"有调用是多行整块(shape={[s for s in calls if s[0] != 1]}) —— "
                        f"那正是 P2-21 试过、因不逐位相同而放弃的做法")


class TestDailyEngineEmptyWindowGuard(unittest.TestCase):
    """P2-21 边界: end_date 落在加载窗口之前 ⇒ recent 为空, 原 recent[0] 直接 IndexError。"""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.path = self.tmp.name
        self.tmp.close()
        conn = sqlite3.connect(self.path)
        conn.executescript(db.SCHEMA)
        conn.execute("INSERT INTO daily (ts_code, trade_date, close) VALUES ('000001.SZ','20260911',10.0)")
        conn.execute("INSERT INTO stock_basic (ts_code, name, industry) VALUES ('000001.SZ','平安银行','银行')")
        conn.commit()
        conn.close()
        from app.factors import daily_engine
        self.de = daily_engine
        self.patch_db = mock.patch.object(
            daily_engine.db, "get_conn", side_effect=lambda: sqlite3.connect(self.path))
        self.patch_db.start()

    def tearDown(self):
        self.patch_db.stop()
        os.unlink(self.path)

    def _market_after(self, end_date):
        # 加载窗口内的交易日**全部晚于** end_date ⇒ recent 为空(补跑一个早于已入库数据的日期)
        idx = pd.DatetimeIndex(["2026-09-01", "2026-09-02", "2026-09-11"])
        return {"close": pd.DataFrame(1.0, index=idx, columns=["000001.SZ"]),
                "amount": pd.DataFrame(1.0, index=idx, columns=["000001.SZ"]),
                "circ_mv": pd.DataFrame(1.0, index=idx, columns=["000001.SZ"])}

    def test_end_date_before_loaded_window_returns_zero(self):
        with mock.patch.object(self.de, "load_market", return_value=self._market_after("20260101")):
            with self.assertLogs("app.factors.daily_engine", level="WARNING") as cm:
                n = self.de.compute_daily("20260101")
        self.assertEqual(n, 0)
        # 走到的是"窗口内没有 <= end_date 的交易日"这条分支(而不是更早的"行情为空"分支)
        self.assertIn("没有 <=", "".join(cm.output))

    def test_precondition_of_the_bug(self):
        """佐证: 该场景下 recent 确实为空 —— 没有守卫就是 recent[0] IndexError。"""
        m = self._market_after("20260101")
        dates = pd.to_datetime(m["close"].index)
        self.assertEqual(len(dates[dates <= pd.Timestamp("20260101")]), 0)


# ---------------------------------------------------------------------------
# P2-20: 限速门是账号级的(跨实例/跨线程), 不是每个 client 各算各的
# ---------------------------------------------------------------------------

class TestTushareRateGateIsAccountWide(unittest.TestCase):
    """P2-20: 原 `self._last` 是实例级 ⇒ 一个进程里多个 client(updater/regime)并发时,
    实际频率是 实例数×187 次/分, 一起撞账号 200/分 的墙。

    这条测试**不用 mock 时间** —— 真起线程、真等, 因为要证的正是"两个实例之间的等待
    互相可见"。设 min_interval 很小(0.12s)把测试压到秒级, 断言的是**间隔性质**而非墙钟值。
    """

    def setUp(self):
        from app.datasource import tushare_client as tc
        self.tc = tc
        self._lock, self._last = tc.TushareClient._rate_lock, tc.TushareClient._last_call
        tc.TushareClient._last_call = 0.0

    def tearDown(self):
        self.tc.TushareClient._last_call = self._last

    def _client(self):
        c = self.tc.TushareClient.__new__(self.tc.TushareClient)   # 跳过 __init__(要 token)
        c.min_interval = 0.12
        return c

    def test_two_instances_share_the_gate(self):
        a, b = self._client(), self._client()
        self.tc.TushareClient._last_call = time.time()
        t0 = time.time()
        a._wait()
        b._wait()
        # 第二个实例必须等够一个间隔 —— 实例级计时的话它几乎立刻返回(差 <0.05s)
        self.assertGreaterEqual(time.time() - t0, 0.12,
                                "第二个 client 实例没有排队 ⇒ 限速门还是实例级的")
        # 且它预留了下一个时隙(不是"各算各的")
        self.assertGreaterEqual(self.tc.TushareClient._last_call, t0 + 0.12)

    def test_concurrent_threads_do_not_bunch_up(self):
        """8 个线程同时抢 → 两两间隔都要 >= min_interval(原实现会让它们几乎同时发出)。"""
        clients = [self._client() for _ in range(8)]
        stamps = []
        lock = threading.Lock()

        def _fire(c):
            c._wait()
            with lock:
                stamps.append(time.time())
        threads = [threading.Thread(target=_fire, args=(c,)) for c in clients]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        stamps.sort()
        self.assertEqual(len(stamps), 8)
        gaps = [b - a for a, b in zip(stamps, stamps[1:])]
        self.assertGreaterEqual(min(gaps), 0.115,
                                f"有请求挤在一起(最小间隔 {min(gaps):.3f}s) —— 限速失效")


# ---------------------------------------------------------------------------
# P2-18: 运行记录只有一个钟(东八区)
# ---------------------------------------------------------------------------

class TestSinglePipelineClock(unittest.TestCase):
    """P2-18: 原 `started_at` 走 SQLite `datetime('now','localtime')`(机器时区), 而
    `run_date` 走 Asia/Shanghai —— 非东八区部署下两者跨日错位, 而"最近 N 天失败重跑"
    的窗口判定读的正是 started_at。

    本机两者恰好一致(东八区), 所以这些用例**注入假时钟**来验证"同源"这个结构性事实,
    而不是靠机器时区碰巧对上。
    """

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.path = self.tmp.name
        self.tmp.close()
        conn = sqlite3.connect(self.path)
        conn.executescript(db.SCHEMA)
        conn.commit()
        conn.close()

    def tearDown(self):
        os.unlink(self.path)

    def _conn(self):
        return sqlite3.connect(self.path)

    def test_stage_start_uses_injected_clock(self):
        from app import timeutil
        conn = self._conn()
        with mock.patch.object(timeutil, "now", return_value=datetime.datetime(2026, 3, 1, 23, 30, 5)):
            db.stage_start(conn, "daily", "20260302")
        started = conn.execute("SELECT started_at, run_date FROM pipeline_run").fetchone()
        conn.close()
        self.assertEqual(started[0], "2026-03-01 23:30:05",
                         "started_at 没走 timeutil —— 还是 SQLite 的机器本地时钟")
        self.assertEqual(started[1], "20260302")

    def test_stage_finish_and_set_status_same_clock(self):
        from app import timeutil
        conn = self._conn()
        with mock.patch.object(timeutil, "now", return_value=datetime.datetime(2026, 3, 1, 9, 0, 0)):
            sid = db.stage_start(conn, "daily", "20260301")
            db.stage_finish(conn, sid, "ok", rows=1)
            db.set_status(conn, "daily", "20260301", 1)
        ended = conn.execute("SELECT ended_at FROM pipeline_run").fetchone()[0]
        upd = conn.execute("SELECT updated_at FROM data_status").fetchone()[0]
        conn.close()
        self.assertEqual(ended, "2026-03-01 09:00:00")
        self.assertEqual(upd, "2026-03-01 09:00:00")

    def test_notify_log_same_clock(self):
        from app import notify, timeutil
        # conn_ctx 会关闭连接(那正是 P2-12 修复点), 所以每次给一条新连接
        with mock.patch.object(timeutil, "now", return_value=datetime.datetime(2026, 3, 1, 9, 0, 0)), \
             mock.patch.object(notify.db, "get_conn",
                               side_effect=lambda: sqlite3.connect(self.path)):
            notify._log_send("test", True)
        conn = self._conn()
        created = conn.execute("SELECT created_at FROM notify_log").fetchone()[0]
        conn.close()
        self.assertEqual(created, "2026-03-01 09:00:00")

    def test_run_date_matches_timeutil(self):
        from app import timeutil
        with mock.patch.object(timeutil, "now", return_value=datetime.datetime(2026, 3, 1, 23, 59, 0)):
            self.assertEqual(scheduler._run_date(), "20260301")
        with mock.patch.object(timeutil, "now", return_value=datetime.datetime(2026, 3, 2, 0, 1, 0)):
            self.assertEqual(scheduler._run_date(), "20260302")

    def test_retry_window_uses_the_same_clock_as_the_writer(self):
        """写入侧(started_at)与读取侧(窗口下界)必须同源: 4 天前的 error 不算失败,
        2 天前的算 —— 用注入时钟写、用注入时钟判, 全在 timeutil 的钟上。"""
        from app import timeutil
        conn = self._conn()
        now = datetime.datetime(2026, 3, 10, 12, 0, 0)
        with mock.patch.object(timeutil, "now", return_value=now - datetime.timedelta(days=4)):
            db.stage_start(conn, "old_stage", "20260306")
            db.stage_finish(conn, 1, "error")
        with mock.patch.object(timeutil, "now", return_value=now - datetime.timedelta(days=2)):
            db.stage_start(conn, "new_stage", "20260308")
            db.stage_finish(conn, 2, "error")
        with mock.patch.object(timeutil, "now", return_value=now):
            self.assertFalse(scheduler._stage_failed(conn, ["old_stage"]),
                             "4 天前的失败被算进 3 天窗口 ⇒ 读侧没跟写入侧同一个钟")
            self.assertTrue(scheduler._stage_failed(conn, ["new_stage"]))
        conn.close()

    def test_timeutil_stamp_format_matches_sqlite(self):
        """格式必须与 SQLite datetime() 逐字符一致, 否则历史行与新行比不了大小。"""
        import sqlite3 as _sq
        conn = self._conn()
        sqlite_now = conn.execute("SELECT datetime('now','localtime')").fetchone()[0]
        conn.close()
        self.assertRegex(sqlite_now, r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
        from app import timeutil
        self.assertRegex(timeutil.stamp(), r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
        self.assertEqual(len(timeutil.stamp()), len(sqlite_now))


if __name__ == "__main__":
    unittest.main()
