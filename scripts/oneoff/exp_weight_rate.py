"""仓位兑换率实验(2026-09-10 深夜 第三轮): 回撤在**现行基线**下多少钱一 pp?

前两轮已排除的:
  第一轮(exp_slot_recycle)  提高暴露: A1/A2/A3 三种「把钱投出去」全被支配 —— 闲置的
                            现金是承重的。
  第二轮(exp_stoploss)      止损 / 迟滞带: 止损越松越糟(sl20 两轴都比不止损差), sl10
                            的兑换率 **1.70pp 收益/pp 回撤**; 迟滞带只降 4.9pp 换手却
                            两轴都差 —— 换手不是主要成本项。

于是剩下唯一没在**新基线**上量过的杠杆就是仓位本身。旧基线(20260910_fees0910)上的
前沿扫描给出的兑换率极便宜(恒定 0.873 → 0.70 只花 0.50pp 收益买 6.12pp 回撤 ≈
0.08pp/pp), 但那一轮扫的是**择时映射 vs 恒定仓**, 且 max DD 当时是 2022 的整手伪影区。
换档后(恒定 0.873 + 固定 6 只, max DD = 2018 崩盘)从没重测 —— 而用户的诉求正是
「回撤还是大」, 所以这个兑换率现在是**唯一决策相关的数字**。

只改 regime_equity_weight(三态同值=恒定), 其余全为现行生产配置。窗口 full + h1。
跑完自清。
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pandas as pd

from app import config as cfg_mod
from app import db
from app.backtest import engine as E
from app.logger import setup_logger

setup_logger()
db.init_db()

WINDOWS = [("full", None, None), ("h1", "20170101", "20211231")]
WEIGHTS = [0.80, 0.75, 0.70]

_orig_cfg = cfg_mod.load_config

out = []
TAGS = []
t0 = time.time()
try:
    for w in WEIGHTS:
        def _cfg(*a, **k):
            c = _orig_cfg(*a, **k)
            c["regime_equity_weight"] = {p: {s: w for s in ("attack", "neutral", "defense")}
                                         for p in ("aggressive", "balanced", "conservative")}
            return c

        cfg_mod.load_config = _cfg
        for wname, start, end in WINDOWS:
            m = E.run_backtest(run_tag="wrx_{}_{}".format(int(w * 100), wname),
                               start=start, end=end)
            rd = m["run_date"]
            TAGS.append(rd)
            row = {"w": w, "win": wname, "run": rd,
                   "sys_ann": m["sys_ann_return"], "sys_mdd": m["sys_max_drawdown"],
                   "sys_mdd_daily": m["sys_mdd_daily"], "idle": m["avg_cash_idle"],
                   "n_avg": m["avg_stocks"], "sharpe": m.get("sharpe")}
            out.append(row)
            print("[{:>4.0f}s] {}".format(time.time() - t0,
                                          json.dumps(row, ensure_ascii=False)), flush=True)
finally:
    cfg_mod.load_config = _orig_cfg

# 现行 0.873 的基线(不重跑, 直接引用 pin 20260910_const873_fix6)
BASE = {"full": (0.1105, -0.4190, -0.4244, 0.3228), "h1": (-0.0272, -0.4190, -0.4244, 0.3934)}

print("\n=== 汇总(系统线) —— 含 pin 基线对照 ===")
for wname in ("full", "h1"):
    print("\n-- {} --".format(wname))
    ba, bm, bmd, bi = BASE[wname]
    print("  {:6s} 年化 {:6.2f}%   回撤(月) {:7.2f}%  回撤(日) {:7.2f}%   闲置 {:5.1%}"
          .format("0.873*", ba * 100, bm * 100, bmd * 100, bi))
    for r in [x for x in out if x["win"] == wname]:
        print("  {:6.3f} 年化 {:6.2f}%   回撤(月) {:7.2f}%  回撤(日) {:7.2f}%   闲置 {:5.1%}  "
              "只数 {:.2f}  年化代价 {:+5.2f}pp  回撤收益 {:5.2f}pp  兑换率 {:.2f}pp/pp"
              .format(r["w"], r["sys_ann"] * 100, r["sys_mdd"] * 100, r["sys_mdd_daily"] * 100,
                      r["idle"], r["n_avg"], (r["sys_ann"] - ba) * 100,
                      (r["sys_mdd"] - bm) * 100,
                      abs((r["sys_ann"] - ba) / (r["sys_mdd"] - bm)) if r["sys_mdd"] != bm else float("nan")))

print("\n对照: 第二轮 sl10 的兑换率 = 1.70pp 收益/pp 回撤(越便宜越好)")

conn = db.get_conn()
for t in ("backtest_nav", "backtest_nav_daily"):
    conn.execute("DELETE FROM {} WHERE run_date IN ({})".format(t, ",".join("?" * len(TAGS))),
                 TAGS)
conn.commit()
conn.close()
print("\n已清理 {} 个临时 run".format(len(TAGS)))
