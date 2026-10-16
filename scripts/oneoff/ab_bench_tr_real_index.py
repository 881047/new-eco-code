"""A/B：基准腿 价格指数 000906.SH vs **真实全收益指数 H00906.CSI**（2026-09-19）

判据跑前冻结在 `plans/b1-real-tr-index-20260919.md`。上游：三补二十七用**合成**全收益尺估出抬高
2.37pp/年，而 09 号"一之补"探明中证800 的全收益指数 `H00906.CSI` 直接可取 ⇒ 本轮用**真值**重做
一遍，合成尺最大的两个限制（一阶估计、2017~2018 无权重需填充）就此消失。

**本轮不改基准腿、不改 pin、不改任何生产口径** —— 只把"换尺之后是什么样"测出来（含三方同步清单）。

## 两臂 × 两窗

| 臂 | 基准腿 | | 窗 | `run_backtest(start=…)` |
|---|---|---|---|---|
| A | `000906.SH` close（价格指数，现行尺） | | prod | `cfg.eval_start`（85 期，现行 pin） |
| C | `H00906.CSI` close（全收益，税前股息再投资） | | full | `None`（115 期） |

唯一变量 = 基准腿取哪条指数；同一 panel、同一进程、同一套参数。

## 三道门（冻结）

- **C1 数据对等性**：H 与 PR 的交易日集合完全相同（双向差集空）；比值 `H/PR` **无实质下降**
  （降幅 >0.001% 的天数 = 0；指数发布值本身有 1e-7 量级舍入抖动，见预注册 §7）；
  端点法年化对得上预注册冻结的 09 号实读值；两条腿各自 `close` 逐日收益与库内 `pct_chg`
  自洽 ≤0.01%；逐年 `(TR−PR)` 收益和落在 [0.5, 3.5]pp 内（"确实只多了股息"的直接检验）。
- **C2 量级门**：Δexcess_ann ∈ [1.8, 2.6] pp/年，**两窗都要**落带内（中枢是 09 号实测 2.22pp）。
- **C3 不变性门**：除 `bench_ann/excess_ann/win_rate` 外两臂全部指标逐位相等 ⇒ 换尺不动决策、不动回撤轴。

跑之前不取任何读数；C2/C1 不过 ⇒ 查实现，**不许改带、不许改门**。

复用：`ab_bench_total_return.py` 的 `run_arm/diff`（按文件路径加载，与 `tests/test_expert.py::_frozen_gate`
同一手法）—— 那两份是已登记的回测对照实现，复制一份只会多一处会漂移的口径。

运行：`D:\\Python313\\python.exe -X utf8 scripts\\oneoff\\ab_bench_tr_real_index.py`
产物：`logs/ab_bench_tr_real_index.json`；回测行 tag `20260919_b1real_*`（不碰生产 run_date）。
"""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE))

OUT = BASE / "logs" / "ab_bench_tr_real_index.json"
PREV = BASE / "scripts" / "oneoff" / "ab_bench_total_return.py"
PR_CODE = "000906.SH"
TR_CODE = "H00906.CSI"
C2_BAND_PP = (1.8, 2.6)                      # Δexcess 的冻结带（中枢 2.22pp，±0.4）
C1_ENDPOINT = {                              # 09 号"一之补"实读值（写死，用来核对没接错列）
    "prod": {"pr": 3.76, "tr": 5.98},
    "full": {"pr": 3.08, "tr": 5.30},
}
WINDOWS = {"prod": "eval_start", "full": None}


def _load_prev():
    spec = importlib.util.spec_from_file_location("_ab_prev", PREV)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _close_series(code: str) -> pd.DataFrame:
    """取 `close` + `pct_chg`（后者只用于 C1-§4/§5 的自洽与逐年差检查，不进任何一臂）。"""
    con = sqlite3.connect(f"file:{BASE / 'data' / 'neweco.db'}?mode=ro", uri=True)
    con.execute("PRAGMA query_only=1")
    try:
        d = pd.read_sql("SELECT trade_date, close, pct_chg FROM index_daily WHERE ts_code=? "
                        "ORDER BY trade_date", con, params=(code,))
    finally:
        con.close()
    d["trade_date"] = d["trade_date"].astype(str)
    return d.set_index("trade_date")


def c1_checks(pr: pd.Series, tr: pd.Series) -> dict:
    """交易日集合相同 + 比值无实质下降 + 端点年化 == 预注册冻结值 + 两道正检查（§C1-补）。

    ⚠️ 容差 1e-5(0.001%) 是与预注册 §3/§7 对齐后的值：第一版实现写了 1e-9，比指数发布值的
    舍入精度还细 ⇒ 被 744 处 1e-7 抖动误判 FAIL（数据本身没错）。真正能抓"错位/错列"的是
    下面 §4/§5 两道正检查，不是这条单调性。
    """
    a, b = set(pr.index), set(tr.index)
    ratio = (tr["close"].reindex(pr.index) / pr["close"])
    step = ratio.diff().dropna()
    res = {
        "rows_pr": int(len(pr)), "rows_tr": int(len(tr)),
        "n_only_in_pr": len(a - b), "n_only_in_tr": len(b - a),
        "only_in_pr": sorted(a - b)[:5], "only_in_tr": sorted(b - a)[:5],
        "material_drops": int((step < -1e-5).sum()),
        "max_drop_pct": round(float(step.min()) * 100, 8) if len(step) else None,
        "coverage_tr_from": str(tr.index.min()), "coverage_tr_to": str(tr.index.max()),
        "endpoint_ann_pct": {},
    }
    # §4 每条腿自己的 close 逐日收益必须与库里的 pct_chg 自洽（错位/错列必炸）
    for lab, ser in (("pr", pr), ("tr", tr)):
        calc = (ser["close"] / ser["close"].shift(1) - 1.0) * 100.0
        res[f"selfconsistency_{lab}_max_mismatch_pct"] = round(
            float((calc - ser["pct_chg"]).abs().max()), 6)
    # §5 逐年 (TR − PR) 收益和 = 那一年"多出来的股息收益"，必须像个股息率
    gap = (tr.reindex(pr.index)["pct_chg"] - pr["pct_chg"]).dropna()
    yearly = gap.groupby([i[:4] for i in gap.index]).sum() / 100.0     # 百分点→小数
    res["yearly_tr_minus_pr_pct"] = {k: round(float(v) * 100, 2) for k, v in yearly.items()}
    for w, (s, e) in {"prod": ("20190731", "20260831"),
                      "full": ("20170126", "20260831")}.items():
        for lab, ser in (("pr", pr), ("tr", tr)):
            x = ser.loc[(ser.index >= s) & (ser.index <= e), "close"]
            yrs = (pd.Timestamp(e) - pd.Timestamp(s)).days / 365.25
            res["endpoint_ann_pct"][f"{w}_{lab}"] = round(
                float((x.iloc[-1] / x.iloc[0]) ** (1 / yrs) - 1) * 100, 2)
    # 端点值必须与预注册 §3-C1-③ 冻结的 09 号实读一致（±0.05pp 只容浮点与末日差）
    mism = {k: [obs, C1_ENDPOINT[k.split("_")[0]][k.split("_")[1]]]
            for k, obs in res["endpoint_ann_pct"].items()
            if abs(obs - C1_ENDPOINT[k.split("_")[0]][k.split("_")[1]]) > 0.05}
    res["endpoint_mismatch"] = mism
    bad_year = {k: v for k, v in res["yearly_tr_minus_pr_pct"].items()
                if not (0.5 <= v <= 3.5)}
    res["yearly_gap_out_of_band"] = bad_year
    res["pass"] = bool(res["n_only_in_pr"] == 0 and res["n_only_in_tr"] == 0
                       and res["material_drops"] == 0 and not mism and not bad_year
                       and res["selfconsistency_pr_max_mismatch_pct"] <= 0.01
                       and res["selfconsistency_tr_max_mismatch_pct"] <= 0.01)
    return res
    return res


def main() -> int:
    from app import config as cfg_mod
    from app.backtest import engine
    from app.factors import factor_engine as fe

    prev = _load_prev()
    eval_start = cfg_mod.load_config().get("eval_start") or None
    pr_db, tr_db = _close_series(PR_CODE), _close_series(TR_CODE)
    c1 = c1_checks(pr_db, tr_db)
    print(f"C1 数据对等性: pass={c1['pass']} | PR {c1['rows_pr']} 行 / TR {c1['rows_tr']} 行 "
          f"({c1['coverage_tr_from']}~{c1['coverage_tr_to']}) | 日期差集 {c1['n_only_in_pr']}/"
          f"{c1['n_only_in_tr']} | 实质降幅 {c1['material_drops']} 处"
          f"(最大 {c1['max_drop_pct']}%) | 自洽不符 PR {c1['selfconsistency_pr_max_mismatch_pct']}%"
          f"/TR {c1['selfconsistency_tr_max_mismatch_pct']}%")
    print(f"   端点年化: " + "  ".join(f"{k}={v}" for k, v in c1["endpoint_ann_pct"].items()))
    print(f"   逐年(TR−PR)收益和(百分点/年): " + " ".join(f"{y}:{v}" for y, v in
                                              c1["yearly_tr_minus_pr_pct"].items()))
    if not c1["pass"]:
        print("⚠️ C1 未过 ⇒ 数据接入有问题（日期集合/列/代码/自洽），先查接入，不跑臂")
        print("   端点不符:", c1["endpoint_mismatch"], " 逐年出带:", c1["yearly_gap_out_of_band"])
        return 2

    # 面板只加载一次；两臂都往同一 DatetimeIndex 上对齐，避免任何"两臂数据版本不同"的混杂
    bench_idx = fe.load_market()["bench"].index
    bench = {}
    for code, ser in ((PR_CODE, pr_db), (TR_CODE, tr_db)):
        d = pd.to_datetime(ser.index)
        bench[code] = pd.Series(ser["close"].to_numpy(dtype=np.float32),
                                index=d).reindex(bench_idx)

    import functools
    real_load = engine.load_market

    def panel_factory(code: str):
        m = dict(real_load())
        m["bench"] = bench[code]
        return m

    arms = {}
    for wname, start in (("prod", eval_start), ("full", None)):
        for aname, code in (("A", PR_CODE), ("C", TR_CODE)):
            tag = f"b1real_{wname}_{aname.lower()}"
            pf = functools.partial(panel_factory, code)
            print(f"[{wname}/{aname}] run_backtest(start={start}, run_tag={tag}, "
                  f"bench={'价格指数' if code == PR_CODE else '全收益 H00906'}) …")
            arms[f"{wname}_{aname}"] = prev.run_arm(engine, pf, tag, start)
            r = arms[f"{wname}_{aname}"]
            print(f"    → 年化 {r['ann_return']*100:.2f}% / 基准 {r['bench_ann']*100:.2f}% / "
                  f"超额 {r['excess_ann']*100:.2f}% / 回撤 {r['max_drawdown']*100:.2f}% / "
                  f"胜率 {r['win_rate']:.4f} / IC {r.get('ic_mean')}")

    diffs, gates_c2, deltas = {}, {}, {}
    c3_ok = True
    for w in ("prod", "full"):
        d = prev.diff(arms[f"{w}_A"], arms[f"{w}_C"])
        diffs[w] = d
        c3_ok = c3_ok and not d["invariance_violations"]
        de = (arms[f"{w}_A"]["excess_ann"] - arms[f"{w}_C"]["excess_ann"]) * 100.0
        deltas[w] = round(de, 3)
        gates_c2[w] = C2_BAND_PP[0] <= de <= C2_BAND_PP[1]

    res = {
        "prereg": "plans/b1-real-tr-index-20260919.md",
        "bench_legs": {"A": PR_CODE + " 价格指数", "C": TR_CODE + " 全收益(税前)"},
        "C1_data_parity": c1,
        "gates": {
            "C1_pass": c1["pass"],
            "C2_magnitude_band_pp": list(C2_BAND_PP),
            "C2_delta_excess_pp_per_year": deltas,
            "C2_pass": gates_c2,
            "C3_invariance_pass": c3_ok,
            "C3_violations": {w: diffs[w]["invariance_violations"] for w in diffs},
        },
        "expected_to_change": {w: diffs[w]["expected_to_change"] for w in diffs},
        "arms": arms,
        "not_a_pin_change": "本轮只测数；改基准腿 = 换 pin，待用户拍板",
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    print("\n== 门 ==")
    print(f"  C1 数据对等性: {'PASS' if c1['pass'] else 'FAIL'}")
    print(f"  C2 量级带 {C2_BAND_PP}pp/年: " + "  ".join(
        f"{w}={deltas[w]:+.2f}pp {'PASS' if gates_c2[w] else 'FAIL'}" for w in deltas))
    print(f"  C3 不变性（除基准三字段外两臂逐位相等 ⇒ 决策与回撤不受影响）: "
          f"{'PASS' if c3_ok else 'FAIL'}")
    for w in diffs:
        if not c3_ok and diffs[w]["invariance_violations"]:
            print("    违反项:", diffs[w]["invariance_violations"][:6])
        print(f"  [{w}] 会变的三字段: " + "; ".join(
            f"{k}={v['a']}→{v['b']}" for k, v in diffs[w]["expected_to_change"].items()))
    print(f"\n产物: {OUT.relative_to(BASE)}   （回测行 tag: 20260919_b1real_*）")
    all_c2 = all(gates_c2.values())
    if not c3_ok:
        print("⚠️ C3 未过 ⇒ 基准进了决策，等级升高，另立一轮（预注册 §4）")
        return 2
    if not all_c2:
        print("⚠️ C2 未过 ⇒ 查实现/接入，**不许改带**（预注册 §3）")
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
