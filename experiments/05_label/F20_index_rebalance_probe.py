"""F20 · 指数调样(纳入/剔除)作为**外生信息源**的探针 —— 阶段②的真异构候选

## 为什么这条值得单独测

前几轮把"同数据换算法"的路走完了(Transformer 序列 die −5.42pp、多周期标签噪声级、
新数据族四种用法全否)。**指数调样是外生信息** —— 决定来自指数委员会, 不由价格派生,
所以它与所有基于量价的 die 在信息上天然不同(这正是"按信息源切 die"要的那种异构)。

数据: 本地 `index_weight` 的 6 个指数月度快照(中证1000/800/500、沪深300、创业板指、科创50,
2019-01~2026-08, 92 期)。相邻两期取差集 ⇒ **纳入 / 剔除**事件。

## 口径与防前视

- 事件日 = **快照日**(该期权重数据出现的时点); 用它之后的信息一律不用;
- 因子值(周锚点 t) = 各指数事件的**衰减和**: 纳入 +1、剔除 −1, 126 个自然日内线性衰减;
- 票池/标签沿用生产同口径(可交易域 + 可交易窗口 5 日超额)。

## 预注册判据(与 F17 台账同款)

    ① 死区与活跃区 IC **同号** 且 |IC_死区| ≥ 0.02;
    ② 独立组合(100万/top100/带8×/真费用)超额 > 0;
三条同时满足 ⇒ 该因子可作 die 进入融合; 否则记"指数调样无可用 alpha"(并说明只测了
月度快照可见的那部分——真实调样公告通常提前 ~2 周发布, 若数据只有快照日, 本实验是该效应的**下界**)。

用法:
    ./.venv/Scripts/python.exe -X utf8 experiments/05_label/F20_index_rebalance_probe.py
"""
import hashlib
import importlib.util
import json
import logging
import sqlite3
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

from app import config as cfg_mod            # noqa: E402
from app.factors import factor_engine as fe  # noqa: E402
from app.pit.engine import PITEngine         # noqa: E402

log = logging.getLogger("f20")
OUT = HERE / "F20_index_rebalance_probe_result.json"
CAPITAL = 1_000_000.0
DECAY_DAYS = 126          # 事件影响窗口(自然日)


def build_factor(conn, wk: pd.DatetimeIndex, cols, strict: bool = True) -> pd.DataFrame:
    """指数调样因子: 每个周锚点上, 各指数最近 126 天内 纳入(+1)/剔除(−1) 的线性衰减和。

    ⚠️ **数据完整性守卫(2026-09-13 实跑抓到, 这条不加会算出假 alpha)**:
    本地 `index_weight` **不完整** —— 以 000906.SH 为例, 有 21 个月缺快照
    (2019-12-31 直接跳到 2020-04-30), 且部分快照只抓到 **600/800** 只。
    于是 600→800 的差分被算成"进 200 / 出 0" ⇒ 每个 5 月都凭空出现 200 个"纳入"事件,
    因子被这些假事件主导, 组合回测读出 **+73.5%/年、超额 +56.83pp**(真实 IC 却是 **−0.0286**,
    方向自相矛盾)。**这就是本次假结果的来源**。

    strict=True(默认) ⇒ 丢弃"规模不等于该指数众数"的快照(部分快照), 并**只比较相邻快照**;
    若因此事件太少, 会在返回值与日志里如实反映, 而不是拿假事件凑数。
    """
    w = pd.read_sql("SELECT index_code, con_code, trade_date FROM index_weight", conn)
    w["d"] = pd.to_datetime(w["trade_date"])
    if strict:
        sz = w.groupby(["index_code", "trade_date"]).size().rename("n").reset_index()
        mode = sz.groupby("index_code")["n"].agg(lambda s: s.mode().iloc[0]).rename("mode")
        sz = sz.merge(mode, on="index_code")
        bad = sz[sz["n"] != sz["mode"]]
        if len(bad):
            log.warning("丢弃 %d 个**部分快照**(规模≠众数), 例: %s",
                        len(bad), bad.head(3).to_dict("records"))
        keep = set(zip(sz.loc[sz["n"] == sz["mode"], "index_code"],
                       sz.loc[sz["n"] == sz["mode"], "trade_date"]))
        w = w[[ (i, t) in keep for i, t in zip(w["index_code"], w["trade_date"]) ]]
    events = []                                  # (date, code, sign, index)
    for idx, g in w.groupby("index_code"):
        snaps = sorted(g["d"].unique())
        prev_s, prev = None, None
        for s in snaps:
            cur = set(g.loc[g["d"] == s, "con_code"])
            if prev is not None:
                # 只在**相邻月份**之间取差: 跨月缺口的差分会把期间所有变动堆到一天
                months = (s.year - prev_s.year) * 12 + (s.month - prev_s.month)
                if months == 1:
                    for c in cur - prev:
                        events.append((s, c, +1, idx))
                    for c in prev - cur:
                        events.append((s, c, -1, idx))
            prev_s, prev = s, cur
    ev = pd.DataFrame(events, columns=["d", "con_code", "sign", "index_code"])
    log.info("调样事件 %d 条(纳入 %d / 剔除 %d), 覆盖 %d 只",
             len(ev), int((ev["sign"] > 0).sum()), int((ev["sign"] < 0).sum()),
             ev["con_code"].nunique() if len(ev) else 0)
    F = pd.DataFrame(np.nan, index=wk, columns=cols)
    if ev.empty:
        return F
    for j, d in enumerate(wk):
        lo = d - pd.Timedelta(days=DECAY_DAYS)
        sub = ev[(ev["d"] <= d) & (ev["d"] >= lo)]
        if sub.empty:
            continue
        age = (d - sub["d"]).dt.days.to_numpy()
        val = sub["sign"].to_numpy() * (1.0 - age / DECAY_DAYS)
        s = pd.Series(val, index=sub["con_code"]).groupby(level=0).sum()
        F.iloc[j] = s.reindex(cols)
    return F


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        datefmt="%H:%M:%S", force=True)
    t0 = time.time()
    cfg = cfg_mod.load_config()
    m = fe.load_market(since="20140101")
    dates = pd.to_datetime(m["close"].index)
    wk = f1.week_end_dates(dates)
    pit = PITEngine(m=m, cfg=cfg)
    cols = list(m["close"].columns)
    mask = f1.tradable_frame(pit, wk, cols)
    conn = sqlite3.connect(f"file:{BASE/'data'/'neweco.db'}?mode=ro", uri=True)
    F = build_factor(conn, wk, cols)
    conn.close()
    cov = F.notna().to_numpy().mean()
    log.info("因子覆盖: 锚点×股票格子 %.2f%% | 有事件的锚点 %d/%d",
             100 * cov, int(F.notna().any(axis=1).sum()), len(wk))

    lab = f1b.forward_excess_open(m, mask, 5)
    Yr = f1._rank(lab)
    okv = lab.notna().to_numpy()
    Xr = f1._rank(F)
    ic = pd.Series(f1.row_ic(Xr, Yr, okv & np.isfinite(Xr)), index=wk).dropna()
    st = {"n": int(len(ic)), "ic": round(float(ic.mean()), 4) if len(ic) else None,
          "DEAD": f1b.zone_stat_hac(ic, "DEAD", 2) if len(ic) else None,
          "ACTIVE": f1b.zone_stat_hac(ic, "ACTIVE", 2) if len(ic) else None}
    log.info("IC(全史) %s | 死区 %s | 活跃区 %s", st["ic"], st["DEAD"], st["ACTIVE"])

    op_adj = (m["open"] * m["adj"]).reindex(index=dates, columns=cols).astype(np.float64).to_numpy()
    pos_of = {d: i for i, d in enumerate(dates)}
    mask_np = mask.to_numpy()
    res = {"script_sha": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:16],
           "decay_days": DECAY_DAYS, "coverage": round(float(cov), 5), "ic": st,
           "portfolio": {}, "verdict": {}, "secs": None}
    df = f1b.run_overlap_portfolio(m, {}, wk, mask, 5, {**cfg, "capital": CAPITAL}, pit, 0,
                                   score_matrix=F, top_n=100, rank_band=8.0, hyst=0.2)
    # ⚠️ **带宽守卫(2026-09-13 实测)**: 排名退出带 8×top_n = 800, 而本因子的候选池只有
    # "近 126 天内发生过纳入/剔除"的约 250 只 ⇒ **持仓永远落在带内, 组合从不卖出**,
    # 退化成"买入持有最初那批事件股"。这样得到的"超额"是买入持有与基准的比较,
    # **不是因子检验**(实测: IC = −0.0409 为负, 组合却报 +45.57pp —— 方向自相矛盾)。
    n_pool = int(np.isfinite(F.to_numpy()).sum(axis=1).max())
    res["pool_guard"] = {"max_pool": n_pool, "band_width": 800,
                         "band_meaningful": bool(n_pool > 800)}
    if n_pool <= 800:
        log.error("候选池只有 %d 只 ≤ 带宽 800 ⇒ 退出带失效、组合退化成买入持有; "
                  "本次组合读数**不可作为因子检验**(只看 IC)", n_pool)
    if df is not None:
        bench, prev_exec, prev_ap = [], None, None
        for _, r in df.iterrows():
            e = pos_of[r["date"]]
            if prev_exec is None or prev_ap is None:
                bench.append(np.nan)
            else:
                good = (mask_np[int(prev_ap)] & np.isfinite(op_adj[prev_exec])
                        & np.isfinite(op_adj[e]) & (op_adj[prev_exec] > 0))
                p0, p1 = op_adj[prev_exec], op_adj[e]
                bench.append(float(np.mean(p1[good] / p0[good] - 1)) if good.sum() >= 200 else np.nan)
            prev_exec, prev_ap = e, int(r["anchor_pos"])
        df["bench_ret"] = bench
        ok = df["ret"].notna() & df["bench_ret"].notna()
        a_, b_ = f1b.ann(df.loc[ok, "ret"], 52.0), f1b.ann(df.loc[ok, "bench_ret"], 52.0)
        nav = (1 + df.loc[ok, "ret"]).cumprod()
        res["portfolio"] = {"n": int(ok.sum()), "excess_pp": round(100 * (a_ - b_), 2),
                            "ann": round(a_, 4), "mdd": round(float((nav / nav.cummax() - 1).min()), 4),
                            "turnover": round(float(df.loc[ok, "turnover"].mean()), 3)}
        d = res["portfolio"]
        log.info("独立组合: 超额 %+.2fpp | 年化 %+.1f%% | MDD %.1f%% | 换手 %.3f | 期数 %d",
                 d["excess_pp"], 100 * d["ann"], 100 * d["mdd"], d["turnover"], d["n"])
    z1, z2 = st["DEAD"] or {}, st["ACTIVE"] or {}
    same = (z1.get("ic") is not None and z2.get("ic") is not None
            and (z1["ic"] > 0) == (z2["ic"] > 0))
    # **荒谬值守卫**: 年化超过 200% 一律判为"结果不可信"(疑似价格数据错误/样本过稀疏),
    # 不得据此判"可作 die"。2026-09-13 实测: 清洗部分快照后组合年化读出 +1.97 万亿 %,
    # 与 IC(−0.032)方向矛盾 ⇒ 这类读数只能当作"实验失败", 不能当作发现。
    ann_pct = 100 * res["portfolio"].get("ann", 0.0)
    absurd = bool(abs(ann_pct) > 200)
    if absurd:
        log.error("结果不可信: 组合年化 %+.0f%%(阈值 200%%) ⇒ 判为实验失败, 不作发现", ann_pct)
    res["verdict"] = {
        "ic_same_sign_and_ge_0.02": bool(same and abs(z1.get("ic") or 0) >= 0.02),
        "excess_positive": bool(res["portfolio"].get("excess_pp", -9) > 0),
        "absurd_return_guard": absurd,
        "zones": {"DEAD": z1.get("ic"), "ACTIVE": z2.get("ic")},
    }
    res["verdict"]["adopt"] = bool(res["verdict"]["ic_same_sign_and_ge_0.02"]
                                   and res["verdict"]["excess_positive"] and not absurd)
    if n_pool <= 800:
        res["verdict"]["adopt"] = False          # 组合臂无效 ⇒ 不得据此采纳
        res["verdict"]["invalid_portfolio_arm"] = True
    res["secs"] = round(time.time() - t0, 1)
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    log.info("== 判决: 指数调样可作 die = %s (IC判据 %s / 超额 %s) | %.1fs → %s ==",
             res["verdict"]["adopt"], res["verdict"]["ic_same_sign_and_ge_0.02"],
             res["verdict"]["excess_positive"], res["secs"], OUT)


if __name__ == "__main__":
    main()
