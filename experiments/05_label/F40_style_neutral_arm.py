"""F40 · 风格中性组合臂（F24 预注册过、却从没跑过的那条）—— 预注册

## 为什么要跑这一条（不是新点子，是**欠账**）

F24 的预注册写明：「若判决风格依赖 ⇒ 开 F25（**规模中性对照臂**）量纯选股还剩多少」。
但 F25 后来被拿去做了「权益权重刻度」，**这条对照臂从来没跑过**。

同时 F32b 留了一个必须兑现的线索：把打分对风格+行业取残差之后，

    真实模型  风格中性 IC = +0.0279 (t=12.81)
    置换模型  风格中性 IC = +0.0023 (t=1.84)

⇒ **打分里确实有风格之外的横截面信息**。问题是它**能不能变成钱**。本实验就是回答这个。

## 方法（跑数前冻结）

1. 打分 = **线上构型**的融合分 `2×W5 + 1×pit`（F22 判决定下来的那个），用 F29 的**两套种子集**
   `SA=(42,7,123,2024)` / `SB=(43,8,124,2025)`；

   **⚠ 命名陷阱（2026-09-13 核对时发现，写在这里免得下次再踩）**：仓库里有两套*不同*的种子对，
   名字很像但**不是同一个东西** ——

   - `seedA/seedB`（`F4_scores_seed*.npz`）= **生产的**两套种子，全史 +14.91 / **+14.30**。
     F21/F22/F23/F24/F28 用的是这一对；
   - `SA/SB`（`F29_scores_S{A,B}_A0_base.npz`）= F29 为尾部加权实验**重新训的**两组种子，
     全史 +14.91 / **+15.07**。`SA` 与生产 `seedA` 同种子元组 (42,7,123,2024) ⇒ 读数逐位相同；
     `SB` 不是生产 `seedB`。

   所以本实验的留出集读数是 `7.34 / 8.83`（F31 的 `A0_fused` 同口径、逐位一致），
   而 F28 报的 `7.34 / 8.62` 用的是 `seedA/seedB` —— 差异来自**种子集不同**，不是口径漂移。
   两条路线的结论方向一致（都远低于 C3 的 +10pp），本实验的判据因此不受影响。
2. 每个锚点做横截面回归，把分对 **9 个风格 + 申万一级 PIT 行业哑变量 + 缺失指示**取残差。
   暴露面板与**设计矩阵全部调生产模块** `app/weekly/attrib.py`（`style_panels` / `_design`），
   残差化写法与 F32b 逐字一致 —— 不写第二份口径；
3. 用残差当新打分，**其余一律不动**（同一 top100 / 带 8× / 加成 0.2σ / 同一成交与费用口径），
   走 `F29._run_series`（f1b 的唯一实现）；
4. 两轴读数（超额 + MDD），三个窗口：全史 / DEAD(2020-09~2023-12) / ACTIVE(2024-01~2026-08)。

两个候选臂（**预注册就这两个，不做参数扫描**）：

    NEUT_SIZE 只把 size 中性化（F24 原文承诺的那条）
    NEUT_ALL  全部 9 风格中性化（F32b 的线索指向的那条，**主臂**）

## 预注册判据（看到任何数字之前冻结）

目的**不是**"把全史做高" —— 剥掉风格，全史本来就该掉。要回答的是
**"不靠小盘 beta，这套打分还能不能赚钱"**，所以判据只压在 ACTIVE（最近的那段）：

    ① 两套种子 ACTIVE 超额 ≥ +10pp/年（§1.1 C3 那一关）**且** ≥ 同窗口 BASE 臂；
    ② 两套种子全史 MDD 不劣于 BASE × 1.10（与 F31 同一口径）；
    ③ **中性化真落到选股上**：中性臂"按分取前 100 只"等权篮子的**平均 |风格 z|**
       ≤ BASE 臂的 1/2。③ 不成立 ⇒ 本实验判**无效**，①②的数字一律不得引用。

全史超额下降**不算失败**，但必须如实记 —— 那是"小盘 beta 值多少钱"的直接读数。

## 诚实边界（多重比较）

- ACTIVE 段就是 F28 已经开封过的留出集 ⇒ 这是**又一次**看它。F38 已判 DSR 不通过，
  **本实验无论结果如何都不能把 ACTIVE 读数当新的显著性证据**，只能当"这条路要不要继续走"的方向判断；
- 预注册候选数 = 2。想加臂必须另开 F41 重新预注册，**不许在本脚本里加**；
- 数据、构型、费用、基准口径与 F28/F29/F31 完全一致（同口径才可比）。

用法：
    ./.venv/Scripts/python.exe -X utf8 experiments/05_label/F40_style_neutral_arm.py
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
f1 = f29.f1
f1b = f29.f1b
f17 = _load("f17", HERE / "F17_die_registry_kgd.py")
f22 = _load("f22", HERE / "F22_pair_final_selection.py")

from app import config as cfg_mod                        # noqa: E402
from app.factors import factor_engine as fe              # noqa: E402
from app.factors.holder_factors import sw_industry_map_pit  # noqa: E402
from app.pit.engine import PITEngine                     # noqa: E402
from app.weekly import attrib as wk_attrib               # noqa: E402  风格暴露只用生产定义

log = logging.getLogger("f40")
OUT = HERE / "F40_style_neutral_arm_result.json"
SETS = {"SA": "F29_scores_SA_A0_base.npz", "SB": "F29_scores_SB_A0_base.npz"}
FULL = ("2010-01-01", "2030-12-31")
DEAD = ("2020-09-01", "2023-12-31")
ACTIVE = ("2024-01-01", "2026-08-31")
MIN_NAMES = wk_attrib.MIN_NAMES
ARMS = ("BASE", "NEUT_SIZE", "NEUT_ALL")
ARMS_REGISTERED = ("NEUT_SIZE", "NEUT_ALL")     # 预注册候选数 = 2
ACTIVE_BAR = 10.0                               # C3: 年化超额 ≥ +10pp

# main() 里装好、供 normalize()/basket_style_z() 复用（这两个函数只为可读性拆出来，
# 不想把 5 个面板参数抄来抄去）。
wk = mask_np = styles_np = ind_np = None


def _resid(y: np.ndarray, A: np.ndarray, ok: np.ndarray) -> np.ndarray:
    """与 F32b 逐字同口径（带截距的等价写法: y 先去均值, A 的列已中心化）。"""
    out = np.full_like(y, np.nan, dtype=np.float64)
    if ok.sum() < MIN_NAMES:
        return out
    beta, *_ = np.linalg.lstsq(A[ok], y[ok] - float(np.mean(y[ok])), rcond=None)
    out[ok] = y[ok] - float(np.mean(y[ok])) - A[ok] @ beta
    return out


def normalize(Sdf: pd.DataFrame, only) -> tuple:
    """按 `only` 指定的风格取残差（"ALL" = 全部 9 个 + 行业）。

    返回 (新打分 DataFrame, 有效锚点数)。
    """
    S = Sdf.to_numpy(dtype=np.float64)
    out = np.full(S.shape, np.nan, dtype=np.float64)
    used = 0
    for k in range(len(wk)):
        y = S[k]
        ok = mask_np[k] & np.isfinite(y)
        if ok.sum() < MIN_NAMES:
            continue
        reg_ok = ok & np.all([np.isfinite(styles_np[n][k]) for n in
                              wk_attrib.CORE_STYLES], axis=0)
        d = wk_attrib._design(k, styles_np, ind_np, reg_ok)
        if d is None:
            continue
        names, A = d
        if only != "ALL":
            keep = [i for i, nm in enumerate(names) if nm in only]
            if not keep:
                continue
            A = A[:, keep]
        ok2 = reg_ok & ok
        if ok2.sum() < MIN_NAMES:
            continue
        out[k] = _resid(y, A, ok2)
        used += 1
    return pd.DataFrame(out, index=Sdf.index, columns=Sdf.columns), used


def basket_style_z(Sdf: pd.DataFrame) -> dict:
    """"按分取前 TOP_N 只"等权篮子相对票池的风格暴露（单位 σ）—— 判据 ③ 的读数。

    只看**新建篮子**（不含退出带/迟滞）: 带只影响留存, 不影响初始倾斜。数值越接近 0
    说明打分越不靠风格。BASE 臂应当明显不为 0（F24: 风格占总超额一半以上）。
    """
    S = Sdf.to_numpy(dtype=np.float64)
    per_style = {nm: [] for nm in wk_attrib.STYLES}
    for k in range(len(wk)):
        ok = mask_np[k] & np.isfinite(S[k])
        if ok.sum() < MIN_NAMES:
            continue
        order = np.argsort(-np.where(ok, S[k], -np.inf))[:f29.TOP_N]
        for nm in wk_attrib.STYLES:
            v = styles_np[nm][k]
            sel = v[order]
            sel = sel[np.isfinite(sel)]
            pool = v[ok]
            pool = pool[np.isfinite(pool)]
            if len(sel) < 5 or len(pool) < MIN_NAMES:
                continue
            sd = float(np.std(pool))
            if sd <= 1e-12:
                continue
            per_style[nm].append(float((np.mean(sel) - np.mean(pool)) / sd))
    mean = {nm: (round(float(np.mean(v)), 4) if v else None) for nm, v in per_style.items()}
    abs_vals = [abs(x) for x in mean.values() if x is not None]
    return {"per_style_z": mean,
            "mean_abs_z": round(float(np.mean(abs_vals)), 4) if abs_vals else None}


def main():
    global wk, mask_np, styles_np, ind_np
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
    conn = sqlite3.connect(f"file:{BASE / 'data' / 'neweco.db'}?mode=ro", uri=True)
    PITSC = f17.table_scores(conn, "model_score_die_pit", wk, cols)
    conn.close()
    styles_np = {k: v.to_numpy(dtype=np.float64)
                 for k, v in wk_attrib.style_panels(m, wk, cols).items()}
    ind_np = sw_industry_map_pit(wk, "L1").reindex(index=wk, columns=cols).to_numpy(object)
    log.info("面板就绪 | 锚点 %d | 票池 %d 只", len(wk), len(cols))

    def load_w5(fn: str) -> pd.DataFrame:
        z = np.load(HERE / fn, allow_pickle=True)
        return pd.DataFrame(np.asarray(z["scores"], dtype=np.float32),
                            index=pd.to_datetime([str(d) for d in z["anchors"]]),
                            columns=[str(c) for c in z["cols"]]).reindex(index=wk, columns=cols)

    res = {"script_sha": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:16],
           "arms_registered": list(ARMS_REGISTERED), "active_bar_pp": ACTIVE_BAR,
           "windows": {"full": FULL, "dead": DEAD, "active": ACTIVE},
           "arms": {}, "exposure": {}, "verdict": {}, "secs": None}
    common = None
    series = {}
    for set_name, fn in SETS.items():
        # 线上构型: 2×W5 + 1×pit（与 F31 同一写法, wr 返回的已是 wk×cols 的分位排名均值）
        fused = pd.DataFrame(f22.wr((load_w5(fn), 2), (PITSC, 1)), index=wk, columns=cols)
        arms = {"BASE": fused}
        arms["NEUT_SIZE"], n1 = normalize(fused, ("size",))
        arms["NEUT_ALL"], n2 = normalize(fused, "ALL")
        log.info("[%s] 中性化有效锚点: size=%d all=%d", set_name, n1, n2)
        for tag in ARMS:
            res["exposure"].setdefault(set_name, {})[tag] = basket_style_z(arms[tag])
            log.info("[%s|%s] 篮子平均|风格z| = %s", set_name, tag,
                     res["exposure"][set_name][tag]["mean_abs_z"])
        for tag in ARMS:
            s = f29._run_series(m, wk, mask, pit, cfg, pos_of, mask_np, op_adj, arms[tag])
            series[(set_name, tag)] = s
            ds = set(pd.to_datetime(s.loc[s["ret"].notna() & s["bench_ret"].notna(), "date"]))
            common = ds if common is None else (common & ds)
    common = sorted(common or [])
    log.info("同窗口对齐: %d 期", len(common))

    for (set_name, tag), s in series.items():
        full = f29._aligned_stats(s, common)
        dead = f29._zone(s, *DEAD)
        active = f29._zone(s, *ACTIVE)
        base = series[(set_name, "BASE")]
        d = (s.assign(date=pd.to_datetime(s["date"])).set_index("date")["ret"]
             - base.assign(date=pd.to_datetime(base["date"])).set_index("date")["ret"]).dropna()
        t = f29.f1b.hac_t(d.to_numpy(), 2) if len(d) > 10 else float("nan")
        res["arms"].setdefault(set_name, {})[tag] = {
            "full": full, "dead": dead, "active": active,
            "t_diff_vs_base": None if not np.isfinite(t) else round(float(t), 2)}
        log.info("[%s|%s] 全史 %spp (MDD %.1f%%) | DEAD %spp | ACTIVE %spp (MDD %.1f%%) | 差值 t=%s",
                 set_name, tag, full.get("excess_pp"), 100 * full.get("mdd", float("nan")),
                 dead.get("excess_pp"), active.get("excess_pp"),
                 100 * active.get("mdd", float("nan")),
                 res["arms"][set_name][tag]["t_diff_vs_base"])

    passed, exposure_ok = [], True
    for set_name in SETS:
        e = res["exposure"][set_name]
        if not (e["NEUT_ALL"]["mean_abs_z"] is not None
                and e["NEUT_ALL"]["mean_abs_z"] <= 0.5 * e["BASE"]["mean_abs_z"]):
            exposure_ok = False
    for arm in ARMS_REGISTERED:
        ok = True
        for set_name in SETS:
            b = res["arms"][set_name]["BASE"]
            c = res["arms"][set_name][arm]
            if not (c["active"].get("excess_pp") is not None
                    and c["active"]["excess_pp"] >= ACTIVE_BAR
                    and c["active"]["excess_pp"] >= b["active"]["excess_pp"]
                    and c["full"].get("mdd") is not None
                    and c["full"]["mdd"] >= b["full"]["mdd"] * 1.10):
                ok = False
        if ok:
            passed.append(arm)
    res["verdict"] = {
        "exposure_check_passed": exposure_ok,
        "candidates_passed": passed,
        "primary_arm": "NEUT_ALL",
        "criteria": "两套种子同时: ①ACTIVE ≥ +10pp 且 ≥ BASE ②全史 MDD ≥ BASE×1.10 ③篮子平均|风格z| ≤ BASE 的 1/2",
        "note": "ACTIVE 是 F28 已开封的留出集, 本读数**不是**新的显著性证据(F38 DSR 已不通过)",
    }
    res["secs"] = round(time.time() - t0, 1)
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    log.info("== F40: 通过=%s | 中性化落到位=%s | %.1fs → %s ==",
             passed or "无", exposure_ok, res["secs"], OUT)


if __name__ == "__main__":
    main()
