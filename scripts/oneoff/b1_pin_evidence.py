"""ONEOFF(2026-09-20, B1 采纳轮 S0): 换 pin 前后的不变量留证。只读。

判据冻结在 plans/b1-tr-pin-adoption-20260920.md：
  P-a 端点恒等  —— 新 prod 行 13 键必须 == 20260919_b1real_prod_c（全史行 == ..._full_c）
  P-b 特征不变  —— compute_price_factors 的 22 个价量因子（尤其 beta_250）逐位不变
  P-c 决策不变  —— 两份回测金标准一字不动（由 tests/test_backtest_golden.py 守，不在本脚本）

用法（同一份数据上 before/after 各跑一次，两份 JSON 对比即机器验收）：
  D:\\Python313\\python.exe -X utf8 scripts\\oneoff\\b1_pin_evidence.py --stage before
  D:\\Python313\\python.exe -X utf8 scripts\\oneoff\\b1_pin_evidence.py --stage after
产物：logs/b1_pin_evidence_<stage>.json

只读边界：DB 一律 `mode=ro` + `PRAGMA query_only=1`；本脚本不含任何写库语句。
唯一磁盘产物 = 上面那个 JSON。行情面板走 `load_market` 的正常缓存路径（缓存命中时不写盘）。
"""
import argparse
import hashlib
import json
import sqlite3
import sys
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE))
LOGS = BASE / "logs"

DB = BASE / "data" / "neweco.db"
PIN_ROWS = [
    "20260912_limitquant",      # 现行 pin（价格指数尺）
    "20260919_b1real_prod_a",   # 真尺 A/B：价格指数臂（应与现行 pin 同尺）
    "20260919_b1real_prod_c",   # 真尺 A/B：H00906 全收益臂  ← P-a 的比对基准
    "20260919_b1real_full_a",
    "20260919_b1real_full_c",   # 全史窗 P-a 基准
]
BENCH_CODES = ("000906.SH", "H00906.CSI")
API_PROBE = ("http://127.0.0.1:8787/api/overview",
             "http://127.0.0.1:8787/api/weekly",
             "http://127.0.0.1:8787/api/paper")
# 指纹算法版本：v1 把 Series 的 name 混进了指纹（同一份数据走缓存路径 name='bench'、走 SQL
# 重建路径 name='close' ⇒ 两条合法路径会算出不同指纹 = 工具假红）。v2 只看索引值 + 数据字节。
# ⚠️ 本轮（2026-09-20）的 before/after 两份 JSON 都是 **v1** 产物 ⇒ 它们之间可比、P-b 已据此判过；
#    以后重跑得到的是 v2 值，不要拿它去和这两份直接比字符串。
SHA_SCHEME = "v2-index-values-no-name"
# P-b 的子集：固定 400 列 × 末尾 500 行（滚动窗需要 ≥250 行 ⇒ 前 250 行按构造是 NaN，
# before/after 同形 ⇒ 可比）。取子集是因为全面板 22 个因子要占 GB 级内存。
N_CODES, N_DATES = 400, 500


def _sha(obj) -> str:
    """DataFrame/Series 的**数值**指纹（列名+索引值+数据字节；顺序也进指纹）。

    ⚠️ Series 的 `name` 有意**不进**指纹：同一个 `m["bench"]` 走缓存路径 name='bench'、
    走 SQL 重建路径 name='close'（`_cache_save` 用 `.to_frame("bench")` 存）。上一版把 name
    算进去了，于是"缓存命中 vs 重建"这两种合法路径会被报成"基准腿变了"——那是测量工具的
    假红，不是实现错。数值等价性改由索引值+数据字节证明（旧/新缓存的 bench.parquet 已实测
    逐位相同）。
    """
    if isinstance(obj, pd.DataFrame):
        cols = "|".join(map(str, obj.columns))
        idx = hashlib.sha256("|".join(map(str, obj.index)).encode()).hexdigest()[:8]
        body = obj.to_numpy(dtype=np.float64, na_value=np.nan).tobytes()
    elif isinstance(obj, pd.Series):
        cols = ""
        idx = hashlib.sha256("|".join(map(str, obj.index)).encode()).hexdigest()[:8]
        body = obj.to_numpy(dtype=np.float64, na_value=np.nan).tobytes()
    else:
        cols, idx, body = "", "", repr(obj).encode()
    return hashlib.sha256(f"{cols}#{idx}#".encode() + body).hexdigest()[:16]


def _sample(obj, k: int = 6) -> list:
    """几个固定位置的原始值（指纹撞不出来时给人看的）。"""
    v = np.asarray(obj, dtype=np.float64).ravel()
    if v.size == 0:
        return []
    pos = np.linspace(0, v.size - 1, k).astype(int)
    return [None if np.isnan(v[i]) else round(float(v[i]), 9) for i in pos]


def read_db() -> dict:
    conn = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only=1")
        out = {}
        want = ",".join("?" * len(PIN_ROWS))
        rows = conn.execute(
            f"SELECT run_date, metrics FROM backtest_summary WHERE run_date IN ({want})",
            PIN_ROWS).fetchall()
        out["backtest_rows"] = {r["run_date"]: json.loads(r["metrics"]) for r in rows}
        out["missing_pin_rows"] = [r for r in PIN_ROWS if r not in out["backtest_rows"]]
        # 数据新鲜度：before/after 只有在这些 MAX 相同的前提下才可比
        out["data_max"] = {t: conn.execute(f"SELECT MAX(trade_date) FROM {t}").fetchone()[0]
                           for t in ("daily", "daily_basic", "adj_factor", "index_daily")}
        out["bench_coverage"] = {
            c: dict(conn.execute(
                "SELECT COUNT(*) AS n, MIN(trade_date) AS first, MAX(trade_date) AS last, "
                "SUM(close IS NULL) AS null_close, SUM(open IS NULL) AS null_open "
                "FROM index_daily WHERE ts_code=?", (c,)).fetchone())
            for c in BENCH_CODES}
        # 袖套账本与模拟盘基准列的现状（证明本轮没动 paper_nav）
        out["paper_nav"] = dict(conn.execute(
            "SELECT COUNT(*) AS n, MIN(trade_date) AS first, MAX(trade_date) AS last, "
            "MIN(bench_nav) AS bmin, MAX(bench_nav) AS bmax FROM paper_nav").fetchone())
        out["stage_ledger_running"] = [
            dict(r) for r in conn.execute(
                "SELECT id, stage, run_date, status FROM pipeline_run WHERE status='running'")]
        return out
    finally:
        conn.close()


def read_panel() -> dict:
    """改前/改后同一把尺：`bench` 序列与 22 个价量因子的指纹。"""
    from app.factors import factor_engine as fe
    m = fe.load_market()
    keys = ["close", "pre_close", "open", "high", "low", "vol", "amount", "adj", "pct",
            "turnover", "pe_ttm", "pb", "dv_ttm", "total_mv", "circ_mv"]
    common = sorted(set.intersection(*[set(m[k].columns) for k in keys]))
    codes = common[:N_CODES]
    dates = m["close"].index[-N_DATES:]
    sub = {k: m[k].reindex(index=dates, columns=codes) for k in keys}
    sub["bench"] = m["bench"].reindex(dates)
    if "bench_tr" in m:                       # 只有改后才存在 —— 有它就单独指纹
        sub["bench_tr"] = m["bench_tr"].reindex(dates)
    f = fe.compute_price_factors(sub)
    out = {
        "panel_dates": [str(dates[0].date()), str(dates[-1].date())],
        "n_codes": len(codes),
        "bench_series": {"sha": _sha(m["bench"]), "name": str(m["bench"].name),
                         "last": round(float(m["bench"].iloc[-1]), 6),
                         "nan_in_window": int(m["bench"].reindex(dates).isna().sum())},
        "price_factors": {k: {"sha": _sha(v), "sample": _sample(v)} for k, v in sorted(f.items())},
    }
    for leg in ("bench", "bench_tr"):
        if leg in sub:
            out[f"leg_{leg}"] = {"sha": _sha(sub[leg]),
                                 "nan_in_window": int(sub[leg].isna().sum())}
    return out


def read_api() -> dict:
    out = {}
    for url in API_PROBE:
        name = url.rsplit("/", 1)[-1]
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                d = json.loads(r.read().decode("utf-8"))
        except Exception as e:                       # 服务可能在重启 —— 留证不许炸
            out[name] = {"error": f"{type(e).__name__}: {e}"}
            continue
        if name == "overview":
            bt = d.get("backtest") or {}
            out[name] = {"pin_missed": d.get("backtest_pin_missed"),
                         "run_date": bt.get("run_date"),
                         "excess_ann": bt.get("excess_ann"), "bench_ann": bt.get("bench_ann"),
                         "ann_return": bt.get("ann_return"),
                         "max_drawdown": bt.get("max_drawdown"),
                         "win_rate": bt.get("win_rate")}
        elif name == "weekly":
            k = ((d.get("ledger") or {}).get("kpi")) or d.get("kpi") or {}
            out[name] = {kk: k.get(kk) for kk in ("ann_return", "bench_ann", "excess_pp",
                                                  "note", "weeks", "nav")}
        else:
            out[name] = {"bench_nav_last": (d.get("nav") or [{}])[-1].get("bench_nav"),
                         "nav_last": (d.get("nav") or [{}])[-1].get("nav")}
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", required=True, choices=("before", "after"))
    args = ap.parse_args()

    res = {"stage": args.stage, "sha_scheme": SHA_SCHEME,
           "data_max_expectation": "两份 JSON 的 data_max 必须相同才可比",
           "db": read_db(), "panel": read_panel(), "api": read_api()}
    LOGS.mkdir(parents=True, exist_ok=True)
    out = LOGS / f"b1_pin_evidence_{args.stage}.json"
    out.write_text(json.dumps(res, ensure_ascii=False, indent=2, default=str),
                   encoding="utf-8")

    pf = res["panel"]["price_factors"]
    joint = "‖".join(f"{k}={v['sha']}" for k, v in sorted(pf.items()))
    print(f"== 证据已落 {out.relative_to(BASE)}")
    print(f"  数据新鲜度 data_max   : {res['db']['data_max']}")
    print(f"  缺失的留证 pin 行     : {res['db']['missing_pin_rows'] or '无'}")
    print(f"  beta_250 指纹         : {pf['beta_250']['sha']}  样本 {pf['beta_250']['sample']}")
    print(f"  价量因子共 {len(pf)} 个，整体指纹 = {hashlib.sha256(joint.encode()).hexdigest()[:16]}")
    print(f"  bench 序列指纹        : {res['panel']['bench_series']['sha']}"
          f"（窗内 NaN {res['panel']['bench_series']['nan_in_window']}）")
    for leg in ("leg_bench", "leg_bench_tr"):
        if leg in res["panel"]:
            print(f"  {leg:<19}: {res['panel'][leg]}")
    print(f"  paper_nav（本轮不动） : {res['db']['paper_nav']}")
    print(f"  仍 running 的阶段行   : {res['db']['stage_ledger_running']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
