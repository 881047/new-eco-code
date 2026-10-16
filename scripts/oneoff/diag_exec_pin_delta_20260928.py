"""诊断: 撮合口径换 pin 的 Δ 到底来自哪里(2026-09-28, P1.5 查明步骤)

背景(必须查明的原因)
--------------------
翻 FILL_AT_OPEN=False→True + OVERLAP_REBAL_COST=False→True 后:
    prod 系统年化 22.12% → 29.13%  (Δ = +7.01pp)
    full 系统年化 13.80% → 18.77%  (Δ = +4.97pp)

而 2026-09-12 的 E6 实测在旧基线上只有 +2.33pp(prod)。Δ 大了 3 倍, 且**超出
`plans/pin-exec-round-prereg-20260928.md` 冻结的 ±0.3pp 容差** ⇒ 按预注册第 2 步,
必须查明原因, 不许直接采纳。

另外一个必须查的红旗: full 窗**系统年化(18.77%) 反超满仓年化(18.65%)**。系统线权益
仓位只有满仓的 0.873 且闲置 31.6%, 单位资金收益却超过满仓线 —— 这在数学上反常,
要么我对两条线的理解有误, 要么存在实现问题。

本脚本回答三个问题(只读, 不改任何生产数据)
-------------------------------------------
Q1. Δ 是"普遍的小幅优势累积"还是"少数极端月的巨幅贡献"?
    → 若集中在 1-3 个月 ⇒ 可疑(可能是某次异常成交/封板判定);
      若分散且符号一致 ⇒ 可信(系统性执行优势)。
Q2. 系统线 vs 满仓线的"单位资金效率"为何在新口径下反常?
    → 对比两条线的月度收益与投入比例。
Q3. 成交率是否变化(作废单/结转是否增多)?
    → 若开盘价判封板导致大量卖不出 ⇒ 新口径有实盘可行性风险。

用法
----
  ./.venv/Scripts/python.exe -X utf8 scripts/oneoff/diag_exec_pin_delta_20260928.py
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app import db

OLD_RUN = "20260928_probe"        # 旧口径(收盘价, FILL_AT_OPEN=False)
NEW_RUN = "20260928_exe"          # 新口径(开盘价, FILL_AT_OPEN=True)
OLD_FULL = OLD_RUN + "_full"
NEW_FULL = NEW_RUN + "_full"


def _find_nav_table(conn):
    """NAV 序列存在哪张表(不同版本可能改名, 不猜)。"""
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    cands = [r[0] for r in rows if "nav" in r[0].lower() or "backtest" in r[0].lower()]
    out = []
    for t in cands:
        cols = [c[1] for c in conn.execute(f"PRAGMA table_info({t})").fetchall()]
        if "run_date" in cols and ("sys_nav" in cols or "nav" in cols):
            out.append((t, cols))
    return out


def _load(conn, table, cols, run):
    """取某 run 的月末 NAV 序列(排除月内日频盯市行)。

    ⚠️ 2026-09-28 自查修正: 首版写成 `ORDER BY nav`(按**净值大小**排序) ⇒ 月度序列被
    完全打乱, 配对 Δ 全是噪声, 据此得出的结论无效。必须按 **trade_date** 排序
    (这与项目"日频 NAV 用于回撤"的纪律同源: 序列的顺序本身就是数据)。
    """
    has_sys = "sys_nav" in cols
    sel = "trade_date, nav" + (", sys_nav" if has_sys else "")
    df = pd.read_sql(
        f"SELECT {sel} FROM {table} WHERE run_date=? ORDER BY trade_date",
        conn, params=(run,))
    # 月末表理论上每期一行; 若月内日频行混入, 按 trade_date 去重保留最后一条
    return df.drop_duplicates(subset="trade_date", keep="last").reset_index(drop=True)


def main():
    db.init_db()
    conn = db.get_conn()
    tables = _find_nav_table(conn)
    print("=" * 88)
    print("候选 NAV 表:", [t for t, _ in tables])
    if not tables:
        print("!! 没找到 NAV 表, 退出")
        return 1

    for tname, cols in tables:
        print(f"\n--- 表 {tname}  列 {cols}")
        for run in (OLD_RUN, NEW_RUN, OLD_FULL, NEW_FULL):
            n = conn.execute(
                f"SELECT COUNT(*) FROM {tname} WHERE run_date=?", (run,)).fetchone()[0]
            print(f"    {run:<24} {n} 行")

    # 选一张两个 run 都有数据的表
    table, cols = None, None
    for t, c in tables:
        cnt = {}
        for run in (OLD_RUN, NEW_RUN):
            cnt[run] = conn.execute(
                f"SELECT COUNT(*) FROM {t} WHERE run_date=?", (run,)).fetchone()[0]
        if all(v > 0 for v in cnt.values()):
            table, cols = t, c
            break
    if table is None:
        print("\n!! 没有同时含两个 run 的表, 退出")
        return 1

    print("\n" + "=" * 88)
    print(f"使用表 {table}")
    print("=" * 88)

    for window, ro, rn in (("prod", OLD_RUN, NEW_RUN), ("full", OLD_FULL, NEW_FULL)):
        do = _load(conn, table, cols, ro)
        dn = _load(conn, table, cols, rn)
        if do.empty or dn.empty:
            print(f"\n[{window}] 数据缺失 old={len(do)} new={len(dn)}, 跳过")
            continue
        # 按 trade_date 严格配对(两 run 同窗口同锚点, 日期应完全一致)
        m = do.merge(dn, on="trade_date", suffixes=("_o", "_n"))
        print(f"\n[{window}] 期数 old={len(do)} new={len(dn)} "
              f"配对后={len(m)} (按 trade_date 对齐)")
        if len(m) != len(do) or len(m) != len(dn):
            print(f"    ⚠️ 配对后期数与单侧不等, 日期可能错位, 结论不可信")
        n = len(m)
        for label, key in (("满仓 nav", "nav"), ("系统 sys_nav", "sys_nav")):
            if key not in cols:
                continue
            ao = m[key + "_o"].values
            an = m[key + "_n"].values
            ro_ = np.diff(ao) / ao[:-1]
            rn_ = np.diff(an) / an[:-1]
            d = rn_ - ro_
            ppy = 12
            geoo = ao[-1] ** (ppy / (n - 1)) - 1
            geon = an[-1] ** (ppy / (n - 1)) - 1
            print(f"\n  {label}:")
            print(f"    年化(几何) old {geoo:+.2%} → new {geon:+.2%}  "
                  f"Δ {(geon - geoo) * 100:+.2f}pp")
            print(f"    月度 Δ: 均值 {d.mean() * 100:+.3f}pp  中位 "
                  f"{np.median(d) * 100:+.3f}pp  σ {d.std() * 100:.3f}pp")
            print(f"    月度 Δ 符号: 正 {(d > 0).sum()} / 负 {(d < 0).sum()} / "
                  f"零 {(d == 0).sum()}  (共 {len(d)})")
            # 集中度: 最大的 3 个月贡献了多少
            idx = np.argsort(-d)[:3]
            tot = d.sum()
            top3 = d[idx].sum()
            print(f"    Δ 集中度: 最大 3 个月占总 Δ 的 "
                  f"{top3 / tot * 100:.1f}% (总 Δ={tot * 100:+.2f}pp)")
            print(f"    最大 3 个月(1-based 期序): "
                  f"{[(int(i) + 1, round(d[i] * 100, 2)) for i in idx]}")
            print(f"    最差 3 个月: "
                  f"{[(int(i) + 1, round(d[i] * 100, 2)) for i in np.argsort(d)[:3]]}")
            # 月度 Δ 的 t 值(配对差)
            if d.std() > 0:
                t = d.mean() / (d.std() / np.sqrt(len(d)))
                print(f"    配对差 t = {t:+.2f}")

    # ---- Q3: 最大 Δ 月份明细(判定是"真实执行优势"还是"单月异常/搬移") ----
    print("\n" + "=" * 88)
    print("Q3 最大贡献月明细(系统线, prod 窗)")
    print("=" * 88)
    do = _load(conn, table, cols, OLD_RUN)
    dn = _load(conn, table, cols, NEW_RUN)
    mm = do.merge(dn, on="trade_date", suffixes=("_o", "_n"))
    mm["r_o"] = mm["sys_nav_o"].pct_change()
    mm["r_n"] = mm["sys_nav_n"].pct_change()
    mm["d"] = mm["r_n"] - mm["r_o"]
    top = mm.dropna(subset=["d"]).nlargest(4, "d")
    bot = mm.dropna(subset=["d"]).nsmallest(4, "d")
    for title, sub in (("最大 4 个 Δ 月", top), ("最差 4 个 Δ 月", bot)):
        print(f"\n  {title}:")
        for _, r in sub.iterrows():
            print(f"    {r['trade_date']}  old {r['r_o'] * 100:+7.2f}%  "
                  f"new {r['r_n'] * 100:+7.2f}%  Δ {r['d'] * 100:+7.2f}pp  "
                  f"| nav_o {r['nav_o']:.4f} nav_n {r['nav_n']:.4f}")
    # 相邻月正负成对 ⇒ 收益在月间"搬移"而非创造
    seq = mm.dropna(subset=["d"])["d"].values
    flip = int((np.sign(seq[1:]) != np.sign(seq[:-1])).sum())
    print(f"\n  月度 Δ 符号翻转次数: {flip} / {len(seq) - 1} "
          f"({flip / (len(seq) - 1) * 100:.0f}%)  "
          f"—— 接近 50% 说明是随机摆动, 显著低于 50% 说明是持续同向")

    print("\n" + "=" * 88)
    print("判读口径(预注册冻结):")
    print("  · 月度 Δ 符号一致率 ≥2/3 且 |t|≥2 ⇒ 系统性执行优势, 采纳")
    print("  · 最大 3 个月占总 Δ >60% ⇒ 集中在少数月, 视为可疑, 需逐月人工复核")
    print("  · 低集中度 + 分散同号 ⇒ 可信")
    print("=" * 88)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
