"""M2（Q7 / 外部审查 P2-3）：跨月停牌结转的"占资计息"上界有多大 —— 只读实测。

问题：`backtest/engine._roll_stuck` 把结转持仓按 `bp_raw` 计入 `invested`（第 144 行），
而闲置现金另走 `cash_credit`（第 253 行）⇒ 结转那笔资金**没有被当成闲置资金计息**。
若这笔"少计的息"可观，回测收益被低估；若可忽略 ⇒ 关闭（SQL 看不见，历来只能插桩，
所以台账把它标了三天"未跑"）。

做法（**不改 `app/**`、不写库**）：在本脚本里把模块级函数 `engine._roll_stuck` **包一层**，
每次调用时记录"进入本函数时仍挂着的结转格" `(月份, code, bp_raw)` ⇒ 求和即"结转·月"。
同时把落库动作在进程内掐掉（`db.upsert_df` 换成计数器 + `conn_ctx` 换成"写语句一律拦掉"的代理），
跑完用**独立只读连接**比对手前手后的 `backtest_summary`/`backtest_nav` 指纹（M2-a）⇒ 零写入不是口头承诺。
闲置利率与引擎同源：`repo_rate.overnight`（20230904 起有数），缺失月份用引擎同一个
`FALLBACK_CASH=1.5%` 兜底（2017~2023-08 全靠它，见 09 号那条断点）。
跑两窗：生产 85 期（`config.eval_start`）与全史 115 期。

判据跑前冻结在 `plans/ledger-settlement-criteria-20260920.md §M2`：
  U = Σ(bp_raw_结转月 × 该月闲置年化利率/12) / 平均净值 / 年数  （pp/年）
  U < 0.2pp/年 ⇒ 关闭（沿用 Q9 那条同量级门槛）；U ≥ 0.2pp/年 ⇒ 单列一轮（动回测账务 = 换 pin）。
门：M2-a 零写入指纹 / M2-b 包装版逐月净值与在册 pin 行**逐位相同** / M2-c 利率置 0 ⇒ U 必须为 0 /
    M2-d 结转笔数 0 时不得判"无影响"，写 not_measured。
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app import config as cfg_mod                       # noqa: E402
from app.backtest import engine                         # noqa: E402

OUT = ROOT / "experiments" / "b_stage_20260920" / "m2_stuck_rollover_interest.json"

_records: list = []          # [(月份, code, bp_raw)] —— 由包装器填
_real = engine._roll_stuck


def _wrapped(stuck, m, dates, pos1, st_codes, slip, cost, **kw):
    """进入时快照，离开时快照 ⇒ "整个这一月都挂着"的格 = 两次快照都在的那批。"""
    before = {c: float(v.get("bp_raw", 0.0)) for c, v in (stuck or {}).items()}
    r = _real(stuck, m, dates, pos1, st_codes, slip, cost, **kw)
    after = {c: float(v.get("bp_raw", 0.0)) for c, v in (stuck or {}).items()}
    d = dates[pos1] if pos1 < len(dates) else None
    # `dates` 是面板日期（Timestamp），不是 'YYYYMMDD' 串 —— 第一版直接 str()[:6] 得到
    # '2017-0' 这种半截月份，两窗回测跑完才在聚合处炸 ⇒ 显式规范化成 'YYYYMM'
    ym = "" if d is None else (d[:6] if isinstance(d, str) else pd.Timestamp(d).strftime("%Y%m"))
    for c, v in before.items():
        if c in after:                       # 本月末仍在结转 ⇒ 这一整月的占资没进闲置计息
            _records.append((ym, c, v))
    return r


FALLBACK_CASH = engine.FALLBACK_CASH          # 与引擎同一个兜底年利率(%)，不自己另写一个
_blocked: list = []                            # 被拦掉的写语句（应当正好是 3 次）
_real_conn_ctx = engine.db.conn_ctx
_real_upsert = engine.db.upsert_df

_WRITE_HEADS = ("INSERT", "UPDATE", "DELETE", "REPLACE", "CREATE", "DROP", "ALTER")


class _NoWrite:
    """代理真连接：读全部放行，写一律拦（本脚本只需要返回值，不需要落库）。"""

    def __init__(self, real):
        self._real = real

    def execute(self, sql, *a, **kw):
        if str(sql).strip().upper().startswith(_WRITE_HEADS):
            _blocked.append(str(sql).strip()[:60])
            return None
        return self._real.execute(sql, *a, **kw)

    def commit(self):
        return None

    def cursor(self):
        return self._real.cursor()

    def __getattr__(self, k):
        return getattr(self._real, k)


def _install_blockers():
    def ctx():
        cm = _real_conn_ctx()
        real = cm.__enter__()
        return _Ctx(_NoWrite(real), cm)

    engine.db.conn_ctx = ctx
    engine.db.upsert_df = lambda conn, df, table, **kw: _blocked.append(f"upsert:{table}")


class _Ctx:
    def __init__(self, w, inner):
        self.w, self.inner = w, inner

    def __enter__(self):
        return self.w

    def __exit__(self, *a):
        return self.inner.__exit__(*a)


def uninstall_blockers():
    engine.db.conn_ctx = _real_conn_ctx
    engine.db.upsert_df = _real_upsert


def rate_by_month(conn) -> dict:
    """闲置资金利率：`repo_rate.overnight`（%）按月均值 ÷12；缺失月份由调用方补 FALLBACK_CASH。"""
    df = pd.read_sql("SELECT trade_date, overnight FROM repo_rate ORDER BY trade_date", conn)
    if not len(df):
        return {}
    df["ym"] = df["trade_date"].str[:6]
    df["overnight"] = df["overnight"].astype(float)
    ann = df.groupby("ym")["overnight"].mean()
    return {k: float(v) / 100.0 / 12.0 for k, v in ann.items()}


def monthly_rate(rates, ym) -> float:
    return rates.get(ym, (FALLBACK_CASH / 100.0) / 12.0)


def _U_pp_yr(frame: pd.DataFrame, rates: dict, mean_nav: float, span_years: float):
    """把"结转·月 × 月息"折成 pp/年。frame 为空 ⇒ 返回 0（不是 None）。"""
    if frame is None or not len(frame) or not mean_nav:
        return 0.0
    per = frame.groupby("ym")["bp_raw"].sum()
    tot = float(sum(v * monthly_rate(rates, k) for k, v in per.items()))
    return 100 * tot / mean_nav / span_years


def _scale_control(frame, rates, mean_nav, span_years, u_actual):
    """M2-c 真对照：清空结转 ⇒ U 必须为 0；结转资金翻倍 ⇒ U 必须精确翻倍。

    （第一版这里写成"利率置 0 ⇒ U=0"——那是个**永远不会红**的空检查，
    与三补二十六 §② 那条"门的逻辑本身也要被用例证伪"同一族错误，已换成响应性对照。）
    """
    if u_actual is None:
        return {"pass": False, "why": "u_actual is None"}
    zero = _U_pp_yr(frame.iloc[0:0] if len(frame) else frame, rates, mean_nav, span_years)
    dbl_frame = frame.copy()
    if len(dbl_frame):
        dbl_frame["bp_raw"] = dbl_frame["bp_raw"] * 2.0
    dbl = _U_pp_yr(dbl_frame, rates, mean_nav, span_years)
    ok_zero = abs(zero) < 1e-12
    ok_dbl = abs(dbl - 2 * u_actual) <= 1e-9 * max(1.0, abs(u_actual))
    ok_move = (u_actual > 0) if len(frame) else True
    return {"pass": bool(ok_zero and ok_dbl and ok_move),
            "U_zero_when_no_records": zero, "U_when_bp_doubled": round(dbl, 6),
            "U_actual": None if u_actual is None else round(u_actual, 6),
            "rule": "清空必须 0；翻倍必须精确翻倍；有结转记录时 U 必须 > 0"}


def fingerprint(conn) -> str:
    import hashlib
    q = ("SELECT run_date, metrics FROM backtest_summary ORDER BY run_date")
    rows = conn.execute(q).fetchall()
    h = hashlib.sha256(repr(rows).encode()).hexdigest()[:16]
    n = conn.execute("SELECT COUNT(*) FROM backtest_nav").fetchone()[0]
    return f"{h}/{n}"


def main() -> int:
    cfg = cfg_mod.load_config()
    pin = cfg.get("backtest_run")
    ro = "file:" + str((ROOT / "data" / "neweco.db")).replace("\\", "/") + "?mode=ro"
    con = sqlite3.connect(ro, uri=True)
    con.execute("PRAGMA query_only=ON")
    fp_before = fingerprint(con)
    rates = rate_by_month(con)
    pin_rows = {r[0]: r[1] for r in con.execute(
        "SELECT run_date, metrics FROM backtest_summary WHERE run_date IN (?,?)", (pin, pin + "_full"))}
    con.close()

    engine._roll_stuck = _wrapped
    _install_blockers()
    windows, err = [], None
    try:
        for label, start, key in (("prod", cfg.get("eval_start"), pin), ("full", None, pin + "_full")):
            t0 = time.time()
            m = engine.run_backtest(run_tag=f"m2probe_{label}", start=start)
            windows.append({"window": label, "metrics_keys": len(m),
                            "ann_return": m.get("ann_return"), "max_drawdown": m.get("max_drawdown"),
                            "periods": m.get("periods"), "runtime_s": round(time.time() - t0, 1),
                            "pin_row": key})
    except Exception as ex:                               # noqa: BLE001
        err = f"{type(ex).__name__}: {ex}"
    finally:
        uninstall_blockers()
        engine._roll_stuck = _real                        # 进程内还原，不留桩

    df = pd.DataFrame(_records, columns=["ym", "code", "bp_raw"])
    mean_nav = 20_000_000.0                                # 回测本金口径(config.capital)
    cap = cfg.get("capital")
    if isinstance(cap, (int, float)) and cap > 0:
        mean_nav = float(cap)
    def _mi(ymstr) -> int:                      # 'YYYYMM' → 月序号（跨年不能按 /100 算）
        return int(str(ymstr)[:4]) * 12 + int(str(ymstr)[4:6])
    span_years = (max(1.0, (_mi(df["ym"].max()) - _mi(df["ym"].min()) + 1) / 12.0)
                  if len(df) else 1.0)
    U_pp_yr = _U_pp_yr(df, rates, mean_nav, span_years) if len(df) else None

    con = sqlite3.connect(ro, uri=True); con.execute("PRAGMA query_only=ON")
    fp_after = fingerprint(con)
    eq = {}
    for w in windows:                                       # M2-b：包装+拦写之后读数仍须与在册 pin 逐位同
        m_pin = pin_rows.get(w["pin_row"])
        if m_pin and w.get("ann_return") is not None:
            j = json.loads(m_pin)
            eq[w["window"]] = {
                "ann_return_same": abs(j.get("ann_return", -1) - w["ann_return"]) < 1e-12,
                "max_drawdown_same": abs(j.get("max_drawdown", -1) - w["max_drawdown"]) < 1e-12}
    con.close()

    res_out = {
        "criteria": "plans/ledger-settlement-criteria-20260920.md §M2（跑前冻结）",
        "verdict": None,
        "U_pp_per_year": None if U_pp_yr is None else round(U_pp_yr, 4),
        "threshold_pp_per_year": 0.2,
        "stuck_cell_months": int(len(df)),
        "distinct_stuck_codes": int(df["code"].nunique()) if len(df) else 0,
        "months_with_rollover": int(df["ym"].nunique()) if len(df) else 0,
        "bp_raw_sum_top5_months": (df.groupby("ym")["bp_raw"].sum().nlargest(5)
                                   .to_dict() if len(df) else {}),
        "idle_rate_source": "repo_rate.overnight 月均/12；缺月份用引擎同一个 "
                            f"FALLBACK_CASH={FALLBACK_CASH}%（2017~2023-08 全靠它）",
        "rate_months_from_repo": len(rates),
        "windows": windows,
        "backtest_error": err,
        "writes_blocked": sorted(set(_blocked)),
        "gates": {
            "M2a_zero_write": {"fp_before": fp_before, "fp_after": fp_after,
                               "pass": fp_before == fp_after},
            "M2b_parity_with_registered_pin": {"detail": eq,
                                               "pass": bool(eq) and len(eq) == len(windows) and all(
                                                   v["ann_return_same"] and v["max_drawdown_same"]
                                                   for v in eq.values())},
            "M2c_instrument_responds": _scale_control(df, rates, mean_nav, span_years, U_pp_yr),
            "M2d_nonzero_guard": {"pass": bool(len(df) > 0)}},
        "not_measured": [],
    }
    if err:
        res_out["not_measured"].append(f"回测未跑通：{err}")
        res_out["verdict"] = "未跑完（如实登记，不给估计值）"
    elif not res_out["gates"]["M2a_zero_write"]["pass"]:
        res_out["verdict"] = "本轮读数作废：拦写没拦住（M2-a 未过）⇒ 立刻查 `writes_blocked`"
    elif not res_out["gates"]["M2b_parity_with_registered_pin"]["pass"]:
        res_out["verdict"] = "本轮读数作废：包装/拦写改变了引擎结果（M2-b 未过）⇒ 不能拿它当尺"
    elif len(df) == 0:
        res_out["not_measured"].append(
            "结转格数 = 0 ⇒ 测不出东西，**不得**据此判'无影响'（M2-d；上一版就是在 0 笔上写了结论）")
        res_out["verdict"] = "未测量（两窗都没有跨月结转持仓）"
    elif U_pp_yr < 0.2:
        res_out["verdict"] = "关闭：跨月结转占资少计的息 < 0.2pp/年 ⇒ 不值得动回测账务（动它=换 pin）"
    else:
        res_out["verdict"] = "单列一轮：量级越门 ⇒ 改回测账务须 A/B + 重钉"
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(res_out, indent=2, ensure_ascii=False, default=str) + "\n", "utf-8")
    print(json.dumps({k: res_out[k] for k in ("verdict", "U_pp_per_year", "stuck_cell_months",
                                              "months_with_rollover", "writes_blocked",
                                              "backtest_error")}, ensure_ascii=False))
    print("gates:", {k: v["pass"] for k, v in res_out["gates"].items()})
    print("parity:", json.dumps(res_out["gates"]["M2b_parity_with_registered_pin"]["detail"],
                                ensure_ascii=False))
    print(f"→ {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
