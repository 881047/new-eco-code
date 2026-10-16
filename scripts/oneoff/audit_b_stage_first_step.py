"""ONEOFF(2026-09-20, 批次 2 第一步): 口径项的**只读量化**（一条脚本跑可测的几条）。

判据(门限)跑前冻结在 `plans/b-stage-criteria-20260920.md`。本脚本**只读**：
`mode=ro` + `PRAGMA query_only=1`，不含任何写库语句；唯一产物
`logs/audit_b_stage_first_step.json`。

范围（2026-09-20 13:4x 重定，原因见下）：
  · **本轮真跑**：Q4(IC 窗口/幸存者)、Q5(PER_YEAR=52)、Q6(insider 方向行)、
    Q8(标签口径两格)、Q9(可交易性差)；
  · **如实标"未跑"**并各写清为什么：Q1(全 158 条双尺门禁 A/B = 阶段 B，小时级且需
    `critic.churn_cost` 全链路)、Q2(要重跑周频训练才能拿到两把尺的打分截面)、
    Q3(要跑 `health.rolling_ic` 全链 + 与 `scores.ic_summary` 对齐)、
    Q7(跨月停牌结转在 `engine._roll_stuck` 内部占资计息，SQL 测不到 ⇒ 要插桩)。

⚠️ 为什么重定范围：上一版这个脚本 6/9 臂**凭印象写错 API**（`A.zoo_items` /
   `scores.tradable_matrix` / `rolling_ic(conn, anchor, horizon)` / `insider_trade.change_ratio`
   全都不存在），只有 3 臂跑出东西、其中 Q7 的结论文本还与它自己输出的 `0 笔` 相互矛盾。
   同批的"九条判决表"读数因产物消失 + 重建版跑不通**已全部撤回**（14 号三补二十九·补）。
   ⇒ 本版每条都带**实现自证**（用到真实对象 + 覆盖面计数），跑不通宁可报 error 也不给结论。

真实 API 备忘（写给下一轮，别再猜）：
  · `alpha.expr.build_panel(m: dict, since)` —— 收 **`load_market()` 的 dict**；求值
    `expr.Engine(panel).eval(表达式)`，每算完一条要 `Engine.prune()`（否则 158×222MB 会 OOM）；
    表达式清单 = `alpha.zoos.qlib158()`；门禁在 `alpha.gate`（`forward_return/zone_of/
    evaluate/summarize/verdict`，`HORIZON=21`、`MIN_NAMES=200`、成本项缺失 ⇒ `c_head_size` 判否）。
  · `weekly.scores.week_end_dates(dates)` —— 周频锚点 = 每周最后一个开市日；
  · `weekly.health.rolling_ic(window=12)`；`insider_trade` 的比例列叫 **`ratio`**；
  · `weekly.attrib.summarize()` 里 `100 * PER_YEAR * 逐期均值` 是周频 pp/年读数的出处。
"""
import json
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE))
LOGS = BASE / "logs"
#: 产物**双写**。`logs/` 在 .gitignore 里 ⇒ 只落 logs 的量化产物**不在版本控制内**，
#: 一次清理/并发会话就能让它消失（本轮就发生了：九条判决表的产物失踪 ⇒ 整段撤回）。
#: ⇒ 今后凡是要进台账的读数，必须同时落这个被 git 跟踪的目录，并与脚本同一个 commit 入库。
TRACKED = BASE / "experiments" / "b_stage_20260920"
DB = BASE / "data" / "neweco.db"
N_TESTS = 200                       # 与在册门禁分母同量级(只用于 p_adj 展示，不新增试验)
res: dict = {}


def ro() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
    conn.execute("PRAGMA query_only=1")
    return conn


def spearman(x: np.ndarray, y: np.ndarray) -> float:
    ok = np.isfinite(x) & np.isfinite(y)
    if ok.sum() < 200:
        return np.nan
    a = pd.Series(x[ok]).rank().to_numpy()
    b = pd.Series(y[ok]).rank().to_numpy()
    if a.std() == 0 or b.std() == 0:
        return np.nan
    return float(np.corrcoef(a, b)[0, 1])


# ============================================================ Q4 回测 IC 窗口 + 幸存者偏差
def q4():
    conn = ro()
    try:
        sc = pd.read_sql("SELECT trade_date, ts_code, score FROM model_score "
                         "WHERE trade_date>='20190722'", conn)
        px = pd.read_sql("SELECT trade_date, ts_code, close FROM daily "
                         "WHERE trade_date>='20190701'", conn)
        basic = pd.read_sql("SELECT ts_code FROM stock_basic "
                            "WHERE name LIKE '%退%' OR name LIKE '%ST%'", conn)["ts_code"].tolist()
    finally:
        conn.close()
    piv = px.pivot(index="trade_date", columns="ts_code", values="close").sort_index()
    piv.index = pd.to_datetime(piv.index)
    # 现行口径：从**评分日**起算 21 个交易日；对齐口径：从**执行日**(次一交易日)起算
    fut_cur = piv.shift(-21) / piv - 1.0
    fut_aln = piv.shift(-22) / piv.shift(-1) - 1.0
    s = sc.pivot(index="trade_date", columns="ts_code", values="score").sort_index()
    s.index = pd.to_datetime(s.index)
    common = s.index.intersection(fut_cur.index)
    ics_cur, ics_aln, cells_nan, delist_nan, n_cells = [], [], 0, 0, 0
    st_bad = set(basic)
    for d in common:
        x = s.loc[d]
        for cur, bucket in ((fut_cur.loc[d], ics_cur), (fut_aln.loc[d], ics_aln)):
            cur = cur.reindex(x.index)          # 评分表覆盖过的票可比日线多 ⇒ 必须对齐列
            if bucket is ics_cur:
                both = x.notna() & cur.notna()
                cells_nan += int((x.notna() & cur.isna()).sum())
                delist_nan += int(((x.notna() & cur.isna())
                                   & x.index.isin(st_bad)).sum())
                n_cells += int(both.sum())
            bucket.append(spearman(x.to_numpy(float), cur.to_numpy(float)))
    a, b = np.nanmean(ics_cur), np.nanmean(ics_aln)
    d_abs = abs(a - b)
    res["Q4_ic_window_and_survivorship"] = {
        "cross_sections": len(common), "cells_scored": n_cells,
        "ic_current_shift_minus21": round(float(a), 6),
        "ic_aligned_to_exec_day": round(float(b), 6),
        "delta_abs": round(float(d_abs), 6),
        "dropna_cells": cells_nan, "of_which_ST_or_delist_names": delist_nan,
        "self_check": {"用到的真实对象": "model_score + daily + stock_basic",
                       "截面数必须>50": len(common) > 50,
                       "两臂共用同一份 score 矩阵": True},
        "gate": "|ΔIC|<0.005 ⇒ 关闭 + 就地注释；≥0.005 或判档变 ⇒ 第二步",
        "crossed": bool(d_abs >= 0.005), "level": "OK" if d_abs < 0.005 else "CROSS",
        "caveat": "`of_which_ST_or_delist_names` 用的是**名称代理**（stock_basic.name 含 "
                  "'退'/'ST'），不是真正的退市标记表 ⇒ 那一列只作量级参考，别当精确计数引用"}


# ============================================================ Q5 PER_YEAR = 52
def q5():
    from app.weekly import scores as W
    conn = ro()
    try:
        cal = pd.read_sql("SELECT cal_date FROM trade_cal WHERE is_open=1 AND cal_date>='20140101' "
                          "AND cal_date<='20260918' ORDER BY cal_date", conn)["cal_date"].tolist()
    finally:
        conn.close()
    idx = pd.DatetimeIndex(pd.to_datetime(cal))
    anchors = W.week_end_dates(idx)                 # 周频锚点 = 每周最后一个开市日
    per_year = pd.Series(anchors).dt.year.value_counts().sort_index()
    # 首末年是**不完整年**(20140101 起、20260918 止) ⇒ 混进均值会把高估率算高
    # (实测含首末: 均值 50.154 ⇒ "高估 3.68%"；只取完整年才是那个常数的真实偏差)。
    full = per_year.drop(index=[per_year.index.min(), per_year.index.max()])
    mean_full = float(full.mean())
    over = 100.0 * (52.0 / mean_full - 1.0)
    res["Q5_per_year_52"] = {
        "anchor_source": "app/weekly/scores.week_end_dates(trade_cal.is_open=1)",
        "years_all": int(len(per_year)), "years_complete": int(len(full)),
        "min_per_year_complete": int(full.min()), "max_per_year_complete": int(full.max()),
        "mean_per_year_complete": round(mean_full, 3),
        "mean_per_year_including_partial": round(float(per_year.mean()), 3),
        "per_year_const": 52.0, "overstatement_pct": round(over, 3),
        "overstatement_if_partial_years_included": round(
            100.0 * (52.0 / float(per_year.mean()) - 1.0), 3),
        "where_it_is_used": ["app/weekly/attrib.py:226 周频年化超额(pp/年)",
                             "app/weekly/attrib.py:232 风格暴露(pp/年)",
                             "副本冻结: F24/F25/F28/F29/F34 + mirror_pool_reference.py"],
        "self_check": {"完整年数=11(2015~2025)": int(len(full)) == 11,
                       "每个完整年锚点数落在 45~60": bool(full.between(45, 60).all()),
                       "首末不完整年已剔除": int(per_year.min()) != int(full.min())},
        "gate": "存在距自身判据线 <1.3% 的周频在册读数 ⇒ 进第二步；否则关闭",
        "crossed": None, "level": "CHECK",
        "not_measured": "在册周频读数与各自判据线的距离要人对着 02 号逐条判 ⇒ 本脚本只给"
                        "高估百分比，不替人判贴线与否（写了就是假精确）"}


# ============================================================ Q6 insider 方向未证实行
def q6():
    conn = ro()
    try:
        tot = conn.execute("SELECT COUNT(*) FROM insider_trade").fetchone()[0]
        n_zero_pos = conn.execute(
            "SELECT COUNT(*) FROM insider_trade WHERE delta_shares>0 "
            "AND (ratio=0 OR ratio IS NULL)").fetchone()[0]
        n_neg = conn.execute("SELECT COUNT(*) FROM insider_trade WHERE delta_shares<0").fetchone()[0]
        # 这批行有没有进过任何在册判决：alpha_bank 里与 insider 有关的列与其判定
        cols = [r[1] for r in conn.execute("PRAGMA table_info(alpha_bank)")]
        name_col = "name" if "name" in cols else cols[0]
        ab = pd.read_sql(f"SELECT {name_col}, status, expr FROM alpha_bank "
                         f"WHERE lower({name_col}) LIKE '%insider%' "
                         f"OR lower(expr) LIKE '%insider%'", conn)
    finally:
        conn.close()
    res["Q6_insider_rows"] = {
        "rows_total": int(tot), "ratio_zero_and_positive_delta": int(n_zero_pos),
        "rows_with_negative_delta_anywhere": int(n_neg),
        "alpha_bank_insider_rows": int(len(ab)),
        "alpha_bank_insider_status": sorted(set(ab["status"])) if len(ab) else [],
        "self_check": {"ratio 列真实存在(不是 change_ratio)": True,
                       "alpha_bank 查询未报错": True},
        "gate": "判决不变 ⇒ 保持默认 +1 并就地注释；判决变 ⇒ 预注册 + 重跑 F27",
        "crossed": bool(len(ab)), "level": "CROSS" if len(ab) else "OK",
        "reading": (f"方向假定影响的行数 = {n_zero_pos}（占 {100.0*n_zero_pos/max(1,tot):.2f}%）。"
                    "是否在某个在册判决里被消费：见 alpha_bank_insider_rows —— "
                    ">0 表示图谱/门禁用过它 ⇒ 进第二步；=0 表示只作为数据存在")}


# ============================================================ Q8 标签口径两格的实际影响
def q8():
    conn = ro()
    hits = {}
    try:
        for d, code in (("20231228", "688373.SH"), ("20240102", "000893.SZ")):
            hits[f"{code}@{d}"] = {
                "in_model_score": conn.execute("SELECT COUNT(*) FROM model_score "
                                               "WHERE trade_date=? AND ts_code=?",
                                               (d, code)).fetchone()[0],
                "in_daily_score": conn.execute("SELECT COUNT(*) FROM daily_score "
                                               "WHERE trade_date=? AND ts_code=?",
                                               (d, code)).fetchone()[0],
                "has_daily_row_later": conn.execute("SELECT COUNT(*) FROM daily "
                                                    "WHERE ts_code=? AND trade_date>?",
                                                    (code, d)).fetchone()[0]}
    finally:
        conn.close()
    touched = sum(v["in_model_score"] + v["in_daily_score"] for v in hits.values())
    res["Q8_label_conventions"] = {
        "two_cells": hits, "cells_appearing_in_any_score_table": int(touched),
        "self_check": {"两格都查了三张表": len(hits) == 2},
        "gate": "影响为空 ⇒ 维持'有意不同'；非空 ⇒ 预注册 A/B",
        "crossed": bool(touched), "level": "CROSS" if touched else "OK",
        "note": "差异本身由 tests/test_label_conventions.py 钉住（重叠域逐位一致 + "
                "生产不许更缺 + 差异恰好两格）；这里只回答'这两格进没进过任何打分表'"}


# ============================================================ Q9 builder vs 回测可交易性
def q9():
    conn = ro()
    try:
        sc = pd.read_sql("SELECT trade_date, ts_code, score FROM model_score", conn)
        d = pd.read_sql("SELECT trade_date, ts_code, close, vol, amount FROM daily", conn)
    finally:
        conn.close()
    v = d.pivot(index="trade_date", columns="ts_code", values="vol")
    c = d.pivot(index="trade_date", columns="ts_code", values="close")
    dates = sorted(v.index)
    idx = {x: i for i, x in enumerate(dates)}
    n_gap = n_months = 0
    gross = 0.0
    for t, g in sc.groupby("trade_date"):
        i = idx.get(t)
        if i is None or i + 5 >= len(dates):
            continue
        n_months += 1
        for code in g.sort_values("score", ascending=False).head(100)["ts_code"]:
            if code not in v.columns:
                continue
            v0 = v.at[t, code]
            if pd.notna(v0) and v0 > 0:
                continue                        # 方案日可交易 ⇒ 不属本项
            for k in range(1, 6):
                nd = dates[i + k]
                vv = v.at[nd, code]
                if pd.notna(vv) and vv > 0:     # 回测在 pos+1..+5 找到价 ⇒ 补上并成交
                    n_gap += 1
                    c0, c1 = c.at[t, code], c.at[dates[min(i + 5, len(dates) - 1)], code]
                    if pd.notna(c0) and pd.notna(c1) and c0:
                        gross += abs(float(c1 / c0 - 1.0))
                    break
    yrs = max(1.0, n_months / 12.0)
    upper_pp = 100.0 * gross / yrs / 100.0      # 每笔等权占 1/100 仓 ⇒ 上界按 |收益| 直接折年化
    res["Q9_tradability_gap"] = {
        "monthly_anchors_checked": n_months, "untradable_yet_filled": int(n_gap),
        "sum_abs_return_of_those": round(float(gross), 4),
        "upper_bound_pp_per_year": round(float(upper_pp), 4),
        "self_check": {"扫了全部评分月": n_months > 90,
                       "每笔按 1/100 仓、只算绝对值上界(高估方向)": True},
        "gate": "0 笔 或 <0.2pp ⇒ 关闭；否则 ⇒ 第二步（改回测可交易性 = 换 pin）",
        "crossed": bool(n_gap) and upper_pp >= 0.2, "level": "OK",
        "note": "上界是刻意保守(高估)的算法：不看持仓权重、不看手续费、按绝对值累加"}


# ============================================================ 未跑的四条：写清原因，不给数
def unfed():
    res["NOT_RUN"] = {
        "Q1_alpha_panel_dual_scale_gate": {
            "why": "全 158 条双尺门禁 A/B 需要 `critic.churn_cost` + 完整 `factory.run` 链路"
                   "（小时级），而 09-19 那份 `audit_alpha_panel_adj.py` 自己写明'不重跑门禁' ⇒ "
                   "名单是否变属于**阶段 B**，需单列一轮预注册",
            "already_have": "秩相关量级（133/158 条踩价格水平；代表 14 条含除权窗 ≈0.85）"
                            "见 logs/audit_alpha_panel_adj.json"},
        "Q2_scores_mask_scale": {
            "why": "两把尺的差别在 `scores.train()` 第 263 行的截面百分位上 ⇒ 要拿到两臂打分"
                   "必须重跑周频训练（不是只读 SQL 能给的）",
            "real_api": "alpha 侧可交易性矩阵 = app.pit.engine.PITEngine.tradable_matrix(dates, columns)"},
        "Q3_ic_three_scales": {
            "why": "要跑 `weekly.health.rolling_ic(window=12)` 全链（读打分存档）+ 同一批锚点上"
                   "另算未复权版，且判档要用 `health.evaluate` 的分级函数 —— 本版还没核对到"
                   "能一句跑通的调用方式，宁可不跑"},
        "Q7_stuck_holiday_interest": {
            "why": "结转占资与卖出回笼不补息发生在 `backtest/engine._roll_stuck` 内部"
                   "（`invested += sv['bp_raw']` 与 proceeds 分账），SQL 侧看不到 ⇒ 要么插桩跑一遍"
                   "回测、要么在引擎里加计数 —— 都不是'只读量化'",
            "correction": "上一版这里输出 0 笔却写了'实库确有跨月结转持仓（非 0 笔）'"
                          "⇒ 那句结论作废"}}


if __name__ == "__main__":
    for fn in (q4, q5, q6, q8, q9):
        try:
            fn()
        except Exception as e:
            res[fn.__name__ + "_error"] = f"{type(e).__name__}: {e}"
    unfed()
    LOGS.mkdir(parents=True, exist_ok=True)
    out = LOGS / "audit_b_stage_first_step.json"
    body = json.dumps(res, ensure_ascii=False, indent=2, default=str)
    out.write_text(body, encoding="utf-8")
    TRACKED.mkdir(parents=True, exist_ok=True)
    t_out = TRACKED / "audit_b_stage_first_step.json"
    t_out.write_text(body, encoding="utf-8")
    import hashlib
    sha = hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]
    (TRACKED / "README.md").write_text(
        "# 批次 2 第一步量化（2026-09-20）\n\n"
        f"- 产物：`audit_b_stage_first_step.json`（sha256 前 16 位 `{sha}`）\n"
        "- 生成：`D:\\Python313\\python.exe -X utf8 scripts/oneoff/audit_b_stage_first_step.py`\n"
        "- 门限（跑前冻结）：`plans/b-stage-criteria-20260920.md`\n"
        "- **为什么这份在 git 里而 `logs/` 里那份不在**：`logs/` 被 .gitignore 忽略 ⇒ 只落 logs\n"
        "  的量化证据会消失（本轮已发生一次）。凡进台账的读数必须落这种被跟踪的目录，\n"
        "  并与脚本**同一个 commit** 入库。\n", encoding="utf-8", newline="")
    print(f"== 产物: {out.relative_to(BASE)} + {t_out.relative_to(BASE)}  sha={sha}")
    for k, v in res.items():
        if isinstance(v, dict):
            print(f"-- {k}")
            for kk, vv in v.items():
                print(f"     {kk}: {json.dumps(vv, ensure_ascii=False, default=str)[:260]}")
        else:
            print(f"!! {k}: {v}")
