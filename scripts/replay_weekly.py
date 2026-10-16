"""用**生产代码路径**回放周频袖套 —— 验证执行链路 + 生成 as-if 记录

## 为什么需要它

研究侧的收益读数来自独立模拟器(`experiments/05_label/F1b/F22`), 而**实盘走的是另一条代码路径**:
`app/weekly/plan.py` 出方案 → `app/weekly/ledger.py` 调 `paper.engine._exec_price` 成交。
两条路径若不一致, 回测再好看也不算数 —— 本项目已经因为这类"看着装好了其实不工作"的缺口
吃过两次亏(打分存档过期、09:32 拿不到开盘价)。**本脚本就是把生产路径按历史日期重放一遍**,
用真实日线开盘价成交, 检查它是否与研究侧读数同向同量级。

## 口径(如实声明)

- 这是 **as-if 回放**, 不是实盘记录: 成交价取历史日线开盘价(与生产晚间档同一函数);
- 出方案用生产 `plan.build`(但 `write=False`, **不覆盖盘中要用的实盘方案文件**);
- 账本落到独立文件(`data/weekly/ledger_replay.json`), 与实盘袖套账本隔离;
- 每个锚点回放后立刻在**其次一交易日**按下单规则成交(一字板/停牌顺延由生产函数决定);
- 不足 8 周不报年化(账本内置守则)。

用法:
    ./.venv/Scripts/python.exe -X utf8 scripts/replay_weekly.py --weeks 12
    ./.venv/Scripts/python.exe -X utf8 scripts/replay_weekly.py --from 20260601 --to 20260911
"""
import argparse
import logging
import sys
from pathlib import Path

import pandas as pd

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from app import config as cfg_mod            # noqa: E402
from app import db                           # noqa: E402
from app.weekly import plan as wk_plan       # noqa: E402
from app.weekly import scores as wk_scores   # noqa: E402
from app.weekly.ledger import Ledger         # noqa: E402

REPLAY_LEDGER = BASE / "data" / "weekly" / "ledger_replay.json"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="d0", default=None, help="起始锚点(YYYYMMDD)")
    ap.add_argument("--to", dest="d1", default=None, help="结束锚点(YYYYMMDD)")
    ap.add_argument("--weeks", type=int, default=12, help="不给 from 时, 回放最近 N 个锚点")
    ap.add_argument("--capital", type=float, default=None)
    ap.add_argument("--reset", action="store_true", help="清掉回放账本重新开始")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        datefmt="%H:%M:%S", force=True)
    cfg = cfg_mod.load_config()
    capital = wk_plan.sleeve_capital(cfg, a.capital)

    _, anchors, _, _, path = wk_scores.latest()
    anchors = pd.DatetimeIndex(sorted(anchors))
    if a.d0:
        anchors = anchors[anchors >= pd.Timestamp(a.d0)]
    if a.d1:
        anchors = anchors[anchors <= pd.Timestamp(a.d1)]
    if not a.d0:
        anchors = anchors[-a.weeks:]
    if len(anchors) == 0:
        print("(区间内没有锚点)")
        return
    print(f"回放 {len(anchors)} 个锚点: {anchors[0].date()} ~ {anchors[-1].date()} "
          f"| 打分存档 {path.name} | 资金 {capital:,.0f}")

    if a.reset and REPLAY_LEDGER.exists():
        REPLAY_LEDGER.unlink()
    led = Ledger.load(capital, REPLAY_LEDGER)
    conn = db.get_conn()
    try:
        for anchor in anchors:
            ad = anchor.strftime("%Y%m%d")
            nxt = pd.read_sql("SELECT MIN(trade_date) AS d FROM daily WHERE trade_date>?",
                              conn, params=(ad,))["d"].iloc[0]
            if nxt is None:
                print(f"  锚点 {ad}: 之后还没有交易日, 跳过(等下一个开盘日)")
                continue
            # 关键: held 必须取自**回放账本**(而不是实盘账本), 否则退出带用的是别人的持仓
            p = wk_plan.build(asof=ad, capital=capital, write=False,
                              ledger_path=REPLAY_LEDGER)
            done = led.execute_plan(p, str(nxt), use_quotes=False)
            nav = led.state["nav_history"][-1]["nav"]
            print(f"  锚点 {ad} → 成交日 {nxt}: {len(done):3d} 笔 | 目标 {p['n_target']} 只 "
                  f"(买不起丢弃 {p['dropped']['unaffordable']}) | 投出 {p['invested']:,.0f} "
                  f"| NAV {nav:,.0f}")
    finally:
        conn.close()

    snap = led.snapshot(benchmark=True)
    print("\n=== 回放结果(as-if, 生产代码路径, 历史日线开盘价成交) ===")
    from app.weekly.report import kpi_lines
    for line in kpi_lines(snap):
        print("  ", line)
    hist = led.state["nav_history"]
    if len(hist) >= 2:
        print("\n  净值序列:")
        for h in hist:
            print(f"    {h['date']}  NAV {h['nav']:>12,.0f}  持仓市值 {h['invested']:>12,.0f}")


if __name__ == "__main__":
    main()
