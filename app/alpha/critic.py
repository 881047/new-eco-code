"""Devil / Critic —— 否证实验生成器(2026-09-11)

对应用户框架图的 `Devil / Critic`。**定位必须说清楚, 否则它是有害的:**

    ❌ Critic **不产出结论**。让一个 LLM/AI 看完回测说"这个因子不行"或"这个因子很好",
       在统计上等于没有信息 —— 它既不能被证伪, 也不能被重放。
    ✅ Critic **产出可预注册的否证实验 + 它们的读数**。它负责问"这个读数可能是怎么来的",
       并把每一种怀疑变成**一个能算出数字的检验**。

本项目已有三个「门禁/ KGD 通过却对策略无用」的反例(`die_flow` / `head_vt` / `h10`),
每一个都有同一个特征: **某个读数很好看, 且它有一个没被检查的替代解释**。
`die_flow` 的替代解释是"全由 2023 一年贡献"; `head_vt` 的是"head 好看但 top6 不行"。
Critic 就是把这类替代解释**在跑之前**列出来。

## 硬规则

**Critic 的输出不得直接当过滤器用。** 任何"因为 Critic 说 X 所以不要这个 alpha"都是
事后加判据 —— 想加, 就得把它写进 `gate.py` 并**重跑**(而不是在结果上筛)。
本模块的读数只做两件事: ①写进 `alpha_bank.note` 留痕; ②作为下一轮预注册的输入。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from app.alpha import gate


def _head_of(wide: pd.DataFrame, fwd: pd.DataFrame, md, zones, mask=None) -> pd.DataFrame:
    if mask is not None:
        wide = wide.where(mask)
    return gate.evaluate(wide, fwd, md, zones)


def drop_best_year(wide: pd.DataFrame, fwd: pd.DataFrame, md, zones, mask=None) -> dict:
    """① 单年贡献: 逐剔除一年, 看死区 head 还剩多少。

    `die_flow` 就是这样被抓出来的(剔掉 2023 后 ≈ 0)。**保留所有年份的读数**,
    不只看"最好的那年" —— 只看最好的那年本身就是一种挑拣。
    """
    det = _head_of(wide, fwd, md, zones, mask=mask)
    if det.empty:
        return {}
    dead = det[det["zone"] == "DEAD"]
    if dead.empty:
        return {}
    full = gate._stat(dead["head_excess"])[0]
    years = sorted({d.year for d in dead.index})
    out = {"full": full, "by_drop": {}}
    for y in years:
        sub = dead[dead.index.year != y]
        m, t, hit, n = gate._stat(sub["head_excess"])
        out["by_drop"][int(y)] = {"head": m, "t": t, "hit": hit, "n": n}
    worst = min(out["by_drop"].items(),
                key=lambda kv: (kv[1]["head"] if kv[1]["head"] == kv[1]["head"] else 9e9))
    out["most_dependent_on"] = int(worst[0])
    out["head_wo_best_year"] = worst[1]["head"]
    out["caveat"] = ("单年依赖不必然是坏事(经济上可能是 regime), 但它必须被**看见** —— "
                     "声明'死区 head +X%'时若剔掉某一年就归零, 那句话就不完整")
    return out


def corr_with_known(wide: pd.DataFrame, others: dict, md) -> dict:
    """③ 换皮检验: 与已知因子的平均逐截面秩相关。

    > 0.95 ⇒ 与某个已有因子是同一个东西(`KGD_MAX_CORR` 的阈值), 无新增信息。
    `others` 可以是存量 31 因子、可以是其它候选。
    """
    out = {}
    for nm, other in others.items():
        cs = []
        for d in md:
            if d not in wide.index or d not in other.index:
                continue
            x, y = wide.loc[d], other.loc[d]
            m = x.notna() & y.notna()
            if int(m.sum()) < gate.MIN_NAMES:
                continue
            xv, yv = x[m].rank().to_numpy(float), y[m].rank().to_numpy(float)
            if xv.std() == 0 or yv.std() == 0:
                continue
            cs.append(float(np.corrcoef(xv, yv)[0, 1]))
        if cs:
            out[nm] = float(np.mean(cs))
    if out:
        mx = max(out.items(), key=lambda kv: abs(kv[1]))
        out["_max_abs"] = {"factor": mx[0], "rho": mx[1]}
    return out


def tail_dependence(wide: pd.DataFrame, fwd: pd.DataFrame, md, zones,
                    fracs=(0.05, 0.02), mask=None) -> dict:
    """④ 极值依赖: 把截面两端各 frac 的分数置 NaN 后再算 head。

    若 head 只在含极端值时成立, 说明读数由少数股票的极端分数撑起 ——
    而**极端分数同时也是最不可信的部分**(噪声最大的地方)。
    """
    out = {}
    for q in fracs:
        masked = wide.copy()
        for d in masked.index:
            row = masked.loc[d].dropna()
            if len(row) < gate.MIN_NAMES:
                continue
            k = max(1, int(len(row) * q))
            idx = row.sort_values()
            masked.loc[d, idx.index[:k]] = np.nan
            masked.loc[d, idx.index[-k:]] = np.nan
        det = _head_of(masked, fwd, md, zones, mask=mask)
        dead = det[det["zone"] == "DEAD"] if not det.empty else pd.DataFrame()
        if not dead.empty:
            m, t, hit, n = gate._stat(dead["head_excess"])
            out[f"drop_{int(q * 100)}pct"] = {"head": m, "t": t, "n": n}
    return out


def turnover(wide: pd.DataFrame, md, top_n: int = 6) -> dict:
    """⑧ 换手估算: 逐期 top-N 名单的相对换手(策略持有 top-6)。

    换手不是"越低越好"(本项目实测: 迟滞带只降换手却两轴都差)。这里只出**读数**,
    因为因子换手直接决定它在 5 元佣金地板下能不能用(2 万账户下单边费率对仓位极度敏感)。
    """
    prev, tos = None, []
    for d in md:
        if d not in wide.index:
            continue
        row = wide.loc[d].dropna()
        if len(row) < top_n:
            continue
        cur = list(row.sort_values(ascending=False).index[:top_n])
        if prev is not None:
            tos.append(len(set(cur) - set(prev)) / float(top_n))
        prev = cur
    if not tos:
        return {}
    return {"mean_top6_turnover": float(np.mean(tos)), "n": len(tos),
            "note": "名单换手; 成本要按 app/portfolio/fees.py 逐笔算, 不按百分比"}


def churn_cost(wide: pd.DataFrame, md, top_n: int = 6,
               per_name_notional: float = None, cfg: dict = None) -> dict:
    """⑨ 边际成本读数（门禁 v2 ①，判据见 KB 14 号 三补十四）。

    把 `turnover()` 的名单换手换算成**钱**：`自换手率 × 往返真实费率`，单位是
    "占仓位的比例 / 月"。费率**不写常量**——一律走 `app/portfolio/fees.py` 的
    `effective_rate()`（含 5 元最低佣金、印花税、过户费），所以费率表一改这里自动跟着改。

    - `per_name_notional` 缺省 = `weekly.capital ÷ 100`（≈ 1 万元/票）——取这个量级是为了让
      **5 元最低佣金**真的咬得住：名义金额太小则费率被佣金地板抬高，太大则趋近纯费率。
    - 口径：自换手率是"采纳该因子后**新增**换手"的**上界**（因子自己的换手 ≥ 它带来的边际换手）
      ⇒ 换算出的成本只多不少，门禁只会更严。

    返回 `{}`（无有效期数）时，调用方**必须**把它当"成本算不出"处理 —— 门禁那侧是 fail-closed。
    """
    to = turnover(wide, md, top_n=top_n)
    if not to:
        return {}
    from app import config as cfg_mod
    from app.portfolio import fees as fees_mod
    cfg = cfg if cfg is not None else cfg_mod.load_config()
    p = fees_mod.fee_params(cfg)
    if per_name_notional is None:
        cap = float((cfg.get("weekly") or {}).get("capital", 1_000_000))
        per_name_notional = cap / 100.0
    buy = fees_mod.effective_rate(per_name_notional, "buy", None, p)
    sell = fees_mod.effective_rate(per_name_notional, "sell", None, p)
    rt = float(buy + sell)
    churn = float(to["mean_top6_turnover"])
    return {"mean_top6_turnover": churn, "round_trip_rate": rt,
            "buy_rate": float(buy), "sell_rate": float(sell),
            "per_name_notional": float(per_name_notional),
            "cost_per_month": churn * rt, "n": to["n"],
            "note": "自换手 × 往返真实费率; 自换手是新增换手的上界 ⇒ 成本只多不少"}


def style_exposure(wide: pd.DataFrame, md, style: dict) -> dict:
    """⑤ 风格暴露: 与 size / turnover / vol 等风格因子的秩相关。

    本项目两因子回归的结论: **唯一统计显著的暴露是小盘价差(t=2.74)**,
    残余 alpha 10.47%/年 t=1.75 **不显著**。故一个"新 alpha"必须先回答:
    它的收益是不是又一次小盘/高波的暴露(那就是 beta, 不是 alpha)。
    """
    return corr_with_known(wide, style, md)


def attack_all(alpha_id: str, wide: pd.DataFrame, fwd: pd.DataFrame, md, zones,
               known: dict = None, style: dict = None, mask=None) -> dict:
    """对一个候选跑完整的否证清单。**返回读数, 不返回结论。**

    `mask`（2026-09-13 新增，独立审查 #4）：PIT 可交易域。**不传就与门禁不是同一个域** ——
    门禁的 head_excess 只在买得到的票上算，而否证清单若在全市场算，两者的读数会系统性不同。
    调用方（`Pipeline.critic`）应传 `factory.tradable_mask(m, md)`。
    """
    rep = {"alpha_id": alpha_id, "n_months": int(len(md))}
    if mask is None:
        rep["_mask_warning"] = ("未传 PIT 可交易域 ⇒ 本报告的 head/tail 读数与门禁不是同一个域；"
                                "请用 critic.attack_all(..., mask=...) 重跑")
    for key, fn in (("drop_best_year", drop_best_year),
                    ("tail_dependence", tail_dependence)):
        try:
            rep[key] = fn(wide, fwd, md, zones, mask=mask)
        except Exception as e:      # 否证工具本身不该拖垮流程, 但要留痕
            rep[key] = {"error": f"{type(e).__name__}: {e}"}
    try:
        rep["turnover"] = turnover(wide, md)
    except Exception as e:
        rep["turnover"] = {"error": str(e)}
    if known:
        rep["corr_known"] = corr_with_known(wide, known, md)
    if style:
        rep["corr_style"] = style_exposure(wide, md, style)
    rep["_disclaimer"] = ("本报告的每一条都是**读数**。任何据此的取舍都必须先写进 "
                          "gate.py 再重跑 —— 在结果上筛选 = 看到结果后换判据")
    return rep
