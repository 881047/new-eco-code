"""F47 · 把**周频模型**训练到最优：超参网格（预注册）—— 判在组合层两轴，不看 IC

## 由来（为什么超参是没动过的真实空间）

周频模型的超参是**从月频继承的**：`app/models/trainer.PARAMS` 里
`num_leaves=31 / min_data_in_leaf=200 / learning_rate=0.05`，加上
`weekly.scores.ROUNDS=50`（那是 2026-09-04 **月频**轮数扫描的结论）。
但周频吃的是 **5 日标签**（`weekly.HORIZON=5`），而月频吃 21 日标签 ——
**标签噪声结构完全不同**：持有期越短，信噪比越低，最优模型通常**更小、更强正则**。
这套超参从来没在周频口径上扫过 ⇒ 这是"把模型训练到最优"里唯一没被碰过的一块。

## 网格（预注册就 3 个点，不做扩展搜索；基线 = 现役参数）

    BASE : 现役（num_leaves 31 / min_data 200 / lr 0.05 / 50 轮）—— 用已归档的 F4 分数
    C1   : 更小更稳      num_leaves 15 / min_data 500  / lr 0.05 / 50 轮
    C2   : 中正则+多轮   num_leaves 31 / min_data 500  / lr 0.03 / 100 轮 / ff 0.6 / λ2 5
    C3   : 强正则        num_leaves 15 / min_data 1000 / lr 0.03 / 150 轮 / ff 0.5 / bag 0.7 / λ2 10

其余全部冻结：36 个月训练窗、禁运 3 锚点、月末重训、4 种子分位排名平均、H=5 标签、
2×W5+1×pit 的融合权重、100 万 / top100 / 带 8× / 满仓 w=1.0。

## 预注册判据（组合层两轴；与 F41/F43/F44 同一套）

    ① 全史（491 期同窗口）**两套种子同时** 超额 ≥ BASE + **1.0pp**；
    ② ACTIVE = 留出集(2024-01~2026-08) 两套种子 ≥ BASE；
    ③ 全史 MDD 两套种子不劣于 BASE × 1.10；
    ④ **对照**：BASE 臂必须逐位复现 F41 记录的满仓读数（+18.00/+17.30、ACTIVE +10.38/+11.80）。
    多个配置同时满足 ⇒ 取**全史超额均值最高**者（事前定，不许事后挑）。
    全部不满足 ⇒ 结论是"超参不是这根杠杆"，写进证伪登记，**不再扩大网格**（避免变成搜索机器）。

## 诚实边界

- 3 个点是在**同一段**491 期上比较 ⇒ 又多一次多重比较；因此判据要求"≥BASE+1.0pp"而不是"≥BASE"，
  且要求两套种子同时满足 —— 不是为了好看，是为了压住"挑运气好的那个"；
- ACTIVE 仍是已开封留出集 ⇒ 只作方向；本实验的判决主要靠 ①；
- **训练型改动必须走 walk-forward**：本脚本用 `weekly.scores.train`（生产同一个函数），
  只通过 `params_override` 换超参，不另写训练循环 —— 少一份实现就少一处漂移。

用法：
    ./.venv/Scripts/python.exe -X utf8 experiments/05_label/F47_weekly_hyperparams.py [--train-only]
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
BASE = HERE.parent.parent
sys.path.insert(0, str(BASE))


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


f1 = _load("f1", HERE / "F1_weekly_ic_decay.py")
f1b = _load("f1b", HERE / "F1b_weekly_open_overlap.py")
f17 = _load("f17", HERE / "F17_die_registry_kgd.py")
f22 = _load("f22", HERE / "F22_pair_final_selection.py")
f29 = _load("f29", HERE / "F29_tail_weighted_training.py")

from app import config as cfg_mod                 # noqa: E402
from app.factors import factor_engine as fe       # noqa: E402
from app.pit.engine import PITEngine              # noqa: E402
from app.weekly import scores as wsc              # noqa: E402

log = logging.getLogger("f47")
OUT = HERE / "F47_weekly_hyperparams_result.json"
FULL = ("2010-01-01", "2030-12-31")
DEAD = ("2020-09-01", "2023-12-31")
ACTIVE = ("2024-01-01", "2026-08-31")
SETS = {"SA": (42, 7, 123, 2024), "SB": (43, 8, 124, 2025)}
BASE_ARCHIVE = {"SA": "F4_scores_seedA.npz", "SB": "F4_scores_seedB.npz"}
W = 1.0
BASE_REF = {"full": [18.00, 17.30], "active": [10.38, 11.80], "n_common": 491}

# 预注册网格（写死在这里；要加配置必须新开实验）
GRID = {
    "C1_small": {"params": {"num_leaves": 15, "min_data_in_leaf": 500}, "rounds": 50},
    "C2_mid": {"params": {"num_leaves": 31, "min_data_in_leaf": 500, "learning_rate": 0.03,
                          "feature_fraction": 0.6, "lambda_l2": 5.0}, "rounds": 100},
    "C3_strong": {"params": {"num_leaves": 15, "min_data_in_leaf": 1000, "learning_rate": 0.03,
                             "feature_fraction": 0.5, "bagging_fraction": 0.7,
                             "lambda_l2": 10.0}, "rounds": 150},
}


def _fp(tag: str, set_name: str, cfg_item: dict, panel) -> str:
    payload = {
        "script": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:16],
        "tag": tag, "set": set_name, "seeds": list(SETS[set_name]),
        "params": {k: str(v) for k, v in cfg_item["params"].items()},
        "rounds": cfg_item["rounds"],
        "horizon": wsc.HORIZON, "months": wsc.TRAIN_MONTHS, "embargo": wsc.EMBARGO,
        "train_src": hashlib.sha256(
            __import__("inspect").getsource(wsc.train).encode()).hexdigest()[:16],
        "panel": [str(panel["anchors"][0]), str(panel["anchors"][-1]),
                  len(panel["anchors"]), len(panel["cols"])],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()[:16]


def train_cfg(panel, cfg, set_name, tag, item) -> pd.DataFrame:
    """用生产训练函数 + params_override 训一个配置；指纹一致才复用（绝不静默用旧分）。"""
    p = HERE / f"F47_scores_{set_name}_{tag}.npz"
    fp = _fp(tag, set_name, item, panel)
    if p.exists():
        z = np.load(p, allow_pickle=True)
        if "fp" in z and str(z["fp"]) == fp:
            log.info("  [%s|%s] 复用已训存档 (指纹 %s)", set_name, tag, fp)
            return pd.DataFrame(z["scores"], index=pd.to_datetime([str(d) for d in z["anchors"]]),
                                columns=[str(c) for c in z["cols"]])
        log.info("  [%s|%s] 指纹变了 ⇒ 重训", set_name, tag)
    t0 = time.time()
    S = wsc.train(panel, cfg, seeds=SETS[set_name], rounds=item["rounds"],
                  params_override=item["params"])
    np.savez_compressed(p, scores=S.astype(np.float32),
                        anchors=np.array([str(a) for a in panel["anchors"]]),
                        cols=np.array([str(c) for c in panel["cols"]]),
                        fp=fp, tag=tag, set_name=set_name)
    log.info("  [%s|%s] 训练完成 %.0fs → %s", set_name, tag, time.time() - t0, p.name)
    return pd.DataFrame(S, index=panel["anchors"], columns=panel["cols"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--train-only", action="store_true", help="只训练并落盘, 不做组合层评估")
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
    panel = wsc.build_panel(m, wk, cfg)          # 与生产同源
    log.info("面板: %s", panel["F"].shape)
    res = {"script_sha": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:16],
           "grid": {k: v for k, v in GRID.items()},
           "frozen": {"train_months": wsc.TRAIN_MONTHS, "embargo": wsc.EMBARGO,
                      "horizon": wsc.HORIZON, "sets": {k: list(v) for k, v in SETS.items()},
                      "equity_weight": W},
           "windows": {"full": FULL, "dead": DEAD, "active": ACTIVE},
           "base_reference": BASE_REF, "arms": {}, "increment": {}, "verdict": {}, "secs": None}

    mats = {}          # (set, tag) -> score DataFrame(anchors × cols)
    for set_name in SETS:
        z = np.load(HERE / BASE_ARCHIVE[set_name], allow_pickle=True)
        mats[(set_name, "BASE")] = pd.DataFrame(
            z["scores"], index=pd.to_datetime([str(d) for d in z["anchors"]]),
            columns=[str(c) for c in z["cols"]]).reindex(index=wk, columns=cols)
        for tag, item in GRID.items():
            mats[(set_name, tag)] = train_cfg(panel, cfg, set_name, tag, item) \
                .reindex(index=wk, columns=cols)
    if a.train_only:
        log.info("--train-only: 训练完成，跳过组合层评估")
        return

    mask = f1.tradable_frame(pit, wk, cols)
    mask_np = mask.fillna(False).to_numpy()
    op_adj = (m["open"] * m["adj"]).reindex(index=dates, columns=cols).astype(np.float64).to_numpy()
    pos_of = {d: i for i, d in enumerate(dates)}
    conn = sqlite3.connect(f"file:{BASE / 'data' / 'neweco.db'}?mode=ro", uri=True)
    PITSC = f17.table_scores(conn, "model_score_die_pit", wk, cols)
    conn.close()
    c = {**cfg, "capital": f29.CAPITAL,
         "regime_equity_weight": {"aggressive": {"attack": W, "neutral": W, "defense": W}}}
    series, common = {}, None
    for (set_name, tag), W5 in mats.items():
        Sdf = pd.DataFrame(f22.wr((W5, 2), (PITSC, 1)), index=wk, columns=cols)
        s = f29._run_series(m, wk, mask, pit, c, pos_of, mask_np, op_adj, Sdf)
        series[(set_name, tag)] = s
        ds = set(pd.to_datetime(s.loc[s["ret"].notna() & s["bench_ret"].notna(), "date"]))
        common = ds if common is None else (common & ds)
    common = sorted(common or [])
    log.info("同窗口对齐: %d 期", len(common))
    for (set_name, tag), s in series.items():
        full = f29._aligned_stats(s, common)
        dead = f29._zone(s, *DEAD)
        active = f29._zone(s, *ACTIVE)
        res["arms"].setdefault(set_name, {})[tag] = {"full": full, "dead": dead, "active": active}
        log.info("[%s|%s] 全史 %spp (MDD %.1f%%) | DEAD %spp | ACTIVE %spp",
                 set_name, tag, full.get("excess_pp"), 100 * full.get("mdd", float("nan")),
                 dead.get("excess_pp"), active.get("excess_pp"))

    ctrl = {}
    for i, set_name in enumerate(SETS):
        b = res["arms"][set_name]["BASE"]
        ctrl[set_name] = {"full_was": BASE_REF["full"][i], "full_now": b["full"].get("excess_pp"),
                          "active_was": BASE_REF["active"][i],
                          "active_now": b["active"].get("excess_pp")}
        ctrl[set_name]["ok"] = (abs(float(b["full"].get("excess_pp")) - BASE_REF["full"][i]) < 0.05
                                and abs(float(b["active"].get("excess_pp"))
                                        - BASE_REF["active"][i]) < 0.05)
    ctrl_ok = all(v["ok"] for v in ctrl.values()) and len(common) == BASE_REF["n_common"]
    res["control"] = {"ok": ctrl_ok, "detail": ctrl, "n_common": len(common)}
    log.info("对照(BASE 复现 F41 / 窗口 %d): %s", len(common), "通过" if ctrl_ok else "**失败**")

    ranking = []
    for tag in GRID:
        ok = True
        for set_name in SETS:
            b, x = res["arms"][set_name]["BASE"], res["arms"][set_name][tag]
            if not (x["full"].get("excess_pp") is not None
                    and x["full"]["excess_pp"] >= b["full"]["excess_pp"] + 1.0
                    and x["active"].get("excess_pp") is not None
                    and x["active"]["excess_pp"] >= b["active"]["excess_pp"]
                    and x["full"].get("mdd") is not None
                    and x["full"]["mdd"] >= b["full"]["mdd"] * 1.10):
                ok = False
        mean_full = float(np.mean([res["arms"][s_][tag]["full"].get("excess_pp") or -99
                                   for s_ in SETS]))
        ranking.append({"tag": tag, "passed": bool(ok), "mean_full_excess_pp": round(mean_full, 2)})
        log.info("[判决|%s] 通过=%s | 两套种子全史均值 %.2fpp", tag, ok, mean_full)
    passed = [r for r in ranking if r["passed"]]
    passed.sort(key=lambda r: -r["mean_full_excess_pp"])
    res["verdict"] = {
        "control_ok": ctrl_ok,
        "adopt": (passed[0]["tag"] if passed and ctrl_ok else None),
        "ranking": ranking,
        "criteria": "两套种子同时: ①全史 ≥ BASE+1.0pp ②ACTIVE ≥ BASE ③全史 MDD ≥ BASE×1.10; "
                    "④对照须复现; 多个通过 ⇒ 取全史均值最高(事前定)",
        "note": "3 个点是同一段 491 期上的多重比较; 判据与两套种子要求就是为此设的。"
                "全部不过 ⇒ 超参不是这根杠杆, 不再扩大网格"}
    res["secs"] = round(time.time() - t0, 1)
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    log.info("== F47 判决: 采纳=%s (对照=%s) | %.1fs → %s ==",
             res["verdict"]["adopt"], ctrl_ok, res["secs"], OUT)


if __name__ == "__main__":
    main()
