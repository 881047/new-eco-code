"""三类「无法归因」残差的**策略影响量化** —— 它们到底碰不碰得到策略域。

2026-09-12 D06/D07 验收。L1/L2 逐格比对把 8,966 格归不进既有 6 桶, 诊断
(`experiments/01_data/diag_d06_unknown.py`) 查出它们是三类**真实规则**、引擎缺:

  1. `S股(未股改) ±5%`        —— 600182.SH S佳通, 3 分类里唯一的主板非 ST
  2. `主板 ST 5%→10%` @2026-07-06 —— 155 只, 逐日 142~173 只全量同步切换(交易所实测无过渡日)
  3. `退市整理期 ±10%`        —— 创业板"退"字股, 引擎按改革后 20% 判(仅 3 只 19 格)

本脚本只回答一个问题: **每类涉及的股票, 有没有出现在生产打分表 / 方案 / 因子域里。**
影响的判据是「碰不碰得到」, 不是「分歧多少格」—— 北交所在 L1 里占 86% 的分歧但零影响。

用法: ./.venv/Scripts/python.exe scripts/oneoff/probe_st_cells_impact.py
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
_ORIG_STDOUT = sys.stdout
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from app import db  # noqa: E402


def main():
    conn = db.get_conn()
    tabs = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name LIKE 'model_score%' ORDER BY name")]

    # 三类涉及的代码
    s_share = ["600182.SH"]
    delist = ["300090.SZ", "300156.SZ", "300216.SZ"]
    st_155 = [r[0] for r in conn.execute(
        "SELECT DISTINCT ts_code FROM stk_limit WHERE trade_date>='20260706' "
        "AND trade_date<='20260710'")]
    # 用交易所档位反推 2026-07 那批: 主板 + 当期名称含 ST + 档位 0.10
    st_155 = [r[0] for r in conn.execute(
        "SELECT DISTINCT l.ts_code FROM stk_limit l "
        "WHERE l.trade_date='20260706' AND l.up_limit<99999")]

    print("=" * 74)
    print("三类残差的策略域影响 —— 判据是「碰不碰得到」")
    print("=" * 74)

    def probe(codes, label):
        print("\n--- %s (%d 只候选) ---" % (label, len(codes)))
        inmodel, indaily, infactor = [], [], []
        for t in tabs:
            hit = 0
            for c in codes:
                hit += conn.execute(
                    'SELECT COUNT(*) FROM "%s" WHERE ts_code=?' % t, (c,)).fetchone()[0]
            if hit:
                print("    %-34s %6d 行" % (t, hit))
        for c in codes:
            n = conn.execute("SELECT COUNT(*) FROM model_score WHERE ts_code=?",
                             (c,)).fetchone()[0]
            if n:
                inmodel.append(c)
            n = conn.execute("SELECT COUNT(*) FROM daily_score WHERE ts_code=?",
                             (c,)).fetchone()[0]
            if n:
                indaily.append(c)
            n = conn.execute("SELECT COUNT(*) FROM factor WHERE ts_code=?",
                             (c,)).fetchone()[0]
            if n:
                infactor.append(c)
        print("    model_score(生产分)      : %d 只 %s" % (len(inmodel), inmodel[:8]))
        print("    daily_score(日频推分)    : %d 只 %s" % (len(indaily), indaily[:8]))
        print("    factor(因子域)           : %d 只" % len(infactor))
        n = conn.execute(
            "SELECT COUNT(*) FROM plan WHERE ts_code IN (%s)"
            % ",".join("?" * len(codes)), codes).fetchone()[0]
        print("    plan(历史方案)           : %d 行" % n)

    probe(s_share, "类别 1: S股(未股改) ±5% —— 600182.SH S佳通")
    probe(delist, "类别 3: 退市整理期 ±10% —— 创业板 3 只")
    probe(st_155, "类别 2: 2026-07-06 主板 ST 5%→10% —— 该日在册全部代码")

    print("\n" + "=" * 74)
    print("附: 600182.SH 在 model_score 里的排名(若在域内, 它就可能被买)")
    print("=" * 74)
    rows = list(conn.execute(
        "SELECT trade_date, score FROM model_score WHERE ts_code='600182.SH' "
        "ORDER BY trade_date"))
    if not rows:
        print("  **不在 model_score** —— 类别 1 对策略零影响")
    else:
        print("  共 %d 期, 期间 %s ~ %s" % (len(rows), rows[0][0], rows[-1][0]))
        best = 999
        for d, sc in rows:
            rk = conn.execute("SELECT COUNT(*)+1 FROM model_score WHERE trade_date=? "
                              "AND score>?", (d, sc)).fetchone()[0]
            best = min(best, rk)
        print("  历史最好排名: 第 %d 名(策略买 top-6, 故名次越小于 7 越危险)" % best)
        print("  最近 5 期:")
        for d, sc in rows[-5:]:
            rk = conn.execute("SELECT COUNT(*)+1 FROM model_score WHERE trade_date=? "
                              "AND score>?", (d, sc)).fetchone()[0]
            tot = conn.execute("SELECT COUNT(*) FROM model_score WHERE trade_date=?",
                               (d,)).fetchone()[0]
            print("     %s  score=%.4f  排名 %d/%d" % (d, sc, rk, tot))

    conn.close()


if __name__ == "__main__":
    main()
