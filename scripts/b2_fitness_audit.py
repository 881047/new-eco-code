# -*- coding: utf-8 -*-
"""B2 位级审计器：正交增量适应度改造前后的「生产逐位不变」证明。

判据来源（用户 2026-10-03 拍板 B2=立项(先位级审计)；《T3预注册草案》§H0）：
GPProposer._fitness 现行口径（死区 head 超额 = 门禁同一个量）是挖矿历史可复现性的锚。
改造（AlphaGen 式「对 model_score 残差 + 对 alpha_bank 现有池增量 IC」）必须作为
缺省关闭的可选路径落地；本审计器在冻结候选集上求值，改造前后逐位比对。

用法：
  --freeze   装配 Factory（gp1 同款装配，save=False 不入册不计分母）→ 固定候选集
             逐个 _fitness → experiments/b2_fitness_baseline_20261003.json
  --verify   重装配 → 候选集读自基线 → 重算 → 与基线逐位比对（repr 级）→ 全等 exit 0
注意：--verify 必须在改造 commit 之前先跑一次并确认对未改造代码 PASS（自洽性检验），
改造后再跑一次仍 PASS 才构成证明。
"""
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from app import db
from app.alpha import bank, gate
from app.alpha.factory import Factory, GPBudget, GPProposer, PreRegistration
from app.logger import setup_logger

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "experiments" / "b2_fitness_baseline_20261003.json"
N_EXPR = 25


def git_sha():
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    except Exception:
        return "unknown"


def build_proposer():
    """gp1 同款装配（alpha_factory_gp1.py），save 永不调用。"""
    f = Factory(PreRegistration(
        intent="B2 位级审计：冻结候选集适应度求值（不入册不计分母）"))
    m, dates_all, md_all = f.market()
    dead_dates = md_all[md_all >= pd.Timestamp(gate.DEAD[0])]
    gp = GPProposer(m, dead_dates, budget=GPBudget(population=1, generations=1))
    f.add(gp)
    f.declare()
    return gp, m, dead_dates


def pick_expressions():
    """确定性候选集：bank 全部表达式排序取前 N（冻结进基线，verify 原样复用）。"""
    df = bank.list_alphas()
    exprs = sorted({str(e).strip() for e in df["expr"].dropna() if str(e).strip()})
    if len(exprs) < 5:
        raise RuntimeError(f"bank expressions too few: {len(exprs)}")
    return exprs[:N_EXPR]


def evaluate(gp, exprs):
    """逐表达式求值；异常也是行为，按位冻结（repr 级浮点）。"""
    out = {}
    for e in exprs:
        try:
            head, cplx = gp._fitness(e)
            out[e] = [repr(float(head)), repr(float(cplx))]
        except Exception as ex:
            out[e] = ["EXC:" + type(ex).__name__, str(ex)[:80]]
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--freeze", action="store_true")
    ap.add_argument("--verify", action="store_true")
    args = ap.parse_args()
    if args.freeze == args.verify:
        ap.error("exactly one of --freeze / --verify")
    setup_logger()
    db.init_db()

    t0 = time.time()
    gp, m, dead_dates = build_proposer()
    if args.freeze:
        exprs = pick_expressions()
        results = evaluate(gp, exprs)
        meta = {
            "frozen_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "git_sha": git_sha(),
            "dead_window": [str(gate.DEAD[0]), str(gate.DEAD[-1])],
            "dead_dates_n": int(len(dead_dates)),
            "n_expr": len(exprs),
            "fitness_version": "head-excess (pre-B2, frozen reference)",
        }
        BASELINE.write_text(
            json.dumps({"meta": meta, "expressions": exprs, "results": results},
                       ensure_ascii=False, indent=1), encoding="utf-8")
        n_exc = sum(1 for v in results.values() if v[0].startswith("EXC:"))
        print(f"[freeze] {len(exprs)} exprs → {BASELINE.name} "
              f"({n_exc} exc, {time.time()-t0:.0f}s, sha {meta['git_sha'][:8]})")
        return 0

    # --verify
    base = json.loads(BASELINE.read_text(encoding="utf-8"))
    results = evaluate(gp, base["expressions"])
    diffs = []
    for e in base["expressions"]:
        if results.get(e) != base["results"].get(e):
            diffs.append({"expr": e, "baseline": base["results"].get(e),
                          "current": results.get(e)})
    sha = git_sha()
    if diffs:
        print(f"[verify] FAIL: {len(diffs)}/{len(base['expressions'])} exprs 位级不一致 "
              f"(sha {sha[:8]})")
        for d in diffs[:5]:
            print(f"  - {d['expr'][:60]}\n    base={d['baseline']}\n    curr={d['current']}")
        return 1
    print(f"[verify] PASS: {len(base['expressions'])}/{len(base['expressions'])} exprs "
          f"逐位一致 (base sha {base['meta']['git_sha'][:8]} → current sha {sha[:8]}, "
          f"{time.time()-t0:.0f}s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
