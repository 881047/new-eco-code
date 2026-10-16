"""芯片式异构架构 阶段 1: 专家(die)契约 + Known-Good-Die 测试台

**为什么需要这个模块**(方案 `plans/abstract-sleeping-rainbow.md` §0/§2):
现行「4 种子集成」是 4 份**同一个模型**跑**同一份** 31 因子, 误差几乎完全相关 ——
那是"同一信号做方差缩减", 不是"误差去相关"。芯片架构要让多个 die 真正独立,
靠的是**口粮不同**(信息源异构), 而不是算法族不同。

**KGD 是这套架构的前提, 不是可选项。** 没有它, 「建 N 个模型挑最好的」就是标准
过拟合机器 —— 测的 die 越多, 越容易选中运气好的那个。故本模块把三件事绑在一起:
    1. `run_expert`  —— 专家契约: 一个 die = (因子集, 模型族) → 一张 `score` 表
    2. `kgd`         —— 单个 die 的体检: 死区/活跃区/全区三段读数 + 与基线的秩相关
    3. `bonferroni`  —— 良率校正: 测了 n 个 die, 显著性门槛就要除以 n

**判据为什么不是 IC**: 本项目已三次独立复现「全截面 IC 与 top-6 实盘收益反相关」,
策略只买 6 只, 在意的是分布**头部**; 全截面 IC 由中部几千只股票主导。故主判据是
`head_excess`(按分数取前十分位的前向收益 − 同截面均值得益)。IC 只作参考列。

⚠️ **与 `scripts/oneoff/exp_deadzone_ic.py` 的关系**: 那个脚本是**预注册**的(门禁规则
在跑之前写死), 本模块为 KGD 测试台**另写**了一份统计量, 而不是重构它 —— 冻结的判据
比 DRY 值钱。两份实现的一致性由 `tests/test_expert.py` 里的交叉校验保证(同一输入必须
给出同一个 head_excess), 防止静默漂移。
"""
import logging

import numpy as np
import pandas as pd

from app import db

log = logging.getLogger(__name__)

HORIZON = 21          # 与 trainer.HORIZON 同口径(约一个月)
TOP_FRAC = 0.10       # 头部定义: 按分数取前 10%
MIN_NAMES = 200       # 截面股票数下限(低于此不出读数)

# ---------------------------------------------------------------------------
# 预注册的 KGD 判据(2026-09-11 写死于任何 die 之前 —— 改这里等于改判据)
# ---------------------------------------------------------------------------
KGD_MIN_HEAD = 0.005       # 死区头部月超额下界 0.5%/月
KGD_MIN_T = 2.0            # 死区 t 下界
KGD_ALPHA = 0.05           # 显著性水平(Bonferroni 分母 = 实际测过的 die 数)
KGD_MAX_CORR = 0.95        # 与基线秩相关上限: 超过即"换皮", 无去相关价值
KGD_REQUIRE_SAME_SIGN = True

# 分区(按月末评分日), 与死区体检脚本逐字一致
DEAD = ("2020-09-01", "2023-12-31")
ACTIVE = ("2024-01-01", "2026-08-31")

# 打分表硬契约(app/db.py / backtest/engine.py 已探明)
SCORE_DDL = """
CREATE TABLE IF NOT EXISTS {t} (
    ts_code TEXT, trade_date TEXT, score REAL, pred_ret REAL,
    PRIMARY KEY (ts_code, trade_date)) WITHOUT ROWID
"""


def ensure_score_table(name: str) -> None:
    """按引擎契约建表(幂等)。表名合法性由 trainer.run 的同一正则把关。"""
    import re
    if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
        raise ValueError(f"非法表名: {name}")
    conn = db.get_conn()
    try:
        conn.execute(SCORE_DDL.format(t=name))
        conn.commit()
    finally:
        conn.close()


def zone_of(dates) -> np.ndarray:
    """每个截面日属于 DEAD / ACTIVE / OTHER"""
    d = pd.Series(pd.to_datetime(dates))
    return np.where((d >= DEAD[0]) & (d <= DEAD[1]), "DEAD",
                    np.where((d >= ACTIVE[0]) & (d <= ACTIVE[1]), "ACTIVE", "OTHER"))


# ---------------------------------------------------------------------------
# 1) 专家契约
# ---------------------------------------------------------------------------
def run_expert(name: str, factors: dict, table: str = None, full: bool = True,
               **kw) -> dict:
    """训练一个 die 并落 `score` 表。返回 trainer.run 的结果字典。

    factors: {因子名: (日期 × 股票) DataFrame} —— **必须先做过 neutralize**
             (与 `factor` 表的写入口径一致; 走 factors_override 时因子侧的全部
             过滤责任在调用方, 见 trainer.run 的 docstring)。
    table:   打分表名, 默认 `model_score_die_<name>`。实验表不写 lgb_latest
             (trainer 的 is_prod 门控), 所以不可能污染实盘链路。
    full:    默认 True —— die 是新表, 没有历史分数可增量, 全量训练才是完整读数。

    ⚠️ **`archive_dir` 必须默认指向实验自己的目录(2026-09-11 修复)**：
    `trainer.run` 的归档分支是 `if archive_dir is not None: <实验目录> else: models/ens/`,
    而按月归档的 `lgb_YYYYMM_s*.txt` **不受 `is_prod` 保护**(只有 `lgb_latest_s*` 受保护)。
    本函数原不传 archive_dir ⇒ 每次训练 die 都会**逐月覆盖生产归档 `data/models/ens/`**。
    2026-09-11 实测踩到: 469 个文件里 464 个被 h5/h10 实验覆盖。
    `daily_score.py` 只读 `lgb_latest_s*`(未受影响), 但**审计链断裂** —— 因此必须显式隔离。
    """
    from app.config import DATA_DIR
    from app.models import trainer

    table = table or f"model_score_die_{name}"
    ensure_score_table(table)
    kw.setdefault("archive_dir", DATA_DIR / "models" / f"die_{name}")
    log.info("== 训练 die %s → %s (因子 %d 个, 归档 %s) ==", name, table, len(factors),
             kw["archive_dir"])
    res = trainer.run(score_table=table, factors_override=factors, full=full, **kw)
    res = dict(res or {})
    res["table"] = table
    res["factor_names"] = sorted(factors)
    return res


# ---------------------------------------------------------------------------
# 2) 读数
# ---------------------------------------------------------------------------
def load_factors_from_table(names: list = None) -> dict:
    """从 `factor` 表读回 {因子名: DataFrame} —— 供 D1(现行模型的复刻)与对照组使用。"""
    conn = db.get_conn()
    try:
        if names:
            q = ("SELECT ts_code, trade_date, factor, value FROM factor WHERE factor IN (%s)"
                 % ",".join("?" * len(names)))
            df = pd.read_sql(q, conn, params=list(names))
        else:
            df = pd.read_sql("SELECT ts_code, trade_date, factor, value FROM factor", conn)
    finally:
        conn.close()
    out = {}
    for fname, sub in df.groupby("factor"):
        pv = sub.pivot(index="trade_date", columns="ts_code", values="value")
        pv.index = pd.to_datetime(pv.index)
        out[fname] = pv
    return out


def _forward_return(m: dict, horizon: int = HORIZON) -> pd.DataFrame:
    """KGD 体检用的前向收益（后复权，朴素口径）。

    ⚠️ 与生产标签 `trainer.forward_return(..., delist_last_price=True)` **有意不同**：
    后者对"窗口内退市/长期停牌"的样本回退用最后有效价（不再是 NaN，2026-09-09 修复，
    实测影响 327 个样本）；本函数沿用旧的朴素口径。**统一需要 A/B**（独立审查 §2.3/#5 已登记），
    在那之前：`expert.kgd` 的读数与生产目标之间留着这条已知差异。
    """
    ac = m["close"] * m["adj"]
    return ac.shift(-horizon) / ac - 1.0


def _stat(s: pd.Series) -> dict:
    """均值 / 朴素 t / 命中率 / n。t = mean/(sd/√n) —— 月度读数存在自相关,
    真实 t 会比此值**略小**, 故门槛按保守方向设。"""
    s = pd.Series(s).dropna()
    if len(s) < 3:
        return {"mean": np.nan, "t": np.nan, "hit": np.nan, "n": len(s)}
    sd = s.std(ddof=1)
    return {"mean": float(s.mean()),
            "t": float(s.mean() / (sd / np.sqrt(len(s)))) if sd > 0 else np.nan,
            "hit": float((s > 0).mean()), "n": int(len(s))}


def head_excess_at(score: pd.Series, fwd: pd.Series, top_frac: float = TOP_FRAC) -> float:
    """单个截面的头部超额 = 按分数前 top_frac 的前向收益均值 − **同截面**均值得益。

    分母用「该截面被打分的股票」而非全市场 —— 两融标的/上榜股等稀疏因子的覆盖域
    本就不同, 拿全市场均值做基准会把「覆盖域差异」错读成「选股能力」。
    """
    j = pd.concat([score.rename("s"), fwd.rename("r")], axis=1).dropna()
    if len(j) < MIN_NAMES:
        return np.nan
    k = max(1, int(len(j) * top_frac))
    order = j["s"].sort_values(ascending=False).index
    return float(j["r"][order[:k]].mean() - j["r"].mean())


def load_scores(table: str, start: str = None, end: str = None) -> pd.DataFrame:
    """读打分表 → 宽表(index=日期, columns=ts_code)。只保留月末评分日由调用方保证。"""
    conn = db.get_conn()
    try:
        q = f"SELECT ts_code, trade_date, score FROM {table}"
        w, p = [], []
        if start:
            w.append("trade_date>=?"); p.append(start)
        if end:
            w.append("trade_date<=?"); p.append(end)
        if w:
            q += " WHERE " + " AND ".join(w)
        df = pd.read_sql(q, conn, params=p or None)
    finally:
        conn.close()
    if df.empty:
        return pd.DataFrame()
    pv = df.pivot(index="trade_date", columns="ts_code", values="score")
    pv.index = pd.to_datetime(pv.index)
    return pv.sort_index()


def daily_head_stats(score_wide: pd.DataFrame, fwd: pd.DataFrame,
                     md: pd.DatetimeIndex, top_frac: float = TOP_FRAC,
                     restrict_to: pd.DataFrame = None) -> pd.DataFrame:
    """逐评分日算头部超额 / top6 超额 / 覆盖数。

    restrict_to: 另一张分数宽表。给定时只在**两者共同打分的股票**上算 —— 头部超额是
    相对「同截面被打分的股票」的均值, 若两个 die 覆盖域不同(两融标的≈2300 只 vs 全市场
    ≈4700 只), 直接比各自的 head_excess 会把覆盖域差异读成选股能力差异。
    """
    rows = []
    for d in md:
        if d not in score_wide.index or d not in fwd.index:
            continue
        s, r = score_wide.loc[d], fwd.loc[d]
        if restrict_to is not None and d in restrict_to.index:
            s = s[s.index.isin(restrict_to.loc[d].dropna().index)]
        j = pd.concat([s.rename("s"), r.rename("r")], axis=1).dropna()
        if len(j) < MIN_NAMES:
            continue
        k = max(1, int(len(j) * top_frac))
        order = j["s"].sort_values(ascending=False).index
        mean_all = j["r"].mean()
        rows.append({"date": d, "n": len(j),
                     "head_excess": float(j["r"][order[:k]].mean() - mean_all),
                     "top6_excess": float(j["r"][order[:6]].mean() - mean_all)})
    return pd.DataFrame(rows).set_index("date") if rows else pd.DataFrame()


def rank_corr_with(baseline: pd.DataFrame, other: pd.DataFrame,
                   md: pd.DatetimeIndex) -> float:
    """逐截面 Spearman 的均值 —— 去相关程度的直接读数(与死区脚本 pair_corr 同法)"""
    cs = []
    for d in md:
        if d not in baseline.index or d not in other.index:
            continue
        x, y = baseline.loc[d], other.loc[d]
        m = x.notna() & y.notna()
        if int(m.sum()) < MIN_NAMES:
            continue
        xv = x[m].rank().to_numpy(dtype=float)
        yv = y[m].rank().to_numpy(dtype=float)
        if xv.std() == 0 or yv.std() == 0:
            continue
        cs.append(float(np.corrcoef(xv, yv)[0, 1]))
    return float(np.mean(cs)) if cs else np.nan


def bonferroni(p: float, n_tests: int) -> float:
    """良率校正: 测了 n 个 die, 单个 p 值须 < alpha/n 才算通过。"""
    if p != p:
        return np.nan
    return float(min(1.0, p * max(1, int(n_tests))))


def _p_two_sided(t: float) -> float:
    from math import erfc, sqrt
    if t != t:
        return np.nan
    return float(erfc(abs(t) / sqrt(2)))


# ---------------------------------------------------------------------------
# 3) KGD 测试台
# ---------------------------------------------------------------------------
def kgd(table: str, baseline: str = "model_score", n_tests: int = 1,
        market: dict = None, label: str = None) -> dict:
    """单个 die 的 Known-Good-Die 体检。

    返回三段读数(DEAD / ACTIVE / FULL) + 与基线的秩相关 + Bonferroni 校正后的判定。
    判据(预注册, 见模块头部常量): 死区头部超额 ≥ 0.5%/月、t ≥ 2、与活跃区同号、
    校正后 p < 0.05、且与基线秩相关 < 0.95(否则是换皮, 无去相关价值)。

    ⚠️⚠️ **五条判据全部只看 head(前 10%)—— 而策略买的是 top-6。这是构造上的盲点,
    不是调参能修的。** 2026-09-11 已有**三个 KGD 通过却对策略无用的反例**(全部有留证):

    | die | KGD 死区 head | 策略真实结果 |
    |---|---|---|
    | `die_flow` | +0.51% t=2.97 | top6 几乎全是 2023 一年, 剔掉后 ≈ 0 |
    | `die_head_vt` | +1.59% t=4.85 | **组合层两窗两轴全否**(年化 −5.81pp / −3.95pp) |
    | `die_h10` | +1.35% t=3.72 | **top6 三区段全负**(−0.62% / −0.87% / −0.38%) |

    `die_h10` 最干净: 2021 年 head **+1.82%** 而 top6 **−0.12%**、2022 年 head **+1.25%**
    而 top6 **−4.57%** —— **"前 10% 平均好"与"前 6 名好"可以符号相反**。

    ⇒ **`kgd()` 的正确用法是"入场券"(噪声筛), 不是"判决"(值不值封装)。**
      真正判决必须走组合层两轴 —— 见 `scripts/oneoff/exp_die_stage_b.py`。
      `rec` 里本就带 `{DEAD,ACTIVE,FULL}_top6`, **只报 head 不报 top6 等于挑有利的那个**。
      下一轮若要把 top6 并入判据, 必须**事前预注册**(不得因为本注释就追溯改判: 上面
      三个 die 的结论已经别处记录, 不复判)。
    """
    from app.factors.factor_engine import load_market, month_end_dates

    label = label or table
    market = market or load_market()
    dates = pd.to_datetime(market["close"].index)
    md_all = month_end_dates(dates)
    fwd = _forward_return(market)

    sc = load_scores(table)
    if sc.empty:
        return {"name": label, "table": table, "PASS": False, "reason": "无分数"}
    md = md_all[(md_all >= sc.index.min()) & (md_all <= sc.index.max())]
    zones = zone_of(md)
    det = daily_head_stats(sc, fwd, md)
    if det.empty:
        return {"name": label, "table": table, "PASS": False, "reason": "无有效截面"}

    rec = {"name": label, "table": table, "n_months": len(det)}
    for zone in ("DEAD", "ACTIVE"):
        sub = det[det.index.isin(md[zones == zone])]
        st = _stat(sub["head_excess"])
        rec[f"{zone}_head"], rec[f"{zone}_t"] = st["mean"], st["t"]
        rec[f"{zone}_hit"], rec[f"{zone}_n"] = st["hit"], st["n"]
        rec[f"{zone}_top6"] = _stat(sub["top6_excess"])["mean"]
    rec["FULL_head"] = _stat(det["head_excess"])["mean"]
    rec["FULL_t"] = _stat(det["head_excess"])["t"]
    rec["avg_names"] = float(det["n"].mean())

    p = _p_two_sided(rec.get("DEAD_t"))
    rec["p_adj"] = bonferroni(p, n_tests)

    # 与基线: 秩相关(同集合交集上算) + 基线的分段读数(同一把尺, 便于并列对照)
    rec["corr_with_baseline"] = np.nan
    # 2026-09-12 审查 P2-10: 记下「要求了对比但没比成」的情形 —— 原实现让 corr 停在
    # NaN, 而 corr_ok = (corr != corr) or ... 把 NaN 当宽松分支放行, 于是**基线表读不到
    # 反而让去相关门禁自动通过**, 恰好是最该拦住的一刻。
    baseline_failed = ""
    if baseline:
        try:
            bl = load_scores(baseline)
            if bl.empty:
                baseline_failed = "基线表为空"
            else:
                rec["corr_with_baseline"] = rank_corr_with(bl, sc, md)
                # 基线的读数限制在**两者共同打分的股票**上, 才是同集合对照
                bdet = daily_head_stats(bl, fwd, md, restrict_to=sc)
                if not bdet.empty:
                    rec["baseline_DEAD_head"] = _stat(
                        bdet[bdet.index.isin(md[zones == "DEAD"])]["head_excess"])["mean"]
                    rec["baseline_ACTIVE_head"] = _stat(
                        bdet[bdet.index.isin(md[zones == "ACTIVE"])]["head_excess"])["mean"]
                    rec["baseline_avg_names"] = float(bdet["n"].mean())
        except Exception:
            baseline_failed = "基线读取/对比抛异常"
            log.exception("基线 %s 读取/对比失败", baseline)

    hd, ha = rec.get("DEAD_head", np.nan), rec.get("ACTIVE_head", np.nan)
    same = (KGD_REQUIRE_SAME_SIGN and hd == hd and ha == ha
            and np.sign(hd) == np.sign(ha))
    corr = rec.get("corr_with_baseline", np.nan)
    if not baseline:
        corr_ok = True          # 压根没要求对比(无基线): 不拦, 既有语义
    elif baseline_failed or corr != corr:
        corr_ok = False         # 要求了却没比成 ⇒ 不许当"通过"放行
        rec["reason"] = rec.get("reason") or "基线对比未完成: {}".format(
            baseline_failed or "相关系数算不出")
    else:
        corr_ok = abs(corr) < KGD_MAX_CORR
    rec["same_sign"] = bool(same)
    rec["corr_ok"] = bool(corr_ok)
    rec["PASS"] = bool(same and corr_ok
                       and hd == hd and abs(hd) >= KGD_MIN_HEAD
                       and rec.get("DEAD_t", np.nan) == rec.get("DEAD_t", np.nan)
                       and rec["DEAD_t"] >= KGD_MIN_T
                       and rec["p_adj"] == rec["p_adj"] and rec["p_adj"] < KGD_ALPHA)
    return rec
