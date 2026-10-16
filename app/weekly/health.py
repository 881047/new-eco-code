"""周频袖套的**健康监控**（框架图 L8 的"监控 / 闭环"盒；2026-09-13 新增）

## 为什么需要它

终端能自己出方案、成交、记账，但**不会自己发现"实盘行为与验证过的行为不一致"**。
本模块按框架图第 13 阶段的模型健康实验，用三类读数给袖套打健康度：

    🟢 healthy    滚动 IC ≥ 0.5×冻结基线，换手 ≤ 2×回测，投入率 ≥ 0.60
    🟡 warning    IC ∈ (0, 0.5×)，或换手 2~3×，或投入率 0.50~0.60
    🟠 degrading  连续 6 个锚点滚动 IC < 0，或换手 > 3×
    🔴 dead       连续 12 个锚点滚动 IC < 0 ⇒ 提交退役候选

**健康档本身只出读数与告警，不自动改模型参数/权重/约束**（那是方案的不变量）。

⚠️ 2026-09-13 措辞修正（独立审查 §3.3）：原文只写"只报警，不自动改任何东西"，容易被读成
"这套系统不会自己做任何风控动作" —— 而事实上**风控是会自动动作的**：
`app/weekly/risk.py::apply` 在健康度 `degrading`/`dead` 时**自动停止开新仓**（只卖不买、
敞口只降不升，且**不自动解除**，须人工 `run_weekly.py resume`）。这是 §6.1-4 的硬要求，
与本模块"不改模型/权重/约束"并不矛盾 —— 它改的是**买不买**，不是**怎么算**。

## 实现取舍（如实声明）

- `evaluate()` 是**纯函数**（输入即读数）⇒ 阈值被单测钉死；
- `rolling_ic()` 用**轻量 SQL** 算（打分存档 × 日线复权收益），**不加载全市场面板**
  （健康检查每天要跑，不能花 6 分钟）。因此它**不**按 PIT 可交易域裁剪 ——
  这是一个"行为监控代理指标"，不是研究级 IC；阈值也因此设得宽（0.5×基线）。
- 冻结基线 = 2×W5+pit 在同窗口 491 期上的 IC（F22，两套种子 0.0773）。
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import numpy as np
import pandas as pd

from app import db
from app.factors import factor_engine as fe
from app.weekly import blocks as wk_blocks
from app.weekly.ledger import Ledger
from app.weekly import scores as wsc

log = logging.getLogger(__name__)

# 冻结基线 = 2×W5+pit 在同窗口 491 期上的 IC（F22，两套种子 0.0773）。
# 补充交叉核对（2026-09-13 F50）：生产打分在 606 个有效期上的 IC 均值 0.0875 —— 窗口与掩码口径
# 不同（F22 是 491 期研究口径），故基线仍取 F22 的 0.0773，不因 F50 而改阈值。
FROZEN_IC = 0.0773
# ⚠️ 2026-09-13 修正（07 号 #25）：原值 0.092 是 **F22 的研究构型（hyst=0.2）** 名单更替率，
# 而生产跑的是 `hold_bonus=0.0` ⇒ 换手实测 **0.137**（F52，两套种子 0.137/0.136）。
# 拿 0.092 当基线会把 2× 误判成 ~3× ⇒ 健康档提前判 `degrading`，而 `risk.apply` 会**自动停买**
# （恢复 healthy 后 auto_resume 自动解除，或人工 resume）。基线必须是**生产构型**的回测换手。
BACKTEST_TURNOVER = 0.137
# ⚠️ 2026-09-14 外部审查 P2-1：回测里 `avg_turnover` 的分母从 `n_periods` 修成了
# "**真正计入的期数**"（首期无上期组合 ⇒ 不计），同一条序列读出来会比旧公式 **+1/n**
# （115 期 ≈ +0.9%）。本常量是 F52 用**旧公式**测的 0.137，故严格同口径下应为 ≈0.1382。
# **有意不改这个数**：判据是 2× / 3× 的倍数带（阈值高 0.9% 相当于把带收窄 0.9%），
# 远小于健康档要抓的那种数量级漂移；要改就等下一次重测 F52 时一起换。
LEVELS = ("healthy", "warning", "degrading", "dead")

#: 换手档位判定所需的最少「卖出日」个数（2026-09-24；预注册
#: `plans/health-turnover-guard-prereg-20260924.md`）。
#: 为什么需要它：`current()` 的换手估计是 `sells.tail(window).mean() / npos` ——
#: **卖出日只有 1 个时它也照常出数**，而 `> 3×` 那条会触发 `risk.apply`
#: **自动停买**（恢复 healthy 后 auto_resume 自动解除，或人工 resume）。
#: 实测（2026-09-24）：账本里全部 31 笔卖出**只发生在唯一一天 20260921** ⇒
#: `turnover_est = 31/98 = 0.3163` 是 **n=1 的单点观测**；只要某天卖出 > 40 笔就越过 3× 线。
#: 周频袖套每期调仓一次 ⇒ 4 个卖出日 ≈ 4 周，是"一个可读窗口"的下限。
#: ⚠️ 改这个数 = 改判据 ⇒ 必须另立预注册（不许直接改常量）。
TURNOVER_MIN_DAYS = 4


def evaluate(rolling_ic: float, neg_streak: int, turnover: float,
             deployed_ratio: float, frozen_ic: float = FROZEN_IC,
             n_turnover: int = 0) -> dict:
    """纯判据：四个读数 → 健康度 + 触发原因。

    `n_turnover` = 换手估计所用的**卖出日个数**（`current()` 传真实值）。

    ⚠️ **默认 0 = 样本不足**（2026-09-24，预注册
    `plans/health-turnover-guard-prereg-20260924.md`）：这是**保守默认** ——
    新调用方忘了传样本量时自动获得保护，而不是静默失去保护。
    样本不足时换手**仍可进 warning**（信息不藏起来），但**不得单独升级到 degrading**
    —— 不可逆动作（自动停买）必须被样本量挡住。
    """
    reasons = []
    n = int(n_turnover)
    enough = n >= TURNOVER_MIN_DAYS
    hi = turnover > 3 * BACKTEST_TURNOVER
    mid = 2 * BACKTEST_TURNOVER < turnover <= 3 * BACKTEST_TURNOVER
    if neg_streak >= 12:
        level = "dead"
        reasons.append(f"连续 {neg_streak} 个锚点滚动 IC < 0 ⇒ 退役候选")
    elif neg_streak >= 6 or (hi and enough):
        level = "degrading"
        if neg_streak >= 6:
            reasons.append(f"连续 {neg_streak} 个锚点滚动 IC < 0")
        if hi and enough:
            reasons.append(f"换手 {turnover:.3f} > 3×回测({BACKTEST_TURNOVER})")
    # S4（2026-09-21）：**去掉 `0 <`** —— 原写法把负 IC 排除在 warning 之外，
    # 导致"负 IC 判 healthy、正的小 IC 判 warning"的翻档（实证见 m1_ic_scale_flip.json
    # 的 flip_anchors：20170609 的 −0.0072 判 healthy、20170623 的 +0.0005 判 warning）。
    # 现在只要 rolling_ic < 0.5×frozen（含负值）就进 warning。
    # 分支顺序不变 ⇒ neg_streak>=6 的负 IC 仍优先落 degrading（自证门 S4-a）。
    # 2026-09-24（自证门 T-a/T-b）：本改动**只拦"由换手单独升级到 degrading"这一条路**，
    # 两条 neg_streak 分支（dead / degrading）一字未动。
    elif ((rolling_ic is not None and rolling_ic < 0.5 * frozen_ic)
          or mid or hi
          or (0.50 <= deployed_ratio < 0.60)):
        level = "warning"
        if rolling_ic is not None and rolling_ic < 0.5 * frozen_ic:
            reasons.append(f"滚动 IC {rolling_ic:.4f} < 0.5×冻结基线({frozen_ic})")
        if mid:
            # ⚠️ 2026-09-24：样本不足时**必须**把这句注记也带上。
            # 实测教训：只有 1 个卖出日时，原样输出"换手 0.316 为回测的 2~3 倍"会
            # 读成"袖套长期换手偏高"，而真相是"**那一次调仓**换掉了 31/98 的名字"
            # —— 判据的样本量前提不写出来，读者会照错的前提去做决策。
            reasons.append(f"换手 {turnover:.3f} 为回测的 2~3 倍"
                           + ("" if enough else
                              f"（样本不足 n={n}，<{TURNOVER_MIN_DAYS} 个卖出日"
                              f" ⇒ 仅 n={n} 次调仓的观测，不代表长期水平）"))
        if hi:
            # 能走到这里 ⇒ 必然 `not enough`（enough 时上面已落 degrading）
            reasons.append(f"换手 {turnover:.3f} > 3×回测({BACKTEST_TURNOVER})，"
                           f"但样本不足 n={n}（<{TURNOVER_MIN_DAYS} 个卖出日）"
                           f"⇒ **不升级为停买**")
        if 0.50 <= deployed_ratio < 0.60:
            reasons.append(f"投入率 {deployed_ratio:.2f} 偏低")
    else:
        level = "healthy"
    return {"level": level, "reasons": reasons, "rolling_ic": rolling_ic,
            "neg_streak": int(neg_streak), "turnover": round(float(turnover), 4),
            "deployed_ratio": round(float(deployed_ratio), 4), "frozen_ic": frozen_ic,
            "turnover_n": n, "turnover_min_days": TURNOVER_MIN_DAYS}


def _fwd_excess(conn, anchor: str, horizon: int = 5) -> pd.Series:
    """锚点后第 1 个交易日开盘 → 第 1+H 个交易日开盘的复权收益(轻量 SQL)。

    ⚠️ 尺名（2026-09-20 登记）：本函数用 `open × adj_factor` = **复权/全收益**尺；
    `weekly/scores.forward_excess` 用的是**未复权** `m["open"]`，而 `FROZEN_IC`（本文件 :48）
    是 F22 那一批 491 期算出来的**第三种来历**。⇒ 三处"IC/超额"不同尺是已知事实，
    差值量级的量化那一轮产物已丢失、读数已撤回（14 号三补二十九·补），
    重跑并落产物之前**不要**在这里写具体数字，也不要拿三处的 IC 互相换算。
    """
    cal = [r[0] for r in conn.execute(
        "SELECT DISTINCT trade_date FROM daily WHERE trade_date>=? ORDER BY trade_date LIMIT ?",
        (anchor, horizon + 2))]
    if len(cal) < horizon + 2:
        return pd.Series(dtype=float)
    d0, d1 = cal[1], cal[horizon + 1]
    q = ("SELECT d.ts_code, d.open*a.adj_factor AS p0 FROM daily d "
         "JOIN adj_factor a ON a.ts_code=d.ts_code AND a.trade_date=d.trade_date "
         "WHERE d.trade_date=?")
    p0 = pd.read_sql(q, conn, params=(d0,)).set_index("ts_code")["p0"]
    # 2026-09-19 审查: 这里原写作 `q.replace("d.open", "d.open")` —— 自我替换的**空操作**
    # (疑为早期想换列名时的残留)。同一条 SQL 对 d0/d1 都成立(只是 params 不同), 故直接用 q。
    p1 = pd.read_sql(q, conn, params=(d1,)).set_index("ts_code")["p0"]
    r = (p1 / p0 - 1.0).replace([np.inf, -np.inf], np.nan).dropna()
    return r


def rolling_ic(window: int = 12) -> dict:
    """最近 `window` 个**已实现**锚点的滚动 IC 与连续为负计数（轻量、无全市场面板）。"""
    S, anchors, cols, names, path = wsc.latest()
    scored = [i for i in range(len(anchors)) if np.isfinite(S[i]).any()]
    ics = []
    conn = db.get_conn()
    try:
        for i in reversed(scored):
            a = anchors[i].strftime("%Y%m%d")
            r = _fwd_excess(conn, a)
            if len(r) < 200:
                continue
            s = pd.Series(S[i], index=cols).reindex(r.index).dropna()
            r = r.reindex(s.index)
            if len(s) < 200:
                continue
            _ic = float(s.rank().corr(r.rank()))
            # 2026-09-14（07 #46 同族）：任一侧方差为 0 时秩相关返回 NaN。NaN **不能**
            # 进 ics —— 下面"连续为负"的计数会把 `NaN >= 0` 判为 False、把它当成负 IC
            # 锚点，6 个就足以让 evaluate() 判 degrading ⇒ risk.apply **自动停买**
            # （恢复 healthy 后 auto_resume 自动解除）。非有限值一律不采信。
            if np.isfinite(_ic):
                ics.append(_ic)
            if len(ics) >= window:
                break
    finally:
        conn.close()
    if not ics:
        return {"n": 0, "ic": None, "neg_streak": 0, "path": str(path)}
    ics = list(reversed(ics))                 # 时间升序
    neg = 0
    for v in reversed(ics):
        if v >= 0:                            # ics 已保证全是有限值（原 `v is None` 是死分支）
            break
        neg += 1
    return {"n": len(ics), "ic": round(float(np.mean(ics)), 4),
            "last": round(ics[-1], 4), "neg_streak": neg, "path": str(path)}


def current(window: int = 12, ledger_path=None) -> dict:
    """汇总袖套健康度（取数 + evaluate）。任何异常都不抛出。"""
    try:
        led = Ledger.load(0.0, ledger_path)
        hist = pd.DataFrame(led.state.get("nav_history", []))
        trades = pd.DataFrame(led.state.get("trades", []))
        # 2026-09-14（07 #46）：投入率同样只接受有限值 —— invested/nav 出 NaN 会一路
        # 带到 FastAPI 的 allow_nan=False ⇒ 整个 /api/weekly/health 500。
        deployed = 0.0
        _nav_last = float(hist["nav"].iloc[-1]) if len(hist) else 0.0
        if _nav_last > 0:
            _d = float(hist["invested"].iloc[-1] / _nav_last)
            deployed = _d if np.isfinite(_d) else 0.0
        turn, turn_n = 0.0, 0
        if len(trades):
            sells = trades[trades["side"] == "sell"].groupby("date").size()
            npos = max(1, len(led.state.get("positions", {})))
            # 2026-09-14（07 #46）：**有买无卖**（首周期/纯建仓日）时 `sells` 是空序列，
            # `.mean()` 得 NaN ⇒ starlette 以 allow_nan=False 序列化 ⇒ **整个端点 500**
            # （实拍：首个纸面周期 09:32 建仓后 /api/weekly/health 一直 500）。
            # 空序列的语义是"本窗口还没有卖出"= 换手 0，不是"未知"。
            if len(sells):
                _m = float(sells.tail(window).mean())
                turn = _m / npos if np.isfinite(_m) else 0.0
                # 2026-09-24（预注册 health-turnover-guard-prereg-20260924）：
                # `sells` 是**卖出日**序列 ⇒ 它的长度就是换手估计的样本量。
                # n=1 时上面那个 `_m` 是单点观测，不足以支撑不可逆的停买决策。
                turn_n = int(min(len(sells), window))
        ric = rolling_ic(window)
        out = {"n_nav_points": int(len(hist)), "n_trades": int(len(trades)),
               "rolling_ic": ric.get("ic"), "neg_streak": ric.get("neg_streak", 0),
               "ic_n": ric.get("n"), "deployed_ratio": round(deployed, 4),
               "turnover_est": round(turn, 4), "turnover_n": turn_n}
        if len(hist) < 2:
            # 2026-09-14：unknown 分支也带上 `frozen_ic`/`reasons` —— 否则看板那行会显示
            # "滚动 IC 0.0787(冻结基线 --)"（实测截图抓到），读者会以为基线缺失。
            out.update({"level": "unknown", "reasons": [], "frozen_ic": FROZEN_IC,
                        "note": "净值点不足两期, 暂无健康读数(先跑几次 exec)"})
            return out
        out.update(evaluate(ric.get("ic"), ric.get("neg_streak", 0), turn, deployed,
                            n_turnover=turn_n))
        return out
    except Exception as e:                       # 监控绝不能把链子带崩
        log.exception("袖套健康检查异常")
        return {"level": "unknown", "error": str(e), "reasons": [], "frozen_ic": FROZEN_IC}


# ---------------------------------------------------------------- 告警边沿
# 2026-09-18 邮件优化：原来 19:45 档是**电平触发**（"只要不健康就发"）⇒ 袖套一旦退化，
# 会连着每天发一封内容一模一样的告警，直到有人手工 resume 或它自己好回来。实测这 6 天
# 恰好一直 healthy 所以没暴露。现改成**边沿触发 + 7 天重复提醒**：
#   等级变化（含好转一档）⇒ 发；同一档未处理 ⇒ 每 7 天提醒一次（不能"报一次就永久沉默"）；
#   回到 healthy ⇒ 发一封「已恢复」，让收件人知道那件事结了。
_UNHEALTHY = ("warning", "degrading", "dead")
#: 「同一档还在」时多久重发一次提醒（自然日）。判据退化中的人需要被持续提醒，
#: 但一天一封会把人训练成直接删邮件。
REPEAT_DAYS = 7


def decide_alert(prev_level: str, level: str, prev_date: str = None, today: str = None):
    """要不要发信、发哪种。返回 `None` / `"alert"` / `"repeat"` / `"recovered"`。

    纯函数（不读文件、不查库）⇒ 与 `evaluate` 同样可被单测钉死。
    `prev_level` = 上次**已通知**的等级（没通知过则 None）；`level` = 本次读数。
    `unknown` 一律不算边沿也**不算恢复**（读数缺失不等于健康）。
    """
    if level in (None, "unknown"):
        return None
    if level in _UNHEALTHY:
        if prev_level is None or prev_level not in _UNHEALTHY:
            return "alert"                      # 首次/从健康或未知转入
        if prev_level != level:
            return "alert"                      # 等级变化（升降都算，内容不一样）
        if prev_date and today:
            try:
                gap = (pd.Timestamp(today) - pd.Timestamp(prev_date)).days
            except Exception:
                return "repeat"                 # 日期格式异常 ⇒ 宁可多提醒
            return "repeat" if gap >= REPEAT_DAYS else None
        return "repeat"                         # 没有上次日期可比 ⇒ 保守提醒
    # level == "healthy"
    return "recovered" if prev_level in _UNHEALTHY else None


#: 上次已通知状态落盘位置（与账本同目录；不入库 —— 它只是"邮件发到哪一步了"的进程外记忆）
NOTIFIED_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "weekly" / "health_notified.json"


def load_notified(path=None) -> dict:
    """读上次已通知的 `{level, date}`；读不到/坏档一律返回 `{}`（= 按"没通知过"处理）。

    返回空 dict 的后果是**下一封照发**，不是"下一封不发" —— 这里 fail-open 是有意的：
    状态文件坏掉时宁可多提醒一次，也不许把一次真实退化静默掉。
    """
    p = Path(path or NOTIFIED_PATH)
    try:
        st = json.loads(p.read_text(encoding="utf-8"))
        return st if isinstance(st, dict) else {}
    except Exception:
        return {}


def save_notified(level: str, date8: str, path=None) -> bool:
    """原子写（`os.replace`，同 `ledger.py:152` 的手法）；失败只记日志不抛。"""
    p = Path(path or NOTIFIED_PATH)
    tmp = Path(str(p) + ".tmp")
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps({"level": level, "date": date8}, ensure_ascii=False),
                       encoding="utf-8")
        os.replace(tmp, p)
        return True
    except Exception:
        log.exception("健康告警状态落盘失败(下次会重复通知一次)")
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        return False


# ================================================================ 块级 IC（D 案 ③-1）
# 把"模型整体健康"拆成"六块各自的健康"（分组定义见 `app/weekly/blocks.py`）。
#
# ⚠️ **尺名警告（与 `_fwd_excess` 那条同源）**：`factor` 表 **2026-08 之前只在月末**存因子值，
# 之后日频刷新开始插**每日**行 ⇒ 块级 IC 的锚点是**混频**（历史月末 + 近端日频），
# 实测两者 IC 均值同量级（B1 0.080 vs 0.077）。它与本文件上面的 `rolling_ic`（周频、5 日前瞻）
# 是**两把尺**，不可互相换算；块级 IC 只与**它自己的历史均值**比（判据见下）。
#
# 判据（2026-09-22 冻结，改任一个数 = 改判据 ⇒ 重走预注册）：
#   · 块级 IC = 块内成员 IC 的等权均值，**按该成员基线期的 IC 符号定向**
#     ⇒ 单因子**符号翻转**会直接把块读数拉负（这是模型最怕的失效，不能只看 |IC|）；
#   · 基线 = 该块在**较早的** `BLOCK_IC_BASELINE_ANCHORS` 个锚点上的均值（无前视）；
#   · 报警 = 当前窗均值 < `BLOCK_IC_ALARM_RATIO`(0.5) × 基线（与 S4 修好的负 IC 分支同口径：
#     负 IC **不会**被排除在报警之外）；基线 ≤ 0 时判据不适用，如实标注而非报绿。
#
# ⚠️ **定位 ≠ 提前预警**（2026-09-22 回放后补，判据与阈值**未动**）：2021 年月频回放判 **NULL**
#   —— 块级报警**没有**比全模型读数更早（B1 与全模型同为 20210129 首警，B2/B3/B4 更晚），
#   见台账 §5.16 与 `plans/block-ic-replay-2021-prereg-20260922.md` ⇒ 对外**只承诺定位**，
#   不许拿它当"提前 4 周预警"的证据。
#
# ✅ **独立信息已证**（v2，2026-09-22，判据与阈值仍未动）：在**未见区间** 2022+2023 上，
#   2023 有 **5 个锚点**是"全模型读数健康、P0 块（B2/B1）单独报警" ⇒ 块级确实能看到
#   全模型看不到的劣化（台账 §5.17 / `plans/block-ic-replay-v2-holdout-prereg-20260922.md`）。
#   ⚠️ 但 2022 **零命中**、2021 的 B2 反而晚 28 天 ⇒ **不是稳定的提前预警器**；
#   且"该不该按它处置"**未验** ⇒ 块读数继续**只报警不动作**。
BLOCK_IC_HORIZON = 5             # 前瞻交易日（与 `rolling_ic` 同为**短窗代理尺**；
                                 # 取 21（月频持有期）会让最近约一个月永远没有读数，
                                 # 那正好毁掉监控唯一的价值 —— 时效性）
BLOCK_IC_WINDOW = 12             # 当前窗（锚点数）
BLOCK_IC_BASELINE_ANCHORS = 60   # 基线窗（锚点数，**早于**当前窗，无前视）
BLOCK_IC_ALARM_RATIO = 0.5       # 报警线 = 0.5 × 该块历史均值
BLOCK_MIN_NAMES = 200            # 单锚点最少有效样本（与 rolling_ic 同数量级）
#: 生产默认落盘位置；**实际路径按 `db.DATA_DIR` 现算**（见 `_block_cache_path`）。
BLOCKS_CACHE = Path(__file__).resolve().parent.parent.parent / "data" / "weekly" / "block_ic.json"


def _block_cache_path(cache_path=None) -> Path:
    """缓存路径**现算**，不认模块导入时的常量。

    ⚠️ 2026-09-22（D 案接线时实测）：写成模块级常量会让 `tests/test_weekly_reconcile_slot.py`
    （端到端跑 `_weekly_health_slot`）把**测试库**（空 `factor` 表）算出的"0 个锚点"
    写进生产 `data/weekly/block_ic.json` ⇒ 生产看板/doctor 随后读到一份假读数。
    `tests/_isolation.py` 把 `db.DATA_DIR` 指到临时目录 ⇒ 跟着它走，隔离就由**路径**保证，
    不依赖谁记得 patch（与 `DB_PATH` 同一手法）。
    """
    if cache_path:
        return Path(cache_path)
    try:
        from app import db as _db
        base = Path(getattr(_db, "DATA_DIR", None) or BLOCKS_CACHE.parent.parent)
    except Exception:                                     # 拿不到就回退生产默认
        return BLOCKS_CACHE
    return base / "weekly" / BLOCKS_CACHE.name


def _factor_anchors(conn, n: int, cutoff: str = None) -> list:
    """因子表里最近 `n` 个锚点（时间升序）。

    ⚠️ **频率是混的**：2026-08 之前只有**月末**（12 个/年，月频因子在那儿算），
    之后日频刷新开始往 `factor` 表插**每日**行 ⇒ 近端锚点是日频。这是现状，不是 bug，
    但"当前窗"会因此覆盖更短的自然时间，读数解读时要知道这一点。

    `cutoff`：只取 ≤ 该日子的锚点 —— 没有完整前瞻窗的最近几期算不出 IC，
    与其让它们占掉窗口名额（那样"当前窗"会缺一大半），不如一开始就不取。
    """
    sql = "SELECT DISTINCT trade_date FROM factor"
    args: list = []
    if cutoff:
        sql += " WHERE trade_date<=?"
        args.append(cutoff)
    sql += " ORDER BY trade_date DESC LIMIT ?"
    args.append(int(n))
    rows = conn.execute(sql, args).fetchall()
    return [r[0] for r in reversed(rows)]


def _forward_cutoff(conn, horizon: int) -> str:
    """前瞻窗**完整**的最后一天：再往后就没有 `horizon` 个交易日了。"""
    rows = conn.execute("SELECT DISTINCT trade_date FROM daily ORDER BY trade_date DESC "
                        "LIMIT ?", (int(horizon) + 2,)).fetchall()
    return rows[-1][0] if len(rows) >= int(horizon) + 2 else None


def _cross_section(conn, date8: str, factors) -> pd.DataFrame:
    """单锚点因子截面（行=股票，列=因子）。存的就是**已中性化**的 z-score。"""
    d = pd.read_sql("SELECT ts_code, factor, value FROM factor WHERE trade_date=?",
                    conn, params=(date8,))
    if d.empty:
        return pd.DataFrame(columns=list(factors))
    w = d.pivot_table(index="ts_code", columns="factor", values="value")
    return w.reindex(columns=list(factors))


def _factor_ic_row(w: pd.DataFrame, r: pd.Series) -> dict:
    """单锚点：每个因子对前瞻收益的**秩 IC**（Spearman）。"""
    out = {}
    rr = r.rank()
    for f in w.columns:
        s = w[f]
        m = s.notna() & rr.notna()
        if int(m.sum()) < BLOCK_MIN_NAMES:
            continue
        v = float(s[m].rank().corr(rr[m]))
        if np.isfinite(v):                    # 零方差截面 ⇒ NaN，不采信（同 rolling_ic）
            out[f] = v
    return out


def block_ic_from_ic(ic_rows: dict, cur_dates: list, base_dates: list) -> dict:
    """**纯判据**（无 IO）：逐锚点因子 IC `{date: {factor: ic}}` → 六块的当前/基线/报警。

    拆出来是为了能被单测钉死（"构造块劣化必须报警"），取数那段（`block_ic`）无法在
    单测里复现 70 个锚点的因子表，但判据必须能被证明有效。

    · 定向符号只由**基线期**（较早那段）的 IC 决定 ⇒ 无前视；符号翻转 ⇒ 块读数转负；
    · 块 IC = 块内成员定向 IC 的等权均值；
    · 报警 = 当前窗 < `BLOCK_IC_ALARM_RATIO` × 基线（基线 ≤0 判据不适用，如实标注）。
    """
    base_ic = {}
    for f in wk_blocks.BLOCK_FACTORS:
        v = [ic_rows[d][f] for d in base_dates if f in ic_rows.get(d, {})]
        base_ic[f] = float(np.nanmean(v)) if v else np.nan
    sign = {f: (-1.0 if (np.isfinite(base_ic[f]) and base_ic[f] < 0) else 1.0)
            for f in wk_blocks.BLOCK_FACTORS}

    def _blk_means(dlist) -> dict:
        out = {}
        for b in wk_blocks.BLOCK_KEYS:
            vals = [sign[f] * ic_rows[d][f] for d in dlist
                    for f in wk_blocks.members(b) if f in ic_rows.get(d, {})]
            out[b] = float(np.nanmean(vals)) if vals else None
        return out

    cur, base = _blk_means(cur_dates), _blk_means(base_dates)
    blocks_out, alarms = {}, []
    for b in wk_blocks.BLOCK_KEYS:
        c, bl = cur.get(b), base.get(b)
        ratio = (c / bl) if (c is not None and bl and np.isfinite(bl) and bl > 0) else None
        note, alarm = "", False
        if bl is None or not np.isfinite(bl) or c is None or not np.isfinite(c):
            note = "读数不足（块内成员在该窗没有有效 IC）"
        elif bl <= 0:
            note = f"基线 {bl:.4f} ≤ 0 ⇒ 0.5× 判据不适用（如实标注，不按健康处理）"
        else:
            alarm = bool(c < BLOCK_IC_ALARM_RATIO * bl)
            if alarm:
                alarms.append(b)
        blocks_out[b] = {
            "ic": None if c is None or not np.isfinite(c) else round(float(c), 5),
            "baseline": None if bl is None or not np.isfinite(bl) else round(float(bl), 5),
            "ratio": None if ratio is None else round(float(ratio), 3),
            "alarm": alarm, "priority": wk_blocks.BLOCK_PRIORITY[b],
            "role": wk_blocks.BLOCK_ROLE[b], "n_members": len(wk_blocks.members(b)),
            "note": note}
    return {"blocks": blocks_out, "alarms": alarms,
            "p0_alarms": [b for b in alarms
                          if wk_blocks.BLOCK_PRIORITY[b] in wk_blocks.ALERT_PRIORITIES],
            "factor_baseline_ic": {f: (None if not np.isfinite(v) else round(v, 5))
                                   for f, v in base_ic.items()}}


def block_ic(window: int = BLOCK_IC_WINDOW,
             baseline_anchors: int = BLOCK_IC_BASELINE_ANCHORS,
             horizon: int = BLOCK_IC_HORIZON, conn=None) -> dict:
    """六块的滚动 IC / 历史基线 / 是否报警。只读，不写任何表。

    返回 `{"available": True, "blocks": {B1: {...}}, "alarms": [...], "p0_alarms": [...]}`；
    取不到数时 `available=False` 并给 `note`（监控类读数一律不许抛）。
    """
    own = conn is None
    conn = conn or db.get_conn()
    try:
        total = int(window) + int(baseline_anchors)
        cutoff = _forward_cutoff(conn, horizon)
        dates = _factor_anchors(conn, total + 2, cutoff)
        if len(dates) < 4:
            return {"available": False, "note": f"因子表锚点不足（只有 {len(dates)} 个）"}

        ic_rows, last_w = {}, None
        for d in dates:
            w = _cross_section(conn, d, wk_blocks.BLOCK_FACTORS)
            if w.empty:
                continue
            r = _fwd_excess(conn, d, horizon)
            if len(r) < BLOCK_MIN_NAMES:
                continue
            row = _factor_ic_row(w, r)
            if row:
                ic_rows[d] = row
                last_w = w
        avail = sorted(ic_rows)
        # ⚠️ 窗口在**过滤之后**切：缺前瞻窗的锚点不占名额，否则"当前窗"会缺一大半
        if len(avail) < int(window) + 4:
            return {"available": False,
                    "note": f"只有 {len(avail)}/{len(dates)} 个锚点算得出 IC（样本不足）"}
        cur_dates = avail[-int(window):]
        base_dates = avail[:-int(window)][-int(baseline_anchors):]

        # ---- 判据（纯函数，见 `block_ic_from_ic`）----
        judged = block_ic_from_ic(ic_rows, cur_dates, base_dates)
        # 稀疏门（D 案 ③-4）：新路径进来先过一遍非零占比，结果一并留档
        sparse, coverage = {}, {}
        if last_w is not None:
            for b in wk_blocks.BLOCK_KEYS:
                sub = last_w[[f for f in wk_blocks.members(b) if f in last_w.columns]]
                sparse[b] = fe.guard_sparse(b, sub) if not sub.empty else {}
                coverage[b] = round(float(sub.notna().to_numpy().mean()), 4) if sub.size else 0.0
        return {"available": True, "asof": avail[-1], "horizon": int(horizon),
                "window": int(window), "baseline_anchors": int(baseline_anchors),
                "n_anchors": len(ic_rows),
                "window_range": [cur_dates[0], avail[-1]],
                "baseline_range": [base_dates[0], base_dates[-1]],
                "blocks": judged["blocks"], "alarms": judged["alarms"],
                "p0_alarms": judged["p0_alarms"],
                "coverage": coverage, "sparse_guard": sparse,
                "factor_baseline_ic": judged["factor_baseline_ic"],
                "factor_block": {f: wk_blocks.block_of(f) for f in wk_blocks.BLOCK_FACTORS}}
    finally:
        if own:
            conn.close()


def block_ic_refresh(cache_path=None) -> dict:
    """算一次块级 IC 并落缓存（与 `attrib.refresh` 同形）：读数要 ~20s，不能挂 HTTP 端点。

    2026-09-29 审查：改**原子写**（tmp + `os.replace`，与 `save_notified` 同手法）——
    原 `write_text` 直接覆写，写到一半被杀会留下半截 JSON，doctor/看板当期读数缺失
    （`block_ic_cached` 读坏缓存会 `available=False` 自愈，但那一档就是空窗）。
    写失败只告警不抛（读数不许把 19:45 档带崩），旧缓存保持原样。
    """
    res = block_ic()
    p = _block_cache_path(cache_path)
    tmp = Path(str(p) + ".tmp")
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str),
                       encoding="utf-8")
        os.replace(tmp, p)
    except Exception:
        log.warning("块级 IC 缓存写入失败(旧缓存保持原样): %s", p, exc_info=True)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
    return res


def block_ic_cached(cache_path=None) -> dict:
    """读块级 IC 缓存（doctor / 看板用）。没有缓存时给出可执行的下一步。"""
    p = _block_cache_path(cache_path)
    if not p.exists():
        return {"available": False,
                "note": "还没有块级 IC 缓存 —— 19:45 档会自动算；也可手动 "
                        "`run_weekly.py blockic --refresh`"}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:                      # 缓存坏了也不能把看板带崩
        log.exception("块级 IC 缓存读取失败")
        return {"available": False, "error": str(e)}
