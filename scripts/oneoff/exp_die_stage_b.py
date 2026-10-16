"""芯片架构「阶段 B」通用脚手架: 一个过了 KGD 的 die 值不值得封装?

## 为什么需要这个脚本(而不是每个 die 写一个)

`expert.kgd` 五条判据**只看死区 head(前 10%)**, 而策略买的是 **top-6**。这个缺口已经被
**三次独立的 KGD「通过」**钉死:

| die | KGD 死区 head | 策略真实结果 |
|---|---|---|
| `die_flow` | +0.51% t=2.97 | top6 几乎全是 2023 一年, 剔掉后 ≈ 0 |
| `head_vt` | +1.59% t=4.85 | **组合层两窗两轴全否**(年化 −5.81pp / −3.95pp) |
| `h10` | +1.35% t=3.72 | **top6 三区段全负**(−0.62% / −0.87% / −0.38%) |

⇒ **KGD 是"入场券"(噪声筛), 不是"判决"(值不值)。** 判决只能是组合层两轴。
本脚本就是这个判决的通用执行体 —— 以后每个过 KGD 的 die 都走它, 不再各写一个。

⚠️ **本脚本不盲**: 跑之前调用方已经看过该 die 的 head/top6 读数(否则不会知道它过了 KGD)。
防自欺的办法只有一条 —— **判据必须严到知情也帮不上忙**, 故直接照抄项目标准, 一个字不放宽。

## 预注册(通用, 写于跑之前)

**唯一变量 = 打分表**。除 `--table` 外全部为现行生产 config(`fees` 段逐笔费用、
`state_stocks` 6/6/6、`regime_equity_weight` 恒定 0.873、st_filter 默认 True)。

**两个窗口, 各带一个同窗基线控制臂**(不引用历史 run, 免 config 漂移):
  prod = `20190722`~  (与 pin `20260911_fullhist` 同窗)
  full = `20170101`~  (含 2018 崩盘, 才能看到对最大回撤做了什么)

**判据(照抄项目标准, 不放宽)**:
  两个窗口**都**要在两轴上不劣于各自同窗基线 ⇒ 4 个不等式全部成立才算 PASS。
  任一破 = **否**。
**两轴全过也不接生产** —— 按 `exp_die_head.py` 的预注册, 下一步是独立的 walk-forward 验证
(有效验证样本只有 24 个月), 那是另一轮。

**保留 NAV, 不自清** —— 判决必须靠有 NAV 的 run 承载, 否则日后连分段复算都做不了。

用法:
    ./.venv/Scripts/python.exe scripts/oneoff/exp_die_stage_b.py \
        --table model_score_die_h10 --tag h10stg_b --note "horizon=10"
"""
import argparse
import io
import json
import sys
import time
from pathlib import Path

if hasattr(sys.stdout, "buffer"):
    _ORIG_STDOUT = sys.stdout          # 必须留住引用, 否则底层 buffer 被 GC 关掉
    sys.stdout = io.TextIOWrapper(_ORIG_STDOUT.buffer, encoding="utf-8", errors="replace")

BASE = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE))

from app import db                                    # noqa: E402
from app.backtest import engine as E                  # noqa: E402
from app.logger import setup_logger                   # noqa: E402

WINDOWS = [("prod", "20190722", None), ("full", "20170101", None)]
BASE_T = "model_score"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--table", required=True, help="过 KGD 的 die 打分表")
    ap.add_argument("--tag", required=True, help="run_tag 前缀")
    ap.add_argument("--note", default="", help="写进日志的身份说明")
    args = ap.parse_args()

    setup_logger()
    db.init_db()
    print("阶段 B 判决: {} → {}_{{base,dietbl}}_{{prod,full}}  {}".format(
        args.table, args.tag, args.note), flush=True)

    out, tags = [], []
    t0 = time.time()
    for wname, start, end in WINDOWS:
        for arm, table in (("base", BASE_T), ("die", args.table)):
            m = E.run_backtest(score_table=table,
                               run_tag="{}_{}_{}".format(args.tag, arm, wname),
                               start=start, end=end)
            tags.append(m["run_date"])
            row = {"arm": arm, "win": wname, "run": m["run_date"], "n": m.get("periods"),
                   "sys_ann": m["sys_ann_return"], "sys_mdd": m["sys_max_drawdown"],
                   "sys_mdd_daily": m["sys_mdd_daily"], "idle": m["avg_cash_idle"],
                   "n_avg": m["avg_stocks"], "sharpe": m.get("sharpe"),
                   "full_ann": m.get("ann_return"), "full_mdd": m.get("max_drawdown")}
            out.append(row)
            print("[{:>4.0f}s] {}".format(time.time() - t0,
                                          json.dumps(row, ensure_ascii=False)), flush=True)

    print("\n" + "=" * 100)
    print("=== 预注册判据: **两个窗口都要**两轴不劣于同窗基线(4 个不等式全成立) ===")
    verdict = True
    for wname, _, _ in WINDOWS:
        r = {x["arm"]: x for x in out if x["win"] == wname}
        b, a = r["base"], r["die"]
        d_ann = (a["sys_ann"] - b["sys_ann"]) * 100
        d_mdd = (a["sys_mdd"] - b["sys_mdd"]) * 100
        ok = (a["sys_ann"] >= b["sys_ann"]) and (a["sys_mdd"] >= b["sys_mdd"])
        verdict = verdict and ok
        print("\n-- {} ({} 期) --".format(wname, b["n"]))
        for tag, x in (("基线", b), (args.table, a)):
            print("  {:22s} 系统年化 {:7.2f}%  回撤(月) {:7.2f}%  回撤(日) {:7.2f}%  "
                  "满仓年化 {:7.2f}%  满仓回撤 {:7.2f}%  闲置 {:5.1%}  只数 {:.2f}  夏普 {:+.3f}"
                  .format(tag, x["sys_ann"] * 100, x["sys_mdd"] * 100, x["sys_mdd_daily"] * 100,
                          x["full_ann"] * 100, x["full_mdd"] * 100, x["idle"], x["n_avg"],
                          x["sharpe"] or float("nan")))
        print("  ⇒ Δ年化 {:+.2f}pp | Δ回撤 {:+.2f}pp | 本窗 {}".format(
            d_ann, d_mdd, "PASS" if ok else "**否**"))

    print("\n" + "=" * 100)
    print("**总判决: {}**".format("PASS" if verdict else "**否 —— die '{}' 不封装**"
                                  .format(args.table)))
    print("run 已保留(不自清), TAGS = {}".format(tags))
    print("读法: 两轴全过 ≠ 可接生产 —— 按预注册, 下一步是独立的 walk-forward 验证(有效样本 24 个月)。")


if __name__ == "__main__":
    main()
