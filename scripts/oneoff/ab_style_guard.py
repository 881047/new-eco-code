"""风格防守 A/B(2026-09-10): 择时该看哪个指数?

背景(见 scripts/oneoff/diag_drawdown_attrib.py 与 diag_style_bench.py):
现行 regime 的均线/回撤/波动**全部算在中证800(大中盘)上**, 而持仓是小盘 ——
2023-11-20→2024-02-07(系统线历史谷底 -53.84%)池子(等权全A)跌 30.4%、中证800 只跌
7.4%, -20% 回撤线根本没触发, 防守信号对真正的风险是瞎的。

臂(四个, 均在同一回测口径/逐笔真实费用/2 万本金):
  V0  现行           中证800 一路(生产口径, 对照)
  V1  worse          中证800 + 池子 取「更防守」的一路(只加防守, 不放松进攻)
  V2  replace        完全改用池子分支(等权全A 才是持仓的真实参照系)
  V4  恒定0.873+固定6 既有最优臂(与状态无关, 是要超越的标杆)
窗口 full / h1(2017-2021) / h2(2022-2026), 按项目纪律不选同段最优。

实现: 状态由生产纯函数 regime._state_frame 算出后**改写 regime.state 列**, 回测读表
即生效; finally 还原(日链 18:00 整段重算 regime, 双保险)。V0 臂先与库内现行状态
逐日核对 —— 若不一致说明复算口径有出入, 结果不可信。

跑完自清本次 run。
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import numpy as np
import pandas as pd

# 2026-09-13 护栏：本脚本是**平铺脚本**（导入即执行最后的 DELETE 清理），且过程中会
# `UPDATE regime SET state=?` 改写**生产市场状态**。默认拒绝执行，须显式确认。
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _guard import require_confirm          # noqa: E402

require_confirm(__file__, "改写生产 regime.state（回测臂状态）并在结束时 DELETE 本次 run 的"
                          " backtest_* 记录；中途异常可能留下被改写的状态")

from app import config as cfg_mod
from app import db
from app.logger import setup_logger
from app.backtest import engine as E
from app.models.regime import _state_frame

setup_logger()
db.init_db()

_orig = cfg_mod.load_config

WINDOWS = [("full", None, None), ("h1", "20170101", "20211231"),
           ("h2", "20220101", "20260831")]
VARIANTS = [("v0_now", {}),
            ("v1_worse", {"enabled": True, "mode": "worse"}),
            ("v2_replace", {"enabled": True, "mode": "replace"})]


def _patched_const(w):
    def f(*a, **k):
        cfg = _orig(*a, **k)
        cfg["regime_equity_weight"] = {"scan": {"attack": w, "neutral": w, "defense": w}}
        cfg["risk_profile"] = "scan"
        return cfg
    return f


def _sys_sharpe(run_date: str):
    conn = db.get_conn()
    df = pd.read_sql("SELECT trade_date, sys_nav FROM backtest_nav_daily "
                     "WHERE run_date=? ORDER BY trade_date", conn, params=(run_date,))
    conn.close()
    if len(df) < 30:
        return float("nan")
    r = df["sys_nav"].astype(float).pct_change().dropna()
    if r.std() == 0:
        return float("nan")
    return float(r.mean() / r.std(ddof=1) * np.sqrt(252))


# ---------- 读入基准 / 估值 / 现行状态 ----------
conn = db.get_conn()
close = pd.read_sql("SELECT trade_date, close FROM index_daily WHERE ts_code=? ORDER BY trade_date",
                    conn, params=(cfg_mod.benchmark_code(),))
pe_raw = pd.read_sql("SELECT trade_date, pe_ttm FROM valuation_pe ORDER BY trade_date", conn)
orig = pd.read_sql("SELECT trade_date, state FROM regime", conn)
conn.close()
c = close.set_index("trade_date")["close"].astype(float)
pe = pe_raw.set_index("trade_date")["pe_ttm"].astype(float).reindex(c.index)
orig_s = orig.set_index("trade_date")["state"]


def write_states(s: pd.Series) -> None:
    conn = db.get_conn()
    conn.executemany("UPDATE regime SET state=? WHERE trade_date=?",
                     [(v, k) for k, v in s.items()])
    conn.commit()
    conn.close()


def dist(s: pd.Series) -> str:
    vc = s.value_counts()
    n = len(s)
    return " ".join("{}={:.0%}".format(k, vc.get(k, 0) / n)
                    for k in ("attack", "neutral", "defense"))


# ---------- 先核对 V0 复算 == 库内现行 ----------
v0 = _state_frame(c, pe, {}).set_index("trade_date")["state"]
al = v0.reindex(orig_s.index)
n_diff = int((al != orig_s).sum())
print("V0 复算与库内现行状态: {} 天不一致 / {} 天".format(n_diff, len(orig_s)))
if n_diff:
    bad = al[al != orig_s]
    print("  不一致样本(前 10):")
    print(pd.DataFrame({"复算": bad.head(10), "库内": orig_s.reindex(bad.index).head(10)}).to_string())
print()

out = []
TAGS = []
t0 = time.time()
try:
    for tag, guard in VARIANTS:
        st = _state_frame(c, pe, guard).set_index("trade_date")["state"]
        write_states(st)
        print("[{:>4.0f}s] {} 全史状态分布: {}".format(time.time() - t0, tag, dist(st)))
        for wname, start, end in WINDOWS:
            cfg_mod.load_config = _orig
            m = E.run_backtest(run_tag="sg_{}_{}".format(tag, wname), start=start, end=end)
            rd = m["run_date"]
            TAGS.append(rd)
            row = {"arm": tag, "win": wname, "run_date": rd, "capital": m.get("capital"),
                   "ann": m["ann_return"], "mdd": m["max_drawdown"],
                   "sys_ann": m["sys_ann_return"], "sys_mdd": m["sys_max_drawdown"],
                   "sys_mdd_daily": m["sys_mdd_daily"], "sharpe": _sys_sharpe(rd),
                   "idle": m["avg_cash_idle"], "n_avg": m["avg_stocks"],
                   "periods": m["periods"]}
            out.append(row)
            print("[{:>4.0f}s] {}".format(time.time() - t0, json.dumps(row, ensure_ascii=False)),
                  flush=True)
    # V4: 恒定仓位 + 固定 6 只(状态无关; 用库内现行状态跑, 权重恒等)
    write_states(orig_s)
    cfg_mod.load_config = _patched_const(0.873)
    for wname, start, end in WINDOWS:
        m = E.run_backtest(top_n=6, run_tag="sg_v4const_{}".format(wname),
                           start=start, end=end)
        rd = m["run_date"]
        TAGS.append(rd)
        row = {"arm": "v4_const873_fix6", "win": wname, "run_date": rd,
               "capital": m.get("capital"), "ann": m["ann_return"], "mdd": m["max_drawdown"],
               "sys_ann": m["sys_ann_return"], "sys_mdd": m["sys_max_drawdown"],
               "sys_mdd_daily": m["sys_mdd_daily"], "sharpe": _sys_sharpe(rd),
               "idle": m["avg_cash_idle"], "n_avg": m["avg_stocks"],
               "periods": m["periods"]}
        out.append(row)
        print("[{:>4.0f}s] {}".format(time.time() - t0, json.dumps(row, ensure_ascii=False)),
              flush=True)
finally:
    cfg_mod.load_config = _orig
    write_states(orig_s)
    print("已还原 regime 状态(现行)")

print()
for wname, _s, _e in WINDOWS:
    rows = [r for r in out if r["win"] == wname]
    if not rows:
        continue
    print("=" * 104)
    print("窗口 {} (capital={}, {} 期)".format(wname, rows[0]["capital"], rows[0]["periods"]))
    print("=" * 104)
    print("{:>18} {:>9} {:>9} {:>9} {:>9} {:>8} {:>7} {:>6} {:>5}".format(
        "臂", "满仓年化", "满仓回撤", "系统年化", "系统回撤", "日频回撤", "夏普", "闲置", "只数"))
    for r in rows:
        print("{:>18} {:>+8.2%} {:>9.2%} {:>+8.2%} {:>9.2%} {:>8.2%} {:>7.2f} {:>6.1%} {:>5.2f}".format(
            r["arm"], r["ann"], r["mdd"], r["sys_ann"], r["sys_mdd"],
            r["sys_mdd_daily"], r["sharpe"], r["idle"], r["n_avg"]))
    print()

conn = db.get_conn()
for t in TAGS:
    for tbl in ("backtest_summary", "backtest_nav", "backtest_nav_daily"):
        conn.execute("DELETE FROM {} WHERE run_date=?".format(tbl), (t,))
conn.commit()
conn.close()
print("已清理本次 run ({} 条): {}".format(len(TAGS), TAGS))
