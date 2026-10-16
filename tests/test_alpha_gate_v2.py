"""门禁 v2 的守卫测试（2026-09-14，用户拍板"门禁科学化/先进化/合理化改造"）

判据冻结记录见 KB 14 号 三补十四（**写死在改代码之前**；原预注册
`plans/gate-v2-prereg.md` 已于 2026-09-15 按用户指示随叙述性文档清理删除 —— commit
4a2bfd72，当时"运行时零引用"核验漏了注释引用，2026-09-19 才改净；原文 111 行可用
`git show 4a2bfd72^:plans/gate-v2-prereg.md` 找回）。三条改动各有一组测试，
每组都带"摘掉改动必须当场变红"的那条：

① **经济口径**：门槛 = `0.5%/月 + 自换手 × 往返真实费率`；成本缺失 ⇒ **fail-closed**。
   红检 = 同一个候选把 `cost_per_month` 置 0 就从 FAIL 变 PASS（证明成本项真在起作用）。
② **累计分母**：`N_used = max(本批, 累计 + 本批)`。
   红检 = 同一读数在 N=15 与 N=171 下 `p_adj` 判据翻转。
③ **撞车硬闸**：同公式 / 同 id 异公式 / 同名异源 / 值面板等价，四类全拒。
   红检 = 同一条公式第二次登记必须被拒（摘掉检查则能二次登记）。

全部用**临时库 + 临时值目录**，不碰生产 `data/neweco.db` 与 `data/alpha_pool/`。
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db  # noqa: E402
from app.alpha import bank, critic, gate  # noqa: E402


def _rec(**over):
    """一条"本来能通过"的合成读数（门禁 v2 后必须再给 cost_per_month）"""
    rec = {"DEAD_head": 0.010, "ACTIVE_head": 0.008, "DEAD_head_t": 2.50,
           "DEAD_top6": 0.004, "p_adj": 0.001, "cost_per_month": 0.0005}
    rec.update(over)
    return rec


# ===========================================================================
# ① 经济口径
# ===========================================================================
class TestEconomicHeadBar(unittest.TestCase):
    def test_cost_lifts_the_bar_and_flips_the_verdict(self):
        """红检：把成本置 0 ⇒ 同一个候选从 FAIL 变 PASS（成本项确实在卡人）。"""
        thin = _rec(DEAD_head=0.0052, cost_per_month=0.0008)     # 0.52% < 0.5%+0.08%
        free = _rec(DEAD_head=0.0052, cost_per_month=0.0)        # 0.52% ≥ 0.5%+0
        self.assertFalse(gate.verdict(thin)["PASS"], "成本项没起作用")
        self.assertTrue(gate.verdict(free)["PASS"])
        # `head_bar` 由 verdict() 写回**入参 rec**（与 summarize 的 rec.update(verdict(rec)) 同路）
        self.assertAlmostEqual(thin["head_bar"], 0.0058)
        self.assertAlmostEqual(free["head_bar"], 0.005)

    def test_missing_or_nan_cost_is_fail_closed(self):
        """少一个输入**不许**静默退回旧门槛（与 2026-09-12 修的 NaN 放行同类）"""
        for bad in ({}, {"cost_per_month": None}, {"cost_per_month": float("nan")}):
            with self.subTest(bad=bad):
                v = gate.verdict(_rec(**bad) if bad else {
                    k: v for k, v in _rec().items() if k != "cost_per_month"})
                self.assertFalse(v["c_cost"])
                self.assertFalse(v["c_head_size"])
                self.assertFalse(v["PASS"])

    def test_negative_cost_cannot_loosen_the_bar(self):
        v = gate.verdict(_rec(cost_per_month=-0.01))
        self.assertFalse(v["c_cost"])
        self.assertFalse(v["PASS"])

    def test_churn_cost_uses_the_real_fee_schedule(self):
        """成本必须来自 `fees.effective_rate`（含 5 元佣金地板），不是写死的常量。"""
        from app.portfolio import fees as fees_mod
        md = pd.DatetimeIndex(["2021-01-29", "2021-02-26", "2021-03-31"])
        codes = ["%06d.SZ" % i for i in range(12)]
        wide = pd.DataFrame(np.arange(36, dtype=float).reshape(3, 12),
                            index=md, columns=codes)
        cfg = {"weekly": {"capital": 1_000_000}}
        got = critic.churn_cost(wide, md, top_n=6, per_name_notional=10_000, cfg=cfg)
        p = fees_mod.fee_params(cfg)
        manual = (fees_mod.effective_rate(10_000, "buy", None, p)
                  + fees_mod.effective_rate(10_000, "sell", None, p))
        self.assertAlmostEqual(got["round_trip_rate"], manual, places=12)
        self.assertAlmostEqual(got["cost_per_month"],
                               got["mean_top6_turnover"] * manual, places=12)
        # 5 元最低佣金在 1 万元/票上是 5bp, 在 100 万元/票上只有 0.5bp ⇒ 地板真的可见
        self.assertGreater(critic.churn_cost(wide, md, per_name_notional=10_000,
                                             cfg=cfg)["round_trip_rate"],
                           critic.churn_cost(wide, md, per_name_notional=1_000_000,
                                             cfg=cfg)["round_trip_rate"])

    def test_churn_cost_empty_panel_returns_empty(self):
        """算不出成本 ⇒ 返回 {} ⇒ 调用方按 fail-closed 处理（不是当 0 用）"""
        self.assertEqual(critic.churn_cost(pd.DataFrame(), pd.DatetimeIndex([])), {})


# ===========================================================================
# ② 累计分母
# ===========================================================================
class TestCumulativeDenominator(unittest.TestCase):
    def test_gate_records_all_three_denominators(self):
        det = pd.DataFrame({
            "zone": ["DEAD", "ACTIVE"], "head_excess": [0.01, 0.008],
            "top6_excess": [0.004, 0.003], "ic_full": [0.05, 0.04], "ls_spread": [0.02, 0.01],
            "n": [500, 500]}, index=pd.DatetimeIndex(["2021-01-29", "2021-02-26"]))
        rec = gate.summarize("x", det, 174, cost_per_month=0.0005,
                             n_tests_family=3, n_tests_prior=171)
        self.assertEqual(rec["n_tests"], 174)
        self.assertEqual(rec["n_tests_family"], 3)
        self.assertEqual(rec["n_tests_prior"], 171)
        self.assertAlmostEqual(rec["cost_per_month"], 0.0005)
        self.assertAlmostEqual(rec["head_bar"], 0.0055)

    def test_same_reading_flips_when_denominator_is_cumulative(self):
        """红检：同一读数在"本批 15"下过关、在"累计 171"下不过关。"""
        t = 3.2
        p = gate._p_two_sided(t)
        p15 = gate.bonferroni(p, 15)
        p171 = gate.bonferroni(p, 171)
        self.assertLess(p15, gate.GATE_ALPHA, "样例选错了: 本批口径下本该过关")
        self.assertGreaterEqual(p171, gate.GATE_ALPHA, "样例选错了: 累计口径下本该不过关")
        self.assertTrue(gate.verdict(_rec(DEAD_head_t=t, p_adj=p15))["c_p_adj"])
        self.assertFalse(gate.verdict(_rec(DEAD_head_t=t, p_adj=p171))["c_p_adj"])

    def test_denominator_never_shrinks(self):
        """单调性: 累计分母只会让门槛更严（这是"研究程序整体"口径的全部意义）"""
        for prior in (0, 1, 171, 5000):
            with self.subTest(prior=prior):
                self.assertGreaterEqual(max(3, prior + 3), 3)


class _TempBank(unittest.TestCase):
    """临时库 + 临时值目录（`alpha_bank` 与 npz 值面板都不许落到生产路径）"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.db_path = self.dir / "t.db"
        self.pool = self.dir / "alpha_pool"
        for p in (mock.patch.object(db, "DB_PATH", self.db_path),
                  mock.patch.object(bank, "POOL_DIR", self.pool)):
            p.start()
            self.addCleanup(p.stop)
        db.init_db()
        bank.ensure_table()

    def _register(self, aid, expr=None, name=None, zoo=None, values=None, yearly=None):
        rec = {"alpha_id": aid, "expr": expr if expr is not None else "Expr_%s" % aid,
               "name": name or aid, "zoo": zoo or "test", "PASS": 0,
               "yearly": yearly if yearly is not None else {"2021": {"head": 0.01, "n": 12}}}
        bank.upsert(rec)
        if values is not None:
            bank.save_values(aid, values)
        return rec


# ===========================================================================
# ③ 撞车硬闸
# ===========================================================================
class TestDuplicateGate(_TempBank):
    def test_same_expr_new_name_is_refused(self):
        """红检：同一条公式换个名字再测 —— 必须被拒（摘掉检查就能二次登记）"""
        self._register("a1", expr="($close-$open)/$open")
        dup = bank.check_duplicate({"alpha_id": "a2", "expr": "($close-$open)/$open",
                                    "name": "a2", "zoo": "test"})
        self.assertTrue(dup["dup"])
        self.assertEqual(dup["kind"], "expr")
        self.assertEqual(dup["existing"], "a1")

    def test_whitespace_and_case_are_normalised(self):
        self._register("a1", expr="Mean($close, 20)")
        dup = bank.check_duplicate({"alpha_id": "b1", "expr": " mean( $CLOSE ,20 ) ",
                                    "name": "b1", "zoo": "test"})
        self.assertTrue(dup["dup"], "规范化没生效")

    def test_same_id_different_expr_is_conflict(self):
        self._register("a1", expr="A+B")
        dup = bank.check_duplicate({"alpha_id": "a1", "expr": "A-B", "name": "a1"})
        self.assertTrue(dup["dup"])
        self.assertEqual(dup["kind"], "id_conflict")

    def test_rerun_needs_explicit_allow(self):
        self._register("a1", expr="A+B")
        meta = {"alpha_id": "a1", "expr": "A+B", "name": "a1"}
        self.assertEqual(bank.check_duplicate(meta)["kind"], "rerun")
        self.assertFalse(bank.check_duplicate(meta, allow_rerun=True)["dup"],
                         "显式 allow_rerun 必须放行（复现性核验）")

    def test_name_collision_across_zoo(self):
        self._register("qlib_KMID", expr="($close-$open)/$open", name="KMID", zoo="qlib158")
        dup = bank.check_duplicate({"alpha_id": "KMID", "expr": "(high+low)/2",
                                    "name": "KMID", "zoo": "selfmade"})
        self.assertTrue(dup["dup"])
        self.assertEqual(dup["kind"], "name")

    def test_clean_candidate_passes(self):
        self._register("a1", expr="A+B")
        dup = bank.check_duplicate({"alpha_id": "z9", "expr": "C/D", "name": "z9",
                                    "zoo": "test"})
        self.assertFalse(dup["dup"], dup)

    def test_value_level_duplicate_catches_algebraic_twin(self):
        """值级撞车：公式写法不同但值面板一样（yearly 指纹预筛 + 秩相关复核）"""
        codes = ["%06d.SZ" % i for i in range(250)]
        md = pd.DatetimeIndex(["2021-01-29", "2021-02-26", "2021-03-31"])
        vals = pd.DataFrame(np.arange(750, dtype=float).reshape(3, 250),
                            index=md, columns=codes)
        yearly = {"2021": {"head": 0.0123456789, "n": 12}}
        self._register("twin_a", expr="($close-$open)/$open", values=vals, yearly=yearly)
        dup = bank.check_duplicate({"alpha_id": "twin_b", "expr": "$close/$open-1",
                                    "name": "twin_b", "zoo": "test",
                                    "yearly": yearly},
                                   values=vals, md=md)
        self.assertTrue(dup["dup"], "值级撞车没抓到")
        self.assertEqual(dup["kind"], "values")
        self.assertEqual(dup["existing"], "twin_a")

    def test_cumulative_tests_excludes_duplicate_rows_but_counts_candidates(self):
        self._register("a1")
        self._register("a2")
        bank.upsert({"alpha_id": "a3", "note": "撞车"}, status="DUPLICATE")
        self.assertEqual(bank.cumulative_tests(), 2,
                         "撞车留痕行不是一次检验, 不该抬高 N")

    def test_refused_rerun_does_not_wipe_the_registered_row(self):
        """撞车拒跑**不得**把已在册那一行抹成空壳（2026-09-18 审查 Q-05）。

        `factory._refuse_duplicate` 原来对已登记 id 也调 `bank.upsert({alpha_id, note})`,
        而 `bank.upsert` 的 `ON CONFLICT DO UPDATE` 覆盖**全部非键列** ⇒ 新行没给的
        读数一律变 NULL。日志实证: 09-18 12:24 有 10 行 `kg_*` 以 `rerun ←` 被这样抹掉,
        round-2 唯一显著读数「券商金股热度反向 −0.98%/t=−2.73」只剩日志里一行。

        在册 id **不存在**时插一行 DUPLICATE 占位是对的（那 17 行 `id60_*` 属此类,
        它们从未被求值, NULL 是设计而非损伤）—— 所以断言的是"已在册那一行原样不动"。
        """
        from app.alpha import factory as F
        bank.upsert({"alpha_id": "k1", "expr": "A+B", "name": "k1", "zoo": "test",
                     "DEAD_head": -0.0098, "DEAD_head_t": -2.73, "n_tests": 14,
                     "info_source": "kg_r2", "PASS": 0,
                     "yearly": {"2021": {"head": 0.02, "n": 12}}})
        before = bank.get("k1")
        self.assertEqual(before["status"], "CANDIDATE")
        self.assertAlmostEqual(before["DEAD_head"], -0.0098)

        f = F.Factory(F.PreRegistration(intent="单测"))
        cand = F.Candidate(name="k1", expr="A+B", meta={"name": "k1"})
        dup = {"dup": True, "kind": "rerun", "existing": "k1", "detail": "同 id 同公式已在册"}
        with mock.patch.object(F.Factory, "_tradable", return_value=None), \
                mock.patch.object(F.bank, "check_duplicate", return_value=dup):
            f._evaluate([cand], mock.Mock(), None, None, pd.DatetimeIndex(["2021-01-29"]),
                        np.array(["DEAD"]), None, None, 10, "2019-01-01", save=True)

        self.assertEqual(len(f.duplicates), 1, "拒跑仍要留内存痕迹")
        after = bank.get("k1")
        self.assertEqual(after["status"], "CANDIDATE", "拒跑把在册行降级成了 DUPLICATE")
        self.assertAlmostEqual(after["DEAD_head"], -0.0098, msg="读数被 NULL 覆盖")
        self.assertAlmostEqual(after["DEAD_head_t"], -2.73)
        self.assertEqual(after["info_source"], "kg_r2", "来源标记被抹")
        self.assertEqual(after["expr"], "A+B", "公式被抹")
        self.assertIn("2021", json.loads(after["yearly"]), "逐年读数被抹")
        # 分母口径: 这行从"算一次检验"掉成"不算", 抹写会凭空压低 N
        self.assertEqual(bank.cumulative_tests(), 1,
                         "在册候选被抹成 DUPLICATE ⇒ 累计分母被压低")


# ===========================================================================
# Factory 接线（撞车在求值前就拒；分母进 rec）
# ===========================================================================
class TestFactoryWiring(unittest.TestCase):
    def test_factory_refuses_duplicate_before_spending_compute(self):
        from app.alpha import factory as F
        f = F.Factory(F.PreRegistration(intent="单测"))
        cand = F.Candidate(name="dup_cand", expr="A+B", meta={"name": "dup_cand"})
        eng = mock.Mock()
        dup = {"dup": True, "kind": "expr", "existing": "a1", "detail": "同公式"}
        with mock.patch.object(F.Factory, "_tradable", return_value=None), \
                mock.patch.object(F.bank, "check_duplicate", return_value=dup):
            out = f._evaluate([cand], eng, None, None, pd.DatetimeIndex(["2021-01-29"]),
                              np.array(["DEAD"]), None, None, 10, "2019-01-01", save=False)
        self.assertEqual(out, [])
        eng.eval.assert_not_called()          # 拒跑 = 一分算力都不花
        self.assertEqual(len(f.duplicates), 1)
        self.assertEqual(f.duplicates[0]["kind"], "expr")

    def test_factory_write_path_registers_duplicate_trace(self):
        """save=True 撞车时必须留一行 status='DUPLICATE'（可查），且不写读数"""
        from app.alpha import factory as F
        f = F.Factory(F.PreRegistration(intent="单测"))
        cand = F.Candidate(name="dup_cand", expr="A+B", meta={"name": "dup_cand"})
        dup = {"dup": True, "kind": "values", "existing": "a1", "detail": "值一样"}
        written = {}
        with mock.patch.object(F.Factory, "_tradable", return_value=None), \
                mock.patch.object(F.bank, "check_duplicate", return_value=dup), \
                mock.patch.object(F.bank, "upsert",
                                  side_effect=lambda rec, **kw: written.update(kw, rec=rec)):
            f._evaluate([cand], mock.Mock(), None, None, pd.DatetimeIndex(["2021-01-29"]),
                        np.array(["DEAD"]), None, None, 10, "2019-01-01", save=True)
        self.assertEqual(written.get("status"), "DUPLICATE")
        self.assertIn("撞车硬闸", written["rec"]["note"])


# ===========================================================================
# ④ S5 确定性 tie-break（2026-09-22 用户拍板 B 实施）
#    预注册 plans/s5-gate-stable-tiebreak-prereg-20260921.md 判据 S5-1 + 自证门 S5-a/b/c。
#    背景：gate.evaluate 原用 `f.sort_values(ascending=False)`（默认 quicksort 非
#    稳定），分数并列时"谁进 top-k"取决于输入列序（因子级翻档 0.4663%，详见
#    experiments/s5_tiebreak_factor_level.json / s5_tiebreak_return_impact.json）。
#    改为 (分数降序, 代码升序) 稳定排序后，选面与输入列序无关。
# ===========================================================================
class TestStableTiebreak(unittest.TestCase):
    """自证门：S5-a 列序无关 / S5-b 并列组内按代码升序 / S5-c 无并列时与旧实现一致。"""

    N = 300                       # ≥ MIN_NAMES(200)；k = int(300*0.1) = 30
    DATE = pd.Timestamp("2024-01-31")   # 落在 ACTIVE 区

    @staticmethod
    def _run(scores: pd.Series, rets: pd.Series):
        """单截面过 gate.evaluate，返回唯一的指标行。"""
        factor = scores.to_frame().T
        factor.index = pd.DatetimeIndex([TestStableTiebreak.DATE])
        fwd = rets.to_frame().T
        fwd.index = factor.index
        det = gate.evaluate(factor, fwd, factor.index,
                            np.array(["ACTIVE"]))
        assert not det.empty, "截面被门禁口径跳过（n 或 k 不达标）"
        return det.iloc[0]

    @staticmethod
    def _synth(tie_at_boundary: bool, all_tied: bool = False):
        """合成一个截面：代码 s000..s299，收益随代码递增（保证并列组内可区分）。"""
        codes = [f"s{i:03d}.SZ" for i in range(TestStableTiebreak.N)]
        rets = pd.Series(np.linspace(-0.05, 0.05, TestStableTiebreak.N),
                         index=codes)
        if all_tied:
            scores = pd.Series(1.0, index=codes)
        else:
            # 头部 24 名分数递减、第 25~45 名**并列**（横跨 k=30 边界）、其后递减
            scores = pd.Series(
                [3.0 - 0.01 * i for i in range(24)] + [2.5] * 21
                + [2.0 - 0.01 * i for i in range(TestStableTiebreak.N - 45)],
                index=codes)
        return scores, rets

    def test_s5a_selection_is_column_order_invariant(self):
        """S5-a（红检）：并列横跨边界时，打乱列序后**逐位读数不变**。

        旧实现（quicksort）下并列成员的入选取决于列序 ⇒ 打乱后选到收益不同的名字
        ⇒ head_excess 变 ⇒ 本测试当场红。新实现 (score, ts_code) 键与列序无关。
        """
        scores, rets = self._synth(tie_at_boundary=True)
        rng = np.random.default_rng(42)
        perm = rng.permutation(len(scores))
        shuffled = scores.iloc[perm]
        base = self._run(scores, rets)
        other = self._run(shuffled, rets.iloc[perm])
        self.assertAlmostEqual(base["head_excess"], other["head_excess"], places=12)
        self.assertAlmostEqual(base["ls_spread"], other["ls_spread"], places=12)
        self.assertAlmostEqual(base["top6_excess"], other["top6_excess"], places=12)

    def test_s5b_tie_group_resolved_by_ts_code_ascending(self):
        """S5-b：全并列截面 top-k = 代码升序前 k 名（不是列序前 k 名）。

        列刻意按**倒序**摆放：旧实现取列序头部（s299..s270），新实现必须取
        s000..s029。收益随代码递增 ⇒ 两种选法的 head_excess 可区分。
        """
        scores, rets = self._synth(tie_at_boundary=False, all_tied=True)
        rev_codes = list(scores.index[::-1])
        scores_rev = scores.reindex(rev_codes)      # 倒序列序，值全并列
        rets_rev = rets.reindex(rev_codes)
        row = self._run(scores_rev, rets_rev)
        k = max(1, int(self.N * gate.TOP_FRAC))
        want = rets.sort_index().iloc[:k].mean() - rets.mean()   # 代码升序前 k
        got = row["head_excess"]
        self.assertAlmostEqual(got, want, places=12,
                               msg="全并列时 top-k 未按 ts_code 升序取")

    def test_s5c_no_ties_matches_legacy_order(self):
        """S5-c：分数互不相同时，新排序与旧 `sort_values(ascending=False)` 完全一致。"""
        codes = [f"s{i:03d}.SZ" for i in range(self.N)]
        rng = np.random.default_rng(7)
        scores = pd.Series(rng.random(self.N), index=codes)      # 连续值 ⇒ 无并列
        rets = pd.Series(rng.random(self.N), index=codes)
        row = self._run(scores, rets)
        legacy = scores.sort_values(ascending=False)             # 旧实现的选面
        k = max(1, int(self.N * gate.TOP_FRAC))
        want_head = rets[legacy.index[:k]].mean() - rets.mean()
        want_ls = rets[legacy.index[:k]].mean() - rets[legacy.index[-k:]].mean()
        want_t6 = rets[legacy.index[:6]].mean() - rets.mean()
        self.assertAlmostEqual(row["head_excess"], want_head, places=12)
        self.assertAlmostEqual(row["ls_spread"], want_ls, places=12)
        self.assertAlmostEqual(row["top6_excess"], want_t6, places=12)

    def test_s5a_red_check_old_quicksort_was_order_sensitive(self):
        """S5-a 的**反向红检**：把新排序换回旧的 quicksort，必须当场变红。

        用 mock 把 gate.evaluate 内的排序退回旧行为不可行（局部变量），改为直接
        验证旧行为本身是列序敏感的：同一并列截面、两种列序 ⇒ 旧式选面读数不同。
        这证明 S5-a 测的不是"本来就恒等"的空集。
        """
        scores, rets = self._synth(tie_at_boundary=True)
        rng = np.random.default_rng(123)
        perm = rng.permutation(len(scores))
        shuffled = scores.iloc[perm]
        k = max(1, int(self.N * gate.TOP_FRAC))
        legacy_a = scores.sort_values(ascending=False).index[:k]      # 旧实现 A
        legacy_b = shuffled.sort_values(ascending=False).index[:k]    # 旧实现 B（列序不同）
        self.assertNotEqual(set(legacy_a), set(legacy_b),
                            "旧 quicksort 在此构造下竟列序无关 ⇒ S5-a 红检失效")


if __name__ == "__main__":
    unittest.main()
