"""服务端口**单一口径**守卫（2026-09-22）。

## 为什么需要它

8787 原先硬编码散在 8+ 处：`app/notify.py` 的看板链接、`scripts/service_watchdog.py`
的探活 URL 与拉起命令、`scripts/audit_engineering.py` 的探活、`scripts/diag_narrow_viewport.py`
的默认 URL、`start.bat` 的 4 处、`frontend/vite.config.js` 的 dev proxy。
改端口要改一堆地方，而**漏一处的症状是静默的**：

- 看门狗探错端口 ⇒ 误判"服务不在" ⇒ **重复拉起**（uvicorn 在 Windows 用 `SO_REUSEADDR`，
  第二个实例会成功绑定，于是并存两套 APScheduler、抢同一份 `data/weekly/ledger.json`，07 #38）
- 邮件里的看板链接打不开（而邮件是在手机上读的，点不进去等于没有）

现在口径收成一个：环境变量 `NEWECO_PORT`，默认 8787。**四个地方各有一份"默认值"**
（`app/config.py`、`scripts/service_watchdog.py`、`start.bat`、`frontend/vite.config.js`）
—— 这是刻意的（看门狗不能 import `app.config`；`start.bat` 与 `vite.config.js` 不是 Python），
所以**必须有个东西钉住它们一致**，否则又是"改了一处漏了三处"。本测试就是那个东西。

## 三条判据

1. **四个默认值必须相等**（这是本文件的核心）
2. `app.config.server_port()` 的行为：env 优先 / 非法回落 / 越界回落
3. **棘轮**：生产面（`app/**`、`scripts/*.py`、`start.bat`、`frontend/`）里不许出现
   **新的**硬编码 8787 —— 只允许那 4 处"默认值定义"（注释不算）
"""
import importlib
import os
import re
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent

CONFIG_PY = BASE / "app" / "config.py"
WATCHDOG_PY = BASE / "scripts" / "service_watchdog.py"
START_BAT = BASE / "start.bat"
VITE_CFG = BASE / "frontend" / "vite.config.js"

#: 允许出现"默认值 8787"的文件（且只允许出现在**默认值定义**那一行）
DEFAULT_DEF_FILES = {CONFIG_PY, WATCHDOG_PY, START_BAT, VITE_CFG}


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8", errors="ignore")


def _strip_comment(line: str, suffix: str) -> str:
    """去掉行内注释。够用即可（这几处都不是"字符串里含注释符"的场景）。"""
    if suffix == ".py":
        return line.split("#", 1)[0]
    if suffix in (".js", ".vue"):
        return line.split("//", 1)[0]
    if suffix == ".bat":
        s = line.strip()
        return "" if s.lower().startswith("rem ") or s.lower() == "rem" else line
    return line


class TestDefaultsAgree(unittest.TestCase):
    """判据 1：四处默认值必须相等。"""

    def test_all_four_defaults_are_equal(self):
        cfg = int(re.search(r"^DEFAULT_SERVER_PORT\s*=\s*(\d+)",
                            _read(CONFIG_PY), re.M).group(1))

        # service_watchdog.py：取 `_server_port()` 函数体里的 `return <int>`
        wd_src = _read(WATCHDOG_PY)
        body = wd_src[wd_src.index("def _server_port()"):]
        body = body[:body.index("\n\n\n")]
        wd = int(re.search(r"return\s+(\d+)", body).group(1))

        # start.bat：`set "NEWECO_PORT=<int>"`
        bat = int(re.search(r'set\s+"NEWECO_PORT=(\d+)"', _read(START_BAT)).group(1))

        # vite.config.js：`process.env.NEWECO_PORT || <int>`
        vite = int(re.search(r"NEWECO_PORT\s*\|\|\s*(\d+)", _read(VITE_CFG)).group(1))

        got = {"app/config.py": cfg, "scripts/service_watchdog.py": wd,
               "start.bat": bat, "frontend/vite.config.js": vite}
        self.assertEqual(
            len(set(got.values())), 1,
            f"四处端口默认值不一致 ⇒ 改了一处漏了别处，症状是静默的（探活探错端口 / "
            f"看板链接打不开）：{got}")

    def test_config_helper_matches_the_constant(self):
        """`server_port()` 在无 env 时必须返回那个常量（不是另写一个字面量）。"""
        from app import config as cfg_mod
        saved = os.environ.pop("NEWECO_PORT", None)
        try:
            self.assertEqual(cfg_mod.server_port(), cfg_mod.DEFAULT_SERVER_PORT)
        finally:
            if saved is not None:
                os.environ["NEWECO_PORT"] = saved


class TestServerPortBehaviour(unittest.TestCase):
    """判据 2：`server_port()` 的三种输入。"""

    def setUp(self):
        from app import config as cfg_mod
        self.cfg = cfg_mod
        self._saved = os.environ.pop("NEWECO_PORT", None)

    def tearDown(self):
        os.environ.pop("NEWECO_PORT", None)
        if self._saved is not None:
            os.environ["NEWECO_PORT"] = self._saved

    def test_env_overrides(self):
        os.environ["NEWECO_PORT"] = "9123"
        self.assertEqual(self.cfg.server_port(), 9123)

    def test_whitespace_tolerated(self):
        os.environ["NEWECO_PORT"] = "  9123  "
        self.assertEqual(self.cfg.server_port(), 9123)

    def test_invalid_falls_back_and_does_not_raise(self):
        """端口写错**不该让整个服务起不来** —— 回落默认值并告警。"""
        for bad in ("abc", "8.7", "", "   "):
            with self.subTest(bad=bad):
                os.environ["NEWECO_PORT"] = bad
                self.assertEqual(self.cfg.server_port(), self.cfg.DEFAULT_SERVER_PORT)

    def test_out_of_range_falls_back(self):
        for bad in ("0", "-1", "65536", "999999"):
            with self.subTest(bad=bad):
                os.environ["NEWECO_PORT"] = bad
                self.assertEqual(self.cfg.server_port(), self.cfg.DEFAULT_SERVER_PORT)

    def test_boundaries_accepted(self):
        for good in (1, 65535):
            with self.subTest(good=good):
                os.environ["NEWECO_PORT"] = str(good)
                self.assertEqual(self.cfg.server_port(), good)


class TestNoNewHardcodedPort(unittest.TestCase):
    """判据 3（棘轮）：生产面不许出现**新的**硬编码 8787。

    只允许那 4 处"默认值定义"；注释不算（注释里提到 8787 是解释历史，无害）。
    新加一处硬编码 ⇒ 这里红 ⇒ 提醒你走 `NEWECO_PORT`。
    """

    #: 生产面（oneoff 是冻结证据、docs 是叙述，都不扫）
    def _scan_files(self):
        out = []
        out += [p for p in (BASE / "app").rglob("*.py") if "__pycache__" not in p.parts]
        out += [p for p in (BASE / "scripts").glob("*.py")]
        out.append(START_BAT)
        fe = BASE / "frontend"
        out.append(VITE_CFG)
        for pat in ("*.js", "*.vue"):
            out += [p for p in (fe / "src").rglob(pat)
                    if "node_modules" not in p.parts]
        return [p for p in out if p.exists()]

    #: **真正危险的是"用了端口"的地方** —— 拼 URL 或传 `--port`。
    #: 只在注释/文档字符串/日志消息里提到那个数字是无害的（本文件自己就到处在提），
    #: 所以判据盯"用法"而不是"出现"：
    #:   127.0.0.1:8787 / localhost:8787 / --port 8787 / port=8787 / :8787/
    USAGE_RE = re.compile(
        r"(?:127\.0\.0\.1|localhost):8787|--port[\s=]+8787|port\s*=\s*8787|:8787/",
        re.I)

    def test_only_known_defaults_use_the_port(self):
        bad = []
        for p in self._scan_files():
            suffix = p.suffix.lower()
            for i, line in enumerate(_read(p).splitlines(), 1):
                if "8787" not in line:
                    continue
                code = _strip_comment(line, suffix)
                if not self.USAGE_RE.search(code):
                    continue                    # 注释 / 文档 / 消息文本里提到 ⇒ 无害
                if p in DEFAULT_DEF_FILES:
                    # 允许的形态：默认值定义本身
                    if re.search(r"DEFAULT_SERVER_PORT\s*=\s*8787", code) \
                       or re.search(r"return\s+8787", code) \
                       or re.search(r'set\s+"NEWECO_PORT=8787"', code) \
                       or re.search(r"NEWECO_PORT\s*\|\|\s*8787", code):
                        continue
                bad.append(f"{p.relative_to(BASE).as_posix()}:{i}: {line.strip()[:80]}")
        self.assertEqual(
            bad, [],
            "生产面出现硬编码端口 ⇒ 改端口时会漏掉它，症状是静默的"
            "（探活探错端口 / 看板链接打不开）。请改用 NEWECO_PORT 口径。:\n  "
            + "\n  ".join(bad))


if __name__ == "__main__":
    unittest.main()
