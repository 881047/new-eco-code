"""袖套缩容 A/B（2026-09-28）——预注册 plans/sleeve-downsize-ab-prereg-20260928.md 的跑数器。

只读生产库（sqlite ro）与 F4 打分 npz；产物写 experiments/sleeve_downsize_ab_20260928.json
与逐臂序列 experiments/sleeve_downsize_ab_20260928_series_<seed>_<arm>.json。

- A0 = F52 生产构型逐参数复现（capital=1M / top100 / band 8 / hyst 0 / 无预算重分配）
  ⇒ 校准守卫 G0：与 F52_live_vs_research_shape_result.json 的 A0 读数逐项比对。
- B 臂 = 2 万档缩容网格（top100/20/15/10/6，greedy_budget + fill_through = 引擎既有
  的"买不起顺延/预算重分配"机制，F1b_weekly_open_overlap.py:176 文档字符串）。
- 判决机械生成（P1/P2 见预注册）；本脚本不改 config、不写库、不碰 pin。

用法：.venv\\Scripts\\python.exe -X utf8 scripts\\sleeve_downsize_ab_20260928.py [--seed A|AB]
"""
import argparse
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
BASE = HERE.parent
LB = BASE / "experiments" / "05_label"
sys.path.insert(0, str(BASE))


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod            # @dataclass 需要 cls.__module__ 可查
    spec.loader.exec_module(mod)
    return mod


f1 = _load("f1", LB / "F1_weekly_ic_decay.py")
f1b = _load("f1b", LB / "F1b_weekly_open_overlap.py")
f17 = _load("f17", LB / "F17_die_registry_kgd.py")
f22 = _load("f22", LB / "F22_pair_final_selection.py")

from app import config as cfg_mod              # noqa: E402
from app.factors import factor_engine as fe    # noqa: E402
from app.pit.engine import PITEngine           # noqa: E402

log = logging.getLogger("sleeve_downsize")
OUT = BASE / "experiments" / "sleeve_downsize_ab_20260928.json"
F52_RESULT = LB / "F52_live_vs_research_shape_result.json"
PER_YEAR = 52.0
FULL = ("2010-01-01", "2030-12-31")
ACTIVE = ("2024-01-01", "2026-08-31")
SEEDS = ("seedA", "seedB")
SEED_FILE = {"seedA": "F4_scores_seedA.npz", "seedB": "F4_scores_seedB.npz"}
BAND, HYST = 8.0, 0.0
G0_TOL_EXCESS, G0_TOL_MDD = 0.05, 0.005
P1_KEEP_RATIO, P1_MDD_SLACK = 0.60, 0.05
P2_TURNOVER_MULT = 1.5

ARMS = [
    # (arm, capital, top_n, greedy_budget, fill_through)
    ("A0_anchor_1m_top100", 1_000_000.0, 100, False, False),
    ("B1_2w_top100", 20_000.0, 100, True, True),
    ("B2_2w_top20", 20_000.0, 20, True, True),
    ("B3_2w_top15", 20_000.0, 15, True, True),
    ("B4_2w_top10", 20_000.0, 10, True, True),
    ("B5_2w_top6", 20_000.0, 6, True, True),
]


def zone_stats(d: pd.DataFrame, lo: str, hi: str) -> dict:
    s = d[(d["date"] >= lo) & (d["date"] <= hi)].dropna(subset=["ret", "bench_ret"])
    if len(s) < 2:
        return {"n": int(len(s))}
    a, b = f1b.ann(s["ret"], PER_YEAR), f1b.ann(s["bench_ret"], PER_YEAR)
    nav = s["nav"]
    return {"n": int(len(s)), "excess_pp": round(100 * (a - b), 2),
            "ret_ann": round(100 * a, 2), "bench_ann": round(100 * b, 2),
            "mdd": round(float((nav / nav.cummax() - 1).min()), 4),
            "turnover": round(float(s["turnover"].mean()), 3),
            "invested": round(float(s["invested_ratio"].mean()), 4),
            "names": round(float(s["n_positions"].mean()), 1),
            "sharpe": round(float(s["ret"].mean() / (s["ret"].std() + 1e-12) * (PER_YEAR ** 0.5)), 3)}


def pool_bench(d: pd.DataFrame, op_adj: np.ndarray, pos_of: dict, mask_np: np.ndarray,
               dates: pd.DatetimeIndex, min_names: int = 200) -> list:
    """与 F52 同口径：可交易池等权、锚点→执行日复权开盘。"""
    bench, prev_exec, prev_ap = [], None, None
    for _, r in d.iterrows():
        e = pos_of[r["date"]]
        if prev_exec is None:
            bench.append(np.nan)
        else:
            good = (mask_np[int(prev_ap)] & np.isfinite(op_adj[prev_exec])
                    & np.isfinite(op_adj[e]) & (op_adj[prev_exec] > 0))
            bench.append(float(np.mean(op_adj[e][good] / op_adj[prev_exec][good] - 1))
                         if good.sum() >= min_names else np.nan)
        prev_exec, prev_ap = e, int(r["anchor_pos"])
    return bench


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", default="AB", choices=["A", "AB"])
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        datefmt="%H:%M:%S", force=True)
    t0 = time.time()
    cfg = cfg_mod.load_config()
    m = fe.load_market(since="20140101")
    dates = pd.to_datetime(m["close"].index)
    wk = f1.week_end_dates(dates)
    cols = list(m["close"].columns)
    pit = PITEngine(m=m, cfg=cfg)
    mask = f1.tradable_frame(pit, wk, cols)
    conn = sqlite3.connect(f"file:{BASE / 'data' / 'neweco.db'}?mode=ro", uri=True)
    PITSC = f17.table_scores(conn, "model_score_die_pit", wk, cols)
    conn.close()
    log.info("面板就绪 | 锚点 %d | 票池 %d 只 | %.0fs", len(wk), len(cols), time.time() - t0)

    op_adj = (m["open"] * m["adj"]).reindex(index=dates, columns=cols).astype(np.float64).to_numpy()
    pos_of = {x: i for i, x in enumerate(dates)}
    mask_np = mask.fillna(False).to_numpy()

    seeds = SEEDS if a.seed == "AB" else SEEDS[:1]
    res = {"script_sha": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:16],
           "prereg": "plans/sleeve-downsize-ab-prereg-20260928.md",
           "question": "2 万档缩容（top6-20 + greedy/fill_through）后袖套净超额是否存续？",
           "band": BAND, "hyst": HYST, "windows": {"full": FULL, "active": ACTIVE},
           "arms": {n: {"capital": c, "top_n": t, "greedy": g, "fill": f}
                    for n, c, t, g, f in ARMS},
           "results": {}, "g0": {}, "verdict": {}, "secs": None}

    for seed in seeds:
        z = np.load(LB / SEED_FILE[seed], allow_pickle=True)
        W5 = pd.DataFrame(z["scores"],
                          index=pd.to_datetime([str(d) for d in z["anchors"]]),
                          columns=[str(c) for c in z["cols"]]).reindex(index=wk, columns=cols)
        Sdf = pd.DataFrame(f22.wr((W5, 2), (PITSC, 1)), index=wk, columns=cols)
        res["results"][seed] = {}
        for arm, cap, top_n, greedy, fill in ARMS:
            c = {**cfg, "capital": cap,
                 "regime_equity_weight": {"aggressive": {"attack": 1.0, "neutral": 1.0,
                                                         "defense": 1.0}}}
            d = f1b.run_overlap_portfolio(m, {}, wk, mask, 5, c, pit, 0, score_matrix=Sdf,
                                          top_n=top_n, rank_band=BAND, hyst=HYST,
                                          greedy_budget=greedy, fill_through=fill)
            if d is None:
                log.error("[%s|%s] 无输出（期数 <5）", seed, arm)
                res["results"][seed][arm] = None
                continue
            d = d.rename(columns={"n": "n_positions"})
            d["bench_ret"] = pool_bench(d, op_adj, pos_of, mask_np, dates)
            res["results"][seed][arm] = {"full": zone_stats(d, *FULL),
                                         "active": zone_stats(d, *ACTIVE)}
            ser = d[["date", "ret", "bench_ret", "turnover", "invested_ratio",
                     "n_positions", "nav"]]
            ser.to_json(BASE / "experiments" /
                        f"sleeve_downsize_ab_20260928_series_{seed}_{arm}.json",
                        orient="records", force_ascii=False, indent=1)
            f = res["results"][seed][arm]["full"]
            log.info("[%s|%s] full: 超额 %spp | MDD %s | 换手 %s | 投入 %s | 持仓 %s | 夏普 %s",
                     seed, arm, f.get("excess_pp"), f.get("mdd"), f.get("turnover"),
                     f.get("invested"), f.get("names"), f.get("sharpe"))

    # ---- G0 校准守卫 ----
    f52 = json.loads(F52_RESULT.read_text(encoding="utf-8"))
    g0_ok = True
    for seed in seeds:
        mine = (res["results"].get(seed) or {}).get("A0_anchor_1m_top100")
        ref = (f52["results"].get(seed) or {}).get("A0_生产构型(hyst=0.0)")
        if not mine or not ref:
            g0_ok = False
            res["g0"][seed] = {"ok": False, "reason": "missing arm or reference"}
            continue
        de = abs(mine["full"]["excess_pp"] - ref["full"]["excess_pp"])
        dm = abs(mine["full"]["mdd"] - ref["full"]["mdd"])
        ok = bool(de <= G0_TOL_EXCESS and dm <= G0_TOL_MDD)
        g0_ok = g0_ok and ok
        res["g0"][seed] = {"ok": ok, "delta_excess_pp": round(de, 4),
                           "delta_mdd": round(dm, 5),
                           "ref": [ref["full"]["excess_pp"], ref["full"]["mdd"]],
                           "mine": [mine["full"]["excess_pp"], mine["full"]["mdd"]]}
    res["g0"]["ok"] = bool(g0_ok)

    # ---- P1 / P2 机械判决（判读规则见预注册）----
    verdict = {"g0_ok": bool(g0_ok)}
    p1_pass, p2_pass = [], []
    if g0_ok:
        for arm in ["B2_2w_top20", "B3_2w_top15", "B4_2w_top10"]:
            per_seed = {}
            ok_both = True
            for seed in seeds:
                a0 = res["results"][seed]["A0_anchor_1m_top100"]["full"]
                b = res["results"][seed].get(arm)
                if not b:
                    ok_both = False
                    per_seed[seed] = "missing"
                    continue
                need = P1_KEEP_RATIO * a0["excess_pp"]
                e_ok = b["full"]["excess_pp"] >= need
                m_ok = b["full"]["mdd"] >= a0["mdd"] - P1_MDD_SLACK
                t_ok = b["full"]["turnover"] <= P2_TURNOVER_MULT * a0["turnover"]
                per_seed[seed] = {"excess_pp": b["full"]["excess_pp"], "need_ge": round(need, 2),
                                  "p1_excess": bool(e_ok), "p1_mdd": bool(m_ok),
                                  "p2_turnover": bool(t_ok)}
                ok_both = ok_both and e_ok and m_ok
                if e_ok and m_ok:
                    p1_pass.append(f"{seed}:{arm}")
                if t_ok:
                    p2_pass.append(f"{seed}:{arm}")
            verdict[arm] = per_seed
            verdict.setdefault("p1_arms_pass", []).append({arm: ok_both})
    verdict["P1_pass_arms"] = sorted(set(x.split(":")[1] for x in p1_pass))
    verdict["P2_pass_arms"] = sorted(set(x.split(":")[1] for x in p2_pass))
    verdict["conclusion"] = (
        "G0 失败 ⇒ 全轮证据无效，先修环境"
        if not g0_ok else
        ("P1∧P2 过（档位 " + ",".join(sorted(set(verdict["P1_pass_arms"])
                                             & set(verdict["P2_pass_arms"]))) +
         "）⇒ 2 万双车经济上成立，登记纸面轨道候选（实盘=用户账户决策，另立预注册）"
         if verdict["P1_pass_arms"] and verdict["P2_pass_arms"] else
         "P1/P2 不过 ⇒ 缩容线结清：分散度损失主导，2 万档袖套不可跑"))
    res["verdict"] = verdict
    res["secs"] = round(time.time() - t0, 1)
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({"g0": res["g0"], "verdict": verdict}, ensure_ascii=False, indent=1))
    print(f"写入 {OUT.name}（{res['secs']}s）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
