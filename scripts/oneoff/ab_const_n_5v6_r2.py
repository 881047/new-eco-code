"""固定 6 只 vs 固定 5 只 · 第 2 轮 —— 修正控制臂基线后重跑(2026-09-12, **预注册**)

═══════════════════════════════════════════════════════════════════════════════
零、为什么有第 2 轮(第 1 轮发生了什么)
═══════════════════════════════════════════════════════════════════════════════
第 1 轮(`ab_const_n_5v6.py`)按预注册第 §四 条**当场作废**: 控制臂 `c873_fix6`
未能复现 pin `20260911_fullhist`, 该条写明「不通过则本轮作废, 不解读实验结果」。
**作废照办了 —— 第 1 轮的 5-vs-6 结果没有被当作判据使用。**

随后的独立诊断(`scripts/oneoff/diag_float32...` 与本轮 ① 段)证明:
**漂移的是基线常量, 不是 config, 也不是数据。** DB 里四条 run 逐位互等:

  20260912_d67_legacy_{prod,full}   0.2397/0.2064  0.1256/0.1105   ← 与 pin 逐位相同
  20260912_d67_fixed_{prod,full}    0.2377/0.2047  0.1236/0.1090
  20260912_diag_pincheck_{prod,full} 0.2377/0.2047  0.1236/0.1090   ← 无 top_n, 生产路径
  20260912_cn56_c873_fix6_{prod,full} 0.2377/0.2047 0.1236/0.1090   ← 第 1 轮的对照臂

⇒ 2026-09-12 进生产的 **D06/D07 限幅修复**(7 个文件, A→B = −0.17pp/年)**没有重钉基线**。
   pin 行描述的是修复前的代码产出。第 1 轮把**过时的 pin** 当成了「当前生产值」。

**⇒ 第 1 轮的缺陷是「测量对象写错」, 不是判据有误。** 故本轮:
   · 判据(§三 的 4 个不等式)**一字不改**
   · 臂定义(fix6 / fix5)**一字不改**
   · 只改控制臂基线(§四), 并**加一条 G2** 把「top_n=6 路径 ≡ 生产路径」在本轮内实测,
     不再靠跨 run 引用

═══════════════════════════════════════════════════════════════════════════════
一、为什么跑这个(问题不是"哪个收益高")
═══════════════════════════════════════════════════════════════════════════════
外部体检报告(2026-09-12) B3 指出一处**口径不一致**:
  · 生产 config `max_stocks = 6`, 由两轴 A/B 选出(`ab_const_n*.py` 系列)
  · 而 `optimizer.feasible_n(investable, target_rate=0.5%)` 解出的是 **5 只**

**B3 已于本轮消解**: `feasible_n` 是 `investable` 的函数, 不是策略属性 ——
17460 元 → 7 只, 12000 元 → 5 只, 24000 元 → 10 只。那个「5」是框架阶段随手传的
一个未标定输入, 不是对策略参数的判决。§五 的辅助算式把这句话钉在数字上。

但「恒定 5 只到底行不行」仍是一个**真问题**, 因为恒定 5 只在全项目**从未测过**
(已核 `app/config.py` 已证伪登记处: 只测过 state(6/5/4) 与固定 6)。

═══════════════════════════════════════════════════════════════════════════════
二、臂与窗口(**跑之前写死, 跑完不得增删**)
═══════════════════════════════════════════════════════════════════════════════
臂(3 个; 其中 prod_native 是**有效性控制臂**, 不参与判据):
  prod_native  控制 —— 不传 top_n, 走 state_map 生产路径(**只为验证 G1/G2**)
  c873_fix6    生产对照 —— top_n=6
  c873_fix5    实验臂   —— top_n=5

窗口(2 个, 与 `exp_die_stage_b.py` 同口径):
  prod = 20190722 起(现行 pin 的评估窗, 85 期)
  full = 20170101 起(全史, 115 期)

**不加第三臂**(4 只 / 7 只), **不换窗口**, **不剔任何一段** —— 加臂即多重比较。

═══════════════════════════════════════════════════════════════════════════════
三、判据(**跑之前写死; 与第 1 轮逐字相同, 未改**)
═══════════════════════════════════════════════════════════════════════════════
换用 5 只, 当且仅当以下 **4 个不等式全部成立**(与 `exp_die_stage_b.py` 同一套):

  P1  prod: sys_ann(5) ≥ sys_ann(6)
  P2  prod: sys_mdd(5) ≥ sys_mdd(6)      (回撤是负数, ≥ 意为"不更深")
  P3  full: sys_ann(5) ≥ sys_ann(6)
  P4  full: sys_mdd(5) ≥ sys_mdd(6)

任一不成立 ⇒ **维持 6 只**。
**事后禁止**: 改用满仓线单独判定 / 只看年化 / 只看回撤 / 换判据阈值 / 重跑取好的一次。

═══════════════════════════════════════════════════════════════════════════════
四、控制臂有效性检查(**跑之前写死; 不通过则本轮作废, 不解读实验结果**)
═══════════════════════════════════════════════════════════════════════════════
基线常量改为**现行代码**的产出值(取自 DB 四条互等的 run, 见 §零):

  prod: ann 23.77% / mdd -24.10% / sys_ann 20.47% / sys_mdd -19.66% / sys_mdd_daily -31.72%
  full: ann 12.36% / mdd -51.26% / sys_ann 10.90% / sys_mdd -42.10% / sys_mdd_daily -42.64%

  G1  `prod_native`(生产路径) 逐位复现上述常量     ← 证明代码/数据未漂移
  G2  `c873_fix6`(top_n=6)   也逐位复现同一组常量  ← 证明对照臂与生产等价

比对容差 5e-5(四位小数显示级别)。任一不符 ⇒ 本轮作废, 只报漂移不报结论。
⚠️ G2 是**第 2 轮新增的**(第 1 轮只有 G1 的旧版)。它不是新判据: 它不改变任何不等式,
   只把「top_n=6 ≡ 生产」这条第 1 轮靠跨 run 比对得到的结论, 改为本轮内实测。

═══════════════════════════════════════════════════════════════════════════════
五、辅助读数(**登记但绝不作判据**)
═══════════════════════════════════════════════════════════════════════════════
· 满仓线两轴 / 日频回撤 / 夏普 / avg_stocks / 闲置率
· **费用算术**: 对 N=5 / N=6 分别算 per_position 与 round_trip, 并在三个可投金额上
  打印 `feasible_n` 的解 —— 直接展示 B3 的「5 只」随 investable 而变, 不是策略属性。
  **描述性, 因为 0.5% 目标本身未标定** ⇒ 它说明**为什么**, 不决定**该不该**。
· **可复现性对照**: 本轮的 c873_fix6 / c873_fix5 必须与第 1 轮逐位相同(同代码同数据,
  回测确定性)。**这是登记项, 不是判据**; 若有差异则说明两轮之间还有别的变量。

═══════════════════════════════════════════════════════════════════════════════
六、证据保留
═══════════════════════════════════════════════════════════════════════════════
**保留 NAV 不自清**(沿用 `exp_die_stage_b.py` 的纪律)。本脚本产生 6 个 run,
无论判决如何都留在 `backtest_summary` / `backtest_nav*` 里供复核。

用法: ./.venv/Scripts/python.exe scripts/oneoff/ab_const_n_5v6_r2.py
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import numpy as np
import pandas as pd

from app import db
from app.logger import setup_logger
from app.backtest import engine as E

setup_logger()
db.init_db()

# ── 预注册常量(判据与基线写死在此, 跑完不得改动) ────────────────────────────
TAG = "cn56r2"
# (臂名, top_n) —— top_n=None 表示走生产 state_map 路径, 仅作有效性控制, 不参与判据
ARMS = [("prod_native", None), ("c873_fix6", 6), ("c873_fix5", 5)]
JUDGE_ARMS = ("c873_fix6", "c873_fix5")           # 只有这两臂进判据
WINDOWS = [("prod", "20190722", None), ("full", "20170101", None)]
TOL = 5e-5
# §四: 现行代码产出值(2026-09-12 D06/D07 限幅修复已进生产后)
BASE = {
    "prod": {"ann": 0.2377, "mdd": -0.2410, "sys_ann": 0.2047,
             "sys_mdd": -0.1966, "sys_mdd_daily": -0.3172},
    "full": {"ann": 0.1236, "mdd": -0.5126, "sys_ann": 0.1090,
             "sys_mdd": -0.4210, "sys_mdd_daily": -0.4264},
}
# §五: 第 1 轮(`ab_const_n_5v6.py`)的实测值, 用于可复现性对照(登记项)
R1 = {
    ("c873_fix6", "prod"): {"ann": 0.2377, "mdd": -0.2410, "sys_ann": 0.2047,
                            "sys_mdd": -0.1966, "sys_mdd_daily": -0.3172},
    ("c873_fix6", "full"): {"ann": 0.1236, "mdd": -0.5126, "sys_ann": 0.1090,
                            "sys_mdd": -0.4210, "sys_mdd_daily": -0.4264},
    ("c873_fix5", "prod"): {"ann": 0.2157, "mdd": -0.2335, "sys_ann": 0.2027,
                            "sys_mdd": -0.2042, "sys_mdd_daily": -0.3360},
    ("c873_fix5", "full"): {"ann": 0.0911, "mdd": -0.5648, "sys_ann": 0.1018,
                            "sys_mdd": -0.5064, "sys_mdd_daily": -0.5259},
}
# 报告 B3 的算式: 2 万本金下 0.873 名义投放 ≈ 17460 元; 实测实投更少(整手+行业上限)
INVESTABLE_LEVELS = [17460.0, 12000.0, 24000.0]


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


def _fee_arithmetic():
    """B3 的原始算式: 不同 N 下单笔金额与往返费率, 以及 feasible_n 随 investable 变。**描述性, 非判据。**"""
    from app.portfolio import optimizer as O
    print("\n" + "=" * 96)
    print("辅助读数 · B3 的原始费用算式(描述性, 不作判据) —— 日期取 20260831")
    print("=" * 96)
    print("{:>10} {:>4} {:>12} {:>12} {:>10}".format(
        "可投金额", "N", "单笔金额", "往返费率", "费率倍数"))
    for inv in INVESTABLE_LEVELS:
        rates = {}
        for _tag, n in ARMS[1:]:
            rates[n] = O.round_trip_rate(inv / n, "20260831")
        for _tag, n in ARMS[1:]:
            print("{:>10.0f} {:>4d} {:>12.0f} {:>11.4%} {:>9.2f}x".format(
                inv, n, inv / n, rates[n], rates[n] / rates[6]))
        # feasible_n 自身在该可投额上解出几只(用未标定的默认目标 0.5%)
        f = O.feasible_n(inv, date="20260831")
        print("{:>10}    └ feasible_n(target=0.5%) = {} (单笔 {:.0f} 元, 往返 {:.4%})".format(
            "", f["n"], f["per_position"], f["round_trip"]))
    print("  ⇒ B3 的「5 只」随 investable 变化(17460→7, 12000→5, 24000→10),")
    print("    它不是策略属性, 是一个未标定输入的函数。")
    print()


# ── 跑臂 ────────────────────────────────────────────────────────────────────
out = []
t0 = time.time()
for tag, nfix in ARMS:
    for wname, start, end in WINDOWS:
        run_tag = "{}_{}_{}".format(TAG, tag, wname)
        m = E.run_backtest(top_n=nfix, run_tag=run_tag, start=start, end=end)
        rd = m["run_date"]
        row = {
            "arm": tag, "n": nfix, "win": wname, "run_date": rd,
            "capital": m.get("capital"),
            "ann": m["ann_return"], "mdd": m["max_drawdown"],
            "sys_ann": m["sys_ann_return"], "sys_mdd": m["sys_max_drawdown"],
            "sys_mdd_daily": m["sys_mdd_daily"], "sys_sharpe": _sys_sharpe(rd),
            "idle": m["avg_cash_idle"], "n_avg": m["avg_stocks"],
            "periods": m["periods"],
        }
        out.append(row)
        print("[{:>5.0f}s] {}".format(time.time() - t0,
                                      json.dumps(row, ensure_ascii=False)), flush=True)

# ── ① 控制臂有效性检查 ──────────────────────────────────────────────────────
print("\n" + "=" * 96)
print("① 控制臂有效性检查(G1: 生产路径复现现行基线; G2: top_n=6 臂 ≡ 生产路径)")
print("=" * 96)
ctrl_ok = True
for gate, arm in (("G1", "prod_native"), ("G2", "c873_fix6")):
    print("  {} —— 臂 {}".format(gate, arm))
    for wname, _s, _e in WINDOWS:
        r = next(x for x in out if x["arm"] == arm and x["win"] == wname)
        b = BASE[wname]
        diffs = {k: abs(r[k] - b[k]) for k in b}
        bad = {k: v for k, v in diffs.items() if v > TOL}
        flag = "✓ 逐位一致" if not bad else "✗ 漂移: " + json.dumps(bad)
        if bad:
            ctrl_ok = False
        print("    {:>5}: {}".format(wname, flag))
        for k in b:
            print("           {:<16} 实测 {:>+9.4f}  基线 {:>+9.4f}  |Δ| {:.2e}".format(
                k, r[k], b[k], diffs[k]))

# ── ①b 可复现性对照(登记项, 非判据) ─────────────────────────────────────────
print("\n" + "=" * 96)
print("①b 可复现性对照 —— 本轮 fix6/fix5 是否与第 1 轮逐位相同(登记项, 不作判据)")
print("=" * 96)
for key, b in R1.items():
    r = next(x for x in out if (x["arm"], x["win"]) == key)
    diffs = {k: abs(r[k] - b[k]) for k in b}
    bad = {k: v for k, v in diffs.items() if v > TOL}
    print("  {:<10} {:<5}: {}".format(key[0], key[1],
                                      "✓ 与第 1 轮逐位相同" if not bad
                                      else "✗ 有差异: " + json.dumps(bad)))

# ── ② 判据 ──────────────────────────────────────────────────────────────────
print("\n" + "=" * 96)
print("② 预注册判据(4 个不等式, 全成立才换 5 只)")
print("=" * 96)
verdicts = []
for wname, _s, _e in WINDOWS:
    a = next(x for x in out if x["arm"] == "c873_fix5" and x["win"] == wname)
    b = next(x for x in out if x["arm"] == "c873_fix6" and x["win"] == wname)
    for axis, key in (("年化", "sys_ann"), ("回撤", "sys_mdd")):
        ok = a[key] >= b[key]
        verdicts.append(ok)
        print("  {:<5} {:<4} 5只 {:>+9.4f}  vs  6只 {:>+9.4f}   |Δ| {:>+8.4f}   {}".format(
            wname, axis, a[key], b[key], a[key] - b[key], "成立" if ok else "**不成立**"))

# ── ③ 全量读数表 ────────────────────────────────────────────────────────────
print("\n" + "=" * 96)
print("③ 全量读数(登记用; 判据只看上面 4 个不等式)")
print("=" * 96)
for wname, _s, _e in WINDOWS:
    rows = [r for r in out if r["win"] == wname]
    print("\n窗口 {} (capital={}, {} 期)".format(wname, rows[0]["capital"], rows[0]["periods"]))
    print("{:>12} {:>10} {:>10} {:>10} {:>10} {:>10} {:>8} {:>8} {:>8}".format(
        "臂", "满仓年化", "满仓回撤", "系统年化", "系统回撤", "日频回撤", "夏普", "实持", "闲置"))
    for r in rows:
        print("{:>12} {:>+9.2%} {:>10.2%} {:>+9.2%} {:>10.2%} {:>10.2%} {:>8.2f} {:>8.2f} {:>7.1%}".format(
            r["arm"], r["ann"], r["mdd"], r["sys_ann"], r["sys_mdd"],
            r["sys_mdd_daily"], r["sys_sharpe"], r["n_avg"], r["idle"]))

_fee_arithmetic()

# ── 判决 ────────────────────────────────────────────────────────────────────
print("=" * 96)
if not ctrl_ok:
    print("判决: **本轮作废** —— 控制臂未通过 G1/G2, 说明代码或数据已漂移。")
    print("      不解读实验结果。修好漂移后重跑。")
elif all(verdicts):
    print("判决: **4 个不等式全成立 ⇒ 改用固定 5 只**")
    print("      ⚠️ 换档需独立记录新 pin, 并按项目纪律同步 config.yaml 与需求方确认。")
else:
    print("判决: **维持固定 6 只** —— 5 只未能在两窗口两轴上全部不劣。")
    print("      ⇒ B3 的「不一致」正确表述是: 0.5% 目标未标定, 不该反向指挥策略参数。")
    print("         建议在 config.yaml 的 max_stocks 注释里登记本次判决。")
print("=" * 96)
print("\n运行留痕(按纪律保留, 不自清):")
for r in out:
    print("  {:<12} {:<5} {}".format(r["arm"], r["win"], r["run_date"]))
