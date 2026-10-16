"""F26 · 业绩预告（事件族）的周频 IC 体检 —— 预注册

## 为什么做这一件

F3（2026-09-11）把库里躺着的 5 个**面板型**新数据族（资金流/两融/大宗/股东户数/北向）
测否了：50 个因子、**0 个通过**，并明确登记「月频/更长持有期尚未测试，不算证伪」。
本实验测的是**从未入库的第 6 族**：**业绩预告**（事件流，2013Q1 起，12.1 万行、5600 只，
东财/akshare 拉取，落表 `forecast`）。

## 为什么这一族与前 5 族**事前**不同（不是事后找补）

- 前 5 族是**存量/流量读数的代理**，与价格同源、彼此高度相关（F3 聚类后 33 个独立检验）；
- 业绩预告是**基本面事件的首次披露**，`ann_date` 是真正的时点（不是报告期末），PIT 干净，
  是 PEAD 类文献里最常被复现的一类漂移来源；
- 但它有明确的两面性：**要不要预告由业绩触发**（强制预告偏坏消息）⇒ 覆盖子集是
  "业绩选择后"的样本，见下面「必须如实声明」。

## 三个被测因子（方向**事前冻结**：好消息 → 正超额）

| 因子 | 定义 | 覆盖 |
|---|---|---|
| `fc_chg` | 最近一次**净利润**预告的业绩变动幅度中值（%），截尾 [−100, +300] | 仅已披露子集 |
| `fc_type` | 预告类型序数分：预增/扭亏 +2，略增/续盈/减亏 +1，不确定 0，略减/续亏 −1，预减/首亏/增亏 −2 | 仅已披露子集 |
| `fc_cover90` | 最近 **90 天**内是否发过预告（0/1，**全市场有值**）—— 检验"披露行为本身"是否含信息 | 全市场 |

陈旧规则（事前冻结）：只用 `ann_date ≤ 锚点` 且距锚点 ≤ **180 天**的记录；
同股同日多口径时优先「归属于上市公司股东的净利润」；同股多期则取 `ann_date` **最晚**的一条
（= 市场当时真正看到的那一条；更正公告也会以它自己的 ann_date 进入）。

## 预注册判据（与 F3 逐字同谓词，分母更严）

每个因子按 F1b 的 `q1_pass`：
  死区/活跃区 IC **同号** + `|IC_死区| ≥ 0.02` + HAC t ≥ 2.0 + 聚类校正 p < 0.05。
**分母** = `max(F3 已花掉的 33 个独立检验, F26 自身完全链接聚类数)` ——
新家族必须在"研究预算已经花掉 33 次"的前提下活下来（等价于实际要求 t ≳ 3.2）。

判决：**≥2 个因子通过 ⇒ 该族进入建 die 流程（F27）；否则写入证伪登记**。

## 必须如实声明

- **覆盖面**：已披露子集只占票池的一部分，且与业绩好坏相关（强制预告偏坏消息）⇒
  IC 只在**子集内**计算；即便通过，它也只能当"倾斜"用，不能当全市场排序
  （把没预告的填 0 = 把坏消息股当中性股，是 hk_hold 那类"含义静默改变"的翻版）；
- 拉取层只存**原始事实**（类型/幅度/数值/公告日），东财表里的
  `解禁后20日涨跌幅` 这类未来字段不入库、不参与因子；
- 本脚本只算不写生产表（与 F3 同约定）。

用法：
    ./.venv/Scripts/python.exe -X utf8 experiments/05_label/F26_forecast_weekly_ic.py
"""
import hashlib
import importlib.util
import json
import logging
import math
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
BASE = HERE.parent.parent
sys.path.insert(0, str(BASE))


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


f1 = _load("f1", HERE / "F1_weekly_ic_decay.py")
f1b = _load("f1b", HERE / "F1b_weekly_open_overlap.py")

from app import config as cfg_mod             # noqa: E402
from app import db                            # noqa: E402
from app.factors import factor_engine as fe   # noqa: E402
from app.pit.engine import PITEngine          # noqa: E402

log = logging.getLogger("f26")
OUT = HERE / "F26_forecast_weekly_ic_result.json"
STALE_DAYS = 180          # 预告的"保鲜期": 超过就不再用(事前冻结)
COVER_DAYS = 90           # fc_cover90 的回看窗
CHG_CLIP = (-100.0, 300.0)
TYPE_SCORE = {"预增": 2.0, "扭亏": 2.0, "略增": 1.0, "续盈": 1.0, "减亏": 1.0,
              "不确定": 0.0, "略减": -1.0, "续亏": -1.0, "预减": -2.0, "首亏": -2.0,
              "增亏": -2.0}
MAIN_METRIC = "归属于上市公司股东的净利润"
F3_DENOM = 33             # F3 已花掉的独立检验数(来自 F3 结果 JSON)


def build_panels(conn, anchors: pd.DatetimeIndex, cols) -> dict:
    """三个因子面板（anchors × cols），PIT: 只用 ann_date ≤ 锚点的记录。"""
    fc = pd.read_sql("SELECT ts_code, end_date, ann_date, metric, fc_type, chg_mid "
                     "FROM forecast", conn)
    fc["ann"] = pd.to_datetime(fc["ann_date"])
    fc["tscore"] = fc["fc_type"].map(TYPE_SCORE)
    fc["prio"] = (fc["metric"] == MAIN_METRIC).astype(int)      # 同日同股: 净利润口径优先
    fc = fc.sort_values(["ann", "prio"]).reset_index(drop=True)
    ann_np = fc["ann"].to_numpy()
    ci = pd.Index(cols)
    n, k = len(anchors), len(cols)
    out = {name: np.full((n, k), np.nan, dtype=np.float64)
           for name in ("fc_chg", "fc_type", "fc_cover90")}
    # fc_cover90 是**全市场**的 0/1（"最近 90 天有没有发预告"）—— 没发就是 0，
    # 不是 NaN（留 NaN 会让整列只剩 1.0 一种取值 ⇒ 零方差、IC 全 NaN、因子被静默跳过）
    out["fc_cover90"] = np.zeros((n, k), dtype=np.float64)
    for i, t in enumerate(anchors):
        lo = int(np.searchsorted(ann_np, np.datetime64(t - pd.Timedelta(days=STALE_DAYS)), "left"))
        hi = int(np.searchsorted(ann_np, np.datetime64(t), "right"))
        if hi > lo:
            sub = fc.iloc[lo:hi]
            last = sub.groupby("ts_code", sort=False).tail(1)     # 已按 ann 升序 ⇒ 最晚一条
            idx = ci.get_indexer(last["ts_code"].to_numpy())
            ok = idx >= 0
            idx = idx[ok]
            out["fc_chg"][i, idx] = np.clip(
                pd.to_numeric(last["chg_mid"], errors="coerce").to_numpy()[ok],
                *CHG_CLIP)
            out["fc_type"][i, idx] = last["tscore"].to_numpy()[ok]
        lo2 = int(np.searchsorted(ann_np, np.datetime64(t - pd.Timedelta(days=COVER_DAYS)), "left"))
        if hi > lo2:
            codes = fc.iloc[lo2:hi]["ts_code"].unique()
            j = ci.get_indexer(codes)
            out["fc_cover90"][i, j[j >= 0]] = 1.0                 # 全市场有值(0/1)
    return {name: pd.DataFrame(v, index=anchors, columns=cols) for name, v in out.items()}


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        datefmt="%H:%M:%S", force=True)
    t0 = time.time()
    cfg = cfg_mod.load_config()
    m = fe.load_market(since=f1b.SINCE)
    dates = pd.to_datetime(m["close"].index)
    wk = f1.week_end_dates(dates)
    pit = PITEngine(m=m, cfg=cfg)
    cols = m["close"].columns
    mask = f1.tradable_frame(pit, wk, cols)
    log.info("交易日 %d | 周锚点 %d | 可交易均值 %.0f", len(dates), len(wk),
             float(mask.sum(axis=1).mean()))

    conn = db.get_conn()
    try:
        panels_raw = build_panels(conn, wk, cols)
        ind = fe.industry_map(conn)
    finally:
        conn.close()
    cov = {k: float(v.notna().to_numpy().mean()) for k, v in panels_raw.items()}
    log.info("因子面板 %d 个 | 平均覆盖 %s", len(panels_raw),
             {k: round(100 * x, 1) for k, x in cov.items()})
    panels = {k: fe.neutralize(v, ind) for k, v in panels_raw.items()}

    lab = f1b.forward_excess_open(m, mask, 5)
    Yr = f1._rank(lab)
    ok_lab = lab.notna().to_numpy()
    lag = int(math.ceil(5 / 5)) + 1

    ic_all, stats = {}, {}
    for name, panel in panels.items():
        Xr = f1._rank(panel.reindex(index=wk))
        ic = pd.Series(f1.row_ic(Xr, Yr, ok_lab & np.isfinite(Xr)), index=wk).dropna()
        if len(ic) < 50:
            log.warning("%s 有效锚点不足(%d), 跳过", name, len(ic))
            continue
        ic_all[name] = ic
        stats[name] = {z: f1b.zone_stat_hac(ic, z, lag) for z in ("DEAD", "ACTIVE")}
        stats[name]["n_ic"] = int(len(ic))
        stats[name]["coverage"] = round(cov[name], 4)

    ic_mat = pd.DataFrame(ic_all)
    n_self = len(set(f1b._linkage_clusters(ic_mat, f1b.CLUSTER_RHO, complete=True).values()))
    n_denom = max(F3_DENOM, n_self)
    log.info("被测因子 %d | 自身聚类 %d | 分母取 max(%d, %d)=%d",
             len(ic_all), n_self, F3_DENOM, n_self, n_denom)

    passed = [n for n, z in stats.items() if f1b.q1_pass(z["DEAD"], z["ACTIVE"], n_denom)]
    res = {"script_sha": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:16],
           "since": f1b.SINCE, "horizon": 5, "n_anchor": int(len(wk)),
           "n_tested": len(ic_all), "n_denom_clusters_used": n_denom,
           "coverage": {k: round(v, 4) for k, v in cov.items()},
           "passed": sorted(passed), "stats": stats,
           "verdict": {"forecast_route": bool(len(passed) >= 2), "n_passed": len(passed)},
           "secs": round(time.time() - t0, 1)}
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    for n in sorted(stats):
        d, a = stats[n]["DEAD"], stats[n]["ACTIVE"]
        log.info("[%s] 覆盖 %.1f%% | 死区 IC %s (t=%s, n=%s) | 活跃区 IC %s (t=%s, n=%s) | 通过=%s",
                 n, 100 * stats[n]["coverage"], d["ic"], d["t"], d["n"], a["ic"], a["t"], a["n"],
                 n in passed)
    log.info("== 判决: 业绩预告路线成立=%s (通过 %d/%d) | %.1fs → %s ==",
             res["verdict"]["forecast_route"], len(passed), len(ic_all), res["secs"], OUT)


if __name__ == "__main__":
    main()
