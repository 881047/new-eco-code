"""M4：池等权参照线被 `ABS(pct_chg)<=21` 护栏抬高了多少 —— 只读实测。

`app/models/pool_index.py` 的 docstring 已登记（2026-09-20）：过滤发生在**月内累计之前**
⇒ 退市股终点那一笔 −100% 进不了 `AVG(lvl)` ⇒ 池线系统性**偏乐观**。
受影响读数包括 02 号的"池口径 +17.78pp"与 F74 的池参照线/排除线。
本脚本量它的**上界**（不改任何东西）。

判据跑前冻结在 `plans/ledger-settlement-criteria-20260920.md` §M4：
  B = 逐月月均"基线月收益 − 反事实月收益"（pp/月），B < 0.05pp/月 ⇒ 关闭（02 号写限定语）；
  B ≥ 0.05pp/月 ⇒ 单列口径轮（会同时动 F52/F74 的参照线）。
两版 SQL 只差那一个 `ABS(pct_chg)<=21` ⇒ 差异只能来自护栏本身（M4-a 同域同窗对照）。
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

OUT = ROOT / "experiments" / "b_stage_20260920" / "m4_pool_filter_bias.json"
START, END = "20170206", "20260907"      # F52/F66 的判决窗（491 期那一条），不是随便一段

DOMAIN = ("ts_code NOT LIKE '688%' AND ts_code NOT LIKE '689%' AND ts_code NOT LIKE '8%' "
          "AND ts_code NOT LIKE '4%' AND ts_code NOT LIKE '9%'")
# 终点 −100% 那笔：SQLite 的 LN(0) 返回 NULL 而不是 −inf ⇒ 会被 SUM 跳过（等于"那天没跌"），
# 所以必须显式 clamp。取 −99% 是**保守**下界（真实退市是 −100%）⇒ 测出来的下修量偏小。
CLAMP = "(CASE WHEN pct_chg < -99.0 THEN -99.0 ELSE pct_chg END)"


def pool_sql(extra: str) -> str:
    return f"""
WITH path AS (
    SELECT trade_date,
           exp(SUM(LN(1 + {CLAMP}/100.0)) OVER (
               PARTITION BY ts_code, substr(trade_date,1,6) ORDER BY trade_date)) AS lvl
    FROM daily
    WHERE amount > 0 AND pct_chg IS NOT NULL AND {DOMAIN} {extra}
      AND trade_date BETWEEN '{START}' AND '{END}'
)
SELECT trade_date, AVG(lvl) AS lvl, COUNT(*) AS n FROM path GROUP BY trade_date
"""


def monthly(series: pd.Series) -> pd.Series:
    idx = pd.to_datetime(series.index, format="%Y%m%d")
    grp = series.groupby([idx.year, idx.month])
    return grp.last() / grp.first() - 1.0


def main() -> int:
    ro = "file:" + str((ROOT / "data" / "neweco.db")).replace("\\", "/") + "?mode=ro"
    conn = sqlite3.connect(ro, uri=True)
    conn.execute("PRAGMA query_only=ON")
    try:
        lv = {k: pd.read_sql(pool_sql(e), conn, parse_dates=False)
                  .set_index("trade_date")["lvl"]
              for k, e in (("V0_both", "AND ABS(pct_chg) <= 21"),      # 生产基线（两条尾巴都滤）
                           ("V1_noDown", "AND pct_chg <= 21"),          # 放开深跌/退市终点
                           ("V2_noUp", "AND pct_chg >= -21"),           # 放开暴涨（新股首日等）
                           ("V0_rerun", "AND ABS(pct_chg) <= 21"))}     # M4-a 复跑一致性
        drop_lo = pd.read_sql(
            f"SELECT substr(trade_date,1,6) AS ym, COUNT(*) AS n, MIN(pct_chg) AS worst "
            f"FROM daily WHERE amount > 0 AND pct_chg IS NOT NULL AND pct_chg < -21 "
            f"AND {DOMAIN} AND trade_date BETWEEN '{START}' AND '{END}' GROUP BY ym", conn)
        drop_hi = pd.read_sql(
            f"SELECT COUNT(*) AS n, MAX(pct_chg) AS best FROM daily WHERE amount > 0 "
            f"AND pct_chg IS NOT NULL AND pct_chg > 21 AND {DOMAIN} "
            f"AND trade_date BETWEEN '{START}' AND '{END}'", conn)
        ncells = pd.read_sql(
            f"SELECT COUNT(*) AS c FROM daily WHERE amount>0 AND pct_chg IS NOT NULL "
            f"AND {DOMAIN} AND trade_date BETWEEN '{START}' AND '{END}'", conn)["c"][0]
    finally:
        conn.close()

    def mdiff(a, b):
        ma, mb = monthly(lv[a]), monthly(lv[b])
        common = ma.index.intersection(mb.index)
        return (ma.reindex(common) - mb.reindex(common)) * 100.0, len(common)

    b_down, n_m = mdiff("V0_both", "V1_noDown")     # 护栏砍掉深跌 ⇒ 基线比"放开深跌"高多少
    b_up, _ = mdiff("V0_both", "V2_noUp")           # 护栏砍掉暴涨 ⇒ 负数（基线更低）
    n_deep = int(drop_lo["n"].sum()) if len(drop_lo) else 0
    n_le30 = int(drop_lo[drop_lo["worst"] <= -30]["n"].sum()) if len(drop_lo) else 0
    n_hi = int(drop_hi["n"][0])
    res = {
        "criteria": "plans/ledger-settlement-criteria-20260920.md §M4（跑前冻结；仪器定义见"
                    "'仪器更正版' —— 第一版把两条尾巴混在一个对照里，方向门抓住后已重做）",
        "window": [START, END],
        "verdict": None,
        "B_down_channel_pp_per_month": round(float(b_down.mean()), 4),
        "B_down_abs_max_pp_per_month": round(float(b_down.abs().max()), 4),
        "B_down_months_ge_0.05pp": int((b_down >= 0.05).sum()),
        "B_down_annualized_pp": round(float(b_down.mean()) * 12, 3),
        "B_up_channel_pp_per_month_for_reference": round(float(b_up.mean()), 4),
        "n_months": int(n_m),
        "rows_dropped_deep_le_minus21": n_deep,
        "rows_dropped_le_minus30": n_le30,
        "rows_dropped_big_pos_gt_21": n_hi,
        "worst_dropped_deep_pct_chg": float(drop_lo["worst"].min()) if len(drop_lo) else None,
        "pool_domain_cells": int(ncells),
        "clamp_note": "终点跌幅 clamp 到 −99%（SQLite 的 LN(0)=NULL 会被 SUM 跳过）⇒ "
                      "测得的下修量是**保守下界**，真实退市那笔是 −100%",
        "gate_M4a_rerun_identical": bool(np.allclose(lv["V0_both"].values, lv["V0_rerun"].values)),
        "gate_M4b_no_zero_rows": bool(n_deep > 0),
        "gate_M4c_channels_differ": bool(abs(float(b_down.mean())) > 0 and abs(float(b_up.mean())) > 0),
        "not_measured": [],
    }
    B = res["B_down_channel_pp_per_month"]
    if not (res["gate_M4a_rerun_identical"] and res["gate_M4c_channels_differ"]):
        res["verdict"] = "本轮读数作废：实现自证门未过"
    elif not res["gate_M4b_no_zero_rows"]:
        res["verdict"] = "未测量（判决窗内域里没有 ≤−21% 的被滤行，不得判'无影响'）"
    elif B < 0:
        res["verdict"] = "本轮读数作废：方向与预期相反（深跌通道护栏让池线变低？）⇒ 先修仪器再判"
    elif B < 0.05:
        res["verdict"] = "关闭：退市/深跌半边被护栏抬高的量级 < 冻结门槛 0.05pp/月 ⇒ 02 号补限定语"
    else:
        res["verdict"] = "单列口径轮：量级越门，动它 = 换尺（会同时改 F52/F74 的参照线）"
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(res, indent=2, ensure_ascii=False, default=str) + "\n", "utf-8")
    print(json.dumps({k: res[k] for k in ("verdict", "B_down_channel_pp_per_month",
                                          "B_down_annualized_pp", "B_up_channel_pp_per_month_for_reference",
                                          "rows_dropped_deep_le_minus21", "rows_dropped_big_pos_gt_21",
                                          "gate_M4a_rerun_identical")}, ensure_ascii=False))
    print(f"→ {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
