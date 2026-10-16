"""LightGBM 滚动训练: 每月用过去 train_months 个月的截面训练, 预测当月全市场打分

标签: 未来21个交易日相对行业等权的超额收益(行业中性)
样本过滤: 上市满60交易日、当日未停牌、剔除ST/退市

2026-09-04 审查 P1-4:
- 固定 seed + deterministic: 两次训练结果一致、可复现
- 增量训练(默认): 只训练尚无 model_score 的月份, 历史分数不动;
  因子口径变更后需全量重训时显式传 full=True
- 每期模型按月归档 models/lgb_YYYYMM.txt, 最后一期同步存 lgb_latest.txt 供每日推分
"""
import hashlib
import logging
import os
import re
import shutil
import time
from pathlib import Path

import numpy as np
import pandas as pd
import lightgbm as lgb

from app import db
from app import timeutil
from app import config as cfg_mod
from app.config import DATA_DIR
from app.factors.factor_engine import load_market, month_end_dates, industry_map
from app.pit.engine import PITEngine

log = logging.getLogger(__name__)


def _factor_fingerprint(factor_names) -> str:
    """因子集指纹(sha1)。**排序后**计算: 因子集是集合, 顺序不该产生假失配。"""
    return hashlib.sha1("\n".join(sorted(factor_names)).encode("utf-8")).hexdigest()


def _check_factor_generation(score_table: str, factor_names, full: bool) -> bool:
    """P2-09: 增量训练前核对「上次训练用的因子集」与「当前因子集」。

    增量只补缺失月份, 而特征取自**当前** factor 表 —— 期间新增/删除过因子时, 同一张
    score_table 会静默混入两代特征(历史月是旧口径模型的分, 新月是新口径), 回测/对比
    跨口径且没有任何标记。这里在**训练前**拦下, 让人显式选择 full=True 重训。

    返回 True = 本次训练结束后要把指纹刷成当前因子集(`_record_factor_generation`)。
    ⚠️ 2026-09-19 审查 A5: 指纹**必须训练成功后**才落。原实现在 `full=True` 分支于开跑
    之前就 `set_meta` —— 一次 116 期的全量重训跑到第 40 期崩掉(OOM/强杀/断电)时, 41 期
    之后仍是旧代特征, 守卫却已重新武装 ⇒ 下一次增量训练指纹相等、**静默混代**, 正是本
    函数要拦的那个 P2-09。同文件 `_swap_latest_ensemble`(P1-05)用 meta 当指针做两阶段
    迁移, 是同一个道理: **"要变成什么"要在"真的变成了"之后才登记。**

    首次运行(无指纹)不拦: 只记一条 warning —— 否则升级后第一次增量必挂, 而历史是否
    已经是混合口径, 事后无法从库里判断。
    """
    key = f"factor_fp:{score_table}"
    with db.conn_ctx() as conn:
        saved = db.get_meta(conn, key)
    if saved is None:
        log.warning("首次记录 %s 的因子集指纹(%d 个因子); 此前的历史分数口径无法回溯核验",
                    score_table, len(factor_names))
        return True
    if full:
        return True                                 # 全量重训完成后由调用方落指纹
    if saved != _factor_fingerprint(factor_names):
        with db.conn_ctx() as conn:
            old = (db.get_meta(conn, f"factor_names:{score_table}") or "").split(",")
        old_s, new_s = set(x for x in old if x), set(factor_names)
        raise RuntimeError(
            f"因子集与上次训练 {score_table} 时不一致(新增 {sorted(new_s - old_s)}, "
            f"删除 {sorted(old_s - new_s)}) —— 增量训练会让同一张表混入两代特征。"
            f"请显式全量重训(full=True / --full)。")
    return False


def _record_factor_generation(score_table: str, factor_names, data_asof: str = None) -> None:
    """训练**全部成功后**登记本次训练的溯源信息(与 `_check_factor_generation` 配对, 见 A5)。

    除原有的因子集指纹外, 2026-09-21 起追加两个键 —— 起因是 05 号台账 §5.11:
    生产 `model_score`(09-09 训练)与其后日频刷新的因子表之间出现了分歧, 而库里
    **没有任何记录标明这个模型当时看到的数据截止到哪天** ⇒ "这个模型有多旧"不可知,
    才把组合层 1.1σ 的放大误写成 3.0pp「漂移」。登记后, 模型年龄从不可知变为可查。

    ⚠️ `data_asof` 是**代理指标, 不是完整数据指纹**: 它只取 `factor` 表的最大 trade_date,
    而训练还读 `daily`/`adj_factor` 等。选 factor 表是因为它①每次训练都全量重读 ②正是
    §5.11 里被日频刷新的那张表。**as-of 相同不代表训练输入完全相同**, 不要反过来用。

    判据与字段定义见 `plans/p1p2-model-provenance-20260921.md`(跑前冻结)。

    `data_asof` 为 None(调用方没传, 如既有单测直接调两参数)时**不写该键** ——
    宁可没有记录, 也不写一个来源不明的日期。
    """
    with db.conn_ctx() as conn:
        db.set_meta(conn, f"factor_fp:{score_table}", _factor_fingerprint(factor_names))
        db.set_meta(conn, f"factor_names:{score_table}", ",".join(sorted(factor_names)))
        db.set_meta(conn, f"trained_at:{score_table}", timeutil.stamp())
        if data_asof:
            db.set_meta(conn, f"data_asof:{score_table}", str(data_asof))
        conn.commit()
    log.info("已登记 %s 的因子集指纹(%d 个因子)%s —— 本次训练完整跑通", score_table,
             len(factor_names), f"、数据截止 {data_asof}" if data_asof else "")


def _swap_latest_ensemble(edir: Path, batch) -> None:
    """把最新一期的集成模型**整批**换到位(2026-09-12 审查 P1-05)。

    原实现逐个种子 `save_model` 覆盖 `lgb_latest_s*.txt`, meta 在循环之后才写。训练中途
    崩掉(OOM/强杀/断电)会留下「2 新 + 2 旧」的跨批次混合集成, 而 `daily_score._model_files()`
    只核对文件**存在性**, 照样把它当完整集成拿去日频推分 —— 无痕迹、无报错。

    这里用 meta 当指针, 把"换批"变成一次可观察的状态迁移:
      ① 整批先写进 staging 目录(名字照旧, 但还没进生产位置)
      ② 原子把 meta 改成 `pending` —— 消费者见到就回退单模型(fail-safe), 不会用混合集
      ③ 逐个 `os.replace` 到位(每个 replace 本身原子)
      ④ 原子把 meta 改成 `ready`
    任一步崩溃: 消费者要么看到旧的完整批(改 meta 之前), 要么看到 pending(中间), 要么看到
    新的完整批(之后) —— **不存在"看起来完整其实是混合"的状态**。
    """
    staging = edir / f".staging_{os.getpid()}_{int(time.time() * 1000)}"
    os.makedirs(staging, exist_ok=True)
    meta = edir / "lgb_latest_meta.txt"
    seeds = ",".join(str(s) for s, _ in batch)
    try:
        for s, model in batch:
            model.save_model(str(staging / f"lgb_latest_s{s}.txt"))
        # ② pending: 内容格式 第一行=种子(旧读法仍可解析), 第二行=状态
        meta.write_text(f"{seeds}\npending", encoding="utf-8")
        for s, _ in batch:
            os.replace(staging / f"lgb_latest_s{s}.txt", edir / f"lgb_latest_s{s}.txt")
        meta.write_text(f"{seeds}\nready", encoding="utf-8")
        # 清掉上一代不再被引用的孤儿文件(换种子重训会留下它们)
        keep = {f"lgb_latest_s{s}.txt" for s, _ in batch}
        for p in edir.glob("lgb_latest_s*.txt"):
            if p.name not in keep:
                try:
                    p.unlink()
                except OSError:
                    pass
    finally:
        shutil.rmtree(staging, ignore_errors=True)

HORIZON = 21  # 持有期(交易日), 约一个月

# 头部学习目标(2026-09-11 实验, 见 scripts/oneoff/exp_die_head.py)。
# 与 expert.TOP_FRAC **必须同值** —— 训练时定义的「头部」就是考核时度量的「头部」,
# 否则优化的东西和判据不是一回事。expert 会 import trainer(函数内), 故此处本地定义
# 避免模块级循环导入。
HEAD_TOP_FRAC = 0.10
LABEL_MODES = ("full", "head_cls", "head_vt")

# 北交纳入阈值(2026-09-08): 评分月训练窗口内的北交标签样本数下限(约 2-3 个月截面),
# 达标才允许北交进入该月训练与推分 —— 早期窗口无北交样本, 外推评分是系统性虚高根源
BSE_MIN_SAMPLES = 500

SEED = 42

# 生产集成配置 (2026-09-04 轮数扫描结论): 固定 50 轮(无早停) × 4 种子, 推分取分位排名均值
# 降种子方差。早停配置(平均仅 27 棵树、近期单树桩)过度牺牲收益换回撤, 已废弃。
ENSEMBLE_SEEDS = [42, 7, 123, 2024]
ENSEMBLE_ROUNDS = 50

PARAMS = dict(
    objective="regression",
    metric="rmse",
    learning_rate=0.05,
    num_leaves=31,
    min_data_in_leaf=200,
    feature_fraction=0.8,
    bagging_fraction=0.8,
    bagging_freq=1,
    lambda_l2=1.0,
    n_jobs=4,
    verbose=-1,
    seed=SEED,
    deterministic=True,  # 配合 seed: bagging/特征抽样可复现 (P1-4)
)


# 时点名称公共模块(2026-09-06 第十轮 N10-4): 保留 trainer.name_at_hist 导入路径
# 兼容既有测试, 实现统一在 app/models/names.py
from app.models.names import NameLookup, name_at_hist  # noqa: F401


def forward_return(ac: pd.DataFrame, horizon: int = HORIZON,
                   delist_last_price: bool = True) -> pd.DataFrame:
    """未来 horizon 个交易日的复权收益(标签)。

    2026-09-09 修复(审查项 ③): 原 `ac.shift(-horizon)/ac - 1` 在股票于窗口内退市或
    长期停牌(停牌日 daily 表直接缺行)时取不到价 → 标签 NaN → 被 mask 静默丢弃(实测
    327 个样本), 模型学不到退市尾部。现回退用「窗口内最后有效价」计算真实收益:
    - 与模拟盘 update_nav 的停牌口径(沿用最近收盘)一致, 也与回测结转仓位的盯市一致
    - 退市样本的标签=持有到最后一个可成交价的收益(不再是缺失)
    - 窗口内确实无价(未上市/整段缺失)仍为 NaN, 由 mask 剔除

    delist_last_price=False 仅用于修复前后的独立 A/B 复现(传 False 即旧口径), 生产恒为 True。
    """
    if not delist_last_price:
        return ac.shift(-horizon) / ac - 1
    fwd = ac.shift(-horizon)
    fwd = fwd.fillna(ac.ffill().shift(-horizon))
    return fwd / ac - 1


def _load_factors_wide() -> pd.DataFrame:
    with db.conn_ctx() as conn:
        # conn_ctx 已绑定连接(勿再 get_conn: 新开的那个无人关)
        fac = pd.read_sql("SELECT ts_code, trade_date, factor, value FROM factor", conn)
    if fac.empty:
        raise RuntimeError("factor 表为空, 请先运行 scripts/compute_factors.py")
    wide = fac.pivot_table(index="trade_date", columns=["ts_code", "factor"], values="value")
    wide.index = pd.to_datetime(wide.index)
    return wide.sort_index()


def _apply_exclusions(wide: pd.DataFrame, exclude_factors) -> pd.DataFrame:
    """生产口径的因子剔除（2026-09-27 第 6 次重钉接线; config `factor_exclusions`）。

    在 `_load_factors_wide()` **之后**按名丢列 —— factor 表本身保持全量写入（重测/
    审读机器仍可见全部 31 个）, 剔除只发生在训练/评分的消费侧。None/空 = 原样返回
    （DEFAULT 关纪律, 与 chase.enabled 同款）。未知名字直接忽略并 warning（防拼写
    错误静默少剔）。
    """
    if not exclude_factors:
        return wide
    have = set(wide.columns.get_level_values("factor"))
    drop = [f for f in exclude_factors if f in have]
    missing = [f for f in exclude_factors if f not in have]
    if missing:
        log.warning("factor_exclusions 含 factor 表不存在的因子(忽略): %s", missing)
    if drop:
        log.info("生产因子剔除(配置 factor_exclusions): %s", sorted(drop))
        wide = wide.drop(columns=drop, level="factor")
    return wide


def _wide_from_factors(factors: dict) -> pd.DataFrame:
    """{因子名: (日期 × 股票) DataFrame} → `_load_factors_wide` 同形的宽表(列 = ts_code×factor)

    2026-09-11 「芯片式异构架构」阶段 1 新增: 让 die 能吃到**不在 `factor` 表里**的口粮
    (新数据源因子)。本函数只在 `run(factors_override=...)` 显式传入时被调用 —— 生产路径
    一行不经过它, 所以不变量 #7(阶段 0~4 不碰实盘特征集)不受影响。
    """
    if not factors:
        raise ValueError("factors_override 为空")
    parts = []
    for fname, df in factors.items():
        d = df.copy()
        # 与 _load_factors_wide 的 pivot_table(columns=["ts_code","factor"]) 同构:
        # xs(level="factor") 按**名**取层, 故层序不敏感, 但层名必须齐备
        d.columns = pd.MultiIndex.from_product([[fname], d.columns],
                                               names=["factor", "ts_code"])
        parts.append(d)
    wide = pd.concat(parts, axis=1)
    wide.index = pd.to_datetime(wide.index)
    return wide.sort_index()


def _stack(wide: pd.DataFrame, factor_names, dates_sub):
    """把 (date × (stock,factor)) 宽表按日期子集堆叠成 (date,stock) × factor 矩阵

    注意(2026-09-09 审查项 ②): pandas 3.0 起 `stack()` 默认不再丢弃 NaN 行(pandas 2.x
    默认丢弃), 于是本函数返回的是「日期 × 全部股票」的笛卡尔积 —— 未上市/已退市/停牌
    的组合整行 NaN。调用方必须用 _drop_all_nan_rows 剔除后再训练/推分。
    """
    parts = []
    for f in factor_names:
        s = wide.xs(f, axis=1, level="factor").loc[dates_sub].stack().rename(f)
        parts.append(s)
    return pd.concat(parts, axis=1)


def _drop_all_nan_rows(df: pd.DataFrame, label: str, what: str = "样本") -> pd.DataFrame:
    """剔除全 NaN 特征行(2026-09-09 审查项 ②)

    这类行对 LightGBM 是纯噪声: 所有特征缺失, 预测值恒为基分, 却仍参与损失与分裂统计。
    当前数据下 tradable_mask(vol>0)已恰好挡掉全部此类行(实测入训 0 条, 见
    scripts/oneoff/diag_allnan_samples.py), 本函数为兜底 —— 未来出现数据缺口/因子口径
    变动时不得静默入训。"""
    if df.empty:
        return df
    all_nan = df.isna().all(axis=1)
    n = int(all_nan.sum())
    if n:
        log.warning("%s: 剔除 %d 条全 NaN 特征%s", label, n, what)
        df = df[~all_nan]
    return df


def _apply_train_start(md_all: pd.DatetimeIndex, train_start: str = None) -> pd.DatetimeIndex:
    """训练/评分月序列起点限制(2026-09-09 对照实验: 训练数据仅科创板上市后 vs 全历史)。
    None=不限制(生产行为)。注意它同时限制训练窗口与评分月 —— 训练数据不早于 train_start
    的模型无法为更早的月份推分。"""
    if not train_start:
        return md_all
    return md_all[md_all >= pd.Timestamp(str(train_start))]


def _train_window(md_all: pd.DatetimeIndex, i: int, train_months: int) -> pd.DatetimeIndex:
    """严格 walk-forward 训练窗口 = md_all[i-train_months : i-1](不含上月 i-1)。

    右端必须夹到 0: i=0 时 i-1 = -1 是 Python 负索引, md_all[0:-1] 会退化成
    「除最后一期外的全部月份」—— 2026-09-09 对照实验实测把未来 85 个月喂进首月训练
    (前视)。生产 run 因 backtest_start=2017 跳过 i<36 未触发, 但 md_all 起点 ≥
    backtest_start 的调用(train_start 实验/缩短历史)必踩。夹到 0 后 i=0 得空窗口,
    被调用方的 len<12 挡掉, 语义=首月无历史不推分。"""
    return md_all[max(0, i - train_months): max(0, i - 1)]


def _uniqueness_weights_by_anchor(md_all: pd.DatetimeIndex, cal: pd.DatetimeIndex,
                                  horizon: int = HORIZON) -> pd.Series:
    """逐锚点的平均唯一性权重(López de Prado, 2026-09-19 P0-1/F76 实验参数用)。

    c(d) = 覆盖交易日 d 的在窗标签数(窗 = 锚点之后**严格大于锚点**的 horizon 个
    交易日, 与 forward_return 的 open(t+1) 起算一致; 日历末端照实截断)。
    u(锚点) = 其窗内 1/c(d) 的均值。u=1 即无重叠。先全量累计 c 再取窗内均值
    (F76 初版"边累计边取"会让早锚点少算晚锚点的覆盖, 已在那里同步修正)。
    """
    pos = pd.Series(np.arange(len(cal)), index=cal)
    a_pos = pos.reindex(md_all).astype(int).to_numpy()
    n = len(cal)
    cover = np.zeros(n, dtype=int)
    spans = []
    for ap in a_pos:
        lo, hi = ap + 1, min(ap + horizon, n - 1)
        spans.append((lo, hi))
        if lo <= hi:
            cover[lo:hi + 1] += 1
    out = {}
    for a, (lo, hi) in zip(md_all, spans):
        out[a] = float(np.mean(1.0 / cover[lo:hi + 1])) if lo <= hi else 1.0
    return pd.Series(out)


STYLE_BUCKETS = 5   # size × vol 各分 5 档 = 25 桶


def _style_neutralize(labels_wide: pd.DataFrame, wide: pd.DataFrame,
                      size_f: str = "size", vol_f: str = "vol_20") -> pd.DataFrame:
    """标签改为**风格中性**: 每个交易日内部按 (size 分位 × vol 分位) 桶去均值。

    对照现行**行业中性**(line 203: labels - 同行业均值)。动因(2026-09-11): 因子回归显示
    模型唯一统计显著的暴露是**小盘价差(t=2.74)**, alpha 10.47%/年 t=1.75 不显著, 残差
    18.16%/年; 因子画像 = 小盘+高波动+龙虎榜+跌深反弹。即模型被训练去「选行业内最好的
    股票」, 但盈亏可能来自「押注小盘投机行情」—— 这是两件事。风格中性把后者从标签里扣掉:
    模型只能预测「在同规模同波动的一堆股票里谁更好」。

    分桶用逐日 pct 排名(不依赖分布形状; qcut 遇大量重复值会把边界挤成空桶)。
    size/vol 任一缺失 → 无法定桶 → 该样本标签置 NaN(**保守**: 不训练, 而非落进错桶)。
    """
    idx, cols = labels_wide.index, labels_wide.columns

    def _bucket(fname: str) -> pd.DataFrame:
        try:
            x = wide.xs(fname, level="factor", axis=1)
        except KeyError:
            raise KeyError(f"风格中性需要因子 {fname!r}, 但 factor 表里没有")
        x = x.reindex(index=idx, columns=cols)
        r = x.rank(axis=1, pct=True, na_option="keep")
        return np.floor(r * STYLE_BUCKETS).clip(upper=STYLE_BUCKETS - 1)

    bs, bv = _bucket(size_f), _bucket(vol_f)
    key = (bs * STYLE_BUCKETS + bv).where(bs.notna() & bv.notna())

    # stack() 默认丢 NaN: 标签缺失与桶缺失的样本都不进训练, 语义一致;
    # 桶序列须 reindex 到标签索引上, 两者 NaN 位置不同(标签缺/因子缺)不能直接对齐
    lab = labels_wide.stack()
    kk = key.stack().reindex(lab.index)
    ok = kk.notna()
    lab_ok, kk_ok = lab[ok], kk[ok]
    gkey = pd.MultiIndex.from_arrays([lab_ok.index.get_level_values(0),
                                      kk_ok.astype(int).values])
    out = lab_ok - lab_ok.groupby(gkey).transform("mean")
    return out.reindex(lab.index).unstack().reindex(index=idx, columns=cols)


def run(train_months: int = 36, backtest_start: str = "2017-01-01", full: bool = False,
        score_table: str = "model_score",
        importance_table: str = "model_importance", archive_dir=None,
        num_boost_round: int = 500, early_stopping_rounds: int = 50,
        seed: int = SEED, ensemble_seeds: list = None,
        holdout_recent: int = 6, train_start: str = None,
        label_neutral: str = "industry",
        factors_override: dict = None,
        label_industry: pd.DataFrame = None,
        label_mode: str = "full",
        sample_weight_mode: str = None,
        head_weight: float = None,
        horizon: int = HORIZON,
        exclude_factors: list = None) -> dict:
    """滚动训练。full=False(默认)增量: 只训尚无 score_table 评分的月份;
    full=True 全量重训所有月份(因子口径变更后的刷新入口)。

    num_boost_round/early_stopping_rounds 可调; seed 覆盖随机种子(多种子稳定性验证用)。
    ensemble_seeds: 每期按此种子列表训练多个模型, 推分取分位排名均值落库(生产集成,
    2026-09-04 轮数扫描结论: 50 轮×4 种子); None=单模型(实验/兼容旧行为)。
    train_start: 训练/评分月起点限制(2026-09-09 对照实验, 默认 None=全历史)。
    factors_override: {因子名: (日期×股票) DataFrame}, 给定则**取代** `factor` 表作为
        特征来源(2026-09-11 芯片式架构阶段 1, 见 _wide_from_factors)。默认 None =
        读 `factor` 表(生产行为, 一字不变)。走这条路时因子侧的全部过滤责任在调用方:
        `neutralize` 必须先在其中做过(与 factor 表的写入口径一致)。
    label_industry: (日期 × 股票) DataFrame, 逐期的行业标签。给定则**取代** `industry_map`
        (当前快照)做标签中性化 —— PIT 缺陷修复实验入口, 见下方分支注释。默认 None =
        当前快照(生产行为, 一字不变)。
    label_mode: 学习目标口径(2026-09-11 实验, 见 scripts/oneoff/exp_die_head.py)。
        "full"(默认) = 回归全截面 21 日收益, objective=regression/rmse —— **生产行为, 一字不变**。
        "head_cls"   = 二元标签 1{月内截面百分位 >= 1-HEAD_TOP_FRAC}, binary/auc。
        "head_vt"    = 同上, 但剔除中间 80% 样本(只留头 10% 与尾 10%), binary/auc。
        理由: 策略只买分数前 6 只, 而回归目标在**全截面**上最小化平方误差 ——
        优化的量与决策用的量不是一回事。两变体均**零新参数**(阈值复用 HEAD_TOP_FRAC,
        与 expert.TOP_FRAC 同值), 故「唯一变量」成立。
    horizon: 标签的前向收益天数(2026-09-11 实验, 见 scripts/oneoff/exp_die_weekly.py)。
        默认 HORIZON=21(=生产, 约一个月)。**只改「模型预测多远」, 不改「多久调仓」** ——
        调仓频率由 `md_all`(月末截面)决定, 与本参数无关。评估仍用 21 日(策略的持有期),
        故「训练目标变短」与「策略行为不变」是分离的。
    sample_weight_mode: 样本权重口径(2026-09-19 P0-1/F76 实验参数)。
        None(默认) = 不加权 —— **生产行为, 一字不变**。
        "uniqueness" = López de Prado 平均唯一性: 逐锚点权重 = 其 21 日标签窗内
        1/c(d) 的均值(c(d) = 覆盖该交易日的在窗标签数, 见 _uniqueness_weights_by_anchor)。
        F76 实测月频 c̄=1.061(唯一性均值 0.943, 2026-09-19 修正复跑更新, 初值 0.971
        是增量取窗的实现偏差) ⇒ 预期效应极小, 本参数只为把 P0-1 的判决落在实测上
        而非直觉上。
    head_weight: 多头端样本加权(2026-09-29 B1, 预注册 plans/head-weight-prereg-20260929.md)。
        None(默认) = 不加权 —— **生产行为, 一字不变**。
        非 None(>1.0) = 保持回归标签不变, 仅对「当期截面(可打分域)标签百分位
        ≥ 90%」的样本加权 w=1+(head_weight−1)。LSW2 式多头端加权(广发 2025-06
        选股训练手册: 沪深300 池多头年化 +0.55pp/超额回撤改善; CIKM 2025: 损失
        只改 top-k 组合不改 IC)。与 head_vt(换标签)、uniqueness(时间重叠加权)
        机制不同源; 与 uniqueness 同用时两者相乘。
    2026-09-05 僵尸代码清理: 排序目标(lambdarank)实验管线已整体移除(实验结论: 完败)。
        ⚠️ 该结论**无实证留存**(代码已删、无 run 记录、无日志), 故它不构成
        label_mode 这一轮的证据, 只作背景。2026-09-16 已如实登记进
        docs/know/05_实验台账.md(审查 MOD-02 附带项) —— 它仍然只是背景, 不是证据。"""
    for name in (score_table, importance_table):
        if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
            raise ValueError(f"非法表名: {name}")
    # 归档护栏(2026-09-11): 实验表若未指定 archive_dir, 下面的归档分支会落到
    # `DATA_DIR/models/ens/` —— 而 **`is_prod` 只保护 `lgb_latest_s*`, 不保护按月归档的
    # `lgb_YYYYMM_s*.txt`**。2026-09-11 实测: die 实验与 verify_d1_replica 把生产归档
    # `ens/` 469 个文件里的 464 个覆盖掉了(116 月 × 4 种子)。
    # 影响面: `daily_score.py` 只读 `lgb_latest_s*` ⇒ 交易链未受影响; **审计链断裂**。
    # ⇒ 宁可让调用方显式指定归档目录, 也不允许静默污染。
    if score_table != "model_score" and archive_dir is None:
        raise ValueError(
            f"实验表 {score_table!r} 未指定 archive_dir —— 按月归档会被写进生产目录 "
            f"data/models/ens/ 并静默覆盖生产模型。请显式传 archive_dir="
            f"(expert.run_expert 已默认 data/models/die_<name>)。")
    if label_mode not in LABEL_MODES:
        raise ValueError(f"未知 label_mode: {label_mode!r}(支持 {LABEL_MODES})")
    if sample_weight_mode not in (None, "uniqueness"):
        raise ValueError(
            f"未知 sample_weight_mode: {sample_weight_mode!r}(只支持 None/uniqueness)")
    if head_weight is not None and not (isinstance(head_weight, (int, float)) and head_weight > 1.0):
        raise ValueError(f"head_weight 须为 >1.0 的数或 None, 收到 {head_weight!r}")
    params = dict(PARAMS)
    params["seed"] = seed
    if label_mode != "full":
        # 头部目标把「学什么」从连续收益换成「是不是头部」, 目标函数随之换
        params["objective"] = "binary"
        params["metric"] = "auc"
    log.info("== 模型滚动训练开始 (%s, 表=%s, rounds=%s, 学习目标=%s) ==",
             "全量" if full else "增量", score_table,
             num_boost_round if not early_stopping_rounds else f"{num_boost_round}+早停{early_stopping_rounds}",
             label_mode)
    m = load_market()
    dates = pd.to_datetime(m["close"].index)
    # R11-3(2026-09-06): 市值/流动性硬过滤参数(与 builder/daily_score 同口径)
    _cfg = cfg_mod.load_config()
    # size_pctl / min_amt20 的读取已随 tradable_mask 委托给 PITEngine 而移除(见下方 _pit)
    # 只训练已完结自然月的截面(2026-09-04 审查 P0-2: 月中训练会把当月1日误当"月度截面")
    # 2026-09-28 收编 timeutil(东八区): 裸 date.today() 在非东八区机器上会把"最新已完结
    # 月末"判定差一个月(正是 P0-2 修过的锚点漂移类); 本机逐位无影响
    md_all = month_end_dates(dates, today=timeutil.today())
    md_all = md_all[md_all >= pd.Timestamp("2014-01-01")]
    # 2026-09-09 对照实验: 训练数据起点(窗口与评分月同时受限, 见 _apply_train_start)
    md_all = _apply_train_start(md_all, train_start)
    if train_start:
        log.info("训练/评分月起点限制: %s(共 %d 个月末)", train_start, len(md_all))
    # 样本权重(P0-1/F76 实验参数, 默认 None 不进任何生产行为)
    w_by_anchor = None
    if sample_weight_mode == "uniqueness":
        w_by_anchor = _uniqueness_weights_by_anchor(md_all, dates, horizon)
        log.info("样本权重 uniqueness: mean=%.4f min=%.4f(月频锚点×%d 日标签)",
                 float(w_by_anchor.mean()), float(w_by_anchor.min()), horizon)

    with db.conn_ctx() as conn:
        # conn_ctx 已绑定连接(勿再 get_conn: 新开的那个无人关)
        industry = industry_map(conn)
        have = set(pd.read_sql(f"SELECT DISTINCT trade_date FROM {score_table}", conn)["trade_date"])
    # 时点名称(2026-09-05 D1 根治; 2026-09-06 第十轮 N10-4 统一公共模块): ST/退过滤用
    # 评分日当时的名称而非当前名称 —— 当前 ST/退的股票(含 289 只已退市)原被整体剔出
    # 全部历史, 回测天然规避退市归零尾部(幸存者偏差)。无改名记录回退当前名称。
    nl = NameLookup()
    # 2026-09-11 框架化: 时点口径收进 `app/pit/engine.py` 一等模块。此处**只做委托**,
    # 口径定义仍在 PITEngine(六层表见其 docstring) —— 原来散在这里的 first_trade /
    # is_bse / is_star / _name_at / size_ok / liq_ok 整套闭包已删除, 避免两处实现漂移。
    # `nl` 保留: 本文件另有若干处直接用 nl。
    _pit = PITEngine(m, cfg=_cfg)

    def _name_at(code: str, dt: pd.Timestamp) -> str:
        """评分日当时的股票名称"""
        return nl.name_at(code, dt.strftime("%Y%m%d"))

    if factors_override is not None:
        # 实验路径自带因子集(不经 config 剔除 —— 剔除是生产消费侧的口径, A/B 自己管自己的集)
        wide = _wide_from_factors(factors_override)
    else:
        wide = _apply_exclusions(_load_factors_wide(), exclude_factors)
    factor_names = sorted(wide.columns.get_level_values("factor").unique())
    log.info("因子%s: %s", "(override)" if factors_override is not None else "",
             factor_names)
    # P2-09: 增量训练前核对因子集指纹(不一致直接拒绝, 见函数 docstring)
    # A5: 这里只**校验**; 指纹改由训练跑通后登记 —— 开跑前登记会让中途崩掉的全量重训
    # 把守卫"骗"成已对齐, 下一次增量就静默混代。
    fp_pending = _check_factor_generation(score_table, factor_names, full)

    ac = (m["close"] * m["adj"])
    # ③ 退市/停牌样本按窗口内最后有效价计标签(A/B 复现旧口径: forward_return(..., False))
    fut = forward_return(ac, horizon)
    labels_wide = fut.loc[md_all]
    inds = industry.reindex(labels_wide.columns)
    # label_neutral="industry"(生产默认): 减去同行业均值 —— 选行业内最好的股票
    # label_neutral="style"   (2026-09-11 实验): 减去 size×vol 同桶均值 —— 选同风格里最好的
    if label_industry is not None:
        # PIT 行业标签中性化(2026-09-11 「修 PIT 行业」实验, 见 scripts/oneoff/exp_die_pit.py)。
        # 与 `label_neutral="industry"` 的**唯一区别**是行业标签随日期变化: 旧口径用
        # `stock_basic.industry` 的**当前快照**(含 333 只 NULL 被 fillna("未知") 当成同一个
        # 行业), 即 2017 年的标签用了 2026 年的行业分类 —— 前视。这里改用申万
        # `in_date`/`out_date` 重建的时点归属。
        # 与 fe.neutralize 同为**逐行截面**操作, 故只算 md_all 这些行即可。
        # NaN 行业 = 无申万归属(次新/退市/北交): groupby 会跳过 → 该股标签变 NaN →
        # **不进训练**, 而不是被凑成"未知"行业去减。这是口径切换的既定后果, 覆盖度
        # 变化必须在实验里量出来, 不得静默。
        out = labels_wide.copy()
        n_nan = 0
        for d in labels_wide.index:
            if d not in label_industry.index:
                continue
            g = label_industry.loc[d].reindex(labels_wide.columns)
            row = labels_wide.loc[d]
            n_nan += int((row.notna() & g.isna()).sum())
            out.loc[d] = row - row.groupby(g, sort=False).transform("mean")
        log.info("标签行业中性化用 **PIT 申万**(%d 期), 因无行业归属而标签置 NaN 的样本 %d 条",
                 len(labels_wide), n_nan)
        labels_wide = out
    elif label_neutral == "style":
        labels_wide = _style_neutralize(labels_wide, wide)
    elif label_neutral == "industry":
        labels_wide = labels_wide - labels_wide.T.groupby(inds, sort=False).transform("mean").T
    else:
        raise ValueError(f"未知 label_neutral: {label_neutral!r}(只支持 industry/style)")
    from app.portfolio.board import board_of
    bse_cols = [c for c in labels_wide.columns if board_of(str(c)) == "bse"]

    # 2026-09-11 框架化(用户指令: 按框架图改程序): 时点口径统一由
    # `app.pit.engine.PITEngine` 提供 —— 六层过滤表见其模块 docstring。
    # 此处**只做委托**: 原来散装在本函数里的 first_trade/上市日锚定/is_bse/_star_sel/
    # is_star/size_ok/liq_ok 整套闭包已全部删除, 口径只存在一处, 不再有两份实现漂移。
    # (`_pit` 在 run() 顶部构造; `_name_at` 上方保留供本文件其它处使用。)
    def tradable_mask(dt, allow_bse: bool = False):
        return _pit.tradable_mask(dt, allow_bse=allow_bse)

    n_done = 0
    last_date = None
    for i in range(len(md_all)):
        d = md_all[i]
        if d < pd.Timestamp(backtest_start):
            continue
        if not full and d.strftime("%Y%m%d") in have:
            last_date = d  # 已有该月评分: 跳过训练, 但保留"最新评分日"用于完成日志
            continue
        # 严格 walk-forward(第二轮复查 D2; 2026-09-06 R17-5 措辞修正): 窗口 =
        # md_all[i-36:i-1] 含索引 i-36..i-2, 不含上月(i-1); 窗口最后一期(i-2)的
        # 21 日标签落点约在 i-1 月内, 严格早于预测月 i 的决策点, 无前视
        window = _train_window(md_all, i, train_months)
        if len(window) < 12:
            continue
        # 2026-09-08 审查 P2: 窗口内某月 factor 表缺行时 .loc[window] KeyError 崩断
        # 整轮训练(且该月不在 have, 每次重试同点崩溃) —— 与宽表实际索引求交并 loud
        # 告警缺口月, 训练其余月份; _factor_ready 门控只查最新月末, 缺口月需显式暴露
        gap = [w for w in window if w not in wide.index]
        if gap:
            log.warning("%s 训练窗口缺因子月 %s(数据补齐后增量重训自愈)", d.date(), gap)
        window = [w for w in window if w in wide.index]
        if len(window) < 12:
            continue

        X = _stack(wide, factor_names, window)
        # 2026-09-09 修复(审查项 ② 全 NaN 样本入训): 见 _drop_all_nan_rows
        X = _drop_all_nan_rows(X, f"{d.date()} 训练窗口")
        y = labels_wide.loc[window].stack().reindex(X.index)
        mask = y.notna()
        # 北交纳入(2026-09-08, config bse_enabled 默认关闭): 启用时要求窗口内北交标签
        # 样本数达标(约 2-3 个月截面)才参与 —— 早期窗口无北交样本, 外推评分是系统性
        # 虚高根源; 关闭时训练与推分同 mask 排除
        if _cfg.get("bse_enabled"):
            bse_rows = int(labels_wide.loc[window, bse_cols].notna().sum().sum())
            allow_bse = bse_rows >= BSE_MIN_SAMPLES
        else:
            allow_bse = False
        for wd in window:
            ok = tradable_mask(wd, allow_bse)
            mask.loc[wd] = mask.loc[wd] & ok.reindex(mask.loc[wd].index)
        X_tr = X[mask].astype(np.float32)
        y_tr = y[mask].astype(np.float32)
        # 样本权重(sample_weight_mode="uniqueness"): 逐行取各自锚点的平均唯一性。
        # 权重只依赖锚点月, 与股票无关; 必须在 holdout 切分**之前**建好, 才能同一
        # vmask 同步切训练/验证两份。
        w_tr = None
        if w_by_anchor is not None:
            # ndarray(Index 不收: lgb "Wrong type(Index) for weight")
            w_tr = X_tr.index.get_level_values(0).map(w_by_anchor).astype(float).to_numpy()

        # 多头端样本加权(2026-09-29 B1, plans/head-weight-prereg-20260929.md):
        # 截面定义域与 head_vt 同款(= mask 后的可打分截面), 但**不换标签**只加权 ——
        # 保持 objective=regression + 连续收益标签, 区别于第 11 轮 head_vt 的 binary。
        if head_weight is not None:
            pct_hw = y_tr.groupby(level=0).rank(pct=True)
            boost = 1.0 + (float(head_weight) - 1.0) * (pct_hw >= 1 - HEAD_TOP_FRAC).astype(float)
            w_tr = boost.to_numpy() if w_tr is None else w_tr * boost.to_numpy()
            log.info("%s head_weight=%.1f: 头部样本占比 %.3f, 加权均值 %.3f",
                     d.date(), float(head_weight), float((boost > 1).mean()),
                     float(boost.mean()))

        # 头部学习目标(2026-09-11 实验): 把回归标签换成「是不是这个月的头部」。
        # **截面定义域 = 已应用 tradable_mask 的样本**(上面 mask 那一步刚做完) ——
        # 若在未过滤的全截面里排名, 「前 10%」会混进当日不可交易的股票, 那么训练时
        # 认定的头部与考核时 head_excess 度量的头部(在**可打分截面**上取前 10%)
        # 就不是同一批股票, 优化目标便与判据错位。
        if label_mode != "full":
            pct = y_tr.groupby(level=0).rank(pct=True)
            is_head = (pct >= 1 - HEAD_TOP_FRAC).astype(np.float32)
            log.info("%s 头部目标: %d 个月截面, 样本 %d 条, 头部占比 %.3f",
                     d.date(), int(y_tr.index.get_level_values(0).nunique()),
                     len(y_tr), float(is_head.mean()))
            if label_mode == "head_vt":
                keep = ((pct >= 1 - HEAD_TOP_FRAC) | (pct <= HEAD_TOP_FRAC)).to_numpy()
                X_tr, y_tr = X_tr[keep], is_head[keep]
                if w_tr is not None:
                    w_tr = w_tr[keep]  # 与行切片同步, 否则权重错位
                log.info("%s head_vt: 剔中间 80%%, 剩 %d 条(头 %d / 尾 %d)",
                         d.date(), len(y_tr), int(y_tr.sum()), int((y_tr == 0).sum()))
            else:
                y_tr = is_head

        # 近端留出(2026-09-06 R11-1 回退, 取代第十轮 N10-1): 无论是否早停, 训练集都
        # 剔除窗口最后 holdout_recent 个月 —— 月频高换手(87%)策略的"近端去敏/防追高
        # 保险": 全窗口训练把最近 6 个月的当下风格学得太尖, 风格反转时头部同板块踩踏
        # (20260630 光模块链 -42% 单月实证, 回撤 -30%→-56%)。早停时留出段兼作验证集
        # 选轮, 无早停时直接不参与训练; 这 6 个月下月滚动窗口时自然进入, 信息不丢
        holdout = min(int(holdout_recent or 0), len(window) - 1) if len(window) > 1 else 0
        if holdout > 0:
            vdates = window[-holdout:]
            vmask = X_tr.index.get_level_values(0).isin(vdates)
            w_va = w_tr[vmask] if w_tr is not None else None
            w_train = w_tr[~vmask] if w_tr is not None else None
            if early_stopping_rounds:
                # 验证集先从未切分的 X_tr 上取(与旧口径一致), 再切走
                X_va = X_tr[vmask].reset_index(drop=True)
                y_va = y_tr[vmask].reset_index(drop=True)
            X_tr = X_tr[~vmask].reset_index(drop=True)
            y_tr = y_tr[~vmask].reset_index(drop=True)
            dtrain = lgb.Dataset(X_tr, y_tr, weight=w_train)
            valid_sets = None
            if early_stopping_rounds:
                dvalid = lgb.Dataset(X_va, y_va, reference=dtrain, weight=w_va)
                valid_sets = [dvalid]
                if len(X_tr) < 5000 or len(X_va) < 1000:
                    log.warning("%s 样本不足(train=%d, valid=%d), 跳过",
                                d.date(), len(X_tr), len(X_va))
                    continue
            elif len(X_tr) < 5000:
                log.warning("%s 样本不足(train=%d), 跳过", d.date(), len(X_tr))
                continue
        else:
            X_tr = X_tr.reset_index(drop=True)
            y_tr = y_tr.reset_index(drop=True)
            dtrain = lgb.Dataset(X_tr, y_tr, weight=w_tr)
            valid_sets = None
            if len(X_tr) < 5000:
                log.warning("%s 样本不足(train=%d), 跳过", d.date(), len(X_tr))
                continue

        # 当月预测特征
        X_d = pd.concat(
            [wide.xs(f, axis=1, level="factor").loc[d].rename(f) for f in factor_names], axis=1
        )
        ok = tradable_mask(d, allow_bse)
        X_d = X_d[ok.reindex(X_d.index).fillna(False)]
        # ②同源: 全 NaN 特征行不进预测截面(模型对纯缺失样本的输出是常数外推)
        X_d = _drop_all_nan_rows(X_d, f"{d.date()} 推分截面", what="股票")

        # 训练(集成=逐种子训练) + 推分 + 归档
        is_prod = score_table == "model_score"
        seeds = list(ensemble_seeds) if ensemble_seeds else [params["seed"]]
        raw_preds, pct_ranks, imps = [], [], []
        latest_batch = []          # P1-05: 集成服务模型的整批暂存(循环结束后统一换批)
        for s in seeds:
            ps = dict(params)
            ps["seed"] = s
            model = lgb.train(
                ps,
                dtrain,
                num_boost_round=num_boost_round,
                valid_sets=valid_sets,
                callbacks=[lgb.early_stopping(early_stopping_rounds, verbose=False)]
                if early_stopping_rounds else None,
            )
            p = model.predict(X_d.astype(np.float32))
            raw_preds.append(p)
            pct_ranks.append(pd.Series(p, index=X_d.index).rank(pct=True))
            imp = model.feature_importance("gain")
            imps.append(imp / imp.sum())
            # 归档: 集成模型进 data/models/ens/(带种子后缀), 单模型走原目录逻辑
            if len(seeds) > 1:
                # 2026-09-08 审查 P2: 实验运行(非生产 score_table/archive_dir)也归档到
                # archive_dir —— 原固定写 ens/, 实验重训同月份会静默覆盖生产月度归档
                # (审计链断裂); lgb_latest 系列有 is_prod 保护不受影响
                if archive_dir is not None:
                    edir = archive_dir
                    os.makedirs(edir, exist_ok=True)
                    model.save_model(str(edir / f"lgb_{d.strftime('%Y%m')}_s{s}.txt"))
                else:
                    edir = DATA_DIR / "models" / "ens"
                    os.makedirs(edir, exist_ok=True)
                    model.save_model(str(edir / f"lgb_{d.strftime('%Y%m')}_s{s}.txt"))
                    # 最后一期同步作为每日推分的服务模型(与月度方案同源, 保证打分一致)。
                    # P1-05: 不在这里逐个写盘, 先攒着, 循环结束后整批换(见 _swap_latest_ensemble)
                    if d == md_all[-1] and is_prod:
                        latest_batch.append((s, model))
            else:
                # 每期模型按月归档(lgb_YYYYMM.txt, 体积小; 2026-09-04 P1-4:
                # 原只留最后一期, 历史 model_score 无法审计复现)
                adir = archive_dir if archive_dir is not None else (DATA_DIR / "models")
                os.makedirs(adir, exist_ok=True)
                model.save_model(str(adir / f"lgb_{d.strftime('%Y%m')}.txt"))
                # 实验归档目录/实验表不写 lgb_latest, 避免污染线上推分模型
                if d == md_all[-1] and archive_dir is None and is_prod:
                    model.save_model(str(DATA_DIR / "models" / "lgb_latest.txt"))

        # P1-05: 本期的集成服务模型**整批**换到位(meta 当指针, 见 _swap_latest_ensemble)。
        # 放在这里而不是训练循环里, 是为了保证 4 个种子全都训完了才动生产模型文件。
        if latest_batch:
            _swap_latest_ensemble(DATA_DIR / "models" / "ens", latest_batch)
            log.info("集成服务模型已整批换批: 种子 %s",
                     [s for s, _ in latest_batch])

        # 集成: 分位排名取均值(跨模型原始分不可比, 排名可比); pred_ret 取原始预测均值(仅展示)
        score = (sum(pct_ranks) / len(pct_ranks)) * 100
        pred_ret = np.mean(np.array(raw_preds), axis=0)
        imp_mean = sum(imps) / len(imps)

        with db.conn_ctx() as conn:
            # conn_ctx 已绑定连接(勿再 get_conn: 新开的那个无人关)
            # 2026-09-08 审查 P1: 逐月先删后写(与 daily_score 同口径) —— 原纯 upsert 在
            # 口径切换(bse/star 开关往返重训)后残留旧口径行: 旧北交分在下游无任何过滤,
            # 会静默混入候选池/回测污染生产
            conn.execute(f"DELETE FROM {score_table} WHERE trade_date=?", (d.strftime("%Y%m%d"),))
            conn.execute(f"DELETE FROM {importance_table} WHERE train_date=?",
                         (d.strftime("%Y%m%d"),))
            out = pd.DataFrame({"ts_code": X_d.index, "trade_date": d.strftime("%Y%m%d"),
                                "score": score.values, "pred_ret": pred_ret})
            db.upsert_df(conn, out, score_table)
            imp_df = pd.DataFrame({"train_date": d.strftime("%Y%m%d"), "factor": factor_names,
                                   "importance": imp_mean})
            db.upsert_df(conn, imp_df, importance_table)
            db.set_status(conn, score_table, d.strftime("%Y%m%d"), len(out))

        n_done += 1
        last_date = d
        if n_done % 12 == 0:
            log.info("已训练 %d 期, 最新 %s", n_done, d.date())

    log.info("== 模型滚动训练完成: %d 期, 最新评分日 %s ==", n_done, last_date.date() if last_date is not None else None)
    if fp_pending:
        # A5: 全量/首次训练**跑通**到这里才登记指纹(校验在开跑前, 见 _check_factor_generation)
        # P1 可追溯性: 同时登记"本次训练看到的数据截止日"(factor 表最大 trade_date),
        # 让任意一次产物事后都能回答"这个模型有多旧" —— 见 _record_factor_generation。
        _asof = None
        try:
            _asof = pd.Timestamp(wide.index.max()).strftime("%Y%m%d")
        except Exception:                        # 溯源信息不该让训练失败
            log.exception("取训练数据截止日失败(不影响训练结果, 该次不登记 data_asof)")
        _record_factor_generation(score_table, factor_names, data_asof=_asof)
    return {"trained": n_done, "last_date": last_date.strftime("%Y%m%d") if last_date is not None else None}
