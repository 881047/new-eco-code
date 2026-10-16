"""F32 · 置换检验（无前视审计）—— 预注册

## 为什么做这一件

`plans/weekly-terminal-plan-20260912.md` §6.3 的工程验收里写着一条从来没执行过的判据：

    **无前视：置换检验（target permutation）失去预测能力**；PIT 抽样审计通过。

前视是这类系统最致命、也最难自查的缺陷（本项目历史上已踩到过多次：`hk_hold` 覆盖断崖、
7 日期裸 IC 探针、F20 的带宽退化……）。置换检验是最直接的一道闸：把**训练标签**在每个锚点
内部打乱（保持该锚点的标签分布与可交易集合不变，只切断"特征↔标签"的关联），
**若这样训出来的分数仍然有 IC 或组合超额，就说明链路上有泄漏**。

## 臂（同一面板 / 同参数 / 同种子集 SA=(42,7,123,2024) / horizon=5 / 月末重训 / 禁运 3）

| 臂 | 训练标签 | 期望 |
|---|---|---|
| `REAL` | 真实标签 | IC +0.085、组合超额 +9.5pp（F29 已测，直接复用存档） |
| `PERM` | 每个锚点内打乱 | **IC ≈ 0、组合超额 ≈ 0（或为负=只付成本）** |

## 预注册判据

    ✅ 通过 = `PERM` 臂同时满足：① |IC| < 0.010；② |HAC t| < 3.0；③ 组合全史超额 < +3.0pp
    ❌ 不通过 ⇒ **停止一切上线动作**，按最坏假设排查泄漏面（标签构造/票池/因子面板/成交）

对照臂 `REAL` 必须同时给出（否则这个检验是空洞的：如果连真实标签都没信号，
"置换后没信号"什么也证明不了）。

> 说明：本实验只做**审计**，不产生任何采纳/否决策略的结论；`PERM` 的分数不写生产表。

用法：
    ./.venv/Scripts/python.exe -X utf8 experiments/05_label/F32_permutation_audit.py
"""
import hashlib
import importlib.util
import json
import logging
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
f1 = f29.f1

from app import config as cfg_mod             # noqa: E402
from app.factors import factor_engine as fe   # noqa: E402
from app.weekly import scores as wsc          # noqa: E402

log = logging.getLogger("f32")
OUT = HERE / "F32_permutation_audit_result.json"
SEEDS = (42, 7, 123, 2024)          # 与 F29 的 SA 同种子集(对照臂直接用它的存档)
PERM_SEED = 20260913
CRIT = {"ic_abs_max": 0.010, "t_abs_max": 3.0, "excess_pp_max": 3.0}
FULL = ("2010-01-01", "2030-12-31")


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        datefmt="%H:%M:%S", force=True)
    t0 = time.time()
    cfg = cfg_mod.load_config()
    m = fe.load_market(since="20140101")
    dates = pd.to_datetime(m["close"].index)
    wk = f1.week_end_dates(dates)
    cols = list(m["close"].columns)
    panel = wsc.build_panel(m, wk, cfg)
    mask, pit = panel["mask"], panel["pit"]
    mask_np = mask.fillna(False).to_numpy()
    op_adj = (m["open"] * m["adj"]).reindex(index=dates, columns=cols).astype(np.float64).to_numpy()
    pos_of = {d: i for i, d in enumerate(dates)}
    log.info("面板 %s | 锚点 %d", panel["F"].shape, len(wk))

    lab = f29.f1b.forward_excess_open(m, mask, 5)
    Yr = f1._rank(lab)
    okv = lab.notna().to_numpy()

    def evaluate(tag, Sdf, scores_file):
        Xr = f1._rank(Sdf)
        ic = pd.Series(f1.row_ic(Xr, Yr, okv & np.isfinite(Xr)), index=wk).dropna()
        s = f29._run_series(m, wk, mask, pit, cfg, pos_of, mask_np, op_adj, Sdf)
        full = f29._zone(s, *FULL)
        return {"scores_file": scores_file, "ic": round(float(ic.mean()), 4),
                "ic_t": round(float(f29.f1b.hac_t(ic.to_numpy(), 2)), 2),
                "ic_n": int(len(ic)), "full": full}

    res = {"script_sha": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:16],
           "seeds": list(SEEDS), "permute_seed": PERM_SEED, "criteria": CRIT,
           "arms": {}, "secs": None}
    # 对照臂: 直接复用 F29 的 SA|A0 存档(同种子集/同面板/同参数)
    z = np.load(HERE / "F29_scores_SA_A0_base.npz", allow_pickle=True)
    real = pd.DataFrame(np.asarray(z["scores"], dtype=np.float32),
                        index=pd.to_datetime([str(d) for d in z["anchors"]]),
                        columns=[str(c) for c in z["cols"]]).reindex(index=wk, columns=cols)
    res["arms"]["REAL"] = evaluate("REAL", real, "F29_scores_SA_A0_base.npz")
    log.info("[REAL] IC %+.4f (t=%.2f) | 全史超额 %+.2fpp", res["arms"]["REAL"]["ic"],
             res["arms"]["REAL"]["ic_t"], res["arms"]["REAL"]["full"]["excess_pp"])

    p = HERE / "F32_scores_SA_PERM.npz"
    if p.exists():
        zz = np.load(p, allow_pickle=True)
        S = np.asarray(zz["scores"], dtype=np.float32)
        log.info("  [PERM] 复用已训练存档")
    else:
        S = wsc.train(panel, cfg, seeds=SEEDS, permute_labels=True, permute_seed=PERM_SEED,
                      progress=lambda i, n, s: log.info("  [PERM] %d/%d (%.0fs)", i, n, s))
        np.savez_compressed(p, scores=S, anchors=panel["anchors"].to_numpy(), cols=cols)
    perm = pd.DataFrame(S, index=wk, columns=cols)
    res["arms"]["PERM"] = evaluate("PERM", perm, p.name)
    log.info("[PERM] IC %+.4f (t=%.2f) | 全史超额 %+.2fpp", res["arms"]["PERM"]["ic"],
             res["arms"]["PERM"]["ic_t"], res["arms"]["PERM"]["full"]["excess_pp"])

    a = res["arms"]["PERM"]
    ok = (abs(a["ic"]) < CRIT["ic_abs_max"] and abs(a["ic_t"]) < CRIT["t_abs_max"]
          and a["full"]["excess_pp"] < CRIT["excess_pp_max"])
    res["verdict"] = {"no_lookahead_pass": bool(ok),
                      "real_arm_has_signal": bool(res["arms"]["REAL"]["ic"] > 0.02),
                      "note": "置换后仍显著 ⇒ 停止上线动作并按最坏假设排查泄漏"}
    res["secs"] = round(time.time() - t0, 1)
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    log.info("== 置换检验: 无前视=%s | PERM IC %+.4f (t=%.2f) 超额 %+.2fpp | 对照 REAL IC %+.4f | %.1fs → %s ==",
             ok, a["ic"], a["ic_t"], a["full"]["excess_pp"], res["arms"]["REAL"]["ic"],
             res["secs"], OUT)


if __name__ == "__main__":
    main()
