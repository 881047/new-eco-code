"""周频终端**开跑前体检**（2026-09-13）—— 只读，不改任何状态。

## 为什么需要

终端是**无人值守**的：某个调度档因为"打分存档过期 / 方案缺失 / 账本损坏 / 日历没覆盖 /
磁盘满"而静默空转，从日志里很难一眼看出来 —— 而空转的代价是**整周没有仓位**。
`doctor` 把"下个执行档到底会不会动"变成一条命令（`run_weekly.py doctor`）。

## 设计

- **只读**：不写账本、不写方案、不发邮件、不下单；任何检查抛异常都只记成该条 ✗；
- 每条给 `ok/detail/fix` 三段：✗ 的时候**必须**能照着 fix 修；
- **18 项**（2026-09-22 起，原 14 项 + 四块「块级 IC」；项数由 `tests/test_weekly_doctor.py`
  钉死，加/减一项必须同步 README 与知识库，否则那个"看起来一切都好"的清单会自己烂掉）：
  总开关、运行参数、下个执行档、交易日历覆盖、打分存档、**模型新鲜度**、**块级 IC（B1~B4）**、
  方案文件、账本可读、风控停买状态、执行档看门狗、阶段台账收口、邮件送达、备份新鲜度、磁盘余量。
"""
from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path

import pandas as pd

from app import config as cfg_mod
from app import timeutil
from app.weekly import blocks as wk_blocks
from app.weekly import health as wk_health
from app.weekly import plan as wk_plan
from app.weekly import scores as wk_scores
from app.weekly.ledger import Ledger

log = logging.getLogger(__name__)
DATA = Path(__file__).resolve().parent.parent.parent / "data"


def _ok(name: str, ok: bool, detail: str, fix: str = "") -> dict:
    return {"name": name, "ok": bool(ok), "detail": detail, "fix": fix if not ok else ""}


_LEDGER_FIX = ("从账本的滚动备份 `data/weekly/ledger.json.bak` 或按日快照 "
               "`data/weekly/backup/ledger_YYYYMMDD.json` 恢复（不要手改 JSON），"
               "再用 `run_weekly.py doctor` 复核；细则见 03 号运行手册")


def ledger_or_error(ledger_path=None):
    """读袖套账本 → `(ledger, None)`；**文件存在但读不动**时 `(None, exception)`。

    2026-09-14（07 #48 同族）：原来三处（`check_ledger` / `check_risk` / `check_exec_ran`）
    各自裸调 `Ledger.load`，账本损坏时异常穿出、最后落到体检的通用分支
    `(体检项异常): …`，fix 文案指向 doctor 自己的日志 —— 而当时真正该说的是"从备份恢复"。
    文件**不存在**不算错误（首次运行合法，`Ledger.load` 自己会返回空账本）。
    """
    try:
        return Ledger.load(0.0, ledger_path), None
    except Exception as e:                       # noqa: BLE001 —— 体检不许崩，如实记 ✗
        return None, e


def check_switch(cfg: dict) -> dict:
    on = bool((cfg.get("weekly") or {}).get("enabled", False))
    return _ok("总开关 weekly.enabled", on, f"enabled={on}",
               "config.yaml 里把 weekly.enabled 设为 true（否则执行档在读日历前就返回）")


def check_dynamic(cfg: dict = None) -> dict:
    """动态监控：**开关是否开** + **开着是否真在出信号**（2026-09-24 补）。

    为什么必须加这一项：`dynamic.enabled=false` 会让 `run_monitor` **第一行就 return**
    （`monitor.py:84`），而 `monitor` stage 在 `pipeline_run` 里**仍记 `ok`**
    （正常返回、没抛异常）⇒ **从运行台账完全看不出来**。
    2026-09-24 实踩：`signal` 表停在 20260904、**静默停摆 20 天**，直到人工比对
    `/api/signal` 返回的日期才发现 —— 期间止损 / ST·停牌预警 / 弱势替换全部失效。

    ⇒ 本项把两件事都变成体检可见：① 开关状态；② 开启后信号是否跟上最新交易日
    （防"开着但没跑"，例如配置没被读到、或 monitor 被跳过）。
    """
    cfg = cfg or cfg_mod.load_config()
    on = bool((cfg.get("dynamic") or {}).get("enabled", False))
    if not on:
        return _ok("动态监控", False,
                   "dynamic.enabled=false ⇒ 止损 / ST·停牌预警 / 弱势替换全部失效",
                   "确认要长期关闭则忽略本项；要恢复则 config.yaml 把 dynamic.enabled 设为 true")
    try:
        from app import db
        conn = db.get_conn()
        try:
            sig = conn.execute("SELECT MAX(signal_date) FROM signal").fetchone()[0]
            last = conn.execute("SELECT MAX(trade_date) FROM daily_score").fetchone()[0]
            gap = 0
            if sig and last and sig < last:
                gap = conn.execute(
                    "SELECT COUNT(*) FROM trade_cal WHERE is_open=1 AND cal_date>? AND cal_date<=?",
                    (sig, last)).fetchone()[0]
        finally:
            conn.close()
    except Exception as e:                       # 体检自身不许崩，如实记 ✗
        return _ok("动态监控", False,
                   f"已开启，但信号新鲜度无法判定: {type(e).__name__}: {e}",
                   "检查 neweco.db 可读性与 signal / daily_score / trade_cal 三张表")
    if not last:
        return _ok("动态监控", True, "已开启；无日频打分行，无法比对信号新鲜度")
    if sig == last:
        return _ok("动态监控", True, f"已开启；信号已跟上最新交易日 {last}")
    return _ok("动态监控", False,
               f"已开启，但信号停在 {sig or '（无）'}，落后最新交易日 {last} 共 {gap} 个开市日",
               "检查 monitor 是否在跑、以及服务是否读到新配置（load_config 走 mtime 缓存）")


def params(cfg: dict) -> dict:
    """**运行参数快照**（只读）—— 配置漂移是真实失效模式：改了 config.yaml 忘了改回去，
    袖套就会按另一个敞口/构型下单，而日志里看不出来。"""
    w = cfg.get("weekly") or {}
    cap = wk_plan.sleeve_capital(cfg)
    top_n, band, bonus = wk_plan.pick_shape(cap, cfg)
    eq_w = wk_plan.equity_weight(cfg)
    inh = (w.get("equity_weight") is None)
    fees = cfg.get("fees") or {}
    return {"capital": cap, "top_n": top_n, "band": band, "hold_bonus": bonus,
            "equity_weight": eq_w, "equity_weight_source": "继承月频" if inh else "weekly 覆盖",
            "max_lot_cost_mult": float(w.get("max_lot_cost_mult", 1.5)),
            "email": bool(w.get("email", False)),
            "risk_halt": bool((w.get("risk_halt") or {}).get("enabled", True)),
            "commission_rate": float(fees.get("commission_rate", 0.00025)),
            "slip": float((cfg.get("paper") or {}).get("slip", 0.001))}


def check_params(cfg: dict) -> dict:
    p = params(cfg)
    return _ok("运行参数", True,
               f"资金 {p['capital']:,.0f} | 构型 top{p['top_n']}/带{p['band']:g}×/加成{p['hold_bonus']:g}σ "
               f"| 权益权重 {p['equity_weight']:g}（{p['equity_weight_source']}）"
               f"| 价格上限 {p['max_lot_cost_mult']:g}× | 邮件 {'开' if p['email'] else '关'} "
               f"| 风控停买 {'开' if p['risk_halt'] else '关'} "
               f"| 佣金 {p['commission_rate']:.5f}/滑点 {p['slip']:.4f}")


def check_calendar(cfg: dict, today: str) -> dict:
    from app import db
    conn = db.get_conn()
    try:
        last = conn.execute("SELECT MAX(trade_date) FROM daily").fetchone()[0]
        cal_max = conn.execute("SELECT MAX(cal_date) FROM trade_cal").fetchone()[0]
        nxt = conn.execute("SELECT COUNT(*) FROM trade_cal WHERE cal_date>? AND is_open=1",
                           (today,)).fetchone()[0]
    finally:
        conn.close()
    covered = str(cal_max or "") > today
    return _ok("交易日历覆盖未来", covered and nxt > 0,
               f"daily 最新 {last} | trade_cal 到 {cal_max} | 未来开市日 {nxt} 个",
               "日历通常由月频数据链维护；先跑一次日更（scripts/run_daily.py 或等 18:00 档）")


def check_scores(cfg: dict, anchor: str) -> dict:
    try:
        S, anchors, cols, names, path = wk_scores.latest()
    except Exception as e:
        return _ok("打分存档", False, f"读不到: {e}", "先跑 `run_weekly.py score`")
    cov = pd.Timestamp(anchor) in set(pd.to_datetime(anchors))
    return _ok("打分存档覆盖最新锚点", cov,
               f"{Path(path).name} | 锚点 {anchors[0]}~{anchors[-1]} | 覆盖 {anchor}={cov}",
               f"19:30 打分保鲜档会自动重训；也可手动 `run_weekly.py score --asof {anchor}`")


# 判据冻结见 `plans/p1p2-model-provenance-20260921.md`（2026-09-21 立，跑前定死）。
# 45 = 30 天月频重训周期 + ≤2 天训练日数据滞后 + 13 天宽限
# ⇒ 这个阈值只在「漏掉一整次月频重训」时才翻红，不会把"模型正常地有一个月大"报成故障。
MODEL_MAX_LAG_DAYS = 45


def check_model_freshness(cfg: dict = None, table: str = "model_score") -> dict:
    """**模型新鲜度**：生产模型看到的数据截止日，离当前因子表有多远。

    起因是 05 号台账 §5.11：生产 `model_score`（09-09 训练）与其后被日频刷新的因子表出现
    分歧，而库里**没有任何记录**标明"这个模型当时看到的数据截止到哪天" ⇒ 模型年龄不可知，
    才把组合层约 1.1σ 的放大误写成 3.0pp「漂移」。P1 让 `trainer` 在训练跑通后登记
    `data_asof:{table}`，本项把它变成一条可查、可告警的体检项。

    ⚠️ `data_asof` 只取 `factor` 表，是**代理指标** —— 训练还读 `daily`/`adj_factor` 等，
    as-of 相同不代表训练输入完全相同。文案里因此叫"数据截止日"，不叫"数据指纹"。

    三种情形（判据已冻结；改阈值 = 改判据 ⇒ 重走预注册，不在体检里顺手调）：
      ① 无记录 ⇒ **✗**。"不可追溯"本身就是 §5.11 查出来的缺陷，报绿等于把它藏起来；
         它有明确的解除时点（下一次月频重训跑通后自动转绿），不是永远红的死项。
      ② 滞后 ≤ 45 天 ⇒ ✓（模型正常地有一个月大，不是故障）
      ③ 滞后 > 45 天 ⇒ ✗（已越过一次月频重训周期 + 宽限 ⇒ 该重训却没重训）

    按**自然日**计而非交易日：重训周期是自然月（10-01、11-01…），用自然日才能和
    "下一次重训还有几天"直接对齐；用交易日会把节假日缺口算进去，反而看不出漏没漏重训。
    """
    from app import db
    try:
        conn = db.get_conn()
        try:
            asof = db.get_meta(conn, f"data_asof:{table}")
            cur = conn.execute("SELECT MAX(trade_date) FROM factor").fetchone()[0]
        finally:
            conn.close()
    except Exception as e:                       # noqa: BLE001 —— 体检不许崩，如实记 ✗
        return _ok("模型新鲜度", False, f"读不到溯源记录: {e}",
                   "检查 `model_meta` 与 `factor` 表是否可读（doctor 需要读库）")
    if not cur:
        return _ok("模型新鲜度", False, "factor 表为空或取不到最新日期",
                   "先跑 `scripts/compute_factors.py` 重建因子表")
    if not asof:
        return _ok("模型新鲜度", False,
                   f"{table} 无 `data_asof` 记录 ⇒ **模型年龄不可知**（当前因子表已到 {cur}）",
                   "历史模型无法回溯补记，不编造假值；下一次全量重训（2026-10-01 月频档）"
                   "跑通后会自动补齐并转绿。若要立刻建立基线只能 `--full` 重训 —— "
                   "那会换 pin，**须先批准**")
    try:
        lag = (pd.Timestamp(str(cur)) - pd.Timestamp(str(asof))).days
    except Exception:                            # 日期不可解析 ⇒ 如实 ✗，不猜
        return _ok("模型新鲜度", False, f"`data_asof`={asof} 或因子表日期 {cur} 无法解析",
                   "检查 `model_meta` 里 `data_asof` 的格式（应为 YYYYMMDD）")
    ok = lag <= MODEL_MAX_LAG_DAYS
    return _ok("模型新鲜度", ok,
               f"模型数据截止 {asof} | 当前因子表 {cur} | 滞后 {lag} 天"
               + ("" if ok else f"（阈值 {MODEL_MAX_LAG_DAYS} 天：只在漏掉一整次月频重训时才告警）"),
               f"已越过一次月频重训周期 + 宽限({MODEL_MAX_LAG_DAYS} 天) ⇒ 模型可能已过期；"
               f"跑 `scripts/train_model.py`（全量用 `--full`，会换 pin 需先批准）")


def check_block_ic(cfg: dict, block: str, cache: dict = None) -> dict:
    """**块级 IC 读数**（D 案 ③-1 + 验收 §四）：P0 四块每块**至少**一个读数进体检。

    为什么要有它：整体 IC 还在正常带时，**某一块**可能已经先塌了 —— 而六块是
    `app/weekly/blocks.py` 那张映射表（C 案同一份），块间跨块 |ρ|≤0.4 ⇒ 块级读数有独立
    信息。判据与阈值在 `health.block_ic` 的 docstring 里冻结（0.5× 该块历史均值）。

    ⚠️ 块读数**只报警不改任何东西**：输入层已被 C 案封死（effect_indistinguishable），
    这里是"让局部劣化被看见"，不是"让系统自己调因子"。

    ⚠️ **定位器 ≠ 预警器**（2026-09-22 回放后补）：2021 年月频回放判 **NULL** —— 块级报警
    **没有**比全模型读数更早（B1 与全模型同为 20210129 首警，B2/B3/B4 更晚），见台账
    §5.16 与 `plans/block-ic-replay-2021-prereg-20260922.md`。⇒ 它的价值是**指出哪一块、
    哪个成员在掉**，不是"提前 4 周预警"。对外表述一律按"定位"记账。
    """
    name = f"块级IC {block}{wk_blocks.BLOCK_ROLE.get(block, '')}"
    res = cache if cache is not None else wk_health.block_ic_cached()
    if not res.get("available"):
        return _ok(name, False,
                   f"无块级 IC 读数：{res.get('note') or res.get('error') or '未知'}",
                   "19:45 档会自己算；也可手动 `run_weekly.py blockic --refresh`"
                   "（约 26s，要读 70 个因子锚点）")
    b = (res.get("blocks") or {}).get(block) or {}
    ic, base, ratio = b.get("ic"), b.get("baseline"), b.get("ratio")
    win = res.get("window")
    bn = res.get("baseline_anchors")
    if ic is None or base is None:
        return _ok(name, False,
                   f"{block} 读数不足：{b.get('note') or '块内成员在该窗没有有效 IC'}",
                   "查 `factor` 表该段是否缺因子值（块内成员覆盖率见读数的 coverage）")
    cov = (res.get("coverage") or {}).get(block)
    tail = f" | 窗 {win} 期 / 基线 {bn} 期 | asof {res.get('asof')}" + (
        f" | 覆盖 {cov:.3f}" if isinstance(cov, (int, float)) else "")
    if b.get("alarm"):
        return _ok(name, False,
                   f"{block} IC {ic:+.4f} 跌穿 0.5×基线（基线 {base:.4f}，比值 {ratio:+.2f}×）"
                   + tail,
                   f"{block}（{wk_blocks.BLOCK_ROLE.get(block, '')}）局部劣化 ⇒ 先看读数里的 "
                   f"`factor_baseline_ic` 定位是哪个成员掉的（符号翻转比幅度衰减更危险）；"
                   f"块读数**不自动改任何东西**，处置属换 pin 级决策，需另立预注册。"
                   f"⚠️ 它是**定位器**、不是**提前预警器**：2021 年回放判 NULL"
                   f"（没比全模型读数更早，台账 §5.16）⇒ 别拿它当提前量信号。"
                   f"但它有**独立信息**：v2 在未见区间 2022+2023 判 PASS"
                   f"（2023 有 5 个锚点在全模型健康时单独报警，§5.17）⇒ 该看，"
                   f"只是**不证明处置有效**")
    return _ok(name, True,
               f"{block} IC {ic:+.4f} = 基线 {base:.4f} 的 {ratio:.2f}×" + tail)


#: 执行档开盘时点（与 `scheduler` 的 09:32 档一致）。用于"方案文件"与"执行档看门狗"
#: 两个体检项的**时间闸** —— 它们的判据都必须问"到点了没"，否则每个非执行日都报假 ✗。
_OPEN_SLOT_HHMM = (9, 32)


def check_plan(cfg: dict, anchor: str, today: str = None, exec_day: str = None,
               now=None) -> dict:
    """**方案文件**：下个执行档要用的方案在不在。

    ⚠️ **判据修正（2026-09-24）**：原实现只要 `plan_<anchor>.json` 不存在就报 ✗，
    但**方案是执行日 09:32 档现算的**（`pipeline.run_open_slot`）⇒ 非执行日必然没有，
    于是每个非执行日的体检都挂一条假 ✗ —— `fix` 文案自己都写着"09:32 档会现算"。
    实测：2026-09-24 体检 `16/20`，其中一条 ✗ 就是它。

    现口径与 `check_exec_ran` 的"时间闸"同源（该函数 2026-09-14 已因同类假警报修过一次）：
    · `today < exec_day` ⇒ ✓「非执行日，方案将于 `<exec_day>` 09:32 现算」；
    · `today == exec_day` 且未到 09:32 ⇒ ✓「执行日，方案将在 09:32 现算」；
    · 已过窗口仍无方案 ⇒ ✗（此时才是真的欠方案）。
    ⚠️ 假警报的代价是**报警疲劳** —— 体检一有假 ✗，"有 ✗ 就要查"这条纪律就失效了。
    """
    p = wk_plan.PLAN_DIR / f"plan_{anchor}.json"
    if p.exists():
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            rows = len(d.get("rows") or [])
            ratio = d.get("invested_ratio")
            # 2026-09-13: "投出比例"必须与**放行闸 G3**并排显示 —— 这两个数字错配过一次:
            # G3 的门槛 0.70 是按**回测口径**定的(全史平均投入率 0.769, 那时账户已复利到千万),
            # 而纸面账户从 100 万起步、整手约束更硬 ⇒ 现役配置实测只投出 0.6968, 这道闸**过不去**。
            # 单看"投出 0.6968"没人会觉得有问题, 并排才看得见。
            flag = ""
            try:
                from app.weekly import protocol as wk_proto
                gate = float(wk_proto.MIN_INVESTED)
                if ratio is None or float(ratio) < gate:
                    flag = f" | ⚠ 低于放行闸 G3 门槛 {gate:g}"
            except Exception:                       # 体检不许因为拿不到门槛就报错
                log.exception("G3 门槛读取失败(不影响方案检查)")
            return _ok("方案文件", rows > 0,
                       f"{p.name} | {rows} 只 | 投出 {ratio}{flag}",
                       "方案里没有行 ⇒ 重跑 `run_weekly.py plan`")
        except Exception as e:
            return _ok("方案文件", False, f"{p.name} 解析失败: {e}", f"删掉重算: {p.name}")
    # ---- 方案不在：先过时间闸，再判欠不欠 ----
    if exec_day and today and str(today) < str(exec_day):
        return _ok("方案文件", True,
                   f"{p.name} 尚未生成 —— 非执行日属预期（下个执行档 {exec_day} 的 09:32 现算）")
    if exec_day and today and str(today) == str(exec_day):
        n = now or timeutil.now()
        if (n.hour, n.minute) < _OPEN_SLOT_HHMM:
            return _ok("方案文件", True,
                       f"{p.name} 尚未生成 —— 执行日，将在 09:32 档现算")
    return _ok("方案文件", False, f"{p.name} 不存在（09:32 档会现算, 但需要打分覆盖锚点）",
               f"`run_weekly.py plan --asof {anchor}`")


def next_exec(conn, today: str, horizon_days: int = 12) -> tuple:
    """复刻调度口径, 找出**下一个真正会执行的开市日**与它用的锚点。

    只在"今天不是执行日"时才有意义 —— 体检要回答的是"下个执行档会不会动",
    而不是"今天会不会动"。判据与 `pipeline.run_open_slot` 逐字一致:
    该日与上一个开市日**不在同一 ISO 周**。
    """
    from app.weekly.pipeline import iso_week
    rows = conn.execute(
        "SELECT cal_date FROM trade_cal WHERE is_open=1 AND cal_date>=? "
        "ORDER BY cal_date LIMIT ?", (today, horizon_days)).fetchall()
    for (d,) in rows:
        hist = [r[0] for r in conn.execute(
            "SELECT cal_date FROM trade_cal WHERE is_open=1 AND cal_date<=? "
            "ORDER BY cal_date DESC LIMIT 2", (d,))]
        if len(hist) >= 2 and iso_week(hist[0]) != iso_week(hist[1]):
            return str(d), str(hist[1])
    return None, None


def check_ledger(cfg: dict, ledger_path=None) -> dict:
    led, err = ledger_or_error(ledger_path)
    if err is not None:
        return _ok("账本可读", False, f"账本损坏/不可读: {err}", _LEDGER_FIX)
    st = led.state
    halt = st.get("halt") or {}
    n_nav = len(st.get("nav_history") or [])
    exists = Path(led.path).exists()
    detail = (f"{led.path.name}"
              + ("" if exists else "（**文件还不存在** ⇒ 首次执行会新建, 资金取方案里的 capital）")
              + f" | 净值点 {n_nav} | 持仓 {len(st.get('positions') or {})} "
                f"| 成交 {len(st.get('trades') or [])} | 停买={bool(halt.get('active'))}")
    return _ok("账本可读", True, detail,
               _LEDGER_FIX)          # 成功路径不显示 fix；文案与失败路径保持同一份


def check_risk(cfg: dict, ledger_path=None) -> dict:
    from app.weekly import risk as wk_risk
    led, err = ledger_or_error(ledger_path)
    if err is not None:
        return _ok("风控停买状态", False, f"账本不可读, 无法判定停买状态: {err}", _LEDGER_FIX)
    halt = wk_risk.is_halted(led)
    if halt:
        # 修复文案按 auto_resume 开关分流（2026-09-28 停买改为可自动解除）: 开启时告诉
        # 用户"等恢复档自动解除"这条路存在, 人工 resume 永远是立即生效的备选。
        auto = bool(((cfg.get("weekly") or {}).get("risk_halt") or {}).get("auto_resume"))
        fix = ("auto_resume 已开：健康度恢复 healthy 后 19:45 档自动解除；也可立即人工 "
               "`run_weekly.py resume --reason \"...\"`" if auto else
               "确认信号恢复后人工解除：`run_weekly.py resume --reason \"...\"`")
        return _ok("风控停买状态", False,
                   f"**停买中**（自 {halt.get('since')}）：{halt.get('reason')}", fix)
    return _ok("风控停买状态", True, "未触发（正常开仓）")


def check_exec_ran(cfg: dict, today: str, ledger_path=None, now=None) -> dict:
    """**执行档看门狗**：今天若是执行日，账本里必须有今天的净值点。

    为什么需要：执行档无人值守 —— 它没跑（进程挂了/日历判错/异常早退）与"跑了但没成交"
    在日志里长得差不多，可前者意味着**整周没有仓位**。执行档只要跑过就会 `mark(record=True)`
    落一个当日净值点（成交 0 笔也会落），所以"当日有没有净值点"就是"它到底跑没跑"的判据。
    幂等：重复跑同一交易日会**覆盖**该净值点，不会产生第二个（见 `ledger.mark`）。

    ⚠️ **时间闸**（2026-09-14 早修）：本函数会被 `run_weekly.py doctor` **在任意时刻**调用，
    而执行有两档 —— **09:32 开盘档 / 18:05 晚间兜底档**（还有服务启动补跑）。原实现不看时间，
    于是**每个执行日的早上跑体检都会报 ✗"执行档没跑"**（2026-09-14 06:02 实测假警报）。
    现在的口径：09:32 之前=**尚未到窗口**；09:32~18:15=**等晚间兜底**（设计内，不算失败）；
    **18:15 之后**仍无净值点 ⇒ 才判 ✗（此时两档 + 补跑都已经有机会）。

    ⚠️ **空占位（2026-09-14 07:0x 修，07 号 #34）**：上面那句"跑过就会落点"**反向不成立** ——
    `execute_plan` 在**取不到成交价**时一笔没成也会落点（盘前补跑就是这种情形，实测 06:04）。
    所以判据升级为"当日有净值点 **且** （有持仓 **或** 不是今日 applied 的 0 成交占位）"。
    """
    from app import db
    from app.weekly.pipeline import iso_week
    conn = db.get_conn()
    try:
        hist = [r[0] for r in conn.execute(
            "SELECT cal_date FROM trade_cal WHERE is_open=1 AND cal_date<=? "
            "ORDER BY cal_date DESC LIMIT 2", (today,))]
    finally:
        conn.close()
    if len(hist) < 2 or hist[0] != today:
        return _ok("执行档看门狗", True, f"{today} 不是开市日, 不适用")
    if iso_week(hist[0]) == iso_week(hist[1]):
        return _ok("执行档看门狗", True, f"{today} 与上一开市日同属一周 ⇒ 非执行日")
    led, err = ledger_or_error(ledger_path)
    if err is not None:
        return _ok("执行档看门狗", False, f"账本不可读, 无法判定当日是否执行: {err}", _LEDGER_FIX)
    st = led.state
    ran = any(str(p.get("date")) == today for p in (st.get("nav_history") or []))
    lp = st.get("last_plan") or {}
    # 2026-09-14 修 (07 号 #34): **"有净值点" ≠ "真的执行过"** —— `execute_plan` 在价格取不到时
    # 一笔没成也照样 `mark(record=True)`(成交 0 笔也落点), 于是盘前补跑会把本函数短路。
    # 实测: 06:04 开机补跑 ⇒ 0 成交 + 落当日点 ⇒ 本函数差点报 ✓。改为把"0 持仓 + (今日
    # 已 applied 且 0 成交 或 根本没有方案记录)"视作**空占位**, 仍按时间闸处理。
    empty_marker = bool(ran and not (st.get("positions") or {})
                        and (str(lp.get("applied_on") or "") == today or not lp))
    # 2026-09-15（外部审查 P2-7）：裸 `datetime.now()` → timeutil（东八区）。
    # 本函数用 `now` 判"到 18:15 了没"，机器时区 ≠ 东八区时会误判（提前/滞后报警）。
    now = now or timeutil.now()
    hm = now.hour * 60 + now.minute
    if ran and not empty_marker:
        return _ok("执行档看门狗", True, f"执行日 {today}：账本当日净值点已落 ✓")
    if empty_marker and hm < 18 * 60 + 15:
        return _ok("执行档看门狗", True,
                   f"执行日 {today}：账本只有**空占位**净值点(0 成交 / 0 持仓)"
                   f" —— 等 18:05 晚间兜底(价格取不到时 execute_plan 也会落点, 故不算已执行)")
    if empty_marker:
        return _ok("执行档看门狗", False,
                   f"执行日 {today}：到 {now:%H:%M} 仍只有 0 成交 / 0 持仓的**空占位**净值点"
                   f" ⇒ 本轮实际没有建仓(整周空仓)",
                   "查 logs/app.log 的「周频袖套」与当日行情是否入库; 也可手动 "
                   "`run_weekly.py exec --date " + today + "`")
    if hm < 9 * 60 + 32:
        return _ok("执行档看门狗", True,
                   f"执行日 {today}：现在 {now:%H:%M}，尚未到 09:32 开盘档（18:05 还有晚间兜底）")
    if hm < 18 * 60 + 15:
        return _ok("执行档看门狗", True,
                   f"执行日 {today}：09:32 档未落账，等 18:05 晚间兜底档"
                   f"（同价口径；服务若重启也会补跑）")
    return _ok("执行档看门狗", False,
               f"执行日 {today}：到 {now:%H:%M} 为止账本**仍没有**当日净值点",
               "执行档没跑或中途失败 ⇒ 查 logs/app.log 的「周频袖套」与 logs/server.err.log；"
               "也可手动 `run_weekly.py exec --date " + today + "`")


def check_disk(cfg: dict, floor_gb: float = 5.0) -> dict:
    free = shutil.disk_usage(str(DATA)).free / (1024 ** 3)
    return _ok("磁盘余量", free >= floor_gb, f"{free:.1f} GB 可用（阈值 {floor_gb:.0f} GB）",
               "清理 logs/ 或备份；数据库写入失败会导致当天所有档静默失败")


def check_market_cache(cfg: dict) -> dict:
    """**行情缓存写入**：上一次大面板缓存写盘成没成。

    为什么要有这一项（2026-09-24 审查）：`factor_engine._cache_save` 的失败原来只有一行
    `log.warning` —— 不进邮件、不进 doctor、不进体检 ⇒ 2026-09-24 19:34 那行
    `No type extension with name arrow.py_extension_type found` **沉睡了 2 小时**才被发现。
    体检是"没人看日志时唯一会说话的地方"，所以这一类失败必须有一项在这里。

    判据 = 读 `data/cache/market/_last_write.json`（`_cache_save` 每次写盘都落这份证据）。

    ⚠️ **没有状态文件不算失败**：它只说明"还没有发生过大面板写入"（新部署 / 刚清缓存 /
    当日还没跑到 19:30 档），报绿并注明，不要把它变成每天早上的假红。
    """
    from app.factors import factor_engine as fe
    p = fe._cache_status_path()
    if not p.exists():
        return _ok("行情缓存写入", True, "尚无大面板缓存写入记录（新部署 / 刚清缓存）")
    try:
        st = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        return _ok("行情缓存写入", False, f"状态文件不可读: {e}",
                   "删掉 data/cache/market/_last_write.json 让它重新记录")
    if st.get("ok"):
        return _ok("行情缓存写入", True,
                   f"上次写入成功 {st.get('updated_at', '?')}（key {str(st.get('key'))[:16]}）")
    return _ok("行情缓存写入", False,
               f"上次写入**失败** {st.get('updated_at', '?')}（key {str(st.get('key'))[:16]}）："
               f"{st.get('error')}",
               "重启服务重置进程内 pyarrow 扩展类型状态（最低成本）；若复发，查 "
               "logs/app.log 的「行情缓存写入失败」与 `arrow.py_extension_type`")


def check_backup(cfg: dict, max_age_days: int = 8) -> dict:
    """**备份新鲜度**：数据库周日档 + 账本按日快照。

    备份失效是"看不见的失效" —— 没人会每天去翻 `data/backup/`。判据给 8 天是因为
    数据库备份是**周档**（周日 09:15），留一天余量；账本快照同档生成，账本还不存在时跳过。
    """
    import datetime                      # mtime 与本地钟同源, 见下
    today = timeutil.today()             # 2026-09-15: 与文件名/run_date 同源(东八区)
    def _age(p) -> int:
        # ⚠️ `fromtimestamp` 按**本地时区**解释 mtime；与上面 today 混用会在午夜附近
        # 差一天 —— 但这里的判据是"≤8 天"的粗粒度新鲜度, 一天误差不影响结论。
        return (today - datetime.date.fromtimestamp(p.stat().st_mtime)).days

    dbs = sorted((DATA / "backup").glob("neweco_????????.db"))
    db_age = _age(dbs[-1]) if dbs else None
    led = DATA / "weekly" / "ledger.json"
    snaps = sorted((DATA / "weekly" / "backup").glob("ledger_????????.json")) if led.exists() else []
    snap_age = _age(snaps[-1]) if snaps else None
    ok_db = db_age is not None and db_age <= max_age_days
    ok_snap = (not led.exists()) or (snap_age is not None and snap_age <= max_age_days)
    detail = (f"数据库备份最新 {dbs[-1].name if dbs else '**无**'}"
              f"{'（' + str(db_age) + ' 天前）' if db_age is not None else ''}"
              f" | 账本快照 {'无（账本还没建）' if not led.exists() else str(snap_age) + ' 天前'}")
    return _ok("备份新鲜度", bool(ok_db and ok_snap), detail,
               "周日 09:15 档会自动备份; 也可手动 `scripts/backup_db.py`; "
               "账本快照见 `app/weekly/ledger.py::backup_snapshots`")


def check_stage_ledger(cfg: dict, today: str = None, stages=None) -> dict:
    """**阶段台账有没有没收口的行**（只读；2026-09-19 审查）。

    `scheduler._stage` 每跑一个阶段就往 `pipeline_run` 插一行 'running'、结束时改成
    ok/error。强杀/断电时那行会**永久停在 'running'**，而两个读者都会被带偏：
      · `_stage_watermark_ok` 只看"该阶段最新一行是不是 ok" ⇒ 卡住的 factors/train
        每次开机整段重算（1197 万行）；
      · `_stage_failed` 只认 'error' ⇒ 卡住的阶段**既不告警也不补跑**，缺口静默消失。
    判据只抓"**某阶段最新一行**仍是 'running' 且 run_date 早于今天" —— 已被同阶段后续行
    取代的历史悬挂行（例如 20260911 的 daily_sync）只是取证噪声，不报 ✗。
    `stages` 只为测试可确定性注入（默认 None = 扫全部阶段，生产行为不变）。
    """
    from app import db
    today = str(today or timeutil.date8())
    sql = ("SELECT p.stage, p.run_date, p.started_at FROM pipeline_run p "
           "WHERE p.status='running' AND p.run_date<? "
           "AND p.id=(SELECT MAX(id) FROM pipeline_run q WHERE q.stage=p.stage) ")
    args: list = [today]
    if stages:
        sql += "AND p.stage IN (%s) " % ",".join("?" * len(stages))
        args += list(stages)
    sql += "ORDER BY p.run_date"
    with db.conn_ctx() as conn:
        rows = conn.execute(sql, args).fetchall()
        dangling_total = conn.execute(
            "SELECT COUNT(*) FROM pipeline_run WHERE status='running'").fetchone()[0]
    if rows:
        return _ok(
            "阶段台账收口", False,
            f"{len(rows)} 个阶段的**最新一行**仍是 running 且已是往日起跑: "
            + "; ".join(f"{s}@{d}" for s, d, _t in rows[:6])
            + f"（全库未收口行共 {dangling_total} 行）",
            "该阶段的成败无人知晓：`_stage_failed` 只认 error ⇒ 补跑不会触发、也不会告警。"
            "查 logs/app.log 对应时刻（多半是被强杀/断电），必要时手动重跑该档")
    return _ok("阶段台账收口", True,
               f"没有「当前悬空」的阶段行（全库历史遗留 running 行 {dangling_total} 行，"
               "已被同阶段后续行取代或未过期）",
               "")


def check_notify_delivery(cfg: dict = None, days: int = 7, min_failures: int = 2) -> dict:
    """**邮件通道自己还活着吗**（只读；2026-09-20 三补二十九 / 05 号 P2-6）。

    坏法：`notify._send_with_retry` 两次重试全败时只往 `notify_log` 写一行 `ok=0` ——
    于是"告警通道坏了"这件事**只有主动打开看板才看得见**，而它坏的那天恰好是最需要它的那天
    （不能用邮件报告邮件故障）。所以把它搬进体检。

    判据：近 `days` 天内**同一个 tag 连续 `min_failures` 次失败、中间没有成功** ⇒ ✗。
      · 连续 ⇒ 一次偶发抖动不报警（实测 id=39 `运维告警 timed out` 前面刚成功过一次 ⇒ 不报 ✗）；
      · 按 tag 分组 ⇒ 不会因为"别的邮件发出去了"而掩盖这一封一直发不出去；
      · `email.enabled=false` ⇒ ok(未启用) —— 没发邮件不是故障，拿它当 ✗ 会让关邮件的人天天红。
    """
    from app import db
    if not ((cfg or {}).get("email") or {}).get("enabled"):
        return _ok("邮件送达", True, "email.enabled=false ⇒ 不发信，本项不适用（未启用）", "")
    try:
        with db.conn_ctx() as conn:
            # 2026-09-28 收编 timeutil: 读窗口原用 SQLite localtime —— 写侧
            # notify_log.created_at 走 timeutil.stamp()(东八区), 两钟换机时错位
            rows = conn.execute(
                "SELECT tag, ok, error, created_at FROM notify_log "
                "WHERE created_at >= ? "
                "ORDER BY id DESC LIMIT 400", (timeutil.days_ago(int(days)),)).fetchall()
    except Exception as e:
        return _ok("邮件送达", True, f"读不到 notify_log（视为未使用）: {type(e).__name__}: {e}", "")
    by_tag: dict = {}
    for tag, ok, error, created in rows:         # 已按 id DESC ⇒ 每个 tag 内新→旧
        by_tag.setdefault(str(tag), []).append((int(ok or 0), error, str(created)))
    bad = []
    for tag in sorted(by_tag):
        fails = 0
        for ok, _e, _c in by_tag[tag]:
            if ok:
                break
            fails += 1
        if fails >= min_failures:
            bad.append((tag, fails, by_tag[tag][0][1] or "", by_tag[tag][0][2] or ""))
    newest_failed = next(((t, e, c) for t, o, e, c in rows if not int(o or 0)), None)
    if bad:
        return _ok(
            "邮件送达", False,
            f"近 {days} 天 {len(rows)} 条发送记录里 {len(bad)} 个 tag 连续失败: "
            + "; ".join(f"{t}×{n}（最近一次 {c}: {e}）" for t, n, e, c in bad[:5]),
            "告警通道自己坏了 ⇒ 设置页重填授权码/核对 smtp 端口与网络，"
            "修好后手动触发一次测试邮件（`app/notify.py` 的 `send`）确认恢复")
    tail = ""
    if newest_failed:                            # 未到 ✗ 阈值也如实报出来，别静默
        t, e, c = newest_failed
        tail = f"；最近一条失败是 {t}@{c}（{e}，连续 1 次未到阈值 {min_failures}）"
    return _ok("邮件送达", True,
               f"近 {days} 天 {len(rows)} 条发送记录，无连续失败"
               + ("" if rows else "（表为空 = 还没发过信）") + tail, "")


def checks(cfg: dict = None, today: str = None, anchor: str = None,
           ledger_path=None) -> list:
    """跑全套体检（只读）。任何一条抛异常都只记成该条 ✗，绝不向上抛。"""
    from app import db
    cfg = cfg or cfg_mod.load_config()
    today = str(today or timeutil.date8())      # 2026-09-15: 东八区(原裸 date.today)
    # 锚点默认取"下一个会执行的开市日所用的锚点"（与调度同口径）——
    # 直接拿今天当锚点会误报(周末/周中今天都不是锚点)
    auto = {"exec_day": None, "anchor": None}
    if anchor is None:
        try:
            conn = db.get_conn()
            try:
                auto["exec_day"], auto["anchor"] = next_exec(conn, today)
            finally:
                conn.close()
        except Exception:
            log.exception("推算下个执行档失败")
        anchor = auto["anchor"] or today
    anchor = str(anchor)
    out = []
    # 块级 IC 缓存**只读一次**给四项共用（每项各读一遍是四次磁盘 IO，没意义）
    _bic = None
    try:
        _bic = wk_health.block_ic_cached()
    except Exception:
        log.exception("块级 IC 缓存读取失败")
    for fn in (lambda: check_switch(cfg),
               lambda: check_dynamic(cfg),          # 2026-09-24: 开关 + 信号新鲜度（防静默停摆）
               lambda: check_params(cfg),
               lambda: check_calendar(cfg, today),
               lambda: check_scores(cfg, anchor),
               lambda: check_model_freshness(cfg),
               *[(lambda b=b: check_block_ic(cfg, b, _bic)) for b in wk_blocks.DOCTOR_BLOCKS],
               lambda: check_plan(cfg, anchor, today=today, exec_day=auto["exec_day"]),
               lambda: check_ledger(cfg, ledger_path),
               lambda: check_risk(cfg, ledger_path),
               lambda: check_exec_ran(cfg, today, ledger_path),
               lambda: check_stage_ledger(cfg, today),
               lambda: check_notify_delivery(cfg),
               lambda: check_backup(cfg),
               lambda: check_disk(cfg),
               lambda: check_market_cache(cfg)):      # 2026-09-24: 缓存写盘失败的可观测出口
        try:
            out.append(fn())
        except Exception as e:                       # 体检自身不许崩
            log.exception("体检项异常")
            out.append(_ok("(体检项异常)", False, str(e), "看日志 app/weekly/doctor.py"))
    out.insert(2, _ok(
        "下个执行档", bool(auto["exec_day"]),
        f"{auto['exec_day']} 执行, 锚点 {auto['anchor']}"
        + (f"(打分/方案都按锚点 {anchor} 检查)" if auto["exec_day"] else ""),
        "日历里 12 天内找不到执行日 ⇒ 检查 trade_cal 覆盖"))
    return out


def render(items: list) -> list:
    return [f"{'✓' if it['ok'] else '✗'} {it['name']}: {it['detail']}"
            + (f"\n    → {it['fix']}" if it.get("fix") else "") for it in items]
