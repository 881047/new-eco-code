"""回测脚本: python scripts/run_backtest.py"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.logger import setup_logger
from app import db
from app.backtest.engine import run_backtest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--score-table", default="model_score",
                        help="读取评分的表(2026-09-05 排序实验已整体移除, 仅剩 model_score)")
    parser.add_argument("--tag", default="", help="结果 run_date 后缀, 防覆盖当日生产回测结果")
    parser.add_argument("--top-n", type=int, default=None,
                        help="持仓只数(默认取 config max_stocks; 对照组扫描用)")
    parser.add_argument("--state-stocks", default=None,
                        help="状态自适应只数 JSON(--top-n 显式指定时固定只数优先), "
                             "如 {\"attack\":6,\"neutral\":5,\"defense\":4}")
    parser.add_argument("--candidate-top", type=int, default=None,
                        help="补位候选池大小(默认取 config stock_candidate_top)")
    parser.add_argument("--industry-floor", type=float, default=None,
                        help="行业上限下界(至少容得下N只同行业, 默认取 config, R11-2 A/B 用)")
    parser.add_argument("--stop-loss", type=float, default=None,
                        help="日频止损阈值(如 -0.15; 默认 None=不模拟动态层, R13-1 A/B 用)")
    parser.add_argument("--hysteresis", type=int, default=None,
                        help="迟滞带(在册持仓未跌出 top(n+hyst) 不换, 默认取 config, R11-5 A/B 用)")
    parser.add_argument("--start", default=None,
                        help="评估窗口起始评分日 YYYYMMDD(walk-forward 样本外验证)")
    parser.add_argument("--end", default=None,
                        help="评估窗口结束评分日 YYYYMMDD(walk-forward 样本外验证)")
    parser.add_argument("--no-st-filter", action="store_true",
                        help="关闭候选池 PIT ST/退 过滤(修复前口径, 仅供归因 A/B)")
    args = parser.parse_args()
    setup_logger()
    db.init_db()
    state_stocks = json.loads(args.state_stocks) if args.state_stocks else None
    metrics = run_backtest(score_table=args.score_table, run_tag=args.tag,
                           top_n=args.top_n, state_stocks=state_stocks,
                           candidate_top=args.candidate_top,
                           start=args.start, end=args.end,
                           industry_floor=args.industry_floor,
                           stop_loss=args.stop_loss,
                           hysteresis=args.hysteresis,
                           st_filter=not args.no_st_filter)
    print(json.dumps(metrics, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
