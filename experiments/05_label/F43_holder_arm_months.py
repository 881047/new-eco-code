"""F43 · 把 `holder_chg_4q`（股东户数变化）接进生产构型 —— 组合层两轴判决（预注册）

## 由来

F42 把"事件族在**月频（21 日）窗口**"这半边补上了，其中 F3 的 `--horizon 21` 跑出**本项目第一个
过门槛的非基础因子**：

    holder_chg_4q   死区 IC +0.0251 (t=3.80, n=170) | 活跃区 +0.0296 (t=3.54, n=134)
                    与基础通过因子 |ρ|=0.392（独立）

因子口径：`app/factors/holder_factors.py` —— 股东户数**减少**（筹码集中）取正号，
即"筹码集中 → 未来一个月跑赢"。这是 10 个免费数据族里**唯一**过统计门槛的信号
（F3 预注册的"路线成立"要求 ≥3 个新因子通过 ⇒ 路线仍判否，但**单因子值得过一次组合层**）。

## 本实验只做一件事

把该因子并按进线上融合分，看**钱**（不是看 IC）：

    臂 BASE : 2×W5 + 1×pit                      （现役线上构型）
    臂 ARM  : 2×W5 + 1×pit + 1×holder_chg_4q     （加一路，权重 1，**只测这一个配比**）

其余一律冻结：100 万 / top100 / 退出带 8× / 持仓加成 0.2σ / 两套生产种子 / 同一费用与撮合。

## 预注册判据（跑数前冻结，两轴）

    ① 全史（491 期同窗口）两套种子超额 ≥ BASE + **1.0pp**；
    ② ACTIVE = 留出集(2024-01~2026-08) 两套种子超额 ≥ BASE（不劣）；
    ③ 全史 MDD 两套种子不劣于 BASE × 1.10；
    ④ **对照**：BASE 臂必须复现 F41 已记录的满仓读数（全史 +18.00/+17.30、ACTIVE +10.38/+11.80）。
       复现不了 ⇒ 口径漂了，ARM 的读数一律不得解读。

       **2026-09-13 修正（跑完第一版才发现的判据设计错误，如实登记）**：第一版对照要求
       *全史*也要对上 —— 那是错的。`holder_chg_4q` 的锚点覆盖只有 **50.7%**（股东户数是
       季频披露、早年更稀），ARM 因此在早期锚点上拿不到分 ⇒ 四臂交集从 **491 期缩到 479 期**
       ⇒ BASE 在交集上的全史读数自然从 +18.00/+17.30 变成 +18.47/+17.76（**窗口变了，不是口径漂了**）。
       正确的对照口径是：**ACTIVE 段必须逐位一致**（那段 12 期缺失全在 2024 年之前，ACTIVE 不受影响），
       而全史只做**同窗口**的 ARM-vs-BASE 比较（本来就是同一个交集）。
       修正后判据不变、采纳门槛不变，只是把对照的**窗口口径**说准。
    三条全过 ⇒ 采纳（另开"换 pin + 重算方案"流程）；任一不过 ⇒ 写进证伪登记。

## ⚠ 敞口口径提示（容易踩）

**研究侧引擎读的是 `regime_equity_weight`，线上周频读的是 `weekly.equity_weight`** ——
两个键不同名。本脚本两臂都显式把 `regime_equity_weight` 设成 **1.0**，才能与线上现役满仓对齐。

## 诚实边界

- 这是**第 N+1 次**看那段已开封的留出集（F28/F41 之后）⇒ ACTIVE 栏只当**方向**，不产生新证据；
- 单因子过 IC 门槛 ≠ 组合能赚钱（F40 实证过）；反过来，本臂若**失败**也只否掉"这一种接法"，
  不等于否掉该因子（可能要靠更长持有期/单独建 die，不在本实验范围）；
- 21 日标签的重叠问题照旧：491 期不等于 491 个独立观测。

用法：
    ./.venv/Scripts/python.exe -X utf8 experiments/05_label/F43_holder_arm_months.py
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


f1 = _load("f1", HERE / "F1_weekly_ic_decay.py")
f1b = _load("f1b", HERE / "F1b_weekly_open_overlap.py")
f17 = _load("f17", HERE / "F17_die_registry_kgd.py")
f22 = _load("f22", HERE / "F22_pair_final_selection.py")
f29 = _load("f29", HERE / "F29_tail_weighted_training.py")

from app import config as cfg_mod                 # noqa: E402
from app import db                                # noqa: E402
from app.factors import factor_engine as fe       # noqa: E402
from app.factors import holder_factors            # noqa: E402
from app.pit.engine import PITEngine              # noqa: E402

log = logging.getLogger("f43")
OUT = HERE / "F43_holder_arm_months_result.json"
FULL = ("2010-01-01", "2030-12-31")
DEAD = ("2020-09-01", "2023-12-31")
ACTIVE = ("2024-01-01", "2026-08-31")
SEED_FILES = {"seedA": "F4_scores_seedA.npz", "seedB": "F4_scores_seedB.npz"}
W = 1.0                      # 线上现役敞口(满仓)
# F41 记录的满仓对照值。**全史那两个数只在 491 期窗口上成立**；本实验因为 ARM 缺早期锚点，
# 交集只有 479 期 ⇒ 全史栏不可直接对照（同窗口比较才是对的），ACTIVE 栏必须逐位一致。
BASE_REF = {"full_on_491": [18.00, 17.30], "active": [10.38, 11.80]}


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
    conn = sqlite3.connect(f"file:{BASE / 'data' / 'neweco.db'}?mode=ro", uri=True)
    PITSC = f17.table_scores(conn, "model_score_die_pit", wk, cols)
    conn.close()
    # 股东户数因子（生产实现，不写第二份）: 户数减少=筹码集中 取正号
    conn = db.get_conn()
    try:
        hz = dict(holder_factors.compute_holder_factors_safe(dates))["holder_chg_4q"]
    finally:
        conn.close()
    Hz = pd.DataFrame(hz).reindex(index=wk, columns=cols)
    cov = float(Hz.notna().to_numpy().mean())
    log.info("holder_chg_4q 面板就绪 | 锚点覆盖 %.1f%%", 100 * cov)

    c = {**cfg, "capital": f29.CAPITAL,
         "regime_equity_weight": {"aggressive": {"attack": W, "neutral": W, "defense": W}}}
    res = {"script_sha": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:16],
           "equity_weight": W, "factor": "holder_chg_4q", "coverage": round(cov, 4),
           "windows": {"full": FULL, "dead": DEAD, "active": ACTIVE},
           "base_reference": BASE_REF, "arms": {}, "increment": {}, "verdict": {}, "secs": None}
    series, common = {}, None
    for seed, fn in SEED_FILES.items():
        z = np.load(HERE / fn, allow_pickle=True)
        W5 = pd.DataFrame(z["scores"],
                          index=pd.to_datetime([str(d) for d in z["anchors"]]),
                          columns=[str(c_) for c_ in z["cols"]]).reindex(index=wk, columns=cols)
        arms = {"BASE": pd.DataFrame(f22.wr((W5, 2), (PITSC, 1)), index=wk, columns=cols),
                "ARM": pd.DataFrame(f22.wr((W5, 2), (PITSC, 1), (Hz, 1)), index=wk, columns=cols)}
        for tag, Sdf in arms.items():
            s = f29._run_series(m, wk, mask, pit, c, pos_of, mask_np, op_adj, Sdf)
            series[(seed, tag)] = s
            ds = set(pd.to_datetime(s.loc[s["ret"].notna() & s["bench_ret"].notna(), "date"]))
            common = ds if common is None else (common & ds)     # 四臂取**同一段**历史才可比
    common = sorted(common or [])
    log.info("同窗口对齐: %d 期", len(common))
    for (seed, tag), s in series.items():
        full = f29._aligned_stats(s, common)
        dead = f29._zone(s, *DEAD)
        active = f29._zone(s, *ACTIVE)
        res["arms"].setdefault(seed, {})[tag] = {"full": full, "dead": dead, "active": active}
        log.info("[%s|%s] 全史 %spp (MDD %.1f%%, 换手 %s) | DEAD %spp | ACTIVE %spp (MDD %.1f%%)",
                 seed, tag, full.get("excess_pp"), 100 * full.get("mdd", float("nan")),
                 full.get("turnover"), dead.get("excess_pp"), active.get("excess_pp"),
                 100 * active.get("mdd", float("nan")))

    # 对照(修正后口径): **ACTIVE 段必须逐位一致**(那 12 期缺失全在 2024 之前, ACTIVE 不受影响);
    # 全史栏只报"窗口从 491 缩到 479"这件事, 不做跨窗口对照 —— 真正的全史比较是 ARM vs BASE 同窗口。
    ctrl = {}
    for i, seed in enumerate(SEED_FILES):
        b = res["arms"][seed]["BASE"]
        ctrl[seed] = {
            "active_was": BASE_REF["active"][i], "active_now": b["active"].get("excess_pp"),
            "full_on_491_was": BASE_REF["full_on_491"][i], "full_on_479_now": b["full"].get("excess_pp"),
            "window_shift_note": "全史差异来自窗口 491→479(ARM 早期锚点无 holder 因子), 非口径漂移"}
        ctrl[seed]["ok"] = abs(float(b["active"].get("excess_pp")) - BASE_REF["active"][i]) < 0.05
    ctrl_ok = all(v["ok"] for v in ctrl.values())
    res["control"] = {"ok": ctrl_ok, "detail": ctrl, "n_common": len(common),
                      "note": "对照口径修正(2026-09-13): ACTIVE 逐位一致即算通过; 全史不做跨窗口对照"}
    log.info("对照(ACTIVE 段复现 F41 满仓读数): %s | 共同窗口 %d 期", 
             "通过" if ctrl_ok else "**失败**", len(common))

    for seed in SEED_FILES:
        b, a = series[(seed, "BASE")], series[(seed, "ARM")]
        d = (a.assign(date=pd.to_datetime(a["date"])).set_index("date")["ret"]
             - b.assign(date=pd.to_datetime(b["date"])).set_index("date")["ret"]).dropna()
        act = d[(d.index >= ACTIVE[0]) & (d.index <= ACTIVE[1])]
        res["increment"][seed] = {
            "t_diff_full": round(float(f1b.hac_t(d.to_numpy(), 2)), 2) if len(d) > 10 else None,
            "active_mean_pp_per_period": round(100 * float(act.mean()), 3) if len(act) else None,
            "t_diff_active": (round(float(f1b.hac_t(act.to_numpy(), 2)), 2)
                              if len(act) > 10 else None)}
        log.info("[%s] ARM−BASE: 全史 t=%s | 留出集 每期 %+.3fpp (t=%s)", seed,
                 res["increment"][seed]["t_diff_full"],
                 res["increment"][seed]["active_mean_pp_per_period"] or 0.0,
                 res["increment"][seed]["t_diff_active"])

    passed = True
    for i, seed in enumerate(SEED_FILES):
        b, a = res["arms"][seed]["BASE"], res["arms"][seed]["ARM"]
        if not (a["full"].get("excess_pp") is not None
                and a["full"]["excess_pp"] >= b["full"]["excess_pp"] + 1.0
                and a["active"].get("excess_pp") is not None
                and a["active"]["excess_pp"] >= b["active"]["excess_pp"]
                and a["full"].get("mdd") is not None
                and a["full"]["mdd"] >= b["full"]["mdd"] * 1.10):
            passed = False
    res["verdict"] = {
        "control_ok": ctrl_ok,
        "adopt": bool(passed and ctrl_ok),
        "criteria": "两套种子同时: ①全史 ≥ BASE+1.0pp ②ACTIVE ≥ BASE ③全史 MDD ≥ BASE×1.10; ④对照须复现",
        "note": "ACTIVE 是已开封留出集, 只作方向; 失败只否掉这一种接法, 不等于否掉该因子"}
    res["secs"] = round(time.time() - t0, 1)
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    log.info("== F43 判决: 采纳=%s (对照=%s) | %.1fs → %s ==",
             res["verdict"]["adopt"], ctrl_ok, res["secs"], OUT)


if __name__ == "__main__":
    main()
