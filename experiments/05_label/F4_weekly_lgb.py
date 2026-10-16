"""F4 · 周频 LightGBM（5 日标签 + 月度重训 + 周频调仓 + 成本感知）—— 预注册

## 为什么这是最后一个未测的信号层配置

- F1/F2 用的是**秩融合代理**（31 因子等权秩平均 + 过去 IC 定方向），毛超额上限 +8.45pp；
  但生产用的是 LightGBM（非线性、能学因子交互与条件权重），月频上它显著优于秩融合
  （月频对照：模型净 +10.18%/年 vs 秩融合 +7.89%/年）。
- h5 实验只测过「5 日标签 + **月频**调仓」（KGD FAIL），从没测过
  「5 日标签 + **周频**调仓 + 真实费用 + 成本感知选择」。

## 预注册判据（跑数前冻结）

    Q1 模型层：LightGBM 打分在 5 日持有期上的周频 Rank-IC（全史/死区/活跃区），
               与秩融合基线（同口径 IC 由 F1 记录：单因子最高 rev_5=+0.0446, t=9.83）对比。
    Q2 组合层：打分 top-6 周频调仓的**毛/净**超额（2 万 / 20 万账户，真实费用+滑点+整手+
               涨跌停闸门+除权缩放，账户口径与 F1b 逐行同构）。
    通过：净超额 ≥ +10.0pp 且活跃区净超额 > 0。
    Q3 成本感知：加滞回（持仓名 +0.2 截面标准差）后净超额是否改善。

## 训练纪律（防前视）

- 标签 = 可交易窗口 open(t+1)→open(t+1+5) 相对当日 PIT 票池等权的超额（与 F1b 同源）。
- **月末重训**，周频打分：预测月 m 的模型只用 ≤ m 开始前 **36 个月**的周锚点样本。
- **禁运 3 个锚点**：5 日标签会跨到下一锚点之后，最近的训练锚点必须距预测锚点 ≥3 个锚点
  （≈15 个交易日），否则标签窗口与预测期重叠 = 泄漏。
- 参数 = 生产 `trainer.PARAMS`（regression/rmse, 50 轮, num_leaves=31, 4 种子中取 seed=42 单种子；
  多种子只在单种子证明有效后才加，避免把算力花在没希望的方向）。

用法：
    ./.venv/Scripts/python.exe -X utf8 experiments/05_label/F4_weekly_lgb.py --train
    ./.venv/Scripts/python.exe -X utf8 experiments/05_label/F4_weekly_lgb.py --eval
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

import lightgbm as lgb                       # noqa: E402
from app import config as cfg_mod            # noqa: E402
from app.factors import factor_engine as fe  # noqa: E402
from app.pit.engine import PITEngine         # noqa: E402
from app.models.trainer import PARAMS        # noqa: E402

log = logging.getLogger("f4")

OUT = HERE / "F4_weekly_lgb_result.json"
SCORES = HERE / "F4_weekly_lgb_scores.npz"
SINCE = "20140101"           # 为 36 个月训练窗留历史(2017 起可打分)
TRAIN_MONTHS = 36
EMBARGO = 3                  # 禁运锚点数(5 日标签跨期, 防泄漏)
SEED = 42
SEEDS = [42, 7, 123, 2024]      # 生产同款 4 种子(分位排名均值), 降种子方差
HYST = 0.20


def build_dataset(m, cfg, need_factors: bool = True):
    dates = pd.to_datetime(m["close"].index)
    wk = f1.week_end_dates(dates)
    pit = PITEngine(m=m, cfg=cfg)
    cols = m["close"].columns
    mask = f1.tradable_frame(pit, wk, cols)
    if need_factors:
        factors = f1.build_factors(m, wk)
        names = sorted(factors)
        F = np.stack([factors[n].reindex(index=wk, columns=cols).to_numpy(dtype=np.float32)
                      for n in names], axis=2)          # (A, C, K)
    else:
        names, F = [], None
    return dict(dates=dates, wk=wk, pit=pit, cols=list(cols), mask=mask, names=names,
                F=F, Y=None, n_anchor=len(wk))


def _rank_pct(a: np.ndarray) -> np.ndarray:
    """逐行分位排名(0~1, NaN 保持 NaN) —— 与生产「推分取分位排名均值」同口径。"""
    out = np.full(a.shape, np.nan, dtype=np.float64)
    for i in range(a.shape[0]):
        row = a[i]
        ok = np.isfinite(row)
        if ok.sum() > 1:
            out[i, ok] = pd.Series(row[ok]).rank(pct=True).to_numpy()
    return out


def train_scores(ds, cfg, max_fits: int = 0, seeds: list = None,
                 scores_file: Path = None):
    wk, F, Y, names = ds["wk"], ds["F"], ds["Y"], ds["names"]
    A, C, K = F.shape
    lab = f1b.forward_excess_open(ds["_m"], ds["mask"], 5)
    Y = lab.to_numpy(dtype=np.float32)
    ds["Y"] = Y
    log.info("面板 %d 锚点 × %d 股 × %d 因子", A, C, K)
    # 月末重训点(在周锚点序列里的位置)
    mts = set(pd.Series(wk).dt.to_period("M"))
    is_month_end = np.zeros(A, dtype=bool)
    seen = set()
    for i in range(A - 1, -1, -1):
        p = wk[i].to_period("M")
        if p not in seen:
            is_month_end[i] = True
            seen.add(p)
    month_idx = np.where(is_month_end)[0]
    log.info("月末重训点 %d 个", len(month_idx))

    S = np.full((A, C), np.nan, dtype=np.float32)
    seeds = seeds or [SEED]
    params = dict(PARAMS)
    params.update(n_jobs=16, verbose=-1, deterministic=True)
    t0 = time.time()
    n_fit = 0
    for mi, j in enumerate(month_idx):
        # 训练窗: 提交月 j 之前 36 个月内的锚点, 且距 j 至少 EMBARGO 个锚点
        lo_date = wk[j] - pd.DateOffset(months=TRAIN_MONTHS)
        tr = np.where((wk >= lo_date) & (np.arange(A) <= j - 1 - EMBARGO))[0]
        if len(tr) < 40:
            continue
        Xtr = F[tr].reshape(-1, K)
        ytr = Y[tr].reshape(-1)
        ok = np.isfinite(ytr) & np.isfinite(Xtr).any(axis=1)
        Xtr, ytr = Xtr[ok], ytr[ok]
        if len(ytr) < 20000:
            continue
        # 预测到下一个重训点为止
        nxt = month_idx[mi + 1] if mi + 1 < len(month_idx) else A
        tgt = np.arange(j, nxt)
        if len(tgt) == 0:
            continue
        Xte = F[tgt].reshape(-1, K)
        acc = np.zeros((len(tgt), C), dtype=np.float64)
        for s in seeds:
            dtr = lgb.Dataset(Xtr, label=ytr, feature_name=names, free_raw_data=True)
            bo = lgb.train({**params, "seed": s}, dtr, num_boost_round=50)
            acc += _rank_pct(bo.predict(Xte).reshape(len(tgt), C))
        S[tgt] = (acc / len(seeds)).astype(np.float32)
        n_fit += 1
        if max_fits and n_fit >= max_fits:
            log.info("  达到 --max-fits=%d, 提前结束", max_fits)
            break
        if n_fit % 10 == 0 or mi == len(month_idx) - 1:
            log.info("  已训练 %d 期 (锚点 %s, 样本 %d, %.0fs)",
                     n_fit, wk[j].date(), len(ytr), time.time() - t0)
    log.info("训练完成: %d 个模型, %.0fs", n_fit, time.time() - t0)
    scores_file = scores_file or SCORES
    np.savez_compressed(scores_file, scores=S, anchors=wk.strftime("%Y%m%d").to_numpy(),
                        cols=np.array(ds["cols"], dtype=object), names=np.array(names, dtype=object))
    log.info("打分已存 → %s", scores_file)
    return S


def score_ic(ds, S):
    wk = ds["wk"]
    lab = f1b.forward_excess_open(ds["_m"], ds["mask"], ds["_h"])
    Yr = f1._rank(lab)
    Xr = f1._rank(pd.DataFrame(S, index=wk, columns=ds["cols"]))
    ok = lab.notna().to_numpy() & np.isfinite(Xr)
    ic = pd.Series(f1.row_ic(Xr, Yr, ok), index=wk).dropna()
    return ic


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--train", action="store_true")
    ap.add_argument("--eval", action="store_true")
    ap.add_argument("--since", default=SINCE)
    ap.add_argument("--max-fits", type=int, default=0)
    ap.add_argument("--seeds", default=",".join(str(s) for s in SEEDS))
    ap.add_argument("--scores-file", default=str(SCORES))
    ap.add_argument("--confirm", action="store_true",
                    help="只跑预注册确认臂(冻结 M5 构型), 不做全臂扫描")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        datefmt="%H:%M:%S", force=True)
    t0 = time.time()
    cfg = cfg_mod.load_config()
    m = fe.load_market(since=a.since)
    ds = build_dataset(m, cfg, need_factors=a.train)
    ds["_m"], ds["_h"] = m, 5

    if a.train:
        S = train_scores(ds, cfg, a.max_fits, [int(x) for x in a.seeds.split(",") if x.strip()],
                         scores_file=Path(a.scores_file))
    elif a.eval:
        S = None
    else:
        S = None

    if not a.train and not a.eval:
        log.error("请指定 --train 或 --eval")
        return

    if a.eval:
        z = np.load(a.scores_file, allow_pickle=True)
        S = z["scores"]
        saved_anchors = pd.to_datetime([str(d) for d in z["anchors"]])
        saved_cols = [str(c) for c in z["cols"]]
        # 只保留与当前行情面板同锚点、同代码的子集; 其余列由 reindex 自动对齐成 NaN
        keep_a = [i for i, d in enumerate(saved_anchors) if d in set(ds["wk"])]
        keep_c = [i for i, c in enumerate(saved_cols) if c in set(ds["cols"])]
        S = S[np.ix_(keep_a, keep_c)]
        SCORE_INDEX = saved_anchors[keep_a]
        SCORE_COLS = [saved_cols[i] for i in keep_c]
    else:
        ds["F"] = None
        SCORE_INDEX, SCORE_COLS = ds["wk"], ds["cols"]

    # ---- Q1: 打分 IC ----
    ic = score_ic(ds, S)
    res = {"script_sha": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:16],
           "since": a.since, "train_months": TRAIN_MONTHS, "embargo": EMBARGO,
           "n_anchor": int(len(ds["wk"])), "n_scored": int(np.isfinite(S).any(axis=1).sum()),
           "ic": {"FULL": f1b.zone_stat_hac(ic, "DEAD", 2), "DEAD": None, "ACTIVE": None},
           "portfolio": {}, "verdict": {}}
    res["ic"]["DEAD"] = f1b.zone_stat_hac(ic, "DEAD", 2)
    res["ic"]["ACTIVE"] = f1b.zone_stat_hac(ic, "ACTIVE", 2)
    res["ic"]["ALL"] = {"n": int(len(ic)), "ic": round(float(ic.mean()), 4), "t": round(f1b.hac_t(ic.to_numpy(), 2), 2)}
    log.info("模型打分 IC: 全史 n=%d ic=%.4f t=%.2f | DEAD %s | ACTIVE %s",
             len(ic), ic.mean(), f1b.hac_t(ic.to_numpy(), 2), res["ic"]["DEAD"], res["ic"]["ACTIVE"])

    if not a.eval and not a.train:
        return

    # ---- Q2/Q3: 组合 ----
    Smat = pd.DataFrame(S, index=SCORE_INDEX, columns=SCORE_COLS)
    cap0 = float(cfg.get("capital", 20000))
    # 归因四臂(2026-09-12 复盘): 把「费用」与「可交易性闸门」分开, 量化各自值多少
    arms = [("L0_2w", cap0, 0.0, dict()),                      # 全口径(现状)
            ("L0_nogate_2w", cap0, 0.0, dict(no_gates=True)),  # 只关闸门、仍计费
            ("L0_nofee_2w", cap0, 0.0, dict(no_fees=True)),    # 只免费、仍留闸门
            ("L0_gross_2w", cap0, 0.0, dict(gross=True)),      # 全关(原 gross)
            ("L1_hyst_2w", cap0, HYST, dict()), ("L2_hyst_20w", 200000.0, HYST, dict()),
            ("L2b_hyst_5w", 50000.0, HYST, dict()),
            # 20 万账户的归因与宽度实验(2 万每只 2910 元 ⇒ 价格 >29 元的票买不起, 是隐藏约束)
            ("L2_nogate_20w", 200000.0, HYST, dict(no_gates=True)),
            ("L2_nofee_20w", 200000.0, HYST, dict(no_fees=True)),
            ("L3_top10_20w", 200000.0, HYST, dict(top_n=10)),
            ("L4_top20_20w", 200000.0, HYST, dict(top_n=20)),
            ("L5_top50_20w", 200000.0, HYST, dict(top_n=50)),
            # 排名退出带: 买前 N, 卖需掉出 N×band ⇒ 砍换手
            ("L6_band3_top20_20w", 200000.0, HYST, dict(top_n=20, rank_band=3.0)),
            ("L7_band3_top50_20w", 200000.0, HYST, dict(top_n=50, rank_band=3.0)),
            ("L8_band2_top50_20w", 200000.0, HYST, dict(top_n=50, rank_band=2.0)),
            ("L9_band5_top50_20w", 200000.0, HYST, dict(top_n=50, rank_band=5.0)),
            # 带宽 × 资金 × 宽度 的第二轮网格(每只金额 = capital×0.873/top_n; 5 元佣金地板 ~0.29% @1746 元)
            ("M1_band8_top50_20w", 200000.0, HYST, dict(top_n=50, rank_band=8.0)),
            ("M2_band5_top100_50w", 500000.0, HYST, dict(top_n=100, rank_band=5.0)),
            ("M3_band5_top50_50w", 500000.0, HYST, dict(top_n=50, rank_band=5.0)),
            ("M4_band5_top100_100w", 1000000.0, HYST, dict(top_n=100, rank_band=5.0)),
            ("M5_band8_top100_100w", 1000000.0, HYST, dict(top_n=100, rank_band=8.0)),
            ("M6_band5_top200_100w", 1000000.0, HYST, dict(top_n=200, rank_band=5.0))]
    for tag, cap, hy, kw in arms:
        if a.confirm and tag != "M5_band8_top100_100w":
            continue
        df = f1b.run_overlap_portfolio(m, {}, ds["wk"], ds["mask"], 5, {**cfg, "capital": cap},
                                       ds["pit"], 0, score_matrix=Smat, hyst=hy, **kw)
        if df is None or len(df) < 5:
            log.warning("[%s] 无有效期数", tag)
            continue
        op_adj = (m["open"] * m["adj"]).reindex(index=ds["dates"], columns=ds["cols"]).astype(np.float64)
        op_adj_np = op_adj.to_numpy()
        pos_of = {d: i for i, d in enumerate(ds["dates"])}
        mask_np = ds["mask"].to_numpy()
        bench, prev_exec, prev_ap = [], None, None
        for _, r in df.iterrows():
            e = pos_of[r["date"]]
            if prev_exec is None or prev_ap is None:
                bench.append(np.nan)
            else:
                good = (mask_np[int(prev_ap)] & np.isfinite(op_adj_np[prev_exec])
                        & np.isfinite(op_adj_np[e]) & (op_adj_np[prev_exec] > 0))
                p0, p1 = op_adj_np[prev_exec], op_adj_np[e]
                bench.append(float(np.mean(p1[good] / p0[good] - 1)) if good.sum() >= 200 else np.nan)
            prev_exec, prev_ap = e, int(r["anchor_pos"])
        df["bench_ret"] = bench
        okr = df["ret"].notna() & df["bench_ret"].notna()
        af, bf = f1b.ann(df.loc[okr, "ret"], 52.0), f1b.ann(df.loc[okr, "bench_ret"], 52.0)
        nav = (1 + df.loc[okr, "ret"]).cumprod()
        mdd = float((nav / nav.cummax() - 1).min())
        sd = float(df.loc[okr, "ret"].std())
        sharpe = float(df.loc[okr, "ret"].mean() / sd * math.sqrt(52)) if sd > 0 else float("nan")
        res["portfolio"][tag] = {
            "n": int(okr.sum()), "ann": round(af, 4), "bench": round(bf, 4),
            "excess_pp": round(100 * (af - bf), 2),
            "mdd": round(mdd, 4), "sharpe": round(sharpe, 3),
            "turnover": round(float(df.loc[okr, "turnover"].mean()), 3),
            "hit": round(float((df.loc[okr, "ret"] > 0).mean()), 3),
            "DEAD": f1b.zone_ann(df, "DEAD", 52.0), "ACTIVE": f1b.zone_ann(df, "ACTIVE", 52.0)}
        log.info("[%s] n=%d 组合 %.2f%% / 基准 %.2f%% ⇒ 超额 %+.2fpp | MDD %.1f%% Sharpe %.2f | 换手 %.3f 命中 %.3f%s",
                 tag, res["portfolio"][tag]["n"], 100 * af, 100 * bf, res["portfolio"][tag]["excess_pp"],
                 100 * mdd, sharpe, res["portfolio"][tag]["turnover"], res["portfolio"][tag]["hit"],
                 "  " + str(kw) if kw else "")
        log.info("      DEAD %s", res["portfolio"][tag]["DEAD"])
        log.info("      ACTIVE %s", res["portfolio"][tag]["ACTIVE"])

    best = res["portfolio"].get("L1_hyst_2w")
    res["verdict"]["Q2_pass_10pp"] = None if not best else bool(best["excess_pp"] >= 10.0)
    res["verdict"]["Q2_excess_pp"] = None if not best else best["excess_pp"]
    res["verdict"]["Q2_active_excess_pp"] = None if not best else best["ACTIVE"].get("excess_pp")
    res["secs"] = round(time.time() - t0, 1)
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    log.info("== 完成 %.1fs → %s ==", res["secs"], OUT)


if __name__ == "__main__":
    main()
