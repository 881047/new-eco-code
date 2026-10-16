"""止损 / 迟滞带实验(2026-09-10 深夜 第二轮): 深度解析「回撤还是大, 收益还是小」

第一轮(exp_slot_recycle.py)已证伪「提高暴露」这条路: A1 灌回现有票 5.34%/-61.23%、
A2 补位买更多只 10.69%/-44.69%、A3 两者 8.57%/-59.60% —— 三种「把钱投出去」的方式
全部被现行 A0(11.05%/-41.90%, 闲置 32.3%) 支配。**机械性闲置的现金是承重的**, 分配器
这条线到此为止。

本轮改攻风险的**来源**。回归证据(deep analysis): 系统线对池子 beta 仅 0.576、R² 0.268、
**残差波动 18.17%/年** —— 主导风险是 4.74 只小票的**特异风险**, 不是市场 beta; 而最大
回撤已从旧基线的「6.3 年长坡」**换成了 2018 年那一年的崩盘**(1 年期, -41.90%)。
于是有两个**从不碰选股逻辑**的现成工具从未启用:

  stop_loss    R13-1 逐日盯市止损: 买入次日起, 收盘复权价相对买入成本跌破阈值即当日
               收盘卖出。**直接砍特异风险**, 不需要预测市场(不是择时)。
               注: 一阶近似 —— 重叠持仓用当月买入价、**结转仓位不参与**、收盘判定。
  hysteresis   R11-5 迟滞带: 在册持仓未跌出 top(n+hyst) 不换仓, **不向下补位**
               (engine.py:419 明写)。纯降换手 → 直接省摩擦成本, 冲「收益还是小」。

arms(全部只覆盖单个参数, 其余 = 现行生产配置):
  base    对照, 必须逐位复现 pin 20260910_const873_fix6(11.05%/-41.90%) —— 否则整轮
          结论作废(说明脚本本身改了别的东西)
  sl10/sl15/sl20  止损阈值
  hyst6   迟滞带 6(≈名单半年不动)

窗口 full + h1: **h1 含当前最大回撤(2018)**, 若止损有用必须在这里显形; full 同时检验
止损会不会割掉 2024-25 的修复行情。h2 两轮扫描 DD 全等(-19.66%), 无信息量, 不跑。
跑完自清本次 run(含 base)。
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pandas as pd

from app import db
from app.backtest import engine as E
from app.logger import setup_logger

setup_logger()
db.init_db()

WINDOWS = [("full", None, None), ("h1", "20170101", "20211231")]

# (标签, stop_loss, hysteresis) —— 一次只动一个
ARMS = [("base", None, None), ("sl10", -0.10, None), ("sl15", -0.15, None),
        ("sl20", -0.20, None), ("hyst6", None, 6)]

out = []
TAGS = []
t0 = time.time()
for tag, sl, hy in ARMS:
    for wname, start, end in WINDOWS:
        m = E.run_backtest(run_tag="slx_{}_{}".format(tag, wname),
                           start=start, end=end, stop_loss=sl, hysteresis=hy)
        rd = m["run_date"]
        TAGS.append(rd)
        row = {"arm": tag, "win": wname, "run": rd,
               "ann": m["ann_return"], "mdd": m["max_drawdown"],
               "sys_ann": m["sys_ann_return"], "sys_mdd": m["sys_max_drawdown"],
               "sys_mdd_daily": m["sys_mdd_daily"], "idle": m["avg_cash_idle"],
               "n_avg": m["avg_stocks"], "turnover": m.get("avg_turnover"),
               "periods": m["periods"]}
        out.append(row)
        print("[{:>4.0f}s] {}".format(time.time() - t0,
                                      json.dumps(row, ensure_ascii=False)), flush=True)

print("\n=== 汇总 ===")
df = pd.DataFrame(out)
for w in ("full", "h1"):
    print("\n-- {} --".format(w))
    d = df[df["win"] == w]
    for r in d.itertuples():
        print("  {:7s} 满仓 {:6.2f}%/{:7.2f}%   系统 {:6.2f}%/{:7.2f}% (日频 {:7.2f}%)  "
              "闲置 {:5.1%}  只数 {:.2f}"
              .format(r.arm, r.ann * 100, r.mdd * 100, r.sys_ann * 100, r.sys_mdd * 100,
                      r.sys_mdd_daily * 100, r.idle, r.n_avg))

# 对照臂必须复现 pin, 否则整轮作废
base_full = df[(df["arm"] == "base") & (df["win"] == "full")].iloc[0]
ok = (abs(base_full["sys_ann"] - 0.1105) < 5e-4 and abs(base_full["sys_mdd"] + 0.4190) < 5e-4)
print("\n控制臂复现 pin: {} (base full 系统 {:.4f}/{:.4f}, 期望 0.1105/-0.4190)"
      .format("是" if ok else "**否 —— 本轮结论不可用**",
              base_full["sys_ann"], base_full["sys_mdd"]))

conn = db.get_conn()
for t in ("backtest_nav", "backtest_nav_daily"):
    conn.execute("DELETE FROM {} WHERE run_date IN ({})".format(t, ",".join("?" * len(TAGS))),
                 TAGS)
conn.commit()
conn.close()
print("\n已清理 {} 个临时 run".format(len(TAGS)))
