"""资金规模扫描 —— 逐笔真实费用口径(2026-09-11 第五轮)

背景(为什么这个实验必须重做):
项目测过两次「资金做大」, 两次都在**扁平成本**下:
  ab_capital_diversify.py  10 万 @单边 0.3%  → 4.07%/-68.6%(n6), 结论「更差」
  ab_capital_cost.py       10 万 @单边 0.1%  → 结论「仅小幅改善」
但 0.3% 是**为 1 万本金校准的**(5 元 / 单笔约 1600 元 = 0.31%), 0.1% 是**为 10 万校准的**。
两者都是**扁平**费率 —— 而真实费用里的 5 元最低佣金是**固定成本**, 其百分比惩罚 = 5/单笔金额,
**随资金放大而自动缩小**。扁平费率把这条最重要的规模经济抹平了, 因此两次实验对
「资金做大」都**结构性不利**。该结论不能继承。

用户 2026-09-11 确认**券商没有免五** ⇒ 5 元地板是永久约束, 唯一能降低其百分比咬合的
就是放大单笔金额。故必须用 fees.py 的逐笔真实费率重测。

另: 2 万下 0.873 名义权益实际只投出约 61%(整手粒度+行业上限), 单只预算约 2900 元;
资金放大同时修复「想投 87% 只投得出 61%」这个欠配问题 —— 两个效应同向, 本实验一并量出。

臂(窗口 20190722 起 = 现行 pin, 其余全为现行生产配置, 只动 capital):
  2万(现状) / 3万 / 5万 / 10万 / 20万, 均 6 只
  cap20w_free  20 万 + 免五  ← 分离「规模效应」与「地板效应」的参考点
真费用引擎下 `single_side_cost` 已退化为整手分配预算预留(非成本), 各臂一律不动。

跑完自清本次 run。
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app import config as cfg_mod
from app import db
from app.backtest import engine as E
from app.logger import setup_logger

setup_logger()
db.init_db()

W19 = "20190722"
_orig = cfg_mod.load_config
TAGS = []


def _patched(capital, free=False):
    def f(*a, **k):
        cfg = _orig(*a, **k)
        cfg["capital"] = capital
        if free:
            cfg["fees"] = dict(cfg.get("fees") or {}, min_commission=0.0)
        return cfg
    return f


ARMS = [
    ("cap2w", 20000, False, "现状: 2万, 佣金 max(万2.5, 5元)"),
    ("cap3w", 30000, False, "3万"),
    ("cap5w", 50000, False, "5万"),
    ("cap10w", 100000, False, "10万"),
    ("cap20w", 200000, False, "20万"),
    ("cap20w_free", 200000, True, "20万 + 免五(分离规模效应与地板效应)"),
]

out = []
t0 = time.time()
try:
    for tag, cap, free, note in ARMS:
        cfg_mod.load_config = _patched(cap, free)
        m = E.run_backtest(run_tag=tag, start=W19)
        cfg_mod.load_config = _orig
        rd = m["run_date"]
        TAGS.append(rd)
        row = {"arm": tag, "capital": cap, "free": free, "note": note,
               "periods": m["periods"], "ann": m["ann_return"], "mdd": m["max_drawdown"],
               "sys_ann": m["sys_ann_return"], "sys_mdd": m["sys_max_drawdown"],
               "sys_mdd_daily": m["sys_mdd_daily"], "sharpe": m.get("sharpe"),
               "idle": m["avg_cash_idle"], "n_avg": m["avg_stocks"],
               "turnover": m.get("avg_turnover")}
        out.append(row)
        print("[{:>4.0f}s] {}".format(time.time() - t0,
                                      json.dumps(row, ensure_ascii=False)), flush=True)
finally:
    cfg_mod.load_config = _orig

# ⚠️ 2026-09-13 更正（独立审查 §4.4）：这是**当时的** pin（2 万、起点 20190722），不是现行 ——
# ⚠️ 2026-09-28 时效注记: 本脚本写作时的"现役 pin"=20260912_limitquant(22.62%/19.40%),
# 现役 pin 已重钉至 20260928_exe(31.68%/29.13%, 全史日频 -33.58%) —— 文中 pin 读数均为历史档, 非现行值。
# 现役 pin(`backtest_run=20260912_limitquant`)：满仓 22.62%/-24.10%、系统 19.40%/-19.66%（85 期）。
# 常量是历史判据、不许改。
PIN = {"ann": 0.1659, "mdd": -0.2410, "sys_ann": 0.1542, "sys_mdd": -0.2126}
base = next(r for r in out if r["arm"] == "cap2w")
ok = all(abs(base[k] - v) < 5e-4 for k, v in PIN.items())

print()
print("=" * 112)
print("资金规模扫描 —— 逐笔真实费用, 窗口 20190722 起, 6 只, 其余为现行生产配置")
print("=" * 112)
print("2万 臂复现 pin 登记值: {}  (实跑 {:.4f}/{:.4f}/{:.4f}/{:.4f})".format(
    "是" if ok else "**否 —— 本轮结论不可用**",
    base["ann"], base["mdd"], base["sys_ann"], base["sys_mdd"]))
print()
print("{:>12} {:>8} {:>6} {:>9} {:>9} {:>9} {:>9} {:>8} {:>8} {:>8}".format(
    "臂", "资金", "期数", "满仓年化", "满仓回撤", "系统年化", "系统回撤", "夏普", "闲置", "实持"))
print("-" * 112)
for r in out:
    print("{:>12} {:>8.0f} {:>6} {:>+8.2%} {:>9.2%} {:>+8.2%} {:>9.2%} {:>8} {:>8.1%} {:>8.2f}".format(
        r["arm"], r["capital"], r["periods"], r["ann"], r["mdd"], r["sys_ann"],
        r["sys_mdd"], "{:.3f}".format(r["sharpe"]) if r["sharpe"] is not None else "-",
        r["idle"], r["n_avg"]))
print()
print("相对现状(2万)的系统线增量:")
for r in out[1:]:
    print("  {:>12}  年化 {:>+6.2f}pp   回撤 {:>+6.2f}pp   闲置 {:>+5.1f}pp   实持 {:>+5.2f} 只"
          .format(r["arm"], (r["sys_ann"] - base["sys_ann"]) * 100,
                  (r["sys_mdd"] - base["sys_mdd"]) * 100,
                  (r["idle"] - base["idle"]) * 100, r["n_avg"] - base["n_avg"]))
print()
print("注: cap20w vs cap20w_free 的差 = 20 万规模下 5 元地板的残余咬合")
print("    (2 万下该值已单独测得 = +2.35pp, 见 exp_fee_floor.py)")

conn = db.get_conn()
for t in TAGS:
    for tbl in ("backtest_summary", "backtest_nav", "backtest_nav_daily"):
        conn.execute("DELETE FROM {} WHERE run_date=?".format(tbl), (t,))
conn.commit()
conn.close()
print("已清理 {} 个临时 run".format(len(TAGS)))
