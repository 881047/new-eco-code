"""训练窗口长度实验 —— 检验「36 个月窗口使模型追 regime」假设(2026-09-11)

假设来源(本轮解析)
------------------
模型在 4 个最稳定因子上的暴露是**反向的**: vol_20 0.623 / vol_60 0.604 /
ret_max20 0.581 / bp 0.415(因子表说这三个是**亏钱**方向: 多空 -9.23%/-9.79%/-7.77%,
bp 应偏价值)。而这三个反向暴露**正是近 3 年 regime 的样子**。
⇒ 机制假设: 36 个月的滚动训练窗 = 「永远拟合最近 3 年」= regime chaser;
   窗口拉长应显著降低因子权重的估计方差, 从而减少追 regime。

为什么这个实验值得开机而别的都不值得
------------------------------------
本轮解析的结论是「收益 = 风格暴露, 风格暴露 = 凸性来源, 凸性只在 5/71 个月兑现」,
因此**任何削掉坏倾斜的改动都会同时削掉 payoff**。本假设是这个结论的**唯一可能的例外**:
它不削暴露, 它改**暴露的估计方式**。若它也无效, 「改造模型」这条路可以正式关闭。

事前指定的判定规则(跑之前写死, 不许事后改)
------------------------------------------
主读数 = **P1 = 2020-09 ~ 2023-12(39 个月)**, 即现行模型对风险匹配池子超额为
-3.23%/年的那段。次读数 = P2 = 2024-01 ~ 2026-08(31 个月) 与全期。
  · **采纳**当且仅当: P1 系统年化比对照臂**改善 >= 3.0pp** 且 全期系统年化**退化 <= 2.0pp**
  · 否则 **关闭「改造模型」路径**

对照与校验
----------
四臂全部 train_start=None(全历史) + backtest_start='2019-07-01', 只有 train_months 不同:
    36(对照) / 60 / 120 / 999(始终用全部可得历史)
对照臂应复现**现行代码**的基线: 系统 **+19.40% / -19.66%**(2 万口径)。
复现失败 ⇒ 实验框架不可信, 结果作废。

⚠️ 2026-09-12 更正(两次): 这里原写的是 **+20.64%** —— 那是 D06/D07 限幅修复**进生产之前**
的数字。修复(7 个文件, A→B = -0.17pp/年)之后未重钉基线, 于是 pin 悄悄失效、本脚本的校验值
也随之过时(同形的坑已让 `ab_const_n_5v6.py` 第 1 轮整轮作废)。第 1 次改成 **0.2047**;
**同日第 2 次重钉**(E5 限价入参量化, P0-03)后再改成 **0.1940** —— 那次修改的代价是
系统年化 -1.07pp/年。回撤两次都没动(prod 系统回撤恒为 -0.1966)。
再生路径: `scripts/pin_baseline.py`; 旧两档分别用 `PRE_CLOSE_QUANTIZE=False` 与
D06/D07 的 legacy 分支复现, 见 config.yaml 的 pin 注释。

⚠️ 本脚本是**预注册实验**(判据与结果都在 docstring/`*_result.json` 里), 它的历史判决
不会因为 pin 重钉而失效(每个实验自带同期控制臂), 但**重跑**它会用新 pin 的 VALIDATE
—— 这正是上面那条链要记录的事。

安全
----
实验表 model_score_tm{N}, 不写 model_score / lgb_latest(因 is_prod 判据为
score_table=='model_score'), 不影响生产 pin 与持仓。
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import numpy as np
import pandas as pd

from app import config as cfg_mod
from app import db
from app.backtest import engine as E
from app.logger import setup_logger
from app.models.trainer import ENSEMBLE_ROUNDS, ENSEMBLE_SEEDS, run as train_run

setup_logger()
db.init_db()

ARMS = [36, 60, 120, 999]
BT_START = "2019-07-01"      # 训练侧: 只给 >= 2019-07 的月份推分(省一半训练量)
EVAL_START = "20190722"      # 评估侧: 与生产 pin 同窗口
CAP = 20000.0
P1 = ("2020-09-01", "2023-12-31")   # 无 edge 段(39 个月)
P2 = ("2024-01-01", "2026-08-31")   # 凸性段(31 个月)
VALIDATE = {"sys_ann": 0.1940, "sys_mdd": -0.1966}   # 对照臂应复现(现行代码, 2026-09-12 第 2 次重钉 E5 限价量化; 0.2064→0.2047→0.1940)

_orig = cfg_mod.load_config


def _cap(*a, **k):
    c = _orig(*a, **k)
    c["capital"] = CAP
    return c


mode = sys.argv[1] if len(sys.argv) > 1 else "all"
t0 = time.time()

# ---------- 阶段 1: 训练四个窗口 ----------
if mode in ("train", "all"):
    for tm in ARMS:
        st, it = f"model_score_tm{tm}", f"model_importance_tm{tm}"
        conn = db.get_conn()
        conn.execute(f"CREATE TABLE IF NOT EXISTS {st} ("
                     "ts_code TEXT, trade_date TEXT, score REAL, pred_ret REAL, "
                     "PRIMARY KEY (ts_code, trade_date)) WITHOUT ROWID")
        conn.execute(f"CREATE TABLE IF NOT EXISTS {it} ("
                     "train_date TEXT, factor TEXT, importance REAL, "
                     "PRIMARY KEY (train_date, factor)) WITHOUT ROWID")
        conn.commit()
        n_have = pd.read_sql(f"SELECT COUNT(DISTINCT trade_date) n FROM {st}", conn).iloc[0, 0]
        conn.close()
        print("[{:>5.0f}s] === train_months={} (已有 {} 月) ===".format(
            time.time() - t0, tm, n_have), flush=True)
        arch = Path(__file__).resolve().parents[2] / "data" / "models" / f"ens_tm{tm}"
        arch.mkdir(parents=True, exist_ok=True)
        r = train_run(train_months=tm, train_start=None, backtest_start=BT_START,
                      full=False, score_table=st, importance_table=it, archive_dir=arch,
                      num_boost_round=ENSEMBLE_ROUNDS, early_stopping_rounds=0,
                      ensemble_seeds=ENSEMBLE_SEEDS)
        print("[{:>5.0f}s] train_months={} 完成: {}".format(
            time.time() - t0, tm, json.dumps(r, ensure_ascii=False, default=str)), flush=True)

# ---------- 阶段 2: 回测 + 分段 ----------
if mode in ("bt", "all"):
    conn = db.get_conn()
    p = pd.read_sql("SELECT trade_date, close FROM pool_index", conn)
    conn.close()
    p["trade_date"] = pd.to_datetime(p["trade_date"])
    pool = p.set_index("trade_date")["close"].sort_index()

    rows, keeps = [], []
    cfg_mod.load_config = _cap
    try:
        for tm in ARMS:
            st = f"model_score_tm{tm}"
            m = E.run_backtest(score_table=st, run_tag=f"tm{tm}", start=EVAL_START)
            keeps.append(m["run_date"])
            rows.append({"tm": tm, "run": m["run_date"], "periods": m["periods"],
                         "ann": m["ann_return"], "sys_ann": m["sys_ann_return"],
                         "sys_mdd": m["sys_max_drawdown"], "sharpe": m.get("sharpe"),
                         "ic": m["ic_mean"], "n_avg": m["avg_stocks"],
                         "idle": m["avg_cash_idle"], "turnover": m.get("avg_turnover")})
            print("[{:>5.0f}s] {}".format(time.time() - t0,
                                          json.dumps(rows[-1], ensure_ascii=False)), flush=True)
    finally:
        cfg_mod.load_config = _orig

    # 分段: 每臂用自己的实投暴露 L=1-idle 构造风险匹配池子
    print()
    print("=" * 108)
    print("训练窗口长度实验 —— 评估窗 {} 起, 资金 2 万, 逐笔真实费用, 每臂 4 种子集成".format(EVAL_START))
    print("=" * 108)
    print("{:>7} {:>6} {:>10} {:>10} {:>9} {:>7} {:>8} {:>7} {:>7}   {:<9} {:>9} {:>9}".format(
        "train_m", "期数", "满仓年化", "系统年化", "系统回撤", "夏普", "IC", "实持", "闲置",
        "分段", "系统年化", "超额"))
    print("-" * 108)
    detail = {}
    for r in rows:
        conn = db.get_conn()
        d = pd.read_sql("SELECT trade_date, sys_nav FROM backtest_nav WHERE run_date=? "
                        "ORDER BY trade_date", conn, params=(r["run"],))
        conn.close()
        d["trade_date"] = pd.to_datetime(d["trade_date"])
        nav = d.set_index("trade_date")["sys_nav"]
        L = 1.0 - r["idle"]
        idx = nav.index
        V = L * (pool.reindex(idx) / pool.reindex(idx).iloc[0]) + (1 - L)
        V = V / V.iloc[0]
        rs, rv = nav.pct_change().dropna(), V.pct_change().dropna()
        seg = {}
        for lbl, (a, b) in [("P1 20-09~23-12", P1), ("P2 24-01~26-08", P2)]:
            mm = (rs.index >= a) & (rs.index <= b)
            n = int(mm.sum())
            sa = (1 + rs[mm]).prod() ** (12 / n) - 1
            va = (1 + rv[mm]).prod() ** (12 / n) - 1
            seg[lbl] = (n, sa, va)
        detail[r["tm"]] = {"rs": rs, "rv": rv, "seg": seg, "L": L}
        first = True
        for lbl, (n, sa, va) in seg.items():
            print("{:>7} {:>6} {:>+9.2%} {:>+9.2%} {:>8.2%} {:>7} {:>8.4f} {:>6.2f} {:>6.1%}   {:<13} {:>+8.2%} {:>+8.2%}".format(
                r["tm"] if first else "", r["periods"] if first else "",
                r["ann"] if first else float("nan"), r["sys_ann"] if first else float("nan"),
                r["sys_mdd"] if first else float("nan"),
                "{:.3f}".format(r["sharpe"]) if first else "",
                r["ic"] if first else float("nan"), r["n_avg"] if first else float("nan"),
                r["idle"] if first else float("nan"),
                lbl, sa, sa - va))
            first = False
    print("-" * 108)

    ctl = next(r for r in rows if r["tm"] == 36)
    print("\n对照臂(train_months=36, train_start=None) 复现校验:")
    print("  本实验: 系统 {:+.2%} / 回撤 {:.2%}   历史记录: {:+.2%} / {:.2%}   差 {:+.2f}pp / {:+.2f}pp".format(
        ctl["sys_ann"], ctl["sys_mdd"], VALIDATE["sys_ann"], VALIDATE["sys_mdd"],
        (ctl["sys_ann"] - VALIDATE["sys_ann"]) * 100,
        (ctl["sys_mdd"] - VALIDATE["sys_mdd"]) * 100))
    ok = abs(ctl["sys_ann"] - VALIDATE["sys_ann"]) < 0.02
    print("  ⇒ {}".format("复现成功, 实验框架可信" if ok
                          else "**复现失败 —— 上述全部结果作废**"))
    print("\n判定(P1 系统年化需比对照 +3.0pp 以上, 且全期退化 <= 2.0pp):")
    for tm in ARMS[1:]:
        d1 = detail[tm]["seg"]["P1 20-09~23-12"][1] - detail[36]["seg"]["P1 20-09~23-12"][1]
        df_all = next(r["sys_ann"] for r in rows if r["tm"] == tm) - ctl["sys_ann"]
        verdict = "采纳" if (d1 >= 0.03 and df_all >= -0.02) else "不采纳"
        print("  train_months={:<4} P1 {:+.2f}pp   全期 {:+.2f}pp   ⇒ {}".format(
            tm, d1 * 100, df_all * 100, verdict))
    print("\nrun 保留: {}  (复核后手动清理)".format(", ".join(keeps)))
    print("总耗时 {:.0f}s".format(time.time() - t0))
