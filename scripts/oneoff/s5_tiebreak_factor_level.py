r"""S5 **因子级**对照：选面并列时 tie-break 的影响（**只读**，不改码）。

## 为什么必须另起一个脚本（而不是改 `s5_tiebreak_impact.py`）

`scripts/oneoff/s5_tiebreak_impact.py`（2026-09-21）用的是**生产分** `model_score.score`
逐截面做两臂对照 ⇒ 翻档仅 **12 槽位 / 44385（0.027%）**、存在并列的截面 14/116。

但那次对照**很可能低估** S5，原因有二：

1. S5 选面实际发生在 `app/alpha/gate.py:96` 对**因子值**排序，而生产分是**连续值**、并列少；
2. M5 批次 4 实测 **4/7 条零次齐次因子的头部边界并列间距 `boundary_gap_min_rms = 0`**、
   **71/71 个死区月末**的边界间距小于值噪声 ⇒ 并列是**结构性**的，不是偶发。

⇒ 本脚本改用**真实因子值面板**（`data/alpha_pool/<alpha_id>.npz`，`bank.load_values` 读到的
就是 `gate.evaluate()` 收到的那个 `panel_md`）逐因子逐截面重做对照。

`scripts/oneoff/` 的纪律：**本目录是证据，不得删除、不许批量搬家**；旧脚本与其产物
（生产分那次）保持原样 —— 按 07 号"产物可覆盖 + 哈希手抄"的教训，**重跑换新名**。

## 判据（先冻结再跑，2026-09-22）

- **翻档槽位总数 = 0** ⇒ 当前非稳定排序在真实因子值上**不改变任何选面** ⇒
  S5 无实质收益，按预注册**结清**（不必付"在册 189 条读数变旧选面"的代价）。
- **翻档 > 0** ⇒ 量化翻档率与受影响因子数，再决定是否值得动 `app/alpha/gate.py`
  （动门禁 ⇒ 在册 189 条读数成为"旧选面" ⇒ 需另立预注册 + 用户批准）。

## 只读承诺

不写库、不改 `app/**`、不重训、不碰 `model_score`/`factor` 表。产物只落 `experiments/`。
两臂**只差 `kind`**（quicksort vs stable）⇒ 差异只来自排序稳定性，不来自口径。
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pandas as pd

from app.alpha import bank, gate

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "experiments" / "s5_tiebreak_factor_level.json"

#: 与 `gate` 同口径 —— **不自己写常量**，否则将来 gate 改了这里会静默漂移
TOP_FRAC = gate.TOP_FRAC
MIN_NAMES = gate.MIN_NAMES


def _sections_for(panel: pd.DataFrame) -> list:
    """逐截面算两臂翻档。截面判据与 `gate.evaluate` 一致（`len(f) >= MIN_NAMES`）。"""
    rows = []
    for d in panel.index:
        f = panel.loc[d].dropna()
        n = len(f)
        if n < MIN_NAMES:
            continue
        k = max(1, int(n * TOP_FRAC))
        if k >= n:                      # 与 gate 同构：k < n 才有"第 k+1 名"可比
            continue
        # A 臂 = 当前（sort_values 默认 quicksort，**非稳定**）
        a = f.sort_values(ascending=False)
        # B 臂 = 稳定排序（**只改 kind**，其余逐字相同）
        b = f.sort_values(ascending=False, kind="stable")
        ta, tb = set(a.index[:k]), set(b.index[:k])
        flip = len(ta ^ tb)
        # 边界并列：第 k 名与第 k+1 名的间距（因子值原单位，未做 RMS 归一 —— 本脚本不载行情）
        gap = float(a.iloc[k - 1] - a.iloc[k])
        kth = a.iloc[k - 1]
        rows.append({
            "date": d.strftime("%Y%m%d"),
            "n": int(n), "k": int(k),
            "flip": int(flip),
            "boundary_gap": gap,
            "boundary_tied": bool(gap == 0.0),
            "n_at_kth_value": int((f == kth).sum()),
        })
    return rows


def _dist(affected: list) -> dict:
    """受影响截面的翻档规模分布（`affected` = [(flip, k), ...]）。

    只报总翻档率会丢掉关键区别：**"少数截面翻很多"**（可能是真问题）与
    **"很多截面各翻一两个"**（多半是边界并列的必然结果、且边界上收益本就近似）含义完全不同。
    `flip_share_of_k` = 翻档数 / 该截面 top-k 大小 ⇒ 回答"翻的幅度占选面多大比例"。
    """
    if not affected:
        return {"n": 0}
    flips = sorted(f for f, _ in affected)
    shares = sorted(f / k for f, k in affected if k)
    n = len(flips)

    def q(xs, p):
        return xs[min(n - 1, int(p * n))]

    return {
        "n": n,
        "flip_mean": round(sum(flips) / n, 2),
        "flip_median": flips[n // 2],
        "flip_p90": q(flips, 0.90),
        "flip_max": flips[-1],
        "flip_share_of_k_mean": round(sum(shares) / len(shares), 6),
        "flip_share_of_k_median": round(shares[len(shares) // 2], 6),
        "flip_share_of_k_max": round(shares[-1], 6),
    }


def main() -> int:
    t0 = time.time()
    print("== S5 **因子级**对照：tie-break 对选面的影响（只读）==")
    print(f"口径：TOP_FRAC={TOP_FRAC}（来自 gate）、MIN_NAMES={MIN_NAMES}（来自 gate）")

    alphas = bank.list_alphas()
    if alphas.empty:
        print("alpha_bank 为空 ⇒ 无法跑")
        return 2
    ids = [a for a in alphas["alpha_id"].tolist() if bank.has_values(a)]
    print(f"alpha_bank {len(alphas)} 条，其中**有值面板** {len(ids)} 条")

    per_factor, skipped = [], []
    tot_k = tot_flip = 0
    sections_all = 0
    #: 受影响截面的翻档规模分布 —— 只报"翻档率"不够：0.47% 的总率可能是
    #: "少数截面翻很多"或"很多截面各翻一两个"，两者的处置含义完全不同。
    affected = []

    for aid in ids:
        try:
            panel = bank.load_values(aid)
        except Exception as e:                      # 单个面板坏了不该拖垮整轮
            skipped.append({"alpha_id": aid, "why": f"{type(e).__name__}: {e}"})
            continue
        rows = _sections_for(panel)
        if not rows:
            skipped.append({"alpha_id": aid, "why": "无满足 MIN_NAMES 的截面"})
            continue
        fk = sum(r["k"] for r in rows)
        ff = sum(r["flip"] for r in rows)
        tot_k += fk
        tot_flip += ff
        sections_all += len(rows)
        affected += [(r["flip"], r["k"]) for r in rows if r["flip"] > 0]
        per_factor.append({
            "alpha_id": aid,
            "n_sections": len(rows),
            "top_slots": fk,
            "flip_slots": ff,
            "flip_rate": round(ff / fk, 6) if fk else None,
            "sections_with_flip": int(sum(1 for r in rows if r["flip"] > 0)),
            "sections_boundary_tied": int(sum(1 for r in rows if r["boundary_tied"])),
            "boundary_gap_min": min(r["boundary_gap"] for r in rows),
            "max_flip_in_section": max(r["flip"] for r in rows),
        })

    if not per_factor:
        print("没有任何因子有可用截面 ⇒ 无法判定")
        return 2

    rate = tot_flip / tot_k if tot_k else None
    fac_with_flip = sum(1 for p in per_factor if p["flip_slots"] > 0)
    fac_with_ties = sum(1 for p in per_factor if p["sections_boundary_tied"] > 0)
    sec_with_flip = sum(p["sections_with_flip"] for p in per_factor)

    print("\n== 结果 ==")
    print(f"  因子数            : {len(per_factor)}（跳过 {len(skipped)}）")
    print(f"  截面总数          : {sections_all}")
    print(f"  top 槽位总数      : {tot_k}")
    print(f"  **翻档槽位数**    : {tot_flip}")
    print(f"  **翻档率**        : {rate}")
    print(f"  有翻档的截面数    : {sec_with_flip} / {sections_all}")
    print(f"  有翻档的因子数    : {fac_with_flip} / {len(per_factor)}")
    print(f"  边界并列(=0)的因子: {fac_with_ties} / {len(per_factor)}")
    ds = _dist(affected)
    if ds["n"]:
        print(f"  受影响截面翻档规模: 均值 {ds['flip_mean']}、中位 {ds['flip_median']}、"
              f"p90 {ds['flip_p90']}、最大 {ds['flip_max']} 槽位")
        print(f"  占该截面 top-k 比例: 均值 {ds['flip_share_of_k_mean']:.4%}、"
              f"中位 {ds['flip_share_of_k_median']:.4%}、最大 {ds['flip_share_of_k_max']:.4%}")

    if tot_flip == 0:
        verdict = (f"**翻档 = 0**（{sections_all} 截面 × {len(per_factor)} 因子，共 {tot_k} 个 top 槽位）"
                   "⇒ 当前非稳定排序在**真实因子值**上不改变任何选面 ⇒ S5 **无实质收益**，"
                   "按预注册结清：不付\"在册 189 条读数变旧选面\"的代价。"
                   "（注意：边界并列**确实存在**，见 sections_boundary_tied —— 只是并列时"
                   "两臂恰好选出同一集合，故不影响结果。）")
        print("\n⇒ 翻档 0：真实因子值上未造成任何差异 ⇒ S5 结清，不改门禁")
    else:
        verdict = (f"**翻档 {tot_flip} 槽位 / {tot_k}（{rate:.4%}）**，涉及 {fac_with_flip} 个因子、"
                   f"{sec_with_flip} 个截面 ⇒ 与生产分那次对照（0.027%）相比"
                   f"{'显著更大' if rate and rate > 0.00027 * 3 else '量级相近'} ⇒ "
                   "S5 有实质收益，建议按预注册实施 —— 但动门禁会让在册 189 条读数成为"
                   "\"旧选面\" ⇒ 需另立预注册 + 用户批准。")
        print(f"\n⇒ 翻档 {tot_flip} 槽位（{rate:.4%}）⇒ 需评估是否动门禁（需批准）")

    payload = {
        "ran_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "elapsed_sec": round(time.time() - t0, 1),
        "source": "data/alpha_pool/*.npz（bank.load_values ⇒ gate.evaluate 收到的同一个 panel_md）",
        "scope": {
            "n_factors": len(per_factor),
            "n_factors_skipped": len(skipped),
            "sections_total": sections_all,
            "top_frac": TOP_FRAC,
            "min_names": MIN_NAMES,
        },
        "overall": {
            "total_top_slots": tot_k,
            "total_flip_slots": tot_flip,
            "flip_rate": None if rate is None else round(float(rate), 6),
            "sections_with_flip": sec_with_flip,
            "factors_with_any_flip": fac_with_flip,
            "factors_with_boundary_ties": fac_with_ties,
            "affected_section_stats": _dist(affected),
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
