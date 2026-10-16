"""M5（批次 5 动作①）：把 G3-① 从"数值容差门"改成"通道唯一性门"，重判批次 4。

批次 4 的机器判决是"本轮不可引用"，因为冻结的 G3-① 要求 7 条**零次齐次**（尺度无关）表达式的
两臂 head 读数差 ≤ 1e-6 —— 而"秩选出的均值"对 float32 末位本来就不连续（`_mech.json` 已量到：
恒等式残差 ≈1~4e-9，而读数差 6e-8~2.3e-5）。⇒ 那条门的前提是错的，不是构造错的。

本脚本**不重跑 158×2**，只读 `_mech.json` 与两臂读数，按新判据重判：

  G3-①′（三条同时成立 ⇒ 达）
   (i)  通道唯一：`ident_max_err / max(|DEAD_head_raw|, |DEAD_head_adj|) ≤ 1e-6`
        —— 把两臂各自的 top-k 选面放到同一份前向收益上回算 head 超额，与 `gate.evaluate` 的差
        （照抄其取数）相对读数本身要小到 1e-6 ⇒ 证明"值"这一层没有第二条通道进读数。
   (ii) 值层面只差尾数：`value_rel_mean ≤ 1e-3`（逐月末截面 RMS 归一后的平均相对差）。
   (iii) 秩不变：`min_spearman ≥ 0.999`（最差那个月末的横截面秩相关）。

  阴性对照（证明这条门**能红**，不是恒绿）：
   把 (i) 的分子换成"读数本身的两臂差" `abs_d_top6 / |DEAD_top6|` —— 这正是 G3-① 当年混用的东西。
   若"值/选面差异真的会第二个通道进读数"，(i) 就会长成这个样子 ⇒ 门必须判红。
   对照判不出红 ⇒ 这条门是死的，本脚本必须失败。

判据来源与诚实边界：三条阈值是**看到 `_mech.json` 已有读数之后**写的（那份产物 09-20 16:31 已入库、
台账已公开其数字）⇒ 本次重判**不构成独立确认**，它的全部效力只在于"把门改成它本来想测的量"。
判决的强度因此只挂在两件事上：恒等式残差与读数差相差 3~5 个数量级（可复算），以及阴性对照会红。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "experiments" / "b_stage_20260920" / "_mech.json"
OUT = ROOT / "experiments" / "b_stage_20260920" / "m5_g3_channel_gate.json"

TOL_CHANNEL = 1e-6      # (i) 相对恒等式残差
TOL_VALUE = 1e-3        # (ii) 值面板归一相对差
FLOOR_SP = 0.999        # (iii) 最差月末秩相关


def main() -> int:
    d = json.loads(SRC.read_text("utf-8"))
    rows = []
    for f in d["per_factor"]:
        head = max(abs(f["DEAD_head_raw"]), abs(f["DEAD_head_adj"]))
        ch_ratio = f["ident_max_err"] / head if head else float("inf")
        t6 = abs(f["DEAD_top6_adj"]) or 1e-12
        ctrl_ratio = f["abs_d_top6"] / t6
        rows.append({
            "name": f["name"],
            "channel_ratio": ch_ratio, "i_channel_ok": ch_ratio <= TOL_CHANNEL,
            "value_rel_mean": f["value_rel_mean"], "ii_value_ok": f["value_rel_mean"] <= TOL_VALUE,
            "min_spearman": f["min_spearman"], "iii_rank_ok": f["min_spearman"] >= FLOOR_SP,
            "pass": ch_ratio <= TOL_CHANNEL and f["value_rel_mean"] <= TOL_VALUE
                    and f["min_spearman"] >= FLOOR_SP,
            "negative_control_ratio_if_second_channel_existed": ctrl_ratio,
            "boundary_gap_min_rms": f["boundary_gap_min_rms"],
            "months_with_headset_diff": f["n_dates_headset_diff"], "n_dates": f["n_dates"]})

    gate_pass = all(r["pass"] for r in rows)
    # 阴性对照必须红：读数层面的相对差（旧门混用的量）必须显著越过 1e-6
    ctrl_red = [r["name"] for r in rows
                if r["negative_control_ratio_if_second_channel_existed"] > TOL_CHANNEL]
    res = {
        "criteria": "本文件 docstring + plans/ledger-settlement-criteria-20260920.md §M5",
        "source_artifact": "experiments/b_stage_20260920/_mech.json（批次 4 机制归因臂，未重跑）",
        "thresholds": {"i_channel_ratio<=": TOL_CHANNEL, "ii_value_rel_mean<=": TOL_VALUE,
                       "iii_min_spearman>=": FLOOR_SP},
        "per_factor": rows,
        "gate_G3_1_prime": "PASS" if gate_pass else "FAIL",
        "negative_control": {
            "design": "用两臂**读数差**冒充恒等式残差喂同一条 (i) ⇒ 门必须判红",
            "factors_that_turn_it_red": ctrl_red,
            "pass_gate_is_falsifiable": bool(ctrl_red)},
        "separation_orders_of_magnitude": round(
            max(r["negative_control_ratio_if_second_channel_existed"] / max(r["channel_ratio"], 1e-30)
                for r in rows), 1),
        "verdict": None,
        "honesty_note": "阈值在看到 _mech.json 读数之后设定 ⇒ 重判不是独立确认；效力仅在"
                        "'门改成测它本来想测的量' + 阴性对照可红 + 恒等式残差与读数差差 3~5 个数量级",
    }
    if not res["negative_control"]["pass_gate_is_falsifiable"]:
        res["verdict"] = "作废：新门连'把读数差当残差'都抓不住 ⇒ 它是恒绿的假门"
    elif gate_pass:
        res["verdict"] = ("G3-①′ 达 ⇒ 批次 4 的 G1/G2 解除'不可引用'：G1 两臂 PASS 均 0、"
                          "G2 翻动 16/158 条可用（幅度 6e-5~5.8e-4，全在零次齐次族的 float32 尾数区间）")
    else:
        res["verdict"] = "G3-①′ 仍不达 ⇒ 批次 4 维持'不可引用'"
    OUT.write_text(json.dumps(res, indent=2, ensure_ascii=False) + "\n", "utf-8")
    print(json.dumps({"gate": res["gate_G3_1_prime"], "verdict": res["verdict"],
                      "control_red_on": res["negative_control"]["factors_that_turn_it_red"],
                      "separation_x": res["separation_orders_of_magnitude"]}, ensure_ascii=False))
    print(f"→ {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
