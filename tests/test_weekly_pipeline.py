"""周频袖套调度入口的守卫测试(不触库、不发网络)

守四条:
  1. **默认关闭**: weekly.enabled 缺省/false 时**在读日历之前**就返回(零副作用);
  2. **触发规则**: 新一周的第一个开市日才执行(周五→周一 ✓; 周一→周二 ✗; 节假日周顺延 ✓);
  3. **幂等**: 同一成交日重复运行不再下单;
  4. **异常不外抛**: 出方案失败只返回 error 字段(不影响月频链)。
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from app.weekly import pipeline as wp   # noqa: E402
from app.weekly.ledger import Ledger    # noqa: E402


class TestTriggerRule(unittest.TestCase):
    def test_friday_to_monday_fires(self):
        self.assertTrue(wp.is_first_session_after_anchor("20260904", "20260907"))

    def test_monday_to_tuesday_does_not_fire(self):
        self.assertFalse(wp.is_first_session_after_anchor("20260907", "20260908"))

    def test_holiday_week_uses_last_open_day(self):
        # 国庆周: 周一为本周唯一开市日 → 下周一必须触发
        self.assertTrue(wp.is_first_session_after_anchor("20260928", "20261005"))
        # 同周内不触发
        self.assertFalse(wp.is_first_session_after_anchor("20260928", "20260929"))

    def test_year_boundary(self):
        # 2026-12-31(周四) → 2027-01-04(周一): 跨年但仍是新周
        self.assertTrue(wp.is_first_session_after_anchor("20261231", "20270104"))


class _FakeConn:
    """只支持 pipeline 用到的两种查询: trade_cal 的后续开市日 / daily 的最近交易日。"""

    def __init__(self, cal, traded):
        self.cal = sorted(cal)          # 日历(可含未来)
        self.traded = sorted(traded)    # 已有行情的交易日

    def execute(self, sql, params=()):
        if "trade_cal" in sql:
            d = params[0]
            return [(x,) for x in self.cal if x > d][:10]
        d = params[0]
        return [(x,) for x in sorted([t for t in self.traded if t <= d], reverse=True)][:30]


class TestScoreFreshness(unittest.TestCase):
    """打分保鲜档: 锚点判据决定"要不要重训", 判错会让终端静默不交易。"""

    def setUp(self):
        # 2026-09-07(一) ~ 09-11(五) 开市; 下一周 09-14(一) ~ 09-18(五)
        self.cal = ["20260907", "20260908", "20260909", "20260910", "20260911",
                    "20260914", "20260915", "20260916", "20260917", "20260918"]
        self.conn = _FakeConn(self.cal, self.cal[:5])

    def test_friday_is_week_last_but_thursday_is_not(self):
        self.assertTrue(wp.is_week_last_open(self.conn, "20260911"))
        self.assertFalse(wp.is_week_last_open(self.conn, "20260910"))

    def test_holiday_week_uses_last_open_day(self):
        # 国庆周: 只有 09-28(一) 开市 → 它是该周最后开市日(锚点)
        cal = ["20260924", "20260925", "20260928", "20261005"]
        conn = _FakeConn(cal, cal)
        self.assertTrue(wp.is_week_last_open(conn, "20260928"))
        self.assertTrue(wp.is_week_last_open(conn, "20260925"))

    def test_newest_anchor_from_saturday_and_monday_both_give_friday(self):
        self.assertEqual(wp.newest_anchor(self.conn, "20260912"), "20260911")
        self.assertEqual(wp.newest_anchor(self.conn, "20260914"), "20260911")

    def test_disabled_skips_without_touching_calendar(self):
        out = wp.run_score_slot("20260912", cfg={"weekly": {"enabled": False}},
                                conn=_FakeConn([], []))
        self.assertIn("skipped", out)

    def test_skips_when_archive_already_covers_anchor(self):
        from app.weekly import scores as wsc
        import numpy as np
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            old = wsc.DATA_DIR
            wsc.DATA_DIR = Path(td)
            try:
                np.savez_compressed(Path(td) / "scores_20260911.npz",
                                    scores=np.zeros((1, 1), dtype="float32"),
                                    anchors=np.array(["20260911"]),
                                    cols=np.array(["600000.SH"], dtype=object),
                                    names=np.array(["f"], dtype=object))
                out = wp.run_score_slot("20260912", cfg={"weekly": {"enabled": True}},
                                        conn=self.conn)
                self.assertIn("已覆盖", out["skipped"])
            finally:
                wsc.DATA_DIR = old


class TestRunOpenSlot(unittest.TestCase):
    def setUp(self):
        self.path = BASE / "data" / "weekly" / "_test_pipeline_ledger.json"
        if self.path.exists():
            self.path.unlink()
        # 2026-09-14（我今天踩的坑，写下来）：这个类**必须**把 PLAN_DIR 指到临时目录。
        # 原实现直接对着真 `data/weekly/` 读/写：`run_open_slot` 在"锚点 20260904 无方案"
        # 时会现建一份，而 `ensure_plan` 的"不可解析 ⇒ 重建"分支更是**覆写**已存在的历史方案
        # （今天 12:08 一次中间版本缺 `json` import 的运行，就把 `plan_20260904.json`
        # 从 22,831 字节的生产构型覆盖成了 4,561 字节的**测试构型** capital=20000/top6+bonus0.2）。
        self.plan_dir = Path(tempfile.mkdtemp(prefix="wk_pipe_plan_"))
        self._pdir = mock.patch.object(wp.wk_plan, "PLAN_DIR", self.plan_dir)
        self._pdir.start()
        self.addCleanup(self._pdir.stop)
        # 同上：`ensure_plan` 的补训分支要打桩（否则会读生产存档；存档恰好缺锚点时还会真训练）
        self._ptrain = mock.patch.object(wp, "run_score_slot", return_value={"skipped": "stub"})
        self._ptrain.start()
        self.addCleanup(self._ptrain.stop)
        # 2026-09-14：测试库现在与生产库隔离（见 tests/_isolation.py），`ensure_plan` 的
        # "缺方案 ⇒ 现建"分支要走真实 `build` → `load_market` → `PITEngine`，所以给测试库
        # 灌一份合成面板。以前这条靠"生产缓存里恰好有面板"才绿 —— 那既不可复现，也看不见。
        import _isolation
        _isolation.seed_synthetic_market()
        import shutil
        self.addCleanup(lambda: shutil.rmtree(self.plan_dir, ignore_errors=True))

    def tearDown(self):
        if self.path.exists():
            self.path.unlink()

    def test_disabled_is_zero_side_effect(self):
        called = []
        out = wp.run_open_slot("20260907", cfg={"weekly": {"enabled": False}},
                               ledger_path=self.path,
                               open_days_fn=lambda d: called.append(d) or [])
        self.assertIn("skipped", out)
        self.assertEqual(called, [], "关闭时不应读日历")
        self.assertFalse(self.path.exists())

    def test_not_anchor_day_skips(self):
        out = wp.run_open_slot("20260908", cfg={"weekly": {"enabled": True}},
                               ledger_path=self.path,
                               open_days_fn=lambda d: ["20260908", "20260907", "20260904"])
        self.assertIn("同属一个 ISO 周", out["skipped"])

    def test_executes_on_anchor_day_and_is_idempotent(self):
        # 2026-09-14 改造：桩方案写进**被 patch 的临时 PLAN_DIR**，不再读写生产
        # `data/weekly/plan_20260904.json`。旧写法在"生产目录恰好有方案"时绿、在干净
        # checkout（或数据库隔离之后）变红 —— 测试的成败取决于机器上有什么，这本身就是缺陷。
        plan_path = self.plan_dir / "plan_20260904.json"
        plan_path.write_text(json.dumps(
            {"anchor": "20260904", "capital": 20000.0,
             "rows": [{"ts_code": "600000.SH", "shares": 100}]}), encoding="utf-8")
        led = Ledger(20000.0, self.path)
        led.state["trades"] = [{"date": "20260907", "ts_code": "x", "side": "buy",
                                "shares": 1, "price": 1.0, "fee": 0.0, "reason": "stub"}]
        led.save()
        out = wp.run_open_slot("20260907", cfg={"weekly": {"enabled": True}},
                               ledger_path=self.path,
                               open_days_fn=lambda d: ["20260907", "20260904"])
        self.assertIn("幂等", out["skipped"])

    def test_plan_failure_returns_error_not_raise(self):
        # 用一个**确定没有方案文件**的锚点(2025-01-03 周五 → 2025-01-06 周一),
        # 才能走到 build_plan_fn 的失败分支(否则会直接用现成方案, 见上面的幂等用例)
        out = wp.run_open_slot("20250106", cfg={"weekly": {"enabled": True}},
                               ledger_path=self.path,
                               open_days_fn=lambda d: ["20250106", "20250103"],
                               build_plan_fn=lambda **kw: (_ for _ in ()).throw(RuntimeError("无打分存档")))
        self.assertIn("error", out)
        self.assertIn("无打分存档", out["error"])


class TestEnsurePlan(unittest.TestCase):
    """2026-09-14（07 #50）：**唯一的自动建造点**被抽成 `ensure_plan`，三条路径共用。

    原来只有周一 09:32 会建方案 —— 该档一旦失败（服务没起 / 打分存档没覆盖锚点），
    晚间档与开机补跑都只执行、不建造 ⇒ **整周 0 成交**且无法自动恢复。
    这里钉三条：①已有方案 → 不重建；②缺方案 → 现建；③失败原因是"未覆盖锚点" →
    **先补跑一次打分保鲜**再建一次（其余错误不补训，直接如实返回）。
    """

    def setUp(self):
        import tempfile
        self.dir = Path(tempfile.mkdtemp(prefix="wk_ensure_plan_"))
        self._pdir = mock.patch.object(wp.wk_plan, "PLAN_DIR", self.dir)
        self._pdir.start()
        self.addCleanup(self._pdir.stop)
        import shutil
        self.addCleanup(lambda: shutil.rmtree(self.dir, ignore_errors=True))
        self.cfg = {"weekly": {"enabled": True, "capital": 1_000_000}}
        self.anchor = "20260911"
        self.path = self.dir / f"plan_{self.anchor}.json"

    def test_existing_plan_is_used_without_rebuilding(self):
        self.path.write_text(json.dumps({"anchor": self.anchor, "rows": []}), encoding="utf-8")
        called = []
        out = wp.ensure_plan(self.anchor, self.cfg,
                             build_plan_fn=lambda **kw: called.append(kw))
        self.assertTrue(out["ok"])
        self.assertFalse(out["built"])
        self.assertEqual(called, [], "已有方案时不许重建")

    def test_missing_plan_is_built(self):
        def _build(asof=None, capital=None, **_kw):
            self.assertEqual(asof, self.anchor)
            self.path.write_text(json.dumps({"anchor": asof, "rows": []}), encoding="utf-8")
        out = wp.ensure_plan(self.anchor, self.cfg, build_plan_fn=_build)
        self.assertTrue(out["ok"])
        self.assertTrue(out["built"])
        self.assertTrue(self.path.exists())

    def test_uncovered_anchor_triggers_one_retrain_then_rebuild(self):
        """2026-09-18 审查 Q-04：这条 stub 原来 `raise ValueError("锚点 … 不在打分存档里…")`
        —— 用手写文案冒充生产者。判据从**文案**换成**类型**之后，那种写法必然失效；
        这里改抛生产者的**真异常类**，stub 才真的代表 `plan.build` 会抛的东西。
        （不是"改测试凑绿"：契约本身从"这句话"变成"这个类型"了。反向保险见
        `TestArchiveMissIsTyped.test_consumer_no_longer_matches_on_wording`。）
        """
        from app.weekly.errors import ArchiveAnchorNotCovered
        calls = {"n": 0}

        def _build(**kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ArchiveAnchorNotCovered(
                    "锚点 20260911 不在打分存档里(2014-01-03~2026-09-04)")
            self.path.write_text(json.dumps({"anchor": self.anchor, "rows": []}),
                                 encoding="utf-8")
        with mock.patch.object(wp, "run_score_slot",
                               return_value={"retrained": True}) as p_score:
            out = wp.ensure_plan(self.anchor, self.cfg, build_plan_fn=_build)
        self.assertTrue(out["ok"], out)
        self.assertTrue(out["retrained"])
        self.assertEqual(calls["n"], 2, "失败一次 → 补训 → 重建一次")
        p_score.assert_called_once()
        self.assertEqual(p_score.call_args.kwargs.get("today"), self.anchor)

    def test_other_build_error_does_not_retrain(self):
        def _build(**_kw):
            raise RuntimeError("可交易票池只有 12 只")
        with mock.patch.object(wp, "run_score_slot") as p_score:
            out = wp.ensure_plan(self.anchor, self.cfg, build_plan_fn=_build)
        self.assertFalse(out["ok"])
        self.assertIn("可交易票池", out["error"])
        p_score.assert_not_called()


class TestArchiveMissIsTyped(unittest.TestCase):
    """「存档未覆盖」必须是**类型化**通道（2026-09-18 审查 Q-04）

    原来 `ensure_plan` 靠中文子串 `("不在打分存档", "没有周频打分存档")` 认这个情况，
    而两个生产者只是内联 f-string —— 改一个字的措辞，全仓 0 条测试会红，
    代价却是整周不出方案（袖套 0 成交）。这里把"生产者抛什么类型"单独钉死，
    它与 `test_uncovered_anchor_triggers_one_retrain_then_rebuild`（只锁消费端反应）
    是互补的两半：那条换不掉这个盲点，因为它用的是**手写 stub 假消息**。
    """

    ANCHOR = "20260911"

    def setUp(self):
        import shutil
        import tempfile
        self.dir = Path(tempfile.mkdtemp(prefix="wk_typed_"))
        self.addCleanup(lambda: shutil.rmtree(self.dir, ignore_errors=True))
        p = mock.patch.object(wp.wk_plan, "PLAN_DIR", self.dir)
        p.start()
        self.addCleanup(p.stop)
        self.cfg = {"weekly": {"enabled": True}}
        self.path = self.dir / f"plan_{self.ANCHOR}.json"

    def test_scores_latest_raises_archive_miss_when_no_archive(self):
        """生产者①：一份存档都没有 ⇒ 既是 ArchiveMiss，也**仍然是** FileNotFoundError。

        后半条不是洁癖：`pipeline.py:104` 就写死 `except FileNotFoundError` 把"无存档"
        当成"存档为空 ⇒ 去重训"。若新类型只继承 RuntimeError，那行会漏抓、冒到
        :114 的 `except Exception` ⇒ 返回 error 且**不补训** ⇒ 整周空仓（= Q-04 本身）。
        """
        from app.weekly import scores as wsc
        from app.weekly.errors import ArchiveMiss
        with mock.patch.object(wsc, "DATA_DIR", self.dir / "empty"):
            with self.assertRaises(FileNotFoundError) as cm:
                wsc.latest()
        self.assertIsInstance(cm.exception, ArchiveMiss,
                              "无存档必须带上 ArchiveMiss 类型标记（自愈链不许再看文案）")

    def test_plan_build_raises_archive_miss_for_uncovered_anchor(self):
        """生产者②：锚点不在存档里 ⇒ 既是 ArchiveMiss，也**仍然是** ValueError。"""
        import pandas as pd
        from app.weekly import plan as wpn
        from app.weekly.errors import ArchiveMiss
        anchors = pd.DatetimeIndex(["2014-01-03", "2026-09-04"])
        with mock.patch.object(wpn.sc, "latest",
                               return_value=(None, anchors, [], [], Path("x.npz"))):
            with self.assertRaises(ValueError) as cm:
                wpn.build(asof=self.ANCHOR, capital=1_000_000, held=[],
                          cfg={"top_n": 100, "band": 8, "single_side_cost": 0.003},
                          write=False)
        self.assertIsInstance(cm.exception, ArchiveMiss,
                              "锚点未覆盖必须带上 ArchiveMiss 类型标记")

    def test_reworded_message_still_self_heals(self):
        """改措辞不再掐断自愈链 —— 这条就是 Q-04 缺的那道保险。

        生产者把「存档」写成「档案」、或顺手补个句号，`isinstance` 一律照认。
        """
        from app.weekly.errors import ArchiveAnchorNotCovered
        calls = {"n": 0}

        def _build(**_kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ArchiveAnchorNotCovered("锚点未命中评分档案（全新措辞，旧字一个没有）")
            self.path.write_text(json.dumps({"anchor": self.ANCHOR, "rows": []}),
                                 encoding="utf-8")

        with mock.patch.object(wp, "run_score_slot", return_value={"retrained": True}) as ps:
            out = wp.ensure_plan(self.ANCHOR, self.cfg, build_plan_fn=_build)
        self.assertTrue(out.get("ok"), f"改了措辞自愈链就断了: {out}")
        self.assertTrue(out["retrained"])
        self.assertEqual(calls["n"], 2)
        ps.assert_called_once()

    def test_consumer_no_longer_matches_on_wording(self):
        """通道只剩类型这一条：光有旧文案、没有类型标记 ⇒ 不再触发补训。

        （否则等于双通道并存，文案耦合还在，重构照样能踩。）
        """
        def _boom(**_kw):
            raise ValueError("锚点 20260911 不在打分存档里（旧文案，但没有类型标记）")
        with mock.patch.object(wp, "run_score_slot") as p_score:
            out = wp.ensure_plan(self.ANCHOR, self.cfg, build_plan_fn=_boom)
        self.assertFalse(out["ok"])
        p_score.assert_not_called()
        self.assertIn("不在打分存档", out["error"], "错误仍要如实带回给调用方")


if __name__ == "__main__":
    unittest.main()
