"""模型滚动训练脚本: python scripts/train_model.py"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.logger import setup_logger
from app import db
from app.config import DATA_DIR, load_config
from app.models.trainer import run, ENSEMBLE_SEEDS, ENSEMBLE_ROUNDS


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--train-months", type=int, default=36)
    parser.add_argument("--backtest-start", default="2017-01-01")
    parser.add_argument("--train-start", default=None,
                        help="训练/评分月起点(默认取 config data_start, 与日链同源; "
                             "config data_start 现为 null = 全历史, 见 2026-09-11 起点解耦)")
    parser.add_argument("--full", action="store_true",
                        help="全量重训所有月份(因子口径变更后刷新历史分数; 默认增量只训新月份)")
    parser.add_argument("--score-table", default=None, help="评分结果表(默认 model_score)")
    parser.add_argument("--importance-table", default=None, help="因子重要性表(默认 model_importance)")
    parser.add_argument("--rounds", type=int, default=ENSEMBLE_ROUNDS,
                        help="每模型最大训练轮数(生产集成默认 50 轮无早停, 0904 轮数扫描结论)")
    parser.add_argument("--early-stop", type=int, default=0,
                        help="早停耐心轮数(默认 0=关闭固定轮数训练; 旧配置为 50)")
    parser.add_argument("--holdout-recent", type=int, default=6,
                        help="近端留出月数(2026-09-06 R11-1: 默认 6=生产口径, 训练剔除窗口"
                             "最后 N 个月; 0=全窗口。{0,3,6} 滚动多折样本外择优用)")
    parser.add_argument("--seed", type=int, default=42, help="单模型随机种子(多种子稳定性验证用)")
    parser.add_argument("--ensemble-seeds", default=",".join(map(str, ENSEMBLE_SEEDS)),
                        help="集成种子列表(逗号分隔, 默认 42,7,123,2024; 传 none 关集成=单模型)")
    args = parser.parse_args()
    ensemble = None if args.ensemble_seeds.lower() == "none" \
        else [int(x) for x in args.ensemble_seeds.split(",") if x.strip()]
    score_table = args.score_table or "model_score"
    importance_table = args.importance_table or "model_importance"
    # 实验表(非生产): 归档到独立目录, 不覆盖生产模型文件
    archive_dir = (DATA_DIR / "models" / "control") if args.score_table else None
    # 训练起点默认与日链同源(config data_start); 2026-09-11 起 data_start=null ⇒ 全历史,
    # 与日链 train_run(train_start=) 逐字一致。注意**只影响训练**, 评估窗走 eval_start。
    train_start = args.train_start
    if train_start is None:
        train_start = load_config().get("data_start") or None
    if str(train_start).lower() in ("none", "null", ""):
        train_start = None          # 显式指定: 全历史训练
    # 生产因子剔除（2026-09-27 第 6 次重钉接线; config factor_exclusions, 缺省 [] 不过滤）
    exclude_factors = load_config().get("factor_exclusions") or None
    setup_logger()
    db.init_db()
    run(train_months=args.train_months, backtest_start=args.backtest_start, full=args.full,
        train_start=train_start,
        score_table=score_table, importance_table=importance_table,
        archive_dir=archive_dir, num_boost_round=args.rounds,
        early_stopping_rounds=args.early_stop,
        seed=args.seed, ensemble_seeds=ensemble,
        holdout_recent=args.holdout_recent,
        exclude_factors=exclude_factors)


if __name__ == "__main__":
    main()
