"""周频袖套的调度入口: **锚点后首个交易日 09:32** 自动"出方案 + 成交"

## 判定"今天该不该动"的规则(纯日历, 不依赖任何行情判断)

    锚点 = 上一个已完结 ISO 周的**最后交易日**(周五; 遇节假日顺延到该周最后开市日)
    触发 = 今天所在 ISO 周 ≠ 上一个交易日所在 ISO 周

即:**只要今天是新一周的第一个交易日, 就执行上一周锚点的方案**。
节假日周(整周只有 1 个交易日)、跨年周都由 ISO 周定义自然处理, 不需要额外特判。

## 安全设计(与项目其余新增链路一致)

- **默认关闭**: `config.yaml` 的 `weekly.enabled` 缺省即 false ⇒ 生产行为逐位不变;
  打开它之前, 不会产生任何订单、任何文件写入(函数在读日历**之前**就返回);
- **幂等**: 同一成交日重复运行不会重复下单(账本按成交日校验, 见 `_already_done`);
- **不抛出**: 任何异常只记日志并返回 error 字段, 绝不影响月频链(调度器 `_guarded` 之外再兜一层);
- **不融资、不顺延加价**: 成交规则全部来自 `ledger`(它调用生产 `_exec_price`)。
"""
from __future__ import annotations

import datetime
import json
import logging

import pandas as pd

from app import config as cfg_mod
from app import timeutil
from app.weekly import plan as wk_plan
from app.weekly.errors import ArchiveMiss
from app.weekly.ledger import Ledger

log = logging.getLogger(__name__)


def iso_week(d) -> tuple:
    t = datetime.date(int(str(d)[:4]), int(str(d)[4:6]), int(str(d)[6:8]))
    y, w, _ = t.isocalendar()
    return (y, w)


def is_first_session_after_anchor(prev_open: str, today: str) -> bool:
    """`prev_open` = 今天之前的最后一个开市日。判据: 两者不在同一 ISO 周。"""
    return iso_week(prev_open) != iso_week(today)


def _open_days(conn, today: str) -> list:
    return [r[0] for r in conn.execute(
        "SELECT cal_date FROM trade_cal WHERE is_open=1 AND cal_date<=? "
        "ORDER BY cal_date DESC LIMIT 30", (today,))]


def _already_done(led: Ledger, trade_date: str) -> bool:
    return any(t.get("date") == trade_date for t in led.state.get("trades", []))


# ---------------------------------------------------------------- 打分保鲜
def is_week_last_open(conn, d: str) -> bool:
    """`d` 是否是**它所在 ISO 周的最后一个开市日**(锚点的定义)。

    用 `trade_cal`(已覆盖到 2027)往后看: 只要同一 ISO 周内还有开市日, 它就不是锚点。
    这条判据让"周五晚上跑刷新"能正确识别出当天就是新锚点 ——
    若改用"和今天比 ISO 周"的写法, 周六跑时会误判成上周五(实测踩过这一类的坑)。
    """
    y, w = iso_week(d)
    for (x,) in conn.execute("SELECT cal_date FROM trade_cal WHERE is_open=1 AND cal_date>? "
                             "ORDER BY cal_date LIMIT 10", (d,)):
        return iso_week(x) != (y, w)          # 下一开市日跨周 ⇒ d 是本周最后
    return True


def newest_anchor(conn, today: str = None) -> str:
    """最近一个**已完成的锚点**(≤ today 的、属于某 ISO 周最后开市日的最大交易日)。"""
    today = str(today or timeutil.date8())
    rows = [r[0] for r in conn.execute(
        "SELECT DISTINCT trade_date FROM daily WHERE trade_date<=? ORDER BY trade_date DESC "
        "LIMIT 30", (today,))]
    for d in rows:
        if is_week_last_open(conn, d):
            return d
    return None


def run_score_slot(today: str = None, cfg: dict = None, conn=None,
                   train_fn=None) -> dict:
    """**打分保鲜档**(每日 19:30): 若最新打分存档没覆盖最新锚点, 就重训一次。

    没有它, 下个锚点出方案时会因"存档未覆盖该锚点"而失败 ⇒ 终端静默不交易。
    幂等: 已覆盖即跳过(代价只有一次 npz 读取 + 一次日历查询)。
    """
    cfg = cfg or cfg_mod.load_config()
    if not (cfg.get("weekly") or {}).get("enabled", False):
        return {"skipped": "weekly.enabled=false"}
    own = conn is None
    if own:
        from app import db
        conn = db.get_conn()
    try:
        anchor = newest_anchor(conn, today)
        if anchor is None:
            return {"skipped": "日历/行情不足"}
        from app.weekly import scores as wsc
        try:
            _, anchors, _, _, path = wsc.latest()
        except FileNotFoundError:
            anchors, path = pd.DatetimeIndex([]), None
        have = {d.strftime("%Y%m%d") for d in anchors}
        if anchor in have:
            return {"skipped": f"打分已覆盖锚点 {anchor}", "anchor": anchor}
        log.info("打分存档最新 %s, 锚点 %s 未覆盖 ⇒ 重训",
                 (path.name if path is not None else (max(have) if have else "(无)")), anchor)
        train_fn = train_fn or (lambda: wsc.run_multi())
        p = train_fn()
        return {"retrained": True, "anchor": anchor, "path": str(p)}
    except Exception as e:
        log.exception("打分保鲜档异常")
        return {"error": str(e)}
    finally:
        if own:
            conn.close()


def ensure_plan(anchor: str, cfg: dict, build_plan_fn=None, train_fn=None) -> dict:
    """保证 `plan_<anchor>.json` 存在；缺则用**标准建造器**现建（2026-09-14 链式自愈）。

    ## 为什么要它（07 号 #50）

    改造前全项目**只有一个自动建造点** —— 周一 09:32 的 `run_open_slot`；晚间档
    （`pipeline.py` 的 `run_evening_slot`）与**开机补跑**都只执行、不建造。后果是一条
    安静的整周空仓路径：周五 19:30 打分保鲜因当日行情未及时入库而跳过 ⇒ 周一 09:32
    出方案时存档不覆盖锚点 ⇒ 抛错、不产生方案 ⇒ 之后所有档都"无方案，跳过"，看门狗
    只发一封邮件，`plan_<anchor>.json` 永远不会被自动补建。

    ## 口径（不变的部分与新增的部分）

    - 用的是**同一个** `wk_plan.build` 与同一份打分存档 —— 不新增任何选股口径；
    - **重训只发生在必要处**：仅当建造失败原因是"**打分存档不足以服务该锚点**"时补跑一次
      `run_score_slot`（幂等：已覆盖即秒退），再重建一次；其它错误直接如实返回。
      ⚠️ 2026-09-14 外部复核 D4：判据从"含'存档'两个字"收紧为**显式两条消息**
      （`plan.py` 的"不在打分存档里"与 `scores.py` 的"没有周频打分存档"）—— 原来任何
      含"存档"的异常都会在**调用方的锁里**买一次 6~8 分钟重训，属"字符串太松"的静默行为。
    - 成交价仍由各档自己决定：晚间档/补跑按**当日开盘价** ⇒ 属 **as-if 记账**
      （18:05 做的决策、按当天开盘价入账），与 18:05 档原本的语义一致；
    - `build_plan_fn` / `train_fn` 可注入 ⇒ 测试不会真训练、不会写生产存档。

    返回：`{"ok", "path", "built", "retrained", "error"}`。
    """
    p = wk_plan.PLAN_DIR / f"plan_{anchor}.json"      # 调用时解析（测试会 patch PLAN_DIR）
    if p.exists():
        try:
            json.loads(p.read_text(encoding="utf-8"))
            return {"ok": True, "path": str(p), "built": False, "retrained": False}
        except Exception as e:                        # 文件在但不可解析 ⇒ 按缺失重建
            log.error("方案文件不可解析, 按缺失重建: %s (%s)", p.name, e)

    # 2026-09-18 审查 Q-04：这条判据原来靠**中文文案子串**（`("不在打分存档", "没有周频打分存档")`）
    # 识别，而生产者只是内联 f-string、没有任何测试钉住措辞 —— 谁重构一句报错文案，
    # 0 条测试变红，代价却是整周不出方案（袖套 0 成交）。生产者（plan.build / scores.latest）
    # 现在抛 `ArchiveMiss`，判据改成 isinstance：**措辞随便改，类型不会变**。
    # 07 号 #50 与 2026-09-14 外部复核 D4 都只把子串收紧，没换通道 —— 这次换掉。
    build = build_plan_fn or wk_plan.build
    capital = wk_plan.sleeve_capital(cfg)

    def _try_build() -> bool:
        build(asof=anchor, capital=capital)
        return p.exists()

    retrained = False
    try:
        built = _try_build()
    except Exception as e:
        if not isinstance(e, ArchiveMiss):
            log.exception("锚点 %s 出方案失败(非存档原因)", anchor)
            return {"ok": False, "path": str(p), "built": False, "retrained": False,
                    "error": str(e)}
        log.warning("锚点 %s 未被打分存档覆盖 ⇒ 补跑一次打分保鲜后重建", anchor)
        run_score_slot(today=anchor, cfg=cfg, train_fn=train_fn)     # 幂等
        retrained = True
        try:
            built = _try_build()
        except Exception as e2:
            log.exception("补训后仍出方案失败: %s", anchor)
            return {"ok": False, "path": str(p), "built": False, "retrained": True,
                    "error": str(e2)}
    if not built:
        return {"ok": False, "path": str(p), "built": False, "retrained": retrained,
                "error": f"出方案后仍无文件: {p.name}"}
    return {"ok": True, "path": str(p), "built": True, "retrained": retrained}


def run_open_slot(trade_date: str = None, cfg: dict = None, ledger_path=None,
                  open_days_fn=None, build_plan_fn=None, train_fn=None) -> dict:
    """执行档入口。返回摘要 dict(便于调度日志/测试断言)。"""
    cfg = cfg or cfg_mod.load_config()
    w = cfg.get("weekly") or {}
    if not w.get("enabled", False):
        return {"skipped": "weekly.enabled=false"}          # ← 在读日历之前返回(零副作用)
    today = str(trade_date or timeutil.date8())
    try:
        if open_days_fn is not None:
            days = open_days_fn(today)
        else:
            from app import db
            conn = db.get_conn()
            try:
                days = _open_days(conn, today)
            finally:
                conn.close()
        if not days or days[0] != today:
            return {"skipped": f"{today} 不是开市日(或行情日历未覆盖)"}
        prev_open = days[1] if len(days) > 1 else None
        if prev_open is None:
            return {"skipped": "日历不足两天"}
        if not is_first_session_after_anchor(prev_open, today):
            return {"skipped": f"{today} 与上一开市日 {prev_open} 同属一个 ISO 周, 非执行日"}
        anchor = prev_open
        p = wk_plan.PLAN_DIR / f"plan_{anchor}.json"
        if not p.exists():
            log.info("周频袖套: 锚点 %s 尚无方案, 现算（缺存档时自动补训一次）", anchor)
        # 2026-09-14: 建造走 ensure_plan —— 否则"服务没起/存档没覆盖"这类失败
        # 会让整周没有方案，而晚间档与开机补跑都只执行、不建造（07 #50）。
        ready = ensure_plan(anchor, cfg, build_plan_fn=build_plan_fn, train_fn=train_fn)
        if not ready["ok"]:
            return {"error": f"锚点 {anchor} 出方案失败: {ready.get('error')}"}
        import json
        plan = json.loads(p.read_text(encoding="utf-8"))
        led = Ledger.load(float(plan.get("capital") or wk_plan.sleeve_capital(cfg)), ledger_path)
        if _already_done(led, today):
            return {"skipped": f"{today} 已执行过(幂等)"}
        # 2026-09-28 审查修复(P1 第二道闸, 生产调用方守卫): 空目标集 ≠ 决定清仓 ——
        # 锚点日行情整日缺失的历史坏方案/手工方案若 rows=[] 且账本有持仓, execute_plan
        # 会把**全部持仓**按当日价清空(卖出不受 risk_halt/governor 拦截)。plan.build
        # 源头已 raise 拒绝出空方案, 这里对执行路径兜底(与月盘 paper A7 同语义)。
        # 账本层保留"空 rows=清仓"原语契约(测试/reconcile 复放用), 闸只设在生产入口。
        if not plan.get("rows") and led.state["positions"]:
            return {"error": (f"锚点 {anchor} 方案目标为空而账本有 "
                              f"{len(led.state['positions'])} 只持仓 —— 拒绝执行"
                              f"(空目标集 != 决定清仓)")}
        from app.weekly import risk as wk_risk                     # §6.1-4 风控停买
        halt = wk_risk.is_halted(led)
        gov = wk_risk.governor(led, cfg)          # 回撤治理(2026-09-25): 谨慎态停开新仓
        done = led.execute_plan(plan, today, use_quotes=True,
                                allow_buys=halt is None and not gov.get("cautious"))
        snap = led.snapshot(benchmark=False)
        # 2026-09-18 邮件优化: 袖套成交/净值不再单独发信 —— 由 18:30 日终总结的
        # 「周频袖套」小节承载（`notify._sleeve_section` 直接读同一份账本，不另存一份数）。
        log.info("周频袖套 %s 执行完成: %d 笔 | NAV %.0f | 持仓 %d",
                 today, len(done), snap.get("nav", 0.0), snap.get("n_positions", 0))
        return {"anchor": anchor, "trade_date": today, "trades": len(done),
                "nav": snap.get("nav"), "positions": snap.get("n_positions"),
                "halted": bool(halt), "halt_reason": (halt or {}).get("reason"),
                "governor": gov.get("reason") if gov.get("cautious") else None}
    except Exception as e:                                   # 绝不向上抛(不影响月频链)
        log.exception("周频袖套执行档异常")
        return {"error": str(e)}


def run_evening_slot(trade_date: str = None, cfg: dict = None, ledger_path=None,
                     open_days_fn=None, build_plan_fn=None, train_fn=None) -> dict:
    """**晚间兜底档(18:05)**: 用日线开盘价把开盘档没成交的部分补上。

    与开盘档**同一套账、同一个方案、同一个成交价口径(当日开盘价)** ——
    只是数据来源从实时行情换成已入库的日线, 所以两档账务等价(生产模拟盘同款设计)。
    幂等: 目标差额为 0 时不产生任何成交。

    ⚠️ 2026-09-14（07 #50）：**本档现在会自己建方案**（缺 `plan_<anchor>.json` 时走
    `ensure_plan`）—— 这**推翻了原先刻意钉住的安全规则**（"锚点存在但没有方案文件 ⇒ 跳过，
    不许凭分数现建仓"）。理由：原规则让"09:32 那一档失败"变成**整周不可恢复**（晚间档与
    开机补跑都只执行不建造）；而锚点严格早于本周，所有输入 ≤ 锚点 ⇒ 不是前视，只是
    **as-if 记账**（18:05 做的决策、按当天开盘价入账），与 18:05 档原有语义一致。
    对应地，`tests/test_weekly_evening_slot.py` 里那条旧断言按新语义重写（保留其精神：
    建不出来时仍然跳过、且**不许**建账本）。
    """
    cfg = cfg or cfg_mod.load_config()
    if not (cfg.get("weekly") or {}).get("enabled", False):
        return {"skipped": "weekly.enabled=false"}
    today = str(trade_date or timeutil.date8())
    try:
        if open_days_fn is not None:
            days = open_days_fn(today)
        else:
            from app import db
            conn = db.get_conn()
            try:
                days = _open_days(conn, today)
            finally:
                conn.close()
        if not days or days[0] != today:
            return {"skipped": f"{today} 不是开市日"}
        # 只认**当前周锚点**的方案: 若今天就是锚点(周五), 则锚点=上一个; 否则=上一个已完结周
        anchor = None
        for d in days[1:]:
            if iso_week(d) != iso_week(today):
                anchor = d
                break
        if anchor is None:
            return {"skipped": "本周尚无锚点(新周期未开始)"}
        p = wk_plan.PLAN_DIR / f"plan_{anchor}.json"
        # 2026-09-14（07 #50）：缺方案 ⇒ 先走 ensure_plan 现建（见函数 docstring 的口径说明）；
        # 建不出来才跳过，且**不许**在此之前碰账本（保持"没方案不建账"的旧精神）。
        ready = ensure_plan(anchor, cfg, build_plan_fn=build_plan_fn, train_fn=train_fn)
        if not ready["ok"]:
            return {"skipped": f"锚点 {anchor} 无方案且现建失败({ready.get('error')}), 晚间档不建仓"}
        import json
        plan = json.loads(p.read_text(encoding="utf-8"))
        led = Ledger.load(float(plan.get("capital") or wk_plan.sleeve_capital(cfg)), ledger_path)
        # 2026-09-28 审查修复(P1 第二道闸): 与开盘档同语义 —— 空目标集 ≠ 决定清仓
        if not plan.get("rows") and led.state["positions"]:
            return {"error": (f"锚点 {anchor} 方案目标为空而账本有 "
                              f"{len(led.state['positions'])} 只持仓 —— 拒绝执行"
                              f"(空目标集 != 决定清仓)")}
        from app.weekly import risk as wk_risk                     # §6.1-4 风控停买
        halt = wk_risk.is_halted(led)
        gov = wk_risk.governor(led, cfg)          # 回撤治理(2026-09-25): 谨慎态停开新仓
        done = led.execute_plan(plan, today, use_quotes=False,
                                allow_buys=halt is None and not gov.get("cautious"))
        # 报表出口同执行档：不在这里发信，晚间 18:30 的日终总结统一承载袖套读数
        return {"anchor": anchor, "trade_date": today, "trades": len(done),
                "halted": bool(halt), "halt_reason": (halt or {}).get("reason"),
                "governor": gov.get("reason") if gov.get("cautious") else None}
    except Exception as e:
        log.exception("周频袖套晚间档异常")
        return {"error": str(e)}
