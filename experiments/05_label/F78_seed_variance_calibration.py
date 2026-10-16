r"""F78 —— seed 方差**标定**（2026-09-21 执行；判据先冻结在 plans/f78-seed-variance-calibration-20260921.md）

## 为什么跑
05 号 §5 的「5.9pp seed 方差」只是 **1 次观测**的差值 ⇒ χ²₁ 下 σ 的 95% 区间 = [0.45×, 31.9×]。
拿它当先验门槛去否掉候选，统计上站不住。本实验把它升级为**有置信区间的估计**，并判定噪声结构。

## 相对 F76b 的唯一但关键的修正
F76b 的 `base_A`={42,7,123,2024} 与 `base_B`={7,2024,99,555} **共享 2/4 个 seed** ⇒ ρ≈0.5
⇒ 差值方差被压成 2σ²(1−ρ) ⇒ σ 被低估。本实验所有臂的 seed 集合**两两零重叠** ⇒ ρ=0。

## 设计（冻结，不许跑完再改）
- 模型：`A_i = μ + b_i(持续性, σ_b) + ē_i(逐期, σ_t)`；`Var(A_i) = σ_b² + σ_t² = σ_total²`
- **组 A**：6 臂 × K=4（同生产集成规模）⇒ 24 个互异 seed，集合间零重叠 ⇒ 直接量 σ_total(4)
- **组 B**：3 臂 × K=16 ⇒ 48 个互异 seed（与组 A 也不重叠）⇒ 量 σ_total(16)，检验 σ ∝ 1/√K
- σ_t 由**配对逐期差**标准误测：`σ_time_diff = sd(d_ij)/√n × 12` ⇒ `σ_t = σ_time_diff / √2`
  （注意：`b_i` 是**跨期常量**，它进的是均值不是 sd ⇒ 配对 t 的 SE **看不见** σ_b，这正是 5.5③ 的由来）

## 判据
| # | 判据 | 门限 |
|---|---|---|
| J1 | σ_total(4) 点估计 + 95%CI（χ²₅）| 仅报告 |
| J2 | σ_b 占比 = 1 − σ_t²/σ_total² | >50% ⇒ 持续性主导（加期数无用）；<25% ⇒ 推翻 |
| J3 | σ_total(16)/σ_total(4) vs √（4/16）=0.5 | \|Δ\| ≤ 0.20 ⇒ 接受 1/√K |
| J4 | ratio = √2·σ_total(4)/σ_time_diff（**独立 seed 设计的 t 虚高上界**）| 仅报告；与 F76b 实测 1.77× 对照 |
| J5 | 组 A 各臂年化离散 与 由逐期序列推出的期望 是否相容 | 仅报告 |

## 安全
- **只写隔离表** `model_score_f78_*` / `model_importance_f78_*` 与回测 run 表；**不写生产 `model_score`**
- 不改 `trainer.ENSEMBLE_SEEDS`、不改 pin、不改 `app/**`、不判任何候选（只量仪器）
- 模型归档进 `data/models/f78_<arm>/`（带 seed 后缀，不覆盖生产月度归档）
- 幂等：表已够期数则跳过重训

stage: pilot / retrain / readout / portfolio / analyze
"""
import argparse
import datetime
import io
import itertools
import json
import logging
import math
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

BASE = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE))

from app import config as cfg_mod                                   # noqa: E402
from app import db                                                  # noqa: E402
from app.backtest import engine as E                                # noqa: E402
from app.config import DATA_DIR                                     # noqa: E402
from app.factors import factor_engine as fe                         # noqa: E402
from app.models import trainer                                      # noqa: E402
from app.models.expert import ensure_score_table, zone_of           # noqa: E402
from app.pit.engine import PITEngine                                # noqa: E402
from app.portfolio import fees as fee_mod                           # noqa: E402
from app.weekly import scores as sc                                 # noqa: E402
from app.logger import setup_logger                                 # noqa: E402

HERE = Path(__file__).resolve().parent
OUT = HERE / "F78_seed_variance_calibration_result.json"
log = logging.getLogger("f78")

# ---- 冻结常量 ---------------------------------------------------------------
PROD_BACKTEST_START = "2017-01-01"        # 与 F76b 同窗（116 锚点 / 115 期）
PILOT_START = "2026-06-01"                # ⇒ 3 个锚点，用于成本投影
GROUP_A_K, GROUP_A_M = 4, 12              # 组 A：K=4（同生产）⇒ F78b 从 6 臂扩到 12 臂
GROUP_B_K, GROUP_B_M = 16, 3              # 组 B：K=16 × 3 臂
# F78b 探针臂（**不计入 σ_total 估计**）：P1 = 生产种子集（= F76b 的 base_A），
# P2 = F76b 的 base_B 种子集 —— 现快照重跑 ⇒ 检验 5.9pp 是否为数据漂移
PROD_SEED_SET = [42, 7, 123, 2024]
F76B_B_SEED_SET = [7, 2024, 99, 555]
TOP_N = 100                               # 研究层读数口径（F56/F76b 同源）
HORIZON_READOUT = 21
SELL_REF_DATE = "20260914"
WINDOW_FULL = "20170101"                  # 组合层只用全窗（分段在 research 层做）
IMPORTANCE_DDL = ("CREATE TABLE IF NOT EXISTS {t} (train_date TEXT, factor TEXT, "
                  "importance REAL, PRIMARY KEY (train_date, factor)) WITHOUT ROWID")
# χ² 分位数（用于 σ 的 95% 置信区间）
CHI2 = {1: (0.0009821, 5.0239), 2: (0.0506356, 7.3778), 5: (0.8312116, 12.8325),
        9: (2.70039, 19.0228), 11: (3.81575, 21.9200),
        13: (5.00874, 24.7356), 17: (7.56419, 30.1910)}
# 成本基准：**F76b 实测**（116 锚 × 4 seed = 464 次训练，6.3 min）
F76B_SEC_PER_MODEL = 6.3 * 60 / (116 * 4)     # ≈ 0.815 s/(锚点 × 单模型)
FIXED_OVERHEAD_S = 72.0                        # 面板加载 + 因子计算（pilot 实测 ≈72s/臂）


def _seed_pool(n: int, base: int = 1_000_003, step: int = 7919) -> list:
    """确定性生成 n 个互异 seed（质数步进）；**不含**生产种子 ⇒ 与现役零耦合。"""
    prod = set(int(s) for s in trainer.ENSEMBLE_SEEDS)
    out, v = [], base
    while len(out) < n:
        if v not in prod:
            out.append(v)
        v += step
    return out


_POOL = _seed_pool(GROUP_A_K * GROUP_A_M + GROUP_B_K * GROUP_B_M)   # 48 + 48 = 96
ARMS = {}
for i in range(GROUP_A_M):                                  # 组 A：K=4，12 臂
    ARMS[f"A{i + 1}"] = {"group": "A", "K": GROUP_A_K,
                         "seeds": _POOL[i * GROUP_A_K:(i + 1) * GROUP_A_K]}
_off = GROUP_A_K * GROUP_A_M
for i in range(GROUP_B_M):                                  # 组 B：K=16，3 臂
    ARMS[f"B{i + 1}"] = {"group": "B", "K": GROUP_B_K,
                         "seeds": _POOL[_off + i * GROUP_B_K:_off + (i + 1) * GROUP_B_K]}
# 探针臂：group="P" ⇒ 不进 σ_total 估计（P1 与 P2 共享 2/4 seed，会低估离散）
ARMS["P1"] = {"group": "P", "K": GROUP_A_K, "seeds": list(PROD_SEED_SET)}
ARMS["P2"] = {"group": "P", "K": GROUP_A_K, "seeds": list(F76B_B_SEED_SET)}


def _tables(arm: str) -> tuple:
    return f"model_score_f78_{arm.lower()}", f"model_importance_f78_{arm.lower()}"


GROUP_FILTER = None        # 由 --group 设置；None = 全跑


def _arms() -> dict:
    """按 --group 过滤后的臂（分组执行：先 A 后 B，便于中途看成本再决定）。"""
    if not GROUP_FILTER:
        return ARMS
    return {a: s for a, s in ARMS.items() if s["group"] in GROUP_FILTER}


# ---- 小工具 -----------------------------------------------------------------
def _merge_json(patch: dict) -> dict:
    cur = {}
    if OUT.exists():
        cur = json.loads(OUT.read_text(encoding="utf-8"))
    cur.update(patch)
    cur["updated_at"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    OUT.write_text(json.dumps(cur, ensure_ascii=False, indent=1, default=str),
                   encoding="utf-8")
    return cur


def _expected_anchors(backtest_start: str) -> int:
    m = fe.load_market()
    cal = pd.DatetimeIndex(pd.to_datetime(m["close"].index))
    md = fe.month_end_dates(cal, today=datetime.date.today())
    md = md[md >= pd.Timestamp("2014-01-01")]
    return int((md >= pd.Timestamp(backtest_start)).sum())


def _count_dates(table: str) -> int:
    with db.conn_ctx() as conn:
        return int(pd.read_sql(f"SELECT COUNT(DISTINCT trade_date) c FROM {table}",
                               conn)["c"][0])


def _paired_stats(diffs: pd.Series) -> dict:
    d = diffs.dropna()
    n = int(len(d))
    if n < 5:
        return {"n": n, "mean_pp": None, "sd_pp": None, "t": None}
    mean, sd = float(d.mean()), float(d.std(ddof=1))
    t = mean / (sd / np.sqrt(n)) if sd > 0 else float("inf")
    return {"n": n, "mean_pp": round(100 * mean, 4), "sd_pp": round(100 * sd, 4),
            "t": round(float(t), 3)}


def _sigma_ci(s: float, dof: int) -> dict:
    """样本标准差 s（dof 个自由度）的 σ 95% 置信区间。"""
    if dof not in CHI2 or s <= 0:
        return {"lo": None, "hi": None}
    lo_q, hi_q = CHI2[dof]
    return {"lo": round(s * math.sqrt(dof / hi_q), 3), "hi": round(s * math.sqrt(dof / lo_q), 3)}


# ---- stage: pilot（成本投影） -----------------------------------------------
def stage_pilot() -> None:
    st, it = "model_score_f78_pilot", "model_importance_f78_pilot"
    ensure_score_table(st)
    with db.conn_ctx() as conn:
        conn.execute(IMPORTANCE_DDL.format(t=it))
        conn.commit()
    t0 = time.time()
    trainer.run(full=True, score_table=st, importance_table=it,
                archive_dir=DATA_DIR / "models" / "f78_pilot",
                num_boost_round=trainer.ENSEMBLE_ROUNDS, early_stopping_rounds=0,
                ensemble_seeds=ARMS["A1"]["seeds"], sample_weight_mode=None,
                train_months=36, holdout_recent=6, backtest_start=PILOT_START)
    dt = time.time() - t0
    n = _count_dates(st)
    with db.conn_ctx() as conn:
        conn.execute(f"DROP TABLE IF EXISTS {st}")
        conn.execute(f"DROP TABLE IF EXISTS {it}")
        conn.commit()
    # ⚠️ 投影陷阱（本轮实测的教训）：pilot 只有 3 个锚点 ⇒ 面板加载 + 因子计算等**一次性**
    # 开销（约 72s）被摊到 3×4=12 个模型上 ⇒ 单模型成本被放大到 7.14s（真实 ≈0.8s）。
    # ⇒ 投影必须以 **F76b 的 464 次训练**为基准，pilot 的自算速率只登记不用。
    naive = dt / max(n * GROUP_A_K, 1)              # 含一次性开销 ⇒ 高估，仅登记
    n_model_a = 116 * GROUP_A_K * GROUP_A_M         # 组A: 6×4×116
    n_model_b = 116 * GROUP_B_K * GROUP_B_M         # 组B: 3×16×116
    proj_h = F76B_SEC_PER_MODEL * (n_model_a + n_model_b) / 3600
    fixed = FIXED_OVERHEAD_S * (GROUP_A_M + GROUP_B_M) / 3600
    log.info("pilot: %d 锚点 %.0fs | 自算 %.2f s/模型（**含一次性开销，高估**）", n, dt, naive)
    log.info("投影（以 F76b %.3f s/模型为基准）: 训练 %.2f h + 一次性开销 %.2f h ⇒ **%.2f h**",
             F76B_SEC_PER_MODEL, proj_h, fixed, proj_h + fixed)
    _merge_json({"pilot": {
        "n_anchors": n, "elapsed_s": round(dt, 1),
        "naive_sec_per_model": round(naive, 3),
        "naive_is_inflated": "pilot 仅 3 锚点 ⇒ 一次性开销（面板/因子，约 72s）被摊薄不当 ⇒ 仅登记不用",
        "f76b_sec_per_model": F76B_SEC_PER_MODEL,
        "n_models": {"group_A": n_model_a, "group_B": n_model_b},
        "projection_train_h": round(proj_h, 2),
        "projection_fixed_overhead_h": round(fixed, 2),
        "projection_total_h": round(proj_h + fixed, 2),
        "note": "组A 6臂×K=4 + 组B 3臂×K=16 = 2784 + 5568 次单模型训练"}})


# ---- stage: retrain（9 臂） --------------------------------------------------
def stage_retrain() -> dict:
    expected = _expected_anchors(PROD_BACKTEST_START)
    log.info("全量锚点期望 %d（%s 起）", expected, PROD_BACKTEST_START)
    retr = {}
    for arm, spec in _arms().items():
        st, it = _tables(arm)
        ensure_score_table(st)
        with db.conn_ctx() as conn:
            conn.execute(IMPORTANCE_DDL.format(t=it))
            conn.commit()
        have = _count_dates(st)
        if have >= expected:
            # ⚠️ F78b 教训：组 A 从 6 臂扩到 12 臂后 `_POOL` 切片整体位移 ⇒ 已训臂的
            # 「当前设计种子」≠「上次实际训练的种子」。跳过时必须以**上次实际**为准，否则台账失真。
            prev_seeds = None
            if OUT.exists():
                _prev = json.loads(OUT.read_text(encoding="utf-8"))
                prev_seeds = (_prev.get("retrains", {}).get(arm, {}) or {}).get("seeds")
            seeds_used = list(prev_seeds) if prev_seeds else list(spec["seeds"])
            if prev_seeds and list(prev_seeds) != list(spec["seeds"]):
                log.warning("%s: 表已存在但**本次设计的种子与上次不同**（池切片位移）"
                            " ⇒ 以**上次实际训练**的种子为准，不重训", arm)
            log.info("%s: 已有 %d 期 ⇒ 跳过（幂等）", arm, have)
            retr[arm] = {"group": spec["group"], "K": spec["K"], "table": st,
                         "n_dates": have, "seeds": seeds_used, "skipped": True,
                         "seeds_source": "previous_run" if prev_seeds else "current_design"}
            continue
        log.info("== 重训 %s (组%s, K=%d, seeds=%s...) ==", arm, spec["group"],
                 spec["K"], spec["seeds"][:2])
        t0 = time.time()
        res = trainer.run(
            full=True, score_table=st, importance_table=it,
            archive_dir=DATA_DIR / "models" / f"f78_{arm.lower()}",
            num_boost_round=trainer.ENSEMBLE_ROUNDS, early_stopping_rounds=0,
            ensemble_seeds=spec["seeds"], sample_weight_mode=None,
            train_months=36, holdout_recent=6, backtest_start=PROD_BACKTEST_START)
        retr[arm] = {"group": spec["group"], "K": spec["K"], "table": st,
                     "n_dates": _count_dates(st), "trained": res.get("trained"),
                     "elapsed_min": round((time.time() - t0) / 60, 1),
                     "seeds": spec["seeds"], "skipped": False}
        log.info("%s 完成 %.1f min（%d 期）", arm, retr[arm]["elapsed_min"],
                 retr[arm]["n_dates"])
    # 零重叠自检
    # 零重叠自检（**排除探针臂 P**：P1 是生产种子集、P1/P2 彼此共享 2/4 seed，属设计内）
    est = {a: s for a, s in ARMS.items() if s["group"] != "P"}
    all_seeds = [s for v in est.values() for s in v["seeds"]]
    assert len(all_seeds) == len(set(all_seeds)), "seed 有重复 ⇒ 设计失效"
    for a, b in itertools.combinations(est, 2):
        assert not (set(est[a]["seeds"]) & set(est[b]["seeds"])), f"{a}/{b} seed 重叠"
    assert not (set(all_seeds) & set(int(s) for s in trainer.ENSEMBLE_SEEDS)), "与生产种子重叠"
    log.info("seed 自检通过：%d 个互异、参与估计的臂间零重叠、与生产零重叠（探针臂 P 另计）",
             len(all_seeds))
    _merge_json({"retrains": retr, "expected_anchors": expected,
                 "seed_overlap_check": "PASS（72 个互异 seed，臂间零重叠，与生产零重叠）"})
    return retr


# ---- stage: readout（研究层 top100 月度序列） --------------------------------
def _load_scores(table: str) -> pd.DataFrame:
    with db.conn_ctx() as conn:
        df = pd.read_sql(f"SELECT trade_date, ts_code, score FROM {table}", conn)
    df["trade_date"] = pd.to_datetime(df["trade_date"], format="%Y%m%d")
    return df.pivot_table(index="trade_date", columns="ts_code",
                          values="score").sort_index()


def _top100_series(S, Y, mask_np, sell_rate) -> pd.Series:
    """F56/F76b 逐字同源的 top100 等权净超额序列。"""
    rets, prev = [], None
    for k in range(S.shape[0]):
        y, s = Y[k], S[k]
        ok = mask_np[k] & np.isfinite(s) & np.isfinite(y)
        if ok.sum() < 200:
            rets.append(np.nan)
            continue
        idx = np.argsort(-np.where(ok, s, -np.inf))[:TOP_N]
        w = np.full(len(idx), 1.0 / len(idx))
        gross = float(np.dot(w, y[idx].astype(float)))
        cost = 0.0
        cur = dict(zip(idx, w))
        if prev:
            names = set(cur) | set(prev)
            cost = sum(abs(cur.get(i, 0.0) - prev.get(i, 0.0)) for i in names) * sell_rate
        prev = cur
        rets.append(gross - cost)
    return pd.Series(rets)


def stage_readout() -> dict:
    expected = _expected_anchors(PROD_BACKTEST_START)
    wides = {}
    for arm, spec in _arms().items():
        w = _load_scores(spec["table"] if "table" in spec else _tables(arm)[0])
        if len(w) < expected:
            raise SystemExit(f"{arm} 只有 {len(w)} 期（期望 {expected}）⇒ 先跑 --stage retrain")
        wides[arm] = w
    anchors = pd.DatetimeIndex(sorted(set.intersection(*[set(w.index) for w in wides.values()])))
    cols = sorted(set().union(*[set(w.columns) for w in wides.values()]))
    log.info("共同锚点 %d 期（%s ~ %s）| 列并集 %d", len(anchors),
             anchors[0].date(), anchors[-1].date(), len(cols))

    m = fe.load_market(since=sc.SINCE)
    cfg = cfg_mod.load_config()
    mask = PITEngine(m=m, cfg=cfg).tradable_matrix(anchors, columns=cols).fillna(False)
    Y = sc.forward_excess(m, mask, HORIZON_READOUT)
    sell_rate = fee_mod.effective_rate(25_000.0, "sell", SELL_REF_DATE)
    mask_np = mask.to_numpy(dtype=bool)
    zones = pd.Series(zone_of(anchors), index=anchors)

    series = {}
    for arm, w in wides.items():
        s = _top100_series(w.reindex(index=anchors, columns=cols).to_numpy(dtype=float),
                           Y, mask_np, sell_rate)
        series[arm] = pd.Series(s.to_numpy(), index=anchors)

    # 落盘逐期序列（F78 的**元数据缺失**：只存了年化 ⇒ 研究层 σ_time 只能借 F76b 的 1.07pp。
    # 研究层是分级筛法的入口，它的 σ_time 必须**实测**）。分组执行时按列合并。
    rs_path = HERE / "F78_research_series.csv"
    rs_df = pd.DataFrame(series)
    if rs_path.exists():
        old = pd.read_csv(rs_path, index_col=0, parse_dates=True)
        rs_df = pd.concat([old[[c for c in old.columns if c not in rs_df.columns]], rs_df],
                          axis=1).sort_index()
    rs_df.to_csv(rs_path)
    log.info("研究层逐期序列已落盘 F78_research_series.csv（%d 期 × %d 臂）",
             len(rs_df), rs_df.shape[1])

    per_arm = {}
    for arm, s in series.items():
        row = {"group": ARMS[arm]["group"], "K": ARMS[arm]["K"],
               "n": int(s.notna().sum()),
               "ann_pp": round(100 * float(s.mean()) * 12, 4)}
        for seg, sel in (("ACTIVE", zones == "ACTIVE"), ("DEAD", zones == "DEAD")):
            row[f"{seg}_ann_pp"] = round(100 * float(s[sel].mean()) * 12, 4)
        per_arm[arm] = row
        log.info("%s(组%s K=%d): 全史年化 %.2fpp | ACTIVE %.2f | DEAD %.2f",
                 arm, row["group"], row["K"], row["ann_pp"],
                 row["ACTIVE_ann_pp"], row["DEAD_ann_pp"])

    # 分组执行时**按臂合并**，不要整块覆盖（否则先跑 A 再跑 B 会丢 A）
    prev = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {}
    per_arm_all = dict(prev.get("research", {}).get("per_arm", {}))
    per_arm_all.update(per_arm)
    _merge_json({"research": {
        "protocol": {"top_n": TOP_N, "horizon": HORIZON_READOUT,
                     "sell_rate_bp": round(sell_rate * 1e4, 3),
                     "anchors": len(anchors),
                     "window": [str(anchors[0].date()), str(anchors[-1].date())],
                     "columns_union": len(cols)},
        "per_arm": per_arm_all}})
    return {"per_arm": per_arm_all}


# ---- stage: portfolio（组合层全窗回测） --------------------------------------
def _nav_series(run_date: str) -> pd.Series:
    with db.conn_ctx() as conn:
        df = pd.read_sql("SELECT trade_date, sys_nav FROM backtest_nav "
                         "WHERE run_date=? ORDER BY trade_date", conn, params=[run_date])
    s = pd.Series(df["sys_nav"].to_numpy(dtype=float),
                  index=pd.to_datetime(df["trade_date"]))
    return s[~s.index.duplicated(keep="last")].sort_index()


def stage_portfolio() -> dict:
    rows, navs = [], {}
    t0 = time.time()
    for arm, spec in _arms().items():
        table = spec.get("table") or _tables(arm)[0]
        m = E.run_backtest(score_table=table, run_tag=f"f78_{arm.lower()}",
                           start=WINDOW_FULL, end=None)
        rows.append({"arm": arm, "group": spec["group"], "K": spec["K"],
                     "run": m["run_date"], "n": m.get("periods"),
                     "sys_ann": m["sys_ann_return"], "sys_mdd": m["sys_max_drawdown"],
                     "sys_mdd_daily": m["sys_mdd_daily"], "sharpe": m.get("sharpe"),
                     "turnover": m.get("avg_turnover"), "idle": m.get("avg_cash_idle"),
                     "n_avg": m.get("avg_stocks")})
        navs[arm] = _nav_series(m["run_date"])
        log.info("[%.0fs] %s 年化 %.2f%% MDD %.2f%% 换手 %.3f",
                 time.time() - t0, arm, 100 * m["sys_ann_return"],
                 100 * m["sys_max_drawdown"], m.get("avg_turnover") or float("nan"))
    prev = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {}
    rows_all = [r for r in prev.get("portfolio", {}).get("runs", [])
                if r["arm"] not in {x["arm"] for x in rows}] + rows
    _merge_json({"portfolio": {"window": WINDOW_FULL, "runs": rows_all}})
    # NAV 序列另存（供 analyze 用，避免重复回测）；分组执行时按列合并
    nav_path = HERE / "F78_nav_series.csv"
    nav_df = pd.DataFrame(navs)
    if nav_path.exists():
        old = pd.read_csv(nav_path, index_col=0, parse_dates=True)
        nav_df = pd.concat([old[[c for c in old.columns if c not in nav_df.columns]], nav_df],
                           axis=1).sort_index()
    nav_df.to_csv(nav_path)
    log.info("NAV 序列已落盘 F78_nav_series.csv（%d 行 × %d 臂）", len(nav_df), nav_df.shape[1])
    return {"rows": rows_all}


# ---- stage: analyze（判据 J1~J5） -------------------------------------------
def _group_stats(ann: dict, series_map: dict, group: str, K: int) -> dict:
    """给定一组的「年化读数」与「逐期序列」，算 σ_total / σ_t / σ_b。"""
    arms = sorted([a for a in ann if ann[a]["group"] == group])
    if len(arms) < 2:
        return None                       # 该组还没跑（分组执行时）
    vals = np.array([ann[a]["ann_pp"] for a in arms], dtype=float)
    m_ = len(vals)
    dof = m_ - 1
    s_tot = float(vals.std(ddof=1))              # 跨臂离散 ⇒ σ_total
    # 配对逐期差的标准误（**看不见**持续性分量 b_i，因为它跨期常量）
    ses = []
    for a, b in itertools.combinations(arms, 2):
        d = (series_map[a] - series_map[b]).dropna()
        if len(d) >= 5:
            # ⚠️ 单位：`pct_change()` 是**小数**；×12 年化后再 ×100 才是 pp
            #    （本轮第一版漏了 ×100 ⇒ σ_time 小 100 倍 ⇒ J4 算出 107 的荒谬值）
            ses.append(float(d.std(ddof=1)) / math.sqrt(len(d)) * 12 * 100)
    se_diff = float(np.mean(ses)) if ses else float("nan")
    s_t = se_diff / math.sqrt(2)                 # 单臂的逐期分量
    s_b = math.sqrt(max(s_tot ** 2 - s_t ** 2, 0.0))
    return {"group": group, "K": K, "n_arms": m_, "ann_pp_per_arm": {a: ann[a]["ann_pp"] for a in arms},
            "mean_ann_pp": round(float(vals.mean()), 3),
            "sigma_total_pp": round(s_tot, 3), "sigma_total_ci95": _sigma_ci(s_tot, dof),
            "sigma_time_diff_pp": round(se_diff, 3), "sigma_t_pp": round(s_t, 3),
            "sigma_b_pp": round(s_b, 3),
            "b_share_of_var": round(s_b ** 2 / s_tot ** 2, 3) if s_tot > 0 else None,
            "n_pairs": len(ses)}


def _se_diff(smap: dict, a: str, b: str, sl=None):
    d = (smap[a] - smap[b]).dropna()
    if sl is not None:
        d = d.iloc[sl]
    if len(d) < 5:
        return None
    return float(d.std(ddof=1)) / math.sqrt(len(d)) * 12 * 100


def _b4_sqrtT_check(smap: dict, arms: list) -> dict:
    """B4（**免费**，不重训）：折半检验 σ ∝ 1/√T —— 上一轮"加期数"的整条结论建立在它上面。
    T 减半 ⇒ SE 应放大 √2 ≈ 1.414；实测比值落在 [1.15, 1.60] 才算接受。"""
    pairs = list(itertools.combinations(arms, 2))

    def _mean_se(sl):
        vals = [v for v in (_se_diff(smap, a, b, sl) for a, b in pairs) if v]
        return float(np.mean(vals)) if vals else float("nan")

    n = len(smap[arms[0]])
    half = n // 2
    full, h1, h2 = _mean_se(None), _mean_se(slice(0, half)), _mean_se(slice(half, n))
    ratio = float(np.mean([h1, h2])) / full if full else float("nan")
    return {"n_periods": n, "se_full_pp": round(full, 3),
            "se_first_half_pp": round(h1, 3), "se_second_half_pp": round(h2, 3),
            "ratio_half_over_full": round(ratio, 3), "theory_sqrt2": round(math.sqrt(2), 3),
            "accept_1_over_sqrtT": bool(1.15 <= ratio <= 1.60)}


def _b5_error_budget(sigma_b: float, sigma_t: float, T0: int = 115) -> list:
    """B5：误差预算 `error(N,T) = √2·σ_total(T)/√N`，σ_total(T)=√(σ_b²+σ_e²/T)。
    分辨准则沿用 `error ≤ Δ/2` ⇒ N = (2√2·σ_total(T)/Δ)²。"""
    sigma_e = sigma_t * math.sqrt(T0)
    rows = []
    for T in (115, 608):
        st = sigma_e / math.sqrt(T)
        tot = math.sqrt(max(sigma_b ** 2 + st ** 2, 0.0))
        for d in (1.0, 2.0, 3.0):
            rows.append({"T": T, "sigma_t_pp": round(st, 3), "sigma_total_pp": round(tot, 3),
                         "target_delta_pp": d,
                         "seed_sets_needed": int(math.ceil((2 * math.sqrt(2) * tot / d) ** 2))})
    return rows


def stage_analyze() -> dict:
    prev = json.loads(OUT.read_text(encoding="utf-8"))
    ann = prev["research"]["per_arm"]
    nav = pd.read_csv(HERE / "F78_nav_series.csv", index_col=0, parse_dates=True)

    def _monthly(df):
        return {c: df[c].pct_change().dropna() for c in df.columns}

    out = {}
    for layer, smap in (("research", None), ("portfolio", _monthly(nav))):
        if smap is None:
            # 研究层：逐期序列已落盘（F78b 补齐）⇒ σ_total / σ_time / σ_b 全都能实测
            rs_path = HERE / "F78_research_series.csv"
            if rs_path.exists():
                rdf = pd.read_csv(rs_path, index_col=0, parse_dates=True)
                rmap = {c: rdf[c].dropna() for c in rdf.columns}
                for grp, kk in (("A", GROUP_A_K), ("B", GROUP_B_K)):
                    st = _group_stats(ann, rmap, grp, kk)
                    if st:
                        st["note"] = ("研究层（分数面板 top100 等权、21 日 forward_excess）；"
                                      "σ_time 为**实测**")
                        out[f"research_{grp}"] = st
                # L1 口径重判「加 K」：研究层分辨率是组合层的 3.6× ⇒ 用它复核 J3
                ra, rb = out.get("research_A"), out.get("research_B")
                if ra and rb and ra["sigma_total_pp"]:
                    r = rb["sigma_total_pp"] / ra["sigma_total_pp"]
                    dm = rb["mean_ann_pp"] - ra["mean_ann_pp"]
                    # 均值差的标准误（两组独立，臂数不等）
                    se_m = math.sqrt(ra["sigma_total_pp"] ** 2 / ra["n_arms"]
                                     + rb["sigma_total_pp"] ** 2 / rb["n_arms"])
                    out["research_K_compare"] = {
                        "mean_A_pp": ra["mean_ann_pp"], "mean_B_pp": rb["mean_ann_pp"],
                        "mean_diff_pp": round(dm, 3), "se_of_diff_pp": round(se_m, 3),
                        "t_of_mean_diff": round(dm / se_m, 3) if se_m else None,
                        "sigma_ratio_B_over_A": round(r, 3),
                        "note": "研究层（L1）复核 K=16 是否**降低方差**且**不损均值**；"
                                "组合层因组 B 仅 3 臂证据不足，此处分辨率高 3.6×"}
                continue
            # 序列缺失 ⇒ 退回只报 σ_total（并注明 σ_t 是借来的）
            arms_a = sorted([a for a in ann if ann[a]["group"] == "A"])
            vals = np.array([ann[a]["ann_pp"] for a in arms_a], float)
            out["research_A"] = {"group": "A", "K": GROUP_A_K, "n_arms": len(vals),
                                 "ann_pp_per_arm": {a: ann[a]["ann_pp"] for a in arms_a},
                                 "mean_ann_pp": round(float(vals.mean()), 3),
                                 "sigma_total_pp": round(float(vals.std(ddof=1)), 3),
                                 "sigma_total_ci95": _sigma_ci(float(vals.std(ddof=1)), len(vals) - 1),
                                 "note": "⚠️ 逐期序列未落盘 ⇒ 只有 σ_total；σ_t **借** 05 号登记的 1.07pp（非实测）"}
            continue
        # 组合层：年化用 sys_ann
        ann_pf = {}
        for r in prev["portfolio"]["runs"]:
            ann_pf[r["arm"]] = {"group": r["group"], "ann_pp": round(100 * float(r["sys_ann"]), 4)}
        out["portfolio_A"] = _group_stats(ann_pf, smap, "A", GROUP_A_K)
        out["portfolio_B"] = _group_stats(ann_pf, smap, "B", GROUP_B_K)

    smap_pf = smap                 # 组合层月度序列（F78b 判据 B4 用）
    ann_pf_all = ann_pf            # 组合层年化（**含探针臂 P**，B3 用）
    A, B = out.get("portfolio_A"), out.get("portfolio_B")
    verdicts = {}
    if A:
        # J1
        verdicts["J1_sigma_total_4"] = {"pp": A["sigma_total_pp"], "ci95": A["sigma_total_ci95"]}
        # J2
        share = A["b_share_of_var"]
        verdicts["J2_structure"] = ("persistent_dominant" if share > 0.50
                                    else "time_dominant" if share < 0.25 else "mixed")
        # J4：独立 seed 设计下 t 的虚高上界
        ratio = math.sqrt(2) * A["sigma_total_pp"] / A["sigma_time_diff_pp"]
        verdicts["J4_t_deflation_upper_bound"] = round(ratio, 3)
        verdicts["J4_vs_F76b"] = {"f76b_observed": 1.77,
                                  "consistent": bool(ratio >= 1.77),
                                  "note": "F76b 是共享种子集设计 ⇒ 其 1.77× 应 ≤ 本上界"}
        # J5
        exp_spread = math.sqrt(2) * A["sigma_time_diff_pp"]
        vals = np.array(list(A["ann_pp_per_arm"].values()), float)
        obs_spread = float(vals.max() - vals.min())
        verdicts["J5_consistency"] = {"expected_pair_spread_pp": round(exp_spread, 2),
                                      "observed_max_minus_min_pp": round(obs_spread, 2)}
    if A and B:
        # J3
        r = B["sigma_total_pp"] / A["sigma_total_pp"] if A["sigma_total_pp"] else float("nan")
        verdicts["J3_K_scaling"] = {"ratio": round(r, 3), "theory": 0.5,
                                    "accept_1_over_sqrtK": bool(abs(r - 0.5) <= 0.20)}
    else:
        verdicts["J3_K_scaling"] = "pending：组 B 尚未回测"

    # ---- F78b 判据 ----
    if A:
        arms_a = sorted(A["ann_pp_per_arm"])
        # B1
        verdicts["B1_sigma_total_12arms"] = {"pp": A["sigma_total_pp"], "ci95": A["sigma_total_ci95"],
                                             "vs_f78": "F78(6臂) 2.271pp [1.418, 5.570]"}
        # B2：CI 两端对应的 σ_b 占比（明确排除了哪一档）
        def _share(s_tot):
            if s_tot is None:                      # CI 缺分位数 ⇒ 不硬算
                return None
            if s_tot <= 0:
                return None
            s_b = math.sqrt(max(s_tot ** 2 - A["sigma_t_pp"] ** 2, 0.0))
            return round(s_b ** 2 / s_tot ** 2, 3)
        lo, hi = A["sigma_total_ci95"]["lo"], A["sigma_total_ci95"]["hi"]
        verdicts["B2_structure"] = {
            "point": A["b_share_of_var"],
            "at_ci_low": _share(lo), "at_ci_high": _share(hi),
            "class": ("persistent_dominant" if A["b_share_of_var"] > 0.50
                      else "time_dominant" if A["b_share_of_var"] < 0.25 else "mixed"),
            "excludes_persistent": bool(hi is not None and _share(hi) < 0.50),
            "excludes_time": bool(lo is not None and _share(lo) > 0.25)}
        # B4：折半检验（免费）
        verdicts["B4_sqrtT_check"] = _b4_sqrtT_check(smap_pf, arms_a)
        # B5：误差预算
        verdicts["B5_error_budget"] = _b5_error_budget(A["sigma_b_pp"], A["sigma_t_pp"])
    # B3：漂移检验（探针 P1/P2）
    if "P1" in ann_pf_all and "P2" in ann_pf_all and A:
        vals = np.array(list(A["ann_pp_per_arm"].values()), float)
        mu, sd = float(vals.mean()), float(vals.std(ddof=1))
        p1, p2 = ann_pf_all["P1"]["ann_pp"], ann_pf_all["P2"]["ann_pp"]
        z1, z2 = (p1 - mu) / sd, (p2 - mu) / sd
        verdicts["B3_drift_test"] = {
            "A_mean_pp": round(mu, 3), "A_sd_pp": round(sd, 3),
            "A_min_pp": round(float(vals.min()), 3), "A_max_pp": round(float(vals.max()), 3),
            "P1_prod_seed_set_pp": p1, "P1_z": round(z1, 2),
            "P1_vs_f76b_baseA_10.16pp": round(p1 - 10.16, 2),
            "P2_f76b_baseB_seed_set_pp": p2, "P2_z": round(z2, 2),
            "P2_vs_f76b_baseB_4.26pp": round(p2 - 4.26, 2),
            "P2_back_in_distribution": bool(abs(z2) <= 2.0),
            "drift_supported": bool(abs(z2) <= 2.0 and p2 > 4.26 + 0.5)}

    for k, v in out.items():
        log.info("%s: %s", k, json.dumps(v, ensure_ascii=False)[:400])
    log.info("判据: %s", json.dumps(verdicts, ensure_ascii=False))
    _merge_json({"analyze": {"layers": out, "verdicts": verdicts}})
    return verdicts


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        datefmt="%H:%M:%S", force=True)
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", required=True,
                    help="pilot / retrain / readout / portfolio / analyze（逗号分隔按序执行）")
    ap.add_argument("--group", default="",
                    help="A / B / AB（默认全跑）；分组执行便于先看组 A 实测成本再决定组 B")
    args = ap.parse_args()
    stages = [s.strip() for s in args.stage.split(",")]
    global GROUP_FILTER
    GROUP_FILTER = {g.strip().upper() for g in args.group.split(",") if g.strip()} or None
    if GROUP_FILTER:
        log.info("本次只跑组 %s（%d 臂）", sorted(GROUP_FILTER), len(_arms()))
    setup_logger()
    db.init_db()
    t0 = time.time()
    for s in stages:
        log.info("===== stage %s =====", s)
        if s == "pilot":
            stage_pilot()
        elif s == "retrain":
            stage_retrain()
        elif s == "readout":
            stage_readout()
        elif s == "portfolio":
            stage_portfolio()
        elif s == "analyze":
            stage_analyze()
        else:
            raise SystemExit(f"未知 stage: {s}")
    log.info("全部 stage 完成 | %.0fs", time.time() - t0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
