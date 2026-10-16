"""F42 · 事件族的**月频（21 日）窗口** —— 登记里明确写着"尚未测试"的那半边

## 为什么现在做这一件

用户的目标函数是"**月收益**尽可能超额"，而这个项目的证伪登记里，事件族那一栏写的是：

    F3（资金流/两融/大宗/股东户数/北向）: "周频路线不能靠这批数据补上；**月频/更长持有期尚未测试**，
                                          不算证伪"
    F26（业绩预告）:                        "周频口径否掉；**更长持有期（月频 PEAD）仍未测**，不算完全证伪"
    F27（解禁/增减持）/ F35（回购/增发）:     "周频口径否掉"（同样没测更长持有期）

**周频窗口对事件类信号本来就不公平**：业绩预告/解禁/增减持的信息衰减尺度是数周到数月（PEAD 的
经典峰值在 1~3 个月），5 个交易日只截到事件反应的第一段。所以"周频 IC 差 5~10 倍"**不能**推出
"月频也不行"。这一件就是去把这半边补上 —— 它直接对着用户的"月收益"。

## 预注册判据（跑数前冻结）

- 因子与处理**完全照搬各自的周频实验**（同一 `build_panels`、同样的行业中性化、同样的 rank IC、
  同样的 DEAD/ACTIVE 分区、同样的 HAC lag 规则 `ceil(H/5)+1`），**只改标签窗口 H**；
- 判据沿用同一套 `f1b.q1_pass`：两区同号 + |IC_DEAD| ≥ 0.02 + HAC t ≥ 2.0 + 聚类校正 p < 0.05，
  分母 N = max(25, 自身聚类数)；
- **对照**：同一脚本里把 H=5 也跑一遍，必须复现 F26/F27/F35 已记录的周频 IC/t（逐位或 1e-3 内）。
  对照复现不了 ⇒ 口径漂了，**H=21 的读数一律不得解读**；
- 候选门槛：`n_passed ≥ 2`（一个族至少两个因子通过）才算"这条路值得做组合臂"；
  通过的因子**不直接上线**，下一步必须是组合层两轴判决（超额 + MDD）。

## 诚实边界

- 21 日标签与前一个标签**重叠 4/5**（H=5 时重叠更甚）⇒ HAC lag 已按 `ceil(H/5)+1` 放宽，但
  "重叠样本"这件事本身不产生新信息量，别把 491 期当成 491 个独立观测；
- IC 是**秩相关**，不是钱。F40 刚教训过一次：秩相关显著 ≠ 组合能赚钱 ⇒ 通过也只是**入场券**；
- 免费数据族的覆盖面/口径没变，本实验不引入任何新数据源。

用法：
    ./.venv/Scripts/python.exe -X utf8 experiments/05_label/F42_event_monthly_horizon.py
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
f26 = _load("f26", HERE / "F26_forecast_weekly_ic.py")
f27 = _load("f27", HERE / "F27_event2_weekly_ic.py")
f35 = _load("f35", HERE / "F35_event3_weekly_ic.py")

from app import config as cfg_mod             # noqa: E402
from app import db                           # noqa: E402
from app.factors import factor_engine as fe   # noqa: E402
from app.pit.engine import PITEngine          # noqa: E402

log = logging.getLogger("f42")
OUT = HERE / "F42_event_monthly_horizon_result.json"
HORIZONS = (5, 21)          # 5 = 对照(必须复现周频读数), 21 = 本次要回答的月频
DENOM = 25                  # 与 F26/F27/F35 同一分母口径

# 三个族的因子来源: (族名, 模块, build_panels 是吃 m 还是吃 conn)
SOURCES = (
    ("forecast", f26, "conn"),
    ("event2(解禁/增减持)", f27, "m"),
    ("event3(回购/增发)", f35, "m"),
)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        datefmt="%H:%M:%S", force=True)
    t0 = time.time()
    cfg = cfg_mod.load_config()
    m = fe.load_market(since=f1b.SINCE)
    dates = pd.to_datetime(m["close"].index)
    wk = f1.week_end_dates(dates)
    cols = m["close"].columns
    pit = PITEngine(m=m, cfg=cfg)
    mask = f1.tradable_frame(pit, wk, cols)
    log.info("交易日 %d | 周锚点 %d | 可交易均值 %.0f", len(dates), len(wk),
             float(mask.sum(axis=1).mean()))
    conn = db.get_conn()
    try:
        ind = fe.industry_map(conn)
        raw = {}
        for tag, mod, how in SOURCES:
            panels = mod.build_panels(conn if how == "conn" else m, wk, cols)
            for k, v in panels.items():
                raw[f"{tag}:{k}"] = v
    finally:
        conn.close()
    cov = {k: float(v.notna().to_numpy().mean()) for k, v in raw.items()}
    log.info("因子面板 %d 个 | 平均覆盖 %s", len(raw),
             {k: round(100 * x, 1) for k, x in cov.items()})
    panels = {k: fe.neutralize(v, ind) for k, v in raw.items()}

    res = {"script_sha": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:16],
           "since": f1b.SINCE, "horizons": list(HORIZONS), "denom": DENOM,
           "sources": [s[0] for s in SOURCES], "by_horizon": {}, "secs": None}
    ic_by_h = {}
    for H in HORIZONS:
        lag = int(math.ceil(H / 5)) + 1
        lab = f1b.forward_excess_open(m, mask, H)
        Yr = f1._rank(lab)
        ok_lab = lab.notna().to_numpy()
        stats, ic_all = {}, {}
        for name, panel in panels.items():
            Xr = f1._rank(panel.reindex(index=wk))
            ic = pd.Series(f1.row_ic(Xr, Yr, ok_lab & np.isfinite(Xr)), index=wk).dropna()
            if len(ic) < 50:
                continue
            ic_all[name] = ic
            stats[name] = {z: f1b.zone_stat_hac(ic, z, lag) for z in ("DEAD", "ACTIVE")}
            stats[name]["n_ic"] = int(len(ic))
            stats[name]["coverage"] = round(cov[name], 4)
        ic_mat = pd.DataFrame(ic_all)
        n_self = len(set(f1b._linkage_clusters(ic_mat, f1b.CLUSTER_RHO, complete=True).values()))
        n_denom = max(DENOM, n_self)
        passed = sorted(n for n, z in stats.items()
                        if f1b.q1_pass(z["DEAD"], z["ACTIVE"], n_denom))
        ic_by_h[H] = stats
        res["by_horizon"][str(H)] = {
            "lag": lag, "n_tested": len(ic_all), "n_denom_used": n_denom,
            "passed": passed, "stats": stats,
            "verdict": {"route_worth_arm": bool(len(passed) >= 2), "n_passed": len(passed)}}
        log.info("== H=%d (lag=%d, 分母 %d): 被测 %d, 通过 %d ==", H, lag, n_denom,
                 len(ic_all), len(passed))
        for n in sorted(stats):
            d, a = stats[n]["DEAD"], stats[n]["ACTIVE"]
            log.info("   [%s] 死区 IC %s (t=%s) | 活跃区 IC %s (t=%s) | 通过=%s",
                     n, d["ic"], d["t"], a["ic"], a["t"], n in passed)

    # ---- 对照: H=5 必须复现 F26/F27/F35 已记录的周频读数(逐位或 1e-3), 否则满月频不许解读 ----
    ctrl, ctrl_ok = [], True
    for fname, mod in (("F26_forecast_weekly_ic_result.json", f26),
                       ("F27_event2_weekly_ic_result.json", f27),
                       ("F35_event3_weekly_ic_result.json", f35)):
        p = HERE / fname
        if not p.exists():
            ctrl.append({"file": fname, "note": "缺存档, 跳过对照"})
            continue
        was = json.loads(p.read_text(encoding="utf-8")).get("stats") or {}
        mine = ic_by_h[5]
        for name, st in was.items():
            if name not in mine:
                continue
            d0, d1 = st.get("DEAD", {}), mine[name]["DEAD"]
            same = (d0.get("ic") is not None and d1.get("ic") is not None
                    and abs(float(d0["ic"]) - float(d1["ic"])) < 1e-3)
            if not same:
                ctrl_ok = False
                ctrl.append({"file": fname, "factor": name, "was": d0.get("ic"),
                             "now": d1.get("ic"), "ok": False})
    res["control"] = {"horizon": 5,
                      "ok": ctrl_ok,
                      "note": "H=5 必须复现已记录的周频 IC; 不复现 ⇒ H=21 一律不得解读",
                      "detail": ctrl or "全部逐位/1e-3 内一致"}
    log.info("对照(H=5 复现周频存档): %s", "通过" if ctrl_ok else "**失败**")
    res["secs"] = round(time.time() - t0, 1)
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    log.info("== F42 完成 %.1fs → %s ==", res["secs"], OUT)


if __name__ == "__main__":
    main()
