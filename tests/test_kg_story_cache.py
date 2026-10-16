"""拥挤分索引的缓存契约（2026-09-18）

为什么：`get_stock_story` 原先每次调用把 `data/kg/excluder_scores.jsonl` 整文件读**两遍**
（103 只持仓 ≈ 31 万次 `json.loads`，端点实测 1.08 s）。改成模块级、按 mtime 失效的锚点
索引后，唯一容易写错的地方就是"文件变了但缓存没失效" —— 这条钉住它。

运行: python -m pytest tests/test_kg_story_cache.py -q
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from app import kg_story  # noqa: E402


def _write(p: Path, rows: list) -> None:
    p.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                 encoding="utf-8")


class TestCrowdIndexCache(unittest.TestCase):
    def test_index_parses_and_reloads_on_change(self):
        with tempfile.TemporaryDirectory() as td:
            sp = Path(td) / "scores.jsonl"
            _write(sp, [{"anchor": "20240131", "ts_code": "000001.SZ", "crowd": 5},
                        {"anchor": "20240229", "ts_code": "000001.SZ", "crowd": 7}])
            anchors, by_anchor = kg_story._crowd_index(sp)
            self.assertEqual(anchors, ["20240131", "20240229"])
            self.assertEqual(by_anchor["20240131"]["000001.SZ"]["crowd"], 5)

            # 再读一次走缓存：结果必须一致
            _a2, by_anchor2 = kg_story._crowd_index(sp)
            self.assertEqual(by_anchor2["20240229"]["000001.SZ"]["crowd"], 7)

            # 文件被重写（mtime 抬一档）⇒ 必须重新加载，否则报的是过期拥挤分
            _write(sp, [{"anchor": "20240131", "ts_code": "000001.SZ", "crowd": 9}])
            st = sp.stat()
            os.utime(sp, (st.st_atime, st.st_mtime + 5))
            anchors3, by_anchor3 = kg_story._crowd_index(sp)
            self.assertEqual(anchors3, ["20240131"])
            self.assertEqual(by_anchor3["20240131"]["000001.SZ"]["crowd"], 9)

    def test_missing_file_is_empty_not_crash(self):
        with tempfile.TemporaryDirectory() as td:
            anchors, by_anchor = kg_story._crowd_index(Path(td) / "nope.jsonl")
            self.assertEqual(anchors, [])
            self.assertEqual(by_anchor, {})


if __name__ == "__main__":
    unittest.main()
