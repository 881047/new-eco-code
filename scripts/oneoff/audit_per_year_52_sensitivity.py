"""ONEOFF(2026-09-20, 批次 3 / Q5): `PER_YEAR=52` 换成**实际每年期数**会让读数动多少。

门限跑前冻结在 `plans/b-stage-criteria-20260920.md`：**存在距自身判据线 <1.599% 的
PER_YEAR 系读数 ⇒ 进第二步（改常数 + 5 个冻结脚本加注记）；否则关闭为"已知、不跨判档"。**

为什么用真序列而不是线性折算：年化公式是**几何**的
（`F1b_weekly_open_overlap.py:422 ann() = (1+r).prod()**(per_year/n) − 1`），
52→51.182 是把指数乘 0.98427 ⇒ 对高年化率的绝对影响**大于** 1.6%（凸性）。
线性折算会低估，所以这里直接拿 F66 的逐期序列（491 期，两套种子，`experiments/05_label/`）
把两种 `per_year` 各算一遍。

⚠️ 本脚本**不复现** F52 的在册读数（那是另一件事：构型/掩码/基准都可能有差别）——
它只回答"这把尺的常数错了多少、会不会把某个结论推过线"。F66 的 `bench` 是**池等权**
（镜厅 F74 实测：F66.bench 与中证800 相关仅 0.66）⇒ 所有超额都必须带"池口径"三字引用。

只读：不写任何生产表；产物双写 logs/ + 被 git 跟踪的 experiments/b_stage_20260920/。
"""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parents[2]
SERIES = {s: BASE / "experiments" / "05_label" / f"F66_series_seed{s}.json" for s in ("A", "B")}
TARGET_LINE_PP = 10.0          # 用户目标线（周频超额 ≥ +10pp/年）
CONST = 52.0


def ann(s: pd.Series, per_year: float) -> float:
    """与 F1b:422 逐字同式（几何年化）。"""
    v = s.dropna()
    if len(v) < 2:
        return float("nan")
    return float((1 + v).prod() ** (per_year / len(v)) - 1)


def per_year_actual(idx: pd.DatetimeIndex, n: int) -> float:
    yrs = (idx.max() - idx.min()).days / 365.25
    return n / yrs


def window(df: pd.DataFrame, lo=None, hi=None) -> pd.DataFrame:
    out = df
    if lo:
        out = out[out["date"] >= pd.Timestamp(lo)]
    if hi:
        out = out[out["date"] <= pd.Timestamp(hi)]
    return out


def block(tag: str, df: pd.DataFrame) -> dict:
    idx = pd.DatetimeIndex(df["date"])
    py_act = per_year_actual(idx, len(df))
    r, b = df["ret"], df["bench"]
    e52 = 100 * (ann(r, CONST) - ann(b, CONST))
    e_act = 100 * (ann(r, py_act) - ann(b, py_act))
    return {"window": tag, "periods": int(len(df)), "per_year_actual": round(py_act, 4),
            "excess_pool_52_pp": round(float(e52), 3),
            "excess_pool_actual_pp": round(float(e_act), 3),
            "shift_pp": round(float(e52 - e_act), 3),
            "shift_relative_pct": round(float(100 * (e52 - e_act) / abs(e52)), 3),
            "distance_to_target_line_pp": round(float(abs(e52) - TARGET_LINE_PP), 3)}


res: dict = {"formula": "ann = (1+r).prod()**(per_year/n) - 1  (F1b:422, 几何)",
             "bench_is": "池等权（F66.bench；与中证800 相关仅 0.66 ⇒ 所有超额是**池口径**）",
             "caveat": "不复现 F52 在册读数；只量常数敏感性"}
per_seed = {}
for seed, path in SERIES.items():
    df = pd.DataFrame(json.loads(path.read_text(encoding="utf-8")))
    df["date"] = pd.to_datetime(df["date"], unit="ms")
    per_seed[seed] = [
        block("全史", df),
        block("留出集 2024-01 起", window(df, "2024-01-01")),
        block("死区 2020-09~2023-12", window(df, "2020-09-01", "2023-12-31")),
    ]
res["per_seed"] = per_seed

# 门判定：**同一单位**逐条比 —— 这条读数被常数挪动多少 pp， vs 它**在线的哪一侧**、离得多远。
# ⚠️ 两版纠错记录：① 第一版拿"相对 %" 比 "pp 距离"（单位混了）；② 第二版把"已经在线下方"
#    的读数也算成 flip_risk（负 margin 与正 shift 一比就真，逻辑错）。翻档只可能发生在
#    **当前在线上方、且被挪动后跨过线**的情形 ⇒ margin 必须为正且 ≤ 挪动量。
worst = []
for seed, blocks in per_seed.items():
    for b in blocks:
        margin = b["excess_pool_52_pp"] - TARGET_LINE_PP       # >0 = 当前在线上方
        b["margin_above_line_pp"] = round(margin, 3)
        b["flip_risk"] = bool(margin > 0 and b["shift_pp"] >= margin)
        worst.append({"seed": seed, "window": b["window"], "shift_pp": b["shift_pp"],
                      "margin_above_line_pp": round(margin, 3), "flip_risk": b["flip_risk"]})
res["self_check"] = {
    "复现在册读数(F52 全史池口径 +17.78/+17.47)": {
        "seedA": per_seed["A"][0]["excess_pool_52_pp"],
        "seedB": per_seed["B"][0]["excess_pool_52_pp"],
        "与台账差的绝对值 <0.02pp": bool(
            abs(per_seed["A"][0]["excess_pool_52_pp"] - 17.78) < 0.02
            and abs(per_seed["B"][0]["excess_pool_52_pp"] - 17.47) < 0.02)}}
res["gate"] = {"criterion": "读数当前在判据线上方，且被常数挪动的 pp ≥ 它的线上余量 ⇒ 会翻档 ⇒ 进第二步",
               "per_reading": worst,
               "crossed": any(w["flip_risk"] for w in worst),
               "already_below_line": [w for w in worst if w["margin_above_line_pp"] <= 0],
               "tightest_above": (min([w for w in worst if w["margin_above_line_pp"] > 0],
                                      key=lambda w: w["margin_above_line_pp"] - w["shift_pp"])
                                  if any(w["margin_above_line_pp"] > 0 for w in worst) else None)}
body = json.dumps(res, ensure_ascii=False, indent=2, default=str)
out = BASE / "logs" / "audit_per_year_52_sensitivity.json"
out.parent.mkdir(exist_ok=True)
out.write_text(body, encoding="utf-8")
tr = BASE / "experiments" / "b_stage_20260920" / "per_year_52_sensitivity.json"
tr.parent.mkdir(exist_ok=True)
tr.write_text(body, encoding="utf-8")
import hashlib
print("sha", hashlib.sha256(body.encode()).hexdigest()[:16], "| 双写", out.name, tr.parent.name)
for seed, blocks in per_seed.items():
    for b in blocks:
        print(f"seed{seed} {b['window']:<18} 期数={b['periods']:>3} 实际per_year={b['per_year_actual']:>7} "
              f"超额(池) 52→实际 {b['excess_pool_52_pp']:>8} → {b['excess_pool_actual_pp']:>8} "
              f"(动 {b['shift_pp']}pp = {b['shift_relative_pct']}%)")
print("门:", res["gate"])
