"""S5 对照：选面并列时 tie-break 的影响（**只读**，不改码）。

## 背景（14 号台账开放项 S5）
`app/alpha/gate.py` L96：
    order = f.sort_values(ascending=False).index
`sort_values` 默认 `kind="quicksort"`（**非稳定**）⇒ 分数并列时，"谁进 top-k"取决于算法/输入顺序，而非数值 ⇒ 不可复现。
S5 提议改为：`kind="stable"` + 次键 `ts_code`（确定性 tie-break）。

M5 批次 4 实测：4/7 条零次齐次因子的头部边界并列间距 `boundary_gap_min_rms = 0`、71/71 个死区月末边界间距小于值噪声 ⇒ 并列是**结构性**的，不是偶发。

## 本脚本要量化的
用**真实生产分**（`model_score.score`）逐截面做两臂对照：
  A 臂 = 当前（quicksort，非稳定）
  B 臂 = 稳定排序（kind="stable"）
统计：并列规模、top-k 集合**翻档条数**、翻档率。
⇒ 翻档=0 且 Δ 在噪声内才值得实施（S5 预注册 S3-B3 的代价框架）。

## 只读承诺
不写库、不改 `gate.py`、不重训。产物只落 `experiments/`。
⚠️ 两臂都用**同一个** `sort_values`（只改 `kind`）⇒ 差异只来自稳定性，不来自口径。
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pandas as pd

from app import db

TOP_FRAC = 0.10          # 与 gate.TOP_FRAC 同口径（默认 top 10%）
MIN_NAMES = 50
OUT = Path(__file__).resolve().parents[2] / "experiments" / "s5_tiebreak_impact.json"


def main() -> int:
    t0 = time.time()
    print("== S5 对照：tie-break 对选面的影响（只读）==")

    conn = db.get_conn()
    try:
        ms = pd.read_sql("SELECT ts_code, trade_date, score FROM model_score", conn)
    finally:
        conn.close()
    if ms.empty:
        print("model_score 为空 ⇒ 无法跑")
        return 2
    ms["trade_date"] = pd.to_datetime(ms["trade_date"])
    piv = ms.pivot_table(index="trade_date", columns="ts_code",
                         values="score").sort_index()
    print(f"生产分: {piv.shape[0]} 期 × {piv.shape[1]} 只")

    rows = []
    for d in piv.index:
        f = piv.loc[d].dropna()
        if len(f) < MIN_NAMES:
            continue
        k = max(1, int(len(f) * TOP_FRAC))
        # A：当前（非稳定）
        a = f.sort_values(ascending=False)
        # B：稳定排序（只改 kind，不改口径）
        b = f.sort_values(ascending=False, kind="stable")
        ta, tb = set(a.index[:k]), set(b.index[:k])
        flip = len(ta ^ tb)                 # 对称差 = 翻档条数
        # 并列规模：top-k 内与第 k 名同分的数量
        kth = a.iloc[k - 1] if k <= len(a) else None
        tied = int((f == kth).sum()) if kth is not None else 0
        rows.append({
            "date": d.strftime("%Y%m%d"),
            "n": int(len(f)), "k": k,
            "flip": int(flip),
            "flip_rate": round(flip / k, 6),
            "tied_with_kth": tied,
        })

    if not rows:
        print("无可用截面")
        return 2

    tot_k = sum(r["k"] for r in rows)
    tot_flip = sum(r["flip"] for r in rows)
    rate = tot_flip / tot_k if tot_k else None
    n_tied = sum(1 for r in rows if r["tied_with_kth"] > 1)

    payload = {
        "ran_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "elapsed_sec": round(time.time() - t0, 1),
        "sections": len(rows),
        "top_frac": TOP_FRAC,
        "total_top_slots": tot_k,
        "total_flip_slots": tot_flip,
        "flip_rate": None if rate is None else round(float(rate), 6),
        "sections_with_ties": n_tied,
        "per_section_sample": rows[:10],
    }

    print("\n== 结果 ==")
    print(f"  截面数            : {len(rows)}")
    print(f"  top 槽位总数      : {tot_k}")
    print(f"  **翻档槽位数**    : {tot_flip}")
    print(f"  **翻档率**        : {rate}")
    print(f"  存在并列的截面数  : {n_tied} / {len(rows)}")

    if rate is None:
        payload["verdict"] = "未跑"
    elif tot_flip == 0:
        payload["verdict"] = ("翻档 0 ⇒ 当前非稳定排序在真实分上未造成差异 ⇒ S5 收益有限，"
                             "但为可复现性仍建议实施（并列存在时行为随 pandas 版本可能变。")
        print("\n⇒ 翻档 0：真实分上当前排序未造成差异（但并列存在时为可复现性仍建议实施）")
    else:
        payload["verdict"] = (f"翻档 {tot_flip} 槽位（{rate:.4%}）⇒ 当前排序确实改变选股结果 ⇒ "
                              "S5 有实质收益，建议按预注册实施（需你批准；实施后需重启服务）")
        print(f"\n⇒ 翻档 {tot_flip} 槽位（{rate:.4%}）⇒ S5 有实质收益，建议实施（需批准）")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print("产物:", OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
