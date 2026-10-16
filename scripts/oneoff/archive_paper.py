r"""模拟盘(月频纸面账户)重置前的**只读归档** —— 命令行外壳(2026-09-14)

背景、三道保证与"为什么必须归档"写在 `app/paper/archive.py` 的模块 docstring 里;
本文件只是壳: 导出 / 复验 / 打一行人能读的摘要。**纯只读** —— 库以 `mode=ro` 打开,
不写任何业务表, 所以它**不需要** `_guard` 护栏。

用法:

    # 导出到 data/backup/paper_archive_<时间戳>.json
    D:\Python313\python.exe -X utf8 scripts\oneoff\archive_paper.py

    # 指定输出 / 指定别的库(测试或历史备份库)
    D:\Python313\python.exe -X utf8 scripts\oneoff\archive_paper.py --out data\backup\x.json
    D:\Python313\python.exe -X utf8 scripts\oneoff\archive_paper.py --db data\backup\neweco_20260909.db

    # 复验一份归档(行数/sha256/摘要三查; 不可信 ⇒ 退出码 2)
    D:\Python313\python.exe -X utf8 scripts\oneoff\archive_paper.py --verify data\backup\x.json

退出码: 0 = 成功且归档通过自检; 2 = 归档不可信(`--verify` 判定或写回自检没过)。
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

DEFAULT_DB = ROOT / "data" / "neweco.db"


def main() -> int:
    from app.paper import archive as arc

    ap = argparse.ArgumentParser(description="模拟盘只读归档")
    ap.add_argument("--db", default=str(DEFAULT_DB), help="库路径(默认 data/neweco.db)")
    ap.add_argument("--out", default=None, help="输出文件(默认 data/backup/paper_archive_<ts>.json)")
    ap.add_argument("--verify", default=None, help="只复验一份归档, 不导出")
    args = ap.parse_args()

    if args.verify:
        ok, detail = arc.verify(args.verify)
        print(("[归档复验] %s\n  %s" % ("通过" if ok else "不可信!", detail)))
        return 0 if ok else 2

    payload = arc.dump(args.db)
    path = arc.write(args.out or arc.default_path(args.db), payload)
    ok, detail = arc.verify(path)
    print("[归档] %s" % path)
    print("  %s" % arc.summary_line(payload))
    print("  逐表行数: %s" % payload["counts"])
    print("  sha256: %s" % payload["sha256"])
    print("  写回自检: %s" % detail)
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
