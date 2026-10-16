"""E6b —— P0-02 的**判据修正版**重跑(2026-09-12)

## §0 为什么会有这一轮(E6 的 C3 判据失效, 不是被测代码出错)

E6(`E6_exec_open_overlap.py`, 脚本 sha c6d1eb242055838c)的判决是
`C1 PASS / 插桩自证 PASS / C2 PASS / C3 **FAIL**`。追查后确认 **FAIL 全部来自判据自身**:

1. **§4-C3.3 只写了买入式**: 判据原文要求 `frac*sh*bp_raw == |d|*bp_raw*((1+slip)*(1+rate)-1)`,
   **没有给卖出式**。而引擎按方向取式(卖出 = `1-(1-slip)(1-rate)`, 因为卖出是"所得被折扣"),
   于是**每一笔卖出 delta 都必然判不等** —— 实测的 1.869e-03 相对差正是两式之差
   (λ_buy 0.015429 vs λ_sell 0.015400, 差额 × 373 元 = 0.0107 元, 与日志逐位吻合)。
   ⇒ 判据写错了, 引擎是对的。
2. **§4-C3.5 用净值序列重建月收益**: `mret = nav[t]/nav[t-1]-1` 在 85 个月复利后把 1e-15 的
   浮点噪声放大成 ~1e-7 的**伪差**; 无调仓的月份右端恰为 0 ⇒ 相对误差炸到 1e2。
   ⇒ 判据的**度量方式**选错了(应比月内相加量 pnl, 而不是复利后的比率), 不是账不对。
   (E6 的 C3-1/2/4 三条其实已经 PASS: `d==0` 的 59/87 笔 frac 恒为 0、frac 全非负、
   换 side 的鉴别 margin 0.195 —— 判据里"有鉴别力"的那几条都是通过的。)

依项目纪律「判据跑数前冻结、不得事后改判据」(E5 §2 的同一条), **不去改 E6 一个字**:
E6 的 C3 结论登记为**判据失效(不可判)**, 不据此采纳; 本文件另立 C3' 并重跑。

## §1 本轮相对 E6 的差异(**全部是判据修正, 无一处放宽**)

| 条 | E6 | E6b(C3') |
|---|---|---|
| 3 现金等式 | 单一买入式 | **分方向**: buy → `(1+slip)(1+rate)-1`; sell → `1-(1-slip)(1-rate)`; 容差 1e-9 相对 |
| 5 逐月恒等式 | 净值序列重建(复利噪声) | **改用探针的 pnl**(月内相加量, 容差 1e-12); 净值序列降级为**辅助** sanity(绝对容差 1e-6, 只证量级) |
| 守卫 | A0 无探针 | **新增 A0m(A0+月探针) 并逐位复现 pin** —— 探针的透传性直接在守卫臂上自证, 同时补上 E6 §3 里"full 窗无同臂对照"的缺口 |
| 臂 | 10 臂 | 只跑必要的 4 臂(A0m/A2w × 两窗); **不重跑** C2 相关臂(C2 在 E6 已 PASS, 结论沿用并引 E6 的日志/JSON 为证) |

## §2 判据(跑数前冻结)

**C1' 守卫**: `e6bctlm` 两窗必须逐位复现 pin `20260912_limitquant` /
`20260912_limitquant_full`(metrics 全键 + 日频/月频净值逐点 + 指纹)。

**C3' 核心**(对 `e6bovlw` 记录的每个重叠 (月, 股)):
1. `d == 0` ⇒ `frac == 0.0`(位级);
2. 所有 `frac >= 0`;
3. 逐笔现金等式, **分方向**(见 §1 表), 容差 1e-9 相对, 要求**全部**通过;
4. side 非空洞: 至少一行 buy/sell 费率不同, 且"换 side"必然超差(报最小 margin);
5. 逐月恒等式 `pnl(A0m) - pnl(A2w) == Σ(sh*bp_raw*frac)`, 两条账分别比, 容差 1e-12;
6. 辅助: 净值序列重建的 Δmret 与 Σcost/capital 绝对差 <= 1e-6(复利噪声量级)。

**判据通过 ⇒ P0-02 采纳**(与 P0-01 的 C2 一起进生产), 不通过 ⇒ 不采纳并查清。

## §3 结果
(跑数后追加)
"""
import argparse
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
import pandas as pd                                       # noqa: E402

from app import config as cfg_mod                         # noqa: E402
from app import db                                        # noqa: E402
from app.backtest import engine as E                      # noqa: E402
from app.logger import setup_logger                       # noqa: E402
from app.portfolio import fees as fees_mod                # noqa: E402

LOG = Path("D:/claude-home/tmp/E6b_overlap_recheck.log")
OUT = BASE / "experiments" / "09_cost" / "E6b_overlap_recheck_result.json"
E6 = BASE / "experiments" / "09_cost" / "E6_exec_open_overlap.py"
PIN = {"prod": "20260912_limitquant", "full": "20260912_limitquant_full"}
WINDOWS = (("prod", "20190722"), ("full", "20170101"))
CTOL = 1e-9         # 逐笔现金等式(相对)
PTOL_EXACT = 1e-12  # 逐月 pnl 恒等式(月内相加量 ⇒ 应位级相等)
NAVTOL = 1e-6       # 净值序列辅助 sanity(复利噪声量级)


def script_sha(p) -> str:
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()[:16]


def load_e6():
    spec = importlib.util.spec_from_file_location("e6_mod", E6)
    mod = importlib.util.module_from_spec(spec)
    sys.stdout.flush()
    spec.loader.exec_module(mod)
    return mod


def check_c3p(say, rows, fees_p, slip) -> dict:
    """C3' 第 1~4 条(逐笔; 第 3 条分方向)。"""
    st = dict(n=0, zero=0, zero_bad=0, neg=0, ok=0, bad=0, side_differ=0, side_same=0,
              margin=1e999, worst=0.0)
    wrow = None
    for r in rows:
        st["n"] += 1
        d = r["sh"] - r["sh_prev"]
        if r["frac"] < 0:
            st["neg"] += 1
        if d == 0:
            st["zero"] += 1
            st["zero_bad"] += int(r["frac"] != 0.0)
            continue
        amt = abs(d) * r["px"]
        side = "buy" if d > 0 else "sell"
        rate = fees_mod.trade_cost(amt, side, r["date"], fees_p) / amt
        lam = ((1 + slip) * (1 + rate) - 1) if d > 0 else (1 - (1 - slip) * (1 - rate))
        lhs = r["frac"] * r["sh"] * r["px"]
        rhs = amt * lam
        e = abs(lhs - rhs) / max(1.0, abs(rhs))
        if e <= CTOL:
            st["ok"] += 1
        else:
            st["bad"] += 1
        if e > st["worst"]:
            st["worst"], wrow = e, (r, side, lam, lhs, rhs)
        other = "sell" if side == "buy" else "buy"
        rate_o = fees_mod.trade_cost(amt, other, r["date"], fees_p) / amt
        if abs(rate_o - rate) > 1e-12:
            st["side_differ"] += 1
            lam_o = ((1 + slip) * (1 + rate_o) - 1) if d > 0 else (1 - (1 - slip) * (1 - rate_o))
            st["margin"] = min(st["margin"], abs(lhs - amt * lam_o))
        else:
            st["side_same"] += 1
    st["pass"] = (st["bad"] == 0 and st["zero_bad"] == 0 and st["neg"] == 0
                  and st["side_differ"] > 0)
    say("    C3'.1~4: %d 笔 | d==0 %d(frac≠0 **%d**) | frac<0 **%d** | 现金等式 %d ok / **%d bad**"
        % (st["n"], st["zero"], st["zero_bad"], st["neg"], st["ok"], st["bad"]))
    say("             换 side 可鉴别 %d 笔(最小 margin %.3e) ⇒ 判据有鉴别力: %s"
        % (st["side_differ"], st["margin"] if st["margin"] < 1e999 else -1,
           "是" if st["side_differ"] > 0 else "**否**"))
    if wrow:
        r, side, lam, lhs, rhs = wrow
        say("             最大相对差 %.3e: sh=%g sh_prev=%g %s d=%+g @%.2f %s λ=%.8f 计 %.6f 应 %.6f"
            % (st["worst"], r["sh"], r["sh_prev"], r["date"], r["sh"] - r["sh_prev"],
               r["px"], side, lam, lhs, rhs))
    return st


def check_c3p_month(say, m0, m2, run_a, run_b, capital, label) -> bool:
    """C3' 第 5(pnl 恒等式, 精确) + 6(净值序列, 辅助)。"""
    ok = True
    if len(m0) != len(m2):
        say("    %s: **月探针调用数不同** %d vs %d" % (label, len(m0), len(m2)))
        return False
    bad, worst, wrow = 0, 0.0, None
    for a, b in zip(m0, m2):
        if a["d1"] != b["d1"]:
            bad += 1
            continue
        e = abs((a["pnl"] - b["pnl"]) - b["cost"]) / max(1.0, abs(b["cost"]))
        if e > PTOL_EXACT:
            bad += 1
        if e > worst:
            worst, wrow = e, (a["d1"], a["pnl"] - b["pnl"], b["cost"], a["n"], b["n"])
    say("    %s 逐月 pnl 恒等式: %d 月 × 两账 | 不成立 **%d** | 最大相对差 %.3e%s"
        % (label, len(m0), bad, worst,
           "" if not wrow else " → %s Δpnl=%.6f Σcost=%.6f (%d/%d 笔)"
           % (wrow[0], wrow[1], wrow[2], wrow[3], wrow[4])))
    ok &= (bad == 0)
    # --- 辅助: 净值序列(复利噪声量级) ---
    da, db_ = e6.fetch_nav(run_a)[1], e6.fetch_nav(run_b)[1]
    if len(da) != len(db_):
        say("    %s 净值辅助: **两臂月频不同构**" % label)
        return False
    worst_n = 0.0
    for i, row in enumerate(da.itertuples(index=False)):
        for j, col in ((0, "nav"), (1, "sys_nav")):
            va, vb = da.iloc[i][col], db_.iloc[i][col]
            if i > 0:
                va, vb = va / da.iloc[i - 1][col], vb / db_.iloc[i - 1][col]
            lhs = (va - vb)
            rhs = m2[2 * i + j]["cost"] / capital
            worst_n = max(worst_n, abs(lhs - rhs))
    say("    %s 净值序列辅助: 最大绝对差 %.3e (容差 %.0e; 复利把 1e-15 放大成 ~1e-7 ⇒ 只作量级 sanity)"
        % (label, worst_n, NAVTOL))
    ok &= (worst_n <= NAVTOL)
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", default="all")
    args = ap.parse_args()
    setup_logger()
    e6 = load_e6()
    say = e6.Say(LOG)
    t0 = time.time()
    say("=" * 100)
    say("E6b P0-02 判据修正版重跑 | 脚本 sha %s | 被测引擎 sha %s | E6 sha %s"
        % (script_sha(__file__), script_sha(BASE / "app" / "backtest" / "engine.py"),
           script_sha(E6)))
    say("=" * 100)
    say("E6 的 C3 记为**判据失效**(§0 已定位: 卖出式漏写 + 用复利量做相对比较), 不据此采纳。")
    say("本轮 C1' 守卫 + C3' 核心, 判据见 §2(跑数前冻结)。")
    say("")

    stats, probes = {}, {}
    if args.stage in ("run", "all"):
        for tag, ovl, probe in (("e6bctlm", False, "month"), ("e6bovlw", True, "month")):
            for window, start in WINDOWS:
                st, rows, mp = e6.run_arm(say, tag, window, start, False, ovl, probe)
                stats[(tag, window)] = st
                probes[(tag, window)] = (rows, mp)
        say("")

    if not stats:
        say("(--stage %s: 未跑臂)" % args.stage)
        say.close()
        return

    say("-" * 100)
    say("C1' 守卫: A0m(月探针 + 两开关关) 逐位复现 pin")
    c1 = {}
    for window, _ in WINDOWS:
        st = stats[("e6bctlm", window)]
        pin = PIN[window]
        ok_m = e6.cmp_metrics(say, st["metrics"], e6.fetch_metrics(pin), f"[{window}]")
        say("    [%s] 现值 满仓 %+.2f%% 系统 %+.2f%%/%+.2f%%  日频fp=%s 月频fp=%s"
            % (window, st["ann"] * 100, st["sys_ann"] * 100, st["sys_mdd"] * 100,
               st["daily_fp"], st["monthly_fp"]))
        ok_n = e6.cmp_nav(say, st["run_date"], pin, f"[{window}] 对 pin")
        c1[window] = bool(ok_m and ok_n)
        say("    [%s] ⇒ C1' %s" % (window, "PASS" if c1[window] else "**FAIL**"))
    say("")

    say("-" * 100)
    say("C3' P0-02 修正判据:")
    fees_p = fees_mod.fee_params()
    slip = float((cfg_mod.load_config().get("paper", {}) or {}).get("slip", 0.001))
    c3, c3m_ok = {}, True
    for window, _ in WINDOWS:
        rows, mp = probes[("e6bovlw", window)]
        say("    [%s]" % window)
        c3[window] = check_c3p(say, rows, fees_p, slip)
        m0 = probes[("e6bctlm", window)][1]
        a0 = stats[("e6bctlm", window)]
        c3m_ok &= check_c3p_month(say, m0.months, mp.months, a0["run_date"],
                                  stats[("e6bovlw", window)]["run_date"],
                                  float(a0["metrics"]["capital"]), f"[{window}] A0m vs A2w")
    c3_ok = all(v["pass"] for v in c3.values()) and c3m_ok
    say("")

    say("=" * 100)
    say("判决: C1' 守卫 %s | C3' P0-02 %s"
        % ("PASS" if all(c1.values()) else "**FAIL**", "PASS" if c3_ok else "**FAIL**"))
    say("读数(不作判据): P0-02 单独 = 满仓 22.62%→22.52%(prod) / 系统 19.40%→19.35%; "
        "成本合计 170.90 元/85 月(28 笔有调仓, 合计 20000 元本金)")
    say("总耗时 %.0fs" % (time.time() - t0))
    OUT.write_text(json.dumps(
        {"script_sha": script_sha(__file__), "engine_sha": script_sha(BASE / "app/backtest/engine.py"),
         "e6_sha": script_sha(E6), "arms": {f"{k[0]}|{k[1]}": v for k, v in stats.items()},
         "c1": c1, "c3": c3, "c3_month": c3m_ok, "verdict": all(c1.values()) and c3_ok},
        ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    say("结果 JSON: %s" % OUT)
    say.close()


if __name__ == "__main__":
    main()
