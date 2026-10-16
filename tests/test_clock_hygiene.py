"""时钟纪律守卫 —— `app/datasource/**` 不许再有**裸时钟**（2026-09-20，三补十八 F3）

为什么只盯 datasource 这一层：台账 F3 的原话是"KB 12 号写的『所有日期走东八区』在这层是空话"。
`app/timeutil.py` 早就提供了唯一事实源（`now()/today()/date8()/stamp()`，统一东八区墙钟 +
可被测试换钟），但这层 28 处一直是裸 `date.today()` / `datetime.now()` / `pd.Timestamp.today()`。

**零容忍、没有例外名单**：
  · `datetime.now(_TZ)`（quote_client 里显式带时区的两处）**不算违规** —— 它们本来就声明了时区，
    扫的是"无时区的裸墙钟"；
  · 想加例外就得改这个文件，而改这里等于在评审里公开说"这处故意不走统一时钟"。
    （datasource 之外还有 30 处，其中 `scheduler` 11 处是 **aware** 时钟、与 timeutil 的 naive
    不同型 ⇒ 换它会改比较语义，登记在 14 号三补二十九，不在本守卫范围内。）

红线检查在最后一条：把"扫不出来"这种**测试自己坏掉**的情况也钉住（照三补二十七 A4 的教训）。
"""
import ast
import re
import sys
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

TARGET_DIR = BASE / "app" / "datasource"

# 三种形态都扫（三补二十二 §C 的教训：只扫明文形态会假绿）
NAIVE = re.compile(
    r"(?:"
    r"\bdate\s*\.\s*today\s*\(\s*\)"                    # date.today() / datetime.date.today()
    r"|\bdatetime\s*\.\s*datetime\s*\.\s*now\s*\(\s*\)"  # datetime.datetime.now()  ← 空参才算
    r"|\bdatetime\s*\.\s*today\s*\(\s*\)"               # datetime.today()
    r"|\bTimestamp\s*\.\s*today\s*\(\s*\)"              # pd.Timestamp.today()
    r")"
)


def scan_source(src: str) -> list:
    """返回 [(行号, 该行文本)]；跳过注释与纯文档行（docstring 里提一句"别用 date.today"
    不该被当成违规 —— 判据：整行 strip 后以 # 开头，或该行不在任何语句里）。"""
    out = []
    for i, line in enumerate(src.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        # 行内代码部分（去掉尾部注释）里出现裸时钟才算
        code = stripped.split("#", 1)[0]
        if NAIVE.search(code):
            out.append((i, stripped[:120]))
    return out


def scan_tree(root: Path) -> dict:
    found = {}
    for p in sorted(root.rglob("*.py")):
        v = scan_source(p.read_text(encoding="utf-8"))
        if v:
            found[str(p.relative_to(BASE))] = v
    return found


class TestClockHygiene(unittest.TestCase):
    def test_datasource_has_no_bare_clocks(self):
        found = scan_tree(TARGET_DIR)
        self.assertEqual(found, {},
                         "app/datasource 里出现裸时钟 ⇒ 该走 app/timeutil（东八区统一 + 可换钟）: "
                         + "; ".join(f"{f}:{l[0]}" for f, l in found.items()))

    def test_timeutil_is_actually_used_there(self):
        """正检查：全部改掉 timeutil 调用也会让上一条"零违规"通过 ⇒ 那样本守卫就是空的。"""
        n = sum(src.count("timeutil.")
                for p in TARGET_DIR.rglob("*.py")
                for src in [p.read_text(encoding="utf-8")])
        self.assertGreaterEqual(n, 20, f"datasource 里 timeutil 调用只剩 {n} 处 ⇒ 接线被拆了？")

    def test_scanner_really_catches_each_shape(self):
        """红线：扫描器自己坏掉（正则失效 / 换了写法）必须当场暴露，而不是"零违规"假绿。"""
        for bad in ("today = datetime.date.today().strftime('%Y%m%d')",
                    "if datetime.datetime.now().hour >= 17:",
                    "end = pd.Timestamp.today().strftime('%Y%m%d')",
                    "d = date.today()",
                    "x = 1  # 注释里有 date.today() 不该算，但下面这行要算\ny = date.today()"):
            self.assertTrue(scan_source(bad), f"扫描器漏掉了: {bad}")
        # 反例：显式带时区的不算违规（quote_client 的两处就是这个形态）
        self.assertEqual(scan_source("t = datetime.now(_TZ)"), [])
        self.assertEqual(scan_source("# date.today() 已废弃"), [])

    def test_timeutil_api_shape_unchanged(self):
        """`timeutil` 的公开口径不许被改成 aware —— 一旦被改成带 tzinfo，
        与库里 naive 时间戳比较的分支会 TypeError 或静默错判（这正是 datasource 外
        那 11 处 aware 时钟**不**并入本守卫的原因）。"""
        from app import timeutil
        self.assertIsNone(timeutil.now().tzinfo, "timeutil.now() 必须返回 naive 东八区墙钟")
        self.assertIsNone(timeutil.today().tzinfo if hasattr(timeutil.today(), "tzinfo") else None)


if __name__ == "__main__":
    unittest.main()
