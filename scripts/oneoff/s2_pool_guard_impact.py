"""S2 量化：`pool_index` 的 `ABS(pct_chg)<=21` 护栏对 regime 状态的影响（**只读**）。

## 背景（14 号台账开放项 S2）
`app/models/pool_index.py` L53 的 SQL 带 `ABS(pct_chg) <= 21` 护栏。
2026-09-20 已登记：除已知的"退市深跌被挡（深跌半边）"外，
**上行半边**也会被挡（1692 行 >+21%，主要新股首日）⇒ 池指数**系统性偏低** ≈0.3862pp/月。
2026-09-21 只读定性进一步发现：偏差是**累积**的 ⇒ 在相对比较中不完全抵消，
`regime.py` 侧推算约 **2.3% 相对偏差 ⇒ 系统性偏防守**（见台账 S2 行）。

## 本脚本要量化的
`pool_index` 是 `regime._pool_state_frame` 的输入（风格防守）。
问：**若把护栏拿掉（即修正这个偏低），pool_state 会翻档多少？**

## 方法（两臂对照）
  A 臂 = 现行（有护栏 `ABS(pct_chg)<=21`）
  B 臂 = 无护栏（去掉该条件）
  两臂用**同一个** `regime._pool_state_frame` 计算（**复用而非重写**，避免口径漂移）；
  统计：翻档天数占比、逐档变化、被护栏过滤掉的行数、极端值情况。

## 只读承诺
不写库、不改 `pool_index` 表、不调 `build_pool_index()`。产物只落 `experiments/`。
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

# 与 app/models/pool_index.py:_SQL 同口径，仅护栏条件不同
_SQL_TMPL = """
WITH path AS (
    SELECT trade_date,
           exp(SUM(LN(1 + pct_chg/100.0)) OVER (
               PARTITION BY ts_code, substr(trade_date,1,6) ORDER BY trade_date)) AS lvl
    FROM daily
    WHERE amount > 0 AND pct_chg IS NOT NULL {guard}
      AND ts_code NOT LIKE '688%' AND ts_code NOT LIKE '689%' AND ts_code NOT LIKE '8%'
      AND ts_code NOT LIKE '4%' AND ts_code NOT LIKE '9%'
)
SELECT trade_date, AVG(lvl) AS lvl, COUNT(*) AS n
FROM path GROUP BY trade_date ORDER BY trade_date
"""
SQL_GUARDED = _SQL_TMPL.format(guard="AND ABS(pct_chg) <= 21")
SQL_UNGUARDED = _SQL_TMPL.format(guard="")

BASE_LEVEL = 1000.0
OUT = Path(__file__).resolve().parents[2] / "experiments" / "s2_pool_guard_impact.json"


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
    factor = cum / month_end
    df["close"] = BASE_LEVEL * ym.map(factor) * df["lvl"]
    s = df.set_index("trade_date")["close"].astype(float)
    s.index = pd.to_datetime(s.index)
    return s.sort_index()


def main() -> int:
    t0 = time.time()
    print("== S2 量化：护栏对 regime 状态的影响（只读）==")

    a = _build(SQL_GUARDED)      # 现行
    b = _build(SQL_UNGUARDED)    # 无护栏
    if a.empty or b.empty:
        print("池指数为空 ⇒ 无法跑")
        return 2
    print(f"A 臂（有护栏）: {len(a)} 天")
    print(f"B 臂（无护栏）: {len(b)} 天")

    idx = a.index.intersection(b.index)
    a, b = a.loc[idx], b.loc[idx]
    print(f"共同交易日    : {len(idx)} 天")

    fa = _pool_state_frame(a)
    fb = _pool_state_frame(b)
    j = fa.index.intersection(fb.index)
    sa, sb = fa.loc[j, "pool_state"], fb.loc[j, "pool_state"]
    print(f"状态可比日    : {len(j)} 天（_pool_state_frame 需 ma250 预热）")

    diff = sa != sb
    n_flip = int(diff.sum())
    flip_rate = n_flip / len(j) if len(j) else None

    # 逐档分布
    va, vb = sa.value_counts().to_dict(), sb.value_counts().to_dict()
    # 被护栏过滤掉的行数（daily 层）
    conn = db.get_conn()
    try:
        n_all = pd.read_sql(
            "SELECT COUNT(*) c FROM daily WHERE amount>0 AND pct_chg IS NOT NULL "
            "AND ts_code NOT LIKE '688%' AND ts_code NOT LIKE '689%' "
            "AND ts_code NOT LIKE '8%' AND ts_code NOT LIKE '4%' AND ts_code NOT LIKE '9%'",
            conn).iloc[0]["c"]
        n_kept = pd.read_sql(
            "SELECT COUNT(*) c FROM daily WHERE amount>0 AND pct_chg IS NOT NULL "
            "AND ABS(pct_chg)<=21 AND ts_code NOT LIKE '688%' AND ts_code NOT LIKE '689%' "
            "AND ts_code NOT LIKE '8%' AND ts_code NOT LIKE '4%' AND ts_code NOT LIKE '9%'",
            conn).iloc[0]["c"]
    finally:
        conn.close()
    n_blocked = int(n_all) - int(n_kept)

    payload = {
        "ran_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "elapsed_sec": round(time.time() - t0, 1),
        "common_days": len(idx),
        "state_days": len(j),
        "flip_days": n_flip,
        "flip_rate": None if flip_rate is None else round(float(flip_rate), 6),
        "state_counts_guarded": {str(k): int(v) for k, v in va.items()},
        "state_counts_unguarded": {str(k): int(v) for k, v in vb.items()},
        "daily_rows_blocked_by_guard": n_blocked,
        "guard": "ABS(pct_chg) <= 21",
    }

    print("\n== 结果 ==")
    print(f"  被护栏挡掉的行数 : {n_blocked}")
    print(f"  状态翻档天数     : {n_flip} / {len(j)}")
    print(f"  **翻档率**       : {flip_rate}")
    print(f"  有护栏档位分布   : {va}")
    print(f"  无护栏档位分布   : {vb}")

    if flip_rate is None:
        payload["verdict"] = "未跑（无可比状态日）"
    elif flip_rate == 0:
        payload["verdict"] = "不翻档：护栏对状态零影响 ⇒ S2 可判不改（仅指数水平有偏）"
        print("\n⇒ 翻档率 0 ⇒ 护栏不影响状态，只影响指数水平 ⇒ S2 可判不改（在册读数不受影响）")
    else:
        payload["verdict"] = (f"翻档率 {flip_rate:.4%} ⇒ 护栏确实改变状态；"
                              "需按量级决定是否换尺（见 plans/m3-q2-stage-b-prereg 同类代价框架）")
        print(f"\n⇒ 翻档率 {flip_rate:.4%} ⇒ 护栏改变状态，需评估是否换尺")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print("产物:", OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
