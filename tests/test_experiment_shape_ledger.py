"""实验构型台账守卫（2026-09-13，起因见 07 号 #23）

**问题原型**：18 个 100 万档实验脚本都写死 `hyst=0.2`，而生产 `plan.pick_shape(1e6)`
返回 `hold_bonus=0.0` ⇒ 路演数字与留出集读数描述的不是生产在跑的构型，且**没有任何机制会提醒**。

守四条：
  1. **覆盖**：每个 `experiments/05_label/F*.py` 都必须在 `SHAPE_LEDGER.json` 里有条目
     （新脚本必须申报自己的构型 —— 不申报就红）；
  2. **一致**：脚本里 AST 能解析到的构型常量（CAPITAL/TOP_N/BAND/HYST 与 `hyst=` 实参）
     必须与台账逐字段相同（偷改一个数字就红）；
  3. **基线**：台账的 `_production` 必须等于**实时**的 `pick_shape(1e6)` + `weekly.capital`
     （生产构型若变，台账与本文档必须同步改）；
  4. **留痕**：标为 `RESEARCH_HYST02` 的条目，note 里必须写明它偏离生产构型
     （引用其读数须知会）。

运行: python -m unittest tests.test_experiment_shape_ledger -v
"""
import ast
import json
import sys
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from app import config as cfg_mod            # noqa: E402
from app.weekly import plan as wk_plan       # noqa: E402

EXP_DIR = BASE / "experiments" / "05_label"
LEDGER = EXP_DIR / "SHAPE_LEDGER.json"
NAMES = ("CAPITAL", "TOP_N", "BAND", "HYST")


def _scan(path: Path) -> dict:
    """AST 扫描：脚本里写死的构型常量与 `hyst=` 实参（与台账生成器同一套判据）。"""
    tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
    consts, kwargs = {}, set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            tgt = node.targets[0]
            if isinstance(tgt, ast.Tuple) and isinstance(node.value, ast.Tuple):
                names = [t.id for t in tgt.elts if isinstance(t, ast.Name)]
                for n, v in zip(names, node.value.elts):
                    if n in NAMES and isinstance(v, ast.Constant):
                        consts[n] = v.value
            elif isinstance(tgt, ast.Name) and tgt.id in NAMES and isinstance(node.value, ast.Constant):
                consts[tgt.id] = node.value.value
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg == "hyst" and isinstance(kw.value, ast.Constant):
                    kwargs.add(kw.value.value)
    hyst = consts.get("HYST", sorted(kwargs)[0] if kwargs else None)
    return {"capital": consts.get("CAPITAL"), "top_n": consts.get("TOP_N"),
            "band": consts.get("BAND"), "hyst": hyst, "hyst_kwargs": sorted(kwargs)}


class TestShapeLedger(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ledger = json.loads(LEDGER.read_text(encoding="utf-8"))
        cls.files = {p.name: p for p in sorted(EXP_DIR.glob("F*.py"))}

    def test_every_experiment_is_declared(self):
        declared = set(self.ledger["files"])
        actual = set(self.files)
        self.assertEqual(sorted(actual - declared), [],
                         "有实验脚本没在 SHAPE_LEDGER.json 里申报构型（07 号 #23 就是这类静默漂移）")
        self.assertEqual(sorted(declared - actual), [], "台账里有已不存在的脚本，删掉或改名")

    def test_declared_shape_matches_source(self):
        for name, p in self.files.items():
            s, e = _scan(p), self.ledger["files"].get(name)
            if e is None:
                continue                      # 覆盖检查已单独报
            with self.subTest(script=name):
                self.assertEqual(s["capital"], e["shape"]["capital"])
                self.assertEqual(s["top_n"], e["shape"]["top_n"])
                self.assertEqual(s["band"], e["shape"]["band"])
                self.assertEqual(s["hyst"], e["shape"]["hyst"])
                self.assertEqual(s["hyst_kwargs"], e["hyst_kwargs"])

    def test_production_baseline_matches_live_shape(self):
        cfg = cfg_mod.load_config()
        top_n, band, bonus = wk_plan.pick_shape(1_000_000.0, cfg)
        prod = self.ledger["_production"]
        self.assertEqual(prod["capital"], float((cfg.get("weekly") or {}).get("capital")
                                                or cfg.get("capital")))
        self.assertEqual(prod["top_n"], int(top_n))
        self.assertEqual(prod["band"], float(band))
        self.assertEqual(prod["hyst"], float(bonus),
                         "生产构型变了 ⇒ 台账、02/05 号文件与 plan.py 的预期表都要跟着改")

    def test_research_shape_entries_are_documented(self):
        bad = [n for n, e in self.ledger["files"].items()
               if e["kind"] == "RESEARCH_HYST02" and "F52" not in e["note"] and "07" not in e["note"]]
        self.assertEqual(bad, [], f"偏离生产构型的脚本必须写明差多少（见 07 #23）: {bad}")

    def test_no_ledger_entry_claims_production_match_when_hyst_differs(self):
        """kind=MATCHES_PROD 的条目必须真的 hyst=0.0（防止有人改 kind 蒙混过关）。"""
        wrong = [n for n, e in self.ledger["files"].items()
                 if e["kind"] == "MATCHES_PROD" and e["shape"]["hyst"] != 0.0]
        self.assertEqual(wrong, [])

    def test_scanner_actually_reads_values(self):
        """传感器自检：扫描器必须真能读出数值（否则"全 None"会伪装成"全部一致"）。"""
        import tempfile
        base = "CAPITAL, TOP_N, BAND, HYST = 1_000_000.0, 100, 8.0, {h}\n"
        with tempfile.TemporaryDirectory() as td:
            for h, expect in ((0.2, 0.2), (0.0, 0.0)):
                f = Path(td) / f"x{h}.py"
                f.write_text(base.format(h=h), encoding="utf-8")
                s = _scan(f)
                self.assertEqual(s["capital"], 1_000_000.0)
                self.assertEqual(s["top_n"], 100)
                self.assertEqual(s["band"], 8.0)
                self.assertEqual(s["hyst"], expect)
            kw = Path(td) / "kw.py"
            kw.write_text("def f():\n    run(hyst=0.2)\n", encoding="utf-8")
            self.assertEqual(_scan(kw)["hyst_kwargs"], [0.2])
            self.assertEqual(_scan(kw)["hyst"], 0.2)


if __name__ == "__main__":
    unittest.main()
