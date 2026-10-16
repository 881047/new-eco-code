"""F35 · 第 9、10 个数据族：股票回购 + 增发（发行效应）—— 预注册

## 为什么是这两族

F3(5 族)/F26(业绩预告)/F27(解禁、增减持) 之后，免费可得、**从未接入**的事件族只剩这两个，
而它们的机制与前 8 族不同：

  · **股票回购**：公司真金白银买自家股票 ⇒ 供给减少 + 管理层信号（A 股 2018 年后规模爆发）；
  · **增发（SEO）**：发行效应（issuance effect）是文献里最稳的负向事件之一 —— 增发 = 供给增加 +
    公司认为股价不低。

**幸存者偏差体检（入库后实测）**：回购清单 5,508 行/2,934 只，其中**只有 2 只**不在 `stock_basic`
（该表含 289 只退市股）⇒ 事件清单**保留了退市公司的记录**，这一族没有"只留活到今天的公司"问题；
增发清单 5,834 行/3,023 只，0 只缺失。**这是本项目里少见的没有覆盖断崖的数据族。**

## 三个因子（方向事前冻结）

| 因子 | 定义 | 方向 |
|---|---|---|
| `rp_amt120` | 过去 **120 天**内启动的回购方案**计划金额上限之和** ÷ **锚点日流通市值** | 正向 |
| `rp_any60` | 过去 **60 天**内是否有回购方案启动（0/1） | 正向 |
| `seo_neg90` | 过去 **90 天**内是否有增发**发行**（有 = −1，无 = 0） | 负向 ⇒ 进闸时取 −1 |

PIT 口径（事前冻结）：
  · 回购用 `回购起始时间`（方案启动≈公告日）；**「已回购金额/数量」是当前快照的累计值，
    拿它回填历史就是前视 ⇒ 库里根本没存那两个字段**（见 `app/db.py` 的 DDL 注释）；
  · 增发用 `发行日期`（比"预案公告"晚 ⇒ **保守**，即效应若存在，这里只会更弱）；
  · 三因子都是**全市场有值**（0 = 没有事件），不把缺失当 0。

## 预注册判据（与 F3/F26/F27 逐字同谓词）

死区/活跃区 IC 同号 + `|IC_死区| ≥ 0.02` + HAC t ≥ 2.0 + 聚类校正 p < 0.05；
**分母 = max(33 + 3 + 2, F35 自身聚类数) = 38**（研究预算已经花掉 38 次独立检验）。

判决：≥2 个通过 ⇒ 进入建 die 流程；否则记入证伪登记，并宣布"免费可得的事件族已穷尽"。

用法：
    ./.venv/Scripts/python.exe -X utf8 experiments/05_label/F35_event3_weekly_ic.py
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

log = logging.getLogger("f35")
OUT = HERE / "F35_event3_weekly_ic_result.json"
RP_AMT_DAYS = 120
RP_ANY_DAYS = 60
SEO_DAYS = 90
DENOM = 33 + 3 + 2


def neutralize_sparse(df: pd.DataFrame, ind: pd.Series) -> pd.DataFrame:
    """**稀疏事件面板专用**中性化: 行业去均值 → z-score，**不做 2% 去极值**。

    ⚠️ 这是本轮踩到并修掉的真坑：生产用的 `fe.neutralize` 先做 **2%/98% 分位 winsorize**，
    而事件面板的非零占比只有 **1.4~2.7%** ⇒ 该行的 98% 分位正好是 **0** ⇒ `clip(lower=0, upper=0)`
    把**整行归零** ⇒ 再 z-score 变成全 NaN ⇒ IC **静默消失**。
    实测证据：首跑 `seo_neg90` 在活跃区 n=0（2024+ 有 557 条增发事件却一条 IC 都算不出），
    查下去发现 555 个锚点里只有 ~20% 的事件锚点还能算出 IC —— 共同点是非零占比刚好越过 2%。

    F26/F27 不受影响（业绩预告覆盖 38%、解禁 5.6%、增减持 8%，分位不为 0），已逐项核对。
    """
    x = df.replace([np.inf, -np.inf], np.nan)
    inds = ind.reindex(x.columns)
    x = x - x.T.groupby(inds, sort=False).transform("mean").T      # 行业去均值
    x = x.sub(x.mean(axis=1), axis=0).div(x.std(axis=1), axis=0)   # 截面 z-score
    return x.replace([np.inf, -np.inf], np.nan)


def build_panels(m: dict, anchors: pd.DatetimeIndex, cols) -> dict:
    conn = db.get_conn()
    try:
        rp = pd.read_sql("SELECT ts_code, start_date, amount_cap FROM repurchase", conn)
        seo = pd.read_sql("SELECT ts_code, issue_date FROM seo_issue", conn)
    finally:
        conn.close()
    rp["d"] = pd.to_datetime(rp["start_date"], format="%Y%m%d", errors="coerce")
    rp = rp.dropna(subset=["d"])
    seo["d"] = pd.to_datetime(seo["issue_date"], format="%Y%m%d", errors="coerce")
    seo = seo.dropna(subset=["d"])
    ci = pd.Index(cols)
    close = m["close"].reindex(columns=cols).to_numpy(np.float64)
    circ = m["circ_mv"].reindex(columns=cols).to_numpy(np.float64)     # 万元
    dates = pd.to_datetime(m["close"].index)
    dpos = {d: i for i, d in enumerate(dates)}
    n, k = len(anchors), len(cols)
    out = {name: np.zeros((n, k), dtype=np.float64)
           for name in ("rp_amt120", "rp_any60", "seo_neg90")}
    for i, t in enumerate(anchors):
        e = dpos.get(pd.Timestamp(t))
        if e is None:
            continue
        float_mv = np.where(np.isfinite(circ[e]) & (circ[e] > 0), circ[e], np.nan)
        win = rp[(rp["d"] > t - pd.Timedelta(days=RP_AMT_DAYS)) & (rp["d"] <= t)]
        if not win.empty:
            amt = win.groupby("ts_code")["amount_cap"].sum()
            j = ci.get_indexer(amt.index.to_numpy())
            ok = j >= 0
            with np.errstate(invalid="ignore", divide="ignore"):
                ratio = np.where(float_mv[j[ok]] > 0, amt.to_numpy()[ok] / float_mv[j[ok]], np.nan)
            out["rp_amt120"][i, j[ok]] = np.clip(ratio, 0, 0.30)      # 计划金额/流通市值, 截尾 30%
        win60 = rp[(rp["d"] > t - pd.Timedelta(days=RP_ANY_DAYS)) & (rp["d"] <= t)]
        if not win60.empty:
            j = ci.get_indexer(win60["ts_code"].unique())
            out["rp_any60"][i, j[j >= 0]] = 1.0
        win90 = seo[(seo["d"] > t - pd.Timedelta(days=SEO_DAYS)) & (seo["d"] <= t)]
        if not win90.empty:
            j = ci.get_indexer(win90["ts_code"].unique())
            out["seo_neg90"][i, j[j >= 0]] = -1.0                     # 冻结方向: 增发 = 负
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
    log.info("非零占比(有事件) %s", {k: round(100 * x, 2) for k, x in nz.items()})
    conn = db.get_conn()
    try:
        ind = fe.industry_map(conn)
    finally:
        conn.close()
    panels = {k: neutralize_sparse(v, ind) for k, v in panels_raw.items()}

    lab = f1b.forward_excess_open(m, mask, 5)
    Yr = f1._rank(lab)
    ok_lab = lab.notna().to_numpy()
    lag = 2
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
           "windows_days": {"rp_amt": RP_AMT_DAYS, "rp_any": RP_ANY_DAYS, "seo": SEO_DAYS},
           "nonzero_share": {k: round(v, 4) for k, v in nz.items()},
           "passed": sorted(passed), "stats": stats,
           "verdict": {"event3_route": bool(len(passed) >= 2), "n_passed": len(passed)},
           "secs": round(time.time() - t0, 1)}
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    for n in sorted(stats):
        d, a = stats[n]["DEAD"], stats[n]["ACTIVE"]
        log.info("[%s] 非零 %.2f%% | 死区 %s (t=%s) | 活跃区 %s (t=%s) | 通过=%s",
                 n, 100 * stats[n]["nonzero_share"], d["ic"], d["t"], a["ic"], a["t"],
                 n in passed)
    log.info("== 判决: 回购+增发路线成立=%s (通过 %d/%d) | %.1fs → %s ==",
             res["verdict"]["event3_route"], len(passed), len(ic_all), res["secs"], OUT)


if __name__ == "__main__":
    main()
