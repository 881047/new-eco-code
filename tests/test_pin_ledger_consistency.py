"""pin 台账一致性守卫（2026-09-13，独立审查 §4.4 的防复发）

事故原型：四个脚本把**当时的** pin（16.59% / 15.42% / 12.56% / 11.05%）写成"现行 pin"，
而 pin 的值是"改动有没有动基线"的**唯一判据** ⇒ 带着过期 pin 复核会得出反向结论。

本守卫只抓这一类**具体**的漂移（不做全文措辞审查，避免噪音）：
  文件里同时出现 **"现行 pin" 字样** 与**过期数值**，却**没有引用当前 pin 标签**
  （`config.yaml: backtest_run` 的值）⇒ 判失败。

另外钉住两条事实：① 台账文件里的"现役 pin"数值与标签一致；② 当前标签确实存在于 config。
"""
import re
import sys
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

STALE_VALUES = ("16.59", "15.42", "12.56", "11.05",
                # 2026-09-20 三补二十八：`20260912_limitquant` 起从"现行"退为历史档
                # （报告腿换成全收益指数 H00906.CSI ⇒ 生产窗超额 0.1882 → 0.1660）
                "18.82", "0.1882",
                # 2026-09-28 审查: 第 5/6 次重钉退役的数值此前未追加, 守卫对最近两轮
                # 换 pin"失明"(README chase2 头块漂移能存活至今正是这个原因)
                "22.62", "19.40", "17.82", "14.97",       # 第 5 次退役(20260922_trbench)
                "20.09", "17.04", "9.93", "8.60",         # 第 6 次退役(20260926_chase2)
                "35.51",                                  # 第 7 次退役(20260928_excl5mdd: 全史日频 -35.51%→-35.45%)
                # 2026-09-28 第 8 次退役(20260928_exe): 翻 FILL_AT_OPEN + OVERLAP_REBAL_COST
                # prod 满仓 28.32→31.68、系统 22.12→29.13、回撤 -25.84→-23.63/-22.10→-18.17、
                # 日频 -34.82→-33.58; full 满仓 16.37→18.65、系统 13.80→18.77、
                # 回撤 -43.36→-38.19/-34.41→-32.14
                "28.32", "22.12", "25.84", "22.10", "34.82",
                "16.37", "13.80", "43.36", "34.41", "35.45")
# 2026-09-28 审查: 触发词扩为三种同义写法 —— README 用"当前基线 pin"曾绕开守卫
PIN_LABELS = ("现行 pin", "现行pin", "当前基线 pin", "当前基线pin", "现役 pin", "现役pin")
SCAN_DIRS = ("app", "scripts", "tests")
SCAN_EXTRA = ("README.md",)
# 2026-09-28(第 8 次重钉): `plans/` 与 `experiments/` 从扫描范围**移除**。
# 理由: 它们是**冻结的历史证据** —— 预注册在跑数前冻结、实验产物是已判决的留痕,
# 项目纪律是"证据文件只增不改"(改字节 = 动证据)。而这两处的文件名/正文里普遍写着
# "当时的现行 pin 22.12%" 这类表述, 那在**当时**是正确的。守卫的本意是抓"**现役**
# 文档漂移"(第 6 次重钉漏更 README 那个流程性根因), 不是要求历史记录随每次换 pin
# 回改。故扫描面收窄到现役位置: 代码(app/scripts/tests) + README。
# ⚠️ 代价: 若有人新建 plans 文档时误抄过期 pin, 本守卫抓不到 —— 已知的盲区, 接受。


def _current_pin_tag() -> str:
    txt = (BASE / "config.yaml").read_text(encoding="utf-8")
    m = re.search(r"^backtest_run:\s*(\S+)", txt, re.M)
    return m.group(1) if m else ""


class TestPinLedger(unittest.TestCase):
    def test_config_has_a_pin_tag(self):
        self.assertTrue(_current_pin_tag(), "config.yaml 里找不到 backtest_run 标签")

    def test_no_stale_pin_labelled_as_current(self):
        tag = _current_pin_tag()
        files = []
        for d in SCAN_DIRS:
            files += [p for p in (BASE / d).rglob("*") if p.suffix in (".py", ".md")]
        files += [BASE / f for f in SCAN_EXTRA if (BASE / f).exists()]
        bad = []
        for p in files:
            if p.name == Path(__file__).name:
                continue          # 本文件自己就写着那些历史数值（要检查它们），跳过自指
            t = p.read_text(encoding="utf-8", errors="ignore")
            if not any(w in t for w in PIN_LABELS):
                continue
            if not any(v in t for v in STALE_VALUES):
                continue
            if tag and tag in t:
                continue          # 引用了当前标签 ⇒ 已经交代清楚，放过
            rel = p.relative_to(BASE).as_posix()
            bad.append(rel)
        self.assertEqual(bad, [],
                         f"这些文件把过期 pin 数值与'现行 pin'并列且未引用当前标签({tag}): "
                         f"{bad} —— 请改成'当时的 pin（历史值）'并注明现役值")


if __name__ == "__main__":
    unittest.main()
