"""任务 #22 阶段 B: `head_vt` 的组合层回测 —— **本项目第一次为「改模型」跑回测**

## ⚠️ 这一轮**不盲**, 必须先说清楚

KGD 在 `exp_die_head.py` 里已预注册并跑完(`head_vt` PASS / `head_cls` FAIL), 但
**我在写本脚本之前已经看过逐年读数**(`diag_head_paired.py`)。所以本轮的「判据」不是
盲测, 它是**在知情下写死的**。防自欺的办法只有一条: 判据必须**严到知情也帮不上忙** ——
故下面直接照抄项目既有的两轴标准(「两轴都不劣于现行」), 一个字都不放宽。

## 为什么要跑这一步(以及为什么不是"看到结果后换判据")

`expert.kgd` 的五条判据**只看死区 head**。`head_vt` 恰好卡在它的盲点上:
  · KGD 读数: DEAD head **+1.59%/月 t=4.85** ⇒ PASS
  · 而同一个模型 ACTIVE top6 从 +3.19% 塌到 **+0.60%** —— **活跃区是全部收益来源**
  · 死区正是**系统线 5.9 年水下、最大回撤的所在地**

⇒ 「拿死区换活跃区到底值不值」是**组合层面**的问题, head/top6 统计量在结构上回答不了。
分年读数(两边都是跨年一致的, 不是单年噪声)只能说明这个 trade-off **真实存在**, 说明不了它的净值。
**这才是跑回测的理由 —— 不是为了"再看看", 而是因为上游判据在此处有已知盲点。**
(对照: `exp_fusion_router.py` 的阶段 B **按预注册规则不跑**, 那一条仍然有效 —— 那轮阶段 A
判据**未过**, 事后去跑它才是换判据。)

## 预注册(写于跑之前, 知情)

**唯一变量 = 打分表**。`model_score`(现行生产) vs `model_score_die_head_vt`。其余逐字不动:
现行 config 全量(`single_side_cost` 退化为预算预留 + `fees` 段逐笔费用、`state_stocks` 6/6/6、
`regime_equity_weight` 恒定 0.873、`top_n` 走 state_map、st_filter 默认 True)。

**两个窗口, 各带一个同窗基线控制臂**(不引用历史 run, 免 config 漂移):
  prod = `20190722`~  (与 pin `20260911_fullhist` 同窗, 85 期)
  full = `20170101`~  (含 2018 崩盘, 115 期 —— head_vt 2018 top6 +1.28% vs 基线 −1.70%,
                       故 full 窗才能看到它对最大回撤做了什么)

**判据(照抄项目标准, 不放宽)**:
  在两轴上**都不劣于同窗基线** ⇒ `sys_ann ≥ base_ann` **且** `sys_mdd ≥ base_mdd`
  (回撤是负数, 越大越浅)。任一轴劣 = **否**。
  **两轴全过也不接生产** —— 按 `exp_die_head.py` 的预注册, 下一步是独立的 walk-forward 验证
  (有效验证样本只有 24 个月), 那是另一轮。

**保留 NAV**: 本轮 run **不自清**。head_vt 是十轮"改模型"里唯一做出跨年一致死区改善的臂,
若日后要复算分段/逐月, 没有 NAV 就做不了。

用法: ./.venv/Scripts/python.exe scripts/oneoff/exp_die_headvt_bt.py
"""
import io
import json
import sys
import time
from pathlib import Path

import pandas as pd

if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

BASE = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE))

from app import db                                    # noqa: E402
from app.backtest import engine as E                  # noqa: E402
from app.logger import setup_logger                   # noqa: E402

setup_logger()
db.init_db()

ARMS = [("base", "model_score"), ("headvt", "model_score_die_head_vt")]
WINDOWS = [("prod", "20190722", None), ("full", "20170101", None)]

out = []
TAGS = []
t0 = time.time()
for wname, start, end in WINDOWS:
    for arm, table in ARMS:
        m = E.run_backtest(score_table=table, run_tag="headvt_{}_{}".format(arm, wname),
                           start=start, end=end)
        rd = m["run_date"]
        TAGS.append(rd)
        row = {"arm": arm, "win": wname, "run": rd, "n": m.get("periods"),
               "sys_ann": m["sys_ann_return"], "sys_mdd": m["sys_max_drawdown"],
               "sys_mdd_daily": m["sys_mdd_daily"], "idle": m["avg_cash_idle"],
               "n_avg": m["avg_stocks"], "sharpe": m.get("sharpe"),
               "full_ann": m.get("ann_return"), "full_mdd": m.get("max_drawdown")}
        out.append(row)
        print("[{:>4.0f}s] {}".format(time.time() - t0, json.dumps(row, ensure_ascii=False)),
              flush=True)

print("\n" + "=" * 96)
print("=== 预注册判据: 两轴都不劣于同窗基线 ===")
for wname, _, _ in WINDOWS:
    r = {x["arm"]: x for x in out if x["win"] == wname}
    b, a = r["base"], r["headvt"]
    d_ann = (a["sys_ann"] - b["sys_ann"]) * 100
    d_mdd = (a["sys_mdd"] - b["sys_mdd"]) * 100
    ok = (a["sys_ann"] >= b["sys_ann"]) and (a["sys_mdd"] >= b["sys_mdd"])
    print("\n-- {} ({} 期) --".format(wname, b["n"]))
    print("  {:8s} 系统年化 {:7.2f}%  回撤(月) {:7.2f}%  回撤(日) {:7.2f}%  "
          "满仓年化 {:7.2f}%  满仓回撤 {:7.2f}%  闲置 {:5.1%}  只数 {:.2f}  夏普 {:+.3f}"
          .format("基线", b["sys_ann"] * 100, b["sys_mdd"] * 100, b["sys_mdd_daily"] * 100,
                  b["full_ann"] * 100, b["full_mdd"] * 100, b["idle"], b["n_avg"],
                  b["sharpe"] or float("nan")))
    print("  {:8s} 系统年化 {:7.2f}%  回撤(月) {:7.2f}%  回撤(日) {:7.2f}%  "
          "满仓年化 {:7.2f}%  满仓回撤 {:7.2f}%  闲置 {:5.1%}  只数 {:.2f}  夏普 {:+.3f}"
          .format("head_vt", a["sys_ann"] * 100, a["sys_mdd"] * 100, a["sys_mdd_daily"] * 100,
                  a["full_ann"] * 100, a["full_mdd"] * 100, a["idle"], a["n_avg"],
                  a["sharpe"] or float("nan")))
    print("  ⇒ Δ年化 {:+.2f}pp | Δ回撤 {:+.2f}pp | **两轴判据 {}**"
          .format(d_ann, d_mdd, "PASS" if ok else "**否**"))

print("\nrun 已保留(不自清), TAGS = {}".format(TAGS))
print("读法: 两轴全过 ≠ 可接生产 —— 按预注册, 下一步是独立的 walk-forward 验证(有效样本 24 个月)。")
