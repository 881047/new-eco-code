"""Ensemble → Pure Alpha Score —— 多个 die 的封装融合(2026-09-11)

对应用户框架图的 `(LightGBM | Transformer) → Ensemble → Pure Alpha Score`。

## 为什么不是等权 —— 这是本项目已经花代价学到的

方案 §10c 把「等权秩平均」列为**不拿走**的项, 理由是实测的: 融合后死区 top6 从
+0.50% 升到 +0.76%, **但活跃区 top6 从 +3.17% 掉到 +0.76%** —— 而活跃区是全部收益
来源。同一条读数在 `exp_fusion_router.py` 里被三路复现(R0 等权 / R1 门控 / R2 相对
动量, 三者**无一能在活跃区追平基线**)。

等权失败的原因不是"权重不对", 是**它假设各 die 的分数在同一标度上可比**。它们不可比:
`die_flow` 的分数分布、覆盖域、量纲都与基线不同, 秩平均等于默认"每个 die 的 top-6
都值得占 1/N 的仓位"。**稀疏覆盖的 die(如只覆盖两融标的的 `margin_net_share`)
在这个假设下会被系统性高估。**

## 本模块的做法: out-of-fold stacking + LambdaRank

    die 分数(各自 walk-forward 产出, 天然 out-of-fold) → 截面分位归一(NoC 总线)
        → **按月 walk-forward** 训练 LambdaRank 元学习器 → Pure Alpha Score

三处关键设计:

1. **元学习器的训练也是 walk-forward**, 窗口用 `trainer._train_window()` —— 与生产模型
   **同一个函数**, 不另写一份。评分月 i 只用 i 之前已实现的读数训练。
2. **LambdaRank 而不是回归**: 策略买 top-6, 在意的是**截面内的排序**, 不是收益的绝对值。
   回归盯着 MSE, 会被中部几千只股票的噪声主导; LambdaRank 直接优化 NDCG 式的排序目标。
   (这与 KGD 主判据用 `head_excess` 而非 IC 是同一个理由。)
3. **标度归一是显式的**: 每个 die 每月转成截面分位(0~100), 缺分的 die 对该股票
   **让权**(NaN), 由 LightGBM 原生处理缺失 —— 而不是把 NaN 填成中位数(那会伪造"看多")。

## 与「R1 电源门控」的区别(为什么这个不是追涨杀跌)

R1 是**按 die 的滚动已实现表现决定供不供电** —— 那个被证伪了, 因为滚动表现是
**追涨杀跌**的信号(die_flow 2023 打出 +6.93%/月 ⇒ 一路供电 ⇒ 正好供电进 2024-26
它最差的那段)。

本模块做的是**学习一个条件组合**: 元学习器看的是"在什么情况下哪个 die 的分数更可信",
训练目标是**未来的截面排序**, 不是 die 过去的收益。两者在形式上不同, 但——

⚠️ **这不构成"它一定有用"的论证。** 元学习器的参数量远小于 31 因子 × 数百期的样本,
但本项目已有 4 个「读数好看却对策略无用」的反例, 且**有效验证样本只有 24 个月**
(超随机 edge 全部来自 2024-25)。**判决只能是组合层两轴**(`exp_die_stage_b.py` 的
预注册), 本模块只负责把分数算出来。

## 硬规则

- **只读 die 表 + 写 `model_score_ens`**(实验表名可配), **绝不碰 `model_score`**
- **不提供任何按近期表现调权重/复活的接口** —— 那件事在 `bank.py` 与 `exp_fusion_router.py`
  里已被证伪并写进代码约束
- 元学习器的训练窗口与生产模型**共用 `trainer._train_window`**, 避免"融的时候用一套
  纪律、判的时候用另一套"
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from app import db
from app.models import expert, trainer

log = logging.getLogger(__name__)

HORIZON = trainer.HORIZON          # 21 交易日, 与生产同口径
TRAIN_MONTHS = 36                  # 与生产默认一致(能在噪声里活下来的唯一窗口长度)
N_LABELS = 5                       # LambdaRank 的相关性档位数(按月分位)
PCT_SCALE = 100.0                  # 分位标度上界(NoC 总线: 所有 die 归一到 0~100)


# ---------------------------------------------------------------------------
# 特征: 截面分位归一(NoC 总线)
# ---------------------------------------------------------------------------
def to_percentile(wide: pd.DataFrame) -> pd.DataFrame:
    """把一张打分表按月转成**截面分位**(0~100)。NaN 保持 NaN(缺分 = 让权, 不填 0)。

    这是芯片架构里的"片上网络统一总线": 各 die 的原始分数量纲不同(有的是概率、
    有的是回归值、有的只在子域上有定义), 直接平均没有意义。分位化后它们**可比**。
    """
    out = wide.copy().astype("float64")
    for dt in out.index:
        row = out.loc[dt]
        ok = row.notna()
        if ok.sum() < 2:
            continue
        out.loc[dt, ok] = row[ok].rank(pct=True) * PCT_SCALE
    return out


def _to_long(wide: pd.DataFrame, name: str) -> pd.Series:
    """宽表 → 长表(MultiIndex `[trade_date, ts_code]`), **NaN 原样保留**。

    ⚠️ 这里**刻意不用 `stack()`**, 而是 numpy 展平 + `from_product` 显式造索引:
    NaN 在本模块里是**语义载体**("这个 die 在这个月不覆盖这只股票" ⇒ 让权), 丢了它
    LightGBM 会把缺失当成一种被观测到的取值。而 `stack()` 的 NA 行为在 pandas 2→3
    之间**翻过面**(2.x 丢、3.x 保留, 见 `scripts/oneoff/diag_allnan_samples.py`) ——
    把语义押在版本默认值上, 正是 `write_scores()` 栽的那个坑(它赌 3.x 会丢, 结果没丢,
    落了 39,924 行空分)。`from_product` 造出的笛卡尔积**必然含全部格子**, 与版本无关。

    注: 本段方向原先也写反了(写成"3.x 不再引入 NA 行")。同一个文件里两份互相矛盾的
    注释正是那个 bug 能溜进来的原因 —— 2026-09-11 订正。
    """
    idx = pd.MultiIndex.from_product([wide.index, wide.columns],
                                     names=["trade_date", "ts_code"])
    return pd.Series(wide.to_numpy(dtype="float64").ravel(), index=idx, name=name)


def build_features(die_tables: dict, start: str = None, end: str = None) -> pd.DataFrame:
    """读多张 die 表 → 长表 `[trade_date, ts_code, <die1>, <die2>, ...]`。

    `die_tables`: {列名: 表名}。列名就是元学习器看到的特征名。
    """
    out = None
    for col, tab in die_tables.items():
        wide = expert.load_scores(tab, start=start, end=end)
        if wide.empty:
            log.warning("die 表 %s(%s) 为空, 该特征全 NaN", tab, col)
            continue
        s = _to_long(to_percentile(wide), col)
        out = s.to_frame() if out is None else out.join(s, how="outer")
    return out.dropna(how="all") if out is not None else pd.DataFrame()


# ---------------------------------------------------------------------------
# 标签: 前向收益 → 截面分位档(lambdarank 需要非负整数相关性标签)
# ---------------------------------------------------------------------------
def relevance_labels(fwd: pd.DataFrame, md: pd.DatetimeIndex) -> pd.Series:
    """逐月把前向收益切成 `N_LABELS` 档整数标签(越大越好)。

    逐月切而非全局切: lambdarank 的 NDCG 是**每个 query(=每个月)**内部算的, 标签的
    绝对尺度不影响组内排序, 但全局切会让某些月份所有股票落到同一档 ⇒ 该组无信息。
    """
    parts = []
    for dt in md:
        if dt not in fwd.index:
            continue
        r = fwd.loc[dt].dropna()
        if len(r) < N_LABELS * 2:
            continue
        try:
            lab = pd.qcut(r.rank(method="first"), N_LABELS, labels=False)
        except ValueError:
            continue
        parts.append(pd.DataFrame({"trade_date": dt,
                                   "ts_code": lab.index.to_numpy(),
                                   "y": lab.to_numpy(dtype="float64")}))
    if not parts:
        return pd.Series(dtype="float64")
    out = pd.concat(parts, ignore_index=True).set_index(["trade_date", "ts_code"])["y"]
    # 直接给单层索引赋两个名字会 ValueError; 必须经 MultiIndex 再统一 level 0 的类型
    out.index = out.index.set_levels(
        pd.to_datetime(out.index.levels[0]), level=0).set_names(
        ["trade_date", "ts_code"])
    return out


# ---------------------------------------------------------------------------
# 元学习器
# ---------------------------------------------------------------------------
def _lgb_params(seed: int = 20260911) -> dict:
    return {
        "objective": "lambdarank",
        "metric": "ndcg",
        "ndcg_eval_at": [6, 20],       # 策略只买 6 只 ⇒ 先看 @6
        "label_gain": [0, 1, 3, 7, 15],
        "learning_rate": 0.05,
        "num_leaves": 15,              # 刻意小: 特征只有几个 die, 大了必过拟合
        "min_data_in_leaf": 200,
        "feature_fraction": 0.9,
        "bagging_fraction": 0.8,
        "bagging_freq": 1,
        "lambda_l2": 5.0,
        "verbose": -1,
        "seed": seed,
        "num_threads": 4,
    }


def stack(die_tables: dict, fwd: pd.DataFrame, md: pd.DatetimeIndex,
          train_months: int = TRAIN_MONTHS, min_train_months: int = 12,
          n_rounds: int = 300, seed: int = 20260911,
          feature_names: list = None) -> tuple:
    """out-of-fold stacking: 按月 walk-forward 产出融合分。

    返回 `(pure_score_wide, diag)`:
      - `pure_score_wide`: index=评分月, columns=ts_code, 值 = 融合分(分位标度 0~100)
      - `diag`: 每期的训练样本数 / NDCG@6 / 用到的月份区间

    **纪律**: 训练窗口用 `trainer._train_window(md_all, i, train_months)` —— 与生产模型
    同一个函数。评分月 i 的元学习器**没见过** i 及之后的任何一格。
    """
    import lightgbm as lgb

    feats = build_features(die_tables)
    if feats.empty:
        raise ValueError("没有任何 die 分数可用 —— 检查 die 表名")
    labels = relevance_labels(fwd, md)
    if labels.empty:
        raise ValueError("标签为空 —— 检查 fwd 与 md")

    cols = feature_names or [c for c in feats.columns]
    md_all = pd.DatetimeIndex(md)
    scores, diag = {}, []
    for i, dt in enumerate(md_all):
        if dt not in labels.index.get_level_values("trade_date"):
            continue
        tw = trainer._train_window(md_all, i, train_months)
        tw = [d for d in tw if d in labels.index.get_level_values("trade_date")]
        if len(tw) < min_train_months:
            continue
        tr_idx = labels.index[labels.index.get_level_values("trade_date").isin(tw)]
        # 2026-09-28 审查(36补): group 边界与行序强绑定 —— groupby(level) 按键排序取组
        # 大小, 而行序原跟随 labels 拼接序(隐含"天然有序"假设, md 乱序传入即静默喂错组)。
        # 显式 sort_index 后两者必然一致; 对现产(已有序)是 no-op。
        Xtr = feats.reindex(tr_idx)[cols].sort_index()
        ytr = labels.reindex(Xtr.index).to_numpy()
        ok = ~np.isnan(ytr)
        if ok.sum() < 500:
            continue
        Xtr, ytr = Xtr[ok], ytr[ok].astype(int)
        # 组 = 每个训练月一组的截面
        grp = Xtr.groupby(level="trade_date").size().to_numpy()
        ds = lgb.Dataset(Xtr.to_numpy(dtype="float64"), label=ytr, group=grp,
                         feature_name=cols, free_raw_data=False)
        booster = lgb.train(_lgb_params(seed), ds, num_boost_round=n_rounds)

        cur = feats.loc[feats.index.get_level_values("trade_date") == dt, cols]
        if not len(cur):
            continue
        p = booster.predict(cur.to_numpy(dtype="float64"))
        if len(p) != len(cur):
            # lambdarank 的 predict 在某些版本上按组返回 ⇒ 显式退回逐组预测
            p = np.concatenate([
                booster.predict(g.to_numpy(dtype="float64"))
                for _, g in cur.groupby(level="trade_date")])
        scores[dt] = pd.Series(p, index=cur.index.get_level_values("ts_code"))
        diag.append({"month": dt.strftime("%Y%m%d"), "train_months": len(tw),
                     "train_rows": int(len(Xtr)), "scored": int(len(cur)),
                     "eval": booster.best_score})
    if not scores:
        raise ValueError("没有一期能产出融合分 —— 训练窗口不足或样本太少")
    wide = pd.DataFrame(scores).T
    wide.index = pd.to_datetime(wide.index)
    return wide.sort_index(), pd.DataFrame(diag)


# ---------------------------------------------------------------------------
# 基线: 等权秩平均(**保留是为了可对照** —— 它已被证伪, 不是候选)
# ---------------------------------------------------------------------------
def rank_mean(die_tables: dict, feature_names: list = None) -> pd.DataFrame:
    """等权秩平均。**已证伪**(活跃区被稀释): 保留它是为了让任何新融合器有一个
    同一份输入下的对照组, 而不是让"新方法看起来更好"变成无法归因的断言。"""
    feats = build_features(die_tables)
    if feats.empty:
        raise ValueError("没有任何 die 分数可用")
    cols = feature_names or list(feats.columns)
    return feats[cols].mean(axis=1, skipna=True).unstack("ts_code")


# ---------------------------------------------------------------------------
# 落表
# ---------------------------------------------------------------------------
def write_scores(wide: pd.DataFrame, table: str = "model_score_ens",
                 pred_ret: pd.DataFrame = None) -> dict:
    """按引擎契约落打分表(`ts_code / trade_date / score / pred_ret`)。

    ⚠️ `pred_ret` **必须有**: 引擎不读它, 但 `builder`/`api`/`monitor` 会读 ——
    缺了会在下游崩(方案 §4 的接入契约)。这里缺省用 score 自身填充并**如实标注**,
    因为它只是"期望收益"的占位, 不参与任何判据。
    """
    import re
    if not re.fullmatch(r"[a-z][a-z0-9_]*", table):
        raise ValueError(f"表名非法: {table}")
    if table == "model_score":
        raise PermissionError("拒绝写生产表 model_score —— 融合器只许写实验表")
    expert.ensure_score_table(table)
    # ⚠️⚠️ **必须显式 dropna** —— 这里曾经写反过, 后果是落了一张 11.8% 是空分的表。
    #
    # `wide` 是 `pd.DataFrame(scores).T`(见 `stack()`): 各期的股票集合不同, DataFrame
    # 构造时按**并集**对齐 ⇒ 任何 die 都没打分的 (trade_date, ts_code) 以 NaN 形式存在。
    # 而 **pandas 3.0 起 `stack()` 是"保留 NA"**(不是丢) —— 本项目 2026-09-09 的
    # `scripts/oneoff/diag_allnan_samples.py` 抬头就记录了这条, 本文件 2026-09-11 却把
    # 方向写反了, 于是 `model_score_ens` 落了 39,924 行 `score IS NULL`(339,768 的 11.8%)。
    # 影响面: 引擎用 `nlargest` 选股(丢弃 NaN) ⇒ **不改回测读数**, 但它污染证据表、
    # 违反"过滤责任在打分侧"的契约、且 `builder`/`api`/`monitor` 会读 `pred_ret`。
    long = wide.stack().rename("score").reset_index()
    long.columns = ["trade_date", "ts_code", "score"]
    long = long.dropna(subset=["score"])
    long["trade_date"] = pd.to_datetime(long["trade_date"]).dt.strftime("%Y%m%d")
    if pred_ret is not None:
        pr = pred_ret.stack().rename("pred_ret").reset_index()
        pr.columns = ["trade_date", "ts_code", "pred_ret"]
        pr["trade_date"] = pd.to_datetime(pr["trade_date"]).dt.strftime("%Y%m%d")
        long = long.merge(pr, on=["trade_date", "ts_code"], how="left")
    if "pred_ret" not in long:
        long["pred_ret"] = long["score"]
    long["pred_ret"] = long["pred_ret"].fillna(long["score"])
    conn = db.get_conn()
    try:
        # 每月先删后写(与 trainer 同纪律, 避免旧月份残留)
        for d in sorted(long["trade_date"].unique()):
            conn.execute(f"DELETE FROM {table} WHERE trade_date=?", (str(d),))
        long[["ts_code", "trade_date", "score", "pred_ret"]].to_sql(
            table, conn, if_exists="append", index=False)
        conn.commit()
    finally:
        conn.close()
    return {"table": table, "rows": int(len(long)), "months": int(wide.shape[0])}
