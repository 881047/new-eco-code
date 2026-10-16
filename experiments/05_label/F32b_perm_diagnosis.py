"""F32b · 置换检验失败的**判别诊断**：是"静态风格暴露"还是"动态泄漏"？

## 背景

F32 的置换检验**没通过**（PERM IC +0.0178, t=7.20；判据 |IC|<0.010）。按预注册这等于
"停止上线、按最坏假设排查"。但在动刀之前必须先分清两种**性质完全不同**的解释：

    (A) **静态风格暴露**：随机标签训出来的模型仍然带着一个"特征区域倾斜"（例如偏向小盘/
        低流动性），而这段历史上该倾斜恰好有正收益 ⇒ 残余 IC 是**风格暴露 × 风格收益**，
        **不是**前视。这种情况下组合层面的表现也应当接近"纯风格"（F24 的归因会显示
        风格贡献占满、残差≈0）。
    (B) **动态泄漏**：特征/票池/标签构造里混进了未来信息 ⇒ 残余 IC 里含**真正的选股信息**。
        这种情况下把两侧都做**横截面风格中性化**之后，IC 仍然显著。

判据（**诊断，不是采纳判据**）：

    · 若 `IC_风格中性 ≈ 0`（|IC| < 0.008 且 |t| < 3） ⇒ 归因于 (A)，置换检验的结论应改写为
      "无动态前视；残余是静态风格暴露"，并**另加一条**风格中性的置换判据进 §6.3；
    · 若 `IC_风格中性` 仍显著 ⇒ 认定 (B)，**停止上线**并逐项排查（标签构造/票池/因子面板/成交）。

做法（每个锚点一次横截面回归）：把**标签**与**模型打分**都对同一组暴露（9 风格 + 申万一级
PIT 行业哑变量 + 缺失指示）回归取残差，再算残差之间的秩相关。风格暴露定义直接复用
**生产模块** `app/weekly/attrib.py`（不写第二份）。

用法：
    ./.venv/Scripts/python.exe -X utf8 experiments/05_label/F32b_perm_diagnosis.py
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
f1b = f29.f1b

from app import config as cfg_mod             # noqa: E402
from app.factors import factor_engine as fe   # noqa: E402
from app.factors.holder_factors import sw_industry_map_pit  # noqa: E402
from app.pit.engine import PITEngine          # noqa: E402
from app.weekly import attrib as wk_attrib    # noqa: E402   ← 风格暴露只用生产定义

log = logging.getLogger("f32b")
OUT = HERE / "F32b_perm_diagnosis_result.json"
MIN_NAMES = 200


def _residualize(y: np.ndarray, A: np.ndarray, ok: np.ndarray) -> np.ndarray:
    """对暴露矩阵 A 取残差(带截距的等价写法: y 先去均值, A 的列已中心化)。"""
    out = np.full_like(y, np.nan, dtype=np.float64)
    if ok.sum() < MIN_NAMES:
        return out
    beta, *_ = np.linalg.lstsq(A[ok], y[ok] - float(np.mean(y[ok])), rcond=None)
    out[ok] = y[ok] - float(np.mean(y[ok])) - A[ok] @ beta
    return out


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
    lab = f1b.forward_excess_open(m, mask, 5).to_numpy(dtype=np.float64)
    styles_np = {k: v.to_numpy(dtype=np.float64)
                 for k, v in wk_attrib.style_panels(m, wk, cols).items()}
    ind_np = sw_industry_map_pit(wk, "L1").reindex(index=wk, columns=cols).to_numpy(object)
    log.info("风格面板就绪 | 锚点 %d", len(wk))

    def load_scores(fn):
        z = np.load(HERE / fn, allow_pickle=True)
        return pd.DataFrame(np.asarray(z["scores"], dtype=np.float32),
                            index=pd.to_datetime([str(d) for d in z["anchors"]]),
                            columns=[str(c) for c in z["cols"]]).reindex(
                                index=wk, columns=cols).to_numpy(dtype=np.float64)

    arms = {"REAL": load_scores("F29_scores_SA_A0_base.npz"),
            "PERM": load_scores("F32_scores_SA_PERM.npz")}
    res = {"script_sha": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:16],
           "arms": {}, "secs": None}
    for tag, P in arms.items():
        raw_ic, neu_ic, r2 = [], [], []
        for k in range(len(wk)):
            y = lab[k]
            ok = mask.to_numpy()[k] & np.isfinite(y) & np.isfinite(P[k])
            if ok.sum() < MIN_NAMES:
                continue
            reg_ok = ok & np.all([np.isfinite(styles_np[n][k]) for n in
                                  wk_attrib.CORE_STYLES], axis=0)
            d = wk_attrib._design(k, styles_np, ind_np, reg_ok)   # 生产模块的签名无 score_np
            if d is None:
                continue
            names, A = d
            ok2 = reg_ok & ok
            if ok2.sum() < MIN_NAMES:
                continue
            y_res = _residualize(y, A, ok2)
            p_res = _residualize(P[k], A, ok2)
            sub = ok2 & np.isfinite(y_res) & np.isfinite(p_res)
            if sub.sum() < MIN_NAMES:
                continue
            raw_ic.append(pd.Series(P[k][sub]).rank().corr(pd.Series(y[sub]).rank()))
            neu_ic.append(pd.Series(p_res[sub]).rank().corr(pd.Series(y_res[sub]).rank()))
            # 打分本身有多少能被风格解释
            ss = float(np.var(P[k][sub] - p_res[sub]) / max(1e-12, np.var(P[k][sub])))
            r2.append(ss)
        raw = pd.Series(raw_ic).dropna()
        neu = pd.Series(neu_ic).dropna()
        res["arms"][tag] = {
            "n": int(len(neu)),
            "ic_raw": round(float(raw.mean()), 4),
            "ic_raw_t": round(float(f1b.hac_t(raw.to_numpy(), 2)), 2),
            "ic_style_neutral": round(float(neu.mean()), 4),
            "ic_style_neutral_t": round(float(f1b.hac_t(neu.to_numpy(), 2)), 2),
            "score_style_r2": round(float(np.mean(r2)), 4)}
        a = res["arms"][tag]
        log.info("[%s] 原始 IC %+.4f (t=%.2f) | **风格中性 IC %+.4f (t=%.2f)** | 打分被风格解释 %.1f%%",
                 tag, a["ic_raw"], a["ic_raw_t"], a["ic_style_neutral"],
                 a["ic_style_neutral_t"], 100 * a["score_style_r2"])

    p = res["arms"]["PERM"]
    res["verdict"] = {
        "dynamic_leak": bool(abs(p["ic_style_neutral"]) >= 0.008
                             or abs(p["ic_style_neutral_t"]) >= 3.0),
        "note": ("风格中性后仍显著 ⇒ 动态泄漏, 停止上线并逐项排查"
                 if abs(p["ic_style_neutral"]) >= 0.008
                 or abs(p["ic_style_neutral_t"]) >= 3.0
                 else "风格中性后不显著 ⇒ 残余是**静态风格暴露**, 不是前视")}
    res["secs"] = round(time.time() - t0, 1)
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    log.info("== 判别: %s | %.1fs → %s ==", res["verdict"]["note"], res["secs"], OUT)


if __name__ == "__main__":
    main()
