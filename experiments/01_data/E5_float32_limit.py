"""E5 —— `_exec_side` 限价入参量化(float32 → 分精度) A/B 预注册 (2026-09-12)

审查报告 **P0-03** / 上轮实验 `D06_D07_limit_fill.py` **§E5 现成草案**的正式预注册。
⚠️ 2026-09-28 时效注记: 文中"现行 pin"系 20260912_limitquant 档写作时点读数(23.77% 等);
现役 pin 已重钉至 20260928_excl5mdd —— 本脚本读数为历史档, 只作 E5 判决证据引用。


================================================================================
预注册（跑任何一臂之前写的；脚本 sha256 与判据一起落盘）
================================================================================

## 1. 来龙去脉（上轮已冻结的证据, 本轮不重新论证）

`app/backtest/engine.py:_exec_side` 从 **float32** 市场矩阵取 `pre_close` 再推限价:

    round2(2.10 × 0.95)              = 2.00  ← float64 公式, 与交易所一致
    round2(float(np.float32(2.10)) × 0.95) = 1.99  ← 引擎运行时, 低一分

`round2` 的 1e-9 容差按 float64 标定(缩放单位下只有 1e-7 分), 而 float32 表示误差
是 5e-5 分 —— 小 500 倍。全样本 10.37M 格中 **162,414 格限价被取低一分, 方向 100%
向下**(E2), 组合层代价 = 比真值限价高 **1.07pp/年**(系统线 prod 85 期), 全部来自
`002638.SZ *ST勤上` 2020-07-01~07 的连续 5 个一字跌停被判成「卖得出去」(E1)。

**实盘不受影响**(`paper/engine.py:140` 走 DB float64 标量 + 开盘价判一字板)——
这是纯回测口径缺陷。⇒ 不构成资金风险, 但它让**回测成绩系统性乐观**, 且与真值
不同源。这是本轮的核心动机。

## 2. 相对 §E5 草案的改动：**只有收紧, 没有放宽**（改在跑数之前）

| | §E5 草案 | 本预注册 | 为什么 |
|---|---|---|---|
| 核心判据 | 「四个数(满仓/系统 × 年化/回撤)与臂 C 逐位一致」 | **两窗的月频 + 日频净值序列逐点一致**(附 sha256 指纹) | 4 位小数的汇总数**可能碰撞** —— 两条不同的净值路径可以有相同的年化与回撤。净值序列是同一个量的更强形式, 不是替代判据 |
| 替身自证 | (无) | 新增 **P3**: 臂 2b(替身走推导)必须逐位复现臂 0(生产原样) | 上一轮 C 节的教训: 手抄替身抄错的方向恰好是「看起来一切正常」。替身上次的 P4 是**上一次执行**的自证, 不能替这一次担保 |

**没有改的**: 假设、臂的语义、P4 式守卫、「不接受的替代判据」、以及「不改判据」本身。
判据从草案到现在**只变严**, 且变严写在跑数之前 —— 这两条同时成立才不算「看到结果
再改标准」。

## 3. 四条臂（唯一变量 = 限价来源/精度）

| 臂 | 实现 | 限价怎么来 |
|---|---|---|
| **0 守卫** | **生产原样**(不 patch、不设开关) | 推导, float32（现行 pin 口径） |
| **1 候选** | **生产原样** + `engine.PRE_CLOSE_QUANTIZE = True` | 推导, **先量化回分**再推 |
| **2 真值** | 冻结脚本的替身 `make_observed(truth=conn)` | 读 `stk_limit` **公布值** |
| **2b 替身自证** | 同一个替身, `truth=None` | 推导, float32（应与臂 0 相同） |

⚠️ **臂 1 走的是真代码, 不是替身** —— 这是本轮相对上一轮的结构性改进: 候选臂的行为
不需要靠「我抄得对不对」来担保, 因为 `PRE_CLOSE_QUANTIZE` 只有一行、且是**在调用侧**
(`pv_f = round(float(pv), 2) if PRE_CLOSE_QUANTIZE else float(pv)`)。关闭时
`float(pv)` 对 float32 是精确转换 ⇒ 与改动前**逐位相同**。

## 4. 判据（冻结）

- **P1 守卫**: 臂 0(生产原样, 开关关闭)两窗 **10 式**逐位复现 pin `20260912_fullhist`
  —— prod 85 期 满仓 23.77%/−24.10% + 系统 20.47%/−19.66%; full 115 期 满仓
  12.36%/−51.26% + 系统 10.90%/−42.10%; 外加两窗期数。不过 ⇒ **本实验作废**
  (说明开关默认值的「逐位不变」承诺是假的, 先去修那条)。
- **P2 核心(组合层两轴, 强形式)**: 臂 1 与臂 2(真值限价)在两窗上的
  **月频净值序列** `(nav, sys_nav, bench_nav)` 与**日频净值序列**逐点相等
  (容差 0, 报 sha256 指纹 + 最大绝对差)。**这是等式, 不是近似**。
  依据: 上一轮 `diag_d67_truth_diff.py` 已证两窗内「推导 vs 公布」的**判定级**差异
  只有 5 格(全部是 float32 那批), 而这 5 格恰好是 P2 要消掉的东西。
- **P3 替身自证**: 臂 2b 在 prod 窗逐位复现臂 0。不过 ⇒ 臂 2 的读数不可信,
  **本实验作废**(不得拿「臂 1 ≈ 臂 2 大概是对的」凑合)。
- **覆盖率守卫(读数, 不是判据)**: 臂 2 的 `stk_limit` 回退率必须 ≤ 1%, 否则该臂
  不是有效测量, 只报回退率不下结论。

### 不接受的替代判据（明写, 防事后松绑）

- 「年化/回撤变好了」—— **修复的正确性不由业绩判定, 由与真值臂一致判定**。
  (已知方向: 修复后成绩**变差**约 1pp/年; 若跑出「变好」, 先怀疑插桩错了。)
- 「差距 < 0.5pp / 只差回撤 / 只是接近」—— P2 是等式。差一点就是 FAIL。
- 「P2 差一点但 P3 过了, 所以大概是替身的问题」—— 反过来: P3 过了才轮到 P2 说话。
- **若 P2 FAIL**: 唯一允许的动作是**逐格归因**(把两臂的限价判定差异按 D06/D07 §L1 的
  归因桶分类), 不许把判据改成「差距 < X」或「只比年化」。归因不到桶里的格 = 新缺陷,
  登记后再谈。

## 5. 明写：本轮不做的事

- **不接线 `no_limit_days()`**(上市初期无涨跌幅限制)。它是 L1 的另一个归因类别,
  接线 = 改成交口径 = 又一个独立 arm; 混在一起就分不清哪条贡献了什么(§E5 附带条款)。
- **不动 P0-01(改开盘价成交)、P0-02(重叠持仓 Δshares 成本)、P0-04(PIT 第 7 层)**。
  四条各自独立预注册 —— 一次只动一个变量。
- **不改判据、不删证据**。结果出来后本文件只允许追加「结果」一节(与 D06/D07 同一格式)。
- **不动生产默认值**。`PRE_CLOSE_QUANTIZE` 保持 False; 翻默认值 + 重钉 pin + 重钉
  golden 是**判决通过之后的独立动作**, 且要写进 config.yaml 的 pin 注释。

## 6. 判决通过后要做的（先写下来, 免得到时候忘记）

1. `app/backtest/engine.py` 的 `PRE_CLOSE_QUANTIZE` 默认值改 True, 注释改成「现行口径」。
2. `tests/test_backtest_golden.py` 快照重钉(REGEN_GOLDEN=1 → 人工 diff → 改名提交)。
3. `config.yaml` 的 pin 注释追加新档, 旧档降为历史档(标注「float32 限价口径, 约乐观
   1.07pp/年」)。新 pin 值 = 臂 1 的四个数(= 臂 2 的)。
4. 证据台账登记: 本脚本、`E5_float32_result.json`、日志、7 条 `backtest_summary`/
   `backtest_nav*` 行。

用法:
    ./.venv/Scripts/python.exe experiments/01_data/E5_float32_limit.py            # 全跑(约 20 分钟)
    ./.venv/Scripts/python.exe experiments/01_data/E5_float32_limit.py --arms 0,2b  # 只跑守卫

================================================================================
结果（2026-09-12, 840s, 7 次回测; **判据一个字都没动**）
================================================================================
跑数时的 `app/backtest/engine.py` 修订 sha256[:16] = `65c8809fecb8acab`
(该修订 = 现行生产 + 默认关闭的 `PRE_CLOSE_QUANTIZE`; 判决后翻默认值 ⇒ 见下方「落地」)。

| 臂 | prod 85 期 满仓/系统 | full 115 期 满仓/系统 | 净值指纹(daily/monthly) |
|---|---|---|---|
| 0 生产原样 | 23.77%/−24.10% · 20.47%/−19.66% | 12.36%/−51.26% · 10.90%/−42.10% | 3664634d… / bc78ec68… |
| 1 候选(**量化**) | **22.62%**/−24.10% · **19.40%**/−19.66% | **11.59%**/−51.26% · **10.16%**/−42.10% | 6e5f8fc3… / 29c9e74e… |
| 2 真值(读 stk_limit) | 22.62%/−24.10% · 19.40%/−19.66% | 11.59%/−51.26% · 10.16%/−42.10% | 6e5f8fc3… / 29c9e74e… |
| 2b 替身自证 | 23.77%/−24.10% · 20.47%/−19.66% | (未跑) | 3664634d… / bc78ec68… |

- **P1 = PASS**(10 式; 开关关闭态逐位复现 pin `20260912_fullhist`)。
- **P3 = PASS**: 臂 2b 与臂 0 **逐点相等**(四个序列最大绝对差 `0.000e+00`)⇒ 替身今天
  仍然忠实, 臂 2 的读数可以采信。
- **P2 = PASS(核心): 臂 1 与臂 2 两窗四个序列逐点相等, 最大绝对差 `0.000e+00`,
  sha256 指纹逐个相同** ⇒ **量化后的推导限价与交易所公布值在组合层不可区分**。这正是
  §E5 草案要的那句话 —— 不是"接近", 是**等号**。
- 覆盖率守卫: 臂 2 的 `stk_limit` 回退率 **0.00%**(prod 1344 笔 / full 1770 笔)⇒
  读数不是被回退路径稀释出来的。
- 定向读数(预期为**变差**, 因为修的是"回测比实盘乐观"): prod 满仓 **−1.15pp** / 系统
  **−1.07pp**; full 满仓 −0.77pp / 系统 −0.74pp; **两窗回撤一格未动**(与 D06/D07 的
  B→C 读数逐位吻合, 独立复现)。
- ⭐ 臂 2 被拒 2 笔跌停卖单, 臂 0/2b 一笔都不拒 —— 那 2 笔就是 002638.SZ 那 5 格
  (同一次扫描连续被挡, 归并成 2 笔委托)在组合层的全部痕迹。**1.07pp/年 的来源被
  端到端地钉死了**。

**自我审查: 这轮有没有"看到结果改判据"?** 没有。相对 §E5 草案的两处改动(判据收紧为
净值序列逐点、新增 P3 替身自证)**都写在跑数之前**, 且方向只有紧; 判据在跑之前就冻结
进脚本 sha(`1e2574325686d44d`)。草案里"不接受的替代判据"四条一条没用上 —— 因为 P2
成立, 不需要辩解。

**落地(判决通过之后的动作, 与判据分开记账)**:
1. `app/backtest/engine.py` 的 `PRE_CLOSE_QUANTIZE` 默认值翻 True(修订
   `5b14100e1e5ab4c5`); 旧口径**保留为 False**, 于是**旧 pin 有了再生路径** ——
   这正是 `scripts/pin_baseline.py` 事故记录里缺的那一环。
2. `scripts/pin_baseline.py --tag limitquant` 重钉(新 pin = 臂 1 的四个数)。
3. `tests/test_limit_precision.py`(8 例): 默认值锁 + 半进位行为锁 + 窄面锁。
4. 证据台账登记; `D06_D07_limit_fill.py` **一字不改**(它是那一轮的冻结判据, 不是本轮
   的草稿本)。

**golden 快照的读数(值得单独记一笔)**: `REGEN_GOLDEN=1` 重跑 `tests/test_backtest_golden.py`
⇒ 两个快照的 `.new` 与旧档**字节完全相同**, 即**不需要重钉**。结论有两条, 都是有用的:

- 本次修复是**窄面**的(只动 `floor(x+0.5)` 的半进位格), 合成宇宙(60 股 × 30 个月的
  整数价位)一格都没落在面上 —— 与 §3「窄面锁」的断言互相印证。
- ⚠️ 同时说明 **golden 快照看不见这个缺陷**(缺陷在它出生那天就在, 快照一直是绿的)。
  ⇒ 这正是不许把"测试全绿"当成交口径证据的实例; 本缺陷的覆盖由
  `tests/test_limit_precision.py` 用**真案数值**(2.10 × 0.95 = 1.995)补上, 而不是
  改合成宇宙去撞半进位(那会让全部 golden 数值无谓重钉)。

**遗留(明写, 别当成已修)**: 上市初期无涨跌幅限制(`no_limit_days()`)在**回测里仍未
接线**(模拟盘有), 它与本轮无关、是独立 arm; 本轮把"限价数值本身"修对了, 没有碰
"哪些格子该有限价"。
"""
import argparse
import hashlib
import importlib.util
import io
import json
import sys
import time
from pathlib import Path

_ORIG_STDOUT = sys.stdout          # 必须留住引用, 否则底层 buffer 被 GC 关掉
if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(_ORIG_STDOUT.buffer, encoding="utf-8", errors="replace")

BASE = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE))

import numpy as np                                        # noqa: E402
import pandas as pd                                       # noqa: E402

from app import db                                        # noqa: E402
from app.backtest import engine as E                      # noqa: E402
from app.logger import setup_logger                       # noqa: E402

LOG = Path("D:/claude-home/tmp/E5_float32_limit.log")
OUT = BASE / "experiments" / "01_data" / "E5_float32_result.json"

# --- 判据常量: 冻结在预注册里, 结果出来之前不许改 -----------------------------
# pin `20260912_fullhist`（来源: config.yaml:14-19 的注释 + backtest_summary 对应 run）
PIN = {
    "prod": dict(ann=0.2377, mdd=-0.2410, sys_ann=0.2047, sys_mdd=-0.1966, n=85),
    "full": dict(ann=0.1236, mdd=-0.5126, sys_ann=0.1090, sys_mdd=-0.4210, n=115),
}
WINDOWS = (("prod", "20190722"), ("full", "20170101"))
TOL = 5e-4          # 与 pin 比对口径(engine 存的是 4 位小数)
EXACT = 1e-12       # 净值序列「逐点相等」的数值容差(逐位相同 ⇒ 差应为 0.0)
FALLBACK_MAX_PCT = 1.0   # 真值臂的 stk_limit 回退率上限(覆盖率守卫)

# 冻结脚本按**路径**加载(项目惯例, 见 tests/test_expert.py 的 _frozen_gate):
# 不搬动、不复制、不改它 —— 替身的可信度来自它自己那轮的 P4(臂 B 复现 pin),
# 不是我在这里重抄一遍(重抄 = 上一轮 C 节那个"手抄错得看不出来"的坑)。
FROZEN = BASE / "experiments" / "01_data" / "D06_D07_limit_fill.py"


class Say:
    """同时写控制台与日志文件(项目惯例: 证据落 D:/claude-home/tmp/)。"""

    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.f = open(path, "w", encoding="utf-8")
        self.lines = []

    def __call__(self, s=""):
        print(s, flush=True)
        self.lines.append(s)
        self.f.write(s + "\n")
        self.f.flush()

    def close(self):
        self.f.close()


def script_sha() -> str:
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:16]


def load_frozen():
    """按路径加载冻结的 D06/D07 脚本, 取它的替身工厂 `make_observed` + 记录表。

    ⚠️ 该模块顶层会**再包一次 sys.stdout**(它自己防编码陷阱的做法)。双层 TextIOWrapper
    指向同一个底层 buffer, 先后顺序无所谓; 但加载前必须 flush, 否则我这边缓冲的字会被
    夹在两次换壳之间丢掉。
    """
    sys.stdout.flush()
    spec = importlib.util.spec_from_file_location("_frozen_d67_limit_fill", FROZEN)
    if spec is None or spec.loader is None:
        raise ImportError(f"冻结脚本加载失败: {FROZEN}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def fingerprint(df: pd.DataFrame) -> str:
    """净值矩阵的**位级**指纹 —— 「逐位一致」这句话的物证。"""
    arr = np.asarray(df[["nav", "sys_nav", "bench_nav"]].to_numpy(), dtype=np.float64)
    return hashlib.sha256(arr.tobytes()).hexdigest()[:16]


def fetch_nav(run_date: str):
    conn = db.get_conn()
    try:
        col = "trade_date, nav, sys_nav, bench_nav"
        daily = pd.read_sql(
            f"SELECT {col} FROM backtest_nav_daily WHERE run_date=? ORDER BY trade_date",
            conn, params=(run_date,))
        monthly = pd.read_sql(
            f"SELECT {col} FROM backtest_nav WHERE run_date=? ORDER BY trade_date",
            conn, params=(run_date,))
    finally:
        conn.close()
    return daily, monthly


def run_arm(say, tag, window, start, mode, frozen):
    """跑一条臂。mode ∈ {prod, quant, truth, truth_none}。返回读数 dict。

    prod/quant 走**真代码**(唯一差别是模块开关 `E.PRE_CLOSE_QUANTIZE`);
    truth/truth_none 走冻结替身(限价读 `stk_limit` / 走推导)。
    """
    from unittest import mock

    obs, fb, truth = None, None, None
    if mode in ("prod", "quant"):
        E.PRE_CLOSE_QUANTIZE = (mode == "quant")
    else:
        conn = db.get_conn()
        truth = None if mode == "truth_none" else {"conn": conn}
        frozen.ORDER.clear()
        frozen.BLOCKED.clear()
        fb = [0]
        obs = frozen.make_observed(truth, fb)
        E.PRE_CLOSE_QUANTIZE = False

    run_tag = "e5_%s_%s" % (mode, window)
    t0 = time.time()
    try:
        if obs is None:                      # 生产原样: 不 patch 任何东西
            met = E.run_backtest(score_table="model_score", run_tag=run_tag, start=start)
        else:                                # 替身臂: 只换 _exec_side
            with mock.patch.object(E, "_exec_side", obs):
                met = E.run_backtest(score_table="model_score", run_tag=run_tag,
                                     start=start)
    finally:
        if truth is not None:
            truth["conn"].close()
        E.PRE_CLOSE_QUANTIZE = False

    daily, monthly = fetch_nav(met["run_date"])
    why = {}
    if obs is not None:
        for o in frozen.ORDER:
            why[o["why"]] = why.get(o["why"], 0) + 1
    st = dict(
        tag=run_tag, mode=mode, window=window, start=start, run_date=met["run_date"],
        secs=round(time.time() - t0, 1),
        ann=met["ann_return"], mdd=met["max_drawdown"],
        sys_ann=met["sys_ann_return"], sys_mdd=met["sys_max_drawdown"],
        mdd_daily=met["mdd_daily"], sys_mdd_daily=met["sys_mdd_daily"],
        sharpe=met["sharpe"], ic_mean=met["ic_mean"], icir=met["icir"],
        avg_turnover=met["avg_turnover"], avg_cash_idle=met["avg_cash_idle"],
        n=met["periods"], n_daily=len(daily), n_monthly=len(monthly),
        daily_fp=fingerprint(daily), monthly_fp=fingerprint(monthly),
        orders=len(frozen.ORDER) if obs is not None else None,
        why=why or None,
        fallback=(fb[0] if obs is not None else None),
        fallback_pct=(100.0 * fb[0] / max(1, len(frozen.ORDER))
                      if obs is not None else None),
    )
    say("  [%-16s] %-4s 满仓 %.2f%%/%.2f%%  系统 %.2f%%/%.2f%%  (%d 期, %.0fs)"
        % (tag, window, st["ann"] * 100, st["mdd"] * 100,
           st["sys_ann"] * 100, st["sys_mdd"] * 100, st["n"], st["secs"]))
    say("                   日频 %d 行 fp=%s | 月频 %d 行 fp=%s"
        % (st["n_daily"], st["daily_fp"], st["n_monthly"], st["monthly_fp"]))
    if obs is not None:
        say("                   委托 %d 笔 | 回退 %s (%.2f%%) | 去向: %s"
            % (st["orders"], st["fallback"], st["fallback_pct"] or 0.0,
               " ".join("%s=%d" % kv for kv in sorted(why.items()))))
    return st


def cmp_nav(say, a, b, label) -> bool:
    """两臂的净值序列逐点比对(日频 + 月频)。返回是否逐点相等。"""
    ok = True
    for kind, i in (("日频", 0), ("月频", 1)):
        va, vb = fetch_nav(a["run_date"])[i], fetch_nav(b["run_date"])[i]
        if len(va) != len(vb) or list(va["trade_date"]) != list(vb["trade_date"]):
            say("    %s %s: **长度/日期不同** %d vs %d"
                % (label, kind, len(va), len(vb)))
            ok = False
            continue
        worst = {c: float(np.max(np.abs(np.asarray(va[c], dtype=np.float64)
                                        - np.asarray(vb[c], dtype=np.float64))))
                 for c in ("nav", "sys_nav", "bench_nav")}
        same = max(worst.values()) <= EXACT
        ok &= same
        say("    %s %s: 最大绝对差 %s ⇒ %s"
            % (label, kind, " ".join("%s=%.3e" % kv for kv in worst.items()),
               "逐点相等" if same else "**不等**"))
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--arms", default="0,1,2,2b",
                    help="要跑的臂, 逗号分隔(0=生产原样 1=候选 2=真值 2b=替身自证)")
    args = ap.parse_args()
    wanted = [a.strip() for a in args.arms.split(",") if a.strip()]
    setup_logger()
    db.init_db()
    say = Say(LOG)
    t0 = time.time()
    say("=" * 100)
    say("E5 限价入参量化(float32 → 分精度) —— 审查 P0-03 / D06_D07 §E5")
    say("脚本 sha256[:16] = %s   (预注册冻结在这个哈希上)" % script_sha())
    say("开关现值 PRE_CLOSE_QUANTIZE = %s" % E.PRE_CLOSE_QUANTIZE)
    say("=" * 100)
    frozen = load_frozen()

    res = {}
    # 臂 0/1 走真代码(PRE_CLOSE_QUANTIZE False/True), 臂 2/2b 走冻结替身。
    for win, start in WINDOWS:
        say("\n--- %s 窗(start=%s) ---" % (win, start))
        res[win] = {}
        if "0" in wanted:
            res[win]["0"] = run_arm(say, "arm0 生产原样", win, start, "prod", frozen)
        if "1" in wanted:
            res[win]["1"] = run_arm(say, "arm1 候选", win, start, "quant", frozen)
        if "2" in wanted:
            res[win]["2"] = run_arm(say, "arm2 真值", win, start, "truth", frozen)
        if "2b" in wanted and win == "prod":
            res[win]["2b"] = run_arm(say, "arm2b 替身自证", win, start, "truth_none", frozen)

    verdict = {}
    # ---- P1: 臂 0 逐位复现 pin ----
    say("\n" + "=" * 100)
    say("判据 P1(臂 0 生产原样逐位复现 pin %s, 两窗 10 式)" % "20260912_fullhist")
    bad = []
    for win in ("prod", "full"):
        a0 = res.get(win, {}).get("0")
        if a0 is None:
            continue
        for k, want in PIN[win].items():
            got = a0[k] if k != "n" else a0["n"]
            if got is None or abs(got - want) >= TOL:
                bad.append("%s.%s got=%s want=%s" % (win, k, got, want))
    for b in bad:
        say("    未过: %s" % b)
    verdict["P1"] = "PASS" if (not bad and len(res.get("prod", {})) > 0) else "FAIL"
    say("⇒ P1 = %s%s" % (verdict["P1"],
                        "" if not bad else "  ** 开关默认值的逐位承诺是假的, 实验作废 **"))

    # ---- P3: 替身自证(先看它, 再看 P2 —— 替身不可信时 P2 没有意义) ----
    if "2b" in res.get("prod", {}):
        say("\n判据 P3(臂 2b 替身走推导, 应逐位复现臂 0 = 替身今天仍然忠实)")
        ok3 = cmp_nav(say, res["prod"]["0"], res["prod"]["2b"], "臂0 vs 臂2b")
        for k in ("ann", "mdd", "sys_ann", "sys_mdd"):
            if abs(res["prod"]["0"][k] - res["prod"]["2b"][k]) >= TOL:
                say("    %s 不同: %s vs %s" % (k, res["prod"]["0"][k], res["prod"]["2b"][k]))
                ok3 = False
        verdict["P3"] = "PASS" if ok3 else "FAIL"
        say("⇒ P3 = %s%s" % (verdict["P3"],
                            "" if ok3 else "  ** 替身不忠实, 臂 2 的读数作废, 实验作废 **"))

    # ---- P2: 核心判据 ----
    if "1" in wanted and "2" in wanted:
        say("\n判据 P2(核心: 臂 1 候选 与 臂 2 真值限价, 两窗净值序列逐点相等)")
        ok2 = True
        for win in ("prod", "full"):
            if "1" not in res.get(win, {}) or "2" not in res.get(win, {}):
                continue
            ok2 &= cmp_nav(say, res[win]["1"], res[win]["2"], "臂1 vs 臂2 %s" % win)
            for k in ("ann", "mdd", "sys_ann", "sys_mdd", "mdd_daily", "sys_mdd_daily"):
                if abs(res[win]["1"][k] - res[win]["2"][k]) >= TOL:
                    say("    %s.%s 不同: 臂1=%s 臂2=%s"
                        % (win, k, res[win]["1"][k], res[win]["2"][k]))
                    ok2 = False
            fb = res[win]["2"].get("fallback_pct")
            if fb is not None and fb > FALLBACK_MAX_PCT:
                say("    %s 回退率 %.2f%% > %.1f%% ⇒ 覆盖率守卫不过, 该臂不是有效测量"
                    % (win, fb, FALLBACK_MAX_PCT))
                ok2 = False
        verdict["P2"] = "PASS" if ok2 else "FAIL"
        say("⇒ P2 = %s" % verdict["P2"])

    # ---- 报告: 相对现行 pin 的代价 ----
    say("\n--- 读数(相对 pin 的 Δ; 修复方向预期为**变差**) ---")
    for win in ("prod", "full"):
        a0, a1 = res.get(win, {}).get("0"), res.get(win, {}).get("1")
        if not a0 or not a1:
            continue
        say("  %s: 满仓 %.2f%%→%.2f%% (Δ%+.2fpp) | 系统 %.2f%%→%.2f%% (Δ%+.2fpp) | "
            "回撤 满仓 %.2f%%→%.2f%% 系统 %.2f%%→%.2f%%"
            % (win, a0["ann"] * 100, a1["ann"] * 100, (a1["ann"] - a0["ann"]) * 100,
               a0["sys_ann"] * 100, a1["sys_ann"] * 100,
               (a1["sys_ann"] - a0["sys_ann"]) * 100,
               a0["mdd"] * 100, a1["mdd"] * 100,
               a0["sys_mdd"] * 100, a1["sys_mdd"] * 100))

    say("\n总耗时 %.0fs" % (time.time() - t0))
    OUT.write_text(json.dumps(dict(script_sha=script_sha(), verdict=verdict, arms=res),
                              ensure_ascii=False, indent=1), encoding="utf-8")
    say("结果落盘: %s" % OUT)
    say.close()


if __name__ == "__main__":
    main()
