r"""S5 选项 C：翻档对**头部收益**的实际影响（**只读**，不改码）。

## 背景（前两次对照已完成的）

- `s5_tiebreak_impact.py`（09-21，生产分）：翻档 12/44385（0.027%）
- `s5_tiebreak_factor_level.py`（09-22，因子值面板）：翻档 35702/7655774（**0.4663%**），
  122/189 因子、2974/16646 截面受影响，个别截面翻档占 top-k **90.19%** ⇒ 选面不可复现
  ⇒ "零影响"预期被推翻 ⇒ 是否动门禁挂给用户拍板（选项 A 不动 / B 动 / C 先量化收益影响）。
- 本脚本 = **选项 C**：回答"翻档改变的那些名字，值多少钱"——把对照从**成员资格**推进到
  **前向收益**，让 A/B 决策有数字。

## 口径（与生产门禁逐字对齐）

- 前向收益：`gate.forward_return(m, gate.HORIZON)` —— **门禁读数就是它算的**（朴素标签
  `shift(-h)/cur-1`，与 trainer 生产标签有意不同，见 gate.py 文档串）。不自己写公式。
- 截面/头部：`gate.TOP_FRAC`(0.10) / `gate.MIN_NAMES`(200) 直接 import；zone 过滤与
  `gate.evaluate` 一致（只看 DEAD/ACTIVE）。均值基准 = `r[common].mean()`，同 gate。
- 两臂**只差 `kind`**（quicksort vs stable）⇒ 差异只来自排序稳定性，不来自口径。

## 判据（先冻结再跑，2026-09-22）

记 Δ = 稳定臂 head_excess − 当前臂 head_excess（21 日收益，单位与小数同 gate），
`GATE_MIN_HEAD = 0.5%/月` 为门禁头部下界：

- **不可辨（数据支持选项 A：不动门禁）**：受影响截面 Δ 的 |朴素 t| < 2（均值与 0 不可区分）
  **且** 所有受影响因子 |ΔDEAD_head| < 0.1 × GATE_MIN_HEAD（= 5bp/月，判决量不足门槛一成）
  ⇒ 翻档对门禁读数的影响在噪声分辨力以下 ⇒ 按预注册登记"选面不可复现但收益影响不可辨"。
- **可辨（数据呈给用户拍板，B 的代价照旧）**：|t| ≥ 2（系统性偏移，无论方向）
  或 任一受影响因子 |ΔDEAD_head| ≥ 0.1 × GATE_MIN_HEAD ⇒ 翻档已能搬动判决量 ⇒ A/B 由用户定。
- 附注：t 为朴素 t（与 `gate._stat` 同样未做自相关修正 ⇒ 保守方向）。Δ>0 = 稳定臂更好。

## 只读承诺

不写库、不改 `app/**`、不重训、不碰 `model_score`/`factor`/`alpha_bank` 表。产物只落 `experiments/`。
按 07 号教训**新脚本新产物名**，不动前两次的脚本与产物。
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import numpy as np
import pandas as pd

from app import db
from app.logger import setup_logger
from app.alpha import bank, gate
from app.factors.factor_engine import load_market

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "experiments" / "s5_tiebreak_return_impact.json"

TOP_FRAC = gate.TOP_FRAC
MIN_NAMES = gate.MIN_NAMES
HORIZON = gate.HORIZON
GATE_MIN_HEAD = gate.GATE_MIN_HEAD
#: 冻结判据里的"可辨"门槛：判决量位移 ≥ 10% × GATE_MIN_HEAD
MATERIAL_FRAC_OF_BAR = 0.1


def _sections(panel: pd.DataFrame, fwd: pd.DataFrame) -> list:
    """逐截面两臂 head_excess。口径镜像 gate.evaluate（zone 过滤 + common + MIN_NAMES）。

    返回逐截面记录；flip>0 的截面额外记换名收益差（进 top 的 − 出 top 的）。
    """
    rows = []
    zones = gate.zone_of(panel.index)
    for i, d in enumerate(panel.index):
        if zones[i] == "OTHER":
            continue
        if d not in fwd.index:
            continue
        f = panel.loc[d].dropna()
        r = fwd.loc[d].dropna()
        common = f.index.intersection(r.index)
        if len(common) < MIN_NAMES:
            continue
        f, r = f[common], r[common]
        n = len(f)
        k = max(1, int(n * TOP_FRAC))
        if k >= n:
            continue
        # A 臂 = 当前生产（quicksort，非稳定）；B 臂 = 稳定。只差 kind。
        a = f.sort_values(ascending=False)
        b = f.sort_values(ascending=False, kind="stable")
        ta, tb = set(a.index[:k]), set(b.index[:k])
        flip = len(ta ^ tb)
        mean_all = float(r.mean())
        head_a = float(r[a.index[:k]].mean()) - mean_all
        head_b = float(r[b.index[:k]].mean()) - mean_all
        rec = {
            "date": d.strftime("%Y%m%d"),
            "zone": str(zones[i]),
            "n": int(n), "k": int(k),
            "flip": int(flip),
            "d_head": head_b - head_a,
        }
        if flip:
            entered, exited = tb - ta, ta - tb
            rec["gap_flipped"] = float(r[list(entered)].mean() - r[list(exited)].mean())
        rows.append(rec)
    return rows


def _t(s) -> float:
    s = pd.Series(s).dropna()
    if len(s) < 3:
        return float("nan")
    sd = s.std(ddof=1)
    return float(s.mean() / (sd / np.sqrt(len(s)))) if sd and sd > 0 else float("nan")


def main() -> int:
    t0 = time.time()
    print("== S5 选项 C：tie-break 翻档对头部收益的影响（只读）==")
    print(f"口径：TOP_FRAC={TOP_FRAC}、MIN_NAMES={MIN_NAMES}、HORIZON={HORIZON}"
          f"（均 import 自 gate）；fwd = gate.forward_return（与门禁读数同尺）")

    setup_logger()
    db.init_db()

    m = load_market()
    fwd = gate.forward_return(m, HORIZON)
    print(f"行情面板 {m['close'].shape}，fwd 非全 NaN 截面 {int(fwd.notna().any(axis=1).sum())}")

    alphas = bank.list_alphas()
    ids = [a for a in alphas["alpha_id"].tolist() if bank.has_values(a)]
    print(f"alpha_bank {len(alphas)} 条，其中有值面板 {len(ids)} 条")

    per_factor, skipped = [], []
    all_d = []          # 全部受影响截面的 Δ
    all_gap = []        # 全部受影响截面的换名收益差
    sec_total = sec_flip = 0
    tot_k = tot_flip = 0

    for aid in ids:
        try:
            panel = bank.load_values(aid)
        except Exception as e:
            skipped.append({"alpha_id": aid, "why": f"{type(e).__name__}: {e}"})
            continue
        rows = _sections(panel, fwd)
        if not rows:
            skipped.append({"alpha_id": aid, "why": "无满足门禁口径的截面"})
            continue
        sec_total += len(rows)
        fk = sum(r["k"] for r in rows)
        ff = sum(r["flip"] for r in rows)
        tot_k += fk
        tot_flip += ff
        aff = [r for r in rows if r["flip"] > 0]
        sec_flip += len(aff)
        all_d += [r["d_head"] for r in aff]
        all_gap += [r["gap_flipped"] for r in aff]
        # 判决量（gate.verdict 读的是分区均值）：Δ分区均值 = 稳定臂 − 当前臂
        zdelta = {}
        for z in ("DEAD", "ACTIVE"):
            zr = [r for r in rows if r["zone"] == z]
            if zr:
                zdelta[f"{z}_head_delta"] = float(np.mean([r["d_head"] for r in zr]))
        per_factor.append({
            "alpha_id": aid,
            "n_sections": len(rows),
            "sections_with_flip": len(aff),
            "flip_slots": ff,
            **zdelta,
            "max_abs_d_head": max((abs(r["d_head"]) for r in rows), default=0.0),
        })

    if not per_factor:
        print("没有任何因子有可用截面 ⇒ 无法判定")
        return 2

    ds = pd.Series(all_d)
    gs = pd.Series(all_gap)
    t_d, t_g = _t(ds), _t(gs)
    aff_fac = [p for p in per_factor if p["sections_with_flip"] > 0]

    def _q(x, p):
        x = sorted(x)
        return x[min(len(x) - 1, int(p * len(x)))] if len(x) else float("nan")

    bar = MATERIAL_FRAC_OF_BAR * GATE_MIN_HEAD
    worst = max(aff_fac, key=lambda p: abs(p.get("DEAD_head_delta", 0.0)), default=None)
    n_material_dead = sum(1 for p in aff_fac if abs(p.get("DEAD_head_delta", 0.0)) >= bar)
    t_ok = bool(t_d == t_d and abs(t_d) < 2)
    size_ok = bool(n_material_dead == 0)
    indistinguishable = t_ok and size_ok

    print("\n== 结果（Δ = 稳定臂 − 当前臂，21 日收益）==")
    print(f"  评估截面          : {sec_total}（受影响 {sec_flip}）")
    print(f"  翻档槽位          : {tot_flip} / {tot_k}（{tot_flip / tot_k:.4%}）")
    if len(ds):
        print(f"  受影响截面 Δ(bp)  : 均值 {ds.mean() * 1e4:+.2f}、中位 {ds.median() * 1e4:+.2f}、"
              f"p90(|Δ|) {abs(ds).quantile(0.9) * 1e4:.2f}、最大|Δ| {ds.abs().max() * 1e4:.2f}")
        abs_t = abs(t_d) if t_d == t_d else float("nan")
        print(f"  Δ 朴素 t          : {t_d:+.3f}（|t|={abs_t:.3f}）")
        print(f"  换名收益差(bp)    : 均值 {gs.mean() * 1e4:+.2f}、中位 {gs.median() * 1e4:+.2f}、t {t_g:+.3f}")
    print(f"  受影响因子数      : {len(aff_fac)} / {len(per_factor)}")
    print(f"  ΔDEAD_head 可辨   : {n_material_dead} 个因子 |Δ| ≥ {bar * 1e4:.1f}bp/月"
          f"（= {MATERIAL_FRAC_OF_BAR:.0%} × 门禁下界 {GATE_MIN_HEAD * 1e4:.0f}bp/月）")
    if worst:
        print(f"  最差因子          : {worst['alpha_id']} ΔDEAD_head "
              f"{worst.get('DEAD_head_delta', float('nan')) * 1e4:+.2f}bp/月")

    if indistinguishable:
        verdict = (
            f"**不可辨**：受影响截面 Δ 的 |t| = {abs(t_d):.2f} < 2，且 {len(aff_fac)} 个受影响因子"
            f"的 |ΔDEAD_head| 全部 < {bar * 1e4:.1f}bp/月（门禁下界的一成）"
            "⇒ 翻档对门禁读数的影响在噪声分辨力以下 ⇒ **数据支持选项 A**：不动门禁，"
            "在台账登记\"选面不可复现（成员级 0.4663%）但收益影响不可辨\"。"
        )
        print("\n⇒ 不可辨 ⇒ 数据支持选项 A（不动门禁，登记了事）")
    else:
        why = []
        if not t_ok:
            why.append(f"Δ 系统性非零（|t|={abs(t_d):.2f} ≥ 2）")
        if not size_ok:
            why.append(f"{n_material_dead} 个因子 ΔDEAD_head ≥ {bar * 1e4:.1f}bp/月")
        verdict = (
            f"**可辨**：{'；'.join(why)} ⇒ 翻档能搬动门禁判决量 ⇒ A/B 由用户拍板"
            "（B 代价照旧：另立预注册 + 在册 189 条读数成\"旧选面\"）。"
        )
        print("\n⇒ 可辨 ⇒ 数字呈用户拍板 A/B")

    payload = {
        "ran_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "elapsed_sec": round(time.time() - t0, 1),
        "source": "data/alpha_pool/*.npz + gate.forward_return(load_market()) —— 与门禁读数同尺",
        "criteria_frozen": {
            "material_frac_of_bar": MATERIAL_FRAC_OF_BAR,
            "gate_min_head": GATE_MIN_HEAD,
            "material_bar_bp_per_month": round(bar * 1e4, 2),
            "t_threshold": 2.0,
            "rule": "|t(Δ)|<2 且全部 |ΔDEAD_head| < 0.1×GATE_MIN_HEAD ⇒ 不可辨（支持A）；否则可辨（用户拍板）",
        },
        "scope": {
            "n_factors": len(per_factor),
            "n_factors_skipped": len(skipped),
            "sections_total": sec_total,
            "sections_with_flip": sec_flip,
            "total_top_slots": tot_k,
            "total_flip_slots": tot_flip,
            "top_frac": TOP_FRAC, "min_names": MIN_NAMES, "horizon": HORIZON,
        },
        "overall": {
            "d_head_mean": round(float(ds.mean()), 8) if len(ds) else None,
            "d_head_median": round(float(ds.median()), 8) if len(ds) else None,
            "d_head_p90_abs": round(float(abs(ds).quantile(0.9)), 8) if len(ds) else None,
            "d_head_max_abs": round(float(ds.abs().max()), 8) if len(ds) else None,
            "d_head_t": round(t_d, 4) if t_d == t_d else None,
            "gap_flipped_mean": round(float(gs.mean()), 8) if len(gs) else None,
            "gap_flipped_t": round(t_g, 4) if t_g == t_g else None,
            "factors_with_any_flip": len(aff_fac),
            "factors_material_dead": n_material_dead,
            "worst_factor": worst["alpha_id"] if worst else None,
            "worst_factor_dead_delta": round(worst.get("DEAD_head_delta", float("nan")), 8)
            if worst else None,
        },
        "per_factor": per_factor,
        "skipped": skipped,
        "verdict": verdict,
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print("产物:", OUT)
    print(f"耗时: {payload['elapsed_sec']}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
