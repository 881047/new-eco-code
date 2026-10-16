"""F66 尾部度量：ES / CDaR vs MDD（+ 顺手落盘逐期持仓数与投入率）（2026-09-13 夜）

**来自理论层 26 号 §可检验假设 3**（原话："现役组合的 ES(5%)、CDaR 与 MDD 的关系；
是否在某些年份有隐藏的尾部集中？"）—— 这是"两轴（超额 + MDD）"之外的**第三只眼睛**：
MDD 只看**最深那一次**，ES 看**最差的 5% 期**，CDaR 看**最差的 5% 回撤状态**；
三者若指向不同故事（例：MDD 尚可但 ES 很胖），说明"深坑靠一次运气"而非"尾部整体可控"。

**输入**：F52 的四条**逐期序列**（`F52_series_{seedA,seedB}_{A0,A1}.json`；A0 = **生产构型**
`hyst=0.0`、A1 = 研究构型 `hyst=0.2`）。**只读**，不改任何生产产物。

**同时闭掉一个存量缺口**（KB 14 §三 #9）：那批序列里其实**已经带了** `invested_ratio` 与
`n_positions` 逐期值，只是从没人把它们落成独立产物 ⇒ 本脚本顺手导出 `F66_series_*.json`
（逐期 date / ret / bench_ret / turnover / invested_ratio / n_positions），供健康度与归因直接读。

**口径**：全部用**生产构型（A0）两套种子**；期 = 周锚点（492 期，2017-02~2026-09）。
月频聚合按自然月复利，ES 取最差 5% 的月度收益均值。

运行：``D:\\Python313\\python.exe -X utf8 experiments\\05_label\\F66_tail_metrics.py``
"""
import json
import statistics
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "F66_tail_metrics_result.json"
SERIES = {"seedA": HERE / "F52_series_seedA_A0.json", "seedB": HERE / "F52_series_seedB_A0.json"}
ES_Q = 0.05


def _load(path: Path) -> list:
    rows = json.loads(path.read_text(encoding="utf-8"))
    out = []
    for r in rows:
        if r.get("ret") is None or r.get("bench_ret") is None:
            continue
        out.append({"date": r["date"], "ret": float(r["ret"]), "bench": float(r["bench_ret"]),
                    "turnover": r.get("turnover"), "invested": r.get("invested_ratio"),
                    "n_pos": r.get("n_positions")})
    return out


def _drawdowns(rets: list) -> list:
    nav, peak, dd = 1.0, 1.0, []
    for r in rets:
        nav *= (1 + r)
        peak = max(peak, nav)
        dd.append(nav / peak - 1.0)
    return dd


def _pct(xs: list, q: float) -> float:
    if not xs:
        return float("nan")
    s = sorted(xs)
    i = min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))
    return s[i]


def _stats(rows: list) -> dict:
    rets = [r["ret"] for r in rows]
    bench = [r["bench"] for r in rows]
    dd = _drawdowns(rets)
    k = max(1, int(round(ES_Q * len(rets))))
    worst = sorted(rets)[:k]
    worst_dd = sorted(dd)[:k]
    excess = [r["ret"] - r["bench"] for r in rows]
    inv = [r["invested"] for r in rows if r["invested"] is not None]
    pos = [r["n_pos"] for r in rows if r["n_pos"] is not None]
    # 回撤集中度：最深回撤里，"最差 1/3 期"贡献了多少
    worst_dates = sorted(range(len(rets)), key=lambda i: rets[i])[:k]
    return {
        "n": len(rets),
        "mean_pp": round(100 * statistics.fmean(rets), 3),
        "excess_mean_pp": round(100 * statistics.fmean(excess), 3),
        "sd_pp": round(100 * statistics.pstdev(rets), 3),
        "mdd": round(min(dd), 4),
        "es5_pp": round(100 * statistics.fmean(worst), 3),
        "cdar5": round(statistics.fmean(worst_dd), 4),
        "worst_periods": [
            {"date": rows[i]["date"], "ret_pp": round(100 * rets[i], 2)} for i in worst_dates[:5]],
        "invested_min": round(min(inv), 4) if inv else None,
        "invested_p10": round(_pct(inv, 0.10), 4) if inv else None,
        "invested_median": round(statistics.median(inv), 4) if inv else None,
        "n_pos_min": min(pos) if pos else None,
        "n_pos_median": statistics.median(pos) if pos else None,
    }


def _monthly(rows: list) -> dict:
    """按自然月复利聚合（用户的目标函数是月收益）"""
    import datetime
    buckets, order = {}, []
    for r in rows:
        d = datetime.datetime.fromtimestamp(r["date"] / 1000).strftime("%Y-%m")
        if d not in buckets:
            buckets[d] = 1.0
            order.append(d)
        buckets[d] *= (1 + r["ret"])
    ms = [buckets[d] - 1 for d in order]
    k = max(1, int(round(ES_Q * len(ms))))
    return {"months": len(ms),
            "mean_pp": round(100 * statistics.fmean(ms), 3),
            "hit": round(sum(1 for x in ms if x > 0) / len(ms), 3),
            "es5_pp": round(100 * statistics.fmean(sorted(ms)[:k]), 3),
            "worst_month_pp": round(100 * min(ms), 2),
            "best_month_pp": round(100 * max(ms), 2)}


def main() -> int:
    res = {"protocol": {"input": [p.name for p in SERIES.values()], "es_q": ES_Q,
                        "shape": "生产构型 A0 (hyst=0.0)", "note": "只读 F52 序列；不改任何生产产物"},
           "seeds": {}}
    for seed, path in SERIES.items():
        rows = _load(path)
        res["seeds"][seed] = {"stats": _stats(rows), "monthly": _monthly(rows)}
        # 顺手落盘逐期序列（含 invested_ratio / n_positions）⇒ 闭掉 KB14 #9 的存量缺口
        (HERE / f"F66_series_{seed}.json").write_text(
            json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n生产构型（A0）× 两套种子 | 尾部度量（期=周；单位 pp/期）")
    print(f"{'种子':<7}{'期数':>5}{'超额均值':>9}{'MDD':>9}{'ES5%':>8}{'CDaR5%':>8}"
          f"{'投入率中位':>10}{'最低':>7}{'持仓中位':>9}")
    for seed, e in res["seeds"].items():
        s = e["stats"]
        print(f"{seed:<7}{s['n']:>5}{s['excess_mean_pp']:>9.3f}{s['mdd']:>9.4f}"
              f"{s['es5_pp']:>8.3f}{s['cdar5']:>8.4f}{s['invested_median']:>10.4f}"
              f"{s['invested_min']:>7.4f}{str(s['n_pos_median']):>9}")
    print(f"\n月度（用户目标函数口径）")
    for seed, e in res["seeds"].items():
        m = e["monthly"]
        print(f"{seed:<7}{m['months']:>5} 个月 | 月均 {m['mean_pp']:+.3f}pp | 命中 {m['hit']:.1%}"
              f" | 月度 ES5% {m['es5_pp']:+.2f}pp | 最差月 {m['worst_month_pp']:+.2f}pp")
    print(f"\n结果 → {OUT.name}（另落盘 F66_series_*.json 含逐期投入率/持仓数）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
