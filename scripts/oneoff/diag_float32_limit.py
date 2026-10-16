"""D06/D07 诊断的延伸 —— **float32 的 `pre_close` 让多少格的推导限价偏了一分**。

2026-09-12。背景见 `D:/claude-home/tmp/diag_d67_truth_diff.log`:
真值臂(臂 C)比修复后臂(臂 B)低 1.07pp/年(系统线), 逐笔差异**只有 1 只股票、5 格** ——
`002638.SZ *ST勤上` 2020-07-01~07 连续 5 个一字跌停: 引擎拿自己算出的 dn=1.99 去比
close=2.00, 不等式不成立 ⇒ 判"卖得出去" ⇒ 成交; 而交易所公布的 dn 是 **2.00**, 真值口径
下那 5 天是封死的。

根因: `engine.load_market()` 的市场矩阵是 **float32**(实测 `m['pre_close'].dtype =
float32`), 而 `round2` 的 `1e-9` epsilon 是按 float64 标定的。
`np.float32(2.10) = 2.0999999046`, ×0.95 = 1.99499990 < 1.995 ⇒ +0.5 后 199.99999 < 200
⇒ floor 到 199 ⇒ **限价低一分**。epsilon 在缩放单位下只有 1e-7 分, 而 float32 误差是
5e-5 分 —— 小 500 倍。

**L1 结构上看不见它**: `l1_l2` 是用 `pd.read_sql` 从 SQLite 读的 REAL(→float64)复算的,
引擎运行时走的是 float32 矩阵。**两条不是同一条路径** —— 所以 L1 报 99.983% 吻合,
真值臂却仍然差 1.07pp。本脚本量出「L1 说对、引擎算错」的那部分有多大。

⚠️ 口径(明写, 与 L1 的差别):
  - 档位 `r` **不由 `limit_ratio` 求**, 而是从公布限价反解 `r = lu/pc − 1` 后吸附到
    {0.05, 0.10, 0.20, 0.30}。**float32 效应只取决于 `pc×(1+r)` 这个值**, 不取决于 r 的
    来源 ⇒ 这样得到的是同一批 (pc, r) 对, 且完全向量化(10M 行逐行调 NameLookup 要几十分钟)。
    代价: 吸附失败的格(退市整理/新股无限制/S股等)会被算进"档位解不出"一列单独报, 不混入。
  - 复现的是误差**主导项**(pre_close 的 float32 表示误差, 相对 ~6e-8)。引擎里
    `pv*(1+ratio)` 之后还有一次 float32 乘法, 量级更小且同号时结果不变。

用法: ./.venv/Scripts/python.exe scripts/oneoff/diag_float32_limit.py
输出: D:/claude-home/tmp/diag_float32_limit.log
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE))
_ORIG_STDOUT = sys.stdout
if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(_ORIG_STDOUT.buffer, encoding="utf-8", errors="replace")

import numpy as np                                        # noqa: E402
import pandas as pd                                       # noqa: E402

from app import db                                        # noqa: E402
from app.portfolio.board import round2                    # noqa: E402

LOG = Path("D:/claude-home/tmp/diag_float32_limit.log")
BANDS = np.array([0.05, 0.10, 0.20, 0.30])


def _r2(a):
    """向量化 round2（半进位 + 1e-9，与 app.portfolio.board.round2 逐字同构）。"""
    return np.floor(a * 100.0 + 0.5 + 1e-9) / 100.0


def main():
    lines = []

    def say(s=""):
        print(s)
        lines.append(s)

    conn = db.get_conn()
    say("=" * 92)
    say("float32 的 pre_close 对推导限价的影响 —— 触发面测量（只测量，不修改任何东西）")
    say("=" * 92)
    say("复现: round2(pc × (1±r))，pc 分别取 float64 原值 与 float(np.float32(pc))")

    rows = []
    for y in [r[0] for r in conn.execute(
            "SELECT DISTINCT substr(trade_date,1,4) y FROM stk_limit ORDER BY y")]:
        df = pd.read_sql(
            """SELECT l.trade_date AS d, l.ts_code AS c, l.up_limit AS lu, l.down_limit AS ld,
                      d0.pre_close AS pc, d0.close AS cl
               FROM stk_limit l JOIN daily d0
                 ON d0.trade_date = l.trade_date AND d0.ts_code = l.ts_code
               WHERE substr(l.trade_date,1,4)=? AND d0.pre_close > 0
                 AND l.up_limit < 99999 AND l.down_limit > 0.02""",
            conn, params=(y,))
        if df.empty:
            continue
        pc64 = df["pc"].to_numpy(dtype="float64")
        pc32 = np.float32(pc64).astype("float64")
        lu_t = df["lu"].to_numpy(dtype="float64")
        ld_t = df["ld"].to_numpy(dtype="float64")
        cl = df["cl"].to_numpy(dtype="float64")
        # 从公布值反解档位并吸附到四档；解不出的记为 NaN 单独计数
        ru = np.round(lu_t / pc64 - 1.0, 4)
        rd = np.round(1.0 - ld_t / pc64, 4)
        ru = np.where(np.min(np.abs(ru[:, None] - BANDS[None, :]), axis=1) < 5e-4,
                      BANDS[np.argmin(np.abs(ru[:, None] - BANDS[None, :]), axis=1)], np.nan)
        rd = np.where(np.min(np.abs(rd[:, None] - BANDS[None, :]), axis=1) < 5e-4,
                      BANDS[np.argmin(np.abs(rd[:, None] - BANDS[None, :]), axis=1)], np.nan)
        ok = ~np.isnan(ru) & ~np.isnan(rd)

        lu64, lu32 = _r2(pc64 * (1 + np.nan_to_num(ru))), _r2(pc32 * (1 + np.nan_to_num(ru)))
        ld64, ld32 = _r2(pc64 * (1 - np.nan_to_num(rd))), _r2(pc32 * (1 - np.nan_to_num(rd)))
        du, dl = lu32 - lu64, ld32 - ld64
        moved = ok & ((np.abs(du) > 1e-9) | (np.abs(dl) > 1e-9))
        hit64 = ok & (np.abs(lu64 - lu_t) < 0.005) & (np.abs(ld64 - ld_t) < 0.005)
        hit32 = ok & (np.abs(lu32 - lu_t) < 0.005) & (np.abs(ld32 - ld_t) < 0.005)
        # 「L1 说对、引擎算错」还要求这一分钱**真的改变了不等式**（否则无后果）
        near_buy = ok & (np.abs(cl - lu_t) < 0.005)
        near_sell = ok & (np.abs(cl - ld_t) < 0.005)
        rows.append(dict(
            year=y, cells=len(df), unresolved=int((~ok).sum()), moved=int(moved.sum()),
            up_low=int((ok & (du < -1e-9)).sum()), up_high=int((ok & (du > 1e-9)).sum()),
            dn_low=int((ok & (dl < -1e-9)).sum()), dn_high=int((ok & (dl > 1e-9)).sum()),
            hit64=int(hit64.sum()), hit32=int(hit32.sum()),
            f64_ok_f32_bad=int((hit64 & ~hit32).sum()),
            f64_bad_f32_ok=int((~hit64 & hit32).sum()),
            # ⭐ 真正有后果的: 那一分钱发生在 close 正好等于公布限价的那天
            fatal=int((moved & near_buy).sum()), fatal_sell=int((moved & near_sell).sum()),
        ))
    conn.close()

    R = pd.DataFrame(rows)
    say("\n%-6s %9s %8s %9s %8s %8s %8s %8s %8s %8s %9s"
        % ("年", "格数", "解不出档", "被移动", "上轨↓", "上轨↑", "下轨↓", "下轨↑",
           "f64命中", "f32命中", "f64对f32错"))
    for _, r in R.iterrows():
        say("%-6s %9d %8d %9d %8d %8d %8d %8d %8d %8d %9d"
            % (r["year"], r["cells"], r["unresolved"], r["moved"], r["up_low"],
               r["up_high"], r["dn_low"], r["dn_high"], r["hit64"], r["hit32"],
               r["f64_ok_f32_bad"]))
    t = R.drop(columns=["year"]).sum()
    say("%-6s %9d %8d %9d %8d %8d %8d %8d %8d %8d %9d"
        % ("合计", t["cells"], t["unresolved"], t["moved"], t["up_low"], t["up_high"],
           t["dn_low"], t["dn_high"], t["hit64"], t["hit32"], t["f64_ok_f32_bad"]))

    say("\n--- 真正有后果的那一格: close 正好落在公布限价上的天数 ---")
    say("  float32 把限价移动了、且当天 close == 公布限价 ⇒ **这一分钱改变了拒单不等式**")
    say("  买(涨停): %d 格    卖(跌停): %d 格" % (t["fatal"], t["fatal_sell"]))
    say("  (002638.SZ 那 5 天属于「卖」这一类 —— 引擎 dn 算成 1.99, close 2.00 > 1.99")
    say("   ⇒ 判「卖得出去」; 真值 dn=2.00 ⇒ 封死。)")

    say("\n--- 判读 ---")
    say("  「被移动」= float32 输入让推导限价偏离 float64 至少一分。")
    say("  「f64对f32错」= L1 说对了、引擎运行时算错 —— **L1 结构上看不见的那部分**。")
    say("  方向: 上轨取低 ⇒ 「以为还能买」; 下轨取低 ⇒ 「以为还能卖」(危险方向)。")
    say("  ⚠️ 只测量, 不修 —— 修 `round2` 输入口径 = 改成交口径 = 会让 pin 失效,")
    say("     必须独立预注册 + 组合层两轴判决。")

    LOG.parent.mkdir(parents=True, exist_ok=True)
    LOG.write_text("\n".join(lines), encoding="utf-8")
    print("\n[日志] %s" % LOG)


if __name__ == "__main__":
    main()
