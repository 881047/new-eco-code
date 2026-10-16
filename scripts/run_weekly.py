"""周频终端命令行入口(2026-09-12)

用法:
    ./.venv/Scripts/python.exe -X utf8 scripts/run_weekly.py score
        # 训练周频打分(LightGBM/5 日标签/月度重训/4 种子) → data/weekly/scores_<日期>.npz
        # 耗时: 行情载入 ~5min + 因子 ~1min + 训练 ~5min(4 种子 × 144 期)

    ./.venv/Scripts/python.exe -X utf8 scripts/run_weekly.py plan [--capital N] [--held a,b]
        # 用最近一份打分出周频方案 → data/weekly/plan_<锚点>.json(不下单)

    ./.venv/Scripts/python.exe -X utf8 scripts/run_weekly.py show
        # 打印最近一份方案的摘要与前 20 行

设计边界: 本脚本只做「打分」与「出方案」,不写 `plan` 表、不下单。
接模拟盘是独立一步(需要用户先定资金规模)。
"""
import argparse
import json
import logging
import sys
from pathlib import Path

import pandas as pd

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from app.weekly import plan as wk_plan      # noqa: E402
from app.weekly import scores as wk_scores  # noqa: E402
from app.weekly.ledger import Ledger        # noqa: E402
from app import db                          # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_score = sub.add_parser("score", help="训练并保存周频打分")
    p_score.add_argument("--asof", default=None, help="存档日期标签(默认取最后一个锚点)")
    p_score.add_argument("--single", action="store_true",
                         help="只训单 die(5 日标签);默认是**多 die 集成**(5+21 日 + 生产月频, 见 F14/F15)")
    p_plan = sub.add_parser("plan", help="生成周频方案")
    p_plan.add_argument("--capital", type=float, default=None)
    p_plan.add_argument("--held", default="", help="当前持仓代码(逗号分隔), 用于宽出带")
    p_plan.add_argument("--asof", default=None, help="指定锚点(必须在打分存档内)")
    p_exec = sub.add_parser("exec", help="把最近一份方案落到**周频袖套账本**(独立于月频模拟盘)")
    p_exec.add_argument("--capital", type=float, default=None)
    p_exec.add_argument("--date", default=None, help="成交日(默认取方案锚点之后的第一个交易日)")
    p_exec.add_argument("--asof", default=None, help="用哪个锚点的方案(默认最近一份)")
    p_exec.add_argument("--dry-run", action="store_true", help="只显示将发生的成交, 不落账")
    p_kpi = sub.add_parser("kpi", help="打印周频袖套的当前状态与业绩")
    p_kpi.add_argument("--email", action="store_true",
                       help="同时发邮件(需 config 的 weekly.email=true 且 email 段已启用)")
    p_health = sub.add_parser("health", help="健康监控(滚动 IC / 换手 / 投入率 → 四档)")
    p_health.add_argument("--window", type=int, default=12, help="滚动锚点数(默认 12)")
    p_health.add_argument("--email", action="store_true", help="不健康时发告警邮件")
    p_attrib = sub.add_parser("attrib", help="损益归因(风格/行业/选股/现金/费用 —— 规划 §6.1-5)")
    p_attrib.add_argument("--window", type=int, default=52, help="最多用多少个净值点(默认 52)")
    p_attrib.add_argument("--json", action="store_true", help="输出原始 JSON")
    p_attrib.add_argument("--refresh", action="store_true",
                          help="同时写归因缓存(看板读这份; 19:45 档也会自动刷)")
    p_bic = sub.add_parser("blockic", help="块级 IC(六块各自的健康读数; D 案监控层, 2026-09-22)")
    p_bic.add_argument("--window", type=int, default=None, help="当前窗锚点数(默认 12)")
    p_bic.add_argument("--refresh", action="store_true",
                       help="同时写块级 IC 缓存(doctor/看板读这份; 19:45 档也会自动刷)")
    p_bic.add_argument("--json", action="store_true", help="输出原始 JSON")
    p_resume = sub.add_parser("resume", help="人工解除风控停买(立即生效; auto_resume 开启时"
                                             "恢复 healthy 后 19:45 档也会自动解除)")
    p_resume.add_argument("--reason", default="", help="解除理由(写进账本, 供事后审计)")
    p_doc = sub.add_parser("doctor", help="开跑前体检(只读): 下个执行档到底会不会动")
    p_doc.add_argument("--today", default=None, help="按哪个日期体检(默认今天)")
    p_doc.add_argument("--anchor", default=None, help="按哪个锚点查方案/打分(默认与 --today 相同)")
    p_rec = sub.add_parser("reconcile", help="成交核对(只读): 方案意图 vs 账本实际 vs 当日开盘价")
    p_rec.add_argument("--json", action="store_true", help="输出原始 JSON")
    p_pro = sub.add_parser("protocol", help="纸面→小资金 放行协议(只读): G1~G5 逐闸状态")
    p_pro.add_argument("--json", action="store_true", help="输出原始 JSON")
    sub.add_parser("show", help="打印最近一份方案")
    a = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        datefmt="%H:%M:%S", force=True)
    if a.cmd == "score":
        if a.single:
            p = wk_scores.run(asof=a.asof)
        else:
            p = wk_scores.run_multi(asof=a.asof)
        print(f"OK: {p}")
    elif a.cmd == "plan":
        held = [c.strip() for c in a.held.split(",") if c.strip()]
        out = wk_plan.build(asof=a.asof, capital=a.capital, held=held)
        e = out["expectation"]
        print(f"锚点 {out['anchor']} | 构型 top{out['top_n']}/band{out['band']}/bonus{out['hold_bonus']}"
              f" | {out['n_target']} 只(保留 {out['n_held']}) | 投出 {out['invested']:,.0f} "
              f"| 余 {out['cash_left']:,.0f} | {out.get('path')}")
        ann_txt = f"年化 {e['ann_pct']:+.1f}% | " if e.get("ann_pct") is not None else ""
        print(f"实测预期({e['basis']}): {ann_txt}超额 {e['excess_pp']:+.1f}pp "
              f"| MDD {e['mdd_pct']:.1f}% | 换手 {e['turnover']:.3f}")
        o = e.get("oos_24m")
        if o:      # F28: 只报全史 +14.6pp 是误导 —— 样本外哪一档必须一起出现
            # 2026-09-13: 样本外读数**逐敞口**存(F28 在 w=0.873, F41 在 w=1.0),
            # 所以"谁测的/在哪个敞口上测的"必须一起打印; p 校正值只有 F28 那份有(可能为 None)。
            who = o.get("measured_by", "F28")
            p_txt = (f"校正后 p={o['p_corrected']:.2f}, "
                     if o.get("p_corrected") is not None else "")
            inc = o.get("increment_t")
            inc_txt = (f", 增量 t={inc[0]}/{inc[1]}(满仓−旧敞口)" if inc else "")
            print(f"  ⚠️ 样本外 {o['window']}({who} 测于 w={o.get('measured_at_weight')}): "
                  f"超额 {o['excess_pp'][0]:+.2f}/{o['excess_pp'][1]:+.2f}pp, "
                  f"t={o['t_hac'][0]}/{o['t_hac'][1]}{inc_txt}, {p_txt}"
                  f"当前敞口 w={e.get('equity_weight')} ⇒ {o.get('note') or ''}")
        m_ = e.get("multiplicity")
        if m_:     # F38: 构型是从 ~25 个臂里选出来的 ⇒ 多重比较打折后不显著, 必须一起报
            print(f"  ⚠️ 多重比较(F38): DSR N=7 → {m_['dsr_n7'][0]}/{m_['dsr_n7'][1]}, "
                  f"N=25 → {m_['dsr_n25'][0]}/{m_['dsr_n25'][1]}（门槛 0.95）⇒ {m_['note']}")
        mo = e.get("monthly")
        if mo:     # 用户的目标函数是"月收益" ⇒ 月度那一行必须出现, 且必须带"多久才够判"
            f_, o_ = mo.get("full") or {}, mo.get("oos") or {}
            if f_.get("mean_pp") and o_.get("mean_pp"):
                print(f"  📅 月度口径({mo.get('measured_by')} 测于 w={mo.get('measured_at_weight')}): "
                      f"全史 月均 {f_['mean_pp'][0]:+.2f}/{f_['mean_pp'][1]:+.2f}pp"
                      f"(命中 {100*f_['hit'][0]:.0f}%, 月σ {f_['sd_pp']}pp) | "
                      f"留出集 月均 {o_['mean_pp'][0]:+.2f}/{o_['mean_pp'][1]:+.2f}pp"
                      f"(命中 {100*o_['hit'][0]:.0f}%, 月σ {o_['sd_pp']}pp)")
                if mo.get("months_for_2se_at_0_83pp"):
                    print(f"      ⚠ 要在 2×SE 下分辨 +10pp/年(≈+0.83pp/月) 需要约 "
                          f"{mo['months_for_2se_at_0_83pp']} 个月（≈6 年）⇒ 纸面 8~12 周"
                          f"在统计上什么都证明不了")
        r = e.get("single_die_reference")
        if r:      # 单 die 口径的周分布(含 2015-2016)做参照 —— 不是当前构型的数字
            print(f"  参照(单 die/{r['basis']}): 周均 {r['mean_week_pct']:+.3f}% | "
                  f"≥3% 的周 {100*r['weeks_ge_3pct']:.1f}% | 年化 {r['ann_pct']:+.1f}% "
                  f"| 超额 {r['excess_pp']:+.2f}pp MDD {r['mdd_pct']:.1f}%")
    elif a.cmd == "exec":
        if a.asof:
            target = wk_plan.PLAN_DIR / f"plan_{a.asof}.json"
            if not target.exists():
                print(f"(没有 {target.name}, 先跑 plan --asof {a.asof})")
                return
            plans = [target]
        else:
            plans = sorted(wk_plan.PLAN_DIR.glob("plan_*.json"))
        if not plans:
            print("(还没有周频方案, 先跑 plan)")
            return
        plan = json.loads(plans[-1].read_text(encoding="utf-8"))
        anchor = plan["anchor"]
        conn = db.get_conn()
        try:
            nxt = pd.read_sql("SELECT MIN(trade_date) AS d FROM daily WHERE trade_date>?",
                              conn, params=(anchor,))["d"].iloc[0]
            latest = pd.read_sql("SELECT MAX(trade_date) AS d FROM daily", conn)["d"].iloc[0]
        finally:
            conn.close()
        if a.date:
            exec_date = a.date
        elif nxt is None:
            print(f"方案锚点 {anchor} 之后还没有交易日(等下一个开盘日再跑 exec)")
            return
        else:
            exec_date = min(str(nxt), str(latest))
        cap = float(a.capital if a.capital is not None else plan.get("capital", 20000))
        led = Ledger.load(cap)
        done = led.execute_plan(plan, exec_date, dry_run=a.dry_run)
        print(f"袖套执行 | 方案锚点 {anchor} | 成交日 {exec_date} | "
              f"{'试跑(未落账)' if a.dry_run else '已落账'} {len(done)} 笔")
        for d in done[:15]:
            print(f"  {d['side']:<4} {d['ts_code']} {d['shares']:>7.0f} 股 @{d['price']:.3f} "
                  f"费用 {d['fee']:.2f} ({d['reason']})")
        snap = led.snapshot(benchmark=False)
        print(f"  账本: NAV {snap.get('nav', snap['cash']):,.0f} | 持仓 {snap['n_positions']} 只 "
              f"| 现金 {snap['cash']:,.0f} | 累计成交 {snap['n_trades']} 笔")
    elif a.cmd == "kpi":
        # 账本还没建时(首次执行前)按**配置里的袖套资金**报, 不要报 0 —— 否则邮件里
        # "袖套资金 0" 会让人以为配置没生效
        from app import config as _cfg
        snap = Ledger.load(wk_plan.sleeve_capital(_cfg.load_config())).snapshot()
        from app.weekly.report import kpi_lines
        for line in kpi_lines(snap):
            print(f"  {line}")
        if getattr(a, "email", False):
            # 2026-09-18 邮件优化: 袖套 KPI 独立邮件已取消（内容并入 18:30 日终总结的
            # 「周频袖套」小节），所以 --email 在这个子命令上不再有任何出口，只说明去向。
            print("  邮件: 袖套 KPI 已并入 18:30「今日市场与账户总结」，不再单独发送；"
                  "要看告警类邮件请用 health --email")
    elif a.cmd == "health":
        from app.weekly import health as wk_health
        res = wk_health.current(window=a.window)
        icon = {"healthy": "🟢", "warning": "🟡", "degrading": "🟠", "dead": "🔴"}.get(
            res.get("level"), "⚪")
        print(f"{icon} 健康度: {res.get('level')}")
        for k in ("rolling_ic", "neg_streak", "ic_n", "turnover_est", "turnover_n",
                  "turnover_min_days", "deployed_ratio",
                  "n_nav_points", "n_trades", "note", "error"):
            if k in res:
                print(f"   {k:15s} {res[k]}")
        for r in res.get("reasons", []):
            print(f"   ⚠ {r}")
        if getattr(a, "email", False) and res.get("level") not in (None, "healthy", "unknown"):
            from app.weekly.report import send_health_email
            print("  告警邮件:", "已发送" if send_health_email(res) else "未发送(weekly.email/email 未启用)")
    elif a.cmd == "attrib":
        from app.weekly import attrib as wk_attrib
        res = (wk_attrib.refresh() if getattr(a, "refresh", False)
               else wk_attrib.current(window=a.window))
        if getattr(a, "json", False):
            print(json.dumps(res, ensure_ascii=False, indent=1, default=str))
        else:
            for line in wk_attrib.lines(res):
                print(f"  {line}")
    elif a.cmd == "blockic":
        from app.weekly import health as wk_health
        from app.weekly.report import block_ic_lines
        kw = {} if a.window is None else {"window": a.window}
        res = (wk_health.block_ic_refresh(**kw) if getattr(a, "refresh", False)
               else wk_health.block_ic(**kw))
        if getattr(a, "json", False):
            print(json.dumps(res, ensure_ascii=False, indent=1, default=str))
        else:
            for line in block_ic_lines(res):
                print(f"  {line}")
    elif a.cmd == "resume":
        from app import timeutil
        from app.weekly import risk as wk_risk
        from app.weekly.ledger import DEFAULT_PATH
        # 2026-09-28 审查修复: 账本文件不存在时原 Ledger.load(0.0) 会凭空构造 0 资金
        # 账本并经 clear() 落盘成**毒账本**(此后执行档永远按 0 资金运行) —— 先查文件,
        # 不存在直接提示退出(risk.clear 未停买短路 + Ledger.save 毒账本守卫是后两道闸)。
        if not Path(DEFAULT_PATH).exists():
            print(f"账本不存在({DEFAULT_PATH}) —— 尚无袖套在跑, 无停买可解除")
            return
        led = Ledger.load(0.0)
        before = wk_risk.is_halted(led)
        out = wk_risk.clear(led, timeutil.date8(), a.reason)
        print(f"风控停买: {'已解除' if before else '本来就是正常状态'} | {out}")
    elif a.cmd == "doctor":
        from app.weekly import doctor as wk_doctor
        items = wk_doctor.checks(today=a.today, anchor=a.anchor)
        for line in wk_doctor.render(items):
            print(f"  {line}")
        bad = sum(1 for it in items if not it["ok"])
        print(f"  —— {len(items) - bad}/{len(items)} 项通过" + ("（有 ✗，照着 → 修）" if bad else ""))
    elif a.cmd == "reconcile":
        from app.weekly import reconcile as wk_rec
        res = wk_rec.report()
        if getattr(a, "json", False):
            print(json.dumps(res, ensure_ascii=False, indent=1, default=str))
        else:
            for line in wk_rec.lines(res):
                print(f"  {line}")
    elif a.cmd == "protocol":
        from app.weekly import protocol as wk_proto
        res = wk_proto.status()
        if getattr(a, "json", False):
            print(json.dumps(res, ensure_ascii=False, indent=1, default=str))
        else:
            for line in wk_proto.lines(res):
                print(f"  {line}")
    else:
        plans = sorted(wk_plan.PLAN_DIR.glob("plan_*.json"))
        if not plans:
            print("(还没有周频方案, 先跑 plan)")
            return
        d = json.loads(plans[-1].read_text(encoding="utf-8"))
        print(f"{plans[-1].name} | 锚点 {d['anchor']} | 构型 top{d['top_n']}/band{d['band']} "
              f"| {d['n_target']} 只 | 投出 {d['invested']:,.0f}/{d['capital']:,.0f}")
        for r in d["rows"][:20]:
            print(f"  {r['ts_code']} {r['name'][:8]:<8} 排名{r['rank']:>5} 分{r['score']:.4f} "
                  f"{r['shares']:>6} 股 @{r['close']:.2f} 权重{r['weight']:.3f}"
                  f"{' [保留]' if r['held'] else ''}")


if __name__ == "__main__":
    main()
