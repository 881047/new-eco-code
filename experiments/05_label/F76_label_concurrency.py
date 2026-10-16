"""F76 标签并发诊断：月频 21 日标签到底有没有"重叠"可施唯一性权重（2026-09-19）

**为什么是它**：方案 P0-1 / KB 27 §五 说"本项目没有显式处理重叠权重"。但月频生产
trainer 的锚点是**月末交易日**（`month_end_dates`）、HORIZON=21，锚点间距 ≈20~23 个
交易日 ⇒ 标签窗的重叠可能本来就 ≈0 —— 真若如此，López de Prado 的唯一性权重没有
素材，P0-1 的 A/B（全量重训 × 4 种子）就是白烧机时。**先量，再决定烧不烧**。

**预注册（跑数前冻结，写在 plans/p0-validation-upgrade-20260919.md Phase A）**

  口径    生产 trainer 同源：`load_market()` 的交易日历 → `month_end_dates(dates,
          today)` 过滤 ≥2014-01-01（`_apply_train_start` 生产缺省 None，不施加）。
          标签窗 = 锚点之后**严格大于锚点**的 21 个交易日（与 `forward_return`
          的 open(t+1) 起 21 日一致）。
  并发    c(d) = 覆盖交易日 d 的在窗标签个数；c̄ = 覆盖天内 c 的均值（分年度也报）。
  唯一性  u_i = 标签窗内 1/c(d) 的均值（López de Prado 平均唯一性；u=1 即无重叠）。
  周频    对照组：周锚点 ≈ 每 ISO 周首个交易日（近似口径，脚本内注明；正式周调度
          锚点由 scheduler 生成，此处只回答"量级"问题），HORIZON=5 同口径。
  判据    月频 c̄ ≤ **1.05** ⇒ P0-1 以本诊断**结清**（唯一性权重无素材，不改
          trainer；27 号 §五 更正为"已量，并发≈1"）；
          c̄ > 1.05 ⇒ 进 A/B（trainer 加 sample_weight_mode 实验参数，默认 None，
          双种子 + 组合层两轴 + 噪声门）。
  不做    不重训、不改生产文件、不写任何 DB 表；产物只有本目录两个文件。

运行：``.venv\\Scripts\\python.exe -X utf8 experiments\\05_label\\F76_label_concurrency.py``
"""
import datetime
import json
import logging
import sys
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE))

from app.factors.factor_engine import load_market, month_end_dates      # noqa: E402

HERE = Path(__file__).resolve().parent
OUT = HERE / "F76_label_concurrency_result.json"
log = logging.getLogger("f76")

HORIZON_M = 21           # 月频生产标签（trainer.HORIZON）
HORIZON_W = 5            # 周频袖套标签（app/weekly/scores.HORIZON）
START = pd.Timestamp("2014-01-01")   # 与生产 md_all 过滤一致
GATE = 1.05              # 预注册决策门：月频 c̄ ≤ 1.05 ⇒ 结清


def concurrency_stats(anchors: pd.DatetimeIndex, cal: pd.DatetimeIndex,
                      horizon: int) -> dict:
    """锚点标签窗的逐日并发 c(d) 与逐样本平均唯一性 u_i。

    日历末端的"未来不存在"部分照实截断（最后一个锚点的 21 日窗大多落在日历外，
    生产里该锚点标签本来就是 NaN 不参与训练）。
    """
    pos = pd.Series(np.arange(len(cal)), index=cal)
    a_pos = pos.reindex(anchors).astype(int).to_numpy()
    n = len(cal)
    cover = np.zeros(n, dtype=int)            # 每个交易日被几个标签窗覆盖
    uniq = np.full(len(anchors), np.nan)      # 每个样本的平均唯一性
    spans = []
    for ap in a_pos:
        lo, hi = ap + 1, min(ap + horizon, n - 1)
        spans.append((lo, hi))
        if lo <= hi:                          # 整窗落在日历外
            cover[lo:hi + 1] += 1
    # 先全量累计 c 再取窗内均值(2026-09-19 同步修正: 初版"边累计边取"会让早锚点
    # 少算晚锚点的覆盖 ⇒ u_i 偏高; 生产 trainer 的 _uniqueness_weights_by_anchor
    # 用的就是这个正确顺序, 两处必须同构)
    for k, (lo, hi) in enumerate(spans):
        if lo <= hi:
            uniq[k] = float(np.mean(1.0 / cover[lo:hi + 1]))
    covered = cover > 0
    c_by_day = cover[covered]
    years = pd.Index(cal[covered]).year
    per_year = {int(y): round(float(c_by_day[years == y].mean()), 4)
                for y in sorted(pd.unique(years))}
    u = uniq[np.isfinite(uniq)]
    return {
        "n_anchors": int(len(anchors)),
        "n_days_with_any_label": int(covered.sum()),
        "cover_mean": round(float(c_by_day.mean()), 4),
        "cover_max": int(c_by_day.max()),
        "cover_p90": int(np.percentile(c_by_day, 90)),
        "per_year_cover_mean": per_year,
        "uniqueness_mean": round(float(u.mean()), 4),
        "uniqueness_p10": round(float(np.percentile(u, 10)), 4),
        "uniqueness_min": round(float(u.min()), 4),
    }


def weekly_anchors(cal: pd.DatetimeIndex) -> pd.DatetimeIndex:
    """近似口径：每 ISO 周首个交易日。正式调度锚点由 scheduler 生成，本诊断只
    回答量级（周频标签重叠的结构性结论不依赖 ±1 天的锚点选择）。"""
    s = pd.Series(cal, index=cal)
    iso = cal.isocalendar()
    key = (iso["year"].astype("int64") * 100 + iso["week"].astype("int64")).to_numpy()
    first = s.groupby(key).min()
    return pd.DatetimeIndex(pd.to_datetime(first.values))


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        datefmt="%H:%M:%S", force=True)
    m = load_market()
    cal = pd.DatetimeIndex(pd.to_datetime(m["close"].index))
    log.info("交易日历 %d 天（%s ~ %s）", len(cal), cal[0].date(), cal[-1].date())

    md_all = month_end_dates(cal, today=datetime.date.today())
    md_all = md_all[md_all >= START]
    log.info("月频锚点 %d 个（%s ~ %s）", len(md_all), md_all[0].date(), md_all[-1].date())

    wa = weekly_anchors(cal)
    wa = wa[wa >= START]
    log.info("周频锚点（近似）%d 个（%s ~ %s）", len(wa), wa[0].date(), wa[-1].date())

    monthly = concurrency_stats(md_all, cal, HORIZON_M)
    weekly = concurrency_stats(wa, cal, HORIZON_W)
    log.info("月频: c̄=%.4f max=%d 唯一性均值=%.4f p10=%.4f",
             monthly["cover_mean"], monthly["cover_max"],
             monthly["uniqueness_mean"], monthly["uniqueness_p10"])
    log.info("周频: c̄=%.4f max=%d 唯一性均值=%.4f p10=%.4f",
             weekly["cover_mean"], weekly["cover_max"],
             weekly["uniqueness_mean"], weekly["uniqueness_p10"])

    verdict = ("close_with_diagnostic" if monthly["cover_mean"] <= GATE
               else "ab_with_uniqueness_weights")
    out = {
        "date": "2026-09-19", "horizon_monthly": HORIZON_M,
        "horizon_weekly": HORIZON_W, "gate_cbar_max": GATE,
        "monthly": monthly, "weekly_approx": weekly,
        "decision_rule": "cbar<=1.05 close / >1.05 A/B（plans/p0-validation-upgrade-20260919.md Phase A）",
        "verdict": verdict,
    }
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("判决: %s ⇒ %s", verdict, OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
