"""周频打分: LightGBM(5 日标签) + 月度重训 + 4 种子分位排名均值

口径与 2026-09-12 的实验**逐条同源**(见 `experiments/05_label/F4_weekly_lgb.py`):

- 锚点 = 每个 ISO 周的最后交易日;标签 = 可交易窗口 `open(t+1)→open(t+1+5)`
  相对当日 PIT 票池等权的**超额**;
- **禁运 3 个锚点**:5 日标签的价格窗会跨到下一锚点之后,最近训练锚点必须距预测锚点
  ≥3 个锚点(≈15 个交易日),否则标签窗口与预测期重叠 = 泄漏;
- 月末重训,预测到下一个重训点;训练窗 = 过去 36 个月的周锚点;
- 4 种子 `[42,7,123,2024]`,推分取**分位排名均值**(生产 `trainer` 同款,降种子方差);
- 票池复用 `app.pit.engine.PITEngine.tradable_mask`(生产六层)。

实测(606 周,2016-08~2026-09): IC=+0.0840(t=15.13),死区 +0.0901 / 活跃区 +0.0729;
换种子(11,22,33,44)重训 IC=+0.0843 ⇒ 不是种子运气。

产物:`data/weekly/scores_<asof>.npz`(scores / anchors / cols / names / meta)。
"""
from __future__ import annotations

import logging
import time
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

from app import config as cfg_mod
from app import db
from app.factors import factor_engine as fe
from app.models.trainer import PARAMS
from app.pit.engine import PITEngine
from app.weekly.errors import ArchiveAbsent

log = logging.getLogger(__name__)

HORIZON = 5                 # 周频持有期(交易日)
TRAIN_MONTHS = 36
EMBARGO = 3                 # 禁运锚点数(见模块 docstring 的推导)
SEEDS = (42, 7, 123, 2024)
ROUNDS = 50
N_JOBS = 8                  # 生产机上留出余量给 Web/日链;实验口径用 16
SINCE = "20140101"          # 为 36 个月训练窗留历史
DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "weekly"


# ---------------------------------------------------------------- 锚点/面板
def week_end_dates(dates: pd.DatetimeIndex) -> pd.DatetimeIndex:
    """每个 ISO 周的最大交易日 —— 周频决策锚点(与实验同定义)。"""
    iso = dates.isocalendar()
    key = pd.MultiIndex.from_arrays([np.asarray(iso["year"]), np.asarray(iso["week"])],
                                    names=["iso_year", "iso_week"])
    s = pd.Series(np.asarray(dates), index=key)
    return pd.DatetimeIndex(sorted(s.groupby(level=[0, 1]).max().values))


def build_panel(m: dict, anchors: pd.DatetimeIndex, cfg: dict,
                industry: str = "snapshot") -> dict:
    """在锚点上构造中性化因子面板 + PIT 可交易掩码(与生产 `compute_all` 同源实现)。

    industry="snapshot"(默认) ⇒ `stock_basic.industry` 当前快照(与生产 `factor` 表同口径);
    industry="pit"          ⇒ **申万一级 PIT 时点行业** ⇒ 走 `fe.neutralize_pit`。
      实测(F21/F22, 同窗口 491 期、两套种子): 这个 die 与 W5 按 **2:1** 融合是最优组合
      (+14.91/+14.30pp vs 旧现役 W5+M 的 +12.02/+11.52pp, 回撤持平, IC 0.0773 vs 0.0706)。
    """
    conn = db.get_conn()
    try:
        ind = fe.industry_map(conn)
    finally:
        conn.close()
    ind_pit = None
    if industry == "pit":
        from app.factors.holder_factors import sw_industry_map_pit
        ind_pit = sw_industry_map_pit(anchors, "L1")
        cov = (float(ind_pit.notna().to_numpy().mean())
               if getattr(ind_pit, "size", 0) else 0.0)
        log.info("PIT 行业映射: %s (覆盖 %.0f%%)", getattr(ind_pit, "shape", None), 100 * cov)
    cols = m["close"].columns

    pf = fe.compute_price_factors(m)
    ff = fe.compute_fina_factors(anchors.strftime("%Y%m%d").tolist(),
                                 calendar=pd.to_datetime(m["close"].index).strftime("%Y%m%d").tolist())
    for name in ff:
        ff[name].index = pd.to_datetime(ff[name].index)
    lf = fe.compute_lhb_factors(pd.to_datetime(m["close"].index))

    panels = {}
    for name, df in {**pf, **ff, **lf}.items():
        sub = df.reindex(index=anchors, columns=cols)
        if sub.notna().sum().sum() == 0:
            log.warning("因子 %s 在锚点上全空, 跳过", name)
            continue
        panels[name] = (fe.neutralize_pit(sub, ind_pit) if ind_pit is not None
                        else fe.neutralize(sub, ind)).to_numpy(dtype=np.float32)
    names = sorted(panels)
    F = np.stack([panels[n] for n in names], axis=2) if names else np.zeros((len(anchors), len(cols), 0))
    log.info("周频因子面板: %d 锚点 × %d 股 × %d 因子", *F.shape)

    pit = PITEngine(m=m, cfg=cfg)
    mask = pit.tradable_matrix(anchors, columns=cols).fillna(False)
    return {"anchors": anchors, "cols": list(cols), "F": F, "names": names,
            "mask": mask, "pit": pit, "m": m, "industry": industry}


def forward_excess(m: dict, mask: pd.DataFrame, horizon: int = HORIZON) -> np.ndarray:
    """可交易窗口超额标签(anchors × 股票)。与 F1b 同口径: 不看锚点之后才可得的信息。

    ⚠️ 尺名（2026-09-20 登记）：这里用 `m["open"]` = **未复权**开盘价（P1-1 已按 F73 阶段 A+B
    判否、维持未复权），与 `weekly/health._fwd_excess`（`open×adj_factor`，复权）和 `FROZEN_IC`
    （F22 那批的来历）**不是同一把尺**；差值量级的量化读数已撤回，见 14 号三补二十九·补。
    """
    op = m["open"].copy()
    op.index = pd.to_datetime(op.index)
    fwd = op.shift(-(horizon + 1)) / op.shift(-1) - 1.0
    sub = fwd.reindex(index=mask.index, columns=mask.columns).where(mask)
    return sub.sub(sub.mean(axis=1), axis=0).to_numpy(dtype=np.float32)


def _rank_pct(a: np.ndarray) -> np.ndarray:
    out = np.full(a.shape, np.nan, dtype=np.float64)
    for i in range(a.shape[0]):
        row, ok = a[i], np.isfinite(a[i])
        if ok.sum() > 1:
            out[i, ok] = pd.Series(row[ok]).rank(pct=True).to_numpy()
    return out


# ---------------------------------------------------------------- 逐期 IC（2026-09-13 新增）
def rank_ic_by_anchor(S: np.ndarray, Y: np.ndarray, min_n: int = 200):
    """逐锚点的横截面**秩相关 IC** —— 纯函数，便于单测与落盘。

    为什么要有它（F49 第三方体检暴露的缺口）：体检里 "Sharpe ≈ IC_IR×0.3~0.5" 这条
    **算不了**，因为项目历来只落盘"均值 IC"（0.0773 这种），没有序列 ⇒ 算不出 IC_IR。
    而 IC 序列本来就是现成的：打分 `S` 与标签 `Y`（未来超额）都在面板里。
    ⇒ 打分时顺手算出来落盘，之后任何一次体检都能算 IC_IR，不用重跑回测。

    口径：只用"分数有限 且 标签可用"的股票；有效样本 < `min_n` 的锚点记 NaN
    （最后几个锚点的未来收益尚未实现 ⇒ 天然是 NaN，不硬算、不填 0）。
    """
    S = np.asarray(S, dtype=float)
    Y = np.asarray(Y, dtype=float)
    ic = np.full(S.shape[0], np.nan, dtype=np.float64)
    n_ok = np.zeros(S.shape[0], dtype=np.int32)
    for i in range(S.shape[0]):
        ok = np.isfinite(S[i]) & np.isfinite(Y[i])
        n_ok[i] = int(ok.sum())
        if n_ok[i] >= min_n:
            ic[i] = float(pd.Series(S[i][ok]).corr(pd.Series(Y[i][ok]), method="spearman"))
    return ic, n_ok


def ic_summary(ic: np.ndarray) -> dict:
    """把逐期 IC 汇总成 mean / sd / **IC_IR** —— 供 meta 与第三方体检直接读。"""
    v = np.asarray(ic, dtype=float)
    v = v[np.isfinite(v)]
    if v.size < 2:
        return {}
    sd = float(v.std(ddof=1))
    return {"ic_mean": round(float(v.mean()), 4), "ic_sd": round(sd, 4),
            "ic_ir": (round(float(v.mean()) / sd, 3) if sd > 0 else None),
            "ic_n": int(v.size)}


def ic_or_none(m: dict, mask, S: np.ndarray, horizon: int = HORIZON):
    """算逐期 IC；**失败不阻断打分**，但把原因回传给 meta（不许静默留空）。

    为什么这样设计（2026-09-13）：IC 序列是**诊断产物**，不是打分本身。
    打分链路一旦因为"诊断算不出来"而整条失败，代价远大于收益（19:30 保鲜档容错优先）；
    但"没有 IC"这件事必须自证 —— 所以返回 `(None, 原因)` 让 `save()` 写进 meta，
    而不是让存档看起像"忘了算"。
    """
    try:
        ic, n_ok = rank_ic_by_anchor(S, forward_excess(m, mask, horizon))
        return (ic, n_ok), None
    except Exception as e:                     # 例: 合成面板缺 m["open"]（测试/最小面板）
        log.warning("逐期 IC 计算失败(不阻断打分): %s: %s", type(e).__name__, e)
        return (None, None), f"{type(e).__name__}: {e}"


# ---------------------------------------------------------------- 训练
def train(panel: dict, cfg: dict, seeds=SEEDS, rounds: int = ROUNDS,
          progress=None, horizon: int = HORIZON, tail_weight: float = 0.0,
          tail_quantile: float = 0.80, tail_side: str = "abs",
          permute_labels: bool = False, permute_seed: int = 0,
          params_override: dict = None) -> np.ndarray:
    """月度重训 + 周频打分 → (锚点数 × 股票数) 的分位排名均值矩阵。

    horizon: 标签的前向交易日数(默认 5 = 周频)。**只改"预测多远", 不改调仓频率**;
      多周期集成(5/10/21)时分别训练、再按分位排名平均 —— 见 F14 的实测。

    tail_weight > 0 ⇒ **尾部加权训练**(F29): 组合只持有前 ~2.7%(top100/3700), 而 rmse
      是在**全截面**上优化"平均意义的好" —— 这里把 `|y|`(或 `y`) 最大的那部分训练样本
      权重放大到 `1+tail_weight`, 让损失对准组合真正吃到的尾部。
      `tail_side`: "abs" 双尾 / "pos" 只放大正尾。**默认 0.0 ⇒ 不传 weight, 与原来逐位一致**。

    permute_labels=True ⇒ **置换检验**(默认 False, 规划 §6.3 的无前视审计): 把训练标签在
      **每个锚点内部**打乱(保持该锚点的标签分布与可交易集合不变, 只切断"特征↔标签"的关联)。
      若这样训出来的分数仍有 IC / 组合超额, 说明链路上有泄漏。**只影响训练, 不影响评估标签**。

    params_override(**2026-09-13 新增, 给"把模型训练到最优"用**): 覆盖 `PARAMS` 里的
      LightGBM 超参。**默认 None ⇒ 逐位与原来一致**(生产路径永远传 None)。加这个入口的原因:
      周频模型现在用的 `num_leaves=31 / min_data_in_leaf=200 / lr=0.05 / 固定 50 轮` 是
      **从月频(21 日标签)继承来的**, 而周频吃的是 5 日标签 —— 噪声更大、信号更弱,
      通常要更强正则。没有这个入口就只能去改模块级 `PARAMS`(会动到月频生产)。
    """
    import lightgbm as lgb

    F, names, anchors = panel["F"], panel["names"], panel["anchors"]
    m, mask = panel["m"], panel["mask"]
    A, C, K = F.shape
    if K == 0:
        raise RuntimeError("因子面板为空, 无法训练")
    Y = forward_excess(m, mask, horizon)
    if permute_labels:
        Y = Y.copy()
        rng = np.random.default_rng(permute_seed)
        for i in range(Y.shape[0]):
            row, ok = Y[i], np.isfinite(Y[i])
            if ok.sum() > 1:
                vals = row[ok]
                rng.shuffle(vals)
                row[ok] = vals
        log.warning("置换检验: 训练标签已在每个锚点内打乱(仅用于无前视审计, 不应出现在生产中)")

    seen, is_last = set(), np.zeros(A, dtype=bool)
    for i in range(A - 1, -1, -1):
        p = anchors[i].to_period("M")
        if p not in seen:
            seen.add(p)
            is_last[i] = True
    month_idx = np.where(is_last)[0]
    log.info("月末重训点 %d 个(训练窗 %d 个月, 禁运 %d 锚点, 标签 horizon=%d)",
             len(month_idx), TRAIN_MONTHS, EMBARGO, horizon)

    params = dict(PARAMS)
    if params_override:
        params.update({k: v for k, v in params_override.items() if v is not None})
    params.update(n_jobs=N_JOBS, verbose=-1, deterministic=True)
    S = np.full((A, C), np.nan, dtype=np.float32)
    n_fit, t0 = 0, time.time()
    for mi, j in enumerate(month_idx):
        lo = anchors[j] - pd.DateOffset(months=TRAIN_MONTHS)
        tr = np.where((anchors >= lo) & (np.arange(A) <= j - 1 - EMBARGO))[0]
        if len(tr) < 40:
            continue
        Xtr = F[tr].reshape(-1, K)
        ytr = Y[tr].reshape(-1)
        ok = np.isfinite(ytr) & np.isfinite(Xtr).any(axis=1)
        Xtr, ytr = Xtr[ok], ytr[ok]
        if len(ytr) < 20000:
            continue
        nxt = month_idx[mi + 1] if mi + 1 < len(month_idx) else A
        tgt = np.arange(j, nxt)
        if len(tgt) == 0:
            continue
        wtr = None
        if tail_weight > 0:
            a = np.abs(ytr) if tail_side == "abs" else ytr
            thr = float(np.quantile(a, tail_quantile))
            if np.isfinite(thr):
                wtr = np.where(a >= thr, 1.0 + float(tail_weight), 1.0).astype(np.float32)
        Xte = F[tgt].reshape(-1, K)
        acc = np.zeros((len(tgt), C), dtype=np.float64)
        for s in seeds:
            dtr = lgb.Dataset(Xtr, label=ytr, feature_name=names, weight=wtr)
            bo = lgb.train({**params, "seed": int(s)}, dtr, num_boost_round=rounds)
            acc += _rank_pct(bo.predict(Xte).reshape(len(tgt), C))
        S[tgt] = (acc / len(seeds)).astype(np.float32)
        n_fit += 1
        if progress and n_fit % 10 == 0:
            progress(n_fit, len(month_idx), time.time() - t0)
    log.info("周频打分完成: %d 个模型, %.0fs", n_fit, time.time() - t0)
    return S


# ---------------------------------------------------------------- 读写
def _path(asof: str) -> Path:
    return DATA_DIR / f"scores_{asof}.npz"


def save(asof: str, panel: dict, S: np.ndarray, ic: np.ndarray = None,
         n_by_anchor: np.ndarray = None, ic_skipped: str = None, **info) -> Path:
    """存档打分。

    2026-09-13 修正（独立审查 #6）：原来 meta 写的是**模块常量**（`HORIZON/EMBARGO/
    TRAIN_MONTHS/SEEDS`），而 `run_multi` 连 horizons/权重/die 组合都不记 —— 于是
    「换一组超参/种子/权重跑出来的存档」会**自称是默认配置**，可回溯纪律直接漏掉。
    现在 `info` 里的**本次调用真实参数**优先写入，并把参数指纹一并落盘。
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    p = _path(asof)
    run_info = {"horizon": HORIZON, "embargo": EMBARGO, "train_months": TRAIN_MONTHS,
                "seeds": list(SEEDS), "rounds": ROUNDS}
    run_info.update({k: v for k, v in info.items() if v is not None})
    # 2026-09-13 独立复核抓到：多 horizon 时**单数 `horizon` 是假自述**（会与 `horizons` 矛盾）。
    # 有复数列时以复数为准，删掉单数。
    if "horizons" in run_info:
        run_info.pop("horizon", None)
    # 参数指纹: 名字+值一起哈希(值先转成可比较的字符串, 字典按键排序)
    try:
        payload = json.dumps({k: str(v) for k, v in sorted(run_info.items())},
                             ensure_ascii=False, sort_keys=True)
    except Exception:
        payload = str(run_info)
    run_info["params_sha16"] = hashlib.sha256(payload.encode()).hexdigest()[:16]
    if ic is not None:
        # 逐期 IC 序列 + 摘要（2026-09-13）：序列给第三方体检算 IC_IR，摘要给人一眼看
        run_info.update(ic_summary(ic))
    elif ic_skipped:
        run_info["ic_skipped"] = ic_skipped        # 算不出 IC 时**留痕**（谁都能看见为什么没有）
    arrays = dict(
        scores=S,
        anchors=panel["anchors"].strftime("%Y%m%d").to_numpy(),
        cols=np.array(panel["cols"], dtype=object),
        names=np.array(panel["names"], dtype=object),
        meta=np.array([f"{k}={v}" for k, v in run_info.items()], dtype=object))
    if ic is not None:
        arrays["ic_by_anchor"] = np.asarray(ic, dtype=np.float64)
    if n_by_anchor is not None:
        arrays["n_by_anchor"] = np.asarray(n_by_anchor, dtype=np.int32)
    np.savez_compressed(p, **arrays)
    log.info("周频打分已存 → %s", p)
    return p


def latest() -> tuple:
    """最近一份打分存档 → (S, anchors, cols, names, path)。无存档时抛 ArchiveAbsent。

    2026-09-18 审查 Q-04：改抛带 `ArchiveMiss` 标记的子类，**文案一字不动**、
    且它仍然是 `FileNotFoundError`（`pipeline.py:104` 按这个内建类型抓）。
    自愈链从此只认类型，不再匹配中文字符串。
    """
    files = sorted(DATA_DIR.glob("scores_*.npz"))
    if not files:
        raise ArchiveAbsent(f"没有周频打分存档({DATA_DIR}), 先跑 scripts/run_weekly.py score")
    p = files[-1]
    # 2026-09-28 审查: 用 with 确保 NpzFile 关闭 —— 原 .npz 句柄靠 GC 回收, Windows 上
    # 同进程随后写同名文件时是可收窄的共享模式风险
    with np.load(p, allow_pickle=True) as z:
        return (z["scores"], pd.to_datetime([str(d) for d in z["anchors"]]),
                [str(c) for c in z["cols"]], [str(n) for n in z["names"]], p)


def run(asof: str = None, cfg: dict = None) -> Path:
    """完整链路: 载行情 → 建面板 → 训练打分 → 存档。返回存档路径。"""
    cfg = cfg or cfg_mod.load_config()
    m = fe.load_market(since=SINCE)
    dates = pd.to_datetime(m["close"].index)
    anchors = week_end_dates(dates)
    panel = build_panel(m, anchors, cfg)
    S = train(panel, cfg, progress=lambda i, n, s: log.info("  已训练 %d/%d 期 (%.0fs)", i, n, s))
    (ic, n_ok), ic_err = ic_or_none(m, panel["mask"], S, HORIZON)
    asof = asof or anchors[-1].strftime("%Y%m%d")
    return save(asof, panel, S, ic=ic, n_by_anchor=n_ok, ic_skipped=ic_err,
                horizon=HORIZON, seeds=list(SEEDS), rounds=ROUNDS)


# ---------------------------------------------------------------- 多 die 集成
def production_score_on(anchors: pd.DatetimeIndex, cols) -> pd.DataFrame:
    """生产月频 `model_score` 前向填充到周锚点 —— **只用 ≤ 锚点的最近一期**(无前视)。

    它是与周频 die 真正异构的一路(月度截面 × 21 日标签 × 生产训练器)。
    实测:加入它之后两套种子上**超额 +1.7~3.2pp、回撤 −12pp**(F14/F15)。
    """
    conn = db.get_conn()
    try:
        ms = pd.read_sql("SELECT ts_code, trade_date, score FROM model_score", conn)
    finally:
        conn.close()
    ms["trade_date"] = pd.to_datetime(ms["trade_date"])
    piv = ms.pivot_table(index="trade_date", columns="ts_code", values="score").sort_index()
    return piv.reindex(index=piv.index.union(anchors)).ffill().reindex(index=anchors, columns=cols)


def _mean_rank(*dfs) -> np.ndarray:
    """分位排名均值(与生产"推分取分位排名均值"同口径)。"""
    acc = None
    for d in dfs:
        r = pd.DataFrame(d).rank(axis=1, pct=True)
        acc = r if acc is None else acc + r
    return (acc / len(dfs)).to_numpy(dtype=np.float32)


def run_multi(asof: str = None, cfg: dict = None, horizons=(5,), seeds=SEEDS,
              with_production: bool = False, w5_weight: int = 2,
              pit_weight: int = 1) -> Path:
    """**终端默认打分**:`2 × W5 + 1 × pit` —— 两个 die 的**加权**分位排名平均。

    W5  = 5 日标签 LightGBM, 行业中性化用当前快照(与生产同口径);
    pit = **同一套模型 + 申万一级 PIT 时点行业中性化**(数据 `sw_member`, 映射函数
          `holder_factors.sw_industry_map_pit` 已在 app 内 ⇒ 这是所有候选 die 里**最便宜的一路**)。

    依据(F22, 同窗口 491 期, **两套独立种子各自复现**):

      | 组合 | seedA 超额 | seedB 超额 | MDD | IC |
      |---|---|---|---|---|
      | 旧现役 W5+M(1:1) | +12.02pp | +11.52pp | −31.4/−34.4% | +0.0706 |
      | W5+pit(1:1) | +13.16pp | +12.48pp | −31.8/−32.7% | +0.0713 |
      | **2×W5+pit(本默认)** | **+14.91pp** | **+14.30pp** | −32.3/−32.2% | **+0.0773** |

    ⇒ **+2.8~2.9pp**, 回撤持平, IC 更高。

    同批还证伪了三条(不要重试):
      · `W5+head_vt` 固定配对 **+10.78/+11.31pp**(低于现役) ⇒ F19 里"动态良率 Top2"那 +1.6pp
        是**选择的时间运气**, 不是 head_vt 的质量; 且 head_vt 在月频构造下早被两轴否过。
      · 再叠 M 或 snap 成三路(各 1/3)反而回落(+13.2/+12.1 与 +12.9/+12.2pp)——**加 die 稀释**。
      · `W5+margin/+flow` 远低于现役(新数据族第五次被否)。

    历史依据(第一轮异构, 见 experiments/05_label/F14、F15):

      | 打分 | 全史超额 | MDD |
      |---|---|---|
      | 单 die W5(旧默认) | +9.05 / +8.74pp | −31.6% / −31.5% |
      | W5+W21(两个周频 die) | +9.87 / +9.94pp | −31.2% / −31.0% |
      | **W5+M(本默认)** | **+12.02 / +11.52pp** | −31.4% / −34.4% |
      | W5+W21+M(三 die) | +11.22 / +12.14pp | −30.8% / −31.5% |

    ⇒ M 当年带来 +2.8~3.0pp(现已由 pit 取代); W21 是噪声水平, 默认不并入(传 horizons=(5,21) 可开)。

    ⚠️ **口径警告**:M 只有 2017 年之后的数据, 含 M 的打分在更早的锚点上是 NaN。
    做历史对比时**必须在所有臂的交集窗口上算**(F14 脚本已内置 `common_window`),
    否则会拿"606 期含 2015-2016"与"491 期不含"对比 —— 那是两个不同的历史, 不是两种策略。
    """
    cfg = cfg or cfg_mod.load_config()
    m = fe.load_market(since=SINCE)
    dates = pd.to_datetime(m["close"].index)
    anchors = week_end_dates(dates)
    panel = build_panel(m, anchors, cfg, industry="snapshot")
    panel_pit = build_panel(m, anchors, cfg, industry="pit") if pit_weight > 0 else None
    cols = panel["cols"]
    parts, weights = [], []
    for h in horizons:
        log.info("== 训练 die: horizon=%d ==", h)
        S = train(panel, cfg, seeds=seeds, horizon=h,
                  progress=lambda i, n, s, h=h: log.info("  h%d 已训练 %d/%d (%.0fs)", h, i, n, s))
        parts.append(pd.DataFrame(S, index=anchors, columns=cols)); weights.append(w5_weight)
    if panel_pit is not None:
        log.info("== 训练 die: PIT 行业版(申万一级时点) ==")
        S_pit = train(panel_pit, cfg, seeds=seeds, horizon=HORIZON,
                      progress=lambda i, n, s: log.info("  pit 已训练 %d/%d (%.0fs)", i, n, s))
        parts.append(pd.DataFrame(S_pit, index=anchors, columns=cols)); weights.append(pit_weight)
    if with_production:
        parts.append(production_score_on(anchors, cols))
        weights.append(1)
    log.info("加权分位排名平均: %d 个 die, 权重 %s", len(parts), weights)
    acc, tot = None, 0
    for df, w in zip(parts, weights):
        r = df.rank(axis=1, pct=True) * w
        acc = r if acc is None else acc + r
        tot += w
    S = (acc / tot).to_numpy(dtype=np.float32)
    # 融合分的逐期 IC：口径 = 与 5 日标签(forward_excess, 与 W5 die 同窗口)的秩相关。
    # 注：多 horizon 时这里只对 HORIZON(5) 记 IC —— 融合分是"给组合用的分数"，
    # 组合的持有期就是 HORIZON，用别的期限算会与体检口径不一致。
    (ic, n_ok), ic_err = ic_or_none(m, panel["mask"], S, HORIZON)
    asof = asof or anchors[-1].strftime("%Y%m%d")
    # 存档必须自证: **实际用了哪几路 die + 权重 + 种子 + 各 die 的 horizon** 全部落 meta。
    # 2026-09-13 独立复核抓到：原来 `industry="snapshot+pit"` 是**硬编码**，pit_weight=0 时
    # 会谎报多跑了一路 die ⇒ 现在按真实装配写 dies/industry。
    dies = [f"W5(h={h})" for h in horizons]
    if panel_pit is not None:
        dies.append("pit")
    if with_production:
        dies.append("model_score")
    return save(asof, panel, S, ic=ic, n_by_anchor=n_ok, ic_skipped=ic_err,
                horizons=list(horizons), seeds=list(seeds), rounds=ROUNDS,
                w5_weight=w5_weight, pit_weight=pit_weight, dies=dies,
                with_production=bool(with_production),
                industry=["snapshot"] + (["pit"] if panel_pit is not None else []))
