"""F77 组级消融：27 号 §十二 #5 的正式判决轮（Phase D / P0-3，2026-09-19）

**为什么是它**：31 因子被指控可能存在冗余组。先验（如实写明）：F62 加因子两次被否
+ IC↔top6 反相关四次复现 ⇒ 消融先验偏"纠缠深、单组剔除难有正收益"。价值 = 正式判决，
不是找 alpha。预注册：plans/f77-group-ablation-20260919.md（判据跑前冻结）。

**臂（唯一变量 = 因子组构成，其余 trainer 参数与 F76b/生产逐字同值）**
  G0 = 31 全集      （override 路径基线）
  G1 = 31 − 价量22  （剩 9  = 财务6 + 龙虎榜3）
  G2 = 31 − 财务6   （剩 25）
  G3 = 31 − 龙虎榜3 （剩 28）
  每臂 × 双种子：SEED_A=[42,7,123,2024]（生产）/ SEED_B=[7,2024,99,555]。
  **G0 也走 factors_override**（预注册 §2 设计决策：四臂同路径 ⇒ 组间差不含路径差）；
  生产路径旁证锚 = F76b base_A（生产现役 model_score，只读）。override 面板从
  `_load_factors_wide()` 派生（xs level="factor"）⇒ 与生产加载逐位同源。
  **sanity 门（硬门）**：G0_A vs base_A 分数 Spearman 最低 ≥0.999 且 top100 Jaccard
  均值 ≥0.9，不过 ⇒ 停轮，全轮读数作废重查。
  **偏差修正（2026-09-19，登记于 _result.json）**：v1 门对 base_A FAIL（Spearman min
  0.9583）。逐锚点诊断：2017-2020 共 48 锚点 Spearman 全部 1.0000（逐位一致）⇒ 路径
  无罪；2021+ 才劣化（corr=-0.662）⇒ 生产 model_score 最后训练 2026-09-09，因子表
  09-16~18 被刷新（日频因子/adj_factor 同步日志在案）⇒ 参照过时（数据漂移）。同日
  普通路径（无 override）探针 model_score_ab_pathprobe 重训 116 锚点后：探针 vs G0_A
  Spearman 全部 1.000000（同表状态下两路径逐位一致，override 无罪定案）；探针 vs
  base_A 与 v1 读数完全相同（0.958341/0.989545/0.8081）。**sanity 参照改 = 该探针**
  （唯一差异 = 加载路径，门的原意不变），阈值不变。v1 FAIL 记录保全于
  sanity_v1_stale_ref。

**重训口径**：full=True, rounds=50×4 种子、无早停、train_months=36、holdout_recent=6、
  backtest_start="2017-01-01"、label_neutral=industry。隔离表
  model_score_ab_g{0..3}_{a,b} + importance 同名前缀；归档 data/models/ab_g*/。

**研究层读数（F56/F76b 逐字同源）**：生产 PIT 掩码 → 等权 top100 → 21 日
  forward_excess → 换手 × 真实卖出费率。组贡献 = G0 − Gk（正 = 剔除变差 = 组有正贡献），
  全史判决 / ACTIVE 登记 / DEAD 附注；IC 与头部 100 不作判决。

**判据（冻结，详见预注册 §6）**
  ① 可剔除必要条件（研究层）：双种子 G0−Gk 全史 mean 同号且 ≤0；
  ② 可剔除必要条件（组合层）：full 窗 Δann=Gk−G0 ≥0 且逐月 NAV 配对 t>1.6，
     MDD 轴 Gk ≥ G0；双种子都要过；
  ③ 正贡献宣称：双种子 G0−Gk > 10%×|G0 全史 mean| 且 t>1.6。
  verdict/组：removable（①+②）/ positive（③）/ indeterminate（其余）；
  整体：任一 removable ⇒ group_redundant_evidence，否则 no_removable_group。
  任何结果都不改生产文件、不换 pin、不重生成 golden。

**LOO（登记性筛选）**：G0_A importance top-5 因子，单种子（SEED_A）各重训一臂；
  过筛 = 全史 Δ(loo−G0) > 10%×|G0| 且 t>1.6 ⇒ 升双种子+组合层复验（--stage loo_verify）；
  未过筛只登记。

**成本预案（冻结）**：--stage pilot 计时 3 锚点（backtest_start="2026-06-01"，G0_A 构型）
  ×8 线性外推；8 臂合计 >6h ⇒ 降窗 2019-07（85 期）并登记 deviation。

**产物**：本文件 + F77_group_ablation_result.json（分阶段增量写）+ SHAPE_LEDGER 条目。
  隔离表跑完保留（Phase E 台账引证据）。scripts/oneoff/ 不动。

运行：
  ./.venv/Scripts/python.exe -X utf8 experiments/05_label/F77_group_ablation.py --stage verify
  ... --stage pilot        （计时 3 锚点）
  ... --stage retrain      （8 臂，幂等：表已满期则跳过）
  ... --stage sanity       （G0_A vs 参照锚硬门；偏差修正后 = 同日普通路径探针）
  ... --stage readout      （研究层配对读数 + ①③）
  ... --stage portfolio    （16 回测 + ② + 组/整体 verdict）
  ... --stage loo          （importance top-5 单种子筛选）
  ... --stage loo_verify   （过筛者非空才有实际动作；预期空转）
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
OUT = HERE / "F77_group_ablation_result.json"
log = logging.getLogger("f77")

# ---- 冻结常量（判据见模块 docstring 与预注册，不许跑完再改） ------------------
SEED_A = list(trainer.ENSEMBLE_SEEDS)        # [42, 7, 123, 2024] = 生产
SEED_B = [7, 2024, 99, 555]                  # F56/F76b 同款独立错位集

# 分组（预注册 §1 冻结；verify stage 强制与 factor 表实名集合核对为恰一划分）
G_PV = ("rev_5", "mom_20", "mom_60", "mom_120", "mom_250", "vol_20", "vol_60",
        "skew_20", "ret_max20", "turnover_20", "turnover_chg", "amount_log",
        "illiq", "bias_20", "bias_60", "high_52w", "low_52w", "beta_250",
        "size", "ep", "bp", "dv_ttm")                        # 价量22
G_FINA = ("roe", "netprofit_yoy", "or_yoy", "grossprofit_margin",
          "debt_to_assets", "eps_yoy")                       # 财务6（FINA_FIELDS+eps_yoy）
G_LHB = ("lhb_net20", "lhb_org_net20", "lhb_cnt20")          # 龙虎榜3
GROUPS = {"1": G_PV, "2": G_FINA, "3": G_LHB}                # 组号 → 该组被剔除的因子

# 重训臂: score 表 / importance 表 / 种子 / 被剔除组
def _arms() -> dict:
    arms = {}
    for g in ("0", "1", "2", "3"):
        drop = () if g == "0" else GROUPS[g]   # G0 = 31 全集（drop 为空 ⇒ override 即全集）
        for s, seeds in (("a", SEED_A), ("b", SEED_B)):
            arms[f"g{g}_{s}"] = (f"model_score_ab_g{g}_{s}",
                                 f"model_importance_ab_g{g}_{s}", seeds, drop)
    return arms

# sanity 参照锚（偏差修正 2026-09-19）：原参照 = 生产 model_score（最后训练 09-09），
# 因子表 09-16~18 被刷新 ⇒ 参照过时、门失败系数据漂移而非路径差（证据链见模块 docstring
# 与 _result.json sanity_v1_stale_ref）。改用同日普通路径探针（无 override、SEED_A、同
# 重训口径、隔离表）——与 G0_A 唯一差异 = 加载路径，门的原意（override ≡ 生产加载）在同
# 表状态下直接可判。阈值 SANITY_* 不变。
BASE_A_TABLE = "model_score_ab_pathprobe"    # 同日普通路径重训探针（隔离表，只读参照）
PROD_BACKTEST_START = "2017-01-01"           # 生产口径（F76b 同值）
PILOT_START = "2026-06-01"                   # ⇒ 恰好 3 个全锚点
EXPECTED_PILOT_ANCHORS = 3
PROJ_BUDGET_H = 6.0                          # 8 臂重训合计投影上限（超出 ⇒ 降窗并登记）
REDUCED_START = "2019-07-22"                 # 降窗备选（85 期，评估窗）
TOP_N = 100                                  # 研究层读数口径（F56/F76b 同源）
HORIZON_READOUT = 21                         # 月频持有期（=trainer.HORIZON）
SELL_REF_DATE = "20260914"                   # 卖出费率基准日（F56 同源）
GATE_T = 1.6                                 # 噪声门 t 下界（F70 标尺）
GATE_REL = 0.10                              # 噪声门 Δ 相对基线幅度
WINDOWS = [("prod", "20190722"), ("full", "20170101")]
SANITY_SPEARMAN_MIN = 0.999                  # 硬门：G0_A(override) vs 参照锚
SANITY_JACCARD_MEAN = 0.9
IMPORTANCE_DDL = ("CREATE TABLE IF NOT EXISTS {t} (train_date TEXT, factor TEXT, "
                  "importance REAL, PRIMARY KEY (train_date, factor)) WITHOUT ROWID")
LOO_TOP_K = 5


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


def _override_for(drop: tuple) -> dict:
    """从 _load_factors_wide() 派生 override 面板（与生产加载逐位同源）。

    run() docstring 的调用方责任条款（neutralize 必须先做过）由 factor 表写入口径满足
    —— 生产因子入库前已过 截面去极值 → 行业中性化 → z-score（factor_engine 头注）。
    """
    wide = trainer._load_factors_wide()
    all_names = sorted(set(wide.columns.get_level_values("factor")))
    missing = (set(drop) | set(G_PV) | set(G_FINA) | set(G_LHB)) - set(all_names)
    if missing:
        raise SystemExit(f"override 派生失败：因子表缺 {sorted(missing)}")
    return {f: wide.xs(f, axis=1, level="factor") for f in all_names if f not in drop}


def _backtest_start() -> str:
    """pilot 判定超预算时降窗（deviation 已登记在结果 JSON），否则生产口径。"""
    prev = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {}
    dec = str(prev.get("pilot", {}).get("decision", ""))
    return REDUCED_START if "REDUCED" in dec else PROD_BACKTEST_START


def _require_sanity() -> None:
    """预注册 §2：sanity 门不过/未跑 ⇒ 不得出任何判决。"""
    prev = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {}
    if not prev.get("sanity", {}).get("pass"):
        raise SystemExit("sanity 门未过或未跑 ⇒ 按预注册不得出判决（先跑 --stage sanity）")


# ---- 阶段 1：verify（分组划分硬门 + 实名清单登记） ----------------------------
def stage_verify() -> None:
    with db.conn_ctx() as conn:
        names = sorted(r[0] for r in
                       conn.execute("SELECT DISTINCT factor FROM factor").fetchall())
    frozen = set(G_PV) | set(G_FINA) | set(G_LHB)
    dup = [f for f in G_PV if f in G_FINA or f in G_LHB] + \
          [f for f in G_FINA if f in G_LHB]
    missing = frozen - set(names)
    extra = set(names) - frozen
    ok = (len(names) == 31 and not dup and not missing and not extra
          and len(G_PV) == 22 and len(G_FINA) == 6 and len(G_LHB) == 3)
    log.info("factor 表实名 %d 个 | 冻结清单 %d 个 | 组内重复 %s | 缺 %s | 多 %s",
             len(names), len(frozen), dup or "无", sorted(missing) or "无",
             sorted(extra) or "无")
    log.info("划分核对: %s（价量22/财务6/龙虎榜3 与 13 号清单一致）",
             "恰一划分 ⇒ 通过" if ok else "不通过 ⇒ 拒绝进入后续 stage")
    _merge_json({"group_check": {
        "table_names": names, "n_table": len(names),
        "n_pv": len(G_PV), "n_fina": len(G_FINA), "n_lhb": len(G_LHB),
        "dup": dup, "missing": sorted(missing), "extra": sorted(extra),
        "partition_ok": ok,
        "note": "与 13 号清单一致，无差异" if ok else "与 13 号清单不一致 ⇒ 停轮登记"}})
    if not ok:
        raise SystemExit("分组与 factor 表实况不构成恰一划分 ⇒ 按 verify 硬门停轮")


# ---- 阶段 2：pilot 计时 ------------------------------------------------------
def stage_pilot() -> None:
    st, it = "model_score_ab_g_pilot", "model_importance_ab_g_pilot"
    ensure_score_table(st)
    with db.conn_ctx() as conn:
        conn.execute(IMPORTANCE_DDL.format(t=it))
        conn.commit()
    log.info("构建 G0 override 面板（31 因子全集）…")
    t_build = time.time()
    ov = _override_for(())
    build_s = time.time() - t_build
    log.info("override 就绪（%d 因子）%.0fs", len(ov), build_s)
    t0 = time.time()
    trainer.run(full=True, score_table=st, importance_table=it,
                archive_dir=DATA_DIR / "models" / "ab_g_pilot",
                num_boost_round=trainer.ENSEMBLE_ROUNDS, early_stopping_rounds=0,
                ensemble_seeds=SEED_A, factors_override=ov,
                train_months=36, holdout_recent=6, backtest_start=PILOT_START)
    dt = time.time() - t0
    n = _count_dates(st)
    log.info("pilot: %d 锚点 %.0fs（纯训练）+ override 构建 %.0fs", n, dt, build_s)
    if n != EXPECTED_PILOT_ANCHORS:
        log.warning("pilot 锚点数 %d ≠ 期望 %d ⇒ 投影按实际数外推", n, EXPECTED_PILOT_ANCHORS)
    # 跑完即 DROP（表是计时工具不是证据；模型归档留在 data/models/ab_g_pilot/）
    with db.conn_ctx() as conn:
        conn.execute(f"DROP TABLE IF EXISTS {st}")
        conn.execute(f"DROP TABLE IF EXISTS {it}")
        conn.commit()
    # 投影 = 每臂一次固定构建开销 + 每锚点边际成本 × 全量锚点
    #（F76b 公式把固定开销 ×38 放大，本轮 22M 行 pivot 的构建开销大，必须单列）
    n_full = _expected_anchors(PROD_BACKTEST_START)
    per_arm = build_s + dt / max(n, 1) * n_full
    proj_all = 8 * per_arm
    ok = proj_all / 3600 <= PROJ_BUDGET_H
    log.info("投影: 每臂 %.1f min × 8 = %.1f h ⇒ %s（预算 %.0f h）",
             per_arm / 60, proj_all / 3600, "按计划跑全量" if ok else "超预算 ⇒ 降窗",
             PROJ_BUDGET_H)
    _merge_json({"pilot": {
        "backtest_start": PILOT_START, "n_anchors": n,
        "expected_anchors": EXPECTED_PILOT_ANCHORS, "train_elapsed_s": round(dt, 1),
        "override_build_s": round(build_s, 1),
        "projection_full_per_arm_min": round(per_arm / 60, 1),
        "projection_8arms_h": round(proj_all / 3600, 2),
        "budget_h": PROJ_BUDGET_H,
        "decision": "full_window" if ok else f"REDUCED_WINDOW_{REDUCED_START}(deviation)",
    }})


# ---- 阶段 3：重训 8 臂 -------------------------------------------------------
def stage_retrain() -> dict:
    start = _backtest_start()
    expected = _expected_anchors(start)
    log.info("全量锚点期望 %d（%s 起）", expected, start)
    retr = {}
    for arm, (st, it, seeds, drop) in _arms().items():
        ensure_score_table(st)
        with db.conn_ctx() as conn:
            conn.execute(IMPORTANCE_DDL.format(t=it))
            conn.commit()
        have = _count_dates(st)
        if have >= expected:
            log.info("%s: 表 %s 已有 %d 期 ⇒ 跳过重训（幂等）", arm, st, have)
            retr[arm] = {"table": st, "n_dates": have, "skipped": True}
            continue
        log.info("== 重训 %s ⇒ %s (seeds=%s, 剔除 %d 因子) ==", arm, st, seeds, len(drop))
        t0 = time.time()
        ov = _override_for(drop)
        res = trainer.run(
            full=True, score_table=st, importance_table=it,
            archive_dir=DATA_DIR / "models" / f"ab_{arm}",
            num_boost_round=trainer.ENSEMBLE_ROUNDS, early_stopping_rounds=0,
            ensemble_seeds=seeds, factors_override=ov,
            train_months=36, holdout_recent=6, backtest_start=start)
        retr[arm] = {"table": st, "n_dates": _count_dates(st),
                     "trained": res.get("trained"), "elapsed_min": round((time.time() - t0) / 60, 1),
                     "seeds": seeds, "dropped": list(drop), "skipped": False}
        log.info("%s 完成: %s", arm, retr[arm])
        del ov
    _merge_json({"retrains": retr, "expected_anchors": expected,
                 "backtest_start": start})
    return retr


# ---- 阶段 4：sanity 硬门（G0_A override vs 参照锚，偏差修正后 = 同日普通路径探针）---
def _load_scores(table: str) -> pd.DataFrame:
    with db.conn_ctx() as conn:
        df = pd.read_sql(f"SELECT trade_date, ts_code, score FROM {table}", conn)
    df["trade_date"] = pd.to_datetime(df["trade_date"], format="%Y%m%d")
    return df.pivot_table(index="trade_date", columns="ts_code",
                          values="score").sort_index()


def stage_sanity() -> bool:
    g0 = _load_scores("model_score_ab_g0_a")
    ba = _load_scores(BASE_A_TABLE)
    anchors = g0.index.intersection(ba.index)
    log.info("sanity: G0_A(override) vs %s（参照锚）共同锚点 %d", BASE_A_TABLE, len(anchors))
    sp, jac = [], []
    for d in anchors:
        a = g0.loc[d].dropna()
        b = ba.loc[d].reindex(a.index).dropna()
        a = a.reindex(b.index)
        if len(b) < 200:
            continue
        sp.append(float(a.corr(b, method="spearman")))
        ta, tb = set(a.nlargest(TOP_N).index), set(b.nlargest(TOP_N).index)
        jac.append(len(ta & tb) / len(ta | tb))
    sp_s, jac_s = pd.Series(sp), pd.Series(jac)
    res = {
        "ref_table": BASE_A_TABLE,
        "n_anchors_used": int(len(sp)),
        "spearman_min": round(float(sp_s.min()), 6), "spearman_mean": round(float(sp_s.mean()), 6),
        "jaccard_mean": round(float(jac_s.mean()), 4), "jaccard_min": round(float(jac_s.min()), 4),
        "gate": {"spearman_min": SANITY_SPEARMAN_MIN, "jaccard_mean": SANITY_JACCARD_MEAN},
    }
    res["pass"] = bool(sp_s.min() >= SANITY_SPEARMAN_MIN
                       and jac_s.mean() >= SANITY_JACCARD_MEAN)
    log.info("sanity: Spearman min %.6f (门 %.3f) | top100 Jaccard mean %.4f (门 %.2f) ⇒ %s",
             res["spearman_min"], SANITY_SPEARMAN_MIN, res["jaccard_mean"],
             SANITY_JACCARD_MEAN, "PASS" if res["pass"] else "FAIL")
    _merge_json({"sanity": res})
    if not res["pass"]:
        raise SystemExit("sanity 门不过 ⇒ override 构造≠生产加载，全轮读数作废重查"
                         "（按预注册 §2 不得出任何判决）")
    return True


# ---- 阶段 5：研究层配对读数（F56/F76b 逐字同源） -------------------------------
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


def _research_series(tables: dict) -> tuple:
    """{臂名: score 表} → ({臂名: 日度序列}, anchors, protocol)。读数口径全臂一致。"""
    wides = {}
    for name, tb in tables.items():
        w = _load_scores(tb)
        wides[name] = w
    anchors = pd.DatetimeIndex(sorted(set.intersection(*[set(w.index) for w in wides.values()])))
    cols = sorted(set().union(*[set(w.columns) for w in wides.values()]))
    m = fe.load_market(since=sc.SINCE)
    cfg = cfg_mod.load_config()
    mask = PITEngine(m=m, cfg=cfg).tradable_matrix(anchors, columns=cols).fillna(False)
    Y = sc.forward_excess(m, mask, HORIZON_READOUT)
    sell_rate = fee_mod.effective_rate(25_000.0, "sell", SELL_REF_DATE)
    log.info("面板就绪 | 共同锚点 %d（%s ~ %s）| 掩码均值 %.3f | 卖出费率 %.4f%%",
             len(anchors), anchors[0].date(), anchors[-1].date(),
             float(mask.to_numpy().mean()), sell_rate * 100)
    mask_np = mask.to_numpy(dtype=bool)
    series = {name: pd.Series(
        _top100_series(w.reindex(index=anchors, columns=cols).to_numpy(dtype=float),
                       Y, mask_np, sell_rate).to_numpy(), index=anchors)
        for name, w in wides.items()}
    meta = {"top_n": TOP_N, "horizon": HORIZON_READOUT,
            "sell_rate_bp": round(sell_rate * 1e4, 3), "anchors": len(anchors),
            "window": [str(anchors[0].date()), str(anchors[-1].date())],
            "columns_union": len(cols)}
    return series, anchors, meta


def stage_readout() -> dict:
    _require_sanity()
    arms = _arms()
    tables = {a: v[0] for a, v in arms.items()}
    expected = _expected_anchors(_backtest_start())
    for name, tb in tables.items():
        if _count_dates(tb) < expected:
            raise SystemExit(f"{name} 的表 {tb} 期数不足（期望 {expected}）⇒ 先跑 --stage retrain")
    series, anchors, meta = _research_series(tables)
    zones = pd.Series(zone_of(anchors), index=anchors)

    per_seed = {}
    for g in ("1", "2", "3"):
        conds, gates = {}, {}
        for s in ("a", "b"):
            gk, g0 = series[f"g{g}_{s}"], series[f"g0_{s}"]
            seg = {}
            for seg_name, sel in (("full", pd.Series(True, index=anchors)),
                                  ("ACTIVE", zones == "ACTIVE"), ("DEAD", zones == "DEAD")):
                seg[seg_name] = _paired_stats(g0[sel] - gk[sel])       # 贡献 = G0 − Gk
            full = seg["full"]
            conds[s] = {"segments": seg,
                        "c1_mean_le0": bool(full["mean_pp"] is not None
                                            and full["mean_pp"] <= 0)}
            gates[s] = full
        # ①：双种子全史 mean 同号且 ≤0（各自 ≤0 即等价于"同号且不超 0"）
        c1 = all(v["c1_mean_le0"] for v in conds.values())
        # ③：双种子 Δ>10%×|G0 全史 mean| 且 t>1.6（G0 基线取 seed 对应的 g0_{a,b}）
        c3 = all(
            gates[s]["mean_pp"] is not None
            and gates[s]["mean_pp"] > GATE_REL * abs(
                100 * float(series[f"g0_{s}"].mean()))
            and gates[s]["t"] is not None and gates[s]["t"] > GATE_T
            for s in ("a", "b"))
        per_seed[f"g{g}"] = {"per_seed": conds, "c1_both_seeds": c1, "c3_both_seeds": c3}
        log.info("组 %s: ①可剔(研究层)=%s ③正贡献=%s | seedA 全史贡献 %.4fpp (t=%s) | seedB %.4fpp (t=%s)",
                 g, c1, c3,
                 per_seed[f"g{g}"]["per_seed"]["a"]["segments"]["full"]["mean_pp"],
                 per_seed[f"g{g}"]["per_seed"]["a"]["segments"]["full"]["t"],
                 per_seed[f"g{g}"]["per_seed"]["b"]["segments"]["full"]["mean_pp"],
                 per_seed[f"g{g}"]["per_seed"]["b"]["segments"]["full"]["t"])
    _merge_json({"research": {
        "protocol": {**meta,
                     "segments": "full 判决 / ACTIVE(2024-01~2026-08) 登记 / DEAD 附注",
                     "convention": "贡献 = G0 − Gk（正 = 剔除变差 = 组有正贡献）"},
        "groups": per_seed}})
    return {"research": per_seed}


# ---- 阶段 6：组合层两轴（16 回测 + NAV 配对 t + verdict）-----------------------
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
    _require_sanity()
    arms = _arms()
    rows, navs = [], {}
    t0 = time.time()
    for wname, start in WINDOWS:
        for arm, (st, *_rest) in arms.items():
            m = E.run_backtest(score_table=st, run_tag=f"abg_{wname}_{arm}",
                               start=start, end=None)
            rows.append(_bt_row(m, arm, wname))
            navs[(wname, arm)] = _nav_series(m["run_date"])
            log.info("[%.0fs] %s %s ann=%.2f%% mdd=%.2f%%", time.time() - t0,
                     wname, arm, 100 * m["sys_ann_return"], 100 * m["sys_max_drawdown"])

    prev = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {}
    per_group = {}
    for g in ("1", "2", "3"):
        per_seed, axes_pass = {}, []
        for s in ("a", "b"):
            gk = next(r for r in rows if r["win"] == "full" and r["arm"] == f"g{g}_{s}")
            g0 = next(r for r in rows if r["win"] == "full" and r["arm"] == f"g0_{s}")
            d_ann = (gk["sys_ann"] - g0["sys_ann"]) * 100          # 剔除效应 = Gk − G0
            nk, n0 = navs[("full", f"g{g}_{s}")], navs[("full", f"g0_{s}")]
            common = nk.index.intersection(n0.index)
            diff = nk[common].pct_change().dropna() - n0[common].pct_change().dropna()
            st_ = _paired_stats(diff)
            ann_pass = bool(st_["mean_pp"] is not None and d_ann >= 0
                            and st_["t"] is not None and st_["t"] > GATE_T)
            mdd_pass = bool(gk["sys_mdd"] >= g0["sys_mdd"])
            per_seed[f"seed_{s}"] = {
                "g0_sys_ann": g0["sys_ann"], "gk_sys_ann": gk["sys_ann"],
                "delta_ann_pp": round(d_ann, 3), "paired_monthly": st_,
                "ann_axis_pass": ann_pass,
                "g0_sys_mdd": g0["sys_mdd"], "gk_sys_mdd": gk["sys_mdd"],
                "delta_mdd_pp": round((gk["sys_mdd"] - g0["sys_mdd"]) * 100, 2),
                "mdd_axis_pass": mdd_pass}
            axes_pass.append(ann_pass and mdd_pass)
        c2 = all(axes_pass)
        r = prev.get("research", {}).get("groups", {}).get(f"g{g}", {})
        c1 = bool(r.get("c1_both_seeds"))
        c3 = bool(r.get("c3_both_seeds"))
        verdict = ("removable" if (c1 and c2) else "positive" if c3 else "indeterminate")
        per_group[f"g{g}"] = {"per_seed": per_seed, "c2_both_seeds": c2,
                              "c1_research": c1, "c3_research": c3, "verdict": verdict}
        log.info("组 %s: ②组合层=%s ⇒ verdict=%s", g, c2, verdict)

    any_removable = any(v["verdict"] == "removable" for v in per_group.values())
    overall = "group_redundant_evidence" if any_removable else "no_removable_group"
    note = ("存在可剔除组 ⇒ 27号 §十二#5 按'冗余组有证据'更正（采纳与否=用户拍板+重钉轮）"
            if any_removable else
            "无组可剔除（预期成立）：纠缠深、单组剔除难有正收益 ⇒ 27号 §十二#5 判决落笔")
    _merge_json({"portfolio": {
        "windows": WINDOWS, "runs": rows, "groups": per_group,
        "overall_verdict": overall, "note": note}})
    log.info("overall verdict: %s", overall)
    return overall


# ---- 阶段 7：LOO（importance top-5 单种子筛选，登记性）-------------------------
def stage_loo() -> dict:
    _require_sanity()
    with db.conn_ctx() as conn:
        df = pd.read_sql("SELECT factor, AVG(importance) mi FROM model_importance_ab_g0_a "
                         "GROUP BY factor ORDER BY mi DESC", conn)
    top5 = df["factor"].head(LOO_TOP_K).tolist()
    log.info("G0_A importance top-%d: %s", LOO_TOP_K, top5)
    start = _backtest_start()
    expected = _expected_anchors(start)
    screens = {}
    for f in top5:
        st = f"model_score_ab_loo_{f}"
        it = f"model_importance_ab_loo_{f}"
        ensure_score_table(st)
        with db.conn_ctx() as conn:
            conn.execute(IMPORTANCE_DDL.format(t=it))
            conn.commit()
        if _count_dates(st) < expected:
            log.info("== LOO 重训 -%s (单种子 A) ==", f)
            ov = _override_for((f,))
            trainer.run(full=True, score_table=st, importance_table=it,
                        archive_dir=DATA_DIR / "models" / f"ab_loo_{f}",
                        num_boost_round=trainer.ENSEMBLE_ROUNDS, early_stopping_rounds=0,
                        ensemble_seeds=SEED_A, factors_override=ov,
                        train_months=36, holdout_recent=6, backtest_start=start)
            del ov
        series, anchors, _meta = _research_series({"g0_a": "model_score_ab_g0_a", f: st})
        d = series[f] - series["g0_a"]                                   # 剔除单因子效应
        full = _paired_stats(d)
        gate = GATE_REL * abs(100 * float(series["g0_a"].mean()))
        passed = bool(full["mean_pp"] is not None and full["mean_pp"] > gate
                      and full["t"] is not None and full["t"] > GATE_T)
        screens[f] = {"table": st, "delta_full": full, "gate_pp": round(gate, 4),
                      "pass": passed}
        log.info("LOO -%s: Δ=%.4fpp (门 %.4fpp) t=%s ⇒ %s", f, full["mean_pp"],
                 gate, full["t"], "过筛 ⇒ 需 loo_verify" if passed else "未过筛（只登记）")
    _merge_json({"loo": {"top5": top5, "screens": screens,
                         "any_pass": any(v["pass"] for v in screens.values())}})
    return screens


# ---- 阶段 8：loo_verify（过筛者升双种子 + 组合层复验；先验偏负 ⇒ 预期空转）------
def stage_loo_verify() -> dict:
    prev = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {}
    passers = [f for f, v in prev.get("loo", {}).get("screens", {}).items()
               if v.get("pass")]
    if not passers:
        log.info("LOO 无过筛因子 ⇒ loo_verify 空转（登记性）")
        _merge_json({"loo_verify": {"passers": [], "verdicts": {},
                                    "note": "无过筛因子，无需复验"}})
        return {}
    _require_sanity()
    start = _backtest_start()
    expected = _expected_anchors(start)
    full_start = "20170101" if start == PROD_BACKTEST_START else "20190722"
    # G0 组合层基线（每种子一次，全 passer 共用）
    g0_row, g0_nav = {}, {}
    for s in ("a", "b"):
        m = E.run_backtest(score_table=f"model_score_ab_g0_{s}",
                           run_tag=f"abloo_g0_{s}", start=full_start, end=None)
        g0_row[s] = _bt_row(m, f"g0_{s}", "full")
        g0_nav[s] = _nav_series(m["run_date"])
        log.info("[loo_verify 基线] g0_%s ann=%.2f%% mdd=%.2f%%",
                 s, 100 * m["sys_ann_return"], 100 * m["sys_max_drawdown"])
    verdicts = {}
    for f in passers:
        st_b = f"model_score_ab_loo_{f}_b"
        it_b = f"model_importance_ab_loo_{f}_b"
        ensure_score_table(st_b)
        with db.conn_ctx() as conn:
            conn.execute(IMPORTANCE_DDL.format(t=it_b))
            conn.commit()
        if _count_dates(st_b) < expected:
            log.info("== loo_verify 重训 -%s (种子 B) ==", f)
            ov = _override_for((f,))
            trainer.run(full=True, score_table=st_b, importance_table=it_b,
                        archive_dir=DATA_DIR / "models" / f"ab_loo_{f}_b",
                        num_boost_round=trainer.ENSEMBLE_ROUNDS, early_stopping_rounds=0,
                        ensemble_seeds=SEED_B, factors_override=ov,
                        train_months=36, holdout_recent=6, backtest_start=start)
            del ov
        per_seed = {}
        for s in ("a", "b"):
            st = f"model_score_ab_loo_{f}" if s == "a" else st_b
            # ③ 研究层（基线 = 对应种子的 G0）
            series, _an, _mt = _research_series(
                {"g0": f"model_score_ab_g0_{s}", "loo": st})
            full = _paired_stats(series["loo"] - series["g0"])
            gate = GATE_REL * abs(100 * float(series["g0"].mean()))
            c3_pass = bool(full["mean_pp"] is not None and full["mean_pp"] > gate
                           and full["t"] is not None and full["t"] > GATE_T)
            # ② 组合层（full 窗，基线 = 对应种子的 G0）
            m = E.run_backtest(score_table=st, run_tag=f"abloo_{f}_{s}",
                               start=full_start, end=None)
            row = _bt_row(m, f, "full")
            nav = _nav_series(m["run_date"])
            common = nav.index.intersection(g0_nav[s].index)
            diff = (nav[common].pct_change().dropna()
                    - g0_nav[s][common].pct_change().dropna())
            stt = _paired_stats(diff)
            d_ann = (row["sys_ann"] - g0_row[s]["sys_ann"]) * 100
            ann_pass = bool(stt["mean_pp"] is not None and d_ann >= 0
                            and stt["t"] is not None and stt["t"] > GATE_T)
            mdd_pass = bool(row["sys_mdd"] >= g0_row[s]["sys_mdd"])
            per_seed[f"seed_{s}"] = {"delta_ann_pp": round(d_ann, 3),
                                     "paired_monthly": stt,
                                     "ann_axis_pass": ann_pass,
                                     "mdd_axis_pass": mdd_pass,
                                     "research_c3": {"delta_full": full,
                                                     "gate_pp": round(gate, 4),
                                                     "pass": c3_pass}}
        c2 = all(v["ann_axis_pass"] and v["mdd_axis_pass"] for v in per_seed.values())
        c3 = all(v["research_c3"]["pass"] for v in per_seed.values())
        verdicts[f] = {"per_seed": per_seed, "c2_both_seeds": c2, "c3_both_seeds": c3,
                       "verdict": "loo_positive_candidate" if (c2 and c3)
                       else "indeterminate"}
        log.info("loo_verify -%s: ②=%s ③=%s ⇒ %s", f, c2, c3, verdicts[f]["verdict"])
    _merge_json({"loo_verify": {"passers": passers, "verdicts": verdicts,
                                "note": "过筛者复验完成；任何结果都不改生产/不换 pin"}})
    return verdicts


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        datefmt="%H:%M:%S", force=True)
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", required=True,
                    help="verify / pilot / retrain / sanity / readout / portfolio / loo"
                         " / loo_verify（逗号分隔按序执行）")
    stages = [s.strip() for s in ap.parse_args().stage.split(",")]
    setup_logger()
    db.init_db()
    t0 = time.time()
    for s in stages:
        log.info("===== stage %s =====", s)
        if s == "verify":
            stage_verify()
        elif s == "pilot":
            stage_pilot()
        elif s == "retrain":
            stage_retrain()
        elif s == "sanity":
            stage_sanity()
        elif s == "readout":
            stage_readout()
        elif s == "portfolio":
            stage_portfolio()
        elif s == "loo":
            stage_loo()
        elif s == "loo_verify":
            stage_loo_verify()
        else:
            raise SystemExit(f"未知 stage: {s}")
    log.info("全部 stage 完成 | %.0fs", time.time() - t0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
