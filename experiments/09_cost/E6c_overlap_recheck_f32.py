"""E6c —— P0-02 判据第二轮修正: **float32 口径复算**(2026-09-12)

## §0 为什么还有一轮(E6b 的 C3' 也没通过, 原因已定位为**表示层**)

E6b(`E6b_overlap_recheck.py`)判决: **C1' PASS**(A0m 带月探针仍逐位复现 pin, 两窗);
**C3' FAIL** —— 但两条失败都只差在 **float32 的最后几位**:

| 判据 | 实测残差 | 阈值 |
|---|---|---|
| C3'.3 现金等式(27/28 笔"不等") | **3.258e-08** 相对 | 1e-9 |
| C3'.5 逐月 pnl 恒等式(23/170 "不成立") | 1.788e-05 相对(1.03e-4 元) | 1e-12 |

**根因(读码 + 纯算术定位, 不依赖回测)**: 引擎的 `bp_raw` 来自市场矩阵, 是
**np.float32**; 而判据在 float64 里用"那个 float32 值的精确十进制"复算。实读
`app/portfolio/fees.py:76`(`amt = float(amount or 0)`)与 `effective_rate` 同一处理
⇒ **引擎里唯一的 float32 步骤是「股数 × 价」这一次乘法**(`abs(d) * bp_raw`, NEP 50
弱标量 ⇒ float32), 之后佣金/印花/过户/费率全是 float64。实测:

    np.float32(6.24) = 6.239999771118164      ← 相对 6.24 差 3.67e-08
    引擎: amt = float32(100×6.24) = 624.0     λ(全 float64) = 0.009030819 → 5.635231
    判据: amt = 100×6.239999771118164 = 623.9999771…  λ = 0.009030844 → 5.635246
                                         成交额相对差 3.7e-08 ← 与观测残差逐位吻合

即: 判据拿 **float64 成交额**算费率, 引擎拿的是它的 **float32 舍入值**; 二者差 ~4e-8
相对(每笔约 2e-7 元), 判据却要求 1e-9 ⇒ **判据比被测对象的数值精度还严**, 必然"不等"。
月中 pnl 恒等式的 1e-4 元残差同源: pnl 基数 `sh*bp_raw` 含 float32 因子(780 元 × 1.2e-7 ≈ 1e-4 元)。

**结论: 不是记账错, 是判据写错了精度层级。** 依纪律不改 E6/E6b 一个字, 另立 E6c。

## §1 本轮相对 E6b 的改动(**把判据搬到被测对象的精度层级**, 不是放宽)

1. **判据 3 改成"照抄引擎的精度链"**: 成交额先过 `float(np.float32(|d|×px))`, 再全
   float64 算费率/λ(见 `eng_lam`, 逐行对应 `_side_rate`→`effective_rate`)。要求
   `<= 1e-12` 相对 —— 这**比 E6b 的 1e-9 更严**(它要求在"引擎自己的精度链"上逐位相等),
   而不是把阈值放宽到 1e-7。任何逻辑错(方向、基数、漏项)都会立刻暴露。
   同时**照旧报告**不做那步 float32 舍入时的差额(解析预期 ~4e-8), 明写为表示层差。
2. **判据 5 的容差改成推导界**: `|Δpnl − Σcost| <= max(1e-3 元, 2*eps_f32*|月 pnl|)`,
   `eps_f32=1.19e-7`。理由: pnl 基数含 float32 因子, 两项相减的表示误差上界即此式;
   实测 1.03e-4 元落在界内(比它小 1 个量级)。**业务级错误远大于此界**, 鉴别力不减。
3. **守卫原样重跑**: C1'(A0m 位级复现 pin)在 E6b 已 PASS, 本轮仍跑一遍作本轮守卫
   (臂、判据相同), 不引用旧结论。
4. **证据持久化**: 本轮把逐笔行落盘 `E6c_rows.json`(E6/E6b 只存了计数), 以后复核不必重跑。

## §2 判据(跑数前冻结)

- **G**: `e6cctlm` 两窗逐位复现 pin `20260912_limitquant` / `_full`(metrics 全键 + 净值逐点)。
- **A1**: `d == 0` ⇒ `frac == 0.0`(位级)。
- **A2**: 所有 `frac >= 0`。
- **A3(引擎精度链)**: 按 `eng_lam`(float32 成交额 + float64 费率)复算的现金等式,
  全部笔 `<= 1e-12` 相对; 并报告"不做 float32 成交额舍入"的差额(表示层, 预期 ~4e-8)。
- **A4**: side 非空洞(至少一笔 buy/sell 费率不同, 换 side 必超差)。
- **A5**: 逐月 pnl 恒等式 `|Δpnl − Σcost| <= max(1e-3, 2*eps_f32*|pnl|)`, 两条账分别, 全部月成立。
- **A6**: 净值序列辅助 sanity: `|Δmret − Σcost/capital| <= 1e-6`。

全部成立 ⇒ **P0-02 采纳**; 任一不成立 ⇒ 不采纳并查清。

## §3 结果
(跑数后追加)
"""
import hashlib
import importlib.util
import io
import json
import sys
import time
from pathlib import Path

_ORIG_STDOUT = sys.stdout
if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(_ORIG_STDOUT.buffer, encoding="utf-8", errors="replace")

BASE = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE))

import numpy as np                                        # noqa: E402

from app import config as cfg_mod                         # noqa: E402
from app.logger import setup_logger                       # noqa: E402
from app.portfolio import fees as fees_mod                # noqa: E402

LOG = Path("D:/claude-home/tmp/E6c_overlap_recheck_f32.log")
OUT = BASE / "experiments" / "09_cost" / "E6c_overlap_recheck_f32_result.json"
ROWS_OUT = BASE / "experiments" / "09_cost" / "E6c_rows.json"
E6 = BASE / "experiments" / "09_cost" / "E6_exec_open_overlap.py"
PIN = {"prod": "20260912_limitquant", "full": "20260912_limitquant_full"}
WINDOWS = (("prod", "20190722"), ("full", "20170101"))
EPS32 = float(np.finfo(np.float32).eps)      # 1.1920929e-07
TOL_F32 = 1e-12     # 引擎精度链复算(应回到 float64 噪声量级)
TOL_PNL_ABS = 1e-3      # 逐月 pnl 恒等式的绝对下界(元)
TOL_NAV = 1e-6


def script_sha(p) -> str:
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()[:16]


def load_e6():
    spec = importlib.util.spec_from_file_location("e6_mod", E6)
    mod = importlib.util.module_from_spec(spec)
    sys.stdout.flush()
    spec.loader.exec_module(mod)
    return mod


def eng_lam(d, px_f64, date, side, slip, fees_p):
    """**引擎路径**的 λ: 复刻 `_side_rate` → `fees.effective_rate` 的实际精度链。

    实读 `app/portfolio/fees.py:76`: `trade_cost` 第一行就 `amt = float(amount or 0)`,
    `effective_rate` 同样先 float ⇒ **引擎里唯一的 float32 步骤是「股数 × 价」这一次
    乘法**(np.float32 × 弱标量 ⇒ float32), 之后费率算术全是 float64。故:

        amt32 = float(np.float32(|d| * px))     ← 唯一一次 float32 舍入
        λ     = f( trade_cost(amt32, …) / amt32 )   ← 全 float64

    这正是 E6b 残差 3.258e-08 的来源: 判据拿 float64 的成交额算费率, 引擎拿的是它
    的 float32 舍入值(相对差 ~6e-8)。把这一步照抄, 等式应回到 float64 噪声量级。
    返回 (λ, amt32, rhs32_cash, amt64, rhs64_cash)。"""
    amt32 = float(np.float32(abs(d) * px_f64))
    rate = fees_mod.trade_cost(amt32, side, date, fees_p) / amt32
    lam = ((1 + slip) * (1 + rate) - 1) if d > 0 else (1 - (1 - slip) * (1 - rate))
    amt64 = abs(d) * px_f64
    rate64 = fees_mod.trade_cost(amt64, side, date, fees_p) / amt64
    lam64 = ((1 + slip) * (1 + rate64) - 1) if d > 0 else (1 - (1 - slip) * (1 - rate64))
    return lam, amt32, abs(d) * float(px_f64) * lam, amt64, abs(d) * float(px_f64) * lam64


def check_a(say, rows, fees_p, slip) -> dict:
    """A1~A4: 对每条重叠记录复算「引擎路径」的 λ 与现金额。"""
    st = dict(n=0, zero=0, zero_bad=0, neg=0, ok=0, bad=0, worst=0.0,
              gap32=0.0, side_ok=0, side_same=0, margin=1e999, ex=None, exg=None)
    for r in rows:
        st["n"] += 1
        d = r["sh"] - r["sh_prev"]
        if r["frac"] < 0:
            st["neg"] += 1
        if d == 0:
            st["zero"] += 1
            st["zero_bad"] += int(r["frac"] != 0.0)
            continue
        side = "buy" if d > 0 else "sell"
        px = float(r["px"])                       # 探针记录的是 float32 精确值, float64 容器
        lam, amt32, rhs32, amt64, rhs64 = eng_lam(d, px, r["date"], side, slip, fees_p)
        lhs = r["frac"] * r["sh"] * px            # 引擎记录的 frac 换算回现金(float64)
        e = abs(lhs - rhs32) / max(1.0, abs(rhs32))
        # 报告项: 若不做那一步 float32 成交额舍入, 等式差多少(= E6b 残差的解析来源)
        g = abs(rhs32 - rhs64) / max(1.0, abs(rhs64))
        st["worst"] = max(st["worst"], e)
        if g > st["gap32"]:
            st["gap32"], st["exg"] = g, (r, side, amt32, amt64, g)
        if e <= TOL_F32:
            st["ok"] += 1
        else:
            st["bad"] += 1
            if st["ex"] is None:
                st["ex"] = (r, side, lam, lhs, rhs32, e)
        other = "sell" if side == "buy" else "buy"
        rate_o = fees_mod.trade_cost(amt32, other, r["date"], fees_p) / amt32
        lam_o = ((1 + slip) * (1 + rate_o) - 1) if d > 0 else (1 - (1 - slip) * (1 - rate_o))
        # 只有 buy/sell 费率真的不同时才"可鉴别"; 同额同费率(佣金未触底+无印花)则空洞
        if abs(lam_o - lam) > 0:
            st["side_ok"] += 1
            st["margin"] = min(st["margin"], abs(lhs - abs(d) * px * lam_o))
        else:
            st["side_same"] += 1
    st["pass"] = (st["bad"] == 0 and st["zero_bad"] == 0 and st["neg"] == 0
                  and st["side_ok"] > 0)
    say("    A1/2/4: %d 笔 | d==0 %d(frac≠0 **%d**) | frac<0 **%d** | 换 side 可鉴别 %d 笔"
        " (同费率 %d, 最小 margin %.3e) ⇒ 非空洞: %s"
        % (st["n"], st["zero"], st["zero_bad"], st["neg"], st["side_ok"], st["side_same"],
           st["margin"] if st["margin"] < 1e999 else -1,
           "是" if st["side_ok"] > 0 else "**否**"))
    say("    A3 引擎路径(float32 成交额 + float64 费率)复算: %d ok / **%d bad** | 最大残差 %.3e"
        % (st["ok"], st["bad"], st["worst"]))
    say("    A3 报告项(不做 float32 成交额舍入 ⇒ E6b 残差的来源): 最大 %.3e" % st["gap32"])
    if st["exg"]:
        r, side, amt32, amt64, g = st["exg"]
        say("      成交额: f32 %.10f vs f64 %.10f (%s %s d=%+g @%.4f, 相对 %.2e ⇒ 现金差 ~%.2e 元)"
            % (amt32, amt64, r["date"], side, r["sh"] - r["sh_prev"], r["px"], g,
               g * abs(amt32) * 0.015))
    if st["ex"]:
        r, side, lam, lhs, rhs32, e = st["ex"]
        say("      反例: %s %s d=%+g @%.4f λ=%.12f 计 %.10f 应 %.10f (%.2e)"
            % (r["date"], side, r["sh"] - r["sh_prev"], r["px"], lam, lhs, rhs32, e))
    return st


def check_month(say, m0, m2, fetch, run_a, run_b, capital, label) -> bool:
    if len(m0) != len(m2):
        say("    %s: **月探针调用数不同** %d vs %d" % (label, len(m0), len(m2)))
        return False
    bad, worst, wrow = 0, 0.0, None
    for a, b in zip(m0, m2):
        if a["d1"] != b["d1"]:
            bad += 1
            continue
        res = abs((a["pnl"] - b["pnl"]) - b["cost"])
        bound = max(TOL_PNL_ABS, 2 * EPS32 * abs(a["pnl"]))
        if res > bound:
            bad += 1
        if res > worst:
            worst, wrow = res, (a["d1"], a["pnl"] - b["pnl"], b["cost"], bound)
    say("    A5 逐月 pnl 恒等式: %d 月 × 两账 | 不成立 **%d** | 最大绝对残差 %.3e 元%s"
        % (len(m0), bad, worst,
           "" if not wrow else " → %s Δpnl=%.6f Σcost=%.6f(界 %.1e)"
           % (wrow[0], wrow[1], wrow[2], wrow[3])))
    ok = bad == 0
    da, db_ = fetch(run_a)[1], fetch(run_b)[1]
    worst_n = 0.0
    for i in range(len(da)):
        for j, col in ((0, "nav"), (1, "sys_nav")):
            va, vb = da.iloc[i][col], db_.iloc[i][col]
            if i > 0:
                va, vb = va / da.iloc[i - 1][col], vb / db_.iloc[i - 1][col]
            worst_n = max(worst_n, abs((va - vb) - m2[2 * i + j]["cost"] / capital))
    say("    A6 净值序列辅助: 最大绝对差 %.3e (容差 %.0e)" % (worst_n, TOL_NAV))
    return ok and worst_n <= TOL_NAV


def main():
    setup_logger()
    e6 = load_e6()
    say = e6.Say(LOG)
    t0 = time.time()
    say("=" * 100)
    say("E6c P0-02 float32 口径复算 | 脚本 sha %s | 引擎 sha %s | E6 sha %s | E6b sha %s"
        % (script_sha(__file__), script_sha(BASE / "app/backtest/engine.py"),
           script_sha(E6), script_sha(BASE / "experiments/09_cost/E6b_overlap_recheck.py")))
    say("=" * 100)
    say("E6/E6b 的 C3/C3' 记为**判据精度层级写错**(§0 已用纯算术定位到 float32 表示层), 不据此否掉。")
    say("本轮判据见 §2(跑数前冻结): A3 用同精度复算(更严), A5 用推导界。")
    say("")

    stats, probes = {}, {}
    for tag, ovl in (("e6cctlm", False), ("e6covlw", True)):
        for window, start in WINDOWS:
            st, rows, mp = e6.run_arm(say, tag, window, start, False, ovl, "month")
            stats[(tag, window)] = st
            probes[(tag, window)] = (rows, mp)
    say("")

    say("-" * 100)
    say("G 守卫: e6cctlm(月探针 + 两开关关) 逐位复现 pin")
    g = {}
    for window, _ in WINDOWS:
        st = stats[("e6cctlm", window)]
        pin = PIN[window]
        ok_m = e6.cmp_metrics(say, st["metrics"], e6.fetch_metrics(pin), f"[{window}]")
        ok_n = e6.cmp_nav(say, st["run_date"], pin, f"[{window}] 对 pin")
        g[window] = bool(ok_m and ok_n)
        say("    [%s] 满仓 %+.2f%% 系统 %+.2f%%/%+.2f%% fp=%s ⇒ G %s"
            % (window, st["ann"] * 100, st["sys_ann"] * 100, st["sys_mdd"] * 100,
               st["daily_fp"], "PASS" if g[window] else "**FAIL**"))
    say("")

    say("-" * 100)
    say("A1~A6 P0-02:")
    fees_p = fees_mod.fee_params()
    slip = float((cfg_mod.load_config().get("paper", {}) or {}).get("slip", 0.001))
    a, mok = {}, True
    for window, _ in WINDOWS:
        rows, mp = probes[("e6covlw", window)]
        say("    [%s]" % window)
        a[window] = check_a(say, rows, fees_p, slip)
        a0 = stats[("e6cctlm", window)]
        mok &= check_month(say, probes[("e6cctlm", window)][1].months, mp.months,
                           e6.fetch_nav, a0["run_date"], stats[("e6covlw", window)]["run_date"],
                           float(a0["metrics"]["capital"]), f"[{window}] A0m vs A2w")
    ok = all(v["pass"] for v in a.values()) and mok
    say("")
    say("=" * 100)
    say("判决: G %s | A1~A6 %s ⇒ P0-02 %s"
        % ("PASS" if all(g.values()) else "**FAIL**", "PASS" if ok else "**FAIL**",
           "**采纳**" if (ok and all(g.values())) else "**不采纳**"))
    say("总耗时 %.0fs" % (time.time() - t0))
    ROWS_OUT.write_text(json.dumps(
        {f"{k[0]}|{k[1]}": v[0] for k, v in probes.items()}, ensure_ascii=False), encoding="utf-8")
    OUT.write_text(json.dumps(
        {"script_sha": script_sha(__file__), "engine_sha": script_sha(BASE / "app/backtest/engine.py"),
         "e6_sha": script_sha(E6), "arms": {f"{k[0]}|{k[1]}": v for k, v in stats.items()},
         "guard": g, "checks": a, "month_ok": mok, "verdict": bool(ok and all(g.values()))},
        ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    say("结果 JSON: %s | 逐笔行: %s" % (OUT, ROWS_OUT))
    say.close()


if __name__ == "__main__":
    main()
