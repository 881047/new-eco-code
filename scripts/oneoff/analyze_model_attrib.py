"""模型归因 + 简单因子组合对照(2026-09-11) —— 「深度改造」的靶子在哪

第一步解析(analyze_market_2019.py, 86 期 2019-07→2026-08)的核心发现:
  · 池子(等权全A) 年化 **+12.38%**/回撤 -32.01%/夏普 0.54 —— 而生产策略 15.42%; 只赢 3.0pp
  · **20/31 因子 |t|>=2 且两半段同号**(跨 regime 稳定), 不是「什么都不灵」的市场
  · 最强的单因子: `amount_log` |IC| 0.0890(t=-5.85)、`illiq` 多空 +20.17%/年 **夏普 1.47**、
    `size` 多空 -20.78%/年、`bp` +11.97%、`turnover_20` -12.18%
  · 胜出方向高度一致: **小 + 低流动性 + 低换手 + 低波动 + 价值** = 教科书小盘价值簇

⇒ 而生产模型 IC = **0.0869**, 与最强单因子 `amount_log` 的 0.0890 **几乎相同**。
  若模型只是这一簇因子的高维代理, 那么「深度改造」的正解不是换特征, 而是
  **直接建在真正赚钱的因子上** —— 更透明、更省算力、更可诊断。

本脚本回答三件事:
  A. 模型的实际因子暴露(逐月 top-6 在 31 因子上的分位, 与全市场对照)
  B. 模型 IC 与单因子 IC 的**同口径**对比(全市场截面, 非 engine 候选池)
  C. **简单因子组合能不能打赢模型** —— 三臂回测:
       fb_illiq  单因子 illiq(全期最强)
       fb_fix    8 个教科书符号的因子等权(小/低流动/低换手/低波/价值/反转)
       fb_wf     walk-forward: 只用 t-1 之前的 IC 定符号 + 取 |t| 前 6 名(**无前视**)

判定: 若 fb_wf 逼近或打赢 15.42% ⇒ 模型层可被透明规则替代, 改造 = 换目标而非调参。
      若明显更差 ⇒ 模型在因子组合之上确有增量, 改造方向是模型而非因子。


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
from app.models import trainer as T

setup_logger()
db.init_db()

START = "20190722"
CAP = 20000.0
FIXED = [("illiq", +1), ("amount_log", -1), ("size", -1), ("turnover_20", -1),
         ("bp", +1), ("rev_5", +1), ("vol_60", -1), ("bias_60", -1)]
WF_TOP = 6
WF_MIN_MONTHS = 24
WF_MIN_T = 2.0

t0 = time.time()
print("[{:>4.0f}s] 载入...".format(time.time() - t0), flush=True)
m = T.load_market()
ac = m["close"] * m["adj"]
dates = pd.to_datetime(m["close"].index)
wide = T._load_factors_wide()
FACTORS = sorted(wide.columns.get_level_values("factor").unique())
md = T.month_end_dates(dates, today=pd.Timestamp("2026-09-10").date())
fut = T.forward_return(ac, T.HORIZON)
conn = db.get_conn()
inds = T.industry_map(conn)
labels = fut.loc[md]
labels = labels - labels.T.groupby(inds.reindex(labels.columns), sort=False).transform("mean").T
print("[{:>4.0f}s] {} 期 {} → {}".format(time.time() - t0, len(md),
                                          md[0].date(), md[-1].date()), flush=True)

# 全期因子面板(逐月 rank pct, 用于组合与暴露)
RP = {}
for f in FACTORS:
    x = wide.xs(f, level="factor", axis=1).reindex(index=md, columns=labels.columns)
    RP[f] = x.rank(axis=1, pct=True)
lab_r = labels.rank(axis=1)
IC = {f: RP[f].corrwith(lab_r, axis=1) for f in FACTORS}
ICdf = pd.DataFrame(IC)
ic_mean, ic_std = ICdf.mean(), ICdf.std(ddof=1)
ic_t = ic_mean / ic_std * np.sqrt(ICdf.notna().sum())

# ---------- A. 模型暴露 ----------
score = pd.read_sql("SELECT ts_code, trade_date, score FROM model_score", conn)
conn.close()
score["trade_date"] = pd.to_datetime(score["trade_date"])
print("\n" + "=" * 100)
print("A. 模型实际因子暴露 —— 逐月 top-6 的因子分位(0~1), 与全市场 0.5 对照")
print("=" * 100)
expo = []
for d, g in score.groupby("trade_date"):
    if d not in RP[FACTORS[0]].index:
        continue
    top = g.nlargest(6, "score")["ts_code"]
    row = {}
    for f in FACTORS:
        s = RP[f].loc[d]
        v = s.reindex(top).dropna()
        if len(v):
            row[f] = v.mean()
    if row:
        expo.append(pd.Series(row, name=d))
EX = pd.DataFrame(expo)
mad = (EX - 0.5).mean().sort_values(key=lambda s: s.abs(), ascending=False)
print("{:<20} {:>10} {:>10}   {}".format("因子", "top6分位", "偏离0.5", "方向"))
for f in mad.index[:14]:
    print("{:<20} {:>10.3f} {:>+10.3f}   {}".format(
        f, EX[f].mean(), mad[f], "偏高" if mad[f] > 0 else "偏低"))
print("... (仅列 |偏离| 前 14)")

# ---------- B. 同口径 IC 对比 ----------
print("\n" + "=" * 100)
print("B. 同口径 IC 对比 —— 全市场截面, 行业中性标签, {} 期".format(len(md)))
print("=" * 100)
mIC = pd.Series({f: IC[f].mean() for f in FACTORS})
# 与 analyze_market_2019.py 的表对齐: 只报 2019-07 起的子样本(全样本留给 walk-forward 用)
sub = ICdf.index >= pd.Timestamp(START)
mIC_sub = pd.Series({f: ICdf.loc[sub, f].mean() for f in FACTORS})
print("  模型 model_score IC = 0.0869 (engine 口径, 与下表非同一口径, 仅作参照)")
print("  单因子 |IC| 前三(2019-07 起 {} 期): ".format(int(sub.sum())) + ", ".join(
    "{} {:.4f}".format(f, v) for f, v in
    mIC_sub.abs().sort_values(ascending=False).head(3).items()))
print("  31 因子 |IC| 中位数 = {:.4f}   最大 = {:.4f}".format(
    mIC_sub.abs().median(), mIC_sub.abs().max()))
print("  (全 {} 期口径下: 中位 {:.4f} 最大 {:.4f})".format(
    len(md), mIC.abs().median(), mIC.abs().max()))

# ---------- C. 组合与回测 ----------
def wf_signs(i):
    """只用 md[:i] 的 IC 定符号与选因子(严格无前视)"""
    hist = ICdf.iloc[:i].dropna(how="all")
    if len(hist) < WF_MIN_MONTHS:
        return None
    mu, sd = hist.mean(), hist.std(ddof=1)
    t = mu / sd * np.sqrt(hist.notna().sum())
    t = t.dropna()
    sel = t[t.abs() >= WF_MIN_T].reindex(t.abs().sort_values(ascending=False).index).head(WF_TOP)
    if not len(sel):
        return None
    return {f: float(np.sign(mu[f])) for f in sel.index}


def build(mode):
    parts = []
    for i, d in enumerate(md):
        if mode == "fix":
            comp = FIXED
        elif mode == "illiq":
            comp = [("illiq", +1)]
        else:
            sg = wf_signs(i)
            if sg is None:
                continue
            comp = list(sg.items())
        acc = None
        for f, sgn in comp:
            v = RP[f].loc[d] if sgn > 0 else (1.0 - RP[f].loc[d])
            acc = v if acc is None else acc + v
        acc = (acc / len(comp) * 100.0).dropna()
        parts.append(acc.rename(d))
    return pd.concat(parts, axis=1).T if parts else None


TAGS = []
out = []

# 必须先把原函数存成闭包变量: 若在 _cap 体内引用 cfg_mod.load_config, 替换后就是自引用
# (RecursionError)。其余脚本均用 _orig 闭包写法, 此处补上。
_orig = cfg_mod.load_config


def _cap(*a, **k):
    c = _orig(*a, **k)
    c["capital"] = CAP
    return c


try:
    for mode, tbl, note in [("illiq", "model_score_fb_illiq", "单因子 illiq"),
                            ("fix", "model_score_fb_fix", "8 因子等权·教科书符号(有前视符号)"),
                            ("wf", "model_score_fb_wf", "walk-forward 符号+选因子(无前视)")]:
        S = build(mode)
        if S is None or S.empty:
            print("跳过 {}: 无评分".format(tbl), flush=True)
            continue
        S.index = pd.to_datetime(S.index)
        S = S.reindex(index=md).dropna(how="all")
        ic = S.rank(axis=1).corrwith(lab_r.reindex(index=S.index), axis=1).dropna()
        conn = db.get_conn()
        conn.execute(f"DROP TABLE IF EXISTS {tbl}")
        conn.execute(f"CREATE TABLE {tbl} (ts_code TEXT, trade_date TEXT, score REAL, "
                     "pred_ret REAL, PRIMARY KEY (ts_code, trade_date)) WITHOUT ROWID")
        rec = S.stack().rename("score").reset_index()
        rec.columns = ["trade_date", "ts_code", "score"]
        rec["trade_date"] = rec["trade_date"].dt.strftime("%Y%m%d")
        rec["pred_ret"] = 0.0
        rec.to_sql(tbl, conn, if_exists="append", index=False)
        conn.commit()
        conn.close()
        print("[{:>4.0f}s] {} 已建: {} 期 {} 行, 自算 IC {:.4f}".format(
            time.time() - t0, tbl, len(S), len(rec), ic.mean()), flush=True)
        cfg_mod.load_config = _cap
        mm = E.run_backtest(score_table=tbl, run_tag=tbl, start=START)
        cfg_mod.load_config = _orig
        TAGS.append(mm["run_date"])
        row = {"arm": tbl, "note": note, "periods": mm["periods"], "ann": mm["ann_return"],
               "mdd": mm["max_drawdown"], "sys_ann": mm["sys_ann_return"],
               "sys_mdd": mm["sys_max_drawdown"], "sharpe": mm.get("sharpe"),
               "ic": mm["ic_mean"], "n_avg": mm["avg_stocks"], "idle": mm["avg_cash_idle"],
               "turnover": mm.get("avg_turnover"), "self_ic": ic.mean()}
        out.append(row)
        print("[{:>4.0f}s] {}".format(time.time() - t0,
                                      json.dumps(row, ensure_ascii=False)), flush=True)
finally:
    cfg_mod.load_config = _orig
    conn = db.get_conn()
    for t in TAGS:
        for tb in ("backtest_summary", "backtest_nav", "backtest_nav_daily"):
            conn.execute(f"DELETE FROM {tb} WHERE run_date=?", (t,))
    for t in ("model_score_fb_illiq", "model_score_fb_fix", "model_score_fb_wf"):
        pass   # 表保留供复核; 需要时手动 DROP
    conn.commit()
    conn.close()

print("\n" + "=" * 100)
print("C. 简单因子组合 vs 模型 —— 窗口 20190722 起, 资金 2 万, 逐笔真实费用")
print("=" * 100)
print("{:>26} {:>6} {:>10} {:>10} {:>10} {:>8} {:>8} {:>7} {:>7}".format(
    "臂", "期数", "满仓年化", "系统年化", "系统回撤", "夏普", "IC", "实持", "换手"))
print("-" * 100)
print("{:>26} {:>6} {:>+9.2%} {:>+9.2%} {:>9.2%} {:>8} {:>8.4f} {:>7.2f} {:>7.1%}".format(
    "**生产模型(model_score)**", 72, 0.1659, 0.1542, -0.2126, "0.616", 0.0869, 4.44, 0.9072))
for r in out:
    print("{:>26} {:>6} {:>+9.2%} {:>+9.2%} {:>9.2%} {:>8} {:>8.4f} {:>7.2f} {:>7.1%}".format(
        r["arm"].replace("model_score_", ""), r["periods"], r["ann"], r["sys_ann"],
        r["sys_mdd"], "{:.3f}".format(r["sharpe"]), r["ic"], r["n_avg"], r["turnover"] or 0))
print("-" * 100)
print("注: fb_fix 用**全期符号**(等于用了未来信息, 是上限参考); fb_wf 只用 t-1 之前的数据定符号,")
print("    才是可实盘的诚实版本。两者差距 = 「符号选择」这件事值多少钱。")
print("总耗时 {:.0f}s".format(time.time() - t0))
