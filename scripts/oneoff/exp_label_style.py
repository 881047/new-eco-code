"""模型赚的是个股 alpha 还是小盘风格 beta —— 标签风格中性化实验(2026-09-11)

要回答的问题
------------
⑪B 已证模型对阵**随机选股**确有优势(0/10, +13.15pp/年)。但模型**赚的是什么钱**没测过:
  · 标签 = 21 日前瞻收益 − **同行业均值**(trainer.py:203) —— 模型被训练去「选行业内最好的」
  · 而因子回归显示: 唯一统计显著的暴露是**小盘价差(t=2.74)**, alpha 10.47%/年 **t=1.75
    不显著**, 残差 18.16%/年; 因子画像 = 小盘+高波动+龙虎榜+跌深反弹
⇒ 模型在学「行业内选股」, 但盈亏可能来自「押注小盘投机行情」。**这是两件事。**

若是后者, 模型就是一台昂贵的风格择时装置 —— **没有可改造的选股器**, 要改的是仓位问题;
若是前者, 改造有真实靶子, 值得投入数月。

判据(事前指定, 不可事后改)
--------------------------
把标签的**行业中性换成风格中性**(按 size 分位 × vol_20 分位 共 25 桶去均值, 见
`trainer._style_neutralize`), 其余**全部不动**, 重训后跑**同一个随机基线零假设**:
  · 对随机的优势**保持** ⇒ 模型在赚个股 alpha, 改造有靶子
  · 优势**塌掉**   ⇒ 模型 = 风格 beta 搬运工, 无靶子可改

这是**测量不是改动**: 不写 model_score, 不碰 lgb_latest, 不影响生产 pin 与持仓。

对照(已有, 见 exp_null_random.py / exp_fee_floor.py, 窗口同为 20190722, 资金 2 万)
  行业中性模型: 系统 +15.42%/−21.26%; 随机基线 n=10 均值 +2.27%/sd 4.71pp/0-10 跑赢

运行
  python scripts/oneoff/exp_label_style.py train     # 重训(重, 建议后台)
  python scripts/oneoff/exp_label_style.py bt [N]    # 模型臂 + N 次随机基线(默认 5)
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

SUFFIX = "stylen"
SCORE_T = f"model_score_{SUFFIX}"
IMP_T = f"model_importance_{SUFFIX}"
W19 = "20190722"
CAP = 20000.0

# 行业中性对照(⑪B 实测, 同一窗口/同一资金/同一套组合构造)
# ⚠️ 2026-09-13 更正（独立审查 §4.4）：BASE_MODEL 是**当时的** pin，不是现行 ——
# ⚠️ 2026-09-28 时效注记: 本脚本写作时的"现役 pin"=20260912_limitquant(22.62%/19.40%),
# 现役 pin 已重钉至 20260928_exe(31.68%/29.13%, 全史日频 -33.58%) —— 文中 pin 读数均为历史档, 非现行值。
# 现役 pin(`backtest_run=20260912_limitquant`)：满仓 22.62%/-24.10%、系统 19.40%/-19.66%（85 期）。
BASE_MODEL = {"sys_ann": 0.1542, "sys_mdd": -0.2126, "ann": 0.1659, "mdd": -0.2410}
BASE_NULL = {"n": 10, "mean": 0.0227, "sd": 0.0471, "max": 0.1142, "beat": 0}

_orig = cfg_mod.load_config


def _cap(*a, **k):
    cfg = _orig(*a, **k)
    cfg["capital"] = CAP
    return cfg


def do_train():
    conn = db.get_conn()
    conn.execute(f"CREATE TABLE IF NOT EXISTS {SCORE_T} ("
                 "ts_code TEXT, trade_date TEXT, score REAL, pred_ret REAL, "
                 "PRIMARY KEY (ts_code, trade_date)) WITHOUT ROWID")
    conn.execute(f"CREATE TABLE IF NOT EXISTS {IMP_T} ("
                 "train_date TEXT, factor TEXT, importance REAL, "
                 "PRIMARY KEY (train_date, factor)) WITHOUT ROWID")
    conn.commit()
    conn.execute(f"DELETE FROM {SCORE_T}")
    conn.execute(f"DELETE FROM {IMP_T}")
    conn.commit()
    conn.close()
    arch = Path(__file__).resolve().parents[2] / "data" / "models" / f"ens_{SUFFIX}"
    arch.mkdir(parents=True, exist_ok=True)
    print(f"=== 训练 {SCORE_T} (标签=风格中性 size×vol_20 25 桶) "
          f"train_start={W19} ===", flush=True)
    t0 = time.time()
    r = train_run(full=False, train_start=W19, score_table=SCORE_T,
                  importance_table=IMP_T, archive_dir=arch,
                  num_boost_round=ENSEMBLE_ROUNDS, early_stopping_rounds=0,
                  ensemble_seeds=ENSEMBLE_SEEDS, label_neutral="style")
    print("训练返回:", json.dumps(r, ensure_ascii=False, default=str),
          " 耗时 {:.0f}s".format(time.time() - t0), flush=True)
    conn = db.get_conn()
    got = pd.read_sql(f"SELECT COUNT(DISTINCT trade_date) n, MIN(trade_date) a, "
                      f"MAX(trade_date) b FROM {SCORE_T}", conn).iloc[0]
    conn.close()
    print(f"{SCORE_T}: {int(got['n'])} 个月, {got['a']} → {got['b']}", flush=True)


def do_bt(n_null=5):
    conn = db.get_conn()
    src = pd.read_sql(f"SELECT * FROM {SCORE_T}", conn)
    if src.empty:
        conn.close()
        raise SystemExit(f"{SCORE_T} 为空 —— 先跑 train")
    print(f"{SCORE_T}: {len(src)} 行, {src['trade_date'].nunique()} 个交易日", flush=True)

    null_tables = []
    for k in range(n_null):
        t = f"model_score_{SUFFIX}_null{k+1}"
        conn.execute(f"DROP TABLE IF EXISTS {t}")
        df = src.copy()
        rs = np.random.RandomState(1000 + k)
        df["score"] = df.groupby("trade_date")["score"].transform(
            lambda s: s.values[rs.permutation(len(s))])
        df.to_sql(t, conn, if_exists="replace", index=False)
        null_tables.append(t)
    conn.commit()
    conn.close()
    print("已建 {} 张重排表".format(len(null_tables)), flush=True)

    out = []
    TAGS = []
    cfg_mod.load_config = _cap
    t0 = time.time()
    try:
        for t, note in [(SCORE_T, "风格中性模型")] + [(t, "随机基线") for t in null_tables]:
            m = E.run_backtest(score_table=t, run_tag=t, start=W19)
            rd = m["run_date"]
            TAGS.append(rd)
            row = {"arm": t, "note": note, "periods": m["periods"],
                   "ann": m["ann_return"], "mdd": m["max_drawdown"],
                   "sys_ann": m["sys_ann_return"], "sys_mdd": m["sys_max_drawdown"],
                   "sharpe": m.get("sharpe"), "ic": m["ic_mean"],
                   "idle": m["avg_cash_idle"], "n_avg": m["avg_stocks"],
                   "turnover": m.get("avg_turnover")}
            out.append(row)
            print("[{:>4.0f}s] {}".format(time.time() - t0,
                                          json.dumps(row, ensure_ascii=False)), flush=True)
    finally:
        cfg_mod.load_config = _orig
        conn = db.get_conn()
        for t in null_tables:
            conn.execute(f"DROP TABLE IF EXISTS {t}")
        for t in TAGS:
            for tbl in ("backtest_summary", "backtest_nav", "backtest_nav_daily"):
                conn.execute(f"DELETE FROM {tbl} WHERE run_date=?", (t,))
        conn.commit()
        conn.close()

    model = next(r for r in out if r["note"] == "风格中性模型")
    nulls = [r for r in out if r["note"] == "随机基线"]
    sa = np.array([r["sys_ann"] for r in nulls])

    print()
    print("=" * 104)
    print("标签中性化实验 —— 窗口 20190722 起, 资金 2 万, 逐笔真实费用, 其余全部不动")
    print("=" * 104)
    print("{:>16} {:>8} {:>10} {:>10} {:>9} {:>8} {:>8} {:>7} {:>7}".format(
        "臂", "期数", "满仓年化", "系统年化", "系统回撤", "夏普", "IC", "实持", "闲置"))
    print("-" * 104)
    print("{:>16} {:>8} {:>+9.2%} {:>+9.2%} {:>9.2%} {:>8} {:>8.4f} {:>7.2f} {:>7.1%}".format(
        "行业中性(pin)", 72, BASE_MODEL["ann"], BASE_MODEL["sys_ann"],
        BASE_MODEL["sys_mdd"], "0.616", 0.0869, 4.44, 0.3725))
    print("{:>16} {:>8} {:>+9.2%} {:>+9.2%} {:>9.2%} {:>8} {:>8.4f} {:>7.2f} {:>7.1%}".format(
        "风格中性(本实验)", model["periods"], model["ann"], model["sys_ann"],
        model["sys_mdd"], "{:.3f}".format(model["sharpe"]), model["ic"],
        model["n_avg"], model["idle"]))
    print("-" * 104)
    print("风格中性模型的随机基线 (n={}): 均值 {:+.2f}%  sd {:.2f}pp  [{:+.2f}%, {:+.2f}%]"
          .format(len(nulls), sa.mean() * 100, sa.std(ddof=1) * 100,
                  sa.min() * 100, sa.max() * 100))
    print("行业中性模型的随机基线 (n={}): 均值 {:+.2f}%  sd {:.2f}pp"
          .format(BASE_NULL["n"], BASE_NULL["mean"] * 100, BASE_NULL["sd"] * 100))
    print()
    gap_new = model["sys_ann"] - sa.mean()
    gap_old = BASE_MODEL["sys_ann"] - BASE_NULL["mean"]
    beat = int((sa >= model["sys_ann"]).sum())
    print("对随机的优势:  行业中性 {:+.2f}pp   →   风格中性 {:+.2f}pp   (变化 {:+.2f}pp)"
          .format(gap_old * 100, gap_new * 100, (gap_new - gap_old) * 100))
    print("随机跑赢模型:  行业中性 {}/{}   →   风格中性 {}/{}"
          .format(BASE_NULL["beat"], BASE_NULL["n"], beat, len(nulls)))
    print()
    if gap_new > 0.6 * gap_old and beat == 0:
        print("==> 优势基本保持: 模型赚的是**个股 alpha**, 「改造模型」有真实靶子。")
    elif gap_new <= 0 or beat >= 2:
        print("==> 优势塌掉: 模型是**小盘风格 beta 的搬运工**, 没有选股器可改 —— "
              "问题回到仓位而非模型。")
    else:
        print("==> 优势显著缩小但未消失: 模型的 edge 里**风格 beta 与个股 alpha 各占一部分**, "
              "改造的靶子比原先估计的小。")
    print("已清理 {} 个 run 与全部临时表".format(len(TAGS)))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "bt"
    if cmd == "train":
        do_train()
    elif cmd == "bt":
        do_bt(int(sys.argv[2]) if len(sys.argv) > 2 else 5)
    else:
        raise SystemExit(__doc__)
