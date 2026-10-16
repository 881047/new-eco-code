"""因子六块分组的守卫（2026-09-22，D 案）

分组**不是**输入层改造（C 案已判 effect_indistinguishable，输入层封死），它只服务
监控与归因。所以它唯一需要被钉死的是"这张表没有悄悄漂移"：
  · 恰好覆盖 31 个生产因子、无重叠；
  · 成员/优先级与 C 案同一份映射（换成员 = 换监控口径，块级读数的历史序列会断）；
  · `STYLE_BLOCK` 覆盖 `attrib.STYLES` 的全部风格列（漏一个 ⇒ 归因恒等式把那份贡献吞掉）。

红检方向：往某个块里多塞/少写一个因子 ⇒ `test_covers_exactly_the_31_production_factors` 当场红。
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.models.daily_score import ALL_FACTORS        # noqa: E402
from app.weekly import attrib as wk_attrib            # noqa: E402
from app.weekly import blocks as wk_blocks            # noqa: E402


class TestBlockMapping(unittest.TestCase):

    def test_covers_exactly_the_31_production_factors(self):
        v = wk_blocks.validate(ALL_FACTORS)
        self.assertEqual(v["n_factors"], 31, v)
        self.assertEqual(v["duplicates"], [], "一个因子只能属于一个块")
        self.assertEqual(v["missing"], [], f"有生产因子没进任何块: {v['missing']}")
        self.assertEqual(v["extra"], [], f"块里有不存在的因子: {v['extra']}")
        self.assertTrue(v["covers_production"])

    def test_members_match_the_d_plan(self):
        """与 `plans/factor-block-monitoring-d-20260922.md` §二 的表逐格对齐。"""
        self.assertEqual(wk_blocks.BLOCK_MEMBERS, {
            "B1": ("size", "amount_log", "illiq"),
            "B2": ("vol_20", "vol_60", "ret_max20", "turnover_20", "skew_20",
                   "beta_250", "turnover_chg"),
            "B3": ("mom_20", "mom_60", "mom_120", "mom_250", "high_52w", "low_52w",
                   "bias_60"),
            "B4": ("rev_5", "bias_20"),
            "B5": ("ep", "bp", "dv_ttm", "roe", "netprofit_yoy", "or_yoy",
                   "grossprofit_margin", "eps_yoy", "debt_to_assets"),
            "B6": ("lhb_net20", "lhb_org_net20", "lhb_cnt20"),
        })
        self.assertEqual(wk_blocks.BLOCK_PRIORITY,
                         {"B1": "P0", "B2": "P0", "B3": "P0", "B4": "P0",
                          "B5": "P1", "B6": "P2"})
        self.assertEqual(wk_blocks.BLOCK_KEYS, ("B1", "B2", "B3", "B4", "B5", "B6"))

    def test_only_p0_blocks_are_reported_and_watched(self):
        """D 案定死的分级：P0 进 doctor 且报警上报；P1/P2 只记录。"""
        self.assertEqual(wk_blocks.DOCTOR_BLOCKS, ("B1", "B2", "B3", "B4"))
        self.assertEqual(wk_blocks.ALERT_PRIORITIES, ("P0",))

    def test_block_of_roundtrip(self):
        for f in ALL_FACTORS:
            self.assertIn(wk_blocks.block_of(f), wk_blocks.BLOCK_KEYS, f)
        self.assertEqual(wk_blocks.block_of("一个不存在的因子"), "")

    def test_style_block_covers_every_attrib_style(self):
        """漏一个风格列 ⇒ 那部分贡献在块级分解里**凭空消失**（恒等式会把它塞进"未归类"）。

        这不是"少报一行"，是让"某块贡献为 0"看起来像"那块没用"。B6 是本口径**已知**的
        例外（事件类不在 Fama-MacBeth 风格集内），所以断言的是"风格列全部有归属"。
        """
        unmapped = [s for s in wk_attrib.STYLES if s not in wk_blocks.STYLE_BLOCK]
        self.assertEqual(unmapped, [], f"有风格列没映射到块: {unmapped}")
        for s, b in wk_blocks.STYLE_BLOCK.items():
            self.assertIn(b, wk_blocks.BLOCK_KEYS, s)

    def test_b6_has_no_style_column_and_says_so(self):
        """B6 在归因里恒为 0 —— 这个"0"必须与"它没贡献"区分开（docstring 里写明了）。"""
        self.assertNotIn("B6", wk_blocks.STYLE_BLOCK.values())
        self.assertIn("Fama-MacBeth", wk_blocks.__doc__ or "")


if __name__ == "__main__":
    unittest.main()
