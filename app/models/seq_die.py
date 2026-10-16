"""时序 die —— PatchTST 式 Transformer(2026-09-11 「芯片式异构架构」阶段 5)

对应用户框架图里 `LightGBM | Transformer → Ensemble` 的那个 **Transformer** 盒子。
这是框架图里最后一个此前没有实现物的模块。

## 它是什么, 以及它**不是**什么

**口粮 = 日频量价原始序列**(60 个交易日的 10 个通道), 与 D1 的「31 个月频工程因子」
是**同一份底层数据(行情)的两种表示**。所以:

- 它是**表示异构**, 不是**信息源异构** —— 与 D1 同源, 独立性天然有限。
  真正独立的是 `die_flow` 那类(融资融券/资金流, 与行情是不同的东西)。
- 方案 §5 阶段 5 对它的定位**不是"更高精度"** —— 原文:
  「**价值定位是去相关, 不是更高精度** —— 若与 D1 秩相关 > 0.95 则直接退役」。
  按这条, 本 die 的**预期结局是退役**。跑它是为了把这句话从判断变成读数。

## 为什么可以用它了(前置条件已核实, 2026-09-11)

方案 §5 写的是「前置条件: 全历史日频因子重算」。**这个前置条件不需要** ——
`factor_engine.load_market()` 本来就返回**全历史日频**的量价矩阵(1735 日 × 5795 只 ×
16 通道, 实测), 序列模型吃的是原始量价, 不是 `factor` 表的月末截面。
另: `torch 2.14.0+cpu` **已经装在 .venv 里**(本机另有 RTX 4060, 但装的是 CPU 版)。
⇒ 阶段 4/5 的「需装 PyTorch」这个阻塞项其实不存在。

## 预注册(全部冻结在 `SeqConfig`, 写于跑之前)

判据(与其它 die **同一把尺子**, 不新造): `expert.kgd(table, n_tests=1)` 五项 +
组合层两轴(`scripts/oneoff/exp_die_stage_b.py`)。

⚠️ **`expert.kgd` 是入场券不是判决** —— 已有 4 个「KGD 通过却对策略无用」的反例
(`die_flow` / `head_vt` / `h10` / `margin_net_share`)。**只有组合层两轴是判决。**

## 无前视的四道闸(逐条对应代码)

1. **特征只用过去** —— 全部特征由 `rolling` / `pct_change` 构成, 天然后视
2. **截面标准化按日** —— 每个交易日在**当天**的横截面上做 z-score, 不用全期统计量
3. **训练窗复用 `trainer._train_window`** —— `md_all[i-36 : i-1]`, 与生产**同一行代码**,
   不是照抄一份(照抄会漂移)
4. **可交易域复用 `PITEngine.tradable_mask`** —— 训练域 = 选股域

## 它不写任何生产表

打分表默认 `model_score_die_seq`, 由 `expert.ensure_score_table` 建表。
**不产生任何模型归档文件** —— 与 `expert.run_expert` 不同, 这里没有 `lgb_*.txt` 要落盘,
所以 `data/models/ens/` 被覆盖那类事故(2026-09-11 实测 469 个文件里 464 个被覆盖)
在本模块**结构上不可能发生**。
"""
from __future__ import annotations

import dataclasses
import datetime
import logging
from dataclasses import dataclass

import numpy as np
import pandas as pd

from app import db

log = logging.getLogger(__name__)

# 日频通道(冻结)。全部由 `load_market` 的字段构成, 顺序即输入通道顺序 —— **改顺序等于改模型**。
FEATURES = ("ret1", "ret5", "ret20", "hl", "co", "amt_r", "vol_r", "ma20", "ma60", "turn")


@dataclass(frozen=True)
class SeqConfig:
    """**预注册**: 架构与训练超参全部冻结在这里, 写于跑之前, 事后不得增删。

    `frozen=True` 是刻意的 —— 想改一个数就得新开一轮并说明为什么上一轮不够,
    不能在跑的过程中"顺手调一下"。
    """
    # ---- 输入 ----
    lookback: int = 60          # 回看多少个交易日
    patch: int = 10             # PatchTST 的 patch 长度
    stride: int = 5             # patch 步长 ⇒ patch 数 = (60-10)/5+1 = 11
    # ---- 架构(小模型: 有效验证样本只有 24 个月, 大模型在这个样本量上无法被证真或证伪) ----
    d_model: int = 64
    nhead: int = 4
    nlayers: int = 2
    ff: int = 128
    dropout: float = 0.1
    # ---- 训练 ----
    epochs: int = 12
    lr: float = 1e-3
    weight_decay: float = 1e-4
    batch: int = 512
    train_months: int = 36      # 与生产同口径(不是调参对象)
    max_train_names: int = 1200  # 每月抽多少只进训练 —— 控制内存与时间的**唯一**妥协
    horizon: int = 21           # 与生产同口径
    seed: int = 0               # **单种子**: 种子的用途是方差缩减, 而本 die 问的是"独不独立"

    def n_patch(self) -> int:
        return (self.lookback - self.patch) // self.stride + 1


# ---------------------------------------------------------------------------
# 特征面板
# ---------------------------------------------------------------------------
def feature_panel(m: dict) -> tuple:
    """`load_market` 的 dict → `((交易日, 股票, 通道) 张量, 日期索引, 股票列)`。

    **全部特征只用过去**: 每个通道都是 `pct_change` 或后视 `rolling` —— 无一处用到 t 之后
    的信息。这一条是本模块能否成立的地基, 由 `tests/test_seq_die.py` 的
    `TestNoLookAhead` 逐通道证伪(改掉未来某天的价格, 之前所有天的特征必须逐位不变)。
    """
    ac = (m["close"] * m["adj"]).astype("float32")
    hi = (m["high"] * m["adj"]).astype("float32")
    lo = (m["low"] * m["adj"]).astype("float32")
    op = (m["open"] * m["adj"]).astype("float32")
    amt, vol = m["amount"].astype("float32"), m["vol"].astype("float32")
    out = [
        ac.pct_change(),
        ac.pct_change(5),
        ac.pct_change(20),
        (hi - lo) / ac,
        (ac - op) / op,
        amt / amt.rolling(20, min_periods=10).mean() - 1.0,
        vol / vol.rolling(20, min_periods=10).mean() - 1.0,
        ac / ac.rolling(20, min_periods=10).mean() - 1.0,
        ac / ac.rolling(60, min_periods=30).mean() - 1.0,
        m["turnover"].astype("float32"),
    ]
    # (通道, 日, 股) → (日, 股, 通道); 先取共同索引, 避免 reindex 出重复行
    idx = out[0].index
    cols = out[0].columns
    for o in out[1:]:
        idx = idx.intersection(o.index)
        cols = cols.intersection(o.columns)
    arr = np.stack([o.reindex(index=idx, columns=cols).to_numpy(dtype="float32")
                    for o in out], axis=-1)
    return arr, idx, cols


def cs_zscore(panel: np.ndarray, rel_eps: float = 1e-5) -> np.ndarray:
    """**按日**截面 z-score: 每个交易日用自己的横截面均值/标准差。

    用全期统计量会前视(今天的 z 会用到明年的均值)。逐日横截面标准化在信息上等价于
    生产 `neutralize` 的去极值+z-score, 且**结构上不可能前视**。

    ⚠️ **退化截面必须记 0, 不能硬除**(2026-09-11 单测当场抓到): 某个通道在某个交易日
    可以**近似常数**(合成数据里 `(high-low)/close` 恒等 0.04), 此时 `sd` 是 float32 噪声
    (约 1e-8), 直接 `(x-μ)/sd` 会把舍入误差放大成 ±0.03 的假 z —— 数值上等于给模型喂
    纯噪声, 而它在回测里**看起来像信号**。判据用**相对**量级(`sd ≤ rel_eps × 当天量级`),
    因为绝对阈值会随通道量纲失效。真退化的日子 z=0 是诚实的: 那天这个通道确实没有
    横截面信息。
    """
    import warnings

    with warnings.catch_warnings():                     # 全 NaN 行: nanmax/nanstd 会告警
        warnings.simplefilter("ignore", RuntimeWarning)
        with np.errstate(invalid="ignore", divide="ignore"):
            mu = np.nanmean(panel, axis=1, keepdims=True)
            sd = np.nanstd(panel, axis=1, keepdims=True)
            scale = np.nanmax(np.abs(panel), axis=1, keepdims=True)
    flat = np.isfinite(sd) & (sd <= rel_eps * np.maximum(scale, 1e-12))
    z = (panel - mu) / np.where(flat, 1.0, sd)          # flat 处先换成 1, 避免除 0
    return np.where(flat, 0.0, z)


def _clean(window: np.ndarray, ffill: int = 5) -> tuple:
    """(L, 股, F) → 补缺后的 (股, L, F) + 有效掩码。

    停牌/缺数据先向前填充有限步, 仍缺的记 0(在截面 z 空间里 0 = 中性)。
    **最后一天必须是真数据** —— 否则这只票在评分日根本没在交易, 不该给分。
    """
    a = np.moveaxis(window, 1, 0)                       # (股, L, F)
    valid = np.isfinite(a[:, -1, :]).all(axis=1)        # 末日在交易
    n, L, F = a.shape
    # 沿**时间轴**前向填充: 先重排成 (股×通道, L) —— 每行是一条独立的时间序列, 时间落在
    # **列**上, 所以是 `axis=1`。
    # ⚠️ 两处都容易写错, 2026-09-11 单测各抓到一次:
    #   · 直接 reshape 成 (股, L×F) 再填 —— C 序下会把 (l, F−1) 的值灌进 (l+1, 0), 跨通道串味
    #   · 重排对了但忘了 axis=1 —— 默认 axis=0 是**跨股票**填, 等于把别人的价格搬过来
    flat = pd.DataFrame(a.transpose(0, 2, 1).reshape(n * F, L)).ffill(axis=1, limit=ffill)
    a = flat.to_numpy(dtype="float32").reshape(n, F, L).transpose(0, 2, 1)
    return np.nan_to_num(a, nan=0.0, posinf=0.0, neginf=0.0), valid


# ---------------------------------------------------------------------------
# 模型
# ---------------------------------------------------------------------------
def _torch():
    try:
        import torch
        return torch
    except Exception:                                   # pragma: no cover
        return None


def build_model(n_feat: int, sc: SeqConfig):
    """PatchTST 式(PatchTST, ICLR 2023) —— 把回看窗切成 patch 再编码。

    不用"每个时间步一个 token"的朴素写法: 序列只有 60 步且信噪比极低, 逐点 attention
    在这么短的窗口上学不到东西, 而 patch 化的归纳偏置(局部形状)正好对路。
    """
    torch = _torch()
    if torch is None:
        raise RuntimeError("torch 不可用 —— 本 die 需要 PyTorch")
    import torch.nn as tnn

    class PatchTransformer(tnn.Module):
        def __init__(self):
            super().__init__()
            npatch = sc.n_patch()
            self.embed = tnn.Linear(sc.patch * n_feat, sc.d_model)
            self.pos = tnn.Parameter(torch.zeros(1, npatch, sc.d_model))
            layer = tnn.TransformerEncoderLayer(
                sc.d_model, sc.nhead, sc.ff, sc.dropout,
                batch_first=True, norm_first=True)       # pre-LN: 小样本下更稳
            # enable_nested_tensor 在 pre-LN 下会被 torch 自动关掉并**每次都告警** ——
            # 显式关掉, 免得满屏噪声淹没真正的告警
            self.enc = tnn.TransformerEncoder(layer, sc.nlayers,
                                              enable_nested_tensor=False)
            self.norm = tnn.LayerNorm(sc.d_model)
            self.head = tnn.Linear(sc.d_model, 1)

        def forward(self, x):                            # (B, L, F)
            b = x.shape[0]
            p = x.unfold(1, sc.patch, sc.stride)         # (B, P, F, patch)
            p = p.permute(0, 1, 3, 2).reshape(b, sc.n_patch(), -1)
            h = self.enc(self.embed(p) + self.pos).mean(dim=1)
            return self.head(self.norm(h)).squeeze(-1)

    return PatchTransformer()


def _train_one(X, y, n_feat, sc: SeqConfig):
    torch = _torch()
    torch.manual_seed(sc.seed)
    np.random.seed(sc.seed)
    net = build_model(n_feat, sc)
    opt = torch.optim.AdamW(net.parameters(), lr=sc.lr, weight_decay=sc.weight_decay)
    lossf = torch.nn.MSELoss()
    xt = torch.from_numpy(np.ascontiguousarray(X, dtype="float32"))
    yt = torch.from_numpy(np.ascontiguousarray(y, dtype="float32"))
    n = len(xt)
    net.train()
    for ep in range(sc.epochs):
        perm = torch.randperm(n)
        tot = 0.0
        for i in range(0, n, sc.batch):
            b = perm[i:i + sc.batch]
            opt.zero_grad()
            loss = lossf(net(xt[b]), yt[b])
            loss.backward()
            torch.nn.utils.clip_grad_norm_(net.parameters(), 5.0)
            opt.step()
            tot += float(loss.detach()) * len(b)
        if (ep + 1) % 4 == 0:
            log.debug("    epoch %d/%d loss %.5f", ep + 1, sc.epochs, tot / max(n, 1))
    net.eval()
    return net


def _predict(net, X, sc: SeqConfig) -> np.ndarray:
    torch = _torch()
    out = []
    with torch.no_grad():
        for i in range(0, len(X), 8192):
            xb = torch.from_numpy(np.ascontiguousarray(X[i:i + 8192], dtype="float32"))
            out.append(net(xb).numpy())
    return np.concatenate(out) if out else np.array([], dtype="float32")


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def run_die(name: str = "seq", sc: SeqConfig = None, table: str = None,
            market: dict = None, start: str = "2017-01-01", end: str = None,
            max_months: int = None, since: str = "20130101") -> dict:
    """walk-forward 训练 PatchTST 并落 `model_score_die_<name>`。

    `max_months` 只用于**冒烟**(验证管道通), 冒烟的读数不是证据 —— 与
    `exp_gp_factory.py --smoke` 同一条纪律。
    """
    sc = sc or SeqConfig()
    table = table or f"model_score_die_{name}"
    from app.alpha.gate import forward_return
    from app.factors.factor_engine import (industry_map, load_market, month_end_dates)
    from app.models import trainer
    from app.models.expert import ensure_score_table
    from app.pit.engine import PITEngine

    ensure_score_table(table)
    if market is None:
        log.info("加载行情面板 since=%s …", since)
        market = load_market(since=since)
    dates = pd.to_datetime(market["close"].index)
    md_all = month_end_dates(dates, today=datetime.date.today())
    md_all = md_all[md_all >= pd.Timestamp("2014-01-01")]

    panel, p_idx, p_cols = feature_panel(market)
    log.info("特征面板 %s × %d 通道  (%s)", panel.shape, len(FEATURES), ", ".join(FEATURES))
    z = cs_zscore(panel)
    # 面板日期 → 行号, 供窗口切片
    row_of = {d: i for i, d in enumerate(p_idx)}

    conn = db.get_conn()
    industry = industry_map(conn)
    conn.close()
    labels_wide = forward_return(market, sc.horizon).loc[md_all]
    # ⚠️ reindex 会给**不在 industry_map 里的代码**引入 NaN 键, 而 pandas 的 groupby 默认
    # 丢掉 NaN 组 ⇒ 全部代码都缺映射时 `transform` 撞上空轴, 报的是
    # `IndexError: cannot do a non-empty take from an empty axes` —— 与真实原因(行业映射缺失)
    # 毫无字面关系, 排查会绕远路。2026-09-11 单测拿合成代码跑时当场踩到。
    inds = industry.reindex(labels_wide.columns).fillna("未知")
    # ⚠️ 更正（2026-09-20 审查，原注释写"标签口径与生产**逐字一致**(trainer.py:353)"是**错的**，
    # 行号也不对）：**减同行业均值**这一步确实与 `trainer.py:495` 字面相同；但标签本体来自
    # `from app.alpha.gate import forward_return`（:279，朴素版），而生产走
    # `trainer.forward_return(..., delist_last_price=True)` ⇒ **退市/长停尾部样本不同**，
    # 整体不是"逐字一致"。两者的差异被 `tests/test_label_conventions.py` 钉在尾部两格。
    labels_wide = labels_wide - labels_wide.T.groupby(inds, sort=False).transform("mean").T
    pit = PITEngine(market)

    months = [d for d in md_all if d >= pd.Timestamp(start)
              and (end is None or d <= pd.Timestamp(end))]
    if max_months:
        months = months[:max_months]
    log.info("评分月 %d 期: %s → %s", len(months),
             months[0].date() if months else "-", months[-1].date() if months else "-")

    rows, rng = [], np.random.RandomState(sc.seed)
    for d in months:
        i = int(np.where(md_all == d)[0][0])
        window = trainer._train_window(md_all, i, sc.train_months)
        window = [w for w in window if w in labels_wide.index]
        if len(window) < 12 or d not in row_of:
            log.warning("%s 训练窗口仅 %d 期, 跳过", d.date(), len(window))
            continue
        Xs, ys = [], []
        for w in window:
            ok = pit.tradable_mask(w, allow_bse=False)
            lab = labels_wide.loc[w].reindex(p_cols)
            good = lab.notna().to_numpy() & ok.reindex(p_cols).fillna(False).to_numpy()
            if good.sum() < 50:
                continue
            r = row_of[w]
            win = z[r - sc.lookback + 1: r + 1]           # (L, 股, F) —— 末日 = w
            if win.shape[0] < sc.lookback:
                continue
            arr, valid = _clean(win)
            keep = np.where(good & valid)[0]
            if len(keep) > sc.max_train_names:
                keep = rng.choice(keep, sc.max_train_names, replace=False)
            Xs.append(arr[keep])
            ys.append(lab.to_numpy(dtype="float32")[keep])
        if not Xs:
            log.warning("%s 无可用训练样本, 跳过", d.date())
            continue
        X = np.concatenate(Xs); y = np.concatenate(ys)
        # 目标标准化: 只是常数缩放, 不改排序; 不缩放的话 MSE 梯度比特征小两个量级
        ysd = float(y.std()) or 1.0
        net = _train_one(X, y / ysd, panel.shape[2], sc)

        r = row_of[d]
        win = z[r - sc.lookback + 1: r + 1]
        arr, valid = _clean(win)
        pred = _predict(net, arr, sc) * ysd
        # 评分域也过 tradable_mask —— 训练域 = 选股域, 否则分数里混进买不到的票
        ok = pit.tradable_mask(d, allow_bse=False).reindex(p_cols).fillna(False).to_numpy()
        sel = np.where(valid & ok)[0]
        if not len(sel):
            continue
        for j in sel:
            rows.append((p_cols[j], d.strftime("%Y%m%d"),
                         float(pred[j]), float(pred[j])))
        log.info("  %s: 训练 %d 样本 → 打分 %d 只", d.date(), len(X), len(sel))

    if not rows:
        log.error("没有任何分数产出 —— 检查面板覆盖与 tradable_mask")
        return {"table": table, "n_months": 0, "n_rows": 0, "config": dataclasses.asdict(sc)}

    df = pd.DataFrame(rows, columns=["ts_code", "trade_date", "score", "pred_ret"])
    conn = db.get_conn()
    try:
        for d, sub in df.groupby("trade_date"):
            conn.execute(f"DELETE FROM {table} WHERE trade_date=?", (d,))
            db.upsert_df(conn, sub, table)
        conn.commit()
    finally:
        conn.close()
    log.info("写入 %s: %d 行 / %d 期", table, len(df), df["trade_date"].nunique())
    return {"table": table, "n_rows": int(len(df)),
            "n_months": int(df["trade_date"].nunique()),
            "config": dataclasses.asdict(sc),
            "note": "读数不是判决 —— 判决只能是组合层两轴(exp_die_stage_b.py)"}
