"""F3 · 未接线数据家族的**周频**IC 体检（死区/活跃区）—— 预注册

## 为什么做这个

F1/F1b/F2 的结论：用**现有 31 个因子**，周频毛超额上限 +8.45pp，
而"周频净超额 ≥ +10pp"需要毛 ≥ 16pp（20 万账户）⇒ **缺口 2 倍，且缺口在信号层**。

数据库里躺着一批**已探明可拉、已入库、但从未接线进生产因子表**的数据
（`factor_engine.compute_all(include_new=False)` 是不变量，不是疏漏）：

    moneyflow      12,515,913 行  2013-01~2026-09   个股资金流(大/中/小单)
    margin          6,713,030 行  2013-01~2026-09   融资融券明细
    block_trade       351,626 行  2013-01~2026-09   大宗交易
    holder_num        190,622 行  2016-03~2026-06   股东户数(带 ann_date)
    hk_hold         5,592,714 行  2017-03~2026-09   沪深股通持股(⚠️2024-08 覆盖断崖, 只作研究臂)

它们是当前**唯一未被证伪**的 alpha 来源。本脚本在同一套口径下（周锚点 + PIT 票池 +
可交易窗口标签 + HAC t + 聚类校正）测它们有没有**独立**的周频预测力。

## 预注册判据（跑前冻结）

① 每个新因子按 F1b 的 `q1_pass` 谓词判：死区/活跃区同号 + |IC_dead| ≥ 0.02 + HAC t ≥ 2.0
   + 聚类校正 p < 0.05（分母 = 全体被测因子的**完全链接聚类数**）。
② 判"独立": 与已通过的基础因子（rev_5/illiq/bp/dv_ttm）的**周频 IC 时序相关** |ρ| < 0.5,
   否则视为同一信号的变体，不算增量。
③ 判决：
   - 有 ≥3 个新因子通过 ① 且其中 ≥2 个满足 ② ⇒ **新数据路线成立**，进入建 die 流程；
   - 全部不通过 ⇒ 写入证伪登记处，周频路线的"信号层"缺口无法用这批数据补上。

## 口径偏差（如实声明）

- 与 F1b 相同：中性化只对锚点行做；财务/事件类按公告日对齐；票池 = 生产 PIT 六层。
- `hk_*` 只作**研究臂**（2024-08 覆盖度断崖 = 含义静默改变，方案 §3 不变量 #6）。
- 新因子**不写生产表**（本脚本只算不存）。

用法：
    ./.venv/Scripts/python.exe -X utf8 experiments/05_label/F3_newdata_weekly_ic.py
"""
import argparse
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

from app import config as cfg_mod          # noqa: E402
from app import db                         # noqa: E402
from app.factors import factor_engine as fe  # noqa: E402
from app.factors import flow_factors, holder_factors  # noqa: E402
from app.pit.engine import PITEngine       # noqa: E402

log = logging.getLogger("f3")

# 2026-09-13: 这个脚本带 `--horizon`，但结果文件名**曾经写死** ⇒ 用 21 日跑一次就把
# 周频存档**覆盖**了（文件名写着 weekly，内容却是 horizon=21）—— 归档缺陷，改名分开：
#   h=5  → 沿用历史文件名(所有旧引用都指着它, 且 git 里那份周频记录必须能对上)
#   h≠5 → F3_newdata_ic_h{H}_result.json
OUT = HERE / "F3_newdata_weekly_ic_result.json"
BASE_PASS = ["rev_5", "illiq", "bp", "dv_ttm"]   # F1/F1b 已通过预注册判据的基础因子
INDEP_RHO = 0.5


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default=f1b.SINCE)
    ap.add_argument("--horizon", type=int, default=5)
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        datefmt="%H:%M:%S", force=True)
    t0 = time.time()
    cfg = cfg_mod.load_config()

    m = fe.load_market(since=a.since)
    dates = pd.to_datetime(m["close"].index)
    wk = f1.week_end_dates(dates)
    pit = PITEngine(m=m, cfg=cfg)
    cols = m["close"].columns
    mask = f1.tradable_frame(pit, wk, cols)
    log.info("交易日 %d | 周锚点 %d | 可交易均值 %.0f", len(dates), len(wk),
             float(mask.sum(axis=1).mean()))

    base = f1.build_factors(m, wk)
    log.info("基础因子 %d 个", len(base))

    log.info("计算资金流/融资/大宗因子…")
    t1 = time.time()
    new = dict(flow_factors.compute_flow_factors(m, dates, include_research=False))
    log.info("  资金流 %d 个 (%.1fs)", len(new), time.time() - t1)
    t1 = time.time()
    hk = dict(flow_factors.compute_flow_factors(m, dates, include_research=True))
    hk_only = {k: v for k, v in hk.items() if k not in new}
    log.info("  研究臂 hk_* %d 个 (%.1fs)", len(hk_only), time.time() - t1)
    t1 = time.time()
    hold = dict(holder_factors.compute_holder_factors_safe(dates))
    log.info("  股东户数 %d 个 (%.1fs)", len(hold), time.time() - t1)

    conn = db.get_conn()
    try:
        ind = fe.industry_map(conn)
    finally:
        conn.close()

    panels = {}
    for name, df in new.items():
        panels[name] = fe.neutralize(df.reindex(index=wk, columns=cols), ind)
    for name, df in hold.items():
        panels[name] = fe.neutralize(df.reindex(index=wk, columns=cols), ind)
    hk_panels = {}
    for name, df in hk_only.items():
        hk_panels[name] = fe.neutralize(df.reindex(index=wk, columns=cols), ind)
    log.info("新因子面板 %d 个 (+研究臂 %d)", len(panels), len(hk_panels))

    lab = f1b.forward_excess_open(m, mask, a.horizon)
    Yr = f1._rank(lab)
    ok_lab = lab.notna().to_numpy()
    lag = int(math.ceil(a.horizon / 5)) + 1

    def ic_of(panel):
        Xr = f1._rank(panel.reindex(index=wk))
        return pd.Series(f1.row_ic(Xr, Yr, ok_lab & np.isfinite(Xr)), index=wk).dropna()

    ic_all, stats = {}, {}
    for name, panel in {**base, **panels, **hk_panels}.items():
        ic = ic_of(panel)
        if len(ic) < 50:
            continue
        ic_all[name] = ic
        stats[name] = {z: f1b.zone_stat_hac(ic, z, lag) for z in ("DEAD", "ACTIVE")}

    ic_mat = pd.DataFrame(ic_all)
    cl_single = f1b._linkage_clusters(ic_mat, f1b.CLUSTER_RHO, complete=False)
    cl_comp = f1b._linkage_clusters(ic_mat, f1b.CLUSTER_RHO, complete=True)
    n_denom = max(len(set(cl_single.values())), len(set(cl_comp.values())))
    log.info("被测因子 %d 个 | 聚类 单链 %d / 全链 %d ⇒ 分母 %d",
             len(ic_all), len(set(cl_single.values())), len(set(cl_comp.values())), n_denom)

    passed = [n for n, z in stats.items()
              if f1b.q1_pass(z["DEAD"], z["ACTIVE"], n_denom)]
    new_passed = [n for n in passed if n not in base]
    # 独立性: 与基础通过因子的 IC 时序相关
    indep = []
    for n in new_passed:
        rhos = []
        for b in BASE_PASS:
            if b in ic_all:
                r = ic_all[n].corr(ic_all[b])
                if np.isfinite(r):
                    rhos.append(abs(float(r)))
        stats[n]["max_rho_vs_base"] = round(max(rhos), 3) if rhos else None
        if rhos and max(rhos) < INDEP_RHO:
            indep.append(n)

    res = {"script_sha": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:16],
           "since": a.since, "horizon": a.horizon, "n_anchor": int(len(wk)),
           "n_tested": len(ic_all), "n_denom_clusters": n_denom,
           "passed_all": sorted(passed), "passed_new": sorted(new_passed),
           "independent_new": sorted(indep),
           "stats": stats, "secs": round(time.time() - t0, 1)}
    res["verdict"] = {"new_data_route": bool(len(new_passed) >= 3 and len(indep) >= 2),
                      "n_new_passed": len(new_passed), "n_independent": len(indep)}
    out_path = OUT if a.horizon == 5 else HERE / f"F3_newdata_ic_h{a.horizon}_result.json"
    out_path.write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str), encoding="utf-8")

    log.info("== 通过判据的因子(%d): %s", len(passed), sorted(passed))
    log.info("   其中新数据家族: %s", sorted(new_passed))
    log.info("   独立(与基础因子 |ρ|<0.5): %s", sorted(indep))
    log.info("   判决: 新数据路线成立 = %s | 完成 %.1fs → %s",
                 res["verdict"]["new_data_route"], res["secs"], out_path)
    for n in sorted(new_passed) + sorted(hk_panels):
        if n in stats:
            log.info("   %-22s DEAD %s | ACTIVE %s | rho_base %s", n, stats[n]["DEAD"],
                     stats[n]["ACTIVE"], stats[n].get("max_rho_vs_base"))


if __name__ == "__main__":
    main()
