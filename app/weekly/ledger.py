"""周频袖套(sleeve)的独立账本 —— 与月频模拟盘**物理隔离**

## 为什么要单独一套账

月频模拟盘(`paper_position`/`paper_nav`)是**用户现行的实盘对标记录**。
若把周频方案的订单塞进同一张表, 周频的卖出会清掉月频持仓、两边 NAV 互相污染,
**两套 track record 同时作废**。故周频袖套用自己的 JSON 账本:

    data/weekly/ledger.json   (持仓 / 现金 / 净值序列 / 成交流水)

## 成交口径: 与生产模拟盘**同一套实现**(不复制规则)

直接调用 `app.paper.engine._exec_price` —— 次日开盘价 ± `paper.slip`、
一字涨停买不进 / 一字跌停卖不出、停牌(vol=0)顺延、涨跌停限幅按板块与时点名称 PIT。
费用走 `app.portfolio.fees.trade_cost`(佣金含 5 元最低 + 印花税 + 过户费),
整手走 `app.portfolio.board.lot_shares`。
**这套规则是被 534→672 个测试与实盘对账反复校准过的**, 所以这里只**调用**, 不重写。

## 会计口径

- 持仓按**未复权收盘价**盯市; 每次盯市前按 `adj_factor` 比例缩放股数(除权/送转自动再投资),
  与回测器 `_mtm_adj`、独立实现 F6 的口径一致;
- NAV = 现金 + Σ(股数 × 收盘价); 现金不生息(与回测的"闲置现金"口径一致, 偏保守);
- T+1: 当日买入不可当日卖出(账本按 `bought` 日期硬校验)。
"""
from __future__ import annotations

import datetime
import json
import logging
import os
from pathlib import Path

import pandas as pd

from app import config as cfg_mod
from app import db
from app.filelock import FileLock
from app.models.names import NameLookup
from app.paper.engine import _exec_price, _exec_price_open   # 生产同一套成交规则(两档)
from app.portfolio import board as board_mod
from app.portfolio.fees import fee_params, trade_cost

log = logging.getLogger(__name__)

DEFAULT_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "weekly" / "ledger.json"

# 报年化/回撤/超额所需的最少**自然周**(不是日点) —— 见 `week_anchors`
MIN_REPORT_WEEKS = 8


def week_anchors(dates) -> int:
    """净值点真正覆盖了几个**自然周**（按 ISO 周去重）。**全项目唯一的"周"口径。**

    ## 为什么需要它（2026-09-21 修正了一处会产出误导数字的真实缺陷）

    袖套**每天盯市**落一个净值点 ⇒ `len(nav_history)` 是**日点**数，不是周数；
    而执行档（调仓）只在周一推进一次。此前三处把**日点数**直接当成"周数"用：

    | 位置 | 原写法 | 后果 |
    |---|---|---|
    | `ledger.snapshot` | `n_weeks = len(nav.pct_change())`（日间差） | 9 个日点 ≈ **2 周**就越过"≥8 周才报年化" ⇒ 实测一周基准 −2% 被年化成 **−53%** |
    | `protocol.G1_连续运行` | `len(nav) >= 12`（日点） | 约 12 个日点 ≈ **2.5 周**就点亮"可进入小资金档" |
    | `attrib` | `s["n"]`（归因期 = 日点数） | 同上，8 个日点 ≈ 1.6 周就报"年化/风格读数" |

    ⇒ 这里给出**唯一**的周口径，三处都调它，**不许各算各的**（否则又会漂回日点）。
    判据数值本身**一个都没放宽**，只是把被日点顶穿的守卫还原成它本来的意思。
    """
    idx = pd.to_datetime(pd.Index(list(dates)))
    if len(idx) == 0:
        return 0
    cal = idx.isocalendar()
    return int(len(set(zip(cal.year, cal.week))))


def backup_snapshots(path: Path = None, keep: int = 12) -> str | None:
    """把账本按日快照到 `data/weekly/backup/ledger_<YYYYMMDD>.json`，只留最近 `keep` 份。

    为什么不靠数据库备份: 袖套的成交记录**不在** `neweco.db` 里（物理隔离是设计的一部分），
    所以周日的 `db.backup_db()` 保护不到它。账本丢 = track record 丢，无法重建。
    返回快照路径；账本还不存在时返回 None（首次执行前是正常状态）。
    """
    src = Path(path or DEFAULT_PATH)
    if not src.exists():
        return None
    out_dir = src.parent / "backup"
    out_dir.mkdir(parents=True, exist_ok=True)
    # 2026-09-15：快照文件名是与 run_date/数据库备份同源的"东八区第几天"
    from app import timeutil
    dst = out_dir / f"ledger_{timeutil.date8()}.json"
    # 2026-09-28: 改原子写(同目录 tmp + os.replace) —— 写到一半被杀会留下当日坏快照,
    # 坏档会一直留到被 12 份滚动窗口挤出
    tmp = Path(str(dst) + ".tmp")
    try:
        tmp.write_bytes(src.read_bytes())
        os.replace(tmp, dst)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
    snaps = sorted(out_dir.glob("ledger_????????.json"))
    for old in snaps[:-keep]:
        try:
            old.unlink()
        except OSError:
            log.warning("旧快照删除失败: %s", old)
    return str(dst)


class Ledger:
    INTEREST_FALLBACK = 1.5     # %/年: 取不到逆回购隔夜利率时的兜底(与月频模拟盘同一口径)

    def __init__(self, capital: float, path: Path = None, state: dict = None):
        self.path = Path(path or DEFAULT_PATH)
        self.state = state or {
            "capital": float(capital), "cash": float(capital), "positions": {},
            "nav_history": [], "trades": [], "inception": None, "last_mark": None,
            "interest": 0.0, "interest_upto": None,
        }
        #: 加载时的文件指纹 `(size, mtime_ns)`；不存在记 `(0, 0)`。
        #: `save()` 用它挡**丢失更新**（见 save 的 docstring，2026-09-15 外部审查 P1-3）。
        self._snapshot: tuple = (0, 0)

    # ---------------------------------------------------------------- 读写
    @classmethod
    def load(cls, capital: float, path: Path = None) -> "Ledger":
        p = Path(path or DEFAULT_PATH)
        snap = (0, 0)
        if p.exists():
            raw = p.read_bytes()
            st = json.loads(raw.decode("utf-8"))
            stt = p.stat()
            snap = (stt.st_size, stt.st_mtime_ns)
            led = cls(st["capital"], p, st)
        else:
            led = cls(capital, p)
        led._snapshot = snap
        return led

    def save(self) -> bool:
        """落账（原子写 + 滚动 `.bak` + **跨进程锁 + 丢失更新检测**）。返回是否真的写入。

        ## 为什么加锁与版本校验（2026-09-15，外部审查 P1-3 复核为真）

        账本原来只有**进程内的秒退守卫**（`_already_done` / `risk.apply` 幂等），
        而存在**两条互不知情**的写入路径：
          · 调度档（09:32 / 18:05 …）—— 进程内串行 + `.task.lock`；
          · `scripts/run_weekly.py exec|resume`（人工跑的 CLI）—— **不经过** `.task.lock`。
        两者同时 `load → 改 → save` 时，后完成的那次会**整份覆盖**前者 ⇒ 成交/halt 记录
        静默消失（复核结论：竞态窗口窄，但后果是 track record 不可重建，属不可接受）。

        两道防线，**都必要**：
          ① 跨进程文件锁：把"读-改-写"串行化，避免交错写；
          ② 加载指纹校验：即便串行执行，晚到的写入者拿的也是**陈旧快照** ——
             检测到文件已被别人改过就**拒绝写**并留 ERROR（宁可这次不落账让人来看，
             也不要静默覆盖掉别人的成交）。

        返回值：True = 已按本次 state 落盘；False = 拒绝（有别的写入者/拿不到锁）。
        ⚠️ 调用点应把 False 当成"这次没落账"来对待，而不是默认成功。
        """
        # 2026-09-28 审查修复(毒账本守卫): 账本文件不存在且 capital<=0 ⇒ 拒绝**新建**。
        # 正常初始化永远携带真实本金; 此形态只会来自误操作(如无账本时先敲 resume 的
        # Ledger.load(0.0)) —— 一旦落盘, 后续 Ledger.load 因文件已存在永远按 0 资金
        # 加载, 全部买单被现金约束跳过 ⇒ 袖套永久空仓且无报错。
        if not self.path.exists() and float(self.state.get("capital") or 0) <= 0:
            log.error("拒绝新建 capital=%s 的账本 %s(毒账本守卫: 新账本本金必须为正)",
                      self.state.get("capital"), self.path)
            return False
        # ---- ① 跨进程文件锁（同一个锁文件把 read-modify-write 串行化）
        lock = FileLock(Path(str(self.path) + ".lock"))
        if not lock.acquire():
            log.error("账本被其他进程持有(锁 %s.lock) ⇒ 本次不落账，避免覆盖他人成交", self.path)
            return False
        try:
            return self._save_locked()
        finally:
            lock.release()

    def _save_locked(self) -> bool:
        # ---- ② 丢失更新检测：文件在我 load 之后被别人改过？
        cur = ((self.path.stat().st_size, self.path.stat().st_mtime_ns)
               if self.path.exists() else (0, 0))
        if cur != self._snapshot:
            log.error("账本在本次加载后被其他进程修改(快照 %s → %s) ⇒ 拒绝覆盖写入 %s",
                      self._snapshot, cur, self.path)
            return False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # 2026-09-13 **滚动备份**: 覆写前把上一次的状态原样留一份 `<ledger>.bak`。
        # 账本是袖套唯一的成交记录, 一旦写坏/被误改, track record 无法重建 ——
        # 一次覆写留一份上一版是最便宜的抗损坏(第二层是周日档的按日快照)。
        if self.path.exists():
            try:
                Path(str(self.path) + ".bak").write_bytes(self.path.read_bytes())
            except OSError:
                log.warning("账本滚动备份失败(不阻塞落账): %s", self.path)
        # 2026-09-14 **原子写**（07 号 #48）：先写同目录临时文件、再 `os.replace` 换名。
        # 原实现直接 `write_text` 覆写 —— 写到一半崩/断电会留下**半截 JSON**，而 `.bak`
        # 存的是**上一版**（不是当前版），恢复会丢最近一笔成交。同目录换名在 Windows 上
        # 是原子的（跨盘则不是，故临时文件必须同目录）。
        # ⚠️ 必须沿用**同一套文本写入方式**（默认 newline=None ⇒ Windows 下 CRLF）：
        # 换成 write_bytes/`newline=""` 会把整个账本的行尾改成 LF —— 那是伪装成重构的
        # 格式变更（`tests/test_weekly_ledger.py` 有"`.bak` 与原文件逐字节相同"的断言）。
        tmp = Path(str(self.path) + ".tmp")
        try:
            tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=1),
                           encoding="utf-8")
            os.replace(tmp, self.path)
        finally:
            if tmp.exists():                      # 换名失败/写入失败都不许留临时文件
                try:
                    tmp.unlink()
                except OSError:
                    log.warning("临时账本清理失败(不影响本次落账): %s", tmp)
        # 落账成功 ⇒ 更新指纹，避免**同一个实例**连续 save 第二次被自己的检测挡住
        try:
            stt = self.path.stat()
            self._snapshot = (stt.st_size, stt.st_mtime_ns)
        except OSError:
            self._snapshot = (0, 0)
        return True

    # ---------------------------------------------------------------- 盯市
    def _accrue_interest(self, conn, trade_date: str) -> float:
        """闲置现金按**逆回购隔夜利率**逐**交易日**计息; 返回累计利息。

        为什么要它（2026-09-13）：月频模拟盘一直给闲置现金计息（`app/paper/engine.py`
        的 `_accrued_interest` + `paper_interest` 表），**周频账本却一分钱不算** —— 同一个
        终端里对同一件物理事实有两套记账法，这本身就是缺陷。袖套平均只有 77% 投出去，
        那 23% 现金按近两年逆回购隔夜均值 1.6% 算，一年差 **~0.37pp**：白扔的钱，且方向
        是**低估**自己。

        口径（与研究侧回测**故意不同**，必须知道）：

        - 研究侧（F1b/F22/F28...）的读数**不含**这笔利息 ⇒ 纸面账本从今往后比回测
          略微好看（+0.37pp/年量级），做"纸面 vs 回测"对比时要把这一项单独剔出来；
        - 与月频侧**逐字同口径**：区间**日历日**（含周末/假期）× 上一盯市日最近隔夜利率
          （年化，取不到用 1.5% 兜底）÷ 365；袖套 23% 现金按近两年 1.6% 算 ⇒ **~0.37pp/年**；
        - **幂等**：只补 `(interest_upto, trade_date]` 区间 ⇒ 开盘档与晚间档同日各盯市一次
          也不会重复计息；首个净值点只记起点、不计息（否则会从"时间起点"开始算）。
        """
        self.state.setdefault("interest", 0.0)
        upto = self.state.get("interest_upto")
        if upto is None:                        # 首个净值点: 记起点, 不计息
            self.state["interest_upto"] = trade_date
            return float(self.state["interest"] or 0.0)
        if str(trade_date) <= str(upto):         # 同日或更早 ⇒ 幂等, 不动
            return float(self.state["interest"] or 0.0)
        cash = float(self.state.get("cash") or 0.0)
        if cash <= 0:                            # 满仓/空账本没有利息
            self.state["interest_upto"] = trade_date
            return float(self.state["interest"] or 0.0)
        # 计法与月频侧**逐字同口径**(app/paper/engine.py::_accrued_interest):
        # 区间**日历日**(含周末/节假日) × 上一盯市日最近隔夜利率(年化, 无数据用 1.5%) ÷ 365。
        # 刻意不用"开市日数": 那会只拿到名义利率的 ~250/365, 两个子系统又会不一致 ——
        # 而"两套记账法"正是本次要消灭的东西。
        row = conn.execute("SELECT overnight FROM repo_rate WHERE trade_date<=? "
                           "ORDER BY trade_date DESC LIMIT 1", (upto,)).fetchone()
        rate = float(row[0]) if row and row[0] else float(self.INTEREST_FALLBACK)
        days = (datetime.datetime.strptime(str(trade_date), "%Y%m%d")
                - datetime.datetime.strptime(str(upto), "%Y%m%d")).days
        add = cash * (rate / 100.0) * days / 365.0
        self.state["interest"] = float(self.state["interest"] or 0.0) + add
        self.state["interest_upto"] = trade_date
        return self.state["interest"]

    def mark(self, trade_date: str, conn=None, record: bool = True) -> float:
        """按 `trade_date` 收盘价盯市(先按 adj 缩放股数), 返回 NAV。

        record=True 时把净值点追加到 `nav_history`;交易前估值用 record=False
        (否则账本里会留下"成交前"的点, 而成交后的持仓没被计入 —— 实测踩过:
        snapshot 只显示现金, 看起来像净值腰斩)。
        """
        own = conn is None
        conn = conn or db.get_conn()
        try:
            px = pd.read_sql("SELECT ts_code, close, adj_factor FROM daily d "
                             "LEFT JOIN adj_factor a USING (ts_code, trade_date) "
                             "WHERE d.trade_date=?", conn, params=(trade_date,))
            m = px.set_index("ts_code")
            # 闲置现金计息(见 _accrue_interest): 净值 = 现金 + 累计利息 + 持仓市值
            interest = self._accrue_interest(conn, trade_date)
            mv = 0.0
            for code, pos in self.state["positions"].items():
                if code in m.index:
                    row = m.loc[code]
                    # 复权锚定(2026-09-16 资金线审查 FUND-01): **绝不能把锚落到 1.0**。
                    # 旧写法 `pos.get("adj") or row["adj_factor"] or 1.0` 在「daily 已同步、
                    # adj_factor 同步失败」的日子会把新持仓的锚定成 1.0, 次日真因子
                    # (老股累计因子常在 100+) 出现时 shares ×139 —— 净值爆炸。
                    # 新写法: 当日因子缺失时**保持 None**(不缩放、不锚定), 等真因子出现
                    # 再锚定 —— 正常路径逐位不变(有因子的日子行为一致), 只有病理路径被堵死。
                    row_adj = float(row["adj_factor"]) if (row["adj_factor"] and
                                                           pd.notna(row["adj_factor"])) else None
                    last_adj = float(pos["adj"]) if pos.get("adj") else None
                    cur_adj = row_adj if row_adj is not None else last_adj
                    if last_adj and cur_adj and abs(cur_adj - last_adj) > 1e-12:
                        pos["shares"] = float(pos["shares"]) * cur_adj / last_adj   # 除权再投资
                    if cur_adj is not None:
                        pos["adj"] = cur_adj
                    if pd.notna(row["close"]):
                        pos["last_px"] = float(row["close"])
                mv += float(pos["shares"]) * float(pos.get("last_px") or pos["cost"])
            nav = float(self.state["cash"]) + float(interest) + mv
        finally:
            if own:
                conn.close()
        self.state["last_mark"] = trade_date
        if record:
            # 2026-09-13: 净值点里带上**持仓快照**(股数) —— 归因(app/weekly/attrib.py)需要
            # "那一期持了什么、权重多少"。只存股数(不存价): 价从行情表取, 存价会与复权脱节。
            # 旧格式的点没有这几个键, 归因侧按"跳过该期"处理(向后兼容)。
            pt = {"date": trade_date, "nav": round(nav, 2),
                  # invested 只算**持仓市值**, 不含利息(否则"投入率"会被利息污染)
                  "invested": round(mv, 2),
                  "cash": round(float(self.state["cash"]), 2),
                  "interest": round(float(interest), 2),
                  "positions": {c: float(p["shares"])
                                for c, p in self.state["positions"].items()}}
            # 同一交易日重复盯市(开盘档 + 晚间兜底档) ⇒ **覆盖**, 不新增点。
            # 否则一天会留下两个净值点, 周数翻倍、收益被稀释(实测踩过)。
            if self.state["nav_history"] and self.state["nav_history"][-1]["date"] == trade_date:
                self.state["nav_history"][-1] = pt
            else:
                self.state["nav_history"].append(pt)
            if self.state["inception"] is None:
                self.state["inception"] = trade_date
        return nav

    # ---------------------------------------------------------------- 执行
    def execute_plan(self, plan: dict, trade_date: str, fill_fn=None,
                     dry_run: bool = False, use_quotes: bool = True,
                     allow_buys: bool = True) -> list:
        """把周频方案落到账本上: 先卖(不在目标里)后买(差额), 全部按生产成交规则。

        fill_fn(code, trade_date, side) -> 成交价 | None(None = 一字板/停牌 ⇒ 顺延)。

        **两档结构(与生产模拟盘一致)**:
          use_quotes=True(09:32 开盘档) → 用实时行情今开价 `_exec_price_open`;
            拿不到行情(停牌/接口失败)时返回 None ⇒ 顺延晚间档。
          use_quotes=False(18:05 晚间档) → 用日线开盘价 `_exec_price` 兜底,
            两个档位的**成交价口径相同**(都是当日开盘价), 所以账务等价。
        幂等性来自"按目标差额下单": 已在目标股数的持仓不会重复买入,
        因此同一个方案可以在两个档位各跑一次而不会重复成交。

        allow_buys=False ⇒ **风控停买**(规划 §6.1-4, 见 `app/weekly/risk.py`):
        仍然照方案卖出, 但一笔不买 ⇒ 敞口只降不升。默认 True = 行为不变。
        """
        conn = db.get_conn()
        try:
            if fill_fn is None:
                nl = NameLookup(conn)
                if use_quotes:
                    from app.datasource.quote_client import get_quotes
                    codes = sorted(set(list(self.state["positions"]) +
                                       [r["ts_code"] for r in plan.get("rows", [])]))
                    quotes = get_quotes(codes)
                    if quotes:
                        fill_fn = lambda c, d, s: _exec_price_open(  # noqa: E731
                            quotes, c, d, s, name_fn=nl.name_at)
                    else:
                        log.warning("周频袖套: 实时行情不可用 ⇒ 本档不下单, 顺延晚间档")
                        fill_fn = lambda c, d, s: None                  # noqa: E731
                else:
                    fill_fn = lambda c, d, s: _exec_price(              # noqa: E731
                        conn, c, d, s, name_fn=nl.name_at)
            fee_p = fee_params(cfg_mod.load_config())
            nav = self.mark(trade_date, conn=conn, record=False)      # 成交前估值(定预算)
            targets = {r["ts_code"]: int(r["shares"]) for r in plan.get("rows", [])}
            done = []

            # 1) 卖出: 不在目标名单里的持仓(T+1 校验: 当日买入不可卖)
            for code in list(self.state["positions"]):
                pos = self.state["positions"][code]
                if code in targets or pos.get("bought") == trade_date:
                    continue
                px = fill_fn(code, trade_date, "sell")
                if px is None:
                    continue                                  # 一字跌停/停牌 ⇒ 顺延
                amt = float(pos["shares"]) * px
                fee = trade_cost(amt, "sell", trade_date, fee_p)
                self.state["cash"] += amt - fee
                done.append({"date": trade_date, "ts_code": code, "side": "sell",
                             "shares": pos["shares"], "price": px, "fee": round(fee, 2),
                             "reason": "周频方案调出"})
                del self.state["positions"][code]

            # 2) 买入/补仓: 目标股数与手持的差额
            if not allow_buys:
                log.warning("周频袖套处于风控停买状态 ⇒ 本次只卖不买 (%s)", trade_date)
            for code, want in (targets.items() if allow_buys else ()):
                if code in self.state["positions"]:
                    # **留存持仓不动**(2026-09-13 回放实测修掉的分歧): 回测的硬规则是
                    # "留存股票不调股数", 只有名单变化才成交; 原实现按目标差额给留存持仓
                    # 补仓 ⇒ 每周多出一批买单, 换手与费用都比回测高(实测 22~89 笔/周 vs 回测口径 ~9%)。
                    # ⚠️ 2026-09-19 审查: 这条 `continue` 之后的"pos 可能不为 None"分支
                    # (原 `cur`/`delta` 与 `total = cur + afford` 那半段)是**不可达死代码** ——
                    # 已删除, 只留 `want <= 0` 这道真护栏(它才是原 `delta <= 0` 实际生效的部分)。
                    continue
                if want <= 0:
                    continue                                  # 目标股数非正 ⇒ 不动
                px = fill_fn(code, trade_date, "buy")
                if px is None:
                    continue                                  # 一字涨停 ⇒ 买不进, 顺延
                afford = board_mod.lot_shares(code, want)     # 整手; 不足一手则不动
                if afford <= 0:
                    continue
                amt = afford * px
                fee = trade_cost(amt, "buy", trade_date, fee_p)
                if amt + fee > self.state["cash"]:
                    continue                                  # 现金不足 ⇒ 顺延(不融资)
                self.state["cash"] -= amt + fee
                self.state["positions"][code] = {
                    "shares": float(afford), "cost": px, "bought": trade_date,
                    "last_px": px, "adj": None}
                done.append({"date": trade_date, "ts_code": code, "side": "buy",
                             "shares": afford, "price": px, "fee": round(fee, 2),
                             "reason": "周频方案买入"})
            if dry_run:                                     # 试跑: 不落账, 只返回将发生的成交
                return done
            self.state["trades"].extend(done)
            # 审计链(**产物 → 它的输入版本**): 记下这次成交用的是哪份方案、以及那份方案的
            # 输入指纹(代码版本/打分存档指纹/配置指纹, 见 plan.artifact_meta)。
            # 没有这一条, 半年后问"这笔为什么买"就只能靠锚点日期反推。
            self.state["last_plan"] = {
                "anchor": plan.get("anchor"), "applied_on": trade_date,
                "n_rows": len(plan.get("rows") or []), "n_trades": len(done),
                "meta": plan.get("meta") or {"note": "该方案文件生成于 artifact_meta 之前"}}
            nav = self.mark(trade_date, conn=conn, record=True)        # 成交后按收盘记净值
            # 2026-09-15（外部独立审查 F4）：**落账失败必须响**。save() 的 False 意味着
            # 「有别的写入者/拿不到锁 ⇒ 这次拒绝写」（见其 docstring）。若在这里吞掉，
            # 内存里成交已 extend、净值已记，而盘上什么都没写 ⇒ 调度器报 ok、账实分叉。
            # 抛出去 ⇒ `_weekly_staged` 记 error + 运维告警；今晚 18:05 兜底档会重试
            # （`_already_done` 判"当日未执行"），账本不会重复成交（幂等）。
            if not self.save():
                raise RuntimeError(
                    f"账本落账被拒绝（{self.path.name} 被其他进程修改或锁被占用）—— "
                    f"本次 {len(done)} 笔成交未写盘；请查看日志后由 18:05 兜底档/手动重跑")
            log.info("周频袖套 %s: 成交 %d 笔(买 %d / 卖 %d), NAV %.0f",
                     trade_date, len(done),
                     sum(1 for d in done if d["side"] == "buy"),
                     sum(1 for d in done if d["side"] == "sell"), nav)
            return done
        finally:
            conn.close()

    # ---------------------------------------------------------------- KPI
    def snapshot(self, benchmark: bool = True) -> dict:
        """袖套当前状态与业绩(总收益 / 年化 / 回撤 / 基准对照)。"""
        hist = self.state["nav_history"]
        out = {"capital": self.state["capital"], "inception": self.state["inception"],
               "last_mark": self.state["last_mark"], "n_positions": len(self.state["positions"]),
               "cash": round(float(self.state["cash"]), 2), "n_trades": len(self.state["trades"]),
               "interest": round(float(self.state.get("interest") or 0.0), 2),
               "halt": self.state.get("halt")}     # §6.1-4 风控停买状态(看板/KPI 都要看得见)
        if not hist:
            out["note"] = "还没有净值点(先跑 exec)"
            return out
        out["nav"] = round(float(hist[-1]["nav"]), 2)          # 一律给出最新净值
        if len(hist) < 2:
            out["note"] = "净值点不足两期, 尚无可计算的业绩读数(年化/回撤/超额)"
            return out
        nav = pd.Series([h["nav"] for h in hist],
                        index=pd.to_datetime([h["date"] for h in hist]))
        yrs = max(1e-9, (nav.index[-1] - nav.index[0]).days / 365.25)
        # 2026-09-21 修正: 原来是 `n_weeks = len(ret)`(日间差) —— 把**日点**当周数,
        # 于是 9 个日点(≈2 周)就越过了"≥8 周才报年化"这道守卫。改用真·自然周。
        # 2026-09-22: 上一步改完后 `ret = nav.pct_change().dropna()` 就**没人读了**
        # (原先是 `len(ret)` 的输入) —— 留着是死变量, `scripts/verify_settlement.py`
        # 的 C1 项会报红。已删。`pct_change()` 无副作用, 删掉不改变任何行为。
        n_weeks = week_anchors(nav.index)
        n_days = len(hist)
        out.update({
            "total_return": round(float(nav.iloc[-1] / nav.iloc[0] - 1), 4),
            "ann_return": round(float((nav.iloc[-1] / nav.iloc[0]) ** (1 / yrs) - 1), 4),
            "mdd": round(float((nav / nav.cummax() - 1).min()), 4),
            "weeks": n_weeks,
            "n_day_points": n_days,
            "n_week_anchors": n_weeks,
        })
        # 样本守卫(2026-09-12): 用一两周的数据做年化会把噪声放大成荒谬读数
        # (实测: 一周的基准 -2% 被年化成 -53%)。**不足 8 周只报累计, 不报年化/超额。**
        if n_weeks < MIN_REPORT_WEEKS:
            out["note"] = (f"样本 {n_days} 个净值点但只覆盖 **{n_weeks} 个自然周**"
                           f"(守则: ≥{MIN_REPORT_WEEKS} 周才报年化 ⇒ 约需 {MIN_REPORT_WEEKS * 5} 个日点上下);"
                           f" 当前只有累计收益 {100 * out['total_return']:+.2f}%")
            out.pop("ann_return")
            out.pop("mdd")
            return out
        if benchmark:
            conn = db.get_conn()
            try:
                # 2026-09-20 B1: 与回测报告腿同一把尺 —— 袖套净值按 `adj_factor` 缩放股数
                # (全收益), 基准却取价格指数 ⇒ excess_pp 被抬高一个基准股息率(实测 2.22pp/年)。
                # 取码统一走 config.benchmark_nav_code()；这里原来还自带一份硬编码指数代码
                # 当缺省(第三份真相)，一并消灭 —— 见 tests/test_weekly_ledger_bench.py 的静态断言。
                b = pd.read_sql("SELECT trade_date, close FROM index_daily WHERE ts_code=? "
                                "AND trade_date>=? ORDER BY trade_date",
                                conn, params=(cfg_mod.benchmark_nav_code(), hist[0]["date"]))
            finally:
                conn.close()
            if len(b) > 1:
                b["d"] = pd.to_datetime(b["trade_date"])
                b = b.set_index("d")["close"].reindex(nav.index).ffill().dropna()
                if len(b) > 1:
                    bm = float((b.iloc[-1] / b.iloc[0]) ** (1 / yrs) - 1)
                    out["bench_ann"] = round(bm, 4)
                    out["excess_pp"] = round(100 * (out["ann_return"] - bm), 2)
        return out
