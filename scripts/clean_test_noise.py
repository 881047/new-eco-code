"""把测试探针从生产日志里摘掉 —— 带归档、默认只报告不落笔（2026-09-14）。

为什么需要它：`logs/app.log` 是事故取证的第一手台账（07 号台账 #1/#32 的立论基础），
而 2026-09-14 有 2 行测试探针 `NE-TEST-LOG-PROBE-8f3c1d` 混在里面 —— 来源是当时
`scripts/ci_run_tests.py` 走 unittest 路径、**不加载 conftest** ⇒ 隔离没建立（已修，
见 `tests/_isolation.py` 与 `scripts/ci_run_tests.py` 的 docstring）。噪声源头堵住之后，
历史噪声要不要清是个**取舍**：

  * 留着 —— 它是那次缺陷的实测证据（外部审查报告 §9.4 把它当作第 5 次复发的实证）；
  * 清掉 —— 台账里混着"测试探针"这种东西，会让以后每次 grep 都要先过滤一次，
    而过滤习惯一旦养成，真事故也可能被顺手过滤掉。

本脚本的立场：**清掉，但先归档、并保留统计**。归档是逐字节副本，任何时候都能证明
"摘掉的就是这几行"。app.log 的轮转策略是 5MBx3 ⇒ 目标文件很小，操作代价可忽略。

纪律：
- 默认 **dry-run**，只打印会改什么；真动手要 `--apply`。
- `--apply` 时先复制一份 `data/backup/log_archive_<ts>_<name>`（逐字节），再原子替换。
- 只认**探针字面量**，不做模糊匹配；文件只碰固定白名单（app.log 与 .1/.2/.3）。
- 除探针行外**一个字节都不改**（写完比对"非探针内容摘要 + 行数"，不一致就放弃并报错）。

用法:
    D:\\Python313\\python.exe -X utf8 scripts\\clean_test_noise.py            # 看会改什么
    D:\\Python313\\python.exe -X utf8 scripts\\clean_test_noise.py --apply    # 真摘
"""
import argparse
import hashlib
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

#: 只清这个字面量（与 tests/test_test_isolation.py 的 PROBE 常量保持一致）
PROBE = "NE-TEST-LOG-PROBE-8f3c1d"
#: 白名单：日志目录里允许被本脚本碰的文件
TARGETS = ("app.log", "app.log.1", "app.log.2", "app.log.3")


def _digest(lines) -> str:
    return hashlib.sha256("\n".join(lines).encode("utf-8", "replace")).hexdigest()[:16]


def _process(path: Path, apply: bool) -> dict:
    raw = path.read_bytes()
    text = raw.decode("utf-8", "replace")
    lines = text.splitlines()
    hits = [i for i, ln in enumerate(lines) if PROBE in ln]
    if not hits:
        return {"file": path.name, "hits": 0, "kept": len(lines), "changed": False}

    hit_set = set(hits)
    kept = [ln for i, ln in enumerate(lines) if i not in hit_set]
    if not apply:
        return {"file": path.name, "hits": len(hits), "kept": len(kept), "changed": False}

    # ① 归档逐字节副本（可自证"摘掉的是什么"）
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    arch_dir = ROOT / "data" / "backup"
    arch_dir.mkdir(parents=True, exist_ok=True)
    arch = arch_dir / f"log_archive_{stamp}_{path.name}"
    shutil.copy2(path, arch)
    if arch.read_bytes() != raw:
        raise RuntimeError(f"归档读回自检失败: {arch}")

    # ② 同目录临时文件 → 原子替换
    tmp = path.with_name(path.name + ".cleaning")
    tmp.write_text("\n".join(kept) + "\n", encoding="utf-8")

    # ③ 自检：摘掉探针后的内容必须与预期逐行一致
    back = tmp.read_text(encoding="utf-8").splitlines()
    if _digest(back) != _digest(kept) or len(back) != len(kept):
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"{path.name}: 摘除后自检不一致，已放弃（归档留在 {arch}）")
    tmp.replace(path)
    return {"file": path.name, "hits": len(hits), "kept": len(kept), "changed": True,
            "archive": str(arch)}


def main() -> int:
    ap = argparse.ArgumentParser(description="摘掉生产日志里的测试探针行（默认 dry-run）")
    ap.add_argument("--apply", action="store_true", help="真动手（先归档再原子替换）")
    args = ap.parse_args()

    log_dir = ROOT / "logs"
    rows = [_process(log_dir / n, args.apply) for n in TARGETS if (log_dir / n).exists()]
    touched = [r for r in rows if r["hits"]]
    for r in rows:
        flag = "改" if r.get("changed") else ("待改" if r["hits"] else "干净")
        print(f"[{flag}] {r['file']}: 命中 {r['hits']} 行, 保留 {r['kept']} 行"
              + (f", 归档 {r['archive']}" if r.get("archive") else ""))
    if not touched:
        print("没有探针行 —— 无需处理。")
        return 0
    if not args.apply:
        print(f"\n(dry-run) 共命中 {sum(r['hits'] for r in touched)} 行；"
              "加 --apply 才真摘（会先归档到 data/backup/）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
