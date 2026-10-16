"""F45 · **当前敞口（满仓 w=1.0）的月度超额分布** —— 把"用户要的那个数"与配置对齐

## 为什么必须补这一刀

用户的目标函数是"**月**收益尽可能超额"，但终端现在报的是**年化**超额；而项目里唯一那份
**月度分布**（F36）是在 **w=0.873** 时代测的：

    留出集 月均 +0.55/+0.63pp、命中 62%、月σ≈3.0pp（SE≈0.53pp/月）

敞口在 2026-09-13 已拨到 **w=1.0**（计划 §2.46）⇒ 那份月度读数与现役配置**不是同一个东西**。
这与"方案挂着旧敞口标签"是同一类错误，必须补。

## 做法（不写第二份口径）

直接复用 F36 的月度聚合与统计函数（`F36.to_monthly` / `F36.stats` / `F36.WINDOWS`），
只把敞口换成现役 **w=1.0**，两套生产种子，与 F36 完全同窗口。
另外把 F36 记录的 w=0.873 读数**并排贴出**，让"换挡给月度口径带来多少"一眼可见。

## 诚实边界（月度口径最容易骗人的地方）

- 留出集（32 个月）的**月均**标准误 ≈ 月σ/√n ≈ 3.0/√32 ≈ **0.53pp/月** ⇒ 要在 95% 置信下
  看出"+10pp/年 ≈ +0.83pp/月"这种量级，需要**约 4 年**的月样本；纸面 8~12 周（2~3 个月）
  **在统计上什么都证明不了** —— 这不是消极，是算术。所以本读数只用于"报数口径正确"，
  **不许**当成"已经达到 +10pp/年"的证据；
- 月度是把周频收益**复利**起来的（同一段历史换了个聚合方式），不产生新信息量；
- 与 F28/F41/F36 同一条已开封的留出集 ⇒ 仍然不产生新的显著性证据。

用法：
    ./.venv/Scripts/python.exe -X utf8 experiments/05_label/F45_monthly_at_live_exposure.py
"""
import hashlib
import importlib.util
import json
import logging
import sqlite3
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


f36 = _load("f36", HERE / "F36_monthly_excess_distribution.py")
f29, f17, f22 = f36.f29, f36.f17, f36.f22
f1, f1b = f36.f1, f36.f1b

from app import config as cfg_mod             # noqa: E402
from app.factors import factor_engine as fe   # noqa: E402
from app.pit.engine import PITEngine          # noqa: E402

log = logging.getLogger("f45")
OUT = HERE / "F45_monthly_at_live_exposure_result.json"
F36_RESULT = HERE / "F36_monthly_excess_distribution_result.json"
W = 1.0                       # 现役敞口（满仓）
SEED_FILES = f36.SEED_FILES
WINDOWS = f36.WINDOWS


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        datefmt="%H:%M:%S", force=True)
    t0 = time.time()
    cfg = cfg_mod.load_config()
    cfg = {**cfg, "regime_equity_weight": {"aggressive": {"attack": W, "neutral": W,
                                                          "defense": W}}}
    m = fe.load_market(since="20140101")
    dates = pd.to_datetime(m["close"].index)
    wk = f1.week_end_dates(dates)
    cols = list(m["close"].columns)
    pit = PITEngine(m=m, cfg=cfg)
    mask = f1.tradable_frame(pit, wk, cols)
    mask_np = mask.fillna(False).to_numpy()
    op_adj = (m["open"] * m["adj"]).reindex(index=dates, columns=cols).astype(np.float64).to_numpy()
    pos_of = {d: i for i, d in enumerate(dates)}
    conn = sqlite3.connect(f"file:{BASE / 'data' / 'neweco.db'}?mode=ro", uri=True)
    PITSC = f17.table_scores(conn, "model_score_die_pit", wk, cols)
    conn.close()
    log.info("周锚点 %d | 股票 %d | 敞口 w=%.2f", len(wk), len(cols), W)

    old = {}
    if F36_RESULT.exists():
        old = json.loads(F36_RESULT.read_text(encoding="utf-8")).get("seeds") or {}
    res = {"script_sha": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:16],
           "equity_weight": W, "windows": {k: list(v) for k, v in WINDOWS.items()},
           "old_reference": {"weights": [0.873], "source": F36_RESULT.name},
           "seeds": {}, "delta_vs_0873": {}, "secs": None}
    for seed, fn in SEED_FILES.items():
        z = np.load(HERE / fn, allow_pickle=True)
        W5 = pd.DataFrame(z["scores"],
                          index=pd.to_datetime([str(d) for d in z["anchors"]]),
                          columns=[str(c) for c in z["cols"]]).reindex(index=wk, columns=cols)
        Sdf = pd.DataFrame(f22.wr((W5, 2), (PITSC, 1)), index=wk, columns=cols)
        s = f29._run_series(m, wk, mask, pit, cfg, pos_of, mask_np, op_adj, Sdf)
        mon = f36.to_monthly(s)
        res["seeds"][seed] = {}
        for wname, (lo, hi) in WINDOWS.items():
            g = mon[(mon.index.to_timestamp() >= lo) & (mon.index.to_timestamp() <= hi)]
            st = f36.stats(g)
            res["seeds"][seed][wname] = st
            o = ((old.get(seed) or {}).get(wname) or {})
            if st.get("n", 0) >= 3:
                log.info("[%s|%s] %d 个月 | 月均超额 %+.2fpp(中位 %+.2f) | 正超额月 %.0f%% "
                         "| 月σ %.2fpp | 最差 %+.2f / 最好 %+.2f | 年化超额 %+.2fpp",
                         seed, wname, st["n"], st["mean_pp"], st["median_pp"], 100 * st["hit"],
                         st["sd_pp"], st["worst_pp"], st["best_pp"], st["ann_excess_pp"])
            res["delta_vs_0873"].setdefault(wname, {})[seed] = {
                "mean_pp_0873": o.get("mean_pp"), "mean_pp_now": st.get("mean_pp"),
                "hit_0873": o.get("hit"), "hit_now": st.get("hit"),
                "sd_pp_0873": o.get("sd_pp"), "sd_pp_now": st.get("sd_pp")}
    # 月度口径的**样本量算术**：把"多久才看得出 +10pp/年"写进产物, 免得被误读
    need = {}
    for wname in WINDOWS:
        sd = [res["seeds"][s_][wname].get("sd_pp") for s_ in SEED_FILES
              if res["seeds"][s_][wname].get("n", 0) >= 3]
        if sd:
            sd_m = float(np.mean([x for x in sd if x]))
            se = sd_m / np.sqrt(res["seeds"][list(SEED_FILES)[0]][wname]["n"])
            need[wname] = {
                "month_sd_pp": round(sd_m, 2), "se_pp": round(se, 2),
                "months_for_2se_to_cover_0.83pp": int(np.ceil((2 * sd_m / 0.83) ** 2)),
                "note": "+10pp/年 ≈ +0.83pp/月; 要 2×SE 小于它需要的月数(≈4 年量级)"}
    res["sample_size_math"] = need
    res["secs"] = round(time.time() - t0, 1)
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    log.info("== F45 完成 | %.1fs → %s ==", res["secs"], OUT)
    for wname, v in need.items():
        log.info("样本量算术[%s]: 月σ %.2fpp → SE %.2fpp ⇒ 要 2×SE<0.83pp 需 %d 个月",
                 wname, v["month_sd_pp"], v["se_pp"], v["months_for_2se_to_cover_0.83pp"])


if __name__ == "__main__":
    main()
