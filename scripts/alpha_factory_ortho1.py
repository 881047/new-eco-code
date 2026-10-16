# -*- coding: utf-8 -*-
"""GP 正交增量挖矿首跑 —— `plans/gp-ortho-mining-prereg-20261003.md` 的执行体。

机制: 工厂流程照走(PreRegistration → add(GPProposer fitness_mode="ortho") →
declare() → run(**save=False** 不写库)), **登记全部由本运行器按预注册 §三判决链执行**:
H0 判据"须在正交增量口径过五判据方可 POOLED" ⇒ Factory._evaluate 的 head 路径判决
只作对照读数, bank 登记依据 = 残差面板上的六判据。

用法:
    --smoke   接线验证: GPBudget(8,3)、零登记零分母(读数不入任何判决)
    (默认)    真实轮: GPBudget()=60×15=900 分母, 前沿成员按 ortho 链登记入 alpha_bank
产物: experiments/alpha_factory_ortho1_20261003.json (smoke → *_smoke_*.json)

判读要点(预注册 §四.4): gen 行"最好 -head"恒为 -1e9 = 接线缺陷(全 worst 指纹),
不是数据结论 —— 修接线重跑 smoke 后, 真实轮读数只认修复后那次。
注意: 本脚本非幂等 —— 真实轮登记中途崩溃后重跑会使 bank.cumulative_tests() 抬高
⇒ N_used 漂移; 登记集中在全部判决完成后顺序执行以收窄窗口, 崩溃残留人工处置。
"""
import os

for _v in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS"):
    os.environ.setdefault(_v, "1")   # E2E 轮教训: numpy/OpenBLAS 多线程偶发死锁, import 前设

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import pandas as pd

from app import db
from app.alpha import bank, critic, gate
from app.alpha import expr as E
from app.alpha.factory import (Factory, GPBudget, GPProposer, PreRegistration,
                               _masked_panel, _ortho_residual)
from app.factors import factor_engine as fe
from app.logger import setup_logger

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "experiments" / "alpha_factory_ortho1_20261003.json"
OUT_SMOKE = ROOT / "experiments" / "alpha_factory_ortho1_smoke_20261003.json"
PREREG = "plans/gp-ortho-mining-prereg-20261003.md"


class OrthoGPProposer(GPProposer):
    """前沿交付时改用本轮专属 id 前缀。

    gp1 先例已把 `gp_000..gp_007` 登记在册(gp_000 已 POOLED) ⇒ 缺省 `gp_%03d` 编号
    会把本轮全部成员按 id_conflict 撞车拒跑(--smoke 2026-10-03 实证 4/4 拒跑)。
    只改名字, 不改搜索/适应度/交付语义; 前沿顺序 = 适应度排序, 重编号与原 rank 一致。
    """

    prefix = "gpo1"

    def propose(self):
        cands = super().propose()
        for i, c in enumerate(cands):
            c.name = "%s_%03d" % (self.prefix, i)
            c.meta = {**c.meta, "name": c.name}
        return cands


def assemble_controls(m, dates_all, conn):
    """控制集 C(预注册 §二冻结): ①model_score 日频前向结转 ②bank POOLED 成员面板。

    PIT 规则(冻结): 月 M 的分从 **M+1 的第二个交易日**起生效 —— 月度链在 M+1 首个
    日历日 18:33 后完成, 该规则对"链最晚跑到 M+1 首个交易日晚上"也安全; 门禁只读
    月末锚, 一日陈旧无损。ffill 至下一月分接管; 首个生效日之前为 NaN(该控制当期剔除)。
    """
    dates_idx = pd.DatetimeIndex(dates_all)
    controls, prov = {}, {}

    ms = pd.read_sql("SELECT trade_date, ts_code, score FROM model_score", conn)
    wide = ms.pivot(index="trade_date", columns="ts_code", values="score")
    wide.index = pd.to_datetime(wide.index, format="%Y%m%d")
    eff_index, eff_rows, n_drop, n_clamp = [], [], 0, 0
    for stamp in wide.index:
        nxt = stamp + pd.offsets.MonthBegin(1)                       # M+1 首个日历日
        i1 = dates_idx.searchsorted(nxt, side="left")                # M+1 首个交易日
        i2 = (dates_idx.searchsorted(dates_idx[i1], side="right")
              if i1 < len(dates_idx) else len(dates_idx))            # M+1 第二个交易日
        if i2 >= len(dates_idx):
            n_drop += 1                                               # 窗外, 永不生效
            continue
        eff = dates_idx[i2]
        if i1 == 0 and nxt < dates_idx[0]:
            eff = dates_idx[0]          # 生效期早于数据窗 ⇒ 在窗起点已在效力(钳到首日)
            n_clamp += 1
        eff_index.append(eff)
        eff_rows.append(wide.loc[stamp])
    ms_eff = (pd.DataFrame(eff_rows, index=pd.DatetimeIndex(eff_index),
                           columns=wide.columns).sort_index())
    # 同一 eff 日只保留最新印章(印章升序 ⇒ keep last = 窗起点在效力的那一份)
    ms_eff = ms_eff[~ms_eff.index.duplicated(keep="last")]
    ms_daily = ms_eff.reindex(dates_idx).ffill()
    controls["model_score"] = ms_daily
    prov["model_score"] = {
        "stamps": int(len(wide)), "range": [str(wide.index[0].date()),
                                            str(wide.index[-1].date())],
        "n_stocks": int(wide.shape[1]), "n_dropped_out_of_window": int(n_drop),
        "n_clamped_to_window_start": int(n_clamp),
        "pit_rule": "month M score effective from 2nd trading day of M+1, ffill; "
                    "pre-window stamps clamped to window start (latest stamp kept)",
    }

    panel = E.build_panel(m)
    eng = E.Engine(panel)
    pooled = bank.list_alphas(status="POOLED")
    pooled_used, pooled_skipped = [], []
    for r in pooled.itertuples():
        expr = getattr(r, "expr", None)
        if not isinstance(expr, str) or not expr.strip():
            pooled_skipped.append({"alpha_id": str(r.alpha_id), "reason": "no expr"})
            continue
        p = eng.eval(expr.strip())
        if isinstance(p, pd.Series):
            p = p.to_frame()
        controls[str(r.alpha_id)] = p.replace([np.inf, -np.inf], np.nan)
        pooled_used.append({"alpha_id": str(r.alpha_id), "expr": expr.strip()})
    prov["pooled_members"] = {"used": pooled_used, "skipped": pooled_skipped,
                              "as_of": time.strftime("%Y-%m-%d %H:%M:%S")}
    return controls, prov, eng


def control_coverage(controls, dead_md):
    """死区各月末锚的控制覆盖(min/median 名字数)——接线体检, 只读日志不入判决。"""
    stats = {nm: [] for nm in controls}
    n_zero_dates = 0
    for d in dead_md:
        n_alive = 0
        for nm, c in controls.items():
            k = 0
            if d in c.index:
                k = int(c.loc[d].notna().sum())
            stats[nm].append(k)
            if k >= gate.MIN_NAMES:
                n_alive += 1
        if n_alive == 0:
            n_zero_dates += 1
    out = {nm: {"min": int(min(v)), "median": float(np.median(v))}
           for nm, v in stats.items()}
    return out, n_zero_dates


def judge_ortho(expr, eng, ind, controls, tradable, md, zones, fwd):
    """预注册 §三判决链: raw → neutralize → **残差化** → 掩码 → gate.evaluate。
    返回 (panel_md, det); 无有效截面返回 (panel_md, None)。"""
    raw = eng.eval(expr)
    if isinstance(raw, pd.Series):
        raw = raw.to_frame()
    raw = raw.replace([np.inf, -np.inf], np.nan)
    neu = fe.neutralize(raw, ind)
    res = _ortho_residual(neu, controls, gate.MIN_NAMES)
    panel_md = _masked_panel(res, tradable, md)
    det = gate.evaluate(panel_md, fwd, md, zones)
    return panel_md, (None if det is None or det.empty else det)


def rd(det, zone, key):
    sub = det[det["zone"] == zone]
    return None if not len(sub) else float(sub[key].mean())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--smoke", action="store_true",
                    help="接线验证: 小预算 + 零登记零分母")
    args = ap.parse_args()
    setup_logger()
    db.init_db()

    if args.smoke:
        budget = GPBudget(population=8, generations=3)
        intent = ("wiring smoke — 零登记零分母 "
                  "(gp-ortho-mining-prereg-20261003 冻结前接线验证)")
        out, register = OUT_SMOKE, False
    else:
        budget = GPBudget()          # 60 × 15 = 900, seed=20260911 (冻结)
        intent = ("GP 正交增量挖矿首跑: 适应度=对 model_score+POOLED 池残差的死区 head; "
                  "登记依据=残差面板六判据(H0); 成功定义=跑通入册, 不是挖出 PASS "
                  "(plans/gp-ortho-mining-prereg-20261003.md §四)")
        out, register = OUT, True

    conn = db.get_conn()
    try:
        f = Factory(PreRegistration(intent=intent))
        m, dates_all, md_all = f.market()
        dead_dates = md_all[md_all >= pd.Timestamp(gate.DEAD[0])]

        t0 = time.time()
        controls, prov, eng = assemble_controls(m, dates_all, conn)
        cov, n_zero = control_coverage(controls, dead_dates)
        print(f"[控制集] {sorted(controls)} | 装配 {time.time()-t0:.0f}s | 覆盖 "
              f"{json.dumps(cov, ensure_ascii=False)} | 零控制月末锚 {n_zero}/{len(dead_dates)}",
              flush=True)
        if n_zero == len(dead_dates):
            raise RuntimeError("所有死区月末锚均无控制存留 —— 接线缺陷, 拒绝烧预算(预注册 §四.4)")

        gp = OrthoGPProposer(m, dead_dates, budget=budget,
                             fitness_mode="ortho", ortho_controls=controls)
        f.add(gp)
        n_prior = bank.cumulative_tests()
        denom = f.declare()
        n_used = max(denom, n_prior + denom)
        print(f"[预注册] 分母 {denom} | 跑前在册 {n_prior} | N_used = {n_used} "
              f"| register={register}", flush=True)

        t0 = time.time()
        records = f.run(save=False)          # head 路径对照读数; 不写库
        elapsed_search = time.time() - t0
        print(f"[工厂] 前沿交付 {len(records)} 条 | head 对照口径 | {elapsed_search:.0f}s",
              flush=True)

        # ---- 判决 + 登记(预注册 §三) --------------------------------------
        ind = fe.industry_map(conn)
        md = fe.month_end_dates(dates_all)
        md = md[md >= pd.Timestamp(f.prereg.eval_since)]
        zones = gate.zone_of(md)
        fwd = gate.forward_return(m, gate.HORIZON)
        tradable = f._tradable("eval", m, md)

        members, pending, dup_notes, n_skip = [], [], [], 0
        for hrow in records.to_dict("records"):
            aid, expr = hrow.get("alpha_id"), hrow.get("expr")
            t1 = time.time()
            try:
                panel_md, det = judge_ortho(expr, eng, ind, controls,
                                            tradable, md, zones, fwd)
            except RuntimeError:
                raise                        # 接线错误必炸(预注册 §四.4)
            except Exception as e:
                n_skip += 1
                print(f"[ortho] {aid} 求值失败跳过: {type(e).__name__}: {e}", flush=True)
                members.append({"alpha_id": aid, "expr": expr, "ortho": None,
                                "head": _head_row(hrow), "registered": False,
                                "skip": f"{type(e).__name__}: {e}"})
                continue
            if det is None:              # 工厂既定语义: det.empty ⇒ 落日志不登记
                n_skip += 1
                members.append({"alpha_id": aid, "expr": expr, "ortho": None,
                                "head": _head_row(hrow), "registered": False,
                                "skip": "no valid cross-sections"})
                print(f"[ortho] {aid} 无有效截面, 不登记", flush=True)
                continue
            cost = critic.churn_cost(panel_md, md)
            # 候选适应度留痕 = 工厂 head 记录 note 的最后一段("死区适应度 … / Pareto 前沿第 …")
            cand_note = str(hrow.get("note", "")).rsplit(" | ", 1)[-1]
            rec = gate.summarize(aid, det, n_used, extra={
                "avg_names": float(det["n"].mean()),
                "expr": expr,
                "info_source": "price_volume_gp",
                "source": "GPProposer(NSGA-II, fitness_mode=ortho)",
                "pit_version": "pit_v1_%s" % f.prereg.eval_since,
                "note": "%s | fitness_mode=ortho | head对照: DEAD_head=%+.4f "
                        "t=%+.2f ACTIVE_head=%+.4f p_adj=%s PASS=%s | cand: %s" % (
                            f.prereg.to_note(), hrow.get("DEAD_head", float("nan")),
                            hrow.get("DEAD_head_t", float("nan")),
                            hrow.get("ACTIVE_head", float("nan")),
                            hrow.get("p_adj"), hrow.get("PASS"), cand_note),
            }, cost_per_month=cost.get("cost_per_month"),
                n_tests_family=denom, n_tests_prior=n_prior)
            dup2 = bank.check_duplicate(
                {"alpha_id": aid, "expr": expr, "yearly": rec.get("yearly")},
                values=panel_md, md=md)
            if dup2["dup"]:
                dup_notes.append({"alpha_id": aid, **dup2})
                members.append({"alpha_id": aid, "expr": expr,
                                "ortho": _rec_row(rec), "head": _head_row(hrow),
                                "registered": False, "skip": "duplicate",
                                "dup": dup2})
                print(f"[ortho] {aid} 残差面板撞车拒登({dup2['kind']} ← {dup2['existing']})",
                      flush=True)
                continue
            members.append({"alpha_id": aid, "expr": expr,
                            "ortho": _rec_row(rec), "head": _head_row(hrow),
                            "registered": register})
            pending.append((panel_md, rec))
            print(f"[ortho] {aid} DEAD head {rec['DEAD_head']:+.4f} (t={rec['DEAD_head_t']:+.2f}) "
                  f"top6 {rec['DEAD_top6']:+.4f} | ACTIVE head {rec['ACTIVE_head']:+.4f} "
                  f"| p_adj {rec['p_adj']:.4f} | 线 {rec['head_bar']:.4f} | "
                  f"{'PASS' if rec['PASS'] else '否'} | {time.time()-t1:.1f}s", flush=True)

        if register and pending:
            print(f"[登记] {len(pending)} 条按 ortho 链入册…", flush=True)
            for panel_md, rec in pending:
                bank.save_values(rec["alpha_id"], panel_md)
                bank.upsert(rec)
    finally:
        conn.close()

    n_pass = sum(1 for x in members
                 if (x.get("ortho") or {}).get("PASS"))
    payload = {
        "ran_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "prereg": PREREG,
        "smoke": args.smoke, "register": register,
        "budget": {"population": budget.population, "generations": budget.generations,
                   "seed": budget.seed, "max_depth": budget.max_depth,
                   "fitness_mode": "ortho"},
        "controls": prov,
        "denominator": denom, "n_prior": n_prior, "n_used": n_used,
        "n_front": len(members), "n_pass_ortho": n_pass,
        "n_skip": n_skip, "n_dup_residual": len(dup_notes),
        "duplicates_residual": dup_notes,
        "elapsed_search_sec": round(elapsed_search, 1),
        "members": members,
        "exit_note": "PASS=残差面板六判据全过=POOLED=有资格进组合层回测, 不等于可用; "
                     "0 PASS 的结论 = GP 在本仓死区×900 预算下, 对 model_score+POOLED "
                     "池的正交增量无可检出增量(预注册 §四.2)",
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=1, default=str),
                   encoding="utf-8")
    print(f"\n产物: {out}", flush=True)
    print(f"[读数] 前沿 {len(members)} | ortho PASS {n_pass} | 跳过 {n_skip} "
          f"| 残差撞车 {len(dup_notes)} | 搜索 {elapsed_search:.0f}s", flush=True)
    return 0


def _head_row(hrow):
    keys = ("DEAD_head", "DEAD_head_t", "DEAD_top6", "ACTIVE_head", "p_adj", "PASS")
    return {k: (None if pd.isna(hrow.get(k)) else hrow.get(k)) for k in keys
            if k in hrow}


def _rec_row(rec):
    keys = ("DEAD_head", "DEAD_head_t", "DEAD_top6", "ACTIVE_head", "p_adj",
            "head_bar", "cost_per_month", "PASS",
            "c_same_sign", "c_head_size", "c_t", "c_p_adj", "c_top6", "c_cost",
            "n_tests", "n_tests_family", "n_tests_prior")
    return {k: rec.get(k) for k in keys}


if __name__ == "__main__":
    sys.exit(main())
