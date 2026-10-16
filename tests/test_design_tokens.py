r"""设计 token 落地守卫（2026-10-08 批2 新增）。

背景：本仓前端此前积累了 18 种字号、36 处硬编码色、无字重/行高/动效/z-index 阶梯
—— 这是"看着乱"的根因，也是"改 tokens.css 不生效"的原因（视图内写死值绕过 token）。

CI 环境无 node、无截图工具，视觉一致性**没有自动化验证手段**。本文件是唯一可执行的
回归网：用 grep 守卫"不该出现的东西"，防止后续改动把硬编码值又写回来。

约束：
- 唯一允许写裸色值/裸字号的文件是 `src/styles/tokens.css`（token 定义处）。
- `KpiGrid.vue` 的 `compactValue` prop 默认值 "18px" 是**数据**不是样式（视图通过
  :style 传 fontSize），在白名单内。
- 构建产物 `app/web/static/assets/*.js` 不扫描（打包后必然含字面量）。
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
SRC = BASE / "frontend" / "src"

#: 允许出现裸值的文件（token 定义处）
ALLOW_FILES = {"styles/tokens.css"}

#: 逐文件白名单：{相对路径: [(正则, 允许出现次数上限)]}
PER_FILE_ALLOW = {
    # compactValue 的默认值 "18px" 是传给 :style 的数据，非样式声明
    "components/KpiGrid.vue": [(r'"18px"', 1)],
}

HEX_COLOR = re.compile(r"#[0-9a-fA-F]{3,8}\b")
RGB_FUNC = re.compile(r"rgba?\s*\(")
FONT_PX = re.compile(r"font-size\s*:\s*[\d.]+px")


def _iter_src():
    for p in sorted(SRC.rglob("*")):
        if not p.is_file():
            continue
        if p.suffix not in {".vue", ".js", ".css"}:
            continue
        yield p.relative_to(SRC).as_posix(), p


def _strip_allow(rel: str, text: str) -> str:
    """把该文件白名单内允许的片段挖空，剩下的才参与断言。"""
    for pat, _limit in PER_FILE_ALLOW.get(rel, []):
        text = re.sub(pat, "", text)
    return text


class TestNoHardcodedStyleValues(unittest.TestCase):
    """裸色值 / 裸字号只允许出现在 tokens.css。"""

    def test_no_hardcoded_hex_colors_outside_tokens(self):
        offenders = []
        for rel, path in _iter_src():
            if rel in ALLOW_FILES:
                continue
            text = _strip_allow(rel, path.read_text(encoding="utf-8"))
            for m in HEX_COLOR.finditer(text):
                line = text[: m.start()].count("\n") + 1
                offenders.append(f"{rel}:{line} {m.group(0)}")
        self.assertEqual(
            offenders, [],
            "tokens.css 之外出现裸色值 ⇒ 暗色主题会漏改、色板会再分叉。"
            "请改用 var(--xxx)（CSS/模板内联）或 cssVar('--xxx')（JS 字符串内）。"
            f"违规：{offenders}",
        )

    def test_no_hardcoded_rgb_outside_tokens(self):
        offenders = []
        for rel, path in _iter_src():
            if rel in ALLOW_FILES:
                continue
            text = _strip_allow(rel, path.read_text(encoding="utf-8"))
            for m in RGB_FUNC.finditer(text):
                line = text[: m.start()].count("\n") + 1
                offenders.append(f"{rel}:{line}")
        self.assertEqual(
            offenders, [],
            f"tokens.css 之外出现裸 rgb()/rgba() ⇒ 请改 token。违规：{offenders}",
        )

    def test_no_hardcoded_font_size_px(self):
        offenders = []
        for rel, path in _iter_src():
            if rel in ALLOW_FILES:
                continue
            text = _strip_allow(rel, path.read_text(encoding="utf-8"))
            for m in FONT_PX.finditer(text):
                line = text[: m.start()].count("\n") + 1
                offenders.append(f"{rel}:{line} {m.group(0)}")
        self.assertEqual(
            offenders, [],
            "tokens.css 之外出现裸 font-size px ⇒ 字号阶梯形同虚设。"
            "请改用 var(--fs-1..8)。"
            f"违规：{offenders}",
        )


class TestTokenLadderExists(unittest.TestCase):
    """阶梯本身必须齐备（缺一档，视图就会被迫写裸值）。"""

    REQUIRED = [
        "--fs-1", "--fs-2", "--fs-3", "--fs-4", "--fs-5", "--fs-6", "--fs-7", "--fs-8",
        "--fw-regular", "--fw-medium", "--fw-semibold", "--fw-bold",
        "--lh-tight", "--lh-base", "--lh-loose",
        "--shadow-1", "--shadow-2", "--shadow-3",
        "--z-base", "--z-sticky", "--z-overlay", "--z-panel", "--z-toast",
        "--t-fast", "--t-base", "--ease",
        "--success", "--info", "--danger",
        "--focus-color", "--focus-width", "--font-sans",
        # 此前全仓未定义、导致 .acc-card/.stale-badge 走裸 fallback 的两个变量
        "--card-bg", "--amber",
        # 图表侧（charts.js 经 cssVar 读取，已不再维护第二套色板）
        "--chart-blue", "--chart-red", "--chart-green", "--chart-amber",
        "--chart-gray", "--chart-tooltip-bg", "--chart-grid",
        # 7 个账户序列色
        "--series-1", "--series-2", "--series-3", "--series-4",
        "--series-5", "--series-6", "--series-7",
    ]

    def test_ladder_complete(self):
        css = (SRC / "styles" / "tokens.css").read_text(encoding="utf-8")
        missing = [t for t in self.REQUIRED if f"{t}:" not in css]
        self.assertEqual(missing, [], f"tokens.css 缺 token 定义：{missing}")

    def test_dark_theme_covers_semantic_layer(self):
        """暗色必须覆盖语义层：此前只覆盖 3 处，亮色底在暗色下刺眼。"""
        css = (SRC / "styles" / "tokens.css").read_text(encoding="utf-8")
        dark = css.split('html[data-theme="dark"]', 1)[1]
        for t in ("--tag-ok-bg", "--tag-bad-bg", "--shadow-2", "--shadow-3", "--success"):
            self.assertIn(t, dark, f"暗色未覆盖 {t} ⇒ 该 token 在暗色下会沿用亮色值")


if __name__ == "__main__":
    unittest.main()
