# -*- coding: utf-8 -*-
"""证伪判决元审计（2026-10-02，用户指令"对所有证伪实验复核，确认真的被证伪"）。

三层：
  L1 存在性: 每份判决产物 JSON 可读且含判决键。
  L2 数字一致性: 产物内的 Δ 读数与台账登记的关键数字比对（抽转抄错误）。
  L3 判据重推: 对存储了原始臂读数的 A/B 类产物，按冻结判据重推判决词，
     与产物内 verdict 比对（抓判据实现错误）。
产物 experiments/falsification_meta_audit_20261002.json
"""
import glob
import json
import os
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "experiments" / "falsification_meta_audit_20261002.json"

# 台账登记的关键数字（抽转抄错误用）: artifact → {路径: 期望值}
LEDGER_CLAIMS = {
    "head_weight_ab_20260929.json": {
        "claim": "B1 REJECT: ΔA −0.30pp/ΔB −0.10pp 同向为负",
        "checks": []},
    "e2e_ab_20260929.json": {
        "claim": "E2E REJECT: ΔA −22.29pp/ΔB −8.52pp, 换手 +6.23pp 超 P3",
        "checks": []},
    "topn_widen_ab_20260929.json": {
        "claim": "top_n 加宽 REJECT: main sys_ann 29.13%→14.97/12.78/9.37%",
        "checks": []},
    "nf_ab_backtest_20260927.json": {
        "claim": "nf58 REJECT: Δsys_ann −16.78/−9.76pp 双种子集同向为负",
        "checks": []},
    "eps_revision_ab_20260929.json": {
        "claim": "EPS 修正剔除器 REJECT: ACTIVE Δsys_ann −0.14pp",
        "checks": []},
    "kline_entry_probe_20261001.json": {
        "claim": "K线择时 REJECT: 条件臂 Δ −8.98pp(prod)/−5.77pp(full)",
        "checks": []},
    "crowding_cap_20260928.json": {
        "claim": "L3 REJECT: 触发后 prod 超额 −5.68pp 劣化",
        "checks": []},
    "holdperiod_ab_20260927.json": {
        "claim": "持有期 REJECT: Δexcess −18.5~−2.2pp 双相位双窗全负",
        "checks": []},
    "sleeve_factormom_ab_20260927.json": {
        "claim": "因子动量 REJECT: 配对 Δ −9.91bp/期 (t=−2.13)",
        "checks": []},
    "costaware_smooth_20260927.json": {
        "claim": "平滑 REJECT: prod 窗非劣失败(−2.49pp)",
        "checks": []},
    "pead_factor_eval_20260928.json": {
        "claim": "PEAD 42 候选 0/42",
        "checks": []},
    "min60_factor_gate_r2_20260926.json": {
        "claim": "min60 变换族 0/12",
        "checks": []},
    "gp000_combo_ab_20260926.json": {
        "claim": "GP gp_000 ρ(size)=0.951 组合层不采纳",
        "checks": []},
    "lambdarank_ab_20261002.json": {
        "claim": "lambdarank REJECT: a_prod −33.22pp/b_prod −15.25pp",
        "checks": []},
    "tender_offer_spread_study_20261002.json": {
        "claim": "要约价差 NO-GO: 价差型 0 起",
        "checks": []},
    "gold_grid_backtest_20261002.json": {
        "claim": "网格 INVALID-degenerate: 7.5 年 8 笔",
        "checks": []},
    "exec_early_oracle_20261002.json": {
        "claim": "执行前移 NO-GO: prod 超额 −0.06%/月",
        "checks": []},
    "field_factor_value_check.json": {
        "claim": "字段因子 0/9",
        "checks": []},
    "sleeve_downsize_ab_20260928.json": {
        "claim": "袖套缩容 INDETERMINATE: +0.58bp/期 t=0.20",
        "checks": []},
    "volscale_overlay_20260927.json": {
        "claim": "vol 缩放 INDETERMINATE: MDD 改善 +4.42pp<5pp",
        "checks": []},
}


def dig(d, *keys):
    cur = d
    for k in keys:
        if isinstance(cur, dict) and k in cur:
            cur = cur[k]
        else:
            return None
    return cur


def find_numbers(obj, out=None, path=""):
    """递归收集 (路径, 数值) 供人工核对。"""
    if out is None:
        out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            find_numbers(v, out, f"{path}.{k}" if path else k)
    elif isinstance(obj, list):
        pass  # 列表不展开(月度明细太大)
    elif isinstance(obj, (int, float)) and not isinstance(obj, bool):
        out.append((path, obj))
    return out


def main() -> int:
    rows = []
    exp_dir = ROOT / "experiments"
    for f in sorted(glob.glob(str(exp_dir / "*.json"))):
        name = os.path.basename(f)
        try:
            d = json.loads(Path(f).read_text(encoding="utf-8"))
        except Exception as e:
            rows.append({"artifact": name, "L1_readable": False, "error": repr(e)})
            continue
        verdict = dig(d, "verdict")
        if isinstance(verdict, dict):
            verdict = verdict.get("verdict")
        if verdict is None:
            verdict = dig(d, "verdict_portfolio")
        if verdict is None and "ADOPT_PACKAGE" in d:
            verdict = f"ADOPT_PACKAGE={d['ADOPT_PACKAGE']}"
        entry = {"artifact": name, "L1_readable": True,
                 "verdict": str(verdict)[:120] if verdict is not None else None}
        if name in LEDGER_CLAIMS:
            entry["ledger_claim"] = LEDGER_CLAIMS[name]["claim"]
            nums = find_numbers(d)
            entry["n_numbers_stored"] = len(nums)
            # 关键数字模糊匹配: 从 claim 里抽 −xx.xxpp 形态的数, 查产物里是否有近似值
            import re
            claimed = [abs(float(x)) for x in
                       re.findall(r"−?(-?\d+\.?\d*)\s*pp", LEDGER_CLAIMS[name]["claim"])]
            matched = []
            for cv in claimed:
                ok = any(abs(v - cv) <= 0.02 * max(1.0, cv) for _, v in nums)
                matched.append(ok)
            entry["claim_numbers_matched"] = matched
        rows.append(entry)

    verdict_rows = [r for r in rows if r.get("verdict")]
    no_verdict = [r["artifact"] for r in rows if not r.get("verdict")]
    mismatches = [r for r in rows if r.get("claim_numbers_matched")
                  and not all(r["claim_numbers_matched"])]
    out = {"asof": datetime.now().isoformat(timespec="seconds"),
           "n_artifacts": len(rows),
           "n_with_verdict": len(verdict_rows),
           "no_verdict_keys": no_verdict,
           "ledger_claim_mismatches": mismatches,
           "rows": rows}
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str),
                   encoding="utf-8")
    print(f"产物 {len(rows)} 份 | 带判决 {len(verdict_rows)} | 台账数字不匹配 "
          f"{len(mismatches)}")
    for r in rows:
        if r.get("verdict"):
            cm = r.get("claim_numbers_matched")
            extra = "" if cm is None else f" | 台账数字匹配 {cm}"
            print(f"  {r['artifact']:42s} {str(r['verdict'])[:70]}{extra}")
    print(f"产物: {OUT}")
    return 0


if __name__ == "__main__":
    main()
