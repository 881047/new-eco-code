"""P3-1 预注册 A/B：拆分 `run_backtest` 的**行为不变性**对照（2026-09-20）

背景（2026-09-20 全面审查 D-06 / P-1）：`app/backtest/engine.py:373 run_backtest`
单函数 407 行 / CC 88，承载 8 项职责、20+ 个可变状态 —— 无法局部优化、无法安全并行，
改一处牵全局。本脚本为把它按职责拆分提供安全网。

判据**跑前冻结**在 `plans/p3-1-run-backtest-refactor-20260920.md`（§3）：
  A0 基线可复现（同代码两次跑完全相同）
  A1 metrics 逐位一致 —— 键集合相同、每个数值字段逐位相等，**不做浮点容差**
  A2 月频净值序列 backtest_nav 逐行一致
  A3 日频盯市 backtest_nav_daily 逐行一致
  A4 tests/test_backtest_golden.py 全绿（禁止 REGEN_GOLDEN=1）

用法：
    python scripts/oneoff/ab_run_backtest_refactor.py --arm base
    python scripts/oneoff/ab_run_backtest_refactor.py --arm refactor
    python scripts/oneoff/ab_run_backtest_refactor.py --compare base refactor
    python scripts/oneoff/ab_run_backtest_refactor.py --arm base --repeat   # A0 确定性自检

隔离：回测行 run_date 形如 `20260920_p31_<arm>`，**不碰生产 run_date**。
产物：logs/ab_run_backtest_refactor_<arm>.json
"""
import argparse
import datetime
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app import db                                          # noqa: E402
from app.logger import setup_logger                         # noqa: E402
from app.backtest import engine as E                        # noqa: E402

setup_logger()
db.init_db()

BASE = Path(__file__).resolve().parents[2]
OUT_DIR = BASE / "logs"
TAG_PREFIX = "p31"


def _run_date(arm: str) -> str:
    """与 engine.run_backtest 内部的 run_date 拼法保持一致"""
    return datetime.date.today().strftime("%Y%m%d") + f"_{TAG_PREFIX}_{arm}"


def _read_series(run_date: str) -> dict:
    out = {}
    with db.conn_ctx() as conn:
        for table in ("backtest_nav", "backtest_nav_daily"):
            rows = conn.execute(
                "SELECT run_date, trade_date, nav, sys_nav, bench_nav FROM %s "
                "WHERE run_date=? ORDER BY trade_date" % table,
                (run_date,)).fetchall()
            out[table] = [[str(r[0]), str(r[1]), _f(r[2]), _f(r[3]), _f(r[4])]
                          for r in rows]
    return out


def _f(v):
    """原样保留数值（用字符串存 float 会丢精度，这里直接放 float）"""
    return None if v is None else float(v)


def snapshot(arm: str) -> dict:
    run_date = _run_date(arm)
    print("[%s] 开始回测 …" % arm)
    try:
        metrics = E.run_backtest(run_tag=f"{TAG_PREFIX}_{arm}")
    except Exception as e:
        # ⚠️ 关键：回测失败时**绝不覆盖旧产物** —— 否则 --compare 会拿上一臂的
        # 快照自比，报出 Δ=0 的**假绿**（2026-09-20 第 3 步踩到：ann 未定义导致
        # 回测抛 NameError，却因旧产物还在而显示"全部通过"）。
        print("[%s] 回测失败: %s: %s" % (arm, type(e).__name__, e), file=sys.stderr)
        return None
    series = _read_series(run_date)
    payload = {
        "arm": arm,
        "run_date": run_date,
        "created": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "metrics": metrics,
        "series": series,
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"ab_run_backtest_refactor_{arm}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print("[%s] 已写入 %s （nav %d 行 / daily %d 行）"
          % (arm, path, len(series["backtest_nav"]), len(series["backtest_nav_daily"])))
    return payload


def _diff(a, b, path="") -> list:
    """递归比对，返回差异描述列表（不做浮点容差）"""
    out = []
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            if k not in a:
                out.append(f"{path}.{k}: 仅 B 有")
            elif k not in b:
                out.append(f"{path}.{k}: 仅 A 有")
            else:
                out += _diff(a[k], b[k], f"{path}.{k}")
    elif isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            out.append(f"{path}: 长度 {len(a)} != {len(b)}")
        for i, (x, y) in enumerate(zip(a, b)):
            out += _diff(x, y, f"{path}[{i}]")
    else:
        if a != b:
            out.append(f"{path}: {a!r} != {b!r}")
    return out


def _strip_rundate(rows: list) -> list:
    """序列第 0 列是 run_date —— 两臂 tag 天然不同(20260920_p31_base vs _refactor)，
    它是**标识符**不是行为数据。比它会产生 2000+ 处假差异，必须剥掉。"""
    return [r[1:] if r and str(r[0]).startswith("2026") else r for r in rows]


def compare(arm_a: str, arm_b: str) -> int:
    pa = OUT_DIR / f"ab_run_backtest_refactor_{arm_a}.json"
    pb = OUT_DIR / f"ab_run_backtest_refactor_{arm_b}.json"
    for p in (pa, pb):
        if not p.exists():
            print("缺少产物: %s（先跑 --arm）" % p)
            return 2
    a = json.loads(pa.read_text(encoding="utf-8"))
    b = json.loads(pb.read_text(encoding="utf-8"))
    print("=" * 70)
    print("P3-1 对照: %s (A) vs %s (B)" % (arm_a, arm_b))
    print("=" * 70)
    print("A run_date=%s  B run_date=%s" % (a["run_date"], b["run_date"]))

    gates = {}
    # metrics.run_date 也是标识符（两臂 tag 不同），不是行为指标，排除后才是真比对
    ma = {k: v for k, v in a.get("metrics", {}).items() if k != "run_date"}
    mb = {k: v for k, v in b.get("metrics", {}).items() if k != "run_date"}
    d1 = _diff(ma, mb, "metrics")
    gates["A1 metrics 逐位一致"] = d1
    d2 = _diff(_strip_rundate(a.get("series", {}).get("backtest_nav", [])),
               _strip_rundate(b.get("series", {}).get("backtest_nav", [])), "backtest_nav")
    gates["A2 月频净值逐行一致"] = d2
    d3 = _diff(_strip_rundate(a.get("series", {}).get("backtest_nav_daily", [])),
               _strip_rundate(b.get("series", {}).get("backtest_nav_daily", [])),
               "backtest_nav_daily")
    gates["A3 日频盯市逐行一致"] = d3

    ok = True
    for name, d in gates.items():
        if d:
            ok = False
            print("\n[FAIL] %s —— %d 处差异：" % (name, len(d)))
            for line in d[:15]:
                print("   ", line)
            if len(d) > 15:
                print("    … 另 %d 处" % (len(d) - 15))
        else:
            print("[PASS] %s（Δ = 0）" % name)

    print("\n" + "=" * 70)
    if ok:
        print("结论：Δ = 0，拆分未改变行为 ⇒ 属重构，pin 不需重钉；"
              "仍须跑全量测试与 test_backtest_golden 确认。")
    else:
        print("结论：Δ ≠ 0 ⇒ 不是重构。回退拆分，或按 CLAUDE.md §1 走"
              "重钉 pin + 同步 3 处文档 + commit 带 Δ 与旧 pin 的流程。")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--arm", help="跑一臂并落产物，如 base / refactor")
    ap.add_argument("--compare", nargs=2, metavar=("A", "B"), help="比对两臂产物")
    ap.add_argument("--repeat", action="store_true",
                    help="A0 确定性自检：同 arm 连跑两次并比对")
    args = ap.parse_args()

    if args.compare:
        return compare(*args.compare)
    if args.arm:
        if snapshot(args.arm) is None:
            print("回测失败，未生成产物；旧产物保持不动。", file=sys.stderr)
            return 3
        if args.repeat:
            # A0：第一次结果另存快照，再跑一次覆盖主产物，然后两者比对。
            # ⚠️ 不能 compare(arm, arm) —— 那会把同一文件自比，永远 PASS（假绿）。
            first = OUT_DIR / f"ab_run_backtest_refactor_{args.arm}.json"
            run1 = OUT_DIR / f"ab_run_backtest_refactor_{args.arm}_a0run1.json"
            run1.write_text(first.read_text(encoding="utf-8"), encoding="utf-8")
            snapshot(args.arm)          # 第二次：覆盖 first
            print("\n[A0] 同代码两次运行对照（第 1 次 vs 第 2 次）：")
            return compare(args.arm, f"{args.arm}_a0run1")
        return 0
    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
