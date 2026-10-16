"""F76b 唯一性权重 A/B：P0-1 的判决轮（2026-09-19）

**为什么是它**：F76 实测月频标签并发 c̄=1.0610 > 1.05 门 ⇒ 按 plans/
p0-validation-upgrade-20260919.md Phase A 的冻结规则进 A/B：trainer 已加
`sample_weight_mode` 实验参数（默认 None 生产逐位不变，commit db99789f），
本轮用它在**隔离表**上重训，把"唯一性权重有没有实质改善"落在实测上。

**知情声明（诚实义务）**：写本预注册时已知 F76 的 u 均值 0.9427 / p10 0.8571
⇒ 先验预期"效应极小"。A/B 的目的是把直觉变成测量，判据照抄噪声门、一个字
不放宽（Δ>10%×基线 且 t>1.6，双种子一致 + 组合层两轴）—— 知情也帮不上忙。

**臂（唯一变量 = 种子集 / sample_weight_mode，其余 trainer 参数与生产逐字同值）**
  base_A = `model_score`               生产现役（不重训，只读）        SEED_A, None
  base_B = `model_score_ab_uw_baseb`   重训                            SEED_B, None
  uw_A   = `model_score_ab_uw_a`       重训                            SEED_A, "uniqueness"
  uw_B   = `model_score_ab_uw_b`       重训                            SEED_B, "uniqueness"
  SEED_A = trainer.ENSEMBLE_SEEDS = [42,7,123,2024]（生产）；SEED_B = [7,2024,99,555]
  （F56 同款独立错位集）。重训全走生产口径：full=True, rounds=50×4 种子、无早停、
  train_months=36、holdout_recent=6、backtest_start="2017-01-01"、label_neutral=industry。
  importance 落**隔离表**（不碰生产 model_importance）；模型归档进
  data/models/ab_uw_<arm>/（2026-09-11 事故的 archive_dir 护栏）。

**研究层读数（F56 逐字同源）**：生产 PIT 掩码（PITEngine.tradable_matrix）→
  等权 top100 → 21 日 forward_excess（`sc.forward_excess`, 月频口径）→
  换手 × 真实卖出费率 `fee_mod.effective_rate(25000,'sell','20260914')`。
  配对差 = uw − base（**同种子**配对），分全史 / ACTIVE 段（2024-01~2026-08，
  expert.zone_of 口径；DEAD 段只作描述性附注）。

**组合层读数（exp_die_headvt_bt 惯例）**：每臂 × 两窗全部**现跑**、不引用历史
  run（免 config 漂移）：prod=20190722~（85 期，与 pin 同窗）/ full=20170101~
  （115 期，含 2018 崩盘）。E.run_backtest 全走 config 默认构型（零覆盖）。

**判决（跑前冻结）**
  研究层（全史）：每 seed 的 mean Δ > 10%×|base 全史均值| 且 配对 t > 1.6；
                  双 seed 都过 ⇒ 研究层 PASS。ACTIVE 段只登记不判。
  组合层超额轴（full 窗）：Δsys_ann > 10%×|base sys_ann| 且逐月收益配对 t>1.6；
                  双 seed 都过 ⇒ 超额轴 PASS。
  组合层 MDD 轴（full 窗）：uw sys_mdd ≥ base sys_mdd（回撤负值，越大越浅）；
                  双 seed 都不劣 ⇒ MDD 轴 PASS。两轴标准照抄项目惯例不放宽。
  verdict = "effect_positive"   ⇔ 三层全过（登记为"有实质改善"，**仍不改生产**；
                  采纳与否 = 用户拍板 + 独立预注册的重钉轮，不在本轮范围）；
          = "effect_indistinguishable"（其余一切情形）⇒ P0-1 结清：并发有素材
                  但效应不可辨/不支持，27 号 §五 按"A/B 已做、未见实质改善"更正。
  任何结果都不改生产文件、不换 pin、不重生成 golden。

**成本预案（冻结）**：先 `--stage pilot` 计时 3 锚点（backtest_start="2026-06-01"
  ⇒ 恰好 2026-06/07/08 三个全锚点）线性外推全量（116 锚点）；3 臂合计投影 >6h
  ⇒ 降窗到 2019-07 起（85 期）并在结果 JSON 登记 deviation。组合层 8 个回测
  固定笔数，不设门、跑满。

**产物**：本文件 + `F76b_uniqueness_ab_result.json`（分阶段增量写）。
  隔离表：3×score + 3×importance + pilot 两张（跑完即 DROP）；`scripts/oneoff/` 不动。

运行：
  ./.venv/Scripts/python.exe -X utf8 experiments/05_label/F76b_uniqueness_ab.py --stage pilot
  ... --stage retrain     （重训 3 臂，幂等：表已满期则跳过）
  ... --stage readout     （研究层配对读数）
  ... --stage portfolio   （8 个回测 + NAV 配对 t + 最终判决）
"""
import argparse
import datetime
import io
import json
import logging
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
OUT = HERE / "F76b_uniqueness_ab_result.json"
log = logging.getLogger("f76b")

# ---- 冻结常量（判据见模块 docstring，不许跑完再改） --------------------------
SEED_A = list(trainer.ENSEMBLE_SEEDS)        # [42, 7, 123, 2024] = 生产
SEED_B = [7, 2024, 99, 555]                  # F56 同款独立错位集
BASE_A_TABLE = "model_score"                 # 生产现役（只读）
ARMS = {                                     # 重训臂: score 表 / importance 表 / 种子 / 权重口径
    "base_B": ("model_score_ab_uw_baseb", "model_importance_ab_uw_baseb", SEED_B, None),
    "uw_A":   ("model_score_ab_uw_a",     "model_importance_ab_uw_a",     SEED_A, "uniqueness"),
    "uw_B":   ("model_score_ab_uw_b",     "model_importance_ab_uw_b",     SEED_B, "uniqueness"),
}
PROD_BACKTEST_START = "2017-01-01"           # 生产口径（现役 model_score 同窗）
PILOT_START = "2026-06-01"                   # ⇒ 恰好 3 个全锚点（2026-06/07/08）
EXPECTED_PILOT_ANCHORS = 3
PROJ_BUDGET_H = 6.0                          # 3 臂重训合计投影上限（超出 ⇒ 降窗并登记）
TOP_N = 100                                  # 研究层读数口径（F56 同源）
HORIZON_READOUT = 21                         # 月频持有期（=trainer.HORIZON）
SELL_REF_DATE = "20260914"                   # 卖出费率基准日（F56 同源）
GATE_T = 1.6                                 # 噪声门 t 下界（F70 标尺）
GATE_REL = 0.10                              # 噪声门 Δ 相对基线幅度
WINDOWS = [("prod", "20190722"), ("full", "20170101")]
IMPORTANCE_DDL = ("CREATE TABLE IF NOT EXISTS {t} (train_date TEXT, factor TEXT, "
                  "importance REAL, PRIMARY KEY (train_date, factor)) WITHOUT ROWID")


# ---- 小工具 -----------------------------------------------------------------
def _merge_json(patch: dict) -> dict:
    """分阶段增量写结果文件（读-改-写，各阶段可独立重跑）。"""
    cur = {}
    if OUT.exists():
        cur = json.loads(OUT.read_text(encoding="utf-8"))
    cur.update(patch)
    cur["updated_at"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    OUT.write_text(json.dumps(cur, ensure_ascii=False, indent=1, default=str),
                   encoding="utf-8")
    return cur


def _expected_anchors(backtest_start: str) -> int:
    """trainer 同口径的锚点个数：month_end_dates ≥2014-01-01 再按 backtest_start 截。"""
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
        return {"n": n, "mean_pp": None, "t": None}
    mean, sd = float(d.mean()), float(d.std(ddof=1))
    t = mean / (sd / np.sqrt(n)) if sd > 0 else float("inf")
    return {"n": n, "mean_pp": round(100 * mean, 4), "sd_pp": round(100 * sd, 4),
            "t": round(float(t), 3)}


# ---- 阶段 1：pilot 计时 ------------------------------------------------------
def stage_pilot() -> None:
    st, it = "model_score_ab_uw_pilot", "model_importance_ab_uw_pilot"
    ensure_score_table(st)
    with db.conn_ctx() as conn:
        conn.execute(IMPORTANCE_DDL.format(t=it))
        conn.commit()
    t0 = time.time()
    trainer.run(full=True, score_table=st, importance_table=it,
                archive_dir=DATA_DIR / "models" / "ab_uw_pilot",
                num_boost_round=trainer.ENSEMBLE_ROUNDS, early_stopping_rounds=0,
                ensemble_seeds=SEED_A, sample_weight_mode="uniqueness",
                train_months=36, holdout_recent=6, backtest_start=PILOT_START)
    dt = time.time() - t0
    n = _count_dates(st)
    log.info("pilot: %d 锚点 %.0fs", n, dt)
    # 跑完即 DROP（表是计时工具不是证据；模型归档留在 data/models/ab_uw_pilot/）
    with db.conn_ctx() as conn:
        conn.execute(f"DROP TABLE IF EXISTS {st}")
        conn.execute(f"DROP TABLE IF EXISTS {it}")
        conn.commit()
    per_anchor = dt / max(n, 1)
    proj_arm = per_anchor * 116          # 线性外推全量（保守：固定开销也被放大）
    proj_all = 3 * proj_arm
    ok = proj_all / 3600 <= PROJ_BUDGET_H
    log.info("投影: 每臂 %.1f min × 3 = %.1f h ⇒ %s（预算 %.0f h）",
             proj_arm / 60, proj_all / 3600, "按计划跑全量" if ok else "超预算 ⇒ 降窗",
             PROJ_BUDGET_H)
    _merge_json({"pilot": {
        "backtest_start": PILOT_START, "n_anchors": n, "elapsed_s": round(dt, 1),
        "projection_full_116_per_arm_min": round(proj_arm / 60, 1),
        "projection_3arms_h": round(proj_all / 3600, 2),
        "budget_h": PROJ_BUDGET_H,
        "decision": "full_window" if ok else "REDUCED_WINDOW_2019_07(deviation)",
    }})


# ---- 阶段 2：重训 3 臂 -------------------------------------------------------
def stage_retrain() -> dict:
    expected = _expected_anchors(PROD_BACKTEST_START)
    log.info("全量锚点期望 %d（%s 起）", expected, PROD_BACKTEST_START)
    retr = {}
    for arm, (st, it, seeds, swm) in ARMS.items():
        # 建表前置（幂等）：首跑时表还不存在，计数查询必须放在 ensure 之后
        ensure_score_table(st)
        with db.conn_ctx() as conn:
            conn.execute(IMPORTANCE_DDL.format(t=it))
            conn.commit()
        have = _count_dates(st)
        if have >= expected:
            log.info("%s: 表 %s 已有 %d 期 ⇒ 跳过重训（幂等）", arm, st, have)
            retr[arm] = {"table": st, "n_dates": have, "skipped": True}
            continue
        log.info("== 重训 %s ⇒ %s (seeds=%s, weight=%s) ==", arm, st, seeds, swm)
        t0 = time.time()
        res = trainer.run(
            full=True, score_table=st, importance_table=it,
            archive_dir=DATA_DIR / "models" / f"ab_uw_{arm}",
            num_boost_round=trainer.ENSEMBLE_ROUNDS, early_stopping_rounds=0,
            ensemble_seeds=seeds, sample_weight_mode=swm,
            train_months=36, holdout_recent=6, backtest_start=PROD_BACKTEST_START)
        retr[arm] = {"table": st, "n_dates": _count_dates(st),
                     "trained": res.get("trained"), "elapsed_min": round((time.time() - t0) / 60, 1),
                     "seeds": seeds, "sample_weight_mode": swm, "skipped": False}
        log.info("%s 完成: %s", arm, retr[arm])
    _merge_json({"retrains": retr, "expected_anchors": expected})
    return retr


# ---- 阶段 3：研究层配对读数（F56 同源） --------------------------------------
def _load_scores(table: str) -> pd.DataFrame:
    with db.conn_ctx() as conn:
        df = pd.read_sql(f"SELECT trade_date, ts_code, score FROM {table}", conn)
    df["trade_date"] = pd.to_datetime(df["trade_date"], format="%Y%m%d")
    return df.pivot_table(index="trade_date", columns="ts_code",
                          values="score").sort_index()


def _top100_series(S: np.ndarray, Y: np.ndarray, mask_np: np.ndarray,
                   sell_rate: float) -> pd.Series:
    """F56 逐字同源的 top100 等权净超额序列（换手 × 卖出费率）。"""
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
    tables = {"base_A": BASE_A_TABLE, **{a: v[0] for a, v in ARMS.items()}}
    wides = {}
    for name, tb in tables.items():
        w = _load_scores(tb)
        if len(w) < expected:
            raise SystemExit(f"{name} 的表 {tb} 只有 {len(w)} 期（期望 {expected}）"
                             f" ⇒ 先跑 --stage retrain")
        wides[name] = w
    anchors = sorted(set.intersection(*[set(w.index) for w in wides.values()]))
    anchors = pd.DatetimeIndex(anchors)
    cols = sorted(set().union(*[set(w.columns) for w in wides.values()]))
    col_n = {k: int(v.shape[1]) for k, v in wides.items()}
    log.info("共同锚点 %d 期（%s ~ %s）| 列并集 %d（各表 %s）",
             len(anchors), anchors[0].date(), anchors[-1].date(), len(cols), col_n)

    m = fe.load_market(since=sc.SINCE)
    cfg = cfg_mod.load_config()
    mask = PITEngine(m=m, cfg=cfg).tradable_matrix(anchors, columns=cols).fillna(False)
    Y = sc.forward_excess(m, mask, HORIZON_READOUT)
    sell_rate = fee_mod.effective_rate(25_000.0, "sell", SELL_REF_DATE)
    log.info("面板就绪 | 掩码均值 %.3f | 卖出费率 %.4f%%",
             float(mask.to_numpy().mean()), sell_rate * 100)

    mask_np = mask.to_numpy(dtype=bool)
    series = {name: _top100_series(w.reindex(index=anchors, columns=cols).to_numpy(dtype=float),
                                   Y, mask_np, sell_rate)
              for name, w in wides.items()}
    series = {k: pd.Series(v.to_numpy(), index=anchors) for k, v in series.items()}
    zones = pd.Series(zone_of(anchors), index=anchors)

    per_seed = {}
    for seed, (bname, uname) in {"A": ("base_A", "uw_A"), "B": ("base_B", "uw_B")}.items():
        b, u = series[bname], series[uname]
        seg = {}
        for seg_name, sel in (("full", pd.Series(True, index=anchors)),
                              ("ACTIVE", zones == "ACTIVE"), ("DEAD", zones == "DEAD")):
            bs, us_ = b[sel], u[sel]
            seg[seg_name] = {
                "base_mean_pp": round(100 * float(bs.mean()), 4),
                "uw_mean_pp": round(100 * float(us_.mean()), 4),
                **_paired_stats(us_ - bs)}
        gate = seg["full"]
        thr = GATE_REL * abs(gate["base_mean_pp"])
        gate["gate_delta_pp"] = round(thr, 4)
        gate["pass"] = bool(gate["mean_pp"] is not None and gate["mean_pp"] > thr
                            and gate["t"] is not None and gate["t"] > GATE_T)
        per_seed[f"seed_{seed}"] = {"base": bname, "uw": uname, "segments": seg,
                                    "gate_full_pass": gate["pass"]}
        log.info("seed %s 全史 Δ=%.4fpp (门 %.4fpp) t=%s ⇒ %s",
                 seed, gate["mean_pp"], thr, gate["t"], "PASS" if gate["pass"] else "否")

    research_pass = all(v["gate_full_pass"] for v in per_seed.values())
    _merge_json({"research": {
        "protocol": {"top_n": TOP_N, "horizon": HORIZON_READOUT,
                     "sell_rate_bp": round(sell_rate * 1e4, 3),
                     "anchors": len(anchors), "window": [str(anchors[0].date()),
                                                         str(anchors[-1].date())],
                     "columns_union": len(cols), "columns_per_table": col_n,
                     "segments": "full 判决 / ACTIVE(2024-01~2026-08) 登记 / DEAD 附注"},
        "per_seed": per_seed, "research_pass": research_pass}})
    return {"research_pass": research_pass}


# ---- 阶段 4：组合层两轴（8 回测 + NAV 配对 t）--------------------------------
def _nav_series(run_date: str) -> pd.Series:
    with db.conn_ctx() as conn:
        df = pd.read_sql("SELECT trade_date, sys_nav FROM backtest_nav "
                         "WHERE run_date=? ORDER BY trade_date", conn, params=[run_date])
    s = pd.Series(df["sys_nav"].to_numpy(dtype=float),
                  index=pd.to_datetime(df["trade_date"]))
    return s[~s.index.duplicated(keep="last")].sort_index()


def _bt_row(m: dict, arm: str, win: str) -> dict:
    return {"arm": arm, "win": win, "run": m["run_date"], "n": m.get("periods"),
            "sys_ann": m["sys_ann_return"], "sys_mdd": m["sys_max_drawdown"],
            "sys_mdd_daily": m["sys_mdd_daily"], "sharpe": m.get("sharpe"),
            "full_ann": m.get("ann_return"), "full_mdd": m.get("max_drawdown"),
            "idle": m.get("avg_cash_idle"), "n_avg": m.get("avg_stocks")}


def stage_portfolio() -> str:
    bt_arms = [("base_A", BASE_A_TABLE)] + [(a, v[0]) for a, v in ARMS.items()]
    rows, navs = [], {}
    t0 = time.time()
    for wname, start in WINDOWS:
        for arm, table in bt_arms:
            m = E.run_backtest(score_table=table, run_tag=f"abuw_{wname}_{arm}",
                               start=start, end=None)
            rows.append(_bt_row(m, arm, wname))
            navs[(wname, arm)] = _nav_series(m["run_date"])
            log.info("[%.0fs] %s %s ann=%.2f%% mdd=%.2f%%", time.time() - t0,
                     wname, arm, 100 * m["sys_ann_return"], 100 * m["sys_max_drawdown"])

    per_seed, full_rows = {}, [r for r in rows if r["win"] == "full"]
    for seed, (bname, uname) in {"A": ("base_A", "uw_A"), "B": ("base_B", "uw_B")}.items():
        b = next(r for r in full_rows if r["arm"] == bname)
        u = next(r for r in full_rows if r["arm"] == uname)
        d_ann = (u["sys_ann"] - b["sys_ann"]) * 100
        thr = GATE_REL * abs(b["sys_ann"]) * 100
        nb = _nav_series(b["run"])
        nu = _nav_series(u["run"])
        common = nb.index.intersection(nu.index)
        diff = nu[common].pct_change().dropna() - nb[common].pct_change().dropna()
        st_ = _paired_stats(diff)
        ann_pass = bool(st_["mean_pp"] is not None and d_ann > thr
                        and st_["t"] is not None and st_["t"] > GATE_T)
        mdd_pass = bool(u["sys_mdd"] >= b["sys_mdd"])
        per_seed[f"seed_{seed}"] = {
            "base": bname, "uw": uname,
            "base_sys_ann": b["sys_ann"], "uw_sys_ann": u["sys_ann"],
            "delta_ann_pp": round(d_ann, 3), "gate_delta_pp": round(thr, 3),
            "paired_monthly": st_, "ann_axis_pass": ann_pass,
            "base_sys_mdd": b["sys_mdd"], "uw_sys_mdd": u["sys_mdd"],
            "delta_mdd_pp": round((u["sys_mdd"] - b["sys_mdd"]) * 100, 2),
            "mdd_axis_pass": mdd_pass}
        log.info("seed %s: Δ年化 %+.2fpp (门 %.2fpp) t=%s | ΔMDD %+.2fpp ⇒ 超额轴 %s / MDD %s",
                 seed, d_ann, thr, st_["t"], per_seed[f"seed_{seed}"]["delta_mdd_pp"],
                 "PASS" if ann_pass else "否", "PASS" if mdd_pass else "否")

    prev = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {}
    research_pass = bool(prev.get("research", {}).get("research_pass"))
    axes = all(v["ann_axis_pass"] and v["mdd_axis_pass"] for v in per_seed.values())
    if "research" not in prev:
        verdict = None       # 研究层未跑：只出组合层数据，不判
        note = "研究层未跑 ⇒ verdict 待 --stage readout 后汇总"
    else:
        verdict = ("effect_positive" if (research_pass and axes)
                   else "effect_indistinguishable")
        note = ("三层全过 ⇒ 登记'有实质改善'，仍不改生产（采纳 = 用户拍板 + 独立重钉轮）"
                if verdict == "effect_positive"
                else "P0-1 结清：并发 c̄=1.061 有素材，但 A/B 未见超噪声门的实质改善")
    _merge_json({"portfolio": {
        "windows": WINDOWS, "runs": rows, "per_seed": per_seed,
        "portfolio_pass": axes, "verdict": verdict, "note": note}})
    log.info("verdict: %s", verdict)
    return verdict or "pending"


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        datefmt="%H:%M:%S", force=True)
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", required=True,
                    help="pilot / retrain / readout / portfolio（逗号分隔按序执行）")
    stages = [s.strip() for s in ap.parse_args().stage.split(",")]
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
        else:
            raise SystemExit(f"未知 stage: {s}")
    log.info("全部 stage 完成 | %.0fs", time.time() - t0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
