"""周频袖套健康监控的守卫测试 —— 阈值必须钉死(否则"监控"会静默放过退化)。

四档阈值(见 app/weekly/health.py docstring):
  🟢 滚动 IC ≥ 0.5×冻结基线 且 换手 ≤ 2×回测 且 投入率 ≥ 0.60
  🟡 IC ∈ (0, 0.5×) / 换手 2~3× / 投入率 0.50~0.60
  🟠 连续 6 锚点 IC < 0 / 换手 > 3×
  🔴 连续 12 锚点 IC < 0 ⇒ 退役候选
"""
import json
import math
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from app.weekly import health  # noqa: E402
from app.weekly import blocks as wk_blocks  # noqa: E402


class TestFirstCycleLedgerReadings(unittest.TestCase):
    """2026-09-14（07 #46）：**有买无卖**的首周期账本 —— 读数不许出现 NaN。

    实拍：首个纸面周期 09:32 建仓 98 笔（全是买）之后，`/api/weekly/health` 一直 **500**：
    `sells.tail(window).mean()` 对**空序列**求均值得到 NaN，而 starlette 用
    `allow_nan=False` 序列化 ⇒ `ValueError`。空序列的语义是"本窗口还没卖出"= 换手 0。
    红检：去掉 `if len(sells)` 守卫 ⇒ 本用例当场红（turnover_est 变 NaN）。
    """

    def _ledger_with(self, trades):
        tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w",
                                          encoding="utf-8")
        json.dump({
            "capital": 1_000_000.0, "cash": 133_967.54,
            "positions": {"600000.SH": {"shares": 100.0, "cost": 10.0, "last_px": 10.0}},
            "nav_history": [
                {"date": "20260914", "nav": 999_501.34, "invested": 865_533.8,
                 "cash": 133_967.54, "interest": 0.0, "positions": {"600000.SH": 100.0}},
                {"date": "20260914", "nav": 999_501.34, "invested": 865_533.8,
                 "cash": 133_967.54, "interest": 0.0, "positions": {"600000.SH": 100.0}},
            ],
            "trades": trades, "inception": "20260914", "last_mark": "20260914",
            "interest": 0.0, "interest_upto": "20260914"}, tmp)
        tmp.close()
        self.addCleanup(lambda: Path(tmp.name).unlink(missing_ok=True))
        return Path(tmp.name)

    def _current(self, path):
        with mock.patch.object(health, "rolling_ic",
                               return_value={"n": 0, "ic": None, "neg_streak": 0,
                                             "path": "stub.npz"}):
            return health.current(window=12, ledger_path=path)

    def test_buys_only_ledger_is_finite_and_json_safe(self):
        p = self._ledger_with([{"date": "20260914", "side": "buy", "ts_code": "600000.SH",
                                "shares": 100, "price": 10.0, "fee": 5.0}])
        out = self._current(p)
        for k in ("turnover_est", "deployed_ratio"):
            self.assertTrue(math.isfinite(out[k]), f"{k} 必须是有限值, 实得 {out[k]!r}")
        self.assertEqual(out["turnover_est"], 0.0, "只有买 ⇒ 换手 0")
        json.dumps(out, allow_nan=False, ensure_ascii=False)      # FastAPI 的序列化条件

    def test_zero_nav_does_not_poison_deployed_ratio(self):
        p = self._ledger_with([{"date": "20260914", "side": "buy", "ts_code": "600000.SH",
                                "shares": 100, "price": 10.0, "fee": 5.0}])
        st = json.loads(p.read_text(encoding="utf-8"))
        st["nav_history"][-1]["nav"] = 0.0            # 恶意/损坏：nav=0
        p.write_text(json.dumps(st), encoding="utf-8")
        out = self._current(p)
        self.assertTrue(math.isfinite(out["deployed_ratio"]))
        json.dumps(out, allow_nan=False, ensure_ascii=False)


class TestRollingIcRejectsNaN(unittest.TestCase):
    """零方差截面 ⇒ 秩相关 NaN ⇒ **不许**被计成"连续为负"锚点。"""

    def test_zero_variance_cross_section_is_not_a_negative_anchor(self):
        """6 个 NaN 就足以判 degrading ⇒ risk.apply 自动停买（要人工 resume）。

        桩：两期**常量分数** + 非恒定收益 ⇒ 秩相关 NaN。红检：去掉 `np.isfinite` 守卫
        ⇒ neg_streak 变 2、ic 变 NaN。
        """
        cols = [f"s{i:03d}" for i in range(300)]
        S = np.ones((2, len(cols)))                   # 常量分 ⇒ 秩相关 NaN
        anchors = pd.to_datetime(["20260908", "20260915"])
        r = pd.Series({c: float(i % 7) - 3.0 for i, c in enumerate(cols)})
        with mock.patch.object(health.wsc, "latest",
                               return_value=(S, anchors, cols, ["f"], Path("stub.npz"))), \
                mock.patch.object(health, "_fwd_excess", return_value=r):
            with np.errstate(invalid="ignore", divide="ignore"):   # 零方差是夹具的**故意**输入
                out = health.rolling_ic(window=12)
        self.assertEqual(out["neg_streak"], 0, "NaN 不许计入连续为负")
        self.assertIsNone(out["ic"], "没有有效 IC ⇒ ic 必须是 None（不是 NaN）")


class TestHealthThresholds(unittest.TestCase):
    IC_OK = 0.08          # > 0.5 × 0.0773
    IC_LOW = 0.03

    def test_healthy(self):
        r = health.evaluate(self.IC_OK, 0, 0.10, 0.75)
        self.assertEqual(r["level"], "healthy")
        self.assertEqual(r["reasons"], [])

    def test_warning_on_low_ic(self):
        r = health.evaluate(self.IC_LOW, 0, 0.10, 0.75)
        self.assertEqual(r["level"], "warning")
        self.assertIn("0.5×冻结基线", r["reasons"][0])

    def test_warning_on_low_deployment(self):
        self.assertEqual(health.evaluate(self.IC_OK, 0, 0.10, 0.55)["level"], "warning")

    def test_warning_on_2x_turnover(self):
        # 基线 0.137（生产构型）⇒ 2× = 0.274、3× = 0.411
        self.assertEqual(health.evaluate(self.IC_OK, 0, 0.30, 0.75)["level"], "warning")

    def test_degrading_on_neg_streak_6(self):
        r = health.evaluate(self.IC_OK, 6, 0.10, 0.75)
        self.assertEqual(r["level"], "degrading")

    # ---- S4（2026-09-21）：负 IC 必须进 warning，不能落到 healthy ----
    # 实证（`experiments/b_stage_20260920/m1_ic_scale_flip.json` 的 `flip_anchors`）：
    #   20170609  rolling_ic = **-0.0072**（负）⇒ 曾判 **healthy**
    #   20170623  rolling_ic = +0.0005（正但极小）⇒ 判 warning
    # ⇒ "负的反而比正的健康"的翻档。根因：L75 的 `0 < rolling_ic` 把负 IC 排除在 warning 之外。
    def test_warning_on_negative_ic(self):
        """S4 红线用例：负 IC 且 neg_streak<6 ⇒ 必须 warning（修复前是 healthy）。"""
        r = health.evaluate(-0.0072, 3, 0.10, 0.75)
        self.assertEqual(r["level"], "warning",
                         "负 IC 落到了 healthy —— 这正是 S4 要修的翻档")
        self.assertIn("0.5×冻结基线", r["reasons"][0])

    def test_neg_ic_with_streak_6_still_degrading(self):
        """S4-a：负 IC 且 neg_streak>=6 ⇒ 仍优先 degrading（分支顺序不变，不得降级成 warning）。"""
        r = health.evaluate(-0.0072, 6, 0.10, 0.75)
        self.assertEqual(r["level"], "degrading")

    def test_none_ic_unchanged(self):
        """S4-b：rolling_ic 为 None（无数据）⇒ 行为不变，不进 warning。"""
        r = health.evaluate(None, 0, 0.10, 0.75)
        self.assertEqual(r["level"], "healthy")

    def test_degrading_on_3x_turnover(self):
        # 2026-09-24：必须显式给样本量 —— `n_turnover` 的默认值是 0（= 样本不足，保守），
        # 不传就等于"不知道样本量"，换手便不得单独升级到 degrading。见
        # `TestTurnoverSampleGuard` 与 `plans/health-turnover-guard-prereg-20260924.md`。
        self.assertEqual(health.evaluate(self.IC_OK, 0, 0.45, 0.75,
                                         n_turnover=12)["level"], "degrading")

    def test_dead_on_neg_streak_12(self):
        r = health.evaluate(self.IC_OK, 12, 0.10, 0.75)
        self.assertEqual(r["level"], "dead")
        self.assertIn("退役候选", r["reasons"][0])

    def test_dead_wins_over_turnover(self):
        """多个条件同时命中时, 取最严重那档(dead > degrading > warning)。"""
        self.assertEqual(health.evaluate(0.01, 12, 0.99, 0.10)["level"], "dead")

    def test_frozen_ic_matches_f22(self):
        """IC 基线 = F22 的 0.0773；**换手基线必须是生产构型**（F52 的 0.137，不是研究构型的 0.092）。

        2026-09-13 修正（07 号 #25）：原 0.092 来自 F22 那批 `hyst=0.2` 的实验，而生产
        `hold_bonus=0.0` 的换手实测 0.137 ⇒ 拿旧值当基线会让阈值偏紧 ~50%，把 2× 判成 3×
        并触发 `risk.apply` 自动停买。这条测试就是钉住"基线必须来自生产构型"。
        """
        self.assertAlmostEqual(health.FROZEN_IC, 0.0773, places=4)
        self.assertAlmostEqual(health.BACKTEST_TURNOVER, 0.137, places=3)
        self.assertGreater(health.BACKTEST_TURNOVER, 0.092,
                           "生产构型换手必须高于研究构型的 0.092（F52 实测 0.137）")


class TestTurnoverSampleGuard(unittest.TestCase):
    """换手档位判定的**样本量守卫**（2026-09-24）。

    背景（预注册 `plans/health-turnover-guard-prereg-20260924.md`）：
    `current()` 的换手估计是 `sells.tail(12).mean() / npos` —— **卖出日只有 1 个时也照常出数**。
    实测：账本里全部 31 笔卖出只发生在唯一一天 20260921 ⇒ `turnover_est = 31/98 = 0.3163`
    是 **n=1 的单点观测**；只要某天卖出 > 40 笔就越过 `3×` 线 ⇒ 触发 `risk.apply`
    **自动停买且不自动解除**。

    红线：**n 不足时换手不得单独升级到 degrading**（信息仍以 warning 呈现，不藏起来）。
    红检：把 `evaluate` 的 `and enough` 去掉 ⇒ `test_n1_does_not_degrade` 当场红。
    """

    IC_OK = 0.08          # > 0.5 × 0.0773
    BT = health.BACKTEST_TURNOVER      # 0.137 ⇒ 2×=0.274、3×=0.411

    # ---------- 红线：样本不足 ----------

    def test_n1_does_not_degrade(self):
        """n=1 且换手越过 3× ⇒ **warning**，不得 degrading（这正是本次要修的那条路）。"""
        r = health.evaluate(self.IC_OK, 0, 0.45, 0.75, n_turnover=1)
        self.assertEqual(r["level"], "warning",
                         "n=1 的单点观测把档位升到 degrading ⇒ 会触发不可逆自动停买")
        self.assertTrue(any("样本不足" in s for s in r["reasons"]),
                        f"必须如实标注样本不足: {r['reasons']}")

    def test_default_is_conservative(self):
        """**不传** `n_turnover` ⇒ 默认 0 = 样本不足 ⇒ 保守。

        这是刻意的设计：新调用方忘了传样本量时应当自动获得保护，
        而不是静默失去保护（本项目一贯反对静默失效）。
        """
        r = health.evaluate(self.IC_OK, 0, 0.45, 0.75)
        self.assertEqual(r["level"], "warning", "默认必须保守，不许默认放行")

    def test_boundary_at_min_days(self):
        """边界：n=3 不升级、n=4（= `TURNOVER_MIN_DAYS`）恢复全强度。"""
        self.assertEqual(health.TURNOVER_MIN_DAYS, 4)
        self.assertEqual(health.evaluate(self.IC_OK, 0, 0.45, 0.75,
                                         n_turnover=3)["level"], "warning")
        self.assertEqual(health.evaluate(self.IC_OK, 0, 0.45, 0.75,
                                         n_turnover=4)["level"], "degrading")

    # ---------- 不得被削弱的部分 ----------

    def test_n12_still_degrades(self):
        """样本足够时**行为与改动前逐位一致**（不许借守卫之名把判据放松）。"""
        self.assertEqual(health.evaluate(self.IC_OK, 0, 0.45, 0.75,
                                         n_turnover=12)["level"], "degrading")

    def test_2x_line_unaffected_by_sample_size(self):
        """2×~3× 区间本就是 warning ⇒ 与样本量无关（守卫只拦"单独升级到 degrading"）。"""
        for n in (0, 1, 3, 4, 12):
            self.assertEqual(health.evaluate(self.IC_OK, 0, 0.30, 0.75,
                                             n_turnover=n)["level"], "warning",
                             f"n={n} 时 2× 线行为不该变")

    def test_2x_reason_notes_sample_size_when_insufficient(self):
        """⚠️ 样本不足时 **2×~3× 的 reasons 也要带注记**（2026-09-24 实测后补）。

        起因：只写"换手 0.316 为回测的 2~3 倍"会被读成"袖套长期换手偏高"，
        而真相是"**那一次调仓**换掉了 31/98 的名字"。判据的**样本量前提不写出来**，
        读者会照错的前提去做决策（本项目的原话：读数必须带口径）。
        """
        r = health.evaluate(self.IC_OK, 0, 0.30, 0.75, n_turnover=1)
        self.assertEqual(r["level"], "warning")
        joined = " ".join(r["reasons"])
        self.assertIn("样本不足", joined, f"2× 区间的 reasons 缺样本量注记: {r['reasons']}")
        self.assertIn("n=1", joined)

    def test_2x_reason_clean_when_enough(self):
        """自证门 T-c 的一半：样本足够时 reasons **逐字不变**（不许无差别加注记）。"""
        r = health.evaluate(self.IC_OK, 0, 0.30, 0.75, n_turnover=12)
        self.assertEqual(r["reasons"], ["换手 0.300 为回测的 2~3 倍"])

    def test_neg_streak_branches_untouched(self):
        """自证门 T-a/T-b：两条 `neg_streak` 分支一字未动，不因样本量被削弱或提升。"""
        self.assertEqual(health.evaluate(self.IC_OK, 6, 0.45, 0.75,
                                         n_turnover=1)["level"], "degrading",
                         "neg_streak>=6 仍优先 degrading（与换手样本量无关）")
        self.assertEqual(health.evaluate(self.IC_OK, 12, 0.10, 0.75,
                                         n_turnover=0)["level"], "dead")

    def test_healthy_unchanged(self):
        self.assertEqual(health.evaluate(self.IC_OK, 0, 0.10, 0.75,
                                         n_turnover=0)["level"], "healthy")

    def test_returns_sample_size_fields(self):
        r = health.evaluate(self.IC_OK, 0, 0.10, 0.75, n_turnover=7)
        self.assertEqual(r["turnover_n"], 7)
        self.assertEqual(r["turnover_min_days"], health.TURNOVER_MIN_DAYS)

    # ---------- 端到端：current() 必须传真实样本量 ----------

    def _current_with_sell_days(self, n_sell_days):
        """造一个含 `n_sell_days` 个卖出日的账本，跑 `current()`。"""
        trades = []
        for i in range(n_sell_days):
            trades.append({"date": f"202609{10 + i:02d}", "side": "sell",
                           "ts_code": "600000.SH", "shares": 10, "price": 10.0, "fee": 5.0})
        tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w",
                                          encoding="utf-8")
        json.dump({
            "capital": 1_000_000.0, "cash": 133_967.54,
            "positions": {"600000.SH": {"shares": 100.0, "cost": 10.0, "last_px": 10.0}},
            "nav_history": [
                {"date": "20260914", "nav": 999_501.34, "invested": 865_533.8,
                 "cash": 133_967.54, "interest": 0.0, "positions": {"600000.SH": 100.0}},
                {"date": "20260915", "nav": 999_501.34, "invested": 865_533.8,
                 "cash": 133_967.54, "interest": 0.0, "positions": {"600000.SH": 100.0}},
            ],
            "trades": trades, "inception": "20260914", "last_mark": "20260915",
            "interest": 0.0, "interest_upto": "20260915"}, tmp)
        tmp.close()
        p = Path(tmp.name)
        self.addCleanup(lambda: p.unlink(missing_ok=True))
        with mock.patch.object(health, "rolling_ic",
                               return_value={"n": 0, "ic": None, "neg_streak": 0,
                                             "path": "stub.npz"}):
            return health.current(window=12, ledger_path=p)

    def test_current_reports_sell_day_count(self):
        """`current()` 必须把**真实卖出日数**传给判据 —— 否则守卫形同虚设。"""
        self.assertEqual(self._current_with_sell_days(1)["turnover_n"], 1)
        self.assertEqual(self._current_with_sell_days(5)["turnover_n"], 5)

    def test_current_n1_cannot_reach_degrading_by_turnover(self):
        """端到端红线：1 个卖出日、卖出量越过 3× ⇒ 不得是 degrading。"""
        out = self._current_with_sell_days(1)
        self.assertEqual(out["turnover_n"], 1)
        self.assertNotEqual(out["level"], "degrading",
                            "n=1 的单点观测不该触发不可逆停买")
        json.dumps(out, allow_nan=False, ensure_ascii=False)


class TestAlertEdgeTrigger(unittest.TestCase):
    """告警**边沿**触发（2026-09-18 邮件优化）：不再"每天不健康就每天一封"。

    原来 19:45 档是电平触发 ⇒ 袖套一旦退化，会连发一周内容一模一样的信，
    直到有人 resume 或它自己好回来。这类重复的代价不是"烦"，是**把人训练成不读告警**。
    同时不能矫枉过正：报一次就永久沉默更糟（未处理的退化没人再被提醒）⇒ 满 7 天补一发。
    """

    def test_first_unhealthy_alerts(self):
        self.assertEqual(health.decide_alert(None, "warning"), "alert")

    def test_same_level_is_silent_before_the_repeat_window(self):
        self.assertIsNone(health.decide_alert("warning", "warning", "20260917", "20260918"))

    def test_same_level_repeats_after_7_days(self):
        self.assertEqual(health.decide_alert("warning", "warning", "20260910", "20260918"),
                         "repeat")

    def test_level_change_alerts_in_both_directions(self):
        # 升档要报；降档（degrading→warning）也要报 —— 处置状态变了
        self.assertEqual(health.decide_alert("warning", "degrading", "20260917", "20260918"),
                         "alert")
        self.assertEqual(health.decide_alert("degrading", "warning", "20260917", "20260918"),
                         "alert")

    def test_recovery_sends_one_closure_email(self):
        self.assertEqual(health.decide_alert("degrading", "healthy", "20260917", "20260918"),
                         "recovered")
        # 已经健康过 ⇒ 不再重复发结案信
        self.assertIsNone(health.decide_alert("healthy", "healthy", "20260917", "20260918"))

    def test_unknown_never_counts_as_recovery(self):
        """读数缺失 ≠ 健康。把 unknown 当 healthy 会**误发结案信**，把还没解决的退化说成好了。"""
        self.assertIsNone(health.decide_alert("degrading", "unknown", "20260917", "20260918"))
        self.assertIsNone(health.decide_alert("degrading", None, "20260917", "20260918"))

    def test_missing_prev_date_alerts_rather_than_silences(self):
        """状态文件只有等级、没有日期 ⇒ 宁可多提醒一次，不许静默。"""
        self.assertEqual(health.decide_alert("warning", "warning"), "repeat")

    def test_state_roundtrip_and_corrupt_file(self):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "health_notified.json"
            self.assertEqual(health.load_notified(f), {}, "读不到 = 没通知过(下一封照发)")
            self.assertTrue(health.save_notified("degrading", "20260918", f))
            self.assertEqual(health.load_notified(f), {"level": "degrading", "date": "20260918"})
            f.write_text("{坏档", encoding="utf-8")
            self.assertEqual(health.load_notified(f), {}, "坏档必须降级成空, 不能抛")
            self.assertFalse(f.with_suffix(".tmp").exists(), "原子写不许留下 .tmp")


class TestBlockIcCriterion(unittest.TestCase):
    """**块级 IC 判据**（D 案 ③-1）：构造块劣化必须报警，构造正常不许报警。

    判据被拆成纯函数 `health.block_ic_from_ic` 就是为了让这件事能被证明 —— 取数那段
    （70 个因子锚点）没法在单测里复现，但"报警灵不灵"必须钉死，否则监控又是一块绿招牌。

    判据：定向（按基线期 IC 符号）后的块等权 IC，当前窗 < 0.5× 基线 ⇒ 报警；
    负 IC **不**被排除（S4 同口径）。
    """

    @staticmethod
    def _rows(spec_base: dict, spec_cur: dict, n_base: int = 20, n_cur: int = 6):
        """spec = {块键: 该块每个成员的 IC}。未列出的块 ⇒ 成员没有读数。"""
        def _mk(spec, dates):
            out = {}
            for d in dates:
                row = {}
                for b, v in spec.items():
                    for f in wk_blocks.members(b):
                        row[f] = v
                out[d] = row
            return out
        base_d = [f"b{i:02d}" for i in range(n_base)]
        cur_d = [f"c{i:02d}" for i in range(n_cur)]
        rows = dict(_mk(spec_base, base_d))
        rows.update(_mk(spec_cur, cur_d))
        return rows, cur_d, base_d

    ALL_OK = {b: 0.05 for b in wk_blocks.BLOCK_KEYS}

    def test_sign_flip_in_one_block_alarms_only_that_block(self):
        """单个成员**符号翻转**是最危险的失效：幅度没衰减，|IC| 一点没变，
        只看绝对值的监控会完全看不见。定向后的块读数必须转负并报警。"""
        cur = dict(self.ALL_OK, B3=-0.05)
        rows, c, b = self._rows(self.ALL_OK, cur)
        out = health.block_ic_from_ic(rows, c, b)
        self.assertEqual(out["alarms"], ["B3"], out["blocks"])
        self.assertEqual(out["p0_alarms"], ["B3"])
        self.assertLess(out["blocks"]["B3"]["ic"], 0)
        for k in ("B1", "B2", "B4"):
            self.assertFalse(out["blocks"][k]["alarm"], k)

    def test_decay_boundary_at_half_the_baseline(self):
        """0.6× 不报警；0.4× 报警 —— 阈值两侧都要有钉子（防止"±"写成"<"这类翻向）。"""
        cur = dict(self.ALL_OK, B1=0.03, B2=0.02)      # 基线 0.05 ⇒ 0.5× = 0.025
        rows, c, b = self._rows(self.ALL_OK, cur)
        out = health.block_ic_from_ic(rows, c, b)
        self.assertFalse(out["blocks"]["B1"]["alarm"], f"0.6× 不该报警: {out['blocks']['B1']}")
        self.assertTrue(out["blocks"]["B2"]["alarm"], f"0.4× 必须报警: {out['blocks']['B2']}")
        self.assertAlmostEqual(out["blocks"]["B1"]["ratio"], 0.6, places=2)
        self.assertAlmostEqual(out["blocks"]["B2"]["ratio"], 0.4, places=2)

    def test_negative_ic_is_not_excluded(self):
        """S4 同口径：块读数转负**必须**报警（把负的排除在报警之外就是 S4 修的那个翻档）。"""
        cur = dict(self.ALL_OK, B4=-0.01)
        rows, c, b = self._rows(self.ALL_OK, cur)
        out = health.block_ic_from_ic(rows, c, b)
        self.assertTrue(out["blocks"]["B4"]["alarm"], out["blocks"]["B4"])

    def test_p1_block_alarms_but_is_not_escalated(self):
        """D 案的分级：B5(P1) 劣化要记录，但**不上报**（只有 P0 进运维邮件）。"""
        cur = dict(self.ALL_OK, B5=0.0)
        rows, c, b = self._rows(self.ALL_OK, cur)
        out = health.block_ic_from_ic(rows, c, b)
        self.assertIn("B5", out["alarms"])
        self.assertNotIn("B5", out["p0_alarms"], "P1 块不许进上报名单")

    def test_degenerate_baseline_is_annotated_not_silently_green(self):
        """基线算不出来时**不许**报绿 —— "没有基线"不是"健康"。"""
        spec = {k: 0.05 for k in wk_blocks.BLOCK_KEYS if k != "B6"}   # B6 全程无读数
        rows, c, b = self._rows(spec, spec)
        out = health.block_ic_from_ic(rows, c, b)
        b6 = out["blocks"]["B6"]
        self.assertFalse(b6["alarm"])
        self.assertIn("读数不足", b6["note"], b6)
        self.assertIsNone(b6["ic"])

    def test_zero_baseline_says_criterion_not_applicable(self):
        rows, c, b = self._rows({k: 0.0 for k in wk_blocks.BLOCK_KEYS},
                                {k: 0.01 for k in wk_blocks.BLOCK_KEYS})
        out = health.block_ic_from_ic(rows, c, b)
        self.assertIn("判据不适用", out["blocks"]["B1"]["note"], out["blocks"]["B1"])
        self.assertEqual(out["alarms"], [])


class TestBlockIcCache(unittest.TestCase):
    """缓存与 doctor 读数的契约：没缓存要说"怎么补"，不许显示成一条绿。"""

    def test_missing_cache_is_available_false_with_a_runnable_fix(self):
        with tempfile.TemporaryDirectory() as td:
            res = health.block_ic_cached(Path(td) / "nope.json")
            self.assertFalse(res["available"])
            self.assertIn("blockic", res["note"])

    def test_cache_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "block_ic.json"
            payload = {"available": True, "asof": "20260914", "blocks": {}}
            with mock.patch.object(health, "block_ic", return_value=payload):
                self.assertEqual(health.block_ic_refresh(p), payload)
            self.assertEqual(health.block_ic_cached(p), payload)

    def test_cache_write_is_atomic_and_fail_safe(self):
        """缓存写必须走 tmp+os.replace（与 save_notified 同手法，2026-09-29 审查）：
        写入中途失败时旧缓存必须原样可读、不许抛（读数不许把 19:45 档带崩）、
        不许留 .tmp 残留。原实现 write_text 直接覆写 —— 写一半被杀留下半截 JSON，
        doctor/看板当期读数缺失（自愈但空窗）。"""
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "block_ic.json"
            good = {"available": True, "asof": "20260914", "blocks": {}}
            with mock.patch.object(health, "block_ic", return_value=good):
                health.block_ic_refresh(p)
            self.assertEqual(health.block_ic_cached(p), good)

            # 注入"写完就崩"：任何 write_text 都成功落盘后立即抛 —— 模拟写到一半被杀。
            # 旧实现直接 write_text 覆写目标路径 ⇒ 旧缓存被毁且异常上抛（红）；
            # 原子实现写的是 tmp ⇒ 旧缓存原样、异常被吞成告警（绿）。
            boom = mock.patch.object(Path, "write_text",
                                     side_effect=RuntimeError("crash mid-write"))
            with mock.patch.object(health, "block_ic",
                                   return_value={"available": True, "asof": "20260929",
                                                 "blocks": {}}), \
                    boom, \
                    mock.patch.object(health.log, "warning"):
                out = health.block_ic_refresh(p)          # 不许抛
            self.assertEqual(out["asof"], "20260929")
            self.assertEqual(health.block_ic_cached(p), good,
                             "写入失败时旧缓存必须原样保留")
            self.assertEqual(list(Path(td).glob("*.tmp")), [], "不许留 .tmp 残留")


class TestDoctorBlockIcItem(unittest.TestCase):
    """"P0 四块每块 ≥1 个读数进 doctor"（D 案 §四）。"""

    def _item(self, cache, block="B1"):
        from app.weekly import doctor as wk_doctor
        return wk_doctor.check_block_ic({}, block, cache)

    def test_missing_cache_is_not_ok(self):
        it = self._item({"available": False, "note": "还没算"})
        self.assertFalse(it["ok"])
        self.assertIn("blockic", it["fix"])

    def test_alarming_block_is_not_ok_and_says_where_to_look(self):
        cache = {"available": True, "asof": "20260914", "window": 12, "baseline_anchors": 60,
                 "blocks": {"B1": {"ic": -0.02, "baseline": 0.06, "ratio": -0.33,
                                   "alarm": True, "role": "流动性/规模"}}}
        it = self._item(cache)
        self.assertFalse(it["ok"])
        self.assertIn("B1", it["detail"])
        self.assertIn("factor_baseline_ic", it["fix"], "修的人必须知道去哪定位")

    def test_healthy_block_is_ok_and_shows_the_ratio(self):
        cache = {"available": True, "asof": "20260914", "window": 12, "baseline_anchors": 60,
                 "blocks": {"B1": {"ic": 0.07, "baseline": 0.06, "ratio": 1.17,
                                   "alarm": False, "role": "流动性/规模"}}}
        it = self._item(cache)
        self.assertTrue(it["ok"])
        self.assertIn("1.17", it["detail"])


if __name__ == "__main__":
    unittest.main()
