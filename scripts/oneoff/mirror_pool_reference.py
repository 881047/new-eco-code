"""F74 · 镜厅 M0-lite：参照物跟踪误差与分辨增益（2026-09-15，预注册先冻结）

**预注册**：`plans/mirror-m0-lite-prereg.md`（本脚本运行时把它与自身的 sha256 一起写进产物）。
**性质**：纯研究、**零生产改动** —— 只读行情缓存 + 只读 DB(`mode=ro`) + 只读 F52/F66 序列；
产物只落 `experiments/mirror/`。不重训、不写生产表、不改 pin。

## 这个脚本回答什么（以及为什么重要）

原「镜厅」提案的卖点是"同池随机孪生"能把分辨期压 5~17×。它的隐含前提是：
**拿孪生当参照，比拿现役基准当参照更紧**。但核实代码时发现一件必须先把口径讲清的事：

`experiments/05_label/F52_live_vs_research_shape.py:136-151` 里的 `bench` **不是中证800**，
而是「**可交易池等权**、锚点掩码 × 锚点→执行日复权开盘」：

    good = mask[anchor_pos] & 有限价
    bench_t = mean( op_adj[exec_t][good] / op_adj[exec_{t-1}][good] − 1 )

⇒ 也就是说 F52/F66 序列里那个 `σ(S − bench) = 1.37pp` **本身就是"袖套 vs 池等权"的跟踪误差**。
那么"再拿一个同池随机 100 只（孪生）去配平"到底能不能更紧？这就是本脚本要量的东西。

## 三个参照（同一批 490 个执行区间）

1. **POOL**：按上面 F52 原样算法复现（全池等权，`cols` 取自当期分数存档）；
2. **BENCH800**：中证800 的**执行日开→开**收益（与 POOL 同一价格口径与同一日期向量，
   这样三个数才是同一把尺子）；
3. **S**：F66 序列的 `ret`（生产构型袖套净收益）。

## 判据（预注册 §三；**含一次已在预注册里登记的修订**）

- ~~A1（冻结原文）：`F66.bench` 与"独立算的 000906 两周一期收益" `corr ≥ 0.99`、`σ(差) ≤ 0.30pp`~~
  ⚠️ **该判据的前提被证伪**：`F66.bench` **不是** 000906，而是池等权（实测 corr 仅 0.66）。
  2026-09-15 独立复核 F3 指出脚本"静默换判据"⇒ 现按预注册纪律**登记修订**（见 §三·修订）。
- **A1′（修订后，替代 A1）**：`corr(POOL, F66.bench) ≥ 0.99` 且 `σ(POOL − F66.bench) ≤ 0.30pp`
  —— 这才是"我复现了那条线"的正确检查，且它是**新发现的支撑证据**。
- A1-old（保留留痕）：`corr(F66.bench, 000906 开→开) = 0.66` ⇒ 原文 A1 必失败，如实记录。
- A2：锚点整体平移一格 ⇒ A1′ 必须失败（**红检**：证明检查不是恒真）
- A3：缓存 `bench` 帧 与 DB `000906.SH` 水平值 `corr ≥ 0.999`
- B1：`σ(d_bench800)`、`σ(d_pool)` 与 `G_pool = σ(d_pool)²/σ(d_bench800)²`
- B2：`ρ_req(G) = 1 − 1/(2G)`（G=3.0 / 6.9）
- C：`G_pool ≥ 3` 继续 / `2 ≤ G_pool < 3` 待裁定 / `< 2` 缩编

用法：
    D:\\Python313\\python.exe -X utf8 scripts\\oneoff\\mirror_pool_reference.py
"""
import hashlib
import json
import logging
import sqlite3
import statistics as st
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE))

from app.factors import factor_engine as fe        # noqa: E402
from app.pit.engine import PITEngine               # noqa: E402
from app import config as cfg_mod                  # noqa: E402

log = logging.getLogger("mirror_pool_reference")

LABEL = "experiments/05_label"
PREREG = BASE / "plans" / "mirror-m0-lite-prereg.md"
OUT_DIR = BASE / "experiments" / "mirror"
OUT_JSON = OUT_DIR / "M0_lite_result.json"
SERIES_DIR = OUT_DIR / "M0_series_lite"

CAPITAL, TOP_N, BAND = 1_000_000.0, 100, 8.0
MIN_NAMES = 200                      # 与 F52 同：池成员不足 200 则该期参照线为 NaN
PER_YEAR = 52.0
SEEDS = ("seedA", "seedB")
SEED_FILE = {"seedA": "F4_scores_seedA.npz", "seedB": "F4_scores_seedB.npz"}
SEED_SERIES = {"seedA": "F66_series_seedA.json", "seedB": "F66_series_seedB.json"}
BENCH_CODE = "000906.SH"


def _load(name: str, path: Path):
    """按文件路径加载冻结的 oneoff 模块（照 F52 的 `_load` 写法；不复制它们的逻辑）"""
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _sha16(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()[:16]


def _pp(x) -> float:
    """转成「每锚点百分点」（本脚本所有 σ/均值一律用这个单位）"""
    return float(100.0 * x)


def reproduce_f52_bench(exec_dates: pd.DatetimeIndex, wk: pd.DatetimeIndex, dates: pd.DatetimeIndex,
                        mask: pd.DataFrame, m: dict, cols: list):
    """**原样复现** F52:136-151 的池等权参照线。

    F52 的语义（逐字对照源码）：
      · `d` 的每一行 = 一个**执行日**；`e = pos_of[执行日]`（行情缓存里的位置）
      · `prev_ap` = **上一个执行日所用锚点**在 `wk` 里的下标 ⇒ 池成员取其掩码行
      · `bench_t = mean( op_adj[e_t][good] / op_adj[e_{t−1}][good] − 1 )`，`good` 含 `mask[prev_ap]`
      · `good.sum() < MIN_NAMES` ⇒ NaN

    实现细节：执行日的 `anchor_pos` 取"**不晚于该执行日的最后一个锚点**"下标（与 F1b:404
    里 `k` 的语义一致 —— 执行日用最近一个已完成的锚点选股）。
    """
    op_adj = (m["open"] * m["adj"]).reindex(index=dates, columns=cols).astype(np.float64).to_numpy()
    pos_of = pd.Series(np.arange(len(dates)), index=dates)
    mask_np = mask.fillna(False).to_numpy()
    wk_arr = wk.values

    pool, n_pool = [], []
    prev_exec, prev_ap = None, None
    for t in exec_dates:
        e = int(pos_of.asof(t)) if t in pos_of.index else int(np.searchsorted(dates.values, np.datetime64(t), side="right") - 1)
        ap = int(np.searchsorted(wk_arr, np.datetime64(t), side="right") - 1)   # 不晚于 t 的最后一个锚点
        if prev_exec is None:
            pool.append(np.nan); n_pool.append(0)
        else:
            good = (mask_np[prev_ap] & np.isfinite(op_adj[prev_exec])
                    & np.isfinite(op_adj[e]) & (op_adj[prev_exec] > 0))
            n_pool.append(int(good.sum()))
            pool.append(float(np.mean(op_adj[e][good] / op_adj[prev_exec][good] - 1))
                        if good.sum() >= MIN_NAMES else np.nan)
        prev_exec, prev_ap = e, ap
    return pd.Series(pool, index=exec_dates), n_pool


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        datefmt="%H:%M:%S", force=True)
    t0 = time.time()
    cfg = cfg_mod.load_config()
    m = fe.load_market(since="20140101")
    dates = pd.to_datetime(m["close"].index)
    f1 = _load("f1", BASE / LABEL / "F1_weekly_ic_decay.py")
    wk = f1.week_end_dates(dates)
    cols = list(m["close"].columns)
    pit = PITEngine(m=m, cfg=cfg)
    mask = f1.tradable_frame(pit, wk, cols)
    log.info("面板就绪 | 锚点 %d | 票池 %d 只 | %.0fs", len(wk), len(cols), time.time() - t0)

    # 指数（只读 DB）—— ⚠️ 必须取 **open**：POOL 线是"执行日开→开"（复权开盘），
    # 与它对比的中证800 也必须用开盘价。2026-09-15 独立复核 F1 抓到：初版这里取的是
    # `close`，与池线的开→开**不是同一把尺** ⇒ 混口径把 G 抬高了 1.33×
    # （close 口径 G=5.919 / open 口径 G=4.466）。修正后两列同尺。
    conn = sqlite3.connect(f"file:{BASE / 'data' / 'neweco.db'}?mode=ro", uri=True)
    ipx = pd.read_sql("SELECT trade_date, open FROM index_daily WHERE ts_code=? ORDER BY trade_date",
                      conn, params=(BENCH_CODE,))
    conn.close()
    idx = pd.Series(ipx["open"].values, index=pd.to_datetime(ipx["trade_date"])).astype(float)

    out = {"script_sha16": _sha16(Path(__file__)),
           "prereg_sha16": _sha16(PREREG) if PREREG.exists() else None,
           "window": {"anchors": None, "intervals": None},
           "panels": {"cols": len(cols), "anchors": len(wk)},
           "checks": {}, "seeds": {}, "verdict": {}}
    SERIES_DIR.mkdir(parents=True, exist_ok=True)

    merged = {}
    for seed in SEEDS:
        ser = json.loads((BASE / LABEL / SEED_SERIES[seed]).read_text(encoding="utf-8"))
        s_ret = pd.Series([x["ret"] for x in ser], index=pd.to_datetime([x["date"] for x in ser], unit="ms"))
        s_bench = pd.Series([x["bench"] for x in ser], index=s_ret.index)
        pool_s, n_pool = reproduce_f52_bench(s_ret.index, wk, dates, mask, m, cols)
        tab = pd.DataFrame({"POOL": pool_s.values, "n_pool": n_pool}, index=s_ret.index)
        # BENCH800：与 POOL **同一区间**（上一执行日 → 本执行日），指数收盘价取执行日
        ipx_at = idx.reindex(s_ret.index).ffill()
        tab["BENCH800"] = (ipx_at / ipx_at.shift(1) - 1.0).values
        tab["S"] = s_ret
        tab["F66_bench"] = s_bench
        tab["d_pool_pool"] = tab["S"] - tab["POOL"]        # 袖套 vs 池等权（同算法复现）
        tab["d_bench800"] = tab["S"] - tab["BENCH800"]     # 袖套 vs 中证800（执行日开→开）
        merged[seed] = tab

        def _sd(x):
            v = pd.Series(x).dropna().values
            return float(np.std(v, ddof=1)) if len(v) > 2 else float("nan")
        rec = {
            "n": int(tab["S"].notna().sum()),
            "sd_S_pp": _pp(_sd(tab["S"])),
            "sd_F66bench_pp": _pp(_sd(tab["F66_bench"])),
            "sd_POOL_pp": _pp(_sd(tab["POOL"])),
            "sd_BENCH800_pp": _pp(_sd(tab["BENCH800"])),
            "sd_d_pool_pool_pp": _pp(_sd(tab["d_pool_pool"])),
            "sd_d_bench800_pp": _pp(_sd(tab["d_bench800"])),
            "mean_d_pool_pool_pp": _pp(float(pd.Series(tab["d_pool_pool"]).dropna().mean())),
            "mean_d_bench800_pp": _pp(float(pd.Series(tab["d_bench800"]).dropna().mean())),
            "n_pool_median": int(np.nanmedian(tab["n_pool"].values)),
            # A1′：复现检查（我的 POOL vs F66 序列的 bench）—— 修订后的判据
            "A1_corr_pool_vs_f66bench": float(pd.Series(tab["POOL"]).corr(pd.Series(tab["F66_bench"]))),
            "A1_sd_diff_pp": _pp(_sd(pd.Series(tab["POOL"]) - pd.Series(tab["F66_bench"]))),
            # A1-old：预注册原判据（前提已被证伪：F66.bench 不是 000906）—— **留痕，不删**
            "A1old_corr_f66bench_vs_000906": float(
                pd.Series(tab["F66_bench"]).corr(pd.Series(tab["BENCH800"]))),
            "A1old_sd_diff_pp": _pp(_sd(pd.Series(tab["F66_bench"]) - pd.Series(tab["BENCH800"]))),
        }
        out["seeds"][seed] = rec
        np.savez_compressed(SERIES_DIR / f"M0_lite_{seed}.npz",
                            date=tab.index.values.astype("datetime64[ns]"),
                            S=tab["S"].values, POOL=tab["POOL"].values,
                            BENCH800=tab["BENCH800"].values, F66_bench=tab["F66_bench"].values,
                            d_pool_pool=tab["d_pool_pool"].values, d_bench800=tab["d_bench800"].values,
                            n_pool=tab["n_pool"].values)

    # A2 红检：锚点整体平移一格
    shift_sd, shift_corr = [], []
    for seed in SEEDS:
        tab = merged[seed]
        p = pd.Series(tab["POOL"].values[1:], index=tab.index[:-1])
        q = pd.Series(tab["F66_bench"].values[:-1], index=tab.index[:-1])
        shift_sd.append(_pp(_sd(p - q)))
        shift_corr.append(float(p.corr(q)))
    out["checks"]["A2_shifted_sd_diff_pp"] = [round(x, 4) for x in shift_sd]
    out["checks"]["A2_shifted_corr"] = [round(x, 4) for x in shift_corr]

    # A3：缓存 bench 帧 与 DB 000906 水平值
    bf = m["bench"].copy(); bf.index = pd.to_datetime(bf.index)
    j = pd.DataFrame({"cache": bf, "db": idx}).dropna()
    out["checks"]["A3_corr_cache_vs_db"] = float(j["cache"].corr(j["db"]))
    out["checks"]["A3_overlap_days"] = int(len(j))

    # B：增益与孪生需求
    gs = []
    for seed in SEEDS:
        # G = σ(S vs 市场基准)² / σ(S vs 池等权)² —— "池线比现役口径紧多少倍"。
        # ⚠️ 分子必须是**较松的那个**（市场基准）；写反会把"缩编"和"继续"对调
        # （2026-09-15 首跑就写反过，被 mirror_m0_recompute.py 的逐位复算当场抓到）。
        a, b = out["seeds"][seed]["sd_d_bench800_pp"], out["seeds"][seed]["sd_d_pool_pool_pp"]
        gs.append((a / b) ** 2 if b > 0 else float("nan"))
    # 全精度存档（不做舍入）：复算器因此可以**逐位比对**，而不是比"我舍入后的样子"
    out["G_pool"] = {"seedA": float(gs[0]), "seedB": float(gs[1]),
                     "merged_mean": float(np.nanmean(gs))}
    out["rho_required"] = {"G3.0": 1 - 1 / (2 * 3.0), "G6.9": 1 - 1 / (2 * 6.9)}
    g = out["G_pool"]["merged_mean"]
    out["verdict"] = {"gate": ("继续" if g >= 3 else ("待裁定" if g >= 2 else "缩编")),
                      "rule": "G≥3 继续 / 2≤G<3 待裁定 / G<2 缩编（预注册 §三-C）"}
    out["window"] = {"anchors": int(len(wk)),
                     "intervals": int(merged[SEEDS[0]]["S"].notna().sum())}
    OUT_JSON.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    log.info("产物 → %s（耗时 %.0fs）", OUT_JSON.name, time.time() - t0)
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
