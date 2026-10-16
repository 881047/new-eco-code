"""脚本编码守卫（2026-09-13，07 号 #30/#31）

为什么要有它：Windows PowerShell 5.1 对无 BOM 的 .ps1 会按系统 ANSI（本机 936/GBK）读。
中文的 UTF-8 字节在 GBK 下解成"两个半"，行尾会剩一个孤立前导字节，**.NET 的 GBK 解码器
会把紧跟的换行一起吃掉** ⇒ 下一行被并进上一行的注释里。实测 `scripts/create_shortcut.ps1`：
`$startup = [Environment]::GetFolderPath('Startup')` 就这样变成注释的一部分，`$startup`
保持 `$null`，随后 `Join-Path` 报 "Path because it is null"。

这与 07 #30（`cmd.exe` 被中文 rem 注释带歪偏移、吞掉下一行）是同一类"多字节 + 行解析"事故：
**静默、只在真跑时暴露、且看起来像别处的 bug**。

规则：`scripts/**/*.ps1` 要么纯 ASCII，要么带 UTF-8 BOM（有 BOM，PS 5.1 就按 UTF-8 读）。
"""
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
BOM = b"\xef\xbb\xbf"


class TestPowerShellEncoding(unittest.TestCase):
    def test_ps1_is_ascii_or_has_utf8_bom(self):
        bad = []
        for p in sorted((BASE / "scripts").rglob("*.ps1")):
            raw = p.read_bytes()
            if any(b > 127 for b in raw) and not raw.startswith(BOM):
                bad.append(p.relative_to(BASE).as_posix())
        self.assertEqual(
            bad, [],
            f"这些 .ps1 含非 ASCII 却没有 UTF-8 BOM（PS 5.1 按 ANSI 读 ⇒ 可能吞掉下一行）: {bad}")


if __name__ == "__main__":
    unittest.main()
