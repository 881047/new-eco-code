"""漂移判决实验 —— 用「今天重训 tm36 同配置」三方向定位 ho6 差异的根因(2026-09-16)

为什么需要它（复核请求：证明审查结论 C/E 正确）
------------------------------------------------
exp_holdout_recent v1 的对照臂(ho6, 今天重训, holdout=6)未复现 09-10 重训的 tm36 表
(+14.97% vs +19.40%)。当时的诊断: 全量重训不可跨日复现(09-14/15/16 日更修订历史因子面板)。
本实验做**判决性检验** —— 今天用与 exp_train_months tm36 臂**完全相同的参数**再重训一次
(写入隔离表 model_score_tm36_chk, 不碰证据表), 三个方向对比:

  · chk ≈ ho6(今天)  且 chk ≠ tm36(09-10)  ⇒ **漂移诊断实锤**(同配置同快照必同模型;
    差异只来自数据快照 —— 09-10 的表是旧快照, chk/ho6 是新快照)
  · chk ≈ tm36(09-10) 且 chk ≠ ho6        ⇒ **漂移诊断崩塌**(今天重训竟能复现 09-10 表,
    ho6 的差异必有别的原因 —— 例如 holdout_recent 显式传参 vs 默认值不等价, 即审查脚本有 bug)
  · chk 与两者都不同                       ⇒ **训练非确定**(deterministic=True 的承诺被打破,
    一切全量重训类实验的臂间比较都带不可控噪声)

预期: 第一种。判据(跑之前写死): 三方向两两"逐位一致率"(同锚点同股分数 |Δ|<1e-9 的比例),
以 99.9% 为"≈"的门槛(留浮点重排余量), 达不到 99.9% 即"不同"。

安全
----
只写自己的隔离表 model_score_tm36_chk / model_importance_tm36_chk(与
verify_d1_replica.py 的 model_score_die_d1replica 同类), 不碰 model_score / lgb_latest /
backtest_* / 生产任何表。模型归档进 data/models/ens_tm36_chk(gitignore)。
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pandas as pd

from app import db
from app.logger import setup_logger
from app.models.trainer import ENSEMBLE_ROUNDS, ENSEMBLE_SEEDS, run as train_run

setup_logger()
db.init_db()

ST = "model_score_tm36_chk"
IT = "model_importance_tm36_chk"
MODE = sys.argv[1] if len(sys.argv) > 1 else "all"
t0 = time.time()

if MODE in ("train", "all"):
    conn = db.get_conn()
    conn.execute(f"CREATE TABLE IF NOT EXISTS {ST} ("
                 "ts_code TEXT, trade_date TEXT, score REAL, pred_ret REAL, "
                 "PRIMARY KEY (ts_code, trade_date)) WITHOUT ROWID")
    conn.execute(f"CREATE TABLE IF NOT EXISTS {IT} ("
                 "train_date TEXT, factor TEXT, importance REAL, "
                 "PRIMARY KEY (train_date, factor)) WITHOUT ROWID")
    conn.commit()
    conn.close()
    arch = Path(__file__).resolve().parents[2] / "data" / "models" / "ens_tm36_chk"
    arch.mkdir(parents=True, exist_ok=True)
    # 参数与 exp_train_months 的 tm36 臂逐字一致(holdout 不传 = 默认 6 = tm36 臂的隐式值)
    r = train_run(train_months=36, train_start=None, backtest_start="2019-07-01",
                  full=False, score_table=ST, importance_table=IT, archive_dir=arch,
                  num_boost_round=ENSEMBLE_ROUNDS, early_stopping_rounds=0,
                  ensemble_seeds=ENSEMBLE_SEEDS)
    print("[{:>5.0f}s] chk 重训完成: {}".format(time.time() - t0,
          json.dumps(r, ensure_ascii=False, default=str)), flush=True)

if MODE in ("diff", "all"):
    conn = db.get_conn()
    try:
        pairs = [("tm36(09-10 旧快照)", "model_score_tm36"),
                 ("ho6(09-16 新快照)", "model_score_ho6")]
        out = {}
        for label, tbl in pairs:
            d = pd.read_sql(f"""
                SELECT a.trade_date,
                       COUNT(*) n,
                       SUM(ABS(a.score-b.score) < 1e-9) n_same
                FROM {ST} a JOIN {tbl} b
                  ON a.ts_code=b.ts_code AND a.trade_date=b.trade_date
                GROUP BY a.trade_date ORDER BY a.trade_date
            """, conn)
            tot = pd.read_sql(f"""
                SELECT COUNT(*) n, SUM(ABS(a.score-b.score) < 1e-9) n_same
                FROM {ST} a JOIN {tbl} b
                  ON a.ts_code=b.ts_code AND a.trade_date=b.trade_date
            """, conn)
            n, same = int(tot.iloc[0]["n"]), int(tot.iloc[0]["n_same"] or 0)
            rate = same / n if n else 0.0
            out[label] = {"n_pairs": n, "n_identical": same, "identical_rate": round(rate, 6),
                          "anchors_identical": int((d["n_same"] == d["n"]).sum()),
                          "anchors_total": len(d)}
            print("{:<22} 配对 {:>7}  逐位相同 {:>7} ({:.2%})  逐位相同锚点 {}/{}".format(
                label, n, same, rate, out[label]["anchors_identical"], out[label]["anchors_total"]))
    finally:
        conn.close()

    chk_0910 = out["tm36(09-10 旧快照)"]["identical_rate"]
    chk_today = out["ho6(09-16 新快照)"]["identical_rate"]
    NEAR = 0.999
    if chk_today >= NEAR and chk_0910 < NEAR:
        verdict = ("漂移诊断实锤：今天重训与 ho6(今天) 一致、与 tm36(09-10) 不一致 ⇒ "
                   "同配置同快照必同模型，tm36 表是旧数据快照的产物 —— 全量重训不可跨日复现成立，"
                   "ho 实验的臂间比较(同快照)有效")
    elif chk_0910 >= NEAR and chk_today < NEAR:
        verdict = ("漂移诊断崩塌：今天重训竟能复现 09-10 的 tm36 表 ⇒ ho6 的差异不是数据快照，"
                   "审查必须重查 exp_holdout_recent 脚本(holdout_recent 显式传参 vs 默认值)与训练调用路径")
    elif chk_0910 >= NEAR and chk_today >= NEAR:
        verdict = "意外：chk 与两个表都一致 ⇒ ho6 表与 tm36 表必然本来就一致 —— 与 v1 观测矛盾, 检查表名/查询"
    else:
        verdict = ("训练非确定：chk 与两个表都不同 ⇒ deterministic=True 的承诺被打破(或两次输入都变了)，"
                   "一切全量重训类实验的臂间比较带不可控噪声 —— 需要立即上报")
    print("\n判决:", verdict)

    out_path = (Path(__file__).resolve().parents[2] / "experiments" / "05_label"
                / "verify_tm36_retrain_result.json")
    out_path.write_text(json.dumps({
        "date": time.strftime("%Y-%m-%d %H:%M:%S"),
        "purpose": "复核审查结论 C/E：快照漂移诊断 + 训练确定性",
        "comparisons": out,
        "verdict": verdict,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print("结果已写", out_path)
