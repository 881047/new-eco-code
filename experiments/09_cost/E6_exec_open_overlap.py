"""E6 —— 执行模型 A/B 预注册: P0-01(开盘价成交) / P0-02(重叠持仓 Δshares 计费)

来源: 2026-09-12 外部审查 `NEW_ECO_REVIEW.json` 的 P0-01、P0-02(两项 `verified: true`,
`requires_repin_baseline: true`)。本文件是它们的**独立预注册 + A/B 判据**, 判据在跑数前
冻结(下面 §2/§4 写完才允许启动), 结果只许往 §7 追加。

## §1 两个命题(审查原文的准确复述)

**P0-01**: 回测 `_exec_side` 用**执行日收盘价**(`cv`)既判封板又定成交价; 模拟盘
`paper.engine._exec_price` 用**次日开盘价**(`o`)判一字板、并以 `round(o*(1±slip),3)` 成交。
⇒ 同一决策日, 回测在**已知全天走势之后**才决定成交(尾盘封死涨停的票回测照样买入),
模拟盘是**下单时盲判**。审查指出方向"单一指向回测乐观", 但两个方向其实都存在
(高开回落股: 开盘可买、收盘不可买 ⇒ 回测反而漏掉), 故必须实测。

**P0-02**: 回测对"上月在册"的股票 `is_overlap=True` ⇒ `_month_return` **直接返回收益,
零费零滑点**; 但股数 `sh` 是**本月按新价重新整手分配**的 ⇒ 真实世界里 |Δshares| 的买卖
已经发生(模拟盘 `apply_plan` 按 delta 逐笔计费, 含最低佣金非线性)。⇒ 回测这段是免费的。

## §2 与审查原文的差异(**只有收紧, 无放宽**)

| 项 | 审查的 `fix` 建议 | 本预注册 |
|---|---|---|
| P0-01 | (a) 回测改取 `m["open"]` 成交并同步封板判定 | 同 (a); **但判据不是"改了就完了"**: 要求候选臂的每一笔委托与模拟盘 `_exec_price` 的逐日扫描**逐笔一致**(允许分歧只有两个明写桶) |
| P0-02 | "按 |Δshares| 计单边费用+滑点" | 同上, **但追加三条**: ①d==0 的月份必须与控制臂**逐位相同**; ②逐月 pnl 差恒等式必须成立; ③两侧费率必须与 `fees.trade_cost` 按 **delta 本金**算出的现金成本一致(半角分差都要对齐) |
| 两 | 未提 | **新增守卫**: 两臂都必须**逐位复现现行 pin** `20260912_limitquant`(整数个指标 + 日频/月频净值 sha256), 否则整轮作废 |
| 两 | 未提 | **新增插桩自证**: 记录包装(探针)与不记录的同名臂必须逐位一致 —— 本项目栽过"手抄替身"的坑, 插桩本身也要自证 |

## §3 四臂(两窗)

| 臂 | tag | FILL_AT_OPEN | OVERLAP_REBAL_COST | 探针 | 用途 |
|---|---|---|---|---|---|
| A0 | `e6ctl`    | 关 | 关 | — | **守卫**: 逐位复现现行 pin |
| A1 | `e6open`   | **开** | 关 | — | P0-01 读数 |
| A1w| `e6openw`  | **开** | 关 | `_exec_side` | P0-01 读数 + C2 跨引擎扫描 |
| A2 | `e6ovl`    | 关 | **开** | — | P0-02 读数 |
| A2w| `e6ovlw`   | 关 | **开** | `_month_pnl` | P0-02 读数 + C3 恒等式 |
| A3 | `e6both`   | **开** | **开** | — | 交互 + (若双通过)新 pin |

两窗: `prod`=start `20190722`(85 期, 现行 pin 口径) / `full`=start `20170101`(115 期, 含 2018)。
跑数分配: prod 六臂全跑; full 只跑 A0/A1w/A2w/A3(探针自证的两个同臂对照按"同代码路径"
外推 —— 明写为**已知的覆盖缺口**, 不假装两窗都证了)。

## §4 判据(跑数前冻结; 每条都写清允许的分歧桶与"不接受的替代判据")

### C1 守卫(两项共用; 不过 ⇒ 整轮作废)
`e6ctl` 两窗必须**逐位复现** pin `20260912_limitquant` / `20260912_limitquant_full`:
- `backtest_summary.metrics` 的**全部键**(除 `run_date`)逐键相等(数值容差 1e-12);
- `backtest_nav` 与 `backtest_nav_daily` 的 nav/sys_nav/bench_nav 三列**逐点相等**(容差 1e-12)
  且 sha256 指纹相同。

### C2 = P0-01 核心(**跨引擎一致性, 不是业绩**)
对 `e6openw` 两窗记录的**每一次** `_exec_side` 调用, 用**生产代码** `paper.engine._exec_price`
(slip=0.0 ⇒ 比较裸价)在**同一组 5 个候选交易日**上逐日扫描, 要求:
1. 成交日相同(或同为"不成交"); 2. 成交裸价相同(容差 1e-9)。
不一致的每一笔必须落在**两个明写的分歧桶**之一, 否则 C2 = **FAIL**:
- **桶①`_no_limit_day`**: 模拟盘有"上市初期无涨跌幅限制"层(`paper.engine._no_limit_day`),
  回测没有 ⇒ 那些日子模拟盘判可成交、回测按有限幅顺延。出现即逐笔列出。
- **桶②close 缺失而 open 有效**: 回测的 `_exec_side` 先要求当日 close 存在(净值路径需要),
  模拟盘只要求 open/vol。

**前置校验**(不过则 C2 不可判, 先查数据):
- (a) **同源**: 矩阵 `round(float(x),2)` 与 DB float64 的 open/pre_close 逐格相等、`vol>0` 判定一致;
- (b) **日历同源**: 回测 `dates[pos+k]` == `trade_cal` 自 `dates[pos]` 起的第 k 个开市日(k=1..5);
- (c) **插桩自证**: `e6openw` 与 `e6open` 逐位一致(prod)。

### C3 = P0-02 核心(**逐笔现金等式, 不是业绩**)
对 `e6ovlw` 两窗记录的**每个**重叠 (月份, 股票):
1. **零调仓不变**: `d = sh - sh_prev == 0` ⇒ 该笔 `frac == 0.0`(位级);
2. **方向**: 所有 `frac >= 0`(费用只能减收益);
3. **本金基数**: `frac * sh * bp_raw` 必须等于 `fees.trade_cost(|d|*bp_raw, side, date, params)`
   与滑点 `|d|*bp_raw*slip` 的**乘法合成**: `|d|*bp_raw*((1+slip)*(1+rate)-1)`, 容差 1e-9 相对;
   其中 `side = "buy" if d>0 else "sell"`, `rate = trade_cost(...)/(|d|*bp_raw)`;
4. **side 非空洞**: 至少存在一行, 其 buy 费率与 sell 费率**不同**(否则第 3 条对 side 无鉴别力),
   且要求"用错 side"会**超出容差**——逐行统计"换 side 后的误差", 报告最小的那个 margin;
5. **逐月恒等式**: `pnl(A0) - pnl(A2w)` 必须逐月等于该月 Σ`sh*bp_raw*frac`(容差 1e-9),
   两条账(满仓线/系统线)**分别**比对。
   ⚠️ 该恒等式的前提是"执行价与分配路径不依赖 pnl"(引擎里 `capital` 固定、nav 不回灌分配),
   若某月不成立 ⇒ 说明改动**泄漏**到了非重叠持仓或别的记账项 ⇒ FAIL。

### P3 读数(记录用, **不作判据**)
两窗两轴的 Δ、顺延/封板拒绝计数、`d≠0` 的重叠笔数与成本总额、A3 的交互。

### §4b 明写:**不接受的替代判据**
- "数字更差/更保守所以肯定是对的" —— **不**接受。采纳条件是 C2/C3 的等式, 业绩不是理由
  (与 E5 同一条纪律: 修复的正确性不由业绩判定)。
- "四个不等式没变差" —— **不**接受替代 C2/C3。
- "差异很小, 可以忽略" —— **不**接受替代逐笔等式; 真有差异必须进桶或查清。
- 为了通过 C2 去改模拟盘 / 放宽 5 日顺延窗 / 改 `_no_limit_day` —— **禁止**(那是 P0-04 阶段 B)。

## §5 明写不做的事
不接线 `_no_limit_day`(P0-04, 属"阶段 B 重训练"批次); 不改 `app/paper/**`; 不动其余 P2 项;
不删任何 run/表(本轮的 10 个 run 是证据, 台账登记); **跑数期间两个开关默认值保持 False**
——翻默认值只在 §6 的判决通过之后。

## §6 判决通过后要做的 4 步(与 E5 同一套)
1. 翻 `app/backtest/engine.py` 的开关默认值(仅通过的项);
2. `scripts/pin_baseline.py` 重钉 + `config.yaml`/`app/config.py`/`CLAUDE.md`/
   `app/alpha/pipeline.py`/`scripts/oneoff/exp_train_months.py` 同步 + 旧档再生路径写进注释;
3. 补**回归测试**(把 C2/C3 里有鉴别力的那几条落成 pytest, 防止将来改回去);
4. 更新记忆台账(新增 run 表、pin 分层、新证据)。

## §7 结果
(跑数后追加, 不改上面任何一行)
"""
import argparse
import hashlib
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
from app import config as cfg_mod                         # noqa: E402
from app.backtest import engine as E                      # noqa: E402
from app.logger import setup_logger                       # noqa: E402
from app.paper import engine as PE                        # noqa: E402
from app.portfolio import fees as fees_mod                # noqa: E402

LOG = Path("D:/claude-home/tmp/E6_exec_open_overlap.log")
OUT = BASE / "experiments" / "09_cost" / "E6_exec_open_overlap_result.json"

# --- 判据常量: 冻结在 §2/§4, 结果出来之前不许改 ---------------------------------
PIN = {"prod": "20260912_limitquant", "full": "20260912_limitquant_full"}
WINDOWS = (("prod", "20190722"), ("full", "20170101"))
EXACT = 1e-12       # 净值序列「逐点相等」容差(逐位相同 ⇒ 差应为 0.0)
PTOL = 1e-9         # 跨引擎裸价相等容差
CTOL = 1e-9         # 逐笔现金等式相对容差
ARMS = (("A0", "e6ctl", False, False, None),
        ("A1", "e6open", True, False, None),
        ("A1w", "e6openw", True, False, "exec"),
        ("A2", "e6ovl", False, True, None),
        ("A2w", "e6ovlw", False, True, "month"),
        ("A3", "e6both", True, True, None))
FULL_ARMS = ("A0", "A1w", "A2w", "A3")     # full 窗只跑这些(见 §3 的覆盖缺口声明)


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


def fingerprint(df: pd.DataFrame) -> str:
    arr = np.asarray(df[["nav", "sys_nav", "bench_nav"]].to_numpy(), dtype=np.float64)
    return hashlib.sha256(arr.tobytes()).hexdigest()[:16]


def fetch_nav(run_date: str):
    conn = db.get_conn()
    try:
        col = "trade_date, nav, sys_nav, bench_nav"
        daily = pd.read_sql(f"SELECT {col} FROM backtest_nav_daily WHERE run_date=? "
                            "ORDER BY trade_date", conn, params=(run_date,))
        monthly = pd.read_sql(f"SELECT {col} FROM backtest_nav WHERE run_date=? "
                              "ORDER BY trade_date", conn, params=(run_date,))
    finally:
        conn.close()
    return daily, monthly


def fetch_metrics(run_date: str):
    conn = db.get_conn()
    try:
        row = conn.execute("SELECT metrics FROM backtest_summary WHERE run_date=?",
                           (run_date,)).fetchone()
    finally:
        conn.close()
    return json.loads(row[0]) if row else None


class ExecProbe:
    """`_exec_side` 记录包装(纯透传): 记下每次调用的 (code, side, 决策日, 5 个候选日,
    成交与否, 成交日, 裸价)。C2 的扫描就是拿这些记录去重放模拟盘。"""

    def __init__(self, real):
        self.real = real
        self.rows = []

    def __call__(self, m, dates, pos, code, side, st_codes=None, name_of=None):
        res = self.real(m, dates, pos, code, side, st_codes=st_codes, name_of=name_of)
        n = len(dates)
        days = [dates[min(pos + k, n - 1)] for k in range(1, E.MAX_DELAY + 1)]
        self.rows.append({
            "code": code, "side": side,
            "d": str(dates[pos])[:10].replace("-", ""),
            "days": [str(x)[:10].replace("-", "") for x in days],
            "fill_day": (str(dates[res[2]])[:10].replace("-", "")
                         if res[2] is not None else None),
            "px": (float(res[1]) if res[1] is not None else None)})
        return res


class MonthProbe:
    """`_month_pnl` 记录包装(纯透传): 逐月记 (pnl, Σdelta 成本); 期间把
    `_overlap_delta_frac` 换成记录版(同样是透传)以拿到逐笔 delta 明细。"""

    def __init__(self, real_mp, real_df):
        from unittest import mock
        self.real_mp, self.real_df, self._mock = real_mp, real_df, mock
        self.rows = []      # 逐笔 delta
        self.months = []    # 逐月(每自然月两次调用: 先满仓线、后系统线 —— 引擎固定顺序)

    def __call__(self, *a, **kw):
        dates, pos1 = a[5], a[6]
        buf = []

        # 用局部记录版替换: 只影响本次调用
        def _df(sh, sh_prev, bp_raw, date, slip, cost, fees=None):
            v = self.real_df(sh, sh_prev, bp_raw, date, slip, cost, fees)
            if sh_prev is not None:
                buf.append({"sh": float(sh), "sh_prev": float(sh_prev), "px": float(bp_raw),
                            "date": (str(date)[:10].replace("-", "")
                                     if date is not None else None), "frac": float(v)})
            return v

        with self._mock.patch.object(E, "_overlap_delta_frac", _df):
            res = self.real_mp(*a, **kw)
        self.rows.extend(buf)
        self.months.append({
            "d1": str(dates[pos1])[:10].replace("-", ""),
            "pnl": float(res[1]),
            "cost": float(sum(r["frac"] * r["sh"] * r["px"] for r in buf)),
            "n": len(buf)})
        return res


def run_arm(say, tag, window, start, open_on, ovl_on, probe):
    """跑一条臂。三种探针模式: None / "exec" / "month"。返回读数 dict。"""
    from unittest import mock
    ep, mp = None, None
    run_tag = f"{tag}_{window}"
    t0 = time.time()
    try:
        E.FILL_AT_OPEN, E.OVERLAP_REBAL_COST = open_on, ovl_on
        if probe == "exec":
            ep = ExecProbe(E._exec_side)
            with mock.patch.object(E, "_exec_side", ep):
                met = E.run_backtest(score_table="model_score", run_tag=run_tag, start=start)
        elif probe == "month":
            mp = MonthProbe(E._month_pnl, E._overlap_delta_frac)
            with mock.patch.object(E, "_month_pnl", mp):
                met = E.run_backtest(score_table="model_score", run_tag=run_tag, start=start)
        else:
            met = E.run_backtest(score_table="model_score", run_tag=run_tag, start=start)
    finally:
        E.FILL_AT_OPEN, E.OVERLAP_REBAL_COST = False, False
    daily, monthly = fetch_nav(met["run_date"])
    st = dict(tag=run_tag, window=window, start=start, run_date=met["run_date"],
              secs=round(time.time() - t0, 1), open=open_on, ovl=ovl_on, probe=probe,
              ann=met["ann_return"], mdd=met["max_drawdown"],
              sys_ann=met["sys_ann_return"], sys_mdd=met["sys_max_drawdown"],
              mdd_daily=met["mdd_daily"], sys_mdd_daily=met["sys_mdd_daily"],
              sharpe=met["sharpe"], ic_mean=met["ic_mean"], icir=met["icir"],
              avg_turnover=met["avg_turnover"], avg_cash_idle=met["avg_cash_idle"],
              avg_stocks=met["avg_stocks"], n=met["periods"],
              n_daily=len(daily), n_monthly=len(monthly),
              daily_fp=fingerprint(daily), monthly_fp=fingerprint(monthly),
              metrics={k: v for k, v in met.items() if k != "run_date"})
    say("  [%-10s] %-4s 满仓 %+7.2f%%/%+7.2f%%  系统 %+7.2f%%/%+7.2f%%  (%d 期, %.0fs)"
        % (run_tag, window, st["ann"] * 100, st["mdd"] * 100,
           st["sys_ann"] * 100, st["sys_mdd"] * 100, st["n"], st["secs"]))
    say("               日频 %d fp=%s | 月频 %d fp=%s | 闲置 %.4f 只数 %.2f"
        % (st["n_daily"], st["daily_fp"], st["n_monthly"], st["monthly_fp"],
           st["avg_cash_idle"], st["avg_stocks"]))
    if ep is not None:
        say("               记录委托 %d 笔" % len(ep.rows))
    if mp is not None:
        nz = sum(1 for r in mp.rows if r["frac"] > 0)
        say("               记录重叠笔 %d (有调仓 %d) | 成本合计 %.2f 元 | 月调用 %d"
            % (len(mp.rows), nz, sum(r["frac"] * r["sh"] * r["px"] for r in mp.rows),
               len(mp.months)))
    return st, (ep.rows if ep is not None else (mp.rows if mp is not None else None)), mp


def cmp_nav(say, a, b, label) -> bool:
    """两臂的净值序列逐点比对(日频 + 月频)。返回是否逐点相等。"""
    ok = True
    for kind, i in (("日频", 0), ("月频", 1)):
        va = fetch_nav(a)[i]
        vb = fetch_nav(b)[i]
        if len(va) != len(vb) or list(va["trade_date"]) != list(vb["trade_date"]):
            say("    %s %s: **长度/日期不同** %d vs %d" % (label, kind, len(va), len(vb)))
            ok = False
            continue
        worst = {c: float(np.max(np.abs(np.asarray(va[c], dtype=np.float64)
                                       - np.asarray(vb[c], dtype=np.float64))))
                 for c in ("nav", "sys_nav", "bench_nav")}
        same = max(worst.values()) <= EXACT
        ok &= same
        say("    %s %s: 最大绝对差 %s ⇒ %s"
            % (label, kind, " ".join("%s=%.3e" % kv for kv in sorted(worst.items())),
               "逐点相等" if same else "**不等**"))
    return ok


def cmp_metrics(say, a: dict, b: dict, label) -> bool:
    """两个 metrics 字典逐键比对(数值容差 EXACT)。"""
    ka, kb = set(a) - {"run_date"}, set(b) - {"run_date"}
    bad = []
    if ka != kb:
        bad.append("键不同: 仅A=%s 仅B=%s" % (sorted(ka - kb), sorted(kb - ka)))
    for k in sorted(ka & kb):
        x, y = a[k], b[k]
        if isinstance(x, float) or isinstance(y, float):
            if abs(float(x or 0) - float(y or 0)) > EXACT:
                bad.append("%s: %.10g vs %.10g" % (k, x, y))
        elif json.dumps(x, sort_keys=True, default=str) != json.dumps(y, sort_keys=True, default=str):
            bad.append("%s: %s vs %s" % (k, json.dumps(x, default=str)[:70],
                                         json.dumps(y, default=str)[:70]))
    say("    %s metrics: %s" % (label, "逐键相等" if not bad else "**不等** → " + " | ".join(bad)))
    return not bad


# =============================================================================
# C2: 跨引擎扫描(回测候选臂 vs 生产模拟盘 _exec_price)
# =============================================================================
def scan_paper(conn, nl, code, side, days):
    """模拟盘口径逐日扫描: 首个可成交日与裸价(slip=0 ⇒ 比裸价)。"""
    for d in days:
        px = PE._exec_price(conn, code, d, side, slip=0.0, name_fn=nl.name_at)
        if px is not None:
            return d, float(px)
    return None, None


def check_c2(say, rows, nl) -> dict:
    """对记录的每一次 _exec_side 调用重放模拟盘, 逐笔比对。返回统计 + 是否通过。"""
    conn = db.get_conn()
    stat = dict(n=0, same=0, bucket_no_limit=0, bucket_no_close=0, bad=0)
    samples, bad_samples = [], []
    try:
        for r in rows:
            stat["n"] += 1
            if not r["days"]:
                continue
            day, px = scan_paper(conn, nl, r["code"], r["side"], r["days"])
            ok = (day == r["fill_day"]) and (
                (px is None and r["px"] is None)
                or (px is not None and r["px"] is not None and abs(px - r["px"]) <= PTOL))
            if ok:
                stat["same"] += 1
                continue
            # 归桶: 分歧发生在"回测拒绝/顺延而模拟盘成交"的那一天
            b = None
            if day is not None and r["fill_day"] != day:
                if PE._no_limit_day(conn, r["code"], day):
                    b = "no_limit"
                else:
                    row = conn.execute("SELECT close, open, vol FROM daily WHERE ts_code=? "
                                       "AND trade_date=?", (r["code"], day)).fetchone()
                    if row and row[0] is None and row[1] is not None:
                        b = "no_close"
            if b == "no_limit":
                stat["bucket_no_limit"] += 1
                if len(samples) < 8:
                    samples.append("%s %s %s 模拟盘%d成交@%.2f 回测%s"
                                   % (r["d"], r["code"], r["side"], int(day),
                                      px or 0, r["fill_day"] or "不成交"))
            elif b == "no_close":
                stat["bucket_no_close"] += 1
            else:
                stat["bad"] += 1
                if len(bad_samples) < 12:
                    bad_samples.append(
                        "%s %s %s: 回测 %s@%s vs 模拟盘 %s@%s"
                        % (r["d"], r["code"], r["side"],
                           r["fill_day"] or "不成交",
                           "%.4f" % r["px"] if r["px"] is not None else "-",
                           day or "不成交", "%.4f" % px if px is not None else "-"))
    finally:
        conn.close()
    stat["pass"] = stat["bad"] == 0
    say("    C2 逐笔: %d 笔 | 一致 %d | 桶①no_limit %d | 桶②no_close %d | **桶外 %d**"
        % (stat["n"], stat["same"], stat["bucket_no_limit"], stat["bucket_no_close"],
           stat["bad"]))
    for s in samples:
        say("      桶①: %s" % s)
    for s in bad_samples:
        say("      **桶外**: %s" % s)
    stat["samples"], stat["bad_samples"] = samples, bad_samples
    return stat


# =============================================================================
# C3: 重叠持仓逐笔现金等式
# =============================================================================
def check_c3(say, rows, fees_p, slip) -> dict:
    """对记录的每个重叠笔做 §4-C3 的 5 条检查。"""
    stat = dict(n=0, zero=0, zero_bad=0, neg=0, cash=0, cash_bad=0, side_min_margin=1e9,
                side_differ=0, side_same=0, worst=0.0)
    worst_row = None
    for r in rows:
        stat["n"] += 1
        d = r["sh"] - r["sh_prev"]
        if r["frac"] < 0:
            stat["neg"] += 1
        if d == 0:
            stat["zero"] += 1
            if r["frac"] != 0.0:
                stat["zero_bad"] += 1
            continue
        amt = abs(d) * r["px"]
        side = "buy" if d > 0 else "sell"
        rate = fees_mod.trade_cost(amt, side, r["date"], fees_p) / amt
        lam = (1 + slip) * (1 + rate) - 1
        lhs = r["frac"] * r["sh"] * r["px"]
        rhs = amt * lam
        err = abs(lhs - rhs) / max(1.0, abs(rhs))
        if err > CTOL:
            stat["cash_bad"] += 1
        else:
            stat["cash"] += 1
        if err > stat["worst"]:
            stat["worst"], worst_row = err, (r, side, lam, lhs, rhs)
        # side 鉴别力: 用错 side 会有多大误差
        other = "sell" if side == "buy" else "buy"
        rate_o = fees_mod.trade_cost(amt, other, r["date"], fees_p) / amt
        if abs(rate_o - rate) > 1e-12:
            stat["side_differ"] += 1
            m = abs(lhs - amt * ((1 + slip) * (1 + rate_o) - 1))
            stat["side_min_margin"] = min(stat["side_min_margin"], m)
        else:
            stat["side_same"] += 1
    stat["pass"] = (stat["cash_bad"] == 0 and stat["zero_bad"] == 0
                    and stat["neg"] == 0 and stat["side_differ"] > 0)
    say("    C3 逐笔: %d 笔 | d==0 %d (其中 frac≠0 的 **%d**) | frac<0 **%d** | "
        "现金等式 %d/%d | 换 side 可鉴别的 %d 笔(最小 margin %.3e)"
        % (stat["n"], stat["zero"], stat["zero_bad"], stat["neg"],
           stat["cash"], stat["n"] - stat["zero"], stat["side_differ"],
           stat["side_min_margin"] if stat["side_min_margin"] < 1e9 else -1))
    if worst_row:
        r, side, lam, lhs, rhs = worst_row
        say("      最大误差 %.3e: sh=%g sh_prev=%g (%s d=%+g) @%.2f %s λ=%.6f 计 %.4f 应 %.4f"
            % (stat["worst"], r["sh"], r["sh_prev"],
               r["date"], r["sh"] - r["sh_prev"], r["px"], side, lam, lhs, rhs))
    return stat


def check_c3_month(say, months: list, run_a, run_b, capital, label) -> bool:
    """§4-C3 第 5 条: 逐月恒等式。

    对照物**不是**控制臂的探针(那会跟被测对象同源), 而是控制臂**写进 DB 的净值序列** ——
    `mret = nav[t]/nav[t-1]-1`(引擎里 capital 固定、nav 不回灌分配 ⇒ 两臂的分配路径逐位
    相同, 故 Δmret 只能来自这次改动)。右端 = 探针逐笔 Σ(frac×sh×px)/capital。
    两条账: 每自然月第 1 次调用=满仓线(对 nav)、第 2 次=系统线(对 sys_nav)。"""
    da, db_ = fetch_nav(run_a)[1], fetch_nav(run_b)[1]
    if len(da) != len(db_) or list(da["trade_date"]) != list(db_["trade_date"]):
        say("    %s 逐月恒等式: **两臂月频序列不同构** %d vs %d"
            % (label, len(da), len(db_)))
        return False
    if len(months) != 2 * len(da):
        say("    %s 逐月恒等式: **月探针调用数 %d ≠ 2×期数 %d**" % (label, len(months), 2 * len(da)))
        return False
    bad, worst, wrow = 0, 0.0, None
    for i, row in enumerate(da.itertuples(index=False)):
        for j, col in ((0, "nav"), (1, "sys_nav")):
            m0 = months[2 * i + j]
            if m0["d1"] != str(row.trade_date):
                bad += 1
                continue
            va = [getattr(da.iloc[i], col)] if i == 0 else [da.iloc[i][col], da.iloc[i - 1][col]]
            vb = [db_.iloc[i][col]] if i == 0 else [db_.iloc[i][col], db_.iloc[i - 1][col]]
            ra = (va[0] / 1.0 if i == 0 else va[0] / va[1]) - 1
            rb = (vb[0] / 1.0 if i == 0 else vb[0] / vb[1]) - 1
            lhs = ra - rb
            rhs = m0["cost"] / capital
            e = abs(lhs - rhs) / max(1e-9, abs(rhs))
            if e > CTOL:
                bad += 1
                if e > worst:
                    worst, wrow = e, (row.trade_date, col, lhs, rhs, m0["n"])
            elif e > worst:
                worst = e
    say("    %s 逐月恒等式: %d 月 × 两账 | 不成立 %d | 最大相对差 %.3e%s"
        % (label, len(da), bad, worst,
           "" if not wrow else " → %s %s Δmret=%.6e Σcost/cap=%.6e (%d 笔)"
           % (wrow[0], wrow[1], wrow[2], wrow[3], wrow[4])))
    return bad == 0


def check_pre(say, nl) -> dict:
    """前置校验 (a) 同源 (b) 日历同源。"""
    from app.factors.factor_engine import load_market
    out = {}
    say("  前置(a) 矩阵 vs DB 同源 (open/pre_close 量化后逐格):")
    m = load_market()
    dates = list(m["close"].index)
    conn = db.get_conn()
    try:
        # 抽查: 全市场随机 4000 格 + 每个窗口前 5 个交易日全列
        rng = np.random.default_rng(20260912)
        codes = list(m["close"].columns)
        rows = conn.execute("SELECT COUNT(*) FROM daily WHERE open IS NOT NULL").fetchone()[0]
        bad = dict(open=0, pre=0, vol=0)
        checked = 0
        for _ in range(4000):
            i = int(rng.integers(0, len(dates)))
            c = codes[int(rng.integers(0, len(codes)))]
            dd = dates[i]
            row = conn.execute("SELECT open, pre_close, vol FROM daily WHERE ts_code=? "
                               "AND trade_date=?", (c, dd.strftime("%Y%m%d"))).fetchone()
            if not row:
                continue
            checked += 1
            o, p, v = row
            mo, mp_ = m["open"].loc[dd, c], m["pre_close"].loc[dd, c]
            if o is not None and pd.notna(mo) and round(float(mo), 2) != round(float(o), 2):
                bad["open"] += 1
            if p is not None and pd.notna(mp_) and round(float(mp_), 2) != round(float(p), 2):
                bad["pre"] += 1
            mv = m["vol"].loc[dd, c]
            if (v is None or v <= 0) != (pd.isna(mv) or mv <= 0):
                bad["vol"] += 1
        say("    抽查 %d 格(全库 open 非空 %d 行): 不一致 open=%d pre_close=%d vol=%d"
            % (checked, rows, bad["open"], bad["pre"], bad["vol"]))
        out["a"] = dict(checked=checked, **bad, pass_=sum(bad.values()) == 0)
        say("  前置(b) 日历同源 (矩阵 dates[pos+k] vs trade_cal 第 k 个开市日):")
        cal = [r[0] for r in conn.execute(
            "SELECT cal_date FROM trade_cal WHERE is_open=1 ORDER BY cal_date")]
        idx = {d: i for i, d in enumerate(cal)}
        i0 = idx.get(dates[len(dates) // 2].strftime("%Y%m%d"))
        bad_cal = 0
        for k in range(1, 6):
            a = (dates[len(dates) // 2 + k].strftime("%Y%m%d")
                 if len(dates) // 2 + k < len(dates) else None)
            b = cal[i0 + k] if i0 is not None and i0 + k < len(cal) else None
            if a != b:
                bad_cal += 1
        # 再抽查 50 个随机位置
        for _ in range(50):
            i = int(rng.integers(0, len(dates) - 6))
            i0 = idx.get(dates[i].strftime("%Y%m%d"))
            for k in range(1, 6):
                if i0 + k >= len(cal):
                    continue
                if dates[i + k].strftime("%Y%m%d") != cal[i0 + k]:
                    bad_cal += 1
        say("     抽查 51 个位置 × k=1..5: 不一致 %d" % bad_cal)
        out["b"] = dict(bad=bad_cal, pass_=bad_cal == 0)
    finally:
        conn.close()
    del m
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", default="all",
                    help="pre=只做前置校验; run=只跑臂; check=只做判据; all=全跑")
    args = ap.parse_args()
    setup_logger()
    say = Say(LOG)
    t0 = time.time()
    say("=" * 100)
    say("E6 执行模型 A/B | P0-01 开盘价成交 / P0-02 重叠 Δshares 计费 | 脚本 sha %s"
        % script_sha())
    say("=" * 100)
    say("判据: 见本文件 §4(C1 守卫 / C2 跨引擎逐笔 / C3 逐笔现金等式) —— 跑数前冻结。")
    say("两个开关跑数期间默认 False; 现行 pin %s / %s" % (PIN["prod"], PIN["full"]))
    say("")

    from app.models.names import NameLookup
    nl = NameLookup()

    pre = {}
    if args.stage in ("pre", "all"):
        pre = check_pre(say, nl)
        say("")

    stats, probes = {}, {}
    if args.stage in ("run", "all"):
        for name, tag, o, v, probe in ARMS:
            for window, start in WINDOWS:
                if window == "full" and name not in FULL_ARMS:
                    continue
                st, rows, mp = run_arm(say, tag, window, start, o, v, probe)
                stats[(name, window)] = st
                if probe:
                    probes[(name, window)] = (rows, mp)
        say("")

    if not stats:
        say("(--stage %s: 未跑臂, 判据段跳过)" % args.stage)
        OUT.write_text(json.dumps({"script_sha": script_sha(), "pre": pre,
                                   "note": "stage=%s, 未跑臂" % args.stage},
                                  ensure_ascii=False, indent=1, default=str),
                       encoding="utf-8")
        say.close()
        return

    # ---------- C1 守卫 ----------
    say("-" * 100)
    say("C1 守卫: A0 逐位复现现行 pin")
    c1 = {}
    for window, _ in WINDOWS:
        st = stats.get(("A0", window))
        pin_run = PIN[window]
        ok_m = cmp_metrics(say, st["metrics"], fetch_metrics(pin_run), f"[{window}]")
        say("    [%s] 现值 满仓 %+.2f%% 系统 %+.2f%%/%+.2f%%  日频fp=%s 月频fp=%s"
            % (window, st["ann"] * 100, st["sys_ann"] * 100, st["sys_mdd"] * 100,
               st["daily_fp"], st["monthly_fp"]))
        ok_n = cmp_nav(say, st["run_date"], pin_run, f"[{window}] 对 pin")
        c1[window] = bool(ok_m and ok_n)
        say("    [%s] ⇒ C1 %s" % (window, "PASS" if c1[window] else "**FAIL**"))
    say("")

    # ---------- 插桩自证 ----------
    say("-" * 100)
    say("插桩自证(探针必须纯透传):")
    probe_ok = {}
    for arm_a, arm_w in (("A1", "A1w"), ("A2", "A2w")):
        if ("A0", "prod") not in stats:
            break
        a, w = stats.get((arm_a, "prod")), stats.get((arm_w, "prod"))
        if a is None or w is None:
            say("    %s vs %s: 缺读数(本轮未跑) —— **未自证**" % (arm_a, arm_w))
            probe_ok[arm_a] = False
            continue
        same = (a["metrics"] == w["metrics"] or cmp_metrics(say, w["metrics"], a["metrics"],
                                                            f"{arm_w} vs {arm_a}"))
        same = bool(same) and cmp_nav(say, w["run_date"], a["run_date"], f"{arm_w} vs {arm_a}")
        probe_ok[arm_a] = same
        say("    %s vs %s ⇒ %s" % (arm_w, arm_a, "逐位一致" if same else "**不一致**"))
    say("")

    # ---------- C2 ----------
    say("-" * 100)
    say("C2 P0-01 跨引擎逐笔(P0-01 候选臂 vs 生产模拟盘 _exec_price):")
    c2 = {}
    for window, _ in WINDOWS:
        rows, _mp = probes.get(("A1w", window), (None, None))
        if not rows:
            say("    [%s] 无记录(本轮未跑)" % window)
            c2[window] = None
            continue
        say("    [%s]" % window)
        c2[window] = check_c2(say, rows, nl)
    c2_ok = all(v and v["pass"] for v in c2.values() if v is not None)
    say("")

    # ---------- C3 ----------
    say("-" * 100)
    say("C3 P0-02 重叠持仓逐笔现金等式:")
    fees_p = fees_mod.fee_params()
    slip = float((cfg_mod.load_config().get("paper", {}) or {}).get("slip", 0.001))
    say("  费率参数 %s | slip=%.4f" % (json.dumps(fees_p, ensure_ascii=False), slip))
    c3 = {}
    c3m_ok = True
    for window, _ in WINDOWS:
        rows, mp = probes.get(("A2w", window), (None, None))
        if not rows:
            say("    [%s] 无记录(本轮未跑)" % window)
            c3[window] = None
            continue
        say("    [%s]" % window)
        c3[window] = check_c3(say, rows, fees_p, slip)
        a0 = stats.get(("A0", window))
        if a0 is None:
            say("    [%s] 逐月恒等式: **缺 A0 读数** —— 未验证" % window)
            c3m_ok = False
        else:
            c3m_ok &= check_c3_month(say, mp.months, a0["run_date"], stats[("A2w", window)]["run_date"],
                                     float(a0["metrics"]["capital"]), f"[{window}] A0 vs A2w")
    c3_ok = all(v and v["pass"] for v in c3.values() if v is not None) and c3m_ok
    say("")

    # ---------- 判决 ----------
    say("=" * 100)
    say("判决:")
    say("  C1 守卫      : %s" % ("PASS" if all(c1.values()) else "**FAIL ⇒ 整轮作废**"))
    say("  插桩自证     : %s" % ("PASS" if all(probe_ok.values()) else "**FAIL**"))
    say("  C2 P0-01     : %s" % ("PASS" if c2_ok else "**FAIL**"))
    say("  C3 P0-02     : %s" % ("PASS" if c3_ok else "**FAIL**"))
    verdict = dict(c1=c1, probe=probe_ok,
                   c2={k: (v and {kk: vv for kk, vv in v.items() if kk != "bad_samples"})
                       for k, v in c2.items()},
                   c3=c3, c3_month=c3m_ok)
    say("")
    say("读数(不作判据):")
    for name, tag, o, v, probe in ARMS:
        for window, _ in WINDOWS:
            st = stats.get((name, window))
            if not st:
                continue
            say("  %-6s %-4s 满仓 %+7.2f%%/%+7.2f%%  系统 %+7.2f%%/%+7.2f%%  日频回撤 %+7.2f%%"
                % (tag, window, st["ann"] * 100, st["mdd"] * 100,
                   st["sys_ann"] * 100, st["sys_mdd"] * 100, st["mdd_daily"] * 100))
    say("总耗时 %.0fs" % (time.time() - t0))
    say("run 保留(证据, 不得删除): %s"
        % ", ".join(sorted(st["run_date"] for st in stats.values())))

    OUT.write_text(json.dumps(
        {"script_sha": script_sha(), "pre": pre,
         "arms": {f"{k[0]}|{k[1]}": v for k, v in stats.items()},
         "probe_rows": {f"{k[0]}|{k[1]}": len(v[0] or []) for k, v in probes.items()},
         "verdict": verdict}, ensure_ascii=False, indent=1, default=str),
        encoding="utf-8")
    say("结果 JSON: %s" % OUT)
    say.close()


if __name__ == "__main__":
    main()
