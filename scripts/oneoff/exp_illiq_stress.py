"""illiq 臂的证伪性压力测试(2026-09-11)

同窗对照(exp_fb_window.py)结论: 单因子 illiq 系统 +36.07% vs 模型 +15.94%,
同 71 期窗口。但**本项目被彩票坑过一次**(旧基线里皇台酒业复牌单月 +73.9%,
删掉后年化掉 28.78pp)。6 只票的组合里, +20pp 完全可以来自几个月。

本脚本回答, 若答案不好则 illiq 结论作废:
  1. 复现: start=20200831(评分日口径) 能否复现 pin 的 72 期/15.42%
  2. 集中度: 剔除最好的 1/3/5 个月后, 年化还剩多少
  3. 路径: 逐月收益分布、最差月、与模型逐月相关性(是同一件事还是两条路)
  4. 可买性: illiq 实际 top-6 的市值与成交额(低流动性 top-6 是否买得到)
  5. illiq 与 size 的重合度(是不是同一个交易换了个名字)

run 保留不删, 供复核。


# STATUS: BROKEN(2026-09-12 独立审计 #17)—— 同上, 依赖已删除的 model_score_fb_* 表。"""
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

# 首个**评分日**(pin 的 72 期从这里算) —— 2026-09-13 核对（独立审查 §4.4）：
# `model_score` 实测 20200831~20260731 = **72 期** ⇒ 这句注释是对的；
# 注意 `exp_fb_window.py` 用的是 20200930（71 期）且注释写成"72 期"，两者不是同一窗口。
START = "20200831"
CAP = 20000.0

_orig = cfg_mod.load_config


def _cap(*a, **k):
    c = _orig(*a, **k)
    c["capital"] = CAP
    return c


ARMS = [("model_score", "生产模型(star19)"),
        ("model_score_fb_illiq", "单因子 illiq")]

t0 = time.time()
out, TAGS = [], []
cfg_mod.load_config = _cap
try:
    for tbl, note in ARMS:
        m = E.run_backtest(score_table=tbl, run_tag="stress_" + tbl, start=START)
        TAGS.append(m["run_date"])
        row = {"tbl": tbl, "note": note, "run": m["run_date"], "periods": m["periods"],
               "ann": m["ann_return"], "sys_ann": m["sys_ann_return"],
               "sys_mdd": m["sys_max_drawdown"], "sharpe": m.get("sharpe"),
               "ic": m["ic_mean"], "n_avg": m["avg_stocks"],
               "idle": m["avg_cash_idle"], "turnover": m.get("avg_turnover")}
        out.append(row)
        print("[{:>4.0f}s] {}".format(time.time() - t0,
                                      json.dumps(row, ensure_ascii=False)), flush=True)
finally:
    cfg_mod.load_config = _orig

print()
print("=" * 100)
print("1. 复现与对照 —— start={}, 资金 2 万 (run 保留, 供复核)".format(START))
print("=" * 100)
print("{:>18} {:>6} {:>10} {:>10} {:>10} {:>8} {:>8} {:>7} {:>7} {:>7}".format(
    "臂", "期数", "满仓年化", "系统年化", "系统回撤", "夏普", "IC", "实持", "闲置", "换手"))
print("-" * 100)
for r in out:
    print("{:>18} {:>6} {:>+9.2%} {:>+9.2%} {:>9.2%} {:>8} {:>8.4f} {:>7.2f} {:>6.1%} {:>6.1%}".format(
        r["note"], r["periods"], r["ann"], r["sys_ann"], r["sys_mdd"],
        "{:.3f}".format(r["sharpe"]), r["ic"], r["n_avg"], r["idle"], r["turnover"] or 0))
print("  (pin 记录: 72 期, 满仓 +16.59%, 系统 +15.42%, 回撤 -21.26%, IC 0.0869)")

# ---------- 2/3. 集中度与路径 ----------
conn = db.get_conn()
navs = {}
for r in out:
    d = pd.read_sql("SELECT trade_date, nav FROM backtest_nav WHERE run_date=? ORDER BY trade_date",
                    conn, params=(r["run"],))
    d["trade_date"] = pd.to_datetime(d["trade_date"])
    navs[r["note"]] = d.set_index("trade_date")["nav"]
conn.close()

print()
print("=" * 100)
print("2. 集中度 —— 剔除最好 N 个月后年化还剩多少(6 只票的组合必须做这个测试)")
print("=" * 100)
print("{:>18} {:>10} {:>12} {:>12} {:>12} {:>12}".format(
    "臂", "全期年化", "剔最好1月", "剔最好3月", "剔最好5月", "剔最好10月"))
print("-" * 100)
for r in out:
    nav = navs[r["note"]]
    ret = nav.pct_change().dropna()
    n = len(ret)
    cells = []
    for k in (1, 3, 5, 10):
        keep = ret.drop(ret.nlargest(k).index)
        cells.append("(1+{:.4f})^{:.0f}-1".format((1 + keep).prod(), 12 / len(keep)))
    row = "{:>18} {:>11.2%}".format(r["note"], r["sys_ann"])
    for k in (1, 3, 5, 10):
        keep = ret.drop(ret.nlargest(k).index)
        a = (1 + keep).prod() ** (12 / len(keep)) - 1
        row += " {:>12.2%}".format(a)
    print(row)
print("  (剔月后按期数重算年化; 若剔 3 个月就腰斩, 说明收益靠彩票)")

print()
print("3. 逐月路径")
print("-" * 100)
for r in out:
    ret = navs[r["note"]].pct_change().dropna()
    print("{:>18}: 月数 {}  正 {}  最好 {:+.2%}({})  最差 {:+.2%}({})  月度波动 {:.2%}".format(
        r["note"], len(ret), int((ret > 0).sum()),
        ret.max(), ret.idxmax().date(), ret.min(), ret.idxmin().date(), ret.std()))
    top = ret.nlargest(5)
    print("     最好的 5 个月: " + "  ".join("{}({:+.1%})".format(d.date(), v) for d, v in top.items()))
    print("     这 5 个月合计贡献 {:+.1%}, 其余 {} 个月合计 {:+.1%}".format(
        (1 + top).prod() - 1, len(ret) - 5, (1 + ret.drop(top.index)).prod() - 1))
a, b = navs["生产模型(star19)"].pct_change().dropna(), navs["单因子 illiq"].pct_change().dropna()
j = pd.concat([a, b], axis=1, join="inner").dropna()
j.columns = ["model", "illiq"]
print("     逐月相关系数 = {:.3f}  (n={})".format(j["model"].corr(j["illiq"]), len(j)))
print("     模型赢的月 {} / illiq 赢的月 {}".format(
    int((j["model"] > j["illiq"]).sum()), int((j["illiq"] > j["model"]).sum())))

# ---------- 4/5. 可买性与 size 重合 ----------
print()
print("=" * 100)
print("4. illiq top-6 实际选中的票(最近 6 期) —— 市值/成交额检验可买性")
print("=" * 100)
conn = db.get_conn()
nm = dict(zip(*[pd.read_sql("SELECT ts_code, name FROM stock_basic", conn)[c]
                for c in ("ts_code", "name")]))
sc_i = pd.read_sql("SELECT ts_code, trade_date, score FROM model_score_fb_illiq", conn)
sc_m = pd.read_sql("SELECT ts_code, trade_date, score FROM model_score", conn)
conn.close()
for s in (sc_i, sc_m):
    s["trade_date"] = pd.to_datetime(s["trade_date"])
db_basic = pd.read_sql("SELECT ts_code, trade_date, total_mv FROM daily_basic WHERE trade_date>='20251001'",
                       db.get_conn()) if False else None

conn = db.get_conn()
mv = pd.read_sql("SELECT ts_code, trade_date, total_mv FROM daily_basic WHERE trade_date>='20260101'", conn)
am = pd.read_sql("SELECT ts_code, trade_date, amount FROM daily WHERE trade_date>='20260101'", conn)
conn.close()
for d in (mv, am):
    d["trade_date"] = pd.to_datetime(d["trade_date"])
mvi = mv.set_index(["trade_date", "ts_code"])["total_mv"]
ami = am.set_index(["trade_date", "ts_code"])["amount"]

recent = sorted(sc_i["trade_date"].unique())[-6:]
for d in recent:
    g = sc_i[sc_i["trade_date"] == d].nlargest(6, "score")
    lab = []
    for _, r in g.iterrows():
        k = (d, r["ts_code"])
        v, a = mvi.get(k, np.nan), ami.get(k, np.nan)
        lab.append("{} {}({:.0f}亿,{:.2f}亿额)".format(
            r["ts_code"], nm.get(r["ts_code"], "?"),
            v / 1e4 if pd.notna(v) else np.nan, a / 1e5 if pd.notna(a) else np.nan))
    print("{}  {}".format(pd.Timestamp(d).date(), " | ".join(lab)))

print()
print("5. illiq top-6 与 size top-6 重合度(用因子表内的 rank, 读 data/factor_ic_2019.csv 不可得,")
print("   改由月度 top-6 名单直接比) —— 见下方「同一只票同时被两边选中」次数")
sc_m_latest = sc_m[sc_m["trade_date"] >= recent[0]]
ov = []
for d in sorted(sc_i["trade_date"].unique()):
    if d < recent[0]:
        continue
    si = set(sc_i[sc_i["trade_date"] == d].nlargest(6, "score")["ts_code"])
    sm = set(sc_m[sc_m["trade_date"] == d].nlargest(6, "score")["ts_code"])
    ov.append(len(si & sm))
if ov:
    print("   最近 {} 期, illiq top-6 与模型 top-6 平均重合 {:.2f} / 6".format(len(ov), np.mean(ov)))
print("\n总耗时 {:.0f}s".format(time.time() - t0))
