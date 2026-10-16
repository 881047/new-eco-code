"""模型 vs 随机基线(2026-09-11 第五轮): 「改造模型」到底有没有靶子

背景: 项目九轮实验全部在拿模型的某个变体与模型自身比(8 个排序变体、头部校准、成本
口径、暴露前沿、止损、迟滞、仓位兑换率), 但**从未建立「随机选股」的零假设**。而现有
证据恰恰指向该零假设不一定被拒绝 —— 按月聚合的选股超额全样本 +0.50%/月 **t=1.08**
(与 0 无法区分), 只有两段显著且符号相反(2018 −1.93%/月 t=−2.02, 2024-25 +2.37%/月
t=2.78)。

若模型选不出比随机更好的组合, 则「改造模型」没有靶子 —— 要改造的不是选股器, 而是
「这个池子本身就是 beta」这个事实。若模型明显胜出, 则改造有真实目标且值得投入。

方法(纯零假设, 不引入任何新信息): 把 `model_score` 在**每个交易日内部**随机重排,
生成 score_nullK 表。保持: 候选池、每期候选数、分数分布、整手分配、行业上限、成本
口径、整个组合构造机器**完全不变**; 唯一被破坏的是「股票 ↔ 分数」的对应。因此
null 与 pin 的差 = 模型排序的全部贡献, 且这是**可执行的零假设**(真按随机分选股)。

跑 5 个独立重排给分布(单次随机可能碰巧很好/很坏, 必须看分布而非单点)。
跑完自清: 删表 + 删 run。
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

setup_logger()
db.init_db()

W19 = "20190722"
# 种子可经 argv 追加(如 `python exp_null_random.py 66 77 88`), 用于在首轮结果贴近显著
# 边界时加样本 —— 单次重排是随机变量, z 贴着 2.0 时 5 个样本给不出结论。
SEEDS = [int(x) for x in sys.argv[1:]] or [11, 22, 33, 44, 55]
N_NULL = len(SEEDS)

conn = db.get_conn()
cols = [r[1] for r in conn.execute("PRAGMA table_info(model_score)")]
print("model_score 列: {}".format(cols), flush=True)
src = pd.read_sql("SELECT * FROM model_score", conn)
print("model_score: {} 行, {} 个交易日".format(len(src), src["trade_date"].nunique()), flush=True)

rng = np.random.default_rng(0)
null_tables = []
for k, sd in enumerate(SEEDS):
    t = "model_score_null{}".format(k + 1)
    conn.execute("DROP TABLE IF EXISTS {}".format(t))
    df = src.copy()
    # 在每个 trade_date 内部重排 score(日期分组独立, 池子/分布/期数全不变)
    rs = np.random.RandomState(sd)
    df["score"] = df.groupby("trade_date")["score"].transform(
        lambda s: s.values[rs.permutation(len(s))])
    df.to_sql(t, conn, if_exists="replace", index=False)
    null_tables.append((t, sd))
    print("  已建 {} (seed {})".format(t, sd), flush=True)
conn.commit()

out = []
TAGS = []
t0 = time.time()
try:
    for t, sd in null_tables:
        m = E.run_backtest(score_table=t, run_tag=t, start=W19)
        rd = m["run_date"]
        TAGS.append(rd)
        row = {"arm": t, "seed": sd, "periods": m["periods"],
               "ann": m["ann_return"], "mdd": m["max_drawdown"],
               "sys_ann": m["sys_ann_return"], "sys_mdd": m["sys_max_drawdown"],
               "sys_mdd_daily": m["sys_mdd_daily"], "sharpe": m.get("sharpe"),
               "ic": m["ic_mean"], "avg_stocks": m["avg_stocks"]}
        out.append(row)
        print("[{:>4.0f}s] {}".format(time.time() - t0,
                                      json.dumps(row, ensure_ascii=False)), flush=True)
finally:
    for t, _sd in null_tables:
        conn.execute("DROP TABLE IF EXISTS {}".format(t))
    for t in TAGS:
        for tbl in ("backtest_summary", "backtest_nav", "backtest_nav_daily"):
            conn.execute("DELETE FROM {} WHERE run_date=?".format(tbl), (t,))
    conn.commit()

conn.close()

# ⚠️ 2026-09-13 更正（独立审查 §4.4）：这是**当时的** pin，不是现行 ——
# ⚠️ 2026-09-28 时效注记: 本脚本写作时的"现役 pin"=20260912_limitquant(22.62%/19.40%),
# 现役 pin 已重钉至 20260928_exe(31.68%/29.13%, 全史日频 -33.58%) —— 文中 pin 读数均为历史档, 非现行值。
# 现役 pin(`backtest_run=20260912_limitquant`)：满仓 22.62%/-24.10%、系统 19.40%/-19.66%（85 期）。
# 常量是历史判据、不许改；复核时勿当现行基线。
# 原始记录（保持不变）：(起点 20190722) 满仓 16.59%/-24.10%, 系统 15.42%/-21.26%
PIN = {"ann": 0.1659, "mdd": -0.2410, "sys_ann": 0.1542, "sys_mdd": -0.2126,
       "sys_mdd_daily": -0.3295, "ic": 0.0869, "avg_stocks": 4.44}

sa = np.array([r["sys_ann"] for r in out])
sm = np.array([r["sys_mdd"] for r in out])
fa = np.array([r["ann"] for r in out])

print()
print("=" * 100)
print("模型 vs 随机基线 —— 窗口 20190722 起, 资金 2 万, 逐笔真实费用, 其余为现行生产配置")
print("=" * 100)
print("{:>12} {:>6} {:>9} {:>9} {:>9} {:>9} {:>8} {:>8} {:>7}".format(
    "臂", "期数", "满仓年化", "满仓回撤", "系统年化", "系统回撤", "夏普", "IC", "实持"))
print("-" * 100)
print("{:>12} {:>6} {:>+8.2%} {:>9.2%} {:>+8.2%} {:>9.2%} {:>8} {:>8.4f} {:>7.2f}".format(
    "PIN(模型)", 72, PIN["ann"], PIN["mdd"], PIN["sys_ann"], PIN["sys_mdd"],
    "0.616", PIN["ic"], PIN["avg_stocks"]))
for r in out:
    print("{:>12} {:>6} {:>+8.2%} {:>9.2%} {:>+8.2%} {:>9.2%} {:>8} {:>8.4f} {:>7.2f}".format(
        r["arm"].replace("model_score_", ""), r["periods"], r["ann"], r["mdd"],
        r["sys_ann"], r["sys_mdd"],
        "{:.3f}".format(r["sharpe"]) if r["sharpe"] is not None else "-",
        r["ic"], r["avg_stocks"]))
print("-" * 100)
print("随机基线 {} 次: 系统年化 均值 {:+.2f}%  标准差 {:.2f}pp  [{:+.2f}%, {:+.2f}%]"
      .format(N_NULL, sa.mean() * 100, sa.std(ddof=1) * 100, sa.min() * 100, sa.max() * 100))
print("              系统回撤 均值 {:.2f}%  [{:.2f}%, {:.2f}%]".format(
    sm.mean() * 100, sm.min() * 100, sm.max() * 100))
print()
z = (PIN["sys_ann"] - sa.mean()) / sa.std(ddof=1) if sa.std(ddof=1) > 0 else float("nan")
worse = int((sa >= PIN["sys_ann"]).sum())
print("模型 vs 随机分布:  年化差 {:+.2f}pp   z = {:+.2f}"
      .format((PIN["sys_ann"] - sa.mean()) * 100, z))
print("随机基线中有 {} / {} 次跑赢模型(系统年化)".format(worse, N_NULL))
print()
if worse >= 2:
    print("==> **零假设未被拒绝**: 模型排序相对随机没有稳定优势。改造选股器没有靶子。")
elif z < 2.0:
    print("==> 模型优于随机但不显著(|z|<2)。改造的靶子存在但很弱, 任何改造都难被验证。")
else:
    print("==> 模型显著优于随机(z>2): 排序确实在创造价值, 改造有真实靶子。")

print("已清理 {} 个 run 与全部临时 score 表".format(len(TAGS)))
