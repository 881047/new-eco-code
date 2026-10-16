"""PIN-B A/B：缺 `pre_close` 时封板闸门被整段跳过，会不会改成绩（2026-09-18 审查）

═══════════════════════════════════════════════════════════════════════════════
问题
═══════════════════════════════════════════════════════════════════════════════
`app/paper/engine.py::_exec_price` 拿不到 `pre_close` 时，**涨跌停闸门整段跳过**：

    else:
        limit_up = limit_dn = None
        if not pre and not no_limit:
            log.warning("成交价: %s %s 缺 pre_close ⇒ 无法判定涨跌停, 本次按无闸门放行(%s)", ...)

方向**偏乐观**：一字板也买得进。代码注释自认「改它会与回测引擎分叉、进而动 pin」，
所以一直没动，只加了日志让它"看得见"。

影响面（2026-09-18 实点）：生产库 `daily` 里 **193 行**缺 `pre_close`，
**全部是每只票在库内的首行**（绝大多数是北交/新三板上市首日之前）——
那本就是"还没得交易"的日子，所以**预期 Δ=0**，但仍须实测。

═══════════════════════════════════════════════════════════════════════════════
预注册判据（先冻结再跑）
═══════════════════════════════════════════════════════════════════════════════
  · 主判据：两臂**成交明细逐位一致**（成交价/数量/日期三元组完全相同）。
  · 次判据：任一评估窗年化 `|Δ| > 0.01pp` ⇒ 判"口径变了"。
  · 判定：任一不过 ⇒ **需重钉 pin**，且 `paper/engine.py` 与 `backtest/engine.py`
          **必须同批改**（只改一侧 = 纸面与回测分叉，不予采纳）。

═══════════════════════════════════════════════════════════════════════════════
两臂（通过引擎级开关切换，**不改任何默认行为**）
═══════════════════════════════════════════════════════════════════════════════
  A 臂（对照）：`MISSING_PRE_CLOSE_FAIL_CLOSED = False` —— 现行为，放行。
  B 臂（实验）：`MISSING_PRE_CLOSE_FAIL_CLOSED = True`  —— 缺 pre_close 视为无法判定，
                返回 None（顺延/作废），不再放行。

⚠️ 开关必须**同时存在于 `app/paper/engine.py` 与 `app/backtest/engine.py`**，
   且默认值保持 False（= 现行为）。本脚本只 flip 内存里的开关，不落盘改代码。

用法
────
    D:\\Python313\\python.exe -X utf8 scripts\\oneoff\\ab_paper_preclose_gate.py

输出：影响面（缺 pre_close 的行数与分布）+ 两臂成交明细差异 + 判决。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pandas as pd

from app import db
from app.logger import setup_logger

setup_logger()


def _missing_preclose(conn) -> pd.DataFrame:
    """影响面：库里到底有多少行缺 pre_close，以及它们是不是"库内首行"。"""
    return pd.read_sql(
        """
        SELECT d.ts_code, d.trade_date
        FROM daily d
        WHERE d.pre_close IS NULL
          AND d.trade_date = (SELECT MIN(trade_date) FROM daily x WHERE x.ts_code = d.ts_code)
        ORDER BY d.trade_date
        """, conn)


def _total_missing(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM daily WHERE pre_close IS NULL").fetchone()[0]


def _is_first_row_all(conn) -> tuple[int, int]:
    """(缺 pre_close 的总行数, 其中属于该票库内首行的行数)"""
    total = _total_missing(conn)
    first = conn.execute(
        """
        SELECT COUNT(*) FROM daily d
        WHERE d.pre_close IS NULL
          AND d.trade_date = (SELECT MIN(trade_date) FROM daily x WHERE x.ts_code = d.ts_code)
        """).fetchone()[0]
    return total, first


def main() -> int:
    with db.conn_ctx() as conn:
        total, first = _is_first_row_all(conn)
        df = _missing_preclose(conn)

    print(f"[PIN-B] 缺 pre_close 的行数: {total}")
    print(f"[PIN-B] 其中属于「该票库内首行」的: {first}")
    if total and first == total:
        print("[PIN-B] ⇒ 全部是首行（上市首日之前本就不可交易）⇒ 预期 Δ=0，但仍须 B 臂实测")
    elif total:
        print(f"[PIN-B] ⚠️ 有 {total - first} 行**不是**首行 ⇒ 真的会放行真实交易日，Δ 可能非 0")
    if not df.empty:
        print("[PIN-B] 样本(前 10):")
        print(df.head(10).to_string(index=False))

    # ---- 两臂：只 flip 内存开关，不落盘 ----
    from app.paper import engine as paper_engine
    from app.backtest import engine as bt_engine

    if not hasattr(paper_engine, "MISSING_PRE_CLOSE_FAIL_CLOSED") \
            or not hasattr(bt_engine, "MISSING_PRE_CLOSE_FAIL_CLOSED"):
        print("\n[PIN-B] ⚠️ 两个引擎尚未加 `MISSING_PRE_CLOSE_FAIL_CLOSED` 开关 —— "
              "按预注册要求，开关必须同时存在于 paper 与 backtest 两侧，"
              "且默认值 False（= 现行为）。请先加开关再跑本脚本。")
        print("[PIN-B] 判决: 待测（前置条件未满足）")
        return 0

    orig_p, orig_b = paper_engine.MISSING_PRE_CLOSE_FAIL_CLOSED, bt_engine.MISSING_PRE_CLOSE_FAIL_CLOSED
    try:
        arms = {}
        for name, flag in (("A(放行/现行为)", False), ("B(fail-closed)", True)):
            paper_engine.MISSING_PRE_CLOSE_FAIL_CLOSED = flag
            bt_engine.MISSING_PRE_CLOSE_FAIL_CLOSED = flag
            arms[name] = _run_arm()
        a, b = arms["A(放行/现行为)"], arms["B(fail-closed)"]
        same = _identical(a, b)
        print(f"\n[PIN-B] 两臂成交明细逐位一致: {same}")
        print("[PIN-B] 判决: " + ("Δ=0 ⇒ 不重钉" if same else "Δ≠0 ⇒ 需走重钉流程（两引擎同批改）"))
    finally:
        paper_engine.MISSING_PRE_CLOSE_FAIL_CLOSED = orig_p
        bt_engine.MISSING_PRE_CLOSE_FAIL_CLOSED = orig_b
    return 0


def _run_arm():
    """跑一臂的回测，返回成交明细骨干（日期/代码/价/量）。"""
    from app.backtest import engine as E
    res = E.run_backtest(run_tag="ab_preclose_probe")
    trades = (res or {}).get("trades")
    if trades is None:
        return None
    return trades


def _identical(a, b) -> bool:
    if a is None or b is None:
        return a is b
    try:
        return a.equals(b)
    except Exception:
        return False


if __name__ == "__main__":
    raise SystemExit(main())
