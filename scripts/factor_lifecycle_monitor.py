r"""因子生命周期监控（研究层工具）—— 31 因子库的半衰期/拥挤度仪表盘。

跑法（cwd = 仓根）：
    .venv\\Scripts\\python.exe -X utf8 -m scripts.factor_lifecycle_monitor

## 是什么（调研 plans/neweco-model-innovation-survey-20260925.md 的 #2 落地件）

行业标准因子循环 = 挖掘 → **IC/半衰期/拥挤度监控** → 因子库迭代。老盘已有 IC 抽查
（train/health），缺的是**逐因子全生命周期的衰减与拥挤度量表**。本工具只读研究层，
**不改生产打分、不触发任何 A/B**——它的产出是"哪些因子在衰减/拥挤"的事实表，
供后续预注册（因子降权/替换）引用。

## 口径（对齐 trainer）

- 因子面板：生产库 `factor` 表（31 因子 × 月末 × 20140130..20260924），逐因子读取；
- 标签：`trainer.forward_return` 同式（21 交易日复权收益，含退市"最后有效价"回退），
  从月末交易日往后数 21 个开市日；
- 逐月 rank IC（Spearman，与 trainer 评测同族）；**方向对齐**：训练时低因子值对应
  高收益的因子（如 size/bias）IC 天然为负——监控用 **|IC| 的衰减与稳定性**，
  另报原始符号供人工核对；
- 半衰期代理 = `trailing_12m_IC / long_run_IC`（<1 = 衰减中）；
- 拥挤度代理 = `trailing_12m 多空(前20%−后20%)收益 / 长期同口径`（近端收益骤升
  = 拥挤资金涌入的典型形态，华泰系列口径）；

## 红黄绿灯（事实表，非判据）

    绿: |decay| ≥ 0.7 且拥挤比 ≤ 2.0
    黄: 其余
    红: 强度比 < 0.4（衰减）/ 强度比为负（符号翻转）/ 拥挤比 > 3.0
"""
import json
import sqlite3
import time
from pathlib import Path
from datetime import datetime

import numpy as np
import pandas as pd

DB = r"data/neweco.db"
H = 21                                   # trainer.HORIZON 同值
TOPQ = 0.20                              # 多空分位
LONG_WIN = 60                            # 长窗（月）≈ 5 年
TRAIL_WIN = 12                           # 尾窗（月）＝ 1 年


def month_ends(conn) -> list:
    rows = conn.execute("SELECT DISTINCT trade_date FROM factor ORDER BY trade_date").fetchall()
    return [r[0] for r in rows]


def load_factor_panel(conn, factor: str, months: list) -> pd.DataFrame:
    q = ",".join("?" * len(months))
    d = pd.read_sql(f"SELECT trade_date, ts_code, value FROM factor "
                    f"WHERE factor=? AND trade_date IN ({q})",
                    conn, params=(factor, *months))
    return d.pivot_table(index="trade_date", columns="ts_code", values="value", aggfunc="last")


def load_close_grid(conn, months: list) -> pd.DataFrame:
    """→ 全日历区间的 adj close 网格（行 = 全部开市日，含各月末 + 末月后 H+10 个开市日）。

    ⚠️ 修复记录（2026-09-25）：初版只加载 t+H 目标日的价格 ⇒ 网格行集 = 目标日集，
    forward_returns 里 `pos.get(月末)` 只能靠巧合命中（95 期只命中 15 期）——
    月度标签序列被静默截断。现改为**全区间**加载（行 = 全部开市日），任何月末都可查。"""
    lo = min(months)
    all_dates = [r[0] for r in conn.execute(
        "SELECT DISTINCT trade_date FROM daily WHERE trade_date >= ? ORDER BY trade_date", (lo,))]
    pos = {d: i for i, d in enumerate(all_dates)}
    i_last = pos.get(max(months))
    upper = all_dates[min(i_last + H + 10, len(all_dates) - 1)]
    q = ",".join("?" * len(all_dates[: pos[upper] + 1]))
    span = all_dates[: pos[upper] + 1]
    q = ",".join("?" * len(span))
    px = pd.read_sql(f"SELECT trade_date, ts_code, close FROM daily WHERE trade_date IN ({q})",
                     conn, params=span)
    af = pd.read_sql(f"SELECT trade_date, ts_code, adj_factor FROM adj_factor WHERE trade_date IN ({q})",
                     conn, params=span)
    c = px.merge(af, on=["trade_date", "ts_code"], how="left")
    c["adj_close"] = c["close"] * pd.to_numeric(c["adj_factor"], errors="coerce")
    grid = c.pivot_table(index="trade_date", columns="ts_code",
                         values="adj_close", aggfunc="last").sort_index()
    return grid


def forward_returns(grid: pd.DataFrame, months: list) -> pd.DataFrame:
    """→ {月末: 未来 21 开市日收益}（从月末网格行取 t 与 t+H 行）。"""
    pos = {d: i for i, d in enumerate(grid.index)}
    rows = {}
    for m in months:
        i = pos.get(m)
        if i is None or i + H >= len(grid):
            continue
        p0 = grid.iloc[i]
        p1 = grid.iloc[i + H]
        with np.errstate(invalid="ignore", divide="ignore"):
            rows[m] = p1 / p0 - 1.0
    return pd.DataFrame(rows).T                      # index=月末, columns=ts_code


def rank_ic_series(fac: pd.DataFrame, fwd: pd.DataFrame) -> pd.Series:
    """→ 逐月 Spearman rank IC（当月因子值 vs 未来 21 日收益）。"""
    ics = {}
    for m in fac.index:
        if m not in fwd.index:
            continue
        x = fac.loc[m]
        y = fwd.loc[m]
        ok = x.notna() & y.notna() & np.isfinite(x) & np.isfinite(y)
        if ok.sum() < 100:
            continue
        ics[m] = float(x[ok].rank().corr(y[ok].rank()))
    return pd.Series(ics, name="ic").sort_index()


def ls_return_series(fac: pd.DataFrame, fwd: pd.DataFrame) -> pd.Series:
    """→ 逐月多空收益（前 TOPQ − 后 TOPQ，按因子值方向无关：取 |多空| 的符号跟随 IC）。"""
    ls = {}
    for m in fac.index:
        if m not in fwd.index:
            continue
        x = fac.loc[m]
        y = fwd.loc[m]
        ok = x.notna() & y.notna() & np.isfinite(x) & np.isfinite(y)
        if ok.sum() < 100:
            continue
        r = x[ok].rank(pct=True)
        yv = y[ok]
        hi = yv[r >= 1 - TOPQ].mean()
        lo = yv[r <= TOPQ].mean()
        ls[m] = float(hi - lo)
    return pd.Series(ls, name="ls").sort_index()


def lifecycle(ic: pd.Series, ls: pd.Series) -> dict:
    long_ic = ic.iloc[-LONG_WIN:] if len(ic) > LONG_WIN else ic
    trail_ic = ic.iloc[-TRAIL_WIN:]
    base_ic = ic.iloc[:-TRAIL_WIN] if len(ic) > TRAIL_WIN * 2 else ic
    lr = float(base_ic.mean()) if len(base_ic) else float("nan")
    tr = float(trail_ic.mean()) if len(trail_ic) else float("nan")
    # 强度保持比 = |trailing| / |long-run|（<1 衰减；符号翻转时为负值 ⇒ 红(反转)）
    decay = (abs(tr) / abs(lr)) * (1.0 if (lr > 0) == (tr > 0) else -1.0)         if lr == lr and lr != 0 and tr == tr else float("nan")
    # 拥挤度：近 12 月 |多空| 均值 / 此前同口径
    ls_base = ls.iloc[:-TRAIL_WIN] if len(ls) > TRAIL_WIN * 2 else ls
    ls_tr = ls.iloc[-TRAIL_WIN:]
    lb = float(ls_base.abs().mean()) if len(ls_base) else float("nan")
    lt = float(ls_tr.abs().mean()) if len(ls_tr) else float("nan")
    crowd = lt / lb if lb == lb and lb > 0 else float("nan")
    a = abs(decay) if decay == decay else np.nan
    if decay == decay and decay < 0:
        v = "红(符号翻转)"
    elif crowd == crowd and crowd > 3.0:
        v = "红(拥挤)"
    elif a == a and a < 0.4:
        v = "红(衰减)"
    elif (a == a and a < 0.7) or (crowd == crowd and crowd > 2.0):
        v = "黄"
    else:
        v = "绿"
    return {"long_ic": round(lr, 4) if lr == lr else None,
            "trail_ic_12m": round(tr, 4) if tr == tr else None,
            "ic_ir": round(ic.mean() / ic.std(ddof=1), 2) if len(ic) > 12 and ic.std() else None,
            "decay_ratio": round(decay, 2) if decay == decay else None,
            "crowd_ratio": round(crowd, 2) if crowd == crowd else None,
            "n_months": int(len(ic)),
            "verdict": v}


def main():
    t0 = time.time()
    conn = sqlite3.connect(DB)
    months = month_ends(conn)
    print(f"月末网格 {len(months)} 期（{months[0]}..{months[-1]}）")
    fwd = None
    out = {}
    factors = [r[0] for r in conn.execute("SELECT DISTINCT factor FROM factor ORDER BY factor")]
    print(f"因子 {len(factors)} 个（逐个读取 ~2-3 分钟）", flush=True)
    for i, f in enumerate(factors):
        fac = load_factor_panel(conn, f, months)
        if fwd is None:
            grid = load_close_grid(conn, months)
            fwd = forward_returns(grid, months)
            print(f"  收益网格 {fwd.shape[0]} 期 × {fwd.shape[1]} 票", flush=True)
        ic = rank_ic_series(fac, fwd)
        ls = ls_return_series(fac, fwd)
        if len(ic) < TRAIL_WIN * 2:
            out[f] = {"verdict": "数据不足", "n_months": int(len(ic))}
            continue
        res = lifecycle(ic, ls)
        res["recent_sign"] = "+" if (len(ic) and ic.iloc[-TRAIL_WIN:].mean() > 0) else "-"
        out[f] = res
        print(f"  [{i + 1:2d}/{len(factors)}] {f:20s} IC({res['long_ic']:+.4f}) "
              f"衰减比 {res['decay_ratio']}｜拥挤比 {res['crowd_ratio']}｜{res['verdict']}",
              flush=True)
    conn.close()

    order = {"红(符号翻转)": 0, "红(衰减)": 1, "红(拥挤)": 2, "黄": 3, "绿": 4, "数据不足": 5}
    rows = sorted(out.items(), key=lambda kv: (order.get(kv[1].get("verdict"), 9),
                                               kv[1].get("decay_ratio") if kv[1].get("decay_ratio") is not None else 9))
    print("\n== 生命周期仪表盘（红→绿）==")
    for f, r in rows:
        if r.get("verdict") == "数据不足":
            continue
        print(f"  {r['verdict']:6s} {f:20s} 长期IC {r['long_ic']:+.4f}｜近12月 {r['trail_ic_12m']:+.4f}"
              f"｜衰减比 {r['decay_ratio']}｜拥挤比 {r['crowd_ratio']}")
    q = Path("data") / f"factor_lifecycle_{datetime.now().strftime('%Y%m%d')}.json"
    q.parent.mkdir(parents=True, exist_ok=True)
    payload = {"generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
               "note": "研究层因子生命周期仪表盘：只读，不改生产打分；"
                       "decay=crowd 口径见脚本 docstring；红黄绿灯为事实表非判据。",
               "horizon_days": H, "long_win_months": LONG_WIN, "trail_win_months": TRAIL_WIN,
               "factors": out, "runtime_sec": round(time.time() - t0, 1)}
    q.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\n读数入库：{q}（{payload['runtime_sec']}s）")


if __name__ == "__main__":
    main()
