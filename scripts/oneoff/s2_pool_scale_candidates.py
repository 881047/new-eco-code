"""S2 换尺**设计探针**：`pool_index` 的正确参照尺是哪一把（**只读**，不落库、不改表）。

## 为什么要先做这一步
14 号台账 S2 已量化「现行 vs 无护栏」翻档率 **8.57%**（`s2_pool_guard_impact.py`），
并据此建议"换尺"。但那把"无护栏"的尺**本身可能不是正确的尺**：

- 被护栏挡掉的上行 1692 行主要是**新股首日**（>+21% 在主板/创业板只有新股首日与
  退市整理期首日才可能出现）；
- 而选股域（`app/models/daily_score.py:145` 与 `trainer.tradable_mask` 同口径）要求
  **上市满 60 交易日** ⇒ 新股首日那笔 +44% 在策略侧**根本不可获得**。

⇒ 若"无护栏"这把尺把**策略不可获得的收益**算进了池子，那 0.3862pp/月 与 8.57% 就
不是"现行尺的偏差"，而是"两把尺口径不同"的产物。**换尺前必须先定义正确的尺。**

## 三把尺
| 臂 | 定义 | 性质 |
|---|---|---|
| **V0** | 现行：`ABS(pct_chg) <= 21`，无上市天数过滤 | 数值护栏，两个半边方向相反 |
| **V1** | 去掉 guard（`s2_pool_guard_impact.py` 的 B 臂）| 含新股首日 ⇒ 策略不可获得 |
| **V2** | **规则化**：剔"上市未满 60 交易日"（与选股域同口径）+ 跌幅 clamp −99% | 候选正确尺 |

三臂共用同一个 `regime._pool_state_frame`（**复用而非重写**）与同一套折收盘口径。

## 只读承诺
不写库、不调 `build_pool_index()`、不改 `pool_index` 表。产物只落 `experiments/`。
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pandas as pd

from app import db
# 私有函数，但为"复用而非重写"必须用它 —— 否则状态规则会被抄歪
from app.models.regime import _pool_state_frame

# 2026-09-21: 统一带 `d.` 前缀 —— V2/V3 里 `LEFT JOIN stock_basic s` 也带 ts_code 列,
# 不加前缀会让 `WHERE ts_code NOT LIKE ...` 报 "ambiguous column name"(原版 V2 就卡在这里,
# 所以这个探针此前**从未跑通过**)。⇒ 所有用到它的查询都必须给 daily 起别名 d。
_BASE_WHERE = """
      AND d.ts_code NOT LIKE '688%' AND d.ts_code NOT LIKE '689%' AND d.ts_code NOT LIKE '8%'
      AND d.ts_code NOT LIKE '4%' AND d.ts_code NOT LIKE '9%'"""

# V0 = 现行；V1 = 去护栏；两臂与 app/models/pool_index.py:_SQL 同口径，仅护栏条件不同
_SQL_TMPL = """
WITH path AS (
    SELECT trade_date,
           exp(SUM(LN(1 + pct_chg/100.0)) OVER (
               PARTITION BY ts_code, substr(trade_date,1,6) ORDER BY trade_date)) AS lvl
    FROM daily d
    WHERE d.amount > 0 AND d.pct_chg IS NOT NULL {guard}
""" + _BASE_WHERE + """
)
SELECT trade_date, AVG(lvl) AS lvl, COUNT(*) AS n
FROM path GROUP BY trade_date ORDER BY trade_date
"""
SQL_V0 = _SQL_TMPL.format(guard="AND ABS(pct_chg) <= 21")
SQL_V1 = _SQL_TMPL.format(guard="")

# ⚠️ 2026-09-21 修正（v1 稿的缺陷）：初版 V2 有两处**站不住**，都已修
#   ① 锚定：原用 `COALESCE(s.list_date,'00000000')`，而 PITEngine 的 `_first_trade`
#      = **max(面板首现日, list_date)**（`engine.py:97-118`）。两者不一致时会把
#      "已上市满 60 个**交易日**但 list_date 晚于 cutoff"的股票误剔（反向也会误留）。
#      ⇒ 改为 `MAX(COALESCE(list_date,'00000000'), first_seen)`，与生产逐字同口径。
#   ② 域不完整：选股域是**六层**（`engine.py:13-20`），初版只搬了第 1 层(上市天数)。
#      经核：`stock_size_pctl`/`stock_min_amt20` 生产未配置 ⇒ 默认 0 ⇒ 市值/流动性两层
#      的实际效果只是"剔 NaN"，与上市 60 日层**高度重叠**（上市初期 daily_basic 本就无数据）
#      ⇒ 本轮不实现，但**在产物里如实标注**（见 `domain_layers`）。
#      真正需要补的是 **④ST/退时点名层** ⇒ 单列为 **V3**（展开后仅 73 万行，性能可接受）。
#
# V2 = 规则化：剔上市未满 60 交易日（与生产 `_first_trade` 同口径）+ 跌幅 clamp −99%
#      （防 SQLite LN(0)=NULL 让退市那一笔从 SUM 里消失）
_CUT_FIRST = """
cal AS (
    SELECT trade_date, ROW_NUMBER() OVER (ORDER BY trade_date) AS rn
    FROM (SELECT DISTINCT trade_date FROM daily)
),
cut AS (
    SELECT c.trade_date AS td,
           (SELECT c2.trade_date FROM cal c2 WHERE c2.rn = c.rn - 60) AS cutoff
    FROM cal c
),
first AS (
    -- 列刻意改名成 f_code: 若也叫 ts_code, 下面 filt 的 WHERE 里 `ts_code NOT LIKE ...`
    -- 会与 daily/stock_basic 的同名列冲突 ⇒ SQLite 报 "ambiguous column name"
    SELECT ts_code AS f_code, MIN(trade_date) AS first_seen FROM daily GROUP BY ts_code
)"""

# ④ ST / 退市整理期：按 namechange 的 **时点名** 区间展开（不是当前名），
#   与 `engine.py:198 names.str.contains("ST|退")` 同规则
_ST_CTE = """
st AS (
    -- 同理改名 st_code/st_date, 避免与 daily 的同名列冲突
    SELECT n.ts_code AS st_code, d.trade_date AS st_date
    FROM (SELECT DISTINCT ts_code, start_date, COALESCE(end_date, '99999999') AS e
          FROM namechange
          WHERE name LIKE '%ST%' OR name LIKE '%退%') n
    JOIN (SELECT DISTINCT trade_date FROM daily) d
      ON d.trade_date BETWEEN n.start_date AND n.e
)"""

SQL_V2 = """
WITH """ + _CUT_FIRST + """,
filt AS (
    SELECT d.trade_date, d.ts_code, MAX(d.pct_chg, -99.0) AS pc
    FROM daily d
    JOIN cut c ON c.td = d.trade_date
    JOIN first f ON f.f_code = d.ts_code
    LEFT JOIN stock_basic s ON s.ts_code = d.ts_code
    WHERE d.amount > 0 AND d.pct_chg IS NOT NULL""" + _BASE_WHERE + """
      AND (c.cutoff IS NULL
           OR MAX(COALESCE(s.list_date, '00000000'), f.first_seen) <= c.cutoff)
),
path AS (
    SELECT trade_date,
           exp(SUM(LN(1 + pc/100.0)) OVER (
               PARTITION BY ts_code, substr(trade_date,1,6) ORDER BY trade_date)) AS lvl
    FROM filt
)
SELECT trade_date, AVG(lvl) AS lvl, COUNT(*) AS n
FROM path GROUP BY trade_date ORDER BY trade_date
"""

# V3 = V2 + ④ST/退层（六层里唯一还没被 V2 覆盖、且不与上市 60 日重叠的一层）
SQL_V3 = """
WITH """ + _CUT_FIRST + "," + _ST_CTE + """,
filt AS (
    SELECT d.trade_date, d.ts_code, MAX(d.pct_chg, -99.0) AS pc
    FROM daily d
    JOIN cut c ON c.td = d.trade_date
    JOIN first f ON f.f_code = d.ts_code
    LEFT JOIN stock_basic s ON s.ts_code = d.ts_code
    LEFT JOIN st ON st.st_code = d.ts_code AND st.st_date = d.trade_date
    WHERE d.amount > 0 AND d.pct_chg IS NOT NULL""" + _BASE_WHERE + """
      AND st.st_code IS NULL
      AND (c.cutoff IS NULL
           OR MAX(COALESCE(s.list_date, '00000000'), f.first_seen) <= c.cutoff)
),
path AS (
    SELECT trade_date,
           exp(SUM(LN(1 + pc/100.0)) OVER (
               PARTITION BY ts_code, substr(trade_date,1,6) ORDER BY trade_date)) AS lvl
    FROM filt
)
SELECT trade_date, AVG(lvl) AS lvl, COUNT(*) AS n
FROM path GROUP BY trade_date ORDER BY trade_date
"""

BASE_LEVEL = 1000.0
OUT = Path(__file__).resolve().parents[2] / "experiments" / "s2_pool_scale_candidates.json"


def _build(sql: str) -> pd.Series:
    """按 pool_index.build_pool_index 的同一口径把 SQL 结果折成收盘序列（不落库）。"""
    conn = db.get_conn()
    try:
        df = pd.read_sql(sql, conn)
    finally:
        conn.close()
    if df.empty:
        return pd.Series(dtype=float)
    df["lvl"] = df["lvl"].astype(float)
    ym = df["trade_date"].str[:6]
    month_end = df.groupby(ym)["lvl"].last()
    cum = month_end.cumprod()
    df["close"] = BASE_LEVEL * ym.map(cum / month_end) * df["lvl"]
    s = df.set_index("trade_date")["close"].astype(float)
    s.index = pd.to_datetime(s.index)
    return s.sort_index()


def _annualized(s: pd.Series) -> float:
    """全窗年化（pp）：按交易日复利折算 252 日。"""
    if len(s) < 2:
        return float("nan")
    total = float(s.iloc[-1] / s.iloc[0])
    return (total ** (252.0 / (len(s) - 1)) - 1.0) * 100.0


def _monthly_bias_pp(a: pd.Series, b: pd.Series) -> float:
    """a 相对 b 的月度偏差（pp/月）：月末收益差的均值 ×100。"""
    ma = a.resample("ME").last().pct_change().dropna()
    mb = b.resample("ME").last().pct_change().dropna()
    idx = ma.index.intersection(mb.index)
    if len(idx) == 0:
        return float("nan")
    return float((ma.loc[idx] - mb.loc[idx]).mean() * 100.0)


def _flip(a: pd.Series, b: pd.Series) -> dict:
    """两臂 regime 状态翻档统计（共用 _pool_state_frame）。"""
    fa, fb = _pool_state_frame(a), _pool_state_frame(b)
    j = fa.index.intersection(fb.index)
    if len(j) == 0:
        return {"state_days": 0}
    sa, sb = fa.loc[j, "pool_state"], fb.loc[j, "pool_state"]
    diff = sa != sb
    return {
        "state_days": int(len(j)),
        "flip_days": int(diff.sum()),
        "flip_rate": round(float(diff.mean()), 6),
        "counts_a": {str(k): int(v) for k, v in sa.value_counts().items()},
        "counts_b": {str(k): int(v) for k, v in sb.value_counts().items()},
    }


def _blocked_rows_breakdown(conn) -> dict:
    """被现行护栏挡掉的行：上行/下行分边，并标"是否上市未满 60 交易日"。"""
    where = ("d.amount > 0 AND d.pct_chg IS NOT NULL" + _BASE_WHERE)
    up = pd.read_sql(
        f"SELECT d.ts_code, d.trade_date, d.pct_chg FROM daily d "
        f"WHERE {where} AND d.pct_chg > 21", conn)
    dn = pd.read_sql(
        f"SELECT d.ts_code, d.trade_date, d.pct_chg FROM daily d "
        f"WHERE {where} AND d.pct_chg < -21", conn)
    out = {"up_rows": int(len(up)), "down_rows": int(len(dn))}
    # 一次性算出"截至当日该股已有几个交易日"，替代逐行 COUNT(*)（见下面 seq 的注释）
    _SEQ = {}
    if not (up.empty and dn.empty):
        _seq = pd.read_sql(
            f"""SELECT b.ts_code, b.trade_date, COUNT(*) AS seq
                FROM daily d
                JOIN (SELECT d2.ts_code AS ts_code, d2.trade_date AS trade_date FROM daily d2
                      WHERE {where.replace('d.', 'd2.')}
                        AND (d2.pct_chg > 21 OR d2.pct_chg < -21)) b
                  ON d.ts_code = b.ts_code AND d.trade_date <= b.trade_date
                GROUP BY b.ts_code, b.trade_date""", conn)
        _SEQ = {(r.ts_code, r.trade_date): int(r.seq) for r in _seq.itertuples()}
    for tag, df in (("up", up), ("down", dn)):
        if df.empty:
            out[f"{tag}_seq_le_60"] = 0
            out[f"{tag}_worst_pct"] = None
            continue
        # seq = 该股截至当日的交易日数（用 daily 行序，停牌日不计）
        # 2026-09-21 修性能：原实现对**每一行**单独 `SELECT COUNT(*) FROM daily
        # WHERE ts_code=? AND trade_date<=?` —— 2519 行 = 2519 次扫描（实测会拖到
        # 几十分钟）。改为一次性 join 聚合，语义逐字不变。
        df = df.assign(seq=[_SEQ.get((c, d), 0)
                            for c, d in zip(df["ts_code"], df["trade_date"])])
        out[f"{tag}_seq_le_60"] = int((df["seq"] <= 60).sum())
        out[f"{tag}_worst_pct"] = round(float(df["pct_chg"].abs().max()), 4)
    return out


def main() -> int:
    t0 = time.time()
    print("== S2 设计探针：哪把尺是正确的（只读）==")
    conn = db.get_conn()
    try:
        br = _blocked_rows_breakdown(conn)
    finally:
        conn.close()
    print(f"  被挡上行 {br['up_rows']} 行（其中上市≤60 交易日 {br['up_seq_le_60']}）")
    print(f"  被挡下行 {br['down_rows']} 行（其中上市≤60 交易日 {br['down_seq_le_60']}）")

    arms = {}
    for name, sql in (("V0_guarded", SQL_V0), ("V1_noguard", SQL_V1),
                      ("V2_rule60", SQL_V2), ("V3_rule60_st", SQL_V3)):
        s = _build(sql)
        arms[name] = s
        print(f"  {name}: {len(s)} 天, 年化 {_annualized(s):.2f}pp")

    v0 = arms["V0_guarded"]
    v1 = arms["V1_noguard"]
    v2 = arms["V2_rule60"]
    v3 = arms["V3_rule60_st"]
    payload = {
        "ran_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "elapsed_sec": round(time.time() - t0, 1),
        "blocked_rows": br,
        "annualized_pp": {k: round(_annualized(v), 4) for k, v in arms.items()},
        "monthly_bias_pp": {
            "V0_minus_V1": round(_monthly_bias_pp(v0, v1), 4),
            "V0_minus_V2": round(_monthly_bias_pp(v0, v2), 4),
            "V0_minus_V3": round(_monthly_bias_pp(v0, v3), 4),
            "V1_minus_V2": round(_monthly_bias_pp(v1, v2), 4),
            "V2_minus_V3": round(_monthly_bias_pp(v2, v3), 4),
        },
        "regime_flip": {
            "V0_vs_V1": _flip(v0, v1),
            "V0_vs_V2": _flip(v0, v2),
            "V0_vs_V3": _flip(v0, v3),
        },
        # 六层里每层是否真的搬进了尺 —— 没搬的必须写出来, 别让人以为"同口径"
        "domain_layers": {
            "①上市满60交易日": {"in": ["V2", "V3"], "note": "与生产 _first_trade=max(首现日,list_date) 同口径"},
            "②不停牌(vol>0)": {"in": ["V0", "V1", "V2", "V3"], "note": "四臂统一用 amount>0 近似(与 pool_index 原口径一致)"},
            "③非ST/退": {"in": ["V3"], "note": "V2 未实现 ⇒ V3 才覆盖; 按 namechange 时点名区间"},
            "④非北交/科创": {"in": ["V0", "V1", "V2", "V3"], "note": "_BASE_WHERE 已剔 688/689/8/4/9"},
            "⑤市值下界": {"in": [], "note": "生产 stock_size_pctl 未配置 ⇒ 阈值 0 ⇒ 实际只剔 total_mv 为 NaN 者, 与①高度重叠(上市初期本无数据) ⇒ 本轮不实现"},
            "⑥流动性下界": {"in": [], "note": "生产 stock_min_amt20 未配置 ⇒ 阈值 0 ⇒ 同理只剔 NaN, 与①重叠 ⇒ 本轮不实现"},
            "_结论": "V3 仍未覆盖⑤⑥(NaN 层), 故 V3 也不是'完整六层'; 但⑤⑥与①重叠度高, 影响应远小于③",
        },
    }

    print("\n== 结果 ==")
    print(f"  月均偏差 V0−V1 : {payload['monthly_bias_pp']['V0_minus_V1']:+.4f} pp/月"
          "（对照 M4 的 −0.3862）")
    print(f"  月均偏差 V0−V2 : {payload['monthly_bias_pp']['V0_minus_V2']:+.4f} pp/月"
          " ← 剔新股首日后")
    print(f"  月均偏差 V0−V3 : {payload['monthly_bias_pp']['V0_minus_V3']:+.4f} pp/月"
          " ← 再剔 ST/退（**最贴近策略可得域的一把**）")
    print(f"  月均偏差 V2−V3 : {payload['monthly_bias_pp']['V2_minus_V3']:+.4f} pp/月"
          " ← ST 层单独的贡献")
    for k, v in payload["regime_flip"].items():
        print(f"  {k} 翻档: {v.get('flip_days')}/{v.get('state_days')} "
              f"= {v.get('flip_rate')}")

    payload["verdict"] = (
        "V1（无护栏）含新股首日等策略不可获得收益 ⇒ 不是正确参照尺，"
        "8.57% 与 −0.3862pp/月 是'两把尺口径不同'的产物，不是现行尺的真实偏差。"
        "真实偏差看 V0−V3（V2 剔新股首日、V3 再剔 ST/退；⑤⑥两层未实现但只剔 NaN、"
        "与①重叠，见 domain_layers）。若 |V0−V3| 显著小于 |V0−V1| ⇒ 现行尺问题被"
        "高估，换尺优先级下调；反之按 |V0−V3| 的量级决定是否立预注册执行。"
    )
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print("产物:", OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
