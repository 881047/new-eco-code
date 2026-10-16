"""Alpha Critic 报告 —— 对 alpha_bank 里的候选跑 Devil/Critic 否证清单(2026-09-11)

对应用户框架图的 `Alpha Pool → Devil/Critic`。**本脚本产出读数, 不产出结论** ——
见 `app/alpha/critic.py` 的硬规则: Critic 的输出不得直接当过滤器用, 任何"因为 Critic
说 X 所以不要这个 alpha"都是事后加判据; 想加就写进 `gate.py` 并**重跑**。

它回答的是五个具体的、能算出数字的问题:

| 问题 | 读数 | 为什么问 |
|---|---|---|
| 剔掉贡献最大的那一年, 死区 head 还剩多少? | `drop_best_year` | `die_flow` 就是这样被拆穿的(剔 2023 后归零) |
| head 是不是靠两端极值撑起来的? | `tail_dependence` | 极端分数也是噪声最大的地方 |
| 它与存量 31 因子是不是同一个东西? | `corr_known` | \|ρ\|>0.95 = 换皮, 无新增信息 |
| 它的收益是不是又一次小盘/高波暴露? | `corr_style` | 两因子回归里唯一显著的暴露就是小盘价差 |
| 它的 top-6 名单换手多高? | `turnover` | 换手直接决定它在 5 元佣金地板下能不能用 |

用法:
    ./.venv/Scripts/python.exe scripts/oneoff/alpha_critic_report.py                # 全部 POOLED
    ./.venv/Scripts/python.exe scripts/oneoff/alpha_critic_report.py margin_net_share
输出: 控制台 + D:/claude-home/tmp/critic_<alpha_id>.json + 摘要写回 alpha_bank.note
"""
import io
import json
import logging
import sys
from pathlib import Path

# ⚠️ 先留住原 wrapper: io.TextIOWrapper(sys.stdout.buffer, ...) 后旧 wrapper 被 GC 会
# 关掉底层 buffer(症状=日志正常但控制台全是 closed file)。2026-09-11 踩过。
if hasattr(sys.stdout, "buffer"):
    _ORIG_STDOUT = sys.stdout
    sys.stdout = io.TextIOWrapper(_ORIG_STDOUT.buffer, encoding="utf-8", errors="replace")

BASE = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE))

import pandas as pd                                       # noqa: E402

from app.alpha import bank, critic, gate                  # noqa: E402
from app.factors import factor_engine as fe                # noqa: E402
from app.models import expert as ex                        # noqa: E402

OUT_DIR = Path("D:/claude-home/tmp")
EVAL_SINCE = "2019-01-01"
WARMUP_DAYS = 400
# 风格代理(存量因子里现成的): 小盘 / 换手 / 波动 / 非流动性 / 动量 / 市场beta
STYLE = ["size", "turnover_20", "vol_20", "illiq", "mom_20", "beta_250"]

log = logging.getLogger("critic_report")


def main():
    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="%(asctime)s %(message)s")
    ids = sys.argv[1:]
    if not ids:
        df = bank.list_alphas(status="POOLED")
        ids = list(df["alpha_id"])
        if not ids:
            log.info("alpha_bank 里没有 POOLED 状态的候选 —— 没有值得否证的东西")
            return
    log.info("否证对象: %s", ids)

    mkt_since = (pd.Timestamp(EVAL_SINCE) - pd.Timedelta(days=WARMUP_DAYS)).strftime("%Y%m%d")
    m = fe.load_market(since=mkt_since)
    dates_all = pd.to_datetime(m["close"].index)
    md = fe.month_end_dates(dates_all)
    md = md[md >= pd.Timestamp(EVAL_SINCE)]
    zones = gate.zone_of(md)
    fwd = gate.forward_return(m, gate.HORIZON)
    log.info("面板 %d 天 × %d 只 | 月末截面 %d 个", len(dates_all), m["close"].shape[1], len(md))

    known = ex.load_factors_from_table()
    style = {k: known[k] for k in STYLE if k in known}
    log.info("存量因子 %d 个(风格代理 %d 个)", len(known), len(style))

    for aid in ids:
        wide = bank.load_values(aid)
        rep = critic.attack_all(aid, wide, fwd, md, zones, known=known, style=style)
        p = OUT_DIR / f"critic_{aid}.json"
        json.dump(rep, open(p, "w", encoding="utf-8"), ensure_ascii=False,
                  indent=1, default=float)
        _print_report(rep)
        _write_note(aid, rep, p)


def _print_report(rep: dict):
    aid = rep["alpha_id"]
    print("\n" + "=" * 78)
    print(f"Critic 报告: {aid}   (读数, 非结论)")
    print("=" * 78)

    d = rep.get("drop_best_year") or {}
    if d:
        print(f"① 单年依赖: 死区 head 全样本 {d['full']:+.4%}")
        for y, v in sorted(d["by_drop"].items()):
            print(f"     剔掉 {y}: head {v['head']:+.4%} (t={v['t']:+.2f}, 命中 {v['hit']:.0%}, n={v['n']})")
        print(f"   ⇒ 最依赖 {d['most_dependent_on']} 年; 剔掉后 head {d['head_wo_best_year']:+.4%}")

    t = rep.get("tail_dependence") or {}
    for k, v in t.items():
        print(f"② 极值依赖 [{k}]: head {v['head']:+.4%} (t={v['t']:+.2f}, n={v['n']})")

    c = rep.get("corr_known") or {}
    if c:
        mx = c.get("_max_abs", {})
        print(f"③ 换皮检验: 与存量因子的最大 |ρ| = {abs(mx.get('rho', float('nan'))):.3f} "
              f"({mx.get('factor')})")
        top = sorted(((k, v) for k, v in c.items() if not k.startswith("_")),
                     key=lambda kv: -abs(kv[1]))[:5]
        for k, v in top:
            print(f"     {k:<20s} ρ = {v:+.3f}")

    s = rep.get("corr_style") or {}
    if s:
        print("④ 风格暴露:")
        for k, v in sorted(((k, v) for k, v in s.items() if not k.startswith("_")),
                           key=lambda kv: -abs(kv[1])):
            print(f"     {k:<20s} ρ = {v:+.3f}")

    to = rep.get("turnover") or {}
    if to:
        print(f"⑤ 名单换手: top6 平均 {to['mean_top6_turnover']:.1%} (n={to['n']})")

    print(f"\n⚠️ {rep['_disclaimer']}")
    print("=" * 78)


def _write_note(aid: str, rep: dict, path: Path):
    d = (rep.get("drop_best_year") or {})
    c = (rep.get("corr_known") or {}).get("_max_abs", {})
    bits = []
    if d:
        bits.append(f"剔最依赖年({d.get('most_dependent_on')})后死区 head {d.get('head_wo_best_year'):+.2%}")
    if c:
        bits.append(f"与存量因子最大|ρ| {abs(c.get('rho', float('nan'))):.3f}({c.get('factor')})")
    to = rep.get("turnover") or {}
    if to:
        bits.append(f"top6换手 {to.get('mean_top6_turnover', float('nan')):.0%}")
    note = "Critic: " + "; ".join(bits) + f" | 全文 {path.name}(读数, 非结论)"
    bank.set_note(aid, note)
    log.info("note 已写回 alpha_bank[%s]", aid)


if __name__ == "__main__":
    main()
