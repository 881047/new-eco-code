"""费率常量的**单一真源**守卫（2026-09-24）

## 为什么加这一条

`app/portfolio/fees.py::_DEFAULTS` 与 `app/config.py::DEFAULT_CONFIG["fees"]`
**各写了一份同样的 5 个费率常量**（佣金 / 最低佣金 / 印花税 / 减半前印花税 / 过户费）。
这是"单一真源"纪律（`01_纪律与不变量.md` #8）下的**潜在漂移源**：
两处一旦不同，就会出现"回测按 A 算、fail-safe 回退按 B 算"的静默口径分叉。

实测（2026-09-24 深度审查）：**两处当前完全一致（5 键无漂移）**，
但**全仓没有任何一条测试在比对它们** —— 也就是说"一致"这件事当时**无人守卫**，
只是碰巧。本文件把"碰巧"变成"被钉住"。

## 守四条

1. **键集一致**（对称比较，任一侧多/少键都红）；
2. **逐键数值一致**（`assertEqual`，不是 `AlmostEqual` —— 费率是精确的小数常量）；
3. **合并语义**：`fee_params({})` 必须逐键等于 `_DEFAULTS`（缺 config 时的 fail-safe）；
   且 config 给合法值时覆盖、给非法值时回退（后者已有 `tests/test_fees.py` 覆盖，此处只钉前者）；
4. **棘轮**：`app/` 与 `scripts/` 下**不许出现第三张硬编码费率表**
   （即除 `fees.py` 与 `config.py` 外，不得再有 `"commission_rate": <数字>` 这种字面量）。

红检：把 `fees._DEFAULTS["commission_rate"]` 改成 `0.0003` ⇒ 第 2 条当场红。
"""
import re
import sys
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from app import config as cfg_mod                # noqa: E402
from app.portfolio import fees as fees_mod       # noqa: E402

#: 允许出现硬编码费率表的**唯一两处**
ALLOWED_FEE_TABLES = {"app/portfolio/fees.py", "app/config.py"}

#: 匹配 `"commission_rate": 0.00025` 这类**字面量赋值**（不含读取/比较）
_FEE_LITERAL_RE = re.compile(r"""["']commission_rate["']\s*:\s*[0-9]""")


class TestFeeDefaultsSingleSource(unittest.TestCase):
    def setUp(self):
        self.a = dict(fees_mod._DEFAULTS)
        self.b = dict(cfg_mod.DEFAULT_CONFIG.get("fees") or {})

    def test_key_sets_are_identical(self):
        self.assertEqual(
            set(self.a), set(self.b),
            f"两处费率键集不同 —— 只在一边有的键会静默走另一边的默认值。\n"
            f"  fees._DEFAULTS      = {sorted(self.a)}\n"
            f"  config.DEFAULT_CONFIG = {sorted(self.b)}")

    def test_values_are_identical(self):
        for k in sorted(set(self.a) & set(self.b)):
            with self.subTest(key=k):
                self.assertEqual(
                    self.a[k], self.b[k],
                    f"费率 {k} 在两处不一致：fees._DEFAULTS={self.a[k]!r} "
                    f"vs config.DEFAULT_CONFIG={self.b[k]!r} —— "
                    f"回测与 fail-safe 会按不同口径算成本")

    def test_nonempty(self):
        """防"两边都空"的假绿（键集与数值都相等，但实际没守到任何东西）。"""
        self.assertGreaterEqual(len(self.a), 5, "费率表不该少于 5 键（佣金/最低佣金/印花税×2/过户费）")
        self.assertIn("commission_rate", self.a)


class TestFeeParamsMerge(unittest.TestCase):
    def test_empty_config_falls_back_to_defaults(self):
        """无 config / 空 config ⇒ 逐键等于 `_DEFAULTS`（fail-safe 不改成本口径）。"""
        self.assertEqual(fees_mod.fee_params(None), fees_mod._DEFAULTS)
        self.assertEqual(fees_mod.fee_params({}), fees_mod._DEFAULTS)

    def test_config_overrides_defaults(self):
        got = fees_mod.fee_params({"fees": {"commission_rate": 0.0001, "min_commission": 0.0}})
        self.assertAlmostEqual(got["commission_rate"], 0.0001)
        self.assertAlmostEqual(got["min_commission"], 0.0)
        # 未覆盖的键必须仍是默认值
        self.assertAlmostEqual(got["stamp_tax"], fees_mod._DEFAULTS["stamp_tax"])


class TestNoThirdFeeTable(unittest.TestCase):
    """棘轮：不许出现第三张硬编码费率表。

    起因：费率是"钱的口径"，多一张表 = 多一个静默分叉点。真要再加，
    应当**扩 `_DEFAULTS` / `DEFAULT_CONFIG` 这两处之一**，而不是在别处另起一份。

    ⚠️ **`scripts/oneoff/` 有意排除**（2026-09-24 实测后定的范围）：
    那里是**证据目录**（不得删除、不许批量搬家），其中的脚本会把费率写进**实验臂定义**，
    而按 05 号规矩"数值常量是那些实验的**冻结判据**、一个字不改"。
    实测样本：`scripts/oneoff/exp_fee_floor.py` 的 `ARMS` 里写死
    `{"min_commission": 0.0}`（免五臂）与全零（摩擦上限臂）—— 那是该实验的**判据本身**，
    不是"第三张生产费率表"。把它改成引用 `_DEFAULTS` 反而会**篡改冻结判据**。
    ⇒ 排除是**有理由的**，不是放水：`app/` 与 `scripts/`（除 oneoff）仍全额扫描。
    """

    SCAN_DIRS = ("app", "scripts")
    #: 证据目录：常量即冻结判据，不参与本棘轮
    EXCLUDE_PREFIXES = ("scripts/oneoff/",)

    def test_only_known_files_hardcode_fees(self):
        offenders = []
        for d in self.SCAN_DIRS:
            root = BASE / d
            if not root.is_dir():
                continue
            for p in sorted(root.rglob("*.py")):
                if "__pycache__" in p.parts:
                    continue
                rel = p.relative_to(BASE).as_posix()
                if rel in ALLOWED_FEE_TABLES or rel.startswith(self.EXCLUDE_PREFIXES):
                    continue
                txt = p.read_text(encoding="utf-8", errors="replace")
                if _FEE_LITERAL_RE.search(txt):
                    offenders.append(rel)
        self.assertEqual(
            offenders, [],
            f"出现了第三张硬编码费率表: {offenders} —— "
            f"请改为引用 `fees._DEFAULTS` 或 `config.DEFAULT_CONFIG['fees']`")


if __name__ == "__main__":
    unittest.main()
