"""F27 · 事件族第二轮：限售解禁 + 董监高增减持 —— 预注册

## 为什么还有这一轮

F3（5 个面板族）与 F26（业绩预告）在**周频口径**上都是 0 通过。免费可得、且从未测过的
事件族只剩这两个，而它们的**机制与前 6 族不同**：

  · **限售解禁 = 事先已知的供给冲击**：日程在招股书/公告里提前几个月公开 ⇒ 与价格/资金流
    不同源，且是"可预见的卖压"而不是"已经发生的资金流"；
  · **董监高增减持 = 内部人交易**：经典的信息不对称信号，事件型而非存量读数。

## 两个因子（方向**事前冻结**）

| 因子 | 定义 | 方向 |
|---|---|---|
| `ul_press_60` | **未来 60 天**内解禁股数 ÷ **锚点当日**流通股本 | 负向（供给冲击 ⇒ 预期负超额）⇒ 进闸时取 **−压力** |
| `ins_net_60` | 最近 60 天董监高**净增持股数** ÷ 锚点当日流通股本 | 正向（内部人买入 ⇒ 预期正超额） |

口径细节（全部事前冻结）：
  · 能在锚点用"未来解禁日程"是**合法 PIT**（日程事先公开）；但**分母必须用锚点当日的
    流通股本**（`circ_mv×10000/close` 反算），不用上游的"占解禁前流通市值比例"——
    后者依赖解禁日前一交易日收盘价，解禁前用它是轻微前视；
  · 可见日近似：解禁日 − 90 天（接口不提供公告日 ⇒ 取保守值）；
  · 董监高：接口只给**变动日**，规则要求 2 个交易日内公告 ⇒ 可见日 = 变动日 + 3 个自然日；
  · 两因子都是**全市场有值**（0 = 没有解禁/没有净增持），不把缺失填 0 的做法仅适用于
    "缺失=选择效应"的场景（如业绩预告），这里 0 是真实含义。

## 预注册判据（与 F3/F26 逐字同谓词）

死区/活跃区 IC **同号** + `|IC_死区| ≥ 0.02` + HAC t ≥ 2.0 + 聚类校正 p < 0.05，
且方向必须是上表冻结的那个符号（负向因子进闸前取负号）。
**分母 = max(33 + 3, F27 自身聚类数) = 36** —— 研究预算已经花掉 36 次独立检验。

判决：**≥2 个通过 ⇒ 进入建 die 流程；否则记入证伪登记**。

用法：
    ./.venv/Scripts/python.exe -X utf8 experiments/05_label/F27_event2_weekly_ic.py
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

log = logging.getLogger("f27")
OUT = HERE / "F27_event2_weekly_ic_result.json"
HORIZON_DAYS = 60          # 解禁压力看的未来窗口
INSIDER_LOOKBACK = 60      # 内部人交易的回看窗
UL_VISIBLE_LAG = 90        # 解禁日程的可见日 = 解禁日 − 90 天（保守近似）
INSIDER_VISIBLE_LAG = 3    # 董监高变动 → 公告: 保守 3 个自然日
DENOM = 33 + 3             # F3 的 33 + F26 的 3 = 研究预算已花掉的独立检验数


def build_panels(m: dict, anchors: pd.DatetimeIndex, cols) -> dict:
    """两个因子面板（anchors × cols）。方向已按预注册冻结：`ul_*` 取负, `ins_*` 取正。"""
    conn = db.get_conn()
    try:
        ul = pd.read_sql("SELECT ts_code, release_date, release_shares FROM restricted_release",
                         conn)
        ins = pd.read_sql("SELECT ts_code, change_date, delta_shares FROM insider_trade", conn)
    finally:
        conn.close()
    ci = pd.Index(cols)
    n, k = len(anchors), len(cols)
    close = m["close"].reindex(columns=cols).to_numpy(np.float64)
    circ = m["circ_mv"].reindex(columns=cols).to_numpy(np.float64)
    dates = pd.to_datetime(m["close"].index)
    dpos = {d: i for i, d in enumerate(dates)}

    if not ul.empty:
        ul["rel"] = pd.to_datetime(ul["release_date"], format="%Y%m%d", errors="coerce")
        ul["vis"] = ul["rel"] - pd.Timedelta(days=UL_VISIBLE_LAG)
        ul = ul.dropna(subset=["rel", "vis"])
    if not ins.empty:
        ins["chg"] = pd.to_datetime(ins["change_date"], format="%Y%m%d", errors="coerce")
        ins["vis"] = ins["chg"] + pd.Timedelta(days=INSIDER_VISIBLE_LAG)
        ins = ins.dropna(subset=["chg", "vis"])

    out = {name: np.zeros((n, k), dtype=np.float64)
           for name in ("ul_press_60", "ins_net_60")}
    for i, t in enumerate(anchors):
        e = dpos.get(pd.Timestamp(t))
        if e is None:
            continue
        float_shares = np.where((close[e] > 0) & np.isfinite(circ[e]),
                                circ[e] * 10000.0 / np.where(close[e] > 0, close[e], np.nan),
                                np.nan)                       # circ_mv 单位: 万元
        if not ul.empty:
            win = ul[(ul["vis"] <= t) & (ul["rel"] > t)
                     & (ul["rel"] <= t + pd.Timedelta(days=HORIZON_DAYS))]
            if not win.empty:
                s = win.groupby("ts_code")["release_shares"].sum()
                j = ci.get_indexer(s.index.to_numpy())
                ok = j >= 0
                fs = float_shares[j[ok]]
                with np.errstate(invalid="ignore", divide="ignore"):
                    pressure = np.where(fs > 0, s.to_numpy()[ok] / fs, np.nan)
                out["ul_press_60"][i, j[ok]] = -np.clip(pressure, 0, 5.0)   # 冻结方向: 取负
        if not ins.empty:
            win = ins[(ins["vis"] <= t) & (ins["vis"] >= t - pd.Timedelta(days=INSIDER_LOOKBACK))]
            if not win.empty:
                s = win.groupby("ts_code")["delta_shares"].sum()
                j = ci.get_indexer(s.index.to_numpy())
                ok = j >= 0
                fs = float_shares[j[ok]]
                with np.errstate(invalid="ignore", divide="ignore"):
                    net = np.where(fs > 0, s.to_numpy()[ok] / fs, np.nan)
                out["ins_net_60"][i, j[ok]] = np.clip(net, -0.05, 0.05)     # 冻结方向: 取正
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

    panels_raw = build_panels(m, wk, cols)
    nz = {k: float((v.to_numpy() != 0).mean()) for k, v in panels_raw.items()}
    log.info("非零占比(有事件) %s", {k: round(100 * x, 1) for k, x in nz.items()})
    conn = db.get_conn()
    try:
        ind = fe.industry_map(conn)
    finally:
        conn.close()
    panels = {k: fe.neutralize(v, ind) for k, v in panels_raw.items()}

    lab = f1b.forward_excess_open(m, mask, 5)
    Yr = f1._rank(lab)
    ok_lab = lab.notna().to_numpy()
    lag = 2
    ic_all, stats = {}, {}
    for name, panel in panels.items():
        # 不做"非零才排序": 这两个因子的 0 是**真实含义**(没有解禁/没有净增持),
        # 要检验的正是"有事件 vs 无事件"的差别; 把 0 挖掉会把这件事整个抹掉。
        Xr = f1._rank(panel.reindex(index=wk))
        ic = pd.Series(f1.row_ic(Xr, Yr, ok_lab & np.isfinite(Xr)), index=wk).dropna()
        if len(ic) < 50:
            log.warning("%s 有效锚点不足(%d), 跳过", name, len(ic))
            continue
        ic_all[name] = ic
        stats[name] = {z: f1b.zone_stat_hac(ic, z, lag) for z in ("DEAD", "ACTIVE")}
        stats[name]["n_ic"] = int(len(ic))
        stats[name]["nonzero_share"] = round(nz[name], 4)

    n_self = len(set(f1b._linkage_clusters(pd.DataFrame(ic_all), f1b.CLUSTER_RHO,
                                           complete=True).values())) if ic_all else 0
    n_denom = max(DENOM, n_self)
    log.info("被测因子 %d | 自身聚类 %d | 分母取 max(%d, %d)=%d",
             len(ic_all), n_self, DENOM, n_self, n_denom)
    passed = [n for n, z in stats.items() if f1b.q1_pass(z["DEAD"], z["ACTIVE"], n_denom)]
    res = {"script_sha": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:16],
           "since": f1b.SINCE, "horizon": 5, "n_anchor": int(len(wk)),
           "n_tested": len(ic_all), "n_denom_clusters_used": n_denom,
           "nonzero_share": {k: round(v, 4) for k, v in nz.items()},
           "passed": sorted(passed), "stats": stats,
           "verdict": {"event2_route": bool(len(passed) >= 2), "n_passed": len(passed)},
           "secs": round(time.time() - t0, 1)}
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    for n in sorted(stats):
        d, a = stats[n]["DEAD"], stats[n]["ACTIVE"]
        log.info("[%s] 非零 %.1f%% | 死区 %s (t=%s) | 活跃区 %s (t=%s) | 通过=%s",
                 n, 100 * stats[n]["nonzero_share"], d["ic"], d["t"], a["ic"], a["t"],
                 n in passed)
    log.info("== 判决: 事件族第二轮成立=%s (通过 %d/%d) | %.1fs → %s ==",
             res["verdict"]["event2_route"], len(passed), len(ic_all), res["secs"], OUT)


if __name__ == "__main__":
    main()
