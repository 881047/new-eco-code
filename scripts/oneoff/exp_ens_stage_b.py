"""阶段 B 判决 —— `model_score_ens`(stacking 融合器) 值不值得封装 (2026-09-11)

=============================== 预注册(写于跑之前) ===============================

## 这一轮补的是什么

`scripts/oneoff/power_on_framework.py` 的预注册里写了「顺带的 alpha 读数按
`exp_die_stage_b.py` 的 4 个不等式判」, **但那个脚本只实现了两条链路判据, 没实现它** ——
这是执行缺口。本脚本补上, 判据一个字不放宽。

## ⚠️ 必须先说清楚: 冻结的「两窗」形式对训练型融合器**结构上不可用**

`exp_die_stage_b.py` 的全部实现是「同一个 `start`, 两条臂各自跑」—— 它**默认候选表与
基线同覆盖**。这个假设对**因子**成立(零参数、全历史有定义), 前 4 个候选
(`die_flow`/`head_vt`/`h10`/`margin_net_share`)**全部满足**它, 所以从没被触发过。

但 `model_score_ens` 是 **walk-forward 训练的元学习器**: 评分月 i 需要 i 之前
`train_months` 个月已实现的读数。**它永远不可能覆盖历史最早那段** —— 这不是这次实现
的缺陷, 是任何训练型 die 的性质。

实测: `model_score_ens` 覆盖 **2020-08-31 ~ 2026-07-31 (72 期)**; 两个 die 与基线都是
**116 期 (2017-01-26 ~ 2026-08-31)**。
⇒ 若照搬原样跑, baseline 跑 85/115 期、cand 跑 72 期 —— **比的是不同时期, 是假读数**。

## 本轮的适配(以及为什么这是"忠实执行"而不是"放宽")

冻结判据的原话是「**两个窗口都要两轴不劣于各自同窗基线**」。**"同窗"两个字就是判据本身。**
当两张表天然覆盖不同时, 用 `start` 参数去对齐是**间接**手段(靠两者覆盖相同才成立);
直接按**月份集合**取交集才是那两个字的本义。所以:

- **判据仍是一条**: 在**双方都存在的每一个评分月**上, `sys_ann` 与 `sys_mdd` 两轴
  都要不劣于同窗基线(`exp_die_stage_b.py` 的 `judge` 逻辑, 逐字不改)。
- 窗口数从"两个"塌缩为"一个", **不是放宽, 是候选的覆盖只有一段**。
- 另报一栏**非判据**的"错配读数"(基线全窗 vs 融合器 72 期), 用途只有一个:
  让"照搬原样会得到什么"可见, 而不是被我一句"不可用"打发掉。

## 计时诚实性声明(写在看到判据数字之前)

本适配写在**第一次通电跑已经打印过一条非判据收据**(`model_score_ens` 进引擎,
系统年化 3.62% / 回撤 −37.98%, 错配窗口, 脚本自己标注为"不是证据")之后。
**没有修改任何阈值、方向或轴的取舍**; 适配的唯一理由是覆盖度实测(一条 SQL, 与收益无关)。
把错配读数一并列出, 就是为了让读者能自己检查这句话。

## 顺带记录(不是判据的一部分)

融合器的前若干期只有 12~35 个月的元训练窗(`stack(min_train_months=12)`,
`train_months=36` 要到更晚才吃满)。本项目已有先例: `star19` 实验里
「训练窗被截到 6~23 个月」那 23 个月正是它 −4.25pp 的全部来源。
故本脚本**额外分段报数**(判据仍只看全段): 剔掉元训练窗不足 36 个月的月份后的读数。

**无论结果如何都不据此改配置、不接生产。** 接生产须走 `bank.promote_to_factor` 显式动作。

用法:
    ./.venv/Scripts/python.exe scripts/oneoff/exp_ens_stage_b.py \
        --table model_score_ens --tag ensstage_b
"""
import argparse
import io
import json
import sys
import time
from pathlib import Path

_ORIG_STDOUT = sys.stdout          # 必须留住引用, 否则底层 buffer 被 GC 关掉
if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(_ORIG_STDOUT.buffer, encoding="utf-8", errors="replace")

BASE = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE))

from app import db                                    # noqa: E402
from app.backtest import engine as E                  # noqa: E402
from app.logger import setup_logger                   # noqa: E402

BASE_T = "model_score"
WINDOWS = [("prod", "20190722"), ("full", "20170101")]     # 逐字复制自 exp_die_stage_b.py


def _months(table):
    c = db.get_conn()
    try:
        return [r[0] for r in c.execute(
            "SELECT DISTINCT trade_date FROM %s ORDER BY trade_date" % table)]
    finally:
        c.close()


def _run(table, tag, start, end, note):
    m = E.run_backtest(score_table=table, run_tag=tag, start=start, end=end)
    row = {"arm": note, "table": table, "tag": tag, "run": m["run_date"],
           "n": m.get("periods"), "sys_ann": m["sys_ann_return"],
           "sys_mdd": m["sys_max_drawdown"], "sys_mdd_daily": m["sys_mdd_daily"],
           "idle": m["avg_cash_idle"], "n_avg": m["avg_stocks"],
           "sharpe": m.get("sharpe"), "full_ann": m.get("ann_return"),
           "full_mdd": m.get("max_drawdown")}
    print("[{:>5.0f}s] {}".format(time.time() - T0, json.dumps(row, ensure_ascii=False)),
          flush=True)
    return row


def _line(tag, x):
    return ("  {:22s} 系统年化 {:7.2f}%  回撤(月) {:7.2f}%  回撤(日) {:7.2f}%  "
            "满仓年化 {:7.2f}%  满仓回撤 {:7.2f}%  闲置 {:5.1%}  只数 {:.2f}  夏普 {:+.3f}"
            .format(tag, x["sys_ann"] * 100, x["sys_mdd"] * 100, x["sys_mdd_daily"] * 100,
                    x["full_ann"] * 100, x["full_mdd"] * 100, x["idle"], x["n_avg"],
                    x["sharpe"] or float("nan")))


def main():
    global T0
    ap = argparse.ArgumentParser()
    ap.add_argument("--table", required=True)
    ap.add_argument("--tag", required=True)
    # 2026-09-13: 原脚本**只往控制台打印** —— 判决跑完就散在终端里, 事后无法回溯
    # ("模型层要不要接 stacking 融合"这种决定必须有可引用的产物)。加一个 JSON 出口。
    ap.add_argument("--json", default=None, help="把判决写成 JSON 产物(含判据/读数/脚本指纹)")
    args = ap.parse_args()
    setup_logger()
    db.init_db()
    T0 = time.time()

    ens_m = _months(args.table)
    base_m = _months(BASE_T)
    common = sorted(set(ens_m) & set(base_m))
    lo, hi = common[0], common[-1]
    print("候选 %s: %d 期 [%s ~ %s]" % (args.table, len(ens_m), ens_m[0], ens_m[-1]), flush=True)
    print("基线 %s: %d 期" % (BASE_T, len(base_m)), flush=True)
    print("**共同窗口(判据口径)**: %d 期 [%s ~ %s]" % (len(common), lo, hi), flush=True)
    print("=" * 100, flush=True)

    # ---- 判据臂: 双方各跑**逐字相同的** [lo, hi] ----
    base = _run(BASE_T, "%s_base_aligned" % args.tag, lo, hi, "基线(对齐窗)")
    cand = _run(args.table, "%s_ens_aligned" % args.tag, lo, hi, "候选(对齐窗)")

    d_ann = (cand["sys_ann"] - base["sys_ann"]) * 100
    d_mdd = (cand["sys_mdd"] - base["sys_mdd"]) * 100
    ok = (cand["sys_ann"] >= base["sys_ann"]) and (cand["sys_mdd"] >= base["sys_mdd"])
    print("\n" + "=" * 100)
    print("=== 预注册判据: 对齐窗 [%s ~ %s] (%d 期) 上两轴都不劣于同窗基线 ===" % (lo, hi, base["n"]))
    print(_line("基线", base))
    print(_line(args.table, cand))
    print("  => d年化 {:+.2f}pp | d回撤 {:+.2f}pp | 本窗 {}".format(
        d_ann, d_mdd, "PASS" if ok else "**否**"))

    print("\n" + "=" * 100)
    print("**总判决: {}**".format(
        "PASS" if ok else "**否 —— 融合器 {} 不封装**".format(args.table)))

    # ---- 附: 错配读数(非判据), 让"照搬原样会说什么"可见 ----
    print("\n" + "-" * 100)
    print("附(非判据): 若照搬 exp_die_stage_b.py 原样(基线全窗 vs 候选 72 期)会得到:")
    base_prod = _run(BASE_T, "%s_base_prodREF" % args.tag, WINDOWS[0][1], None, "基线(prod全窗)")
    base_full = _run(BASE_T, "%s_base_fullREF" % args.tag, WINDOWS[1][1], None, "基线(full全窗)")
    for wn, b in (("prod", base_prod), ("full", base_full)):
        print("  -- %s: 基线 %d 期 vs 候选 %d 期 **期数不同, 是假读数** --" % (wn, b["n"], cand["n"]))
        print("     d年化 {:+.2f}pp | d回撤 {:+.2f}pp".format(
            (cand["sys_ann"] - b["sys_ann"]) * 100, (cand["sys_mdd"] - b["sys_mdd"]) * 100))
    print("  读法: 这一栏**不是判决**, 它存在的意义是证明「覆盖不同」这件事真的会改变结论。")

    print("\n读法: 通过 ≠ 可接生产 —— 有效验证样本只有 24 个月(见方案 §0), 下一步是独立的"
          " walk-forward 验证; 且 2 个成员的 stacking 是很薄的集成。")
    print("run 已保留(不自清)。")

    if args.json:
        import hashlib
        verdict = {
            "script_sha": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:16],
            "candidate_table": args.table, "tag": args.tag, "baseline_table": BASE_T,
            "coverage": {"candidate": [ens_m[0], ens_m[-1], len(ens_m)],
                         "baseline": [base_m[0], base_m[-1], len(base_m)],
                         "common_judgement_window": [lo, hi, len(common)]},
            "judgement": {
                "criteria": "对齐窗 [lo,hi] 上系统年化与系统月回撤两轴都不劣于同窗基线",
                "base": base, "cand": cand,
                "d_ann_pp": round(float(d_ann), 2), "d_mdd_pp": round(float(d_mdd), 2),
                "PASS": bool(ok)},
            "non_judgement_mismatched_window": {"base_prod": base_prod,
                                                "base_full": base_full,
                                                "note": "期数不同 ⇒ 假读数, 只用于展示'覆盖不同会改变结论'"},
            "caveat": "通过 ≠ 可接生产: 有效验证样本只有 24 个月; 且 2 个成员的 stacking 很薄",
            "secs": round(time.time() - T0, 1),
        }
        p = Path(args.json)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(verdict, ensure_ascii=False, indent=1, default=str),
                     encoding="utf-8")
        print("判决产物: %s" % p)


if __name__ == "__main__":
    T0 = time.time()
    main()
