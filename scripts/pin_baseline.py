"""重钉生产基线 pin —— 本脚本存在的理由: **之前没有它, 于是 pin 悄悄失效了。**

═══════════════════════════════════════════════════════════════════════════════
事故(2026-09-12 发现)
═══════════════════════════════════════════════════════════════════════════════
pin `20260911_fullhist` 一直是「现行代码能复现的那个数」。2026-09-12 D06/D07 限幅修复
进了 7 个生产文件(A→B = −0.17pp/年), **但没人重跑 pin**。后果:

  · `config.yaml:26 backtest_run: 20260911_fullhist` 描述的是代码已产不出的数字
    (23.97%/20.64%), 而现行代码产的是 23.77%/20.47%
  · `ab_const_n_5v6.py` 第 1 轮据此作废(预注册的闸门没通过, 按规则不许解读结果)
  · `exp_train_months.py:61 VALIDATE` 是同一形态, 下次运行会同样作废

根因不是「谁忘了改注释」—— 是**pin 没有再生路径**。于是有了这个脚本。

═══════════════════════════════════════════════════════════════════════════════
纪律(重要)
═══════════════════════════════════════════════════════════════════════════════
本脚本**只测量, 不决策**。它跑出两个窗口的读数并打印一段可直接粘进 `config.yaml`
的注释块, **不自动改 config** —— 换 pin 是「换成绩口径」, 按项目纪律要有意识地做,
不能在跑脚本时被顺手改掉。

它**不改任何策略参数**: 走的是现行 config 的生产路径(不传 top_n / state_stocks),
与日链回测同一条代码路径。

═══════════════════════════════════════════════════════════════════════════════
用法
═══════════════════════════════════════════════════════════════════════════════
  ./.venv/Scripts/python.exe scripts/pin_baseline.py            # 跑并打印
  ./.venv/Scripts/python.exe scripts/pin_baseline.py --tag X    # 自定义 run_tag 前缀

产出 run: `<YYYYMMDD>_<tag>`(prod 窗) 与 `<YYYYMMDD>_<tag>_full`(全史窗)。
**保留 NAV 不自清**(沿用项目纪律), 无论是否重钉都留着供复核。
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import config as cfg_mod
from app import db
from app.logger import setup_logger
from app.backtest import engine as E

setup_logger()
db.init_db()

_METRICS = [
    ("ann_return", "满仓年化", "{:+.2%}"),
    ("max_drawdown", "满仓回撤", "{:+.2%}"),
    ("sharpe", "满仓夏普", "{:.3f}"),
    ("sys_ann_return", "系统年化", "{:+.2%}"),
    ("sys_max_drawdown", "系统回撤", "{:+.2%}"),
    ("sys_mdd_daily", "系统日频回撤", "{:+.2%}"),
    ("ic_mean", "IC", "{:.4f}"),
    ("icir", "ICIR", "{:.3f}"),
    ("avg_stocks", "实持", "{:.2f}"),
    ("avg_cash_idle", "闲置", "{:.2%}"),
    ("avg_turnover", "换手", "{:.2%}"),
    ("periods", "期数", "{}"),
]


def comment_block(mp: dict, mf: dict, eval_start: str) -> list[str]:
    """生成"可直接粘进 config.yaml"的注释块（**纯函数**，不碰 IO —— 便于测试）。

    2026-09-13：从 `main()` 里抽出来（该改动一度被并行进程覆盖，已重新应用）。
    理由：这个块是**人肉粘进 config 的产物**，它的数字/格式错了就等于把错误基线写进注释；
    原来它埋在打印语句里，没有任何测试。
    """
    return [
        "# pin 现行基线(重钉于 {}): 恒定权益 0.873 + 固定 6 只 + 全历史训练 +".format(
            mp["run_date"].split("_")[0]),
        "# 评估窗 {} 起 —— **{} 期**, 满仓 {:.2%}/{:.2%}(夏普 {:.3f}), 系统".format(
            eval_start, mp["periods"], mp["ann_return"], mp["max_drawdown"], mp["sharpe"]),
        "# {:.2%}/{:.2%}(日频 {:.2%}), IC {:.4f}/ICIR {:.3f}, 实持 {:.2f}/闲置 {:.2%}/换手 {:.2%}。".format(
            mp["sys_ann_return"], mp["sys_max_drawdown"], mp["sys_mdd_daily"],
            mp["ic_mean"], mp["icir"], mp["avg_stocks"], mp["avg_cash_idle"], mp["avg_turnover"]),
        "# 全史窗({} 期): 满仓 {:.2%}/{:.2%}, 系统 {:.2%}/{:.2%}(日频 {:.2%})。".format(
            mf["periods"], mf["ann_return"], mf["max_drawdown"],
            mf["sys_ann_return"], mf["sys_max_drawdown"], mf["sys_mdd_daily"]),
        "# **口径: 不含 2017-10-11→2018-10-25, 即旧基线最大回撤 -41.90% 所在的那段**。",
    ]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="fullhist", help="run_tag(引擎会自动加日期前缀)")
    args = ap.parse_args()

    cfg = cfg_mod.load_config()
    eval_start = cfg.get("eval_start")
    data_start = cfg.get("data_start")
    # prod 窗 = eval_start 起(与 config.yaml 的 pin 同口径); full 窗 = 全史
    windows = [
        ("prod", args.tag, eval_start),
        ("full", args.tag + "_full", None),      # start=None ⇒ 全史(与 data_start 无关)
    ]
    print("=" * 96)
    print("重钉生产基线 —— eval_start={}  data_start={}".format(eval_start, data_start))
    print("=" * 96)

    out = {}
    t0 = time.time()
    for wname, run_tag, start in windows:
        m = E.run_backtest(run_tag=run_tag, start=start)
        out[wname] = (m["run_date"], m)
        print("\n[{:>5.0f}s] {} → run_date={} ({} 期)".format(
            time.time() - t0, wname, m["run_date"], m["periods"]))
        for key, label, fmt in _METRICS:
            if key in m:
                print("        {:<12} {}".format(label, fmt.format(m[key])))

    # ── 打印可直接粘进 config.yaml 的注释块(不自动写) ──────────────────────
    rd_p, mp = out["prod"]
    rd_f, mf = out["full"]
    print("\n" + "=" * 96)
    print("粘进 config.yaml 的注释块(请连同 backtest_run 一起有意识地改):")
    print("=" * 96)
    for line in comment_block(mp, mf, eval_start):
        print(line)
    print("backtest_run: {}".format(rd_p))
    print("=" * 96)
    print("\n留痕(保留不自清):")
    for wname, (rd, _m) in out.items():
        print("  {:<5} {}".format(wname, rd))
    print("\n⚠️ 本脚本不改 config.yaml —— 换 pin 请显式执行, 并同步:")
    print("   · app/config.py 的 data_start/eval_start 注释")
    print("   · scripts/oneoff/exp_train_months.py 的 VALIDATE")
    print("   · 记忆台账 new-money-evidence-ledger")
    print("   · README.md『关键设计』pin 头块(CLAUDE.md §1.3 第 3 步; 2026-09-28 补——")
    print("     第 6 次重钉漏更 README 的流程性根因)")
    print("   · docs/know/02 口径 + 08_pin台账 + test_pin_ledger_consistency 的")
    print("     STALE_VALUES 追加本次退役数值(否则守卫对新 pin 失明)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
