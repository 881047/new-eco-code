"""把知识库 `D:\\know` 和本机 Codex skill 镜像进仓库（2026-09-13）

## 为什么要有它

知识库（纪律/口径/运行手册/实验台账/事故台账/金融/数学/编程）按用户要求建在 **`D:\\know`**。
但那样有两个硬伤：

1. **仓库里看不见它** —— `git log`、文件浏览器、review 流程都没有它的影子，
   看上去像"知识库不见了"（用户 2026-09-13 的疑问就是这么来的）；
2. **没有任何历史** —— 改错/误删就没了，而它里面写的是**口径与纪律**，最不该丢。

⇒ 定 canonical = `D:\\know`（用户指定），**镜像** = 仓库 `docs/know/`（随每次改动提交）。
`D:\\codex-home\\skills\\new-eco-know`（Codex 的知识库路由 skill）同理会丢、也没有历史，
⇒ 一起镜像到 `docs/codex-skill/new-eco-know/`。
本脚本单向同步：源 → 镜像，只增改删镜像侧文件，不碰源。

用法：
    D:\\Python313\\python.exe -X utf8 scripts\\sync_know.py            # 同步
    D:\\Python313\\python.exe -X utf8 scripts\\sync_know.py --check    # 只检查是否一致(供审计/CI)
"""
import argparse
import hashlib
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent

# (名字, 源, 镜像, 要镜像的相对路径 glob, 是否必需)
# ⚠️ 2026-10-13 新增第 5 位「必需」—— 原实现对**任意**源缺失都 `return 2`，
# 于是**可选**的 Codex skill 源一消失（D:\codex-home 已移除），
# **知识库镜像也整个不再同步**（`test_know_mirror.py` 报「镜像与源不一致」、
# `audit_engineering` 第 9 项报红，都是这个原因）。
# 单源缺失不该拖垮其余项 —— 与本仓 `_fterm_track` 的 NEWECO_DB 直连、
# `check_monthly_chain` 的过时 stage 名属**同族**缺陷。
MIRRORS = (
    ("知识库", Path(r"D:\know"), BASE / "docs" / "know", ("*.md",), True),
    ("Codex skill", Path(r"D:\codex-home\skills\new-eco-know"),
     BASE / "docs" / "codex-skill" / "new-eco-know", ("SKILL.md", "agents/*.yaml"), False),
)


def _active_mirrors() -> list:
    """→ 只含「源存在」的镜像项。**必需**源缺失 ⇒ 抛 SystemExit(2)。

    可选源缺失只打印提示并跳过该项（其仓库镜像原样保留，仍可正常阅读）——
    缺失的源若照常进入同步循环，`_files()` 会返回空集 ⇒ 镜像里所有文件被算成 `extra`
    而逐个 unlink；下游那道 `src_n == 0` 闸虽然能拦住，但**从入口就跳过**更直白。
    """
    out = []
    for name, src, mirror, globs, required in MIRRORS:
        if src.exists():
            out.append((name, src, mirror, globs))
        elif required:
            print(f"源不存在: {src} —— **必需**源，拒绝继续", file=sys.stderr)
            raise SystemExit(2)
        else:
            print(f"源不存在: {src}（**可选**源，本次跳过该项；仓库镜像仍在，可正常阅读）",
                  file=sys.stderr)
    return out


def _files(root: Path, globs=("*.md",)) -> dict:
    """{相对路径: Path}；root 不存在 ⇒ 空。"""
    if not root.exists():
        return {}
    out = {}
    for g in globs:
        for p in sorted(root.glob(g)):
            if p.is_file():
                out[p.relative_to(root).as_posix()] = p
    return out


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def diff_one(root: Path, mirror: Path, globs) -> dict:
    """→ {"missing": [...], "stale": [...], "extra": [...]}（都指镜像侧的问题）。"""
    src, dst = _files(root, globs), _files(mirror, globs)
    missing = sorted(n for n in src if n not in dst)
    stale = sorted(n for n in src if n in dst and _sha(src[n]) != _sha(dst[n]))
    extra = sorted(n for n in dst if n not in src)
    return {"missing": missing, "stale": stale, "extra": extra}


def main(argv=None) -> int:
    """`argv=None` ⇒ 取 `sys.argv[1:]`（测试可直接传 `[]`）。"""
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只检查不一致(不写文件)")
    ap.add_argument("--force", action="store_true",
                    help="允许镜像**大幅缩水**（源文件数 < 镜像的 50%%）；默认拒绝")
    a = ap.parse_args(argv)
    active = _active_mirrors()          # 必需源缺失会在此抛 SystemExit(2)
    bad = False
    if a.check:
        notes = []
        for name, src, mirror, globs in active:
            d = diff_one(src, mirror, globs)
            if d["missing"] or d["stale"] or d["extra"]:
                bad = True
                notes.append(f"{name}: 缺 {d['missing']} 旧 {d['stale']} 多 {d['extra']}")
        if bad:
            print("镜像不一致 — " + "；".join(notes), file=sys.stderr)
            return 1
        print("镜像一致: " + "；".join(f"{n} {len(_files(s, g))} 个文件"
                                      for n, s, _m, g in active))
        return 0
    for name, src, mirror, globs in active:
        d = diff_one(src, mirror, globs)
        # 2026-09-14（外部复核 O6）：原来唯一的守卫是"源目录存在"，而 `_files()` 是**非递归** glob ⇒
        # `D:\know` 被清空、或 .md 被移进子目录时 src 会变成 0 个 ⇒ 镜像里所有文件都算 `extra`
        # 被逐个 unlink ⇒ **删光知识库唯一带版本历史的副本**。两道闸：
        #   ① 源 0 个匹配文件 ⇒ 直接拒绝（几乎总是路径/挂载问题）；
        #   ② 源文件数 < 镜像的 50% ⇒ 拒绝，除非显式 `--force`（防"搬了一半就开始同步"）。
        src_n, dst_n = len(_files(src, globs)), len(_files(mirror, globs))
        if src_n == 0:
            print(f"拒绝同步 {name}: 源 {src} 里 0 个匹配文件 {globs} —— 继续同步会把镜像里 "
                  f"{dst_n} 个文件删光。先人工确认源路径/挂载。", file=sys.stderr)
            return 3
        if dst_n >= 4 and src_n * 2 < dst_n and not a.force:
            print(f"拒绝同步 {name}: 源只有 {src_n} 个文件、镜像有 {dst_n} 个（<50%）—— "
                  f"这像是源被搬走了一半。确认无误就加 `--force` 再跑。", file=sys.stderr)
            return 3
        mirror.mkdir(parents=True, exist_ok=True)
        for rel in d["extra"]:          # 源里删掉的, 镜像也删(镜像的语义是"一致")
            (mirror / rel).unlink()
        for rel, p in _files(src, globs).items():
            dst = mirror / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(p.read_bytes())
        print(f"已同步 {name}: {len(_files(src, globs))} 个文件 → {mirror}"
              f"（改 {len(d['missing']) + len(d['stale'])}，删 {len(d['extra'])}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
