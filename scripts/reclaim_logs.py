#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""NEW ECO `logs/` 残留回收器（默认 dry-run，不删任何文件）—— 2026-09-22 立

## 为什么需要它

`logs/` 被 `.gitignore` 整目录忽略，于是**没人管**。2026-09-22 结项审计实测：

- **53 个 `_` 开头的探针/测试残留**：约 25 张 `_dash_*.png` / `_vp_*.png` 截图（~10MB）、
  多份 `_f70_*.log`、`_console_probe*.png`、`_flash*.log/.err`、`_b4_verify.log`
- `logs/_test_watchdog.events.log`（测试落点漏清理的残留 —— 成因已在 2026-09-22 修掉，
  但**已经留下的那份**没人清）
- `logs/launch.log)`（**畸形文件名**，字面含右括号，是批处理重定向 bug 的产物）
- `logs/.__app.lock`（运行中的锁 —— **绝不能删**）

而 `scripts/clean_test_noise.py` 只清 `logs/app.log*` 里的**探针字面量行**，
**不覆盖**上面这些文件 ⇒ 缺口是"没有文件级回收器"。本脚本补这一块。

## 三重保险（与 `scripts/reclaim_assets.py` 同构）

1. **只动"残留"**：只收**名字以 `_` 开头**的文件；另有一份 `NEVER` 硬排除名单
   （运行中的锁、正式日志、看门狗状态文件、畸形名）——命中即跳过，连列都不列进目标
2. **默认 dry-run**：不加 `--apply` 只打印清单
3. **`--apply` 前先归档并自证**：逐文件复制到 `data/backup/log_archive_<ts>/`，
   **读回比对 sha256**，不过就 fail-closed 中止（`01 号纪律第 16 条`：先归档、归档必须能自证）
   再加一道**缩水保护闸**：目标数超过 `logs/` 文件总数 50% 时拒绝，除非 `--force`

## 用法

    D:\\Python313\\python.exe -X utf8 scripts\\reclaim_logs.py                  # dry-run（默认）
    D:\\Python313\\python.exe -X utf8 scripts\\reclaim_logs.py --older-than 3   # 只看 3 天前的
    D:\\Python313\\python.exe -X utf8 scripts\\reclaim_logs.py --apply          # 归档 + 删除
"""
from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOGS = ROOT / "logs"
ARCHIVE_ROOT = ROOT / "data" / "backup"

#: **硬排除** —— 这些永远不是目标，命中直接跳过（连列都不列）
NEVER = {
    ".__app.lock",                  # 运行中的单实例锁
    "app.log", "app.log.1", "app.log.2", "app.log.3",   # 正式日志（轮转由 logger 管）
    "server.log", "server.err.log", "watchdog.log",     # 服务/看门狗日志
    "watchdog_events.log",          # 探活失败现场（要留证）
    "service_watchdog.state", "service_watchdog.alert", "service_heal.json",
    "launch.log)",                  # 畸形名（批处理重定向 bug 产物）⇒ 人工确认后再处理
}
#: 只收这些前缀 —— `_` 开头的都是"探针 / 测试 / 截图"这类临时产物
TARGET_PREFIX = "_"


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _human(n: float) -> str:
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024 or u == "GB":
            return f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} GB"


def collect(older_than_days: float) -> tuple:
    """返回 (目标列表, 被硬排除的清单, logs 总文件数)。"""
    if not LOGS.is_dir():
        return [], [], 0
    allfiles = [p for p in LOGS.iterdir() if p.is_file()]
    cutoff = time.time() - older_than_days * 86400
    targets, excluded = [], []
    for p in sorted(allfiles):
        if p.name in NEVER:
            excluded.append(p.name)
            continue
        if not p.name.startswith(TARGET_PREFIX):
            continue                    # 非 `_` 开头 ⇒ 不是残留，不碰
        st = p.stat()
        if st.st_mtime > cutoff:
            continue                    # 太新 ⇒ 可能还在用
        targets.append({"name": p.name, "bytes": st.st_size,
                        "mtime": time.strftime("%Y-%m-%d %H:%M",
                                               time.localtime(st.st_mtime)),
                        "sha256": None})
    return targets, excluded, len(allfiles)


def main() -> int:
    ap = argparse.ArgumentParser(description="logs/ 残留回收器（默认 dry-run）")
    ap.add_argument("--apply", action="store_true", help="真正归档并删除（默认只列清单）")
    ap.add_argument("--older-than", type=float, default=0.0,
                    help="只回收 N 天前的文件（0 = 不限制，默认）")
    ap.add_argument("--force", action="store_true",
                    help="跳过'目标数 > logs 总数 50%'的缩水保护闸")
    args = ap.parse_args()

    targets, excluded, total = collect(args.older_than)
    tot_bytes = sum(t["bytes"] for t in targets)

    print("== logs/ 残留回收（%s）==" % ("APPLY" if args.apply else "dry-run"))
    print(f"logs 下文件总数 : {total}")
    print(f"命中残留        : {len(targets)} 个（{_human(tot_bytes)}）")
    print(f"硬排除（保留）  : {len(excluded)} 个 —— {', '.join(excluded) if excluded else '无'}")
    if not targets:
        print("\n没有可回收的残留 ⇒ 无事可做")
        return 0

    print("\n-- 清单 --")
    for t in sorted(targets, key=lambda x: -x["bytes"]):
        print(f"   {t['name']:40s} {_human(t['bytes']):>10s}  {t['mtime']}")

    if not args.apply:
        print("\n[dry-run] 未改动任何文件。要执行：加 --apply")
        return 0

    # --- 缩水保护闸（07 #39 同类：清理器不能"一不小心把整个目录清了"）---
    if total and len(targets) / total > 0.5 and not args.force:
        print(f"\n[拒绝] 目标占 logs 文件总数的 {len(targets)/total:.0%}（>50%）—— "
              "这通常是「路径/匹配出了问题」，不是「真的该清这么多」。"
              "\n        确认无误再加 --force。", file=sys.stderr)
        return 3

    # --- 归档并自证（fail-closed：自证不过就不删）---
    stamp = time.strftime("%Y%m%d_%H%M%S")
    adir = ARCHIVE_ROOT / f"log_archive_{stamp}"
    adir.mkdir(parents=True, exist_ok=True)
    print(f"\n-- 归档到 {adir.relative_to(ROOT)} 并自证 --")
    bad = []
    for t in targets:
        src, dst = LOGS / t["name"], adir / t["name"]
        shutil.copy2(src, dst)
        s_src, s_dst = _sha256(src), _sha256(dst)
        t["sha256"] = s_src
        ok = (s_src == s_dst and dst.stat().st_size == t["bytes"])
        print(f"   {'✓' if ok else '✗'} {t['name']:40s} {s_src[:16]}")
        if not ok:
            bad.append(t["name"])
    if bad:
        print(f"\n[中止] {len(bad)} 个文件归档自证失败 ⇒ **一个都不删**（fail-closed）: {bad}",
              file=sys.stderr)
        return 4

    # --- 自证全过才删 ---
    removed, freed = 0, 0
    for t in targets:
        try:
            (LOGS / t["name"]).unlink()
            removed += 1
            freed += t["bytes"]
        except OSError as e:
            print(f"   [警告] 删不掉 {t['name']}: {e}", file=sys.stderr)
    print(f"\n✓ 已归档 {len(targets)} 个、删除 {removed} 个，释放 {_human(freed)}")
    print(f"  归档留存（可随时取回）: {adir}")

    import json
    (adir / "MANIFEST.json").write_text(
        json.dumps({"ran_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "older_than_days": args.older_than,
                    "files": targets}, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
