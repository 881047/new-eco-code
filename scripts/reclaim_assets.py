#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""NEW ECO 前端构建孤儿资产回收器（默认 dry-run，不删任何文件）

背景：frontend/vite.config.js 设了 emptyOutDir:false —— **有意保留**旧 chunk，
防止开着旧标签页的用户切视图时懒加载 404 卡死。代价是产物只增不减。

本工具做"有界回收"：按构建时刻聚类，只回收**更旧的批次**，保留最近 N 次构建。

用法：
    python reclaim_assets.py                     # dry-run：只列清单（默认）
    python reclaim_assets.py --keep-builds 2     # 保留最近 2 次构建（默认）
    python reclaim_assets.py --apply             # 真正删除（请确认服务无人使用）
    python reclaim_assets.py --apply --older-than 7   # 只回收 7 天前的批次（更保守）

三重保险：
  1. 只删"孤儿"——重新计算引用传递闭包，被引用的文件绝不碰
  2. 按批次（同一次构建）整体保留或回收，不会拆散一次构建
  3. 默认 dry-run；--apply 前再校验一次引用状态，且打印完整清单
"""
import argparse
import os
import re
import sys
import time
from pathlib import Path
from collections import defaultdict

# 仓库根：本脚本在 scripts/ 下 ⇒ parents[1] 即仓库根，再拼前端静态目录。
# 不硬编码 D:\NEW ECO —— 换机器 / 换路径也能跑（项目 scripts/oneoff/README 的路径纪律）。
ROOT = str(Path(__file__).resolve().parents[1] / "app" / "web" / "static")
ASSETS = os.path.join(ROOT, "assets")
REF_RE = re.compile(r"assets/([A-Za-z0-9._-]+\.[a-z0-9]+)")


def referenced_set() -> set:
    """当前被 html/js/css 引用到的资产集合（传递闭包入口由它们自身给出）"""
    refd = set()
    for dp, _dns, fns in os.walk(ROOT):
        for fn in fns:
            if fn.endswith((".html", ".js", ".css")):
                try:
                    with open(os.path.join(dp, fn), encoding="utf-8",
                              errors="ignore") as f:
                        refd |= set(REF_RE.findall(f.read()))
                except OSError:
                    pass
    return refd


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep-builds", type=int, default=2,
                    help="保留最近 N 次构建的孤儿（默认 2）")
    ap.add_argument("--older-than", type=float, default=0.0,
                    help="只回收 N 天前的批次（0=不限制，默认）")
    ap.add_argument("--apply", action="store_true", help="真正删除（默认只 dry-run）")
    args = ap.parse_args()

    if not os.path.isdir(ASSETS):
        print("找不到 assets 目录:", ASSETS)
        return 1

    have = set(os.listdir(ASSETS))
    refd = referenced_set()
    orphans = sorted(have - refd)

    # 按构建时刻聚类（同一分钟视为同一次构建）
    batches = defaultdict(list)
    for f in orphans:
        t = os.path.getmtime(os.path.join(ASSETS, f))
        key = time.strftime("%Y-%m-%d %H:%M", time.localtime(t))
        batches[key].append((f, t))
    order = sorted(batches, key=lambda k: batches[k][0][1], reverse=True)

    keep_keys = set(order[:args.keep_builds])
    now = time.time()
    reclaim, keep = [], []
    for k in order:
        files = [f for f, _t in batches[k]]
        if k in keep_keys:
            keep += files
            continue
        if args.older_than > 0:
            age_days = (now - batches[k][0][1]) / 86400.0
            if age_days < args.older_than:
                keep += files
                continue
        reclaim += files

    def size_of(files):
        return sum(os.path.getsize(os.path.join(ASSETS, f)) for f in files)

    print("=" * 70)
    print("构建孤儿资产回收  (dry-run)" if not args.apply else "构建孤儿资产回收  (APPLY)")
    print("=" * 70)
    print(f"资产总数 {len(have)} · 被引用 {len(refd & have)} · 孤儿 {len(orphans)}")
    print()
    print(f"{'构建时刻':<20}{'孤儿数':>7}{'体积':>12}   处置")
    print("-" * 70)
    for k in order:
        files = [f for f, _t in batches[k]]
        mark = "保留" if all(f in keep for f in files) else "回收"
        print(f"{k:<20}{len(files):>7}{size_of(files)/1024:>10.1f} KB   {mark}")
    print("-" * 70)
    print(f"保留 {len(keep)} 个 · 回收 {len(reclaim)} 个 · "
          f"释放 {size_of(reclaim)/1024:.1f} KB")

    if not reclaim:
        print("\n没有可回收的资产。")
        return 0

    if not args.apply:
        print("\n[dry-run] 未删除任何文件。确认服务无人使用后加 --apply 执行。")
        print("回收清单（前 15 个）:")
        for f in reclaim[:15]:
            print("   ", f)
        if len(reclaim) > 15:
            print(f"    ... 另 {len(reclaim)-15} 个")
        return 0

    # --apply：删除前再校验一次引用状态（防止这期间有人重建了引用）
    refd2 = referenced_set()
    safe = [f for f in reclaim if f not in refd2]
    blocked = [f for f in reclaim if f in refd2]
    if blocked:
        print(f"\n⚠ 复核发现 {len(blocked)} 个文件现已被引用，跳过: {blocked[:5]}")
    print(f"\n即将删除 {len(safe)} 个文件（{size_of(safe)/1024:.1f} KB）")
    ok = 0
    for f in safe:
        try:
            os.remove(os.path.join(ASSETS, f))
            ok += 1
        except OSError as e:
            print(f"   删除失败 {f}: {e}")
    print(f"已删除 {ok} 个。请跑 test_built_assets_not_stale_vs_source 并手工刷新页面验证。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
