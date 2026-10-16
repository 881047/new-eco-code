"""批量刷新历史脚本里的「时效注记」(第 8 次重钉 20260928_excl5mdd → 20260928_exe)

为什么要有这个脚本
------------------
`tests/test_pin_ledger_consistency.py` 会扫描现役代码目录, 抓"把过期 pin 数值与
'现行/现役 pin' 并列"的文件。第 7 次重钉时为 6 个历史实验脚本加了「时效注记」
(形如 "现役 pin 已重钉至 20260928_excl5mdd(28.32%/22.12%...)"), 但**注记本身**
在第 8 次重钉后过期 ⇒ 守卫重新报红。

处理原则: 这些脚本是**历史实验记录**, 正文里"当时的 pin 读数"不动(证据只增不改);
只刷新那句"现役 pin 已重钉至 X"的注记, 让它指向当前档位。

⚠️ 编码: 其中 3 个文件被 grep 判为 binary(含非 UTF-8 字节), 故统一按
"先 utf-8, 失败退回 gbk, 再失败 latin-1" 读、并以**读到的那种编码**写回,
不强行统一编码(改写历史文件的编码 = 动证据)。

用法
----
  ./.venv/Scripts/python.exe -X utf8 scripts/oneoff/refresh_pin_staleness_notes_20260928.py
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ONE = ROOT / "scripts" / "oneoff"

TARGETS = [
    "ab_const_n_5v6.py",
    "exp_capital_real_fees.py",
    "exp_fee_floor.py",
    "exp_label_style.py",
    "exp_null_random.py",
    "exp_start_2019.py",
]

# 旧注记 → 新注记(只换"现役 pin 已重钉至"那半句; 前半句"本脚本写作时的现役 pin"
# 是历史事实, 保留不动)
RE_OLD_TAG = re.compile(r"20260928_excl5mdd")
NEW_TAG = "20260928_exe"
RE_OLD_NUMS = re.compile(r"\(28\.32%/22\.12%,?\s*全史日频\s*-35\.45%\)")
NEW_NUMS = "(31.68%/29.13%, 全史日频 -33.58%)"


def _read(path: Path):
    for enc in ("utf-8", "gbk", "latin-1"):
        try:
            return path.read_text(encoding=enc), enc
        except (UnicodeDecodeError, LookupError):
            continue
    return None, None


def main() -> int:
    print("=" * 84)
    print("刷新历史脚本的时效注记: 20260928_excl5mdd → 20260928_exe")
    print("=" * 84)
    changed = 0
    for name in TARGETS:
        p = ONE / name
        if not p.exists():
            print(f"  跳过(不存在) {name}")
            continue
        txt, enc = _read(p)
        if txt is None:
            print(f"  !! 读不出编码 {name}")
            continue
        n1 = len(RE_OLD_TAG.findall(txt))
        n2 = len(RE_OLD_NUMS.findall(txt))
        if n1 == 0 and n2 == 0:
            print(f"  无命中 {name} (enc={enc})")
            continue
        new = RE_OLD_TAG.sub(NEW_TAG, txt)
        new = RE_OLD_NUMS.sub(NEW_NUMS, new)
        if new == txt:
            print(f"  无变化 {name}")
            continue
        p.write_text(new, encoding=enc)
        changed += 1
        print(f"  ✅ {name:<34} (enc={enc}) tag×{n1} nums×{n2}")

    print("\n" + "=" * 84)
    print(f"刷新 {changed} 个文件。下一步: 跑 tests/test_pin_ledger_consistency.py 复检")
    print("=" * 84)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
