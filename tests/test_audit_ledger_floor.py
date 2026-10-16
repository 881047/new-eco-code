"""审计"账本完整性"的**下限守卫**（2026-09-14，外部复核 O13 残余）

为什么：原来只查恒等式 `现金 + 利息 + 市值 = 净值` —— **全 0 也满足**（账本被清空、或写坏成 0
时会判"通过"）。现在补两道：净值必须 > 0；三个分量不能同时为 0。这是**钱**的不变量，
"看不见/全 0"不许等于"通过"（与 07 #41 同一条纪律）。

运行: python -m pytest tests/test_audit_ledger_floor.py -q
"""
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

_spec = importlib.util.spec_from_file_location("audit_eng", BASE / "scripts" / "audit_engineering.py")
ae = importlib.util.module_from_spec(_spec)
sys.modules["audit_eng"] = ae
_spec.loader.exec_module(ae)


def _ledger(**over) -> dict:
    st = {"capital": 1_000_000.0, "cash": 130_885.0, "positions": {}, "trades": [],
          "interest": 12.5,
          "nav_history": [{"date": "20260914", "nav": 1_000_000.0, "invested": 869_102.5,
                           "cash": 130_885.0, "interest": 12.5, "positions": {}}]}
    st.update(over)
    return st


class TestLedgerFloor(unittest.TestCase):
    def _run(self, payload) -> tuple:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "ledger.json"
            p.write_text(json.dumps(payload), encoding="utf-8")
            row = []
            ok = ae.check_ledger(row, path=p)
        return ok, row[0]

    def test_normal_ledger_passes(self):
        ok, r = self._run(_ledger())
        self.assertTrue(ok, r)

    def test_all_zero_ledger_is_rejected(self):
        """全 0 满足恒等式但不合法 ⇒ 必须判 ✗（原来会判"通过"）。"""
        zeros = {"cash": 0.0, "interest": 0.0, "nav_history":
                 [{"date": "20260914", "nav": 0.0, "invested": 0.0}]}
        ok, r = self._run(_ledger(**zeros))
        self.assertFalse(ok, f"全 0 账本被放过了: {r}")
        self.assertIn("不合法", r[2])

    def test_identity_gap_is_still_caught(self):
        bad = _ledger()
        bad["nav_history"][0]["nav"] = 900_000.0        # 故意差 10 万
        ok, r = self._run(bad)
        self.assertFalse(ok, r)

    def test_missing_ledger_is_not_a_failure(self):
        row = []
        ok = ae.check_ledger(row, path=Path(tempfile.gettempdir()) / "nope_ledger.json")
        self.assertTrue(ok, "还没到首个执行档时，没有账本是正常状态")


if __name__ == "__main__":
    unittest.main()
