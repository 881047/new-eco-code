r"""三模型选股榜 + 交集（app/model_picks.py）的契约测试（2026-10-08）。

钉三件事：
1. `_intersect` 纯函数语义（triple/pairs/counts 的键与数值，改口径必须过这里）；
2. 模型键序与 MODEL_SCHEDULE/MODEL_ORDER 三方一致（同 test_web_static_contract ⑱ 的守卫面）;
3. build_picks 聚合：单源故障隔离 + ECO-W2 sidecar 自校验位透传（真实文件，只读）。
"""
import unittest
from unittest import mock

from app import all_accounts as aa
from app import model_picks as mp


class TestIntersect(unittest.TestCase):
    def test_triple_and_pairs_counts(self):
        blocks = {
            "NEW ECO": {"ok": True, "picks": [{"code": "A", "rank": 1}, {"code": "B", "rank": 2},
                                              {"code": "C", "rank": 3}]},
            "Final": {"ok": True, "picks": [{"code": "B", "rank": 1}, {"code": "C", "rank": 2},
                                            {"code": "D", "rank": 3}]},
            "W2": {"ok": True, "picks": [{"code": "C", "rank": 1}, {"code": "A", "rank": 2}]},
        }
        out = mp._intersect(blocks)
        self.assertEqual([t["code"] for t in out["triple"]], ["C"])
        self.assertEqual(out["triple"][0]["ranks"], {"NEW ECO": 3, "Final": 2, "W2": 1})
        self.assertEqual(out["counts"]["triple"], 1)
        self.assertEqual(out["counts"]["pair:NEW ECO ∩ Final"], 2)
        self.assertEqual(out["counts"]["pair:NEW ECO ∩ W2"], 2)
        self.assertEqual(out["counts"]["pair:Final ∩ W2"], 1)

    def test_failed_model_is_excluded(self):
        blocks = {
            "NEW ECO": {"ok": False, "picks": [], "error": "x"},
            "Final": {"ok": True, "picks": [{"code": "A", "rank": 1}]},
            "W2": {"ok": True, "picks": [{"code": "A", "rank": 2}]},
        }
        out = mp._intersect(blocks)
        # 双模型可用 ⇒ 无 triple（只算两两），NEW ECO 不参与
        self.assertEqual(out["triple"], [])
        self.assertEqual(out["counts"]["pair:Final ∩ W2"], 1)
        self.assertNotIn("pair:NEW ECO ∩ Final", out["counts"])

    def test_model_keys_align_with_model_schedule(self):
        """三方同源守卫：model_picks 键序 == all_accounts.MODEL_SCHEDULE 键序 == 前端 MODEL_ORDER。"""
        self.assertEqual(list(mp.MODEL_KEYS), list(aa.MODEL_SCHEDULE.keys()))


class TestBuildPicks(unittest.TestCase):
    def test_single_source_failure_isolated(self):
        """单模型抛错 → 该模型 ok=False + error，其余照常（与 all_accounts 同纪律）。"""
        with mock.patch.object(mp, "_neweco_picks", side_effect=RuntimeError("boom")):
            out = mp.build_picks(force=True)
        self.assertFalse(out["models"]["NEW ECO"]["ok"])
        self.assertIn("boom", out["models"]["NEW ECO"]["error"])
        self.assertIn("Final", out["models"])
        self.assertIn("W2", out["models"])
        self.assertIn("counts", out["intersection"])

    def test_real_sources_aggregate(self):
        """真实文件聚合（只读）：ECO-W2 sidecar 必须已存在且自校验通过；
        NEW ECO daily_score 有数据；fterm 依赖 20:05 档 EMA 状态（换版重置后为空属正常，
        只要求结构完整不要求 ok）。"""
        out = mp.build_picks(force=True)
        self.assertEqual(set(out["models"]), {"NEW ECO", "Final", "W2"})
        eco = out["models"]["W2"]
        self.assertTrue(eco["ok"], f"ECO-W2 sidecar 未就绪或未过自校验: {eco.get('error')}")
        self.assertEqual(eco["n"], 100)
        self.assertTrue(eco["verified"])
        ne = out["models"]["NEW ECO"]
        self.assertTrue(ne["ok"], f"NEW ECO daily_score 空: {ne.get('error')}")
        self.assertLessEqual(ne["n"], 100)
        for p in eco["picks"][:5]:
            self.assertIn("rank", p)
            self.assertIn("code", p)
        # 交集结构（即便为空也要有键）
        self.assertIn("triple", out["intersection"])
        self.assertIn("pairs", out["intersection"])


if __name__ == "__main__":
    unittest.main()
