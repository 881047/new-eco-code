"""一次性迁移 (P1-11/P2-2/P0-3): python scripts/migrate_daily_score.py
1. 把 model_score 中日频推分产生的行(不在 model_importance.train_date 中的日期)迁到 daily_score
2. 回填 paper_position.entry_adj(按每只持仓首笔买入日的 adj_factor)
3. 修正 data_status.fina_indicator 的行数为表内真实行数 (P2-5)
幂等: 重复执行无副作用。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from app.logger import setup_logger
from app import db

log = setup_logger()


def main():
    from _guard import require_confirm
    require_confirm(__file__, "DELETE 生产 model_score / INSERT OR REPLACE daily_score / "
                              "UPDATE paper_position（不可逆）")
    db.init_db()
    conn = db.get_conn()

    # 1) 日频推分 → daily_score
    monthly_dates = {r[0] for r in conn.execute("SELECT DISTINCT train_date FROM model_importance")}
    rows = conn.execute("SELECT COUNT(*) FROM model_score").fetchone()[0]
    if monthly_dates:
        moved = 0
        for (d,) in conn.execute("SELECT DISTINCT trade_date FROM model_score").fetchall():
            if d not in monthly_dates:
                conn.execute(
                    "INSERT OR REPLACE INTO daily_score (ts_code, trade_date, score, pred_ret) "
                    "SELECT ts_code, trade_date, score, pred_ret FROM model_score WHERE trade_date=?",
                    (d,))
                c = conn.execute("DELETE FROM model_score WHERE trade_date=?", (d,))
                moved += c.rowcount
                log.info("迁移日频推分 %s → daily_score: %d 行", d, c.rowcount)
        log.info("model_score 迁移完成: 原 %d 行, 移出 %d 行, 剩余月度训练分", rows, moved)

    # 2) paper_position.entry_adj 回填(按首笔买入日 adj_factor)
    pos = conn.execute("SELECT ts_code FROM paper_position").fetchall()
    n_filled = 0
    for (code,) in pos:
        ea = conn.execute("SELECT entry_adj FROM paper_position WHERE ts_code=?", (code,)).fetchone()[0]
        if ea:
            continue
        first = conn.execute(
            "SELECT exec_date FROM paper_trade WHERE ts_code=? AND side='buy' ORDER BY exec_date LIMIT 1",
            (code,)).fetchone()
        if not first:
            continue
        adj = conn.execute(
            "SELECT adj_factor FROM adj_factor WHERE ts_code=? AND trade_date=?",
            (code, first[0])).fetchone()
        if adj and adj[0]:
            conn.execute("UPDATE paper_position SET entry_adj=? WHERE ts_code=?", (adj[0], code))
            n_filled += 1
    log.info("paper_position.entry_adj 回填 %d 只", n_filled)

    # 3) fina_indicator 状态计数修正
    n_actual = conn.execute("SELECT COUNT(*) FROM fina_indicator").fetchone()[0]
    db.set_status(conn, "fina_indicator", "", n_actual)
    log.info("fina_indicator 状态计数修正为 %d", n_actual)

    conn.commit()
    conn.close()
    log.info("迁移完成")


if __name__ == "__main__":
    main()
