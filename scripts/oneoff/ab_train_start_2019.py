"""对照实验(2026-09-09): 训练数据起点 2017 起 vs 仅科创板上市(2019-07-22)起, 资金 2 万

用户命题
--------
「用 17 年开始的模型训练出来的回测结果进行对照实验, 对照组为只用科创板上市起的数据
 训练的模型, 资金口径上升到两万元。」

臂设计(成本口径两边一致, 均为生产 single_side_cost=0.3% + slip 0.1%)
  R  reference  = 生产模型 model_score(全历史 2014+/2017 起评估), capital=2万
  Rm reference-matched = 同一生产模型, 只截到对照臂的窗口(苹果对苹果)
  S  star-only  = 只用 2019-07-22 起的月末截面训练+推分, capital=2万
  S18 追加臂    = 起点改 2018-01-01(含 2018 熊市但不含 2017) —— 把「少数据」与
                 「没见过熊市」两种解释分开: 若 S18 前段回到 Rm 附近, 则是熊市样本
                 在起作用; 若仍在 S 附近, 则是数据量本身。

为什么要截窗口(Rm): 训练起点受限的模型无法为 2019-07 之前的月份推分(否则是用未来数据
预测过去)。S 臂首个可评分月由「36 月窗口 + >=12 月样本」两道闸门决定, 天然晚于
2019-07。因此对照必须落在同一段评分月上。

运行(重训重, 建议后台)
  python scripts/oneoff/ab_train_start_2019.py smoke [--start 20190722 --suffix star]
  python scripts/oneoff/ab_train_start_2019.py train [--start 20190722 --suffix star]
  python scripts/oneoff/ab_train_start_2019.py bt    [--suffix star] [--extra ...]
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pandas as pd

from app import config as cfg_mod
from app import db
from app.logger import setup_logger
from app.models.trainer import ENSEMBLE_ROUNDS, ENSEMBLE_SEEDS, run as train_run
from app.backtest import engine as E

setup_logger()

CAP = 20000.0                    # 资金口径: 2 万元
STAR_START = "20190722"          # 科创板开市日

_orig = cfg_mod.load_config


def _cap2w(*a, **k):
    cfg = _orig(*a, **k)
    cfg["capital"] = CAP
    return cfg


def names(suffix: str):
    return f"model_score_{suffix}", f"model_importance_{suffix}"


def archive_dir(suffix: str) -> Path:
    d = Path(__file__).resolve().parents[2] / "data" / "models" / f"ens_{suffix}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def ensure_tables(suffix: str):
    score_t, imp_t = names(suffix)
    conn = db.get_conn()
    conn.execute(f"CREATE TABLE IF NOT EXISTS {score_t} ("
                 "ts_code TEXT, trade_date TEXT, score REAL, pred_ret REAL, "
                 "PRIMARY KEY (ts_code, trade_date)) WITHOUT ROWID")
    conn.execute(f"CREATE TABLE IF NOT EXISTS {imp_t} ("
                 "train_date TEXT, factor TEXT, importance REAL, "
                 "PRIMARY KEY (train_date, factor)) WITHOUT ROWID")
    conn.commit()
    conn.close()


def do_train(train_start: str, suffix: str, backtest_start: str = "2017-01-01",
             tag: str = "全量"):
    score_t, imp_t = names(suffix)
    ensure_tables(suffix)
    print(f"=== 训练 {score_t} ({tag}) train_start={train_start} "
          f"backtest_start={backtest_start} ===", flush=True)
    r = train_run(full=False, train_start=train_start, backtest_start=backtest_start,
                  score_table=score_t, importance_table=imp_t,
                  archive_dir=archive_dir(suffix),
                  num_boost_round=ENSEMBLE_ROUNDS, early_stopping_rounds=0,
                  ensemble_seeds=ENSEMBLE_SEEDS)
    print("训练返回:", json.dumps(r, ensure_ascii=False, default=str), flush=True)
    conn = db.get_conn()
    got = pd.read_sql(
        f"SELECT COUNT(DISTINCT trade_date) n, MIN(trade_date) a, MAX(trade_date) b "
        f"FROM {score_t}", conn).iloc[0]
    conn.close()
    print(f"{score_t}: {int(got['n'])} 个月, {got['a']} → {got['b']}", flush=True)
    return str(got["a"]), str(got["b"])


def table_window(table: str):
    conn = db.get_conn()
    try:
        row = pd.read_sql(f"SELECT MIN(trade_date) a, MAX(trade_date) b FROM {table}",
                          conn).iloc[0]
    except Exception:
        return None
    conn.close()
    return None if pd.isna(row["a"]) else (str(row["a"]), str(row["b"]))


def do_backtest(suffix: str, extra: str = None):
    score_t, _ = names(suffix)
    win = table_window(score_t)
    if win is None:
        raise RuntimeError(f"{score_t} 为空, 先跑 train")
    s0, s1 = win
    print(f"对照窗口({score_t}): {s0} → {s1}", flush=True)

    arms = [("ab3_ref2w_full", "model_score", None, None, "生产模型·全窗口(2017起)"),
            ("ab3_ref2w_match", "model_score", s0, s1, "生产模型·截到对照窗口"),
            (f"ab3_{suffix}2w", score_t, s0, s1, f"起点{suffix}·对照窗口")]
    if extra:
        e0 = table_window(f"model_score_{extra}")
        if e0:
            arms.append((f"ab3_{extra}2w", f"model_score_{extra}", s0, s1,
                         f"起点{extra}·对照窗口"))

    cfg_mod.load_config = _cap2w
    out = []
    try:
        for tag, table, st, en, note in arms:
            m = E.run_backtest(score_table=table, run_tag=tag, start=st, end=en)
            row = {
                "arm": tag, "table": table, "note": note, "win": f"{st or '全'}-{en or '全'}",
                "ann": m["ann_return"], "excess": m["excess_ann"], "mdd": m["max_drawdown"],
                "sharpe": m["sharpe"], "vol": m["vol_ann"], "ic": m["ic_mean"],
                "icir": m.get("icir"), "avg_stocks": m["avg_stocks"],
                "idle": m["avg_cash_idle"], "turnover": m["avg_turnover"],
                "sys_ann": m["sys_ann_return"], "sys_mdd": m["sys_max_drawdown"],
                "mdd_daily": m.get("mdd_daily"),
            }
            out.append(row)
            print(json.dumps(row, ensure_ascii=False, default=str), flush=True)
    finally:
        cfg_mod.load_config = _orig

    print()
    print("=" * 122)
    print(f"训练起点对照实验(资金 {CAP:.0f} 元, 成本两边同为生产 0.3%+0.1%)")
    print("=" * 122)
    print(f"{'臂':>16} {'说明':>22} {'窗口':>20} {'年化':>8} {'超额':>8} {'回撤':>8} "
          f"{'夏普':>7} {'IC':>7} {'实持':>6} {'闲置':>7} {'换手':>7}")
    for r in out:
        print(f"{r['arm']:>16} {r['note']:>22} {r['win']:>20} {r['ann']:>+8.2%} "
              f"{r['excess']:>+8.2%} {r['mdd']:>8.2%} {r['sharpe']:>7.3f} {r['ic']:>7.4f} "
              f"{r['avg_stocks']:>6.2f} {r['idle']:>7.1%} {r['turnover']:>7.1%}")
    print()
    print("判定口径: 与 Rm(同一模型、同一窗口)比才是模型差异; 与 R(全窗口)比额外混入 2017-2020 段 beta。")


def _args():
    a = sys.argv[1:]
    cmd = a[0] if a else "bt"
    opts = {}
    i = 1
    while i < len(a):
        if a[i].startswith("--"):
            opts[a[i][2:]] = a[i + 1] if i + 1 < len(a) else ""
            i += 2
        else:
            i += 1
    return cmd, opts


if __name__ == "__main__":
    cmd, opts = _args()
    suffix = opts.get("suffix", "star")
    start = opts.get("start", STAR_START)
    if cmd == "smoke":
        do_train(start, suffix, backtest_start="2026-01-01", tag="冒烟·仅2026")
    elif cmd == "train":
        do_train(start, suffix)
    elif cmd == "bt":
        do_backtest(suffix, opts.get("extra"))
    else:
        raise SystemExit(f"未知命令: {cmd}")
