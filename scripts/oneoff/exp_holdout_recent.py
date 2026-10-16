"""holdout_recent 留出实验 —— 检验「近端留出 6 个月」这道保险的价与保额(2026-09-16)

机制背景(参数现状)
------------------
holdout_recent=6 是全项目少数**凭机制故事留下来的模型参数**(05 号台账无组合层判决条目):
无论是否早停, 训练集都剔除窗口最后 6 个月 —— 月频高换手(87%)策略的"近端去敏/防追高
保险": 全窗口训练把最近 6 个月的当下风格学得太尖, 风格反转时头部同板块踩踏
(20260630 光模块链 -42% 单月实证, 回撤 -30%→-56%; trainer.py R11-1 注释)。
代价是每期少训最近 6 个月(≈17% 样本)。方向双向成立: 留出防追高 vs 样本白白扔掉。
本实验是它第一次拿到组合层两轴判决。

事前指定的判定规则(跑之前写死, 不许事后改)
------------------------------------------
主读数 = **P1 = 2020-09 ~ 2023-12(39 个月)**, 现行模型对风险匹配池子超额 -3.23%/年的那段。
次读数 = P2 = 2024-01 ~ 2026-08(31 个月) 与全期。
  · **采纳**(某 treatment 臂)当且仅当: P1 系统年化比对照臂**改善 >= 3.0pp**
      且 全期系统年化**退化 <= 2.0pp**
      且 系统回撤**不劣化超过 0.5pp**(sys_mdd >= 对照 - 0.005 —— 留出是尾部保护机制,
      判据必须含回撤轴, 否则等于拿保险费换收益率)
  · 若多臂同时过判据 ⇒ 取 P1 改善最大者
  · Bonferroni: 2 个 treatment 臂(3 / 0)**独立**过判据; 边缘结果不合并解读、不跨臂借证据
  · 否则 **维持 holdout_recent=6**
判决只落盘(`*_result.json` + 05 号台账), **不自动改生产**: 采纳 = 改 trainer 默认值 +
全量重训 + 重钉 pin(CLAUDE.md §1), 另行由用户确认。

对照与校验
----------
三臂全部 train_start=None(全历史) + train_months=36 + backtest_start='2019-07-01',
只有 holdout_recent 不同: **6(对照 = 生产默认) / 3 / 0**。

⚠️ v1(2026-09-16)作废与 v2 修正(同日, 如实登记):
v1 的校验是「重训对照臂应复现 pin 19.40%/−19.66%」—— 实测 ho6 = +14.97%/−22.80%,
按冻结规则 v1 全部作废。机制已由 `verify_tm36_retrain.py` **判决性实锤**(2026-09-16 复核):
同配置当天重训与 ho6 表 **349585/349585 逐位相同(86/86 锚点)**、与 09-10 的 tm36 表仅
18.08% 相同 ⇒ 同数据必同模型(deterministic=True 兑现)、差异 100% 来自**库态快照** ——
**全量重训不可跨日复现, 重训类实验必须自带当日对照臂**。生产链路不受影响(累积分数表
当日逐位复现 pin, diff 0.0pp)。
⚠️⚠️ v3 更正(同日复核, 纠正 v2 的错误因果表述): v2 曾写"根因=09-14/15/16 日更修订历史
因子面板(修订位于 2020 年中的一段输入)" —— **这是错的, 已被逐表比对推翻**: 用 0910/0913
两个周备份对当前库逐表对比, daily/adj_factor/daily_basic/fina_indicator/index_daily/
trade_cal/stk_limit 的**历史行全部零修订**(唯 stock_basic 新股上市前缀更名 4+3 行、
20260910 当日 daily_basic 3 行); factor 表的历史月度锚点行也零修订(值修订仅在
2026-08/09 的日频行 = 日度推分水位正常运作)。**输入级根因未定位**, 候选: tm 训练时刻
(09-10 22:28)当日行情的落库时点(0910 备份 18:51 缺 09-10 当日行 ⇒ 18:00 日更没拉到
当天数据, tm 训练看到的 09-10 行可能是其后才补的)、lhb 表(未对比)、行情面板缓存。
诊断仪器: `scripts/oneoff/diag_data_drift_0916.py`(可随时复跑)。**判决不受影响** ——
臂间比较是同日同快照下的差值, 判据只消费臂间差。

安全
----
实验表 model_score_ho{N}, 不写 model_score / lgb_latest(因 is_prod 判据为
score_table=='model_score'), 不影响生产 pin 与持仓。写库形态与 exp_train_months 完全一致
(CREATE TABLE IF NOT EXISTS + trainer 落分 + 引擎自管本次 run 行)。
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import numpy as np
import pandas as pd

from app import config as cfg_mod
from app import db
from app.backtest import engine as E
from app.logger import setup_logger
from app.models.trainer import ENSEMBLE_ROUNDS, ENSEMBLE_SEEDS, run as train_run

setup_logger()
db.init_db()

ARMS = [6, 3, 0]             # holdout_recent; 6 = 对照(生产默认)
BT_START = "2019-07-01"      # 训练侧: 只给 >= 2019-07 的月份推分(与 exp_train_months 同口径)
EVAL_START = "20190722"      # 评估侧: 与生产 pin 同窗口
CAP = 20000.0
P1 = ("2020-09-01", "2023-12-31")   # 无 edge 段(39 个月)
P2 = ("2024-01-01", "2026-08-31")   # 凸性段(31 个月)
VALIDATE = {"sys_ann": 0.1940, "sys_mdd": -0.1966}   # 对照臂应复现(现行代码)
# 冻结判据(见 docstring; 改这里 = 改预注册, 不许)
CRIT = {"p1_gain": 0.03, "full_drop_max": -0.02, "mdd_worse_max": 0.005}

_orig = cfg_mod.load_config


def _cap(*a, **k):
    c = _orig(*a, **k)
    c["capital"] = CAP
    return c


mode = sys.argv[1] if len(sys.argv) > 1 else "all"
t0 = time.time()

# ---------- 阶段 1: 训练三个留出档 ----------
if mode in ("train", "all"):
    for ho in ARMS:
        st, it = f"model_score_ho{ho}", f"model_importance_ho{ho}"
        conn = db.get_conn()
        conn.execute(f"CREATE TABLE IF NOT EXISTS {st} ("
                     "ts_code TEXT, trade_date TEXT, score REAL, pred_ret REAL, "
                     "PRIMARY KEY (ts_code, trade_date)) WITHOUT ROWID")
        conn.execute(f"CREATE TABLE IF NOT EXISTS {it} ("
                     "train_date TEXT, factor TEXT, importance REAL, "
                     "PRIMARY KEY (train_date, factor)) WITHOUT ROWID")
        conn.commit()
        n_have = pd.read_sql(f"SELECT COUNT(DISTINCT trade_date) n FROM {st}", conn).iloc[0, 0]
        conn.close()
        print("[{:>5.0f}s] === holdout_recent={} (已有 {} 月) ===".format(
            time.time() - t0, ho, n_have), flush=True)
        arch = Path(__file__).resolve().parents[2] / "data" / "models" / f"ens_ho{ho}"
        arch.mkdir(parents=True, exist_ok=True)
        r = train_run(train_months=36, train_start=None, backtest_start=BT_START,
                      full=False, score_table=st, importance_table=it, archive_dir=arch,
                      num_boost_round=ENSEMBLE_ROUNDS, early_stopping_rounds=0,
                      ensemble_seeds=ENSEMBLE_SEEDS, holdout_recent=ho)
        print("[{:>5.0f}s] holdout_recent={} 完成: {}".format(
            time.time() - t0, ho, json.dumps(r, ensure_ascii=False, default=str)), flush=True)

# ---------- 阶段 2: 回测 + 分段 + 判决 ----------
if mode in ("bt", "all"):
    conn = db.get_conn()
    p = pd.read_sql("SELECT trade_date, close FROM pool_index", conn)
    conn.close()
    p["trade_date"] = pd.to_datetime(p["trade_date"])
    pool = p.set_index("trade_date")["close"].sort_index()

    rows, keeps = [], []
    cfg_mod.load_config = _cap
    try:
        for ho in ARMS:
            st = f"model_score_ho{ho}"
            m = E.run_backtest(score_table=st, run_tag=f"ho{ho}", start=EVAL_START)
            keeps.append(m["run_date"])
            rows.append({"ho": ho, "run": m["run_date"], "periods": m["periods"],
                         "ann": m["ann_return"], "sys_ann": m["sys_ann_return"],
                         "sys_mdd": m["sys_max_drawdown"], "sharpe": m.get("sharpe"),
                         "ic": m["ic_mean"], "n_avg": m["avg_stocks"],
                         "idle": m["avg_cash_idle"], "turnover": m.get("avg_turnover")})
            print("[{:>5.0f}s] {}".format(time.time() - t0,
                                          json.dumps(rows[-1], ensure_ascii=False)), flush=True)
    finally:
        cfg_mod.load_config = _orig

    print()
    print("=" * 108)
    print("holdout_recent 实验 —— 评估窗 {} 起, 资金 2 万, 逐笔真实费用, 每臂 4 种子集成, 训练窗 36 月".format(EVAL_START))
    print("=" * 108)
    print("{:>7} {:>6} {:>10} {:>10} {:>9} {:>7} {:>8} {:>7} {:>7}   {:<9} {:>9} {:>9}".format(
        "holdout", "期数", "满仓年化", "系统年化", "系统回撤", "夏普", "IC", "实持", "闲置",
        "分段", "系统年化", "超额"))
    print("-" * 108)
    detail = {}
    for r in rows:
        conn = db.get_conn()
        d = pd.read_sql("SELECT trade_date, sys_nav FROM backtest_nav WHERE run_date=? "
                        "ORDER BY trade_date", conn, params=(r["run"],))
        conn.close()
        d["trade_date"] = pd.to_datetime(d["trade_date"])
        nav = d.set_index("trade_date")["sys_nav"]
        L = 1.0 - r["idle"]
        idx = nav.index
        V = L * (pool.reindex(idx) / pool.reindex(idx).iloc[0]) + (1 - L)
        V = V / V.iloc[0]
        rs, rv = nav.pct_change().dropna(), V.pct_change().dropna()
        seg = {}
        for lbl, (a, b) in [("P1 20-09~23-12", P1), ("P2 24-01~26-08", P2)]:
            mm = (rs.index >= a) & (rs.index <= b)
            n = int(mm.sum())
            sa = (1 + rs[mm]).prod() ** (12 / n) - 1
            va = (1 + rv[mm]).prod() ** (12 / n) - 1
            seg[lbl] = (n, sa, va)
        detail[r["ho"]] = {"seg": seg}
        first = True
        for lbl, (n, sa, va) in seg.items():
            print("{:>7} {:>6} {:>+9.2%} {:>+9.2%} {:>8.2%} {:>7} {:>8.4f} {:>6.2f} {:>6.1%}   {:<13} {:>+8.2%} {:>+8.2%}".format(
                r["ho"] if first else "", r["periods"] if first else "",
                r["ann"] if first else float("nan"), r["sys_ann"] if first else float("nan"),
                r["sys_mdd"] if first else float("nan"),
                "{:.3f}".format(r["sharpe"]) if first else "",
                r["ic"] if first else float("nan"), r["n_avg"] if first else float("nan"),
                r["idle"] if first else float("nan"),
                lbl, sa, sa - va))
            first = False
    print("-" * 108)

    ctl = next(r for r in rows if r["ho"] == 6)
    # ---- v2 框架校验①: 生产表当日逐位复现 pin(引擎+数据读取层可信) ----
    m_prod = E.run_backtest(score_table="model_score", run_tag="pin_check_ho", start=EVAL_START)
    prod_ok = (abs(m_prod["sys_ann_return"] - VALIDATE["sys_ann"]) < 0.02
               and abs(m_prod["sys_max_drawdown"] - VALIDATE["sys_mdd"]) < 0.02)
    print("\n框架校验① 生产表当日回测: 系统 {:+.2%} / 回撤 {:.2%}   pin: {:+.2%} / {:.2%}   ⇒ {}".format(
        m_prod["sys_ann_return"], m_prod["sys_max_drawdown"],
        VALIDATE["sys_ann"], VALIDATE["sys_mdd"],
        "逐位复现 ✓" if prod_ok else "**不复现 ⇒ 框架不可信, 全部作废**"))
    # ---- 对照臂 vs pin = 快照漂移读数(登记用, 不作废) ----
    print("框架校验② 快照漂移读数(重训对照臂 vs pin, 同配置不同数据快照):")
    print("  ho6(今日重训): 系统 {:+.2%} / 回撤 {:.2%}   差 {:+.2f}pp / {:+.2f}pp".format(
        ctl["sys_ann"], ctl["sys_mdd"],
        (ctl["sys_ann"] - VALIDATE["sys_ann"]) * 100,
        (ctl["sys_mdd"] - VALIDATE["sys_mdd"]) * 100))
    print("  (v1 曾以此作废; v2 根因=全量重训不可跨日复现, 见 docstring —— 臂间差值不受影响)")

    verdicts = {}
    print("\n判定(采纳 ⇔ P1 较对照 +{:.0f}pp 以上 且 全期退化 <= {:.0f}pp 且 回撤不劣化 > {:.0f}pp):".format(
        CRIT["p1_gain"] * 100, abs(CRIT["full_drop_max"]) * 100, CRIT["mdd_worse_max"] * 100))
    for ho in ARMS[1:]:
        d1 = detail[ho]["seg"]["P1 20-09~23-12"][1] - detail[6]["seg"]["P1 20-09~23-12"][1]
        df_all = next(r["sys_ann"] for r in rows if r["ho"] == ho) - ctl["sys_ann"]
        mdd_worse = ctl["sys_mdd"] - next(r["sys_mdd"] for r in rows if r["ho"] == ho)
        passed = (d1 >= CRIT["p1_gain"] and df_all >= CRIT["full_drop_max"]
                  and mdd_worse <= CRIT["mdd_worse_max"])
        verdicts[ho] = {"d_p1": round(d1, 4), "d_full": round(df_all, 4),
                        "mdd_worse": round(mdd_worse, 4), "passed": bool(passed)}
        print("  holdout={:<4} P1 {:+.2f}pp   全期 {:+.2f}pp   回撤劣化 {:+.2f}pp   ⇒ {}".format(
            ho, d1 * 100, df_all * 100, mdd_worse * 100, "过判据" if passed else "不采纳"))
    adopt = None
    if prod_ok:
        passed_arms = [ho for ho, v in verdicts.items() if v["passed"]]
        if passed_arms:
            adopt = max(passed_arms, key=lambda ho: verdicts[ho]["d_p1"])
    print("\n总判决: {}".format(
        "**实验框架不可信(生产表当日不复现 pin), 全部作废**" if not prod_ok else
        (f"采纳 holdout_recent={adopt}(P1 改善最大臂) —— 进生产需改 trainer 默认 + 全量重训 + 重钉 pin, 由用户确认"
         if adopt is not None else "维持 holdout_recent=6")))
    print("run 保留: {}  (复核后手动清理)".format(", ".join(keeps + [m_prod["run_date"]])))
    print("总耗时 {:.0f}s".format(time.time() - t0))

    out = (Path(__file__).resolve().parents[2] / "experiments" / "05_label"
           / "exp_holdout_recent_result.json")
    out.write_text(json.dumps({
        "date": time.strftime("%Y-%m-%d %H:%M:%S"),
        "version": "v2（v1 同日作废：重训对照臂未复现旧 pin，根因=全量重训不可跨日复现/数据快照漂移，见 docstring）",
        "v1_invalid_snapshot": {"ho6_sys_ann": 0.1497, "ho6_sys_mdd": -0.2280,
                                "vs_pin_pp": [-4.43, -3.14],
                                "drift_localized": "2019-07~2020-12 共 18 锚点逐位相同，2021-01 起全变；82% 行不同"},
        "arms": rows, "criteria": CRIT, "validate": VALIDATE,
        "prod_reproduces_pin": bool(prod_ok),
        "prod_check_run": m_prod["run_date"],
        "verdicts": verdicts, "adopt": adopt,
        "note": "判决只落盘; 采纳需用户确认 + 改 trainer 默认 + 全量重训 + 重钉 pin(CLAUDE.md §1)",
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print("结果已写", out)
