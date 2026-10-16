"""② 修复的独立验证(2026-09-09): 全 NaN 样本兜底对生产训练是否数值惰性

做法: 把 model_score 的全部历史月份(除最新月)复制到临时表 model_score_smoke,
再以同一生产配置(4 种子 × 50 轮)重训最新月 20260831 —— 若 ② 的兜底没有剔掉任何
样本, 则重训分应与生产 model_score 完全一致(逐股 score 差 = 0)。

预期(实测 0 条全 NaN 入训): 完全一致 → 证明本次修复不改变生产模型。
若出现差异, 说明当前数据里确有全 NaN 样本入训, 需重新评估。

运行: PYTHONPATH=".venv/Lib/site-packages" python scripts/oneoff/smoke_022_trainer_inert.py
"""
import logging
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

from app import db
from app.models.trainer import ENSEMBLE_ROUNDS, ENSEMBLE_SEEDS, run

SCORE_TMP = "model_score_smoke"
IMP_TMP = "model_importance_smoke"


def main():
    conn = db.get_conn()
    for t, ddl in (
        (SCORE_TMP, "CREATE TABLE IF NOT EXISTS model_score_smoke ("
                    "ts_code TEXT, trade_date TEXT, score REAL, pred_ret REAL, "
                    "PRIMARY KEY (ts_code, trade_date)) WITHOUT ROWID"),
        (IMP_TMP, "CREATE TABLE IF NOT EXISTS model_importance_smoke ("
                  "train_date TEXT, factor TEXT, importance REAL, "
                  "PRIMARY KEY (train_date, factor)) WITHOUT ROWID")):
        conn.execute(ddl)
    # 除最新月外全部"已有评分" → 只重训最新月
    conn.execute(f"DELETE FROM {SCORE_TMP}")
    conn.execute(
        f"INSERT INTO {SCORE_TMP} (ts_code, trade_date, score, pred_ret) "
        f"SELECT ts_code, trade_date, score, pred_ret FROM model_score "
        f"WHERE trade_date < (SELECT MAX(trade_date) FROM model_score)")
    conn.commit()
    latest = conn.execute("SELECT MAX(trade_date) FROM model_score").fetchone()[0]
    conn.close()

    tmpdir = Path(tempfile.mkdtemp(prefix="smoke022_"))
    try:
        r = run(full=False, score_table=SCORE_TMP, importance_table=IMP_TMP,
                archive_dir=tmpdir, num_boost_round=ENSEMBLE_ROUNDS,
                early_stopping_rounds=0, ensemble_seeds=ENSEMBLE_SEEDS)
        print("重训结果:", r)
        conn = db.get_conn()
        prod = pd.read_sql("SELECT ts_code, score FROM model_score WHERE trade_date=?",
                           conn, params=(latest,)).set_index("ts_code")["score"]
        smoke = pd.read_sql(f"SELECT ts_code, score FROM {SCORE_TMP} WHERE trade_date=?",
                            conn, params=(latest,)).set_index("ts_code")["score"]
        conn.execute(f"DROP TABLE IF EXISTS {SCORE_TMP}")
        conn.execute(f"DROP TABLE IF EXISTS {IMP_TMP}")
        conn.execute("DELETE FROM data_status WHERE name IN (?,?)", (SCORE_TMP, IMP_TMP))
        conn.commit()
        conn.close()
        print(f"生产 {latest}: {len(prod)} 只 | 重训: {len(smoke)} 只")
        if set(prod.index) != set(smoke.index):
            print("!! 股票集合不同:", sorted(set(prod.index) ^ set(smoke.index))[:20])
        diff = (prod - smoke.reindex(prod.index)).abs()
        print(f"score 最大绝对差: {diff.max():.10f} | 均值差: {diff.mean():.10f} | 不一致(>1e-9) 只数: {int((diff > 1e-9).sum())}")
        print("结论:", "数值完全一致 → ② 兜底对生产训练惰性(0 条全 NaN 样本)" if diff.max() < 1e-9
              else "存在差异 → 当前数据确有全 NaN 样本入训, 需复查")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    main()
