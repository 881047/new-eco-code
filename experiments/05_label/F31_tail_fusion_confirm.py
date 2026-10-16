"""F31 · 尾部加权的**融合确认**（线上是 2×W5 + 1×pit，不是单 die）—— 预注册

## 为什么必须再走一步

F29 测的是**单个 W5 组件**。线上跑的是 `2×W5 + 1×pit`（F22）——而 F22 自己实测过**融合不单调**
（`W5+M`/`W5+head_vt`/三路各 1/3 都低于 2:1）。所以"单 die 变好"**不蕴含**"线上变好"，
必须把同一批分数按 2:1 与 pit 融合后再过一遍。

## 本实验的判据（**在看到任何融合数字之前冻结**）

对 F29 里"组合两轴不劣"的每个臂（候选由 F29 结果确定，不在本脚本里挑选），
在**两套种子上同时**：

    ① 全史（491 期同窗口）超额 ≥ 基线融合臂 **+1.0pp**
       （比 F29 的 +0.5pp 更严：这是对同一段历史的**第二次**检验，门槛必须提高）
    ② 留出集（2024-01~2026-08）超额 **不劣于**基线融合臂（≥ 基线）
    ③ MDD 不劣于基线融合臂 × 1.10

IC 只作诊断、不作判据 —— F29 已实测：尾部加权会移动"尾部拟合"而不移动全截面平均 IC
（判据③在 F29 里就是被 0.5% 的 IC 噪声卡住的），而组合只吃尾部。

## 诚实边界

- 这是**第二次**看同一段历史（F29 是第一次）⇒ 判据已按 ① 提高门槛；
- 数据、构型、费用、基准、窗口与 F28/F29 完全一致（同口径才可比）；
- 即便通过，落地仍是"换 W5 组件"这一件事，不改任何其它参数。

用法：
    ./.venv/Scripts/python.exe -X utf8 experiments/05_label/F31_tail_fusion_confirm.py
"""
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


f29 = _load("f29", HERE / "F29_tail_weighted_training.py")
f17 = _load("f17", HERE / "F17_die_registry_kgd.py")
f22 = _load("f22", HERE / "F22_pair_final_selection.py")
f1 = f29.f1

from app import config as cfg_mod             # noqa: E402
from app.factors import factor_engine as fe   # noqa: E402
from app.pit.engine import PITEngine          # noqa: E402

log = logging.getLogger("f31")
OUT = HERE / "F31_tail_fusion_confirm_result.json"
SETS = {"SA": (42, 7, 123, 2024), "SB": (43, 8, 124, 2025)}
CANDIDATES = ("A1_abs", "A3_abs2")      # 只确认这两个(其余两臂在 F29 里已被否)
FULL = ("2010-01-01", "2030-12-31")
OOS = ("2024-01-01", "2026-08-31")


def main():
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
    mask_np = mask.fillna(False).to_numpy()
    op_adj = (m["open"] * m["adj"]).reindex(index=dates, columns=cols).astype(np.float64).to_numpy()
    pos_of = {d: i for i, d in enumerate(dates)}
    conn = sqlite3.connect(f"file:{BASE/'data'/'neweco.db'}?mode=ro", uri=True)
    PITSC = f17.table_scores(conn, "model_score_die_pit", wk, cols)
    conn.close()

    def load_scores(set_name, arm):
        p = HERE / f"F29_scores_{set_name}_{arm}.npz"
        if not p.exists():
            raise FileNotFoundError(f"缺 {p.name}(先跑 F29)")
        z = np.load(p, allow_pickle=True)
        return pd.DataFrame(np.asarray(z["scores"], dtype=np.float32),
                            index=pd.to_datetime([str(d) for d in z["anchors"]]),
                            columns=[str(c) for c in z["cols"]]).reindex(index=wk, columns=cols)

    res = {"script_sha": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:16],
           "candidates": list(CANDIDATES), "sets": {k: list(v) for k, v in SETS.items()},
           "windows": {"full": FULL, "oos": OOS}, "arms": {}, "verdict": {}, "secs": None}
    common = None
    series = {}
    for set_name in SETS:
        W0 = load_scores(set_name, "A0_base")
        arms = {"A0_fused": pd.DataFrame(f22.wr((W0, 2), (PITSC, 1)), index=wk, columns=cols)}
        for arm in CANDIDATES:
            Wt = load_scores(set_name, arm)
            arms[f"{arm}_fused"] = pd.DataFrame(f22.wr((Wt, 2), (PITSC, 1)), index=wk, columns=cols)
        for tag, Sdf in arms.items():
            s = f29._run_series(m, wk, mask, pit, cfg, pos_of, mask_np, op_adj, Sdf)
            series[(set_name, tag)] = s
            ds = set(pd.to_datetime(s.loc[s["ret"].notna() & s["bench_ret"].notna(), "date"]))
            common = ds if common is None else (common & ds)
    common = sorted(common or [])
    log.info("同窗口对齐: %d 期 (%s ~ %s)", len(common),
             common[0].date() if common else "-", common[-1].date() if common else "-")
    for (set_name, tag), s in series.items():
        full = f29._aligned_stats(s, common)
        oos = f29._zone(s, *OOS)
        base = series[(set_name, "A0_fused")]
        d = (s.assign(date=pd.to_datetime(s["date"])).set_index("date")["ret"]
             - base.assign(date=pd.to_datetime(base["date"])).set_index("date")["ret"]).dropna()
        t = f29.f1b.hac_t(d.to_numpy(), 2) if len(d) > 10 else float("nan")
        res["arms"].setdefault(set_name, {})[tag] = {
            "full": full, "OOS": oos,
            "t_diff_vs_base": None if not np.isfinite(t) else round(float(t), 2)}
        log.info("[%s|%s] 全史 %+.2fpp (MDD %.1f%%) | 留出集 %+.2fpp | 差值配对 t=%s",
                 set_name, tag, full.get("excess_pp", float("nan")),
                 100 * full.get("mdd", float("nan")), oos.get("excess_pp", float("nan")),
                 res["arms"][set_name][tag]["t_diff_vs_base"])

    passed = []
    for arm in CANDIDATES:
        ok = True
        for set_name in SETS:
            b = res["arms"][set_name]["A0_fused"]
            c = res["arms"][set_name][f"{arm}_fused"]
            if not (c["full"]["excess_pp"] >= b["full"]["excess_pp"] + 1.0
                    and c["OOS"]["excess_pp"] >= b["OOS"]["excess_pp"]
                    and c["full"]["mdd"] >= b["full"]["mdd"] * 1.10):
                ok = False
        if ok:
            passed.append(arm)
    res["verdict"] = {"adopt_component": passed[0] if passed else None,
                      "candidates_passed": passed,
                      "criteria": "两套种子同时: ①全史≥基线+1.0pp ②留出集≥基线 ③MDD≥基线×1.10"}
    res["secs"] = round(time.time() - t0, 1)
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    log.info("== 融合确认: 采纳 W5 组件 = %s | %.1fs → %s ==",
             res["verdict"]["adopt_component"] or "维持 A0(不加尾部权重)", res["secs"], OUT)


if __name__ == "__main__":
    main()
