"""解禁列量纲契约（2026-09-18）

审查发现：`kg_unlock_90d_ratio` 的分母写成 `circ_mv * 1e3 / close`，而 `daily_basic.circ_mv`
的单位是**万元**（实测：600519/000001/601398/000895 四只票 `total_mv×1e4/close` 与真实总股本
逐一对上）⇒ 流通股本算小 10 倍 ⇒ 整列放大 10 倍。门禁按**逐截面排序**判决，全局常数不改排序
（第五轮 -0.72pp / t=-1.57 与"0 过闸"结论不受影响），但产物数值口径是错的。

这条两头钉：① 源码必须用 ×1e4；② 产物必须与"用原始表按万元→元重算"逐只对齐。

运行: python -m pytest tests/test_kg_unlock_units.py -q
"""
import sqlite3
import sys
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import pandas as pd  # noqa: E402

DB = BASE / "data" / "neweco.db"
PANEL = BASE / "data" / "kg" / "kg_features.parquet"
SRC = BASE / "scripts" / "kg_materialize.py"


class TestUnlockUnits(unittest.TestCase):
    def test_source_converts_wan_yuan_not_thousand(self):
        line = next(l for l in SRC.read_text(encoding="utf-8").splitlines()
                    if "circ_mv" in l and "close" in l and "*" in l)
        self.assertIn("1e4", line, "circ_mv 是万元 ⇒ 必须 ×1e4（×1e3 = 流通股本小 10 倍）")
        self.assertNotIn("1e3", line, "×1e3 是那次 10 倍量纲错误的原样")

    def test_artifact_matches_fresh_recomputation(self):
        """001289.SZ @20241231：分子=未来 90 天解禁股数，分母=circ_mv(万元)→元/收盘价。"""
        code, anchor = "001289.SZ", "20241231"
        panel = pd.read_parquet(PANEL)
        row = panel[(panel["ts_code"] == code) & (panel["anchor"] == anchor)]
        self.assertEqual(len(row), 1, "样例行不见了（锚点或股票池变了就换样例）")
        at = pd.Timestamp(anchor)
        con = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
        try:
            num = con.execute(
                "SELECT SUM(release_shares) FROM restricted_release "
                "WHERE ts_code=? AND release_date>? AND release_date<=?",
                (code, at.strftime("%Y%m%d"),
                 (at + pd.Timedelta(days=90)).strftime("%Y%m%d"))).fetchone()[0]
            circ_mv = con.execute(
                "SELECT circ_mv FROM daily_basic WHERE ts_code=? AND trade_date=?",
                (code, anchor)).fetchone()[0]
            close = con.execute(
                "SELECT close FROM daily WHERE ts_code=? AND trade_date=?",
                (code, anchor)).fetchone()[0]
        finally:
            con.close()
        expected = num / (circ_mv * 1e4 / close)
        got = float(row["kg_unlock_90d_ratio"].iloc[0])
        self.assertAlmostEqual(got, expected, delta=expected * 1e-6,
                               msg=f"产物 {got} ≠ 重算 {expected}（差 {got/expected:.3g} 倍 ⇒ 量纲）")

    def test_column_scale_is_plausible(self):
        """整列量级守卫：×1e3 那版中位数 0.514，×1e4 修正后 ≈0.051。"""
        s = pd.read_parquet(PANEL)["kg_unlock_90d_ratio"].dropna()
        self.assertLess(s.median(), 0.15,
                        f"中位数 {s.median():.3f} 偏大 ⇒ 分母量纲又回到 ×1e3 了")


if __name__ == "__main__":
    unittest.main()
