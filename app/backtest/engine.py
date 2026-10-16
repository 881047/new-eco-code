"""月频 walk-forward 回测: 严格模拟 T+1、涨跌停不可成交、停牌顺延、双边成本

与实盘(builder/模拟盘)同一套整手/资金约束 (P0-2):
- 每月按固定资金 capital 建仓, 100 股整手, 买不起一手剔除, 现金余额计回购收益
- 股票数随资金自适应(与 app/portfolio/sizing.py 同一函数)

两条净值:
- nav:      纯选股组合(每月按模型打分建仓, 权益预算=capital 全额)
- sys_nav:  完整系统(权益仓位随市场状态调整, 现金部分计逆回购收益)
"""
import datetime
import json
import logging
import re

import numpy as np
import pandas as pd

from app import config as cfg_mod
from app import db
from app.factors.factor_engine import load_market
from app.paper.engine import MAX_EXEC_DAYS  # 顺延天数与模拟盘同一常量(2026-09-05 审查修复)
from app.portfolio import fees as fees_mod
from app.portfolio.board import board_of, limit_price, limit_ratio
from app.portfolio.sizing import allocate_lots
from app.models.trainer import HORIZON as TRAINER_HORIZON     # 标签持有期单一来源(2026-09-13)

log = logging.getLogger(__name__)

MAX_DELAY = MAX_EXEC_DAYS  # 买卖执行最多顺延交易日数
FALLBACK_CASH = 1.5  # 无回购数据时的年化现金利率(%)

# 限价入参量化(A/B 开关, **默认开启 = 现行口径**)。
# `_exec_side` 的 pre_close 先量化回 2 位小数再推导限价: 市场矩阵是 float32(见
# board.round2 docstring 的推导), `np.float32(2.10)=2.0999999`, ×0.95 后掉进
# `floor(x+0.5)` 的整数下方 ⇒ 限价低一分(只向下, 全样本 16.2 万格, 回测比真值乐观
# 1.07pp/年 —— 002638.SZ 连续 5 个一字跌停被判成卖得出去)。
#
# 2026-09-12 判决(预注册 `experiments/01_data/E5_float32_limit.py`, 脚本 sha
# 1e2574325686d44d, 判据跑数前冻结): **P2 = PASS** —— 开启后两窗的日频+月频净值序列
# 与「真值臂(读交易所 stk_limit 公布值)」**位级完全相同**(最大绝对差 0.000e+00,
# 四个 sha256 指纹一致), P1 守卫(关闭态逐位复现 pin `20260912_fullhist`)与
# P3(替身自证)同时 PASS, 真值臂 stk_limit 回退率 0.00%。
# ⇒ 推导路径现已与交易所公布值同源, pin 随之重钉。
# False = 修复前的口径, **只留给复现历史读数**(旧 pin 23.77%/20.47% 用它跑得出来)。
PRE_CLOSE_QUANTIZE = True

# ---------------------------------------------------------------------------
# 下面两个开关是 2026-09-12 审查 B 组的两个「改数字」项(P0-01/P0-02)。默认值 =
# **修复前**的现行口径 ⇒ 打开开关前生产/实验/pin 一切照旧。判据见预注册
# `experiments/09_cost/E6_exec_open_overlap.py`(跑数前冻结), 判决通过后才翻默认值。
# ---------------------------------------------------------------------------

# P0-01 开盘价成交(A/B 开关)。False = 现行口径: 顺延日**收盘价**成交并用收盘价判封板;
# True = 顺延日**开盘价**成交并用开盘价判封板。
#
# 审查证据: 模拟盘 `paper.engine._exec_price` 用**次日开盘价 + 滑点**成交、并按开盘价盲判
# 一字板(下单时看不到当天走势); 回测却用**执行日收盘价**成交、按收盘价判封板 —— 同一决策日,
# 回测在已知全天走势之后才决定成交与否, 与同段文档「与实盘同一套撮合规则」直接冲突。
# 方向不一致(高开回落股回测买得进、尾盘封板股回测吃到涨幅), 故必须实测而不能靠推理定方向。
#
# ✅ 2026-09-28 第 8 次重钉(P1.5): 判据已由 E6 产出并**双窗 PASS**, 默认值翻 True。
#    实测 Δ(见 experiments/09_cost/E6_exec_open_overlap_result.json):
#      sys_ann  prod 0.1940 → 0.2173 (+2.33pp) / full 0.1016 → 0.1157 (+1.47pp)
#      sys_mdd  prod −0.1966 → −0.1825(改善) / full −0.4210 → −0.3919(改善)
#    逐笔精度 c2: 1346/1346(prod)、1773/1773(full) 全同桶 bad=0。
#    机制(A 股隔夜负收益 + 日内正溢价, 源于 T+1 逆向选择): 开盘价系统性低于收盘价
#    ⇒ 开盘买更便宜。预注册 `plans/pin-exec-round-prereg-20260928.md`; 新 pin = `20260928_exe`。
#    旧 pin `20260928_excl5mdd` = 系统 22.12%/−22.10%(prod)、13.80%/−34.41%(full) ——
#    **历史值**, 现役读数见 config.yaml 的 `backtest_run` 段与 README「关键设计」。
#    ⚠️ 本轮实测 Δ 是 +7.01pp(prod)/+4.97pp(full), 比 E6 旧基线上的 +2.33pp 大 3 倍,
#    且**统计上不稳健**(diag_exec_pin_delta_20260928.py: 配对 t=1.84/1.74 未达 2、
#    符号一致率 54.8%/51.8%、最大 3 月占 Δ 比 67%/65%、月度 Δ 中位数仅 +0.018pp)
#    ⇒ **不得记作执行层 alpha**。翻开关的理由只是"与实盘同口径"(实盘一直是开盘价成交)。
FILL_AT_OPEN = True

# P0-02 重叠持仓的 |Δshares| 计费(A/B 开关)。False = 现行口径: 上月在册的股票当月
# 「零费零滑点」(`_month_return` 的 is_overlap 分支直接 return 收益); True = 对
# |本月股数-上月股数| 按**该笔成交额**补单边佣金+滑点。
#
# 审查证据: 等权+恒定权益仓位下留存股的目标金额每月随组合总值与自身价格变动 ⇒ 股数必然
# 变化(d = sh_new - sh_prev 通常非 0), 即真实发生了 Δshares 的买卖; 模拟盘 `apply_plan`
# 按 delta 真实计费(含最低佣金非线性), 回测这段却是免费的。
# 方向确定: 只减不增(费用恒为负项), 即现行数字是**上界**。
#
# ✅ 2026-09-28 第 8 次重钉(P1.5): 与 FILL_AT_OPEN 同批翻 True(搭车, 消除已知口径缺陷)。
#    E6c 实测(`experiments/09_cost/E6c_overlap_recheck_f32_result.json`): 重叠成本
#    ≈0.12%/年、30 键逐位相同 ⇒ 收益影响可忽略, 但**口径更真实**(模拟盘 apply_plan
#    按 delta 真实计费, 回测不应免费)。预注册同上。
OVERLAP_REBAL_COST = True


def _mtm_adj(m, dates, pos, code):
    """最近可得复权收盘价(停牌无当日行时向前找), 供结转仓位盯市与回退"""
    for i in range(pos, -1, -1):
        dd = dates[i]
        cv = m["close"].loc[dd, code]
        av = m["adj"].loc[dd, code]
        if pd.notna(cv):
            return cv * av if pd.notna(av) else cv
    return None


def _mtm_raw(m, dates, pos, code):
    """最近可得未复权收盘价(与 _mtm_adj 同一基准日), 作结转仓位盈亏的本金基数
    (2026-09-07 修复: 盈亏=未复权市值×复权环比, 必须同时留存未复权价)"""
    for i in range(pos, -1, -1):
        cv = m["close"].loc[dates[i], code]
        if pd.notna(cv):
            return float(cv)
    return None


def _month_return(bp_adj: float, sp_adj: float, is_overlap: bool, slip: float,
                  cost: float, cost_s: float = None, delta_cost: float = 0.0) -> float:
    """单月持仓收益: 新建仓收双边成本+滑点; 重叠持仓(上月在册, 不换手)无任何交易成本
    (2026-09-05 第二轮复查 N2: 原 else 分支错误地乘了 (1-slip)(1-cost)/(1+slip)≈每月-0.5%)

    2026-09-10 逐笔费用模型: cost/cost_s = 买入/卖出各自的**实际单边费率**(按该笔成交额
    算, 见 fees.py —— 最低佣金使小仓位费率显著更高: 3000 元买 0.167% vs 1500 元买 0.334%)。
    cost_s 省略时等于 cost, 保持旧扁平口径的调用方兼容(既有单测/外部脚本)。

    delta_cost(2026-09-12 P0-02): 重叠持仓的 |Δshares| 补计的单边成本(占本月持仓
    名义额的比例, 见 `_month_pnl` 的 `_overlap_delta_frac`)。0.0 = 修复前口径。"""
    if is_overlap:
        return sp_adj / bp_adj - 1 - float(delta_cost or 0.0)
    if cost_s is None:
        cost_s = cost
    return (sp_adj * (1 - slip) * (1 - cost_s)) / (bp_adj * (1 + slip) * (1 + cost)) - 1


def _stuck_basis(bp_adj: float, slip: float, cost: float) -> float:
    """结转仓位成本基准 = 买入复权价×(1+滑点)×(1+佣金)
    (2026-09-05 第二轮复查 N3: 原漏乘 (1+cost), 买入佣金在这条边缘路径漏计)"""
    return bp_adj * (1 + slip) * (1 + cost)


def _side_rate(shares, price, date, side, cost, fees):
    """单边费率解析(2026-09-10 逐笔费用模型): 按本笔实际成交额(股数×价)取真实费率,
    含最低佣金下限、卖出印花税/过户费。fees/价格/日期任一缺失(未启用逐笔口径的旧
    调用或内部单测、无执行价边缘路径)时退回扁平 cost。str 与 Timestamp 日期都接受。"""
    if fees is None or not price or date is None:
        return cost
    return fees_mod.effective_rate(shares * price, side, date, fees)


def _roll_stuck(stuck: dict, m, dates, pos1, st_codes, slip, cost, fees: dict = None,
                name_of=None, exec_window: str = ""):
    """结转仓位月度处理(2026-09-05 审查修复): 卖不出的持仓跨月盯市结转 —— 原"卖不出
    就消失"等价于按成本价抹零, 复牌补跌由组合承担才对。返回 (本月占用资金, 本月盈亏);
    可卖时按复权收盘变现(含卖出成本与滑点)并从 stuck 移除。
    2026-09-05 第三轮复查 M1: 盈亏基数=股数×上月盯市价(更新前的 last_adj), 而非固定
    原始成本 —— 连续停牌多月时原实现按月放大盈亏(10→5→4 记 -2000, 正确 -1000)。
    2026-09-07 两层修复(口径对齐正常持仓 _month_pnl/日频盯市/模拟盘 paper.engine):
    盈亏 = 股数×上月末未复权价(last_raw)×复权环比, 而非"股数×(复权价-复权盯市价)"
    —— 后者在 adj≠1 时被复权因子整体放大(adj=1.686 即虚增 68.6%); 盯市后同步滚动
    last_raw。连续停牌多月基数为上月末市值(10→5→4 记 -1000, 不按月放大)。
    期末仍未卖出的结转仓位按最近收盘盯市计入净值, 未扣最后一次卖出滑点+成本(极边缘)。"""
    invested = 0.0
    pnl = 0.0
    for code in list(stuck):
        sv = stuck[code]
        invested += sv["bp_raw"]
        sh = sv["shares"]
        sp_adj, sp_raw, si = _exec_side(m, dates, pos1, code, "sell", st_codes,
                                        name_of=name_of, exec_window=exec_window)
        if sp_adj:
            # 2026-09-10 逐笔费用: 卖出费率按该笔实际成交额(最低佣金对小仓位更重)
            rate_s = _side_rate(sh, sp_raw, dates[si] if si is not None else None,
                                "sell", cost, fees)
            proceeds = sp_adj * (1 - slip) * (1 - rate_s)
            # 未复权市值 × 复权环比(总回报口径, 自动吸收期间除权)
            pnl += sh * sv["last_raw"] * (proceeds / sv["last_adj"] - 1)
            del stuck[code]  # 变现, 资金回归现金池
        else:
            v = _mtm_adj(m, dates, pos1, code)
            if v:
                pnl += sh * sv["last_raw"] * (v / sv["last_adj"] - 1)
                sv["last_adj"] = v
                rv = _mtm_raw(m, dates, pos1, code)  # 同步滚动未复权基准
                if rv is not None:
                    sv["last_raw"] = rv
    return invested, pnl


def _overlap_delta_frac(sh, sh_prev, bp_raw, date, slip, cost, fees=None):
    """重叠持仓的 |Δshares| 单边成本, 以「占本月持仓名义额的比例」返回(2026-09-12 P0-02)。

    等权 + 恒定权益仓位下, 留存股的目标金额每月随组合总值与自身价格一起变动 ⇒ 本月股数
    `sh` 与上月 `sh_prev` 通常不等, 真实发生了 |d| 股的买卖。修复前 `_month_return` 的
    is_overlap 分支对这段零费零滑点(代码相同就免单), 而模拟盘 `apply_plan` 是按
    delta=目标-已持 真实计费的 —— 两个引擎对同一件事记了两笔账。

    费率按**该笔 delta 的实际成交额**(|d|×价)取, 含最低佣金的非线性(与 `_side_rate`
    同源); d>0 补买走买入费率, d<0 减卖走卖出费率。两笔都发生在**月初再平衡时点**,
    故两边都用本月执行价 bp_raw(回测的月初再平衡约定; 模拟盘按实际成交日开盘价,
    差异 < 1 个交易日的价格变动 —— 明写为近似)。0.0 = 无调仓(与修复前逐位一致)。"""
    if not sh or sh_prev is None or not bp_raw or bp_raw <= 0:
        return 0.0
    d = float(sh) - float(sh_prev)
    if d == 0:
        return 0.0
    rate = _side_rate(abs(d), bp_raw, date, "buy" if d > 0 else "sell", cost, fees)
    lam = ((1 + slip) * (1 + rate) - 1) if d > 0 else (1 - (1 - slip) * (1 - rate))
    return abs(d) / float(sh) * lam


def _month_pnl(alloc: dict, execs: dict, prev_picks, stuck: dict, m, dates, pos1,
               st_codes, slip, cost, stop_loss=None, cash_r=0.0, hold_days=1.0,
               fees: dict = None, name_of=None, prev_shares: dict = None,
               exec_window: str = ""):
    """单套分配的本月 (占用资金, 盈亏, 止损释放现金的计息补偿)。
    2026-09-05 第五轮复查 P5-2: 满仓线与系统线各持一套 stuck 账本, 分别调用本函数。
    2026-09-06 第十三轮 R13-1: stop_loss 非 None 时逐日模拟日频止损 —— 买入次日起
    按收盘复权价相对买入成本跌破阈值即当日收盘卖出。一阶近似: 重叠持仓用当月买入价
    作基准(实盘 monitor 用真实历史成本)、结转仓位不参与、止损按收盘判定(实盘开盘)"""
    invested, pnl = _roll_stuck(stuck, m, dates, pos1, st_codes, slip, cost, fees=fees,
                                name_of=name_of, exec_window=exec_window)
    cash_credit = 0.0
    for code, sh in alloc["shares"].items():
        bp_adj, bp_raw, bi = execs[code]
        sp_adj, sp_raw, si = _exec_side(m, dates, pos1, code, "sell", st_codes,
                                        name_of=name_of, exec_window=exec_window)
        invested += sh * bp_raw
        # 2026-09-10 逐笔费用模型: 买卖各自按**本笔实际成交额**取单边费率(含最低佣金
        # 的非线性); 成交额缺失(无执行价)时回退扁平 cost
        rate_b = _side_rate(sh, bp_raw, dates[bi] if bi is not None else None,
                            "buy", cost, fees)
        rate_s = _side_rate(sh, sp_raw, dates[si] if si is not None else None,
                            "sell", cost, fees)
        if not bp_adj or not sp_adj or bp_adj <= 0:
            if bp_adj and bp_adj > 0:
                # 买入成功但卖不出: 转入结转仓位按月盯市, 复牌补跌由组合承担而非按
                # 成本价抹零。成本基准=买入复权价×滑点×佣金(N3: 原漏乘 (1+cost))
                entry_adj = _stuck_basis(bp_adj, slip, rate_b)
                v = _mtm_adj(m, dates, pos1, code)
                if code not in stuck:
                    last_adj = v if v else entry_adj
                    # 同基准日未复权收盘(盈亏本金基数); 买入当月全程无行情则回退买入价
                    last_raw = _mtm_raw(m, dates, pos1, code)
                    if last_raw is None:
                        last_raw = float(bp_raw)
                    stuck[code] = {"shares": sh, "bp_raw": sh * bp_raw,
                                   "last_adj": last_adj, "last_raw": last_raw}
                    # 2026-09-07 第二层修复: 买入执行→当月末盯市的浮动盈亏原被下方
                    # continue 整段漏记; 按新仓口径(未复权投入×复权环比, 买入含费/
                    # 尚未卖出不收卖出费)补计当月, 与正常持仓 _month_return 一致
                    pnl += sh * bp_raw * (last_adj / entry_adj - 1)
            continue
        # 日频止损模拟(R13-1): 买入次日到月末卖出日前一天逐日检查
        if stop_loss is not None and bi is not None:
            entry = bp_adj * (1 + slip) * (1 + rate_b)
            end_i = (si - 1) if si is not None else (pos1 - 1)
            hit = False
            for k in range(bi + 1, max(bi + 1, end_i + 1)):
                dd = dates[k]
                cv = m["close"].loc[dd, code]
                av = m["adj"].loc[dd, code]
                vv = m["vol"].loc[dd, code]
                if pd.isna(cv) or pd.isna(av) or pd.isna(vv) or vv <= 0:
                    continue  # 停牌/无行情: 当日不检查
                if (cv * av) / entry - 1 <= stop_loss:
                    rate_s_hit = _side_rate(sh, cv, dd, "sell", cost, fees)
                    proceeds = cv * av * (1 - slip) * (1 - rate_s_hit)
                    # 2026-09-07 第三处同源修复: 与正常新仓 _month_return 同口径 ——
                    # 未复权买入额 sh*bp_raw × 含双边费复权环比(proceeds/entry-1);
                    # 原 sh*(proceeds-entry) 为"股数×(复权卖净-复权买含)", adj!=1 时被
                    # 复权因子放大、且费用本金口径与主框架不一致。上方触发判定是比率
                    # (adj 同约、无放大), 保持不动。
                    pnl += sh * bp_raw * (proceeds / entry - 1)
                    # 释放现金的剩余期计息补偿(日历日占比近似)
                    days_after = (dates[end_i] - dd).days
                    cash_credit += sh * bp_raw * cash_r * days_after / max(1, hold_days)
                    hit = True
                    break
            if hit:
                continue  # 已止损: 不再走月末卖出路径
        overlap = bool(prev_picks and code in prev_picks)
        # P0-02: 重叠持仓的 |Δshares| 补计单边成本(False=修复前口径, delta_cost 恒 0)
        fdelta = 0.0
        if overlap and OVERLAP_REBAL_COST:
            fdelta = _overlap_delta_frac(sh, (prev_shares or {}).get(code), bp_raw,
                                         dates[bi], slip, cost, fees)
        r = _month_return(bp_adj, sp_adj, overlap, slip, rate_b, rate_s, delta_cost=fdelta)
        pnl += sh * bp_raw * r
    return invested, pnl, cash_credit


# 尾盘 1h VWAP 查找表 {(ts_code, "YYYYMMDD"): 未复权 VWAP} —— **仅 A/B 测量注入**。
# 生产路径恒为 None(与 min60 零耦合); 由 scripts/oneoff/ab_exec_window_20260928.py 的
# set_min60_tail() 注入, 用完置回 None。见 plans/exec-window-ab-prereg-20260928.md。
_MIN60_TAIL: dict = None


def set_min60_tail(map_: dict) -> None:
    """注入/清空尾盘 1h VWAP 表(None=清空)。仅供一次性 A/B 脚本调用, 生产不调用。"""
    global _MIN60_TAIL
    _MIN60_TAIL = map_


def _exec_side(m, dates, pos, code, side, st_codes=None, name_of=None,
               exec_window: str = ""):
    """单只股票的买卖执行价: 顺延找首个可交易日, 超限作废(资金留现金)
    撮合口径(2026-09-05 审查修复):
    - 回测执行价=顺延日收盘价(README 披露), 故可交易性判定也用收盘价 —— 原用开盘价
      判定+收盘价成交, 口径错配: 尾盘封死涨停/跌停的收盘价被当成可成交价(两头虚高),
      而 T 字板(开盘封板盘中打开)又被误判不可成交
    - 封死涨停(收盘≥涨停价)买不进 / 封死跌停(收盘≤跌停价)卖不出; T 字板收盘低于涨停价
      → 收盘可成交 ✓
    - 停牌(vol=0)顺延, 满 5 个交易日后作废。原超限后强制按最后收盘价成交,
      停牌后复牌补跌的票在回测里被虚高
    - 限幅按板块+股票状态(2026-09-07 全市场扩展): 主板 10%/ST 5%、创业/科创 20%、
      北交所 30%。st_codes 可为集合(当前名称近似, 旧口径)或可调用 is_st(code, dstr)
      (2026-09-05 第七轮 N7-6: 按执行日时点名称 PIT, 与 trainer 同口径);
      None=按板块普通限幅。
      2026-09-12 D06/D07 验收: 新增 `name_of(code, dstr) -> 当日名称` —— 退市整理期
      ("退"±10%)与 S股("S"前缀 ±5%)两档由**名称**而非代码前缀决定, 只有 is_st 这个
      bool 传不进去。不传时这两档按板块普通档(修复前的行为, 保守方向不虚高)
    返回 (复权价(含股息), 未复权收盘价(整手预算用), 执行日位置索引),
    无有效价返回 (None, None, None)。2026-09-06 第十三轮 R13-1: 第三元素供日频止损
    模拟定位买入/卖出执行日。"""
    n = len(dates)
    if pos + 1 >= n:
        return None, None, None
    for k in range(1, MAX_DELAY + 1):
        dd = dates[min(pos + k, n - 1)]
        cv = m["close"].loc[dd, code]
        av = m["adj"].loc[dd, code]
        vv = m["vol"].loc[dd, code]
        pv = m["pre_close"].loc[dd, code]
        if pd.isna(cv):
            continue
        if pd.isna(vv) or vv <= 0:
            continue  # 停牌顺延
        # 限幅按板块+执行日时点状态(2026-09-07 全市场扩展: 原固定 5%/10% 主板口径,
        # 现 创业/科创 20%、北交 30%、主板 ST 5%; "退"按普通限幅见 is_st_only)。
        # 可调用时用 PIT 名称, 集合为旧口径近似
        # 2026-09-12 D06/D07 验收修复: **必须传执行日** —— 创业板 2020-08-24 注册制
        # 改革前是 ±10%(ST ±5%), 不传日期会把当年已封死 +10% 涨停(生产根本买不到)的
        # 创业板票在回测里判成可买。交易所 stk_limit 对照实测: 2017~2019 年创业板
        # 99.9~100% 是 10% 档、0% 是 20% 档, 而修复前引擎一律按 20% 判。
        ds = dd.strftime("%Y%m%d")
        nm = name_of(code, ds) if name_of is not None else None
        # name_of 另作 name_of= 传入: 创业板退市整理期的**新老划断**要回查改革日当天
        # 该股名称是否已含"退"(存量股沿用 ±10%)。只在"退"字格子上被调用。
        if st_codes is None:
            ratio = limit_ratio(code, False, ds, nm, name_of)
        elif callable(st_codes):
            ratio = limit_ratio(code, st_codes(code, ds), ds, nm, name_of)
        else:
            ratio = limit_ratio(code, code in st_codes, ds, nm, name_of)
        # 执行窗口(2026-09-28 P1 A/B, 预注册 plans/exec-window-ab-prereg-20260928.md):
        #   exec_window=""      ⇒ 沿用全局 FILL_AT_OPEN 开关(**缺省路径逐位不变 ⇒ 不换 pin**)
        #   "close"/"open"      ⇒ 收盘价/开盘价(显式优先于全局开关)
        #   "vwap"              ⇒ 全日 VWAP = amount*10/vol(amount 千元 / vol 手 ⇒ 元/股)
        #   "vwap_tail"         ⇒ 尾盘 1h VWAP(需先 set_min60_tail 注入, 未注入则该日顺延)
        # **所有窗口价一律取未复权基准, 返回时统一 px*av** —— min60 是 adjustflag=3(不复权),
        # 与 daily 裸价同基准 ⇒ 绝不二次乘 adj(预注册 §2.1)。
        w = exec_window or ("open" if FILL_AT_OPEN else "close")
        if w == "close":
            px = cv
        elif w == "open":
            op = m["open"].loc[dd, code]
            if pd.isna(op):
                continue  # 无开盘价 ⇒ 该日不可成交(模拟盘 _exec_price 同判)
            px = round(float(op), 2)  # 矩阵 float32 ⇒ 量化回分(与 PRE_CLOSE_QUANTIZE 同源)
        elif w == "vwap":
            am = m["amount"].loc[dd, code] if "amount" in m else np.nan
            if pd.isna(am) or am <= 0:
                continue  # 无成交额 ⇒ 该日不可成交
            px = round(float(am) * 10.0 / float(vv), 2)
        elif w == "vwap_tail":
            tv = _MIN60_TAIL.get((code, ds)) if _MIN60_TAIL else None
            if tv is None or not np.isfinite(tv) or tv <= 0:
                continue  # 该日无尾盘 1h bar ⇒ 不可成交(顺延下一日)
            px = round(float(tv), 2)
        else:
            raise ValueError(f"未知执行窗口: {w}")
        if pd.notna(pv) and pv > 0:
            # 2026-09-08 审查: 涨跌停价改交易所四舍五入(round2), 原 round 银行家舍入差 1 分
            # 2026-09-12 D06/D07 验收已证实的缺陷: `pv` 来自 float32 市场矩阵, `round2` 的
            # 1e-9 容差按 float64 标定 ⇒ **162,414 格限价取低一分**(只向下, 见 board.round2
            # docstring 与 `experiments/01_data/D06_D07_limit_fill.py` §E1/E2)。组合层代价
            # = 比真值限价高 1.07pp/年(002638.SZ 连续 5 个一字跌停被判成卖得出去)。
            # 修复 = 量化回分(下面 PRE_CLOSE_QUANTIZE): 2026-09-12 E5 预注册判决通过
            # (与真值臂位级一致), 开关默认已翻开; False 仅用于复现修复前的历史读数。
            pv_f = round(float(pv), 2) if PRE_CLOSE_QUANTIZE else float(pv)
            # 2026-10-05: 北交所取整规则接线(上轨 floor/下轨 ceil, 见 board.limit_price)
            # —— 非北交所板内部就是 round2 半进位, 与旧口径逐位相同(不换 pin)。
            limit_up = limit_price(pv_f, ratio, board_of(code))
            limit_dn = limit_price(pv_f, -ratio, board_of(code))
            if w in ("vwap", "vwap_tail"):
                # 封板判定必须随窗口换(预注册 §2.3): VWAP 是全天均价, 用它跟限价比会
                # 漏判盘中封死 ⇒ 一律按**全天封死**判(买 low≥涨停 / 卖 high≤跌停)。
                lo = m["low"].loc[dd, code]
                hi = m["high"].loc[dd, code]
                if side == "buy" and pd.notna(lo) and lo >= limit_up - 0.001:
                    continue  # 全天封死涨停: 买不进
                if side == "sell" and pd.notna(hi) and hi <= limit_dn + 0.001:
                    continue  # 全天封死跌停: 卖不出
            else:
                if side == "buy" and px >= limit_up - 0.001:
                    continue  # 封死涨停: 买不进(收盘口径=收盘封死; 开盘口径=一字板)
                if side == "sell" and px <= limit_dn + 0.001:
                    continue  # 封死跌停: 卖不出(同上)
        if pd.notna(av):
            return (px * av, px, min(pos + k, n - 1))
        # 2026-09-19 审查 A6: 走到这里 = `adj_factor` 缺行 ⇒ 这一腿**静默**退化成"未复权价"
        # 收益, 而同月其它腿仍是全收益 —— adj_factor 是**累积量**, 单边退化会把一次分红
        # 注入成很大的幻影月盈亏, 且事后无处可查。数值维持原样(今天全库仅 1 行缺因子
        # 且无 model_score, 进不了候选 ⇒ 改了反而出假 Δ), 只把静默变成可观察:
        # 与 `portfolio/monitor.py:206` 对同类缺复权因子的处理同向。
        log.warning("回测执行: %s @%s 缺 adj_factor ⇒ 该腿按未复权价计(收益口径退化)",
                    code, dd.strftime("%Y%m%d"))
        return (px, px, min(pos + k, n - 1))
    return None, None, None  # 顺延超限: 订单作废(与模拟盘 expired 一致)


def _industry_for(cand: pd.DataFrame, industry_map: pd.DataFrame) -> pd.Series:
    """候选股行业列(2026-09-05 审查修复): dict 映射取正确行业值, 且索引必须重建为
    ts_code —— 原 map(Series) 的值与索引双错位, DataFrame 构造器按 ts_code 对齐后
    行业全 NaN → 行业上限完全失效(审查实证: 回测组合分配失真, 基线不可信)"""
    ind_map = industry_map.set_index("ts_code")["industry"].to_dict()
    return pd.Series(cand["ts_code"].map(ind_map).to_numpy(), index=cand["ts_code"])


def _hysteresis_keep(prev_picks, lot_df: pd.DataFrame, n: int, hyst: int) -> list:
    """迟滞带保留名单(R11-5): 上月持仓中仍在候选池(可成交)的股票, 按池内分数排序后
    最多保留 n 只 —— 市场状态收缩(defense 只数变小)时只保最高分部分, 其余随换仓卖出。
    hyst<=0 或无上月持仓时返回空列表(与历史口径逐位一致)。"""
    if not hyst or not prev_picks:
        return []
    keep = [c for c in prev_picks if c in lot_df.index]
    sc = lot_df["score"].to_dict()
    return sorted(keep, key=lambda c: sc.get(c, -np.inf), reverse=True)[:n]


def _load_inputs(top_n: int = None, score_table: str = "model_score",
                 run_tag: str = "", state_stocks: dict = None,
                 candidate_top: int = None, start: str = None,
                 end: str = None, hysteresis: int = None,
                 anchor: str = "month") -> dict:
    """P3-1 第 1 步(2026-09-20): run_backtest 的「输入加载 + 派生量」段。

    判据冻结在 plans/p3-1-run-backtest-refactor-20260920.md ——
    抽出后必须与拆分前**逐位一致**(A1 metrics / A2 月频净值 / A3 日频盯市, Δ=0)。

    只做「读配置 / 读库 / 构造派生量」, 不碰 _exec_side / _month_return /
    PRE_CLOSE_QUANTIZE / tradable_mask / allocate_lots(CLAUDE.md §1 的换 pin 位)。

    返回 dict 而非长元组: 本段产出 25 个值, 元组解包极易错位。
    """
    if not re.fullmatch(r"[a-z][a-z0-9_]*", score_table):
        raise ValueError(f"非法表名: {score_table}")
    cfg = cfg_mod.load_config()
    # 状态自适应持仓只数(2026-09-05): top_n 显式指定(对照组扫描)时固定只数;
    # 未指定时按当月市场状态取 state_stocks 映射(参数优先, 否则 config), 与 build_plan 同一套规则
    state_map = state_stocks if state_stocks is not None else (cfg.get("state_stocks") or {})
    top_n_fixed = top_n
    top_n = top_n or int(cfg.get("max_stocks", 12))
    if not top_n_fixed and state_map:
        log.info("持仓只数随市场状态调整: %s (回退 %d)", state_map, top_n)
    cand_top = int(candidate_top if candidate_top is not None
                   else cfg.get("stock_candidate_top", 20))
    # 迟滞带(R11-5, 2026-09-08): 在册持仓未跌出 top(n+hyst) 不换仓 —— 保留名单分数
    # 置顶, 其余名额由当月 top-n 按分补齐, 语义 = kept ∪ (top-n \ kept), 不产生向后
    # 补位。降换手与风格高点接盘; 默认 0=关闭(与历史口径逐位一致, golden 快照不扰动)
    hyst = max(0, int(hysteresis if hysteresis is not None
                      else cfg.get("hysteresis_buffer", 0)))
    cost = float(cfg.get("single_side_cost", 0.0015))
    # 2026-09-10 逐笔费用模型(config `fees` 段, 默认佣金万2.5/最低5元/印花税/过户费):
    # 成本按每笔实际成交额计, 最低佣金对小仓位的非线性惩罚被正确计入。cost 退化为
    # **预算预留**(整手分配用), 与实盘下单前预留资金同义。
    fees_p = fees_mod.fee_params(cfg)
    slip = float(cfg.get("paper", {}).get("slip", 0.001))  # 与模拟盘同口径(2026-09-05 诊断报告修复)
    run_date = datetime.date.today().strftime("%Y%m%d") + (f"_{run_tag}" if run_tag else "")

    m = load_market()
    dates = list(m["close"].index)

    with db.conn_ctx() as conn:
        # conn_ctx 已绑定连接(勿再 get_conn: 新开的那个无人关)
        scores = pd.read_sql(f"SELECT ts_code, trade_date, score FROM {score_table} ORDER BY trade_date", conn)
        # 2026-09-08 用户拍板: 训练全市场、选股排除科创板(与 builder 同口径, 见 config
        # star_enabled 注释) —— 科创"候选名额占位"机制在 1 万本金下拖累 -87% 累计
        if not cfg.get("star_enabled", False):
            # 2026-09-13: 判据唯一来源 board.is_star（含 689 CDR）；与 builder/daily_score 同口径
            from app.portfolio.board import is_star
            scores = scores[~scores["ts_code"].map(is_star).fillna(False)]
        industry_map = pd.read_sql("SELECT ts_code, industry FROM stock_basic", conn)
        repo = pd.read_sql("SELECT trade_date, overnight FROM repo_rate", conn)
        regime = pd.read_sql("SELECT trade_date, state FROM regime", conn)
        # 月末交易日集合: 回测只用月末评分日, 防止日频推分混入破坏月频语义
        me_dates = set(pd.read_sql(
            "SELECT MAX(cal_date) AS d FROM trade_cal WHERE is_open=1 GROUP BY substr(cal_date,1,6)", conn)["d"])
    if scores.empty:
        raise RuntimeError(f"{score_table} 为空, 请先运行 scripts/train_model.py")

    # 2026-09-21 周频化：anchor 控制**调仓频率**(判据 = 用哪些评分日)。
    #   "month"(默认) = 只保留月末(me_dates) —— 与拆分前**逐位一致**，现役 pin 用它；
    #   "week"        = 保留全部评分日(周锚点) —— 供「周频调仓」A/B 使用。
    # 2026-09-27 双月化(创新③ 持有期 A/B, plans/holdperiod-ab-prereg-20260927.md)：
    #   "2month_even"/"2month_odd" = 月末锚点每隔一个保留(双相位防相位脆弱) ⇒ 持有 2 个月。
    # ⚠️ 加法式缝：不改默认 —— "month" 逐位不变, 现役读数一动不动。
    if anchor == "week":
        sd = [d for d in sorted(scores["trade_date"].unique())]
    elif anchor in ("2month_even", "2month_odd"):
        md = [d for d in sorted(scores["trade_date"].unique()) if d in me_dates]
        sd = md[::2] if anchor == "2month_even" else md[1::2]
    else:
        sd = [d for d in sorted(scores["trade_date"].unique()) if d in me_dates]
    if start or end:
        in_range = [d for d in sd
                    if (not start or d >= start) and (not end or d <= end)]
        tail = [d for d in sd if d > end] if end else []
        sd = in_range + tail[:1]  # 末期卖出价用窗口后一个评分日
    if len(sd) < 3:
        raise RuntimeError("评分期数不足")
    # 限幅判定用执行日时点名称(2026-09-05 N7-6, 2026-09-06 第十轮 N10-4 统一公共
    # 模块): 原取当前 ST/退集合 —— 当前戴帽而历史未戴/当前摘帽而历史戴帽均有边界
    # 偏差; 与 trainer/builder/paper/monitor 同用 names.NameLookup 时点名称链
    from app.models.names import NameLookup
    nl = NameLookup()

    def is_st(code: str, dstr: str) -> bool:
        # 2026-09-07 全市场扩展: 限幅用 ST-only(风险警示) —— 退市整理期按板块普通限幅
        return nl.is_st_only(code, dstr)

    ac = (m["close"] * m["adj"])
    # 2026-09-13: 原写字面 21 —— 改为生产标签的持有期常量（**数值不变**，只是不再两处各写一份）。
    # ⚠️ 注意这里仍是"朴素标签"（不含退市/长停尾部回退），与 `trainer.forward_return` 有意不同：
    # 回测用它是为了与既有 pin 的数值逐位一致；要统一必须走 A/B（独立审查 §2.3/#5，已登记）。
    # ⚠️ 已知偏差(2026-09-20 第一步量化实测，产物 experiments/b_stage_20260920/
    # audit_b_stage_first_step.json，sha 354cae920b94ddf6)：这里从**评分日**起算未来 21 日，
    # 而实际建仓在 `dates[pos+1]` ⇒ 错开 1 个交易日；且退市尾部 NaN 被 dropna 丢弃。
    # 86 个截面 / 344,682 格实测：IC 现行 0.097166 vs 对齐到执行日 0.096040 ⇒ **Δ=0.001126**，
    # 远小于门(0.005)且不跨任何判档 ⇒ 按纪律**不改**（改它要让 5 处文档的 IC 一次性刷新而
    # 无决策受益，见 14 号三补三十）。`dropna` 掉的 4,971 格里名称含"退/ST"的 162 格(代理口径)。
    fut = ac.shift(-TRAINER_HORIZON) / ac - 1
    repo_s = repo.set_index("trade_date")["overnight"].astype(float).sort_index() if not repo.empty else pd.Series(dtype=float)
    state_s = regime.set_index("trade_date")["state"] if not regime.empty else pd.Series(dtype=str)
    wmap = cfg.get("regime_equity_weight", {}).get(cfg.get("risk_profile", "balanced"), {})

    return dict(
        cfg=cfg,
        state_map=state_map,
        top_n=top_n,
        top_n_fixed=top_n_fixed,
        cand_top=cand_top,
        hyst=hyst,
        cost=cost,
        fees_p=fees_p,
        slip=slip,
        run_date=run_date,
        m=m,
        dates=dates,
        scores=scores,
        industry_map=industry_map,
        repo=repo,
        regime=regime,
        me_dates=me_dates,
        sd=sd,
        nl=nl,
        is_st=is_st,
        ac=ac,
        fut=fut,
        repo_s=repo_s,
        state_s=state_s,
        wmap=wmap,
    )


def run_backtest(top_n: int = None, score_table: str = "model_score", run_tag: str = "",
                 state_stocks: dict = None, candidate_top: int = None,
                 start: str = None, end: str = None, industry_floor: float = None,
                 stop_loss: float = None, hysteresis: int = None,
                 st_filter: bool = True, anchor: str = "month",
                 exec_window: str = "", rank_weighted: bool = False) -> dict:
    """start/end: 限定评估窗口的评分日区间(YYYYMMDD, 含端点) —— walk-forward 样本外
    验证用(2026-09-05 第七轮 N7-0)。窗口内首期无 prev_picks/结转(重新起步), 末期
    收益仍用到窗口后一个评分日作卖出价。
    st_filter: 候选池 PIT ST/退 过滤开关(默认 True, 与 builder 同口径)。False=修复前
    口径(可买 ST/退), 仅供归因 A/B(2026-09-09), 生产与 golden 一律默认。
    exec_window: 执行时点窗口(2026-09-28 P1 A/B, 预注册
    plans/exec-window-ab-prereg-20260928.md)。**空串=沿用全局 FILL_AT_OPEN 开关,
    缺省路径逐位不变 ⇒ 不换 pin**; 非空仅供一次性 A/B 测量("close"/"open"/"vwap"/
    "vwap_tail"), 生产与 golden 一律缺省。
    rank_weighted: 分数排名加权 sizing(2026-10-02, 预注册
    plans/rankweight-prereg-20261002.md 的 A/B 缝)。False(缺省)= 等权逐位不变;
    True 仅供 A/B 测量(传递至 allocate_lots, 见 sizing.py 缝注释)。"""
    _in = _load_inputs(top_n=top_n, score_table=score_table, run_tag=run_tag,
                       state_stocks=state_stocks, candidate_top=candidate_top,
                       start=start, end=end, hysteresis=hysteresis,
                       anchor=anchor)
    cfg = _in["cfg"]
    state_map = _in["state_map"]
    top_n = _in["top_n"]
    top_n_fixed = _in["top_n_fixed"]
    cand_top = _in["cand_top"]
    hyst = _in["hyst"]
    cost = _in["cost"]
    fees_p = _in["fees_p"]
    slip = _in["slip"]
    run_date = _in["run_date"]
    m = _in["m"]
    dates = _in["dates"]
    scores = _in["scores"]
    industry_map = _in["industry_map"]
    # repo / regime / me_dates / ac **不解包** —— 它们已在 _load_inputs() 内部
    # 消费成 repo_s / state_s / sd / fut，这里再用就是死变量（2026-09-20 P3-1 第 1 步
    # 全量解包留下的 4 条无用赋值，pyflakes 抓到后移除）。
    sd = _in["sd"]
    nl = _in["nl"]
    is_st = _in["is_st"]
    fut = _in["fut"]
    repo_s = _in["repo_s"]
    state_s = _in["state_s"]
    wmap = _in["wmap"]

    # 建仓追高剔除闸(2026-09-26 用户拍板采纳; 三轮预注册 A/B 见 05 号台账 七补/九补)。
    # 单一实现 app/portfolio/chase.py —— 与 A/B 臂逐位同数(m 同为 load_market() 全历史)。
    # ⚠️ 只剔最终 top-n 名单、不补位(=A/B 的 chase6 语义); candidate_top>0 时剔除会
    # 同时作用于补位池 —— 该组合未测试, 生产 config stock_candidate_top=0。
    chase_cfg = cfg.get("chase") or {}
    chase_gate = None
    if chase_cfg.get("enabled"):
        from app.portfolio.chase import ChaseGate
        chase_gate = ChaseGate(m, cfg)
        log.info("chase 闸启用: theta=%.1f (2026-09-26 采纳, 05 号 九补)",
                 float(chase_cfg.get("theta", 2.0)))

    nav = 1.0
    sys_nav = 1.0
    bench_nav = 1.0
    nav_rows = []
    daily_rows = []  # R14-4/D7: 月内日频盯市(只观察, 与月频表分存)
    monthly_model = []
    monthly_bench = []
    ics = []
    # 第十轮 N10-12: 预建日期→位置字典, 原每月 dates.index() 线性查找 O(期×日)
    date_pos = {d: i for i, d in enumerate(dates)}
    prev_picks = None
    prev_picks_sys = None
    # 上月股数(满仓线/系统线各一套, P0-02 用): code -> shares, 与 prev_picks 同步滚动
    prev_sh_full = {}
    prev_sh_sys = {}
    turnover_sum = 0.0
    turnover_n = 0            # 计入换手的期数(首期无上期组合 ⇒ 不计; 2026-09-14 P2-1)
    # 结转仓位(满仓线与系统线各一套账, 2026-09-05 第五轮复查 P5-2 双口径):
    # code -> {"shares": 股数, "bp_raw": 成本, "last_adj": 最近盯市复权价,
    #          "last_raw": 最近盯市未复权价(盈亏本金基数, 2026-09-07)}
    stuck_full = {}
    stuck_sys = {}
    n_effs = []   # 每月实际买入只数(满仓线, 第五轮复查 P5-5 可见性)
    idles = []    # 每月股票预算闲置率(满仓线)

    capital = float(cfg.get("capital", 100000))
    if capital <= 0:
        raise RuntimeError("config capital 必须为正数, 请检查设置页资金配置")
    ind_cap = float(cfg.get("industry_max_weight", 0.25))
    floor_stocks = float(industry_floor if industry_floor is not None
                         else cfg.get("industry_floor_stocks", 2))
    # 第三轮复查 M4: 生效上限显式入日志(与 builder 同口径); 状态自适应时按各状态只数分别生效
    if top_n_fixed or not state_map:
        eff_cap = max(ind_cap, floor_stocks / top_n) if ind_cap < 1.0 else 1.0
        if abs(eff_cap - ind_cap) > 1e-9:
            log.info("行业上限: 配置 %.0f%% → 生效 %.1f%% (自适应放宽 max(cap, floor/top_n))",
                     ind_cap * 100, eff_cap * 100)
    else:
        caps = {st: max(ind_cap, floor_stocks / int(n)) if ind_cap < 1.0 else 1.0
                for st, n in state_map.items()}
        log.info("行业上限: 配置 %.0f%% → 状态只数下生效 %s", ind_cap * 100,
                 {k: f"{v * 100:.1f}%" for k, v in caps.items()})

    def _period_loop():
        """P3-1 第 2 步(2026-09-20): 逐期撮合主循环(原 run_backtest 内联段)。

        用**嵌套函数 + nonlocal** 而非独立函数: 循环体 197 行、几十处状态引用,
        改成 st["x"] 极易错位; 闭包让循环体**零改动**即可抽出来。

        前提(已验证): 循环内无 return/break/continue —— 否则 return 语义会从
        "退出 run_backtest" 塌成 "退出本函数"。

        nonlocal 的 9 个变量由 AST 精确算出(循环外顶层定义 + 循环内被赋值),
        不是凭感觉列; 列表类(nav_rows/daily_rows/monthly_model/monthly_bench/
        ics/n_effs/idles)走 append, 不重新绑定, 故不需 nonlocal。
        """
        nonlocal nav, sys_nav, bench_nav, prev_picks, prev_picks_sys, prev_sh_full, prev_sh_sys, turnover_sum, turnover_n

        for i in range(len(sd) - 1):
            d, d1 = sd[i], sd[i + 1]
            pos = date_pos[pd.Timestamp(d)]
            pos1 = date_pos[pd.Timestamp(d1)]

            # 状态自适应只数(2026-09-05): 与 build_plan 同规则, 按当月市场状态取只数
            st = state_s.get(str(d), "neutral")
            n = top_n if top_n_fixed else int(state_map.get(st, top_n))
            eq_w = wmap.get(st, wmap.get("neutral", 0.8))
            # 第五轮复查 P5-1(2026-09-05): 候选池 = max(只数, candidate_top)。补位机制保留但
            # 默认关闭(config=0) —— 实证在 1 万本金下补位系统性恶化: 池越深回撤越差
            # (pool8 -43.3%/pool20 -50.0% vs 不补位 -30.7%), 进攻期补位连年化都拖累;
            # 闲置现金是整手粒度+行业上限的自然风险控制, 资金做大后可重新评估
            # 迟滞带候选池(R11-5): top(n+hyst); hyst=0 时退化为 top-n 与历史口径一致
            # 2026-09-08 审查 P1: 候选池 ST/退 时点名 PIT 过滤(与 builder 同口径) ——
            # 原回测可买纸面盘永不买的 ST 股, 基线系统性偏离生产口径
            # 2026-09-09 归因修正: 过滤必须在 nlargest 之前(builder.py:91 先过滤再取 top-n)。
            # 原实现先取 top-n 再过滤 → 剔除后不足 n 只且不补位, 比生产更保守(实测少买
            # 1.2 只/月、闲置多 17pp, 年化被系统性低估)。改为先过滤后取 top-n, 与 builder 同口径。
            pool = scores[scores["trade_date"] == d]
            if st_filter:
                pool = pool[~pool["ts_code"].map(lambda c: nl.name_at(c, str(d)))
                            .str.contains("ST|退", na=False)]
            cand = pool.nlargest(max(n + hyst, cand_top), "score").copy()
            if chase_gate is not None:
                _excl = chase_gate.exclusions(
                    d, pool.set_index("ts_code")["score"],
                    theta=float(chase_cfg.get("theta", 2.0)))
                _n0 = len(cand)
                cand = cand[~cand["ts_code"].isin(_excl)]
                if len(cand) < _n0:
                    log.info("chase 剔除 %d 只追高候选(%s, 不补位)",
                             _n0 - len(cand), d)
            execs = {}
            for code in cand["ts_code"]:
                bp_adj, bp_raw, bi = _exec_side(m, dates, pos, code, "buy", is_st,
                                                name_of=nl.name_at, exec_window=exec_window)
                execs[code] = (bp_adj, bp_raw, bi)
            lot_df = pd.DataFrame({
                "score": cand["score"].values,
                "close": [execs[c][1] if execs[c][1] else np.nan for c in cand["ts_code"]],
                "industry": _industry_for(cand, industry_map),
            }, index=cand["ts_code"])
            lot_df = lot_df[lot_df["close"].notna()]
            # 迟滞带保留名单(R11-5): 满仓线/系统线各用自己的上月持仓(两套账独立)
            keep_full = _hysteresis_keep(prev_picks, lot_df, n, hyst)
            keep_sys = _hysteresis_keep(prev_picks_sys, lot_df, n, hyst)
            # 纯迟滞分配池(R11-5): 保留名单 ∪ 当月 top-n, 不向下补位 —— 首版实现直接
            # 用 top(n+hyst) 全池分配, 买不起的 top-n 股被 n+1~n+hyst 名隐式补位
            # (avg_n 3.43→4.10), 而补位经 P5-1 实证系统性恶化回撤, 属实现偏差已修正。
            # 保留名单分数置顶(allocate_lots 按传入顺序贪心, 常数加成不改内部排序)
            base_codes = set(cand.head(max(n, cand_top))["ts_code"])
            # 满仓选股线: 全额资金; 结转仓位不再重复买入, 且占用资金从本期预算中扣除
            lot_f = lot_df[lot_df.index.isin(base_codes | set(keep_full))
                           & ~lot_df.index.isin(list(stuck_full))]
            # 2026-09-21 周频化(anchor="week")暴露的边界：结转(stuck)仓位**不参与**本期选股，
            # 但它可能仍在 keep_full 中(上月持仓且仍在 lot_df)，而 lot_f 已把 stuck 排除
            # ⇒ 必须同步过滤，否则 `lot_f.loc[keep_full]` 抛 KeyError(如 603996.SH 在结转时被保留)。
            # 月频下 keep ∩ stuck 通常为空 ⇒ 对现役读数**无影响**。
            keep_full = [c for c in keep_full if c in lot_f.index]
            if keep_full:
                lot_f = lot_f.copy()
                lot_f.loc[keep_full, "score"] += 1e9
            alloc = allocate_lots(lot_f.sort_values("score", ascending=False),
                                  max(0.0, capital - sum(sv["bp_raw"] for sv in stuck_full.values())),
                                  n, ind_cap=ind_cap, cost_rate=cost, floor_stocks=floor_stocks,
                                  rank_weighted=rank_weighted)
            # 系统线(第五轮复查 P5-2): 预算=资本×权益仓位, 与 builder/模拟盘同口径整手分配
            # —— 原 sret=eq_w×mret 线性缩放允许"分数股", 系统线比实盘平滑、口径不等价
            lot_s = lot_df[lot_df.index.isin(base_codes | set(keep_sys))
                           & ~lot_df.index.isin(list(stuck_sys))]
            # 同上：系统线也要把"已在结转(stuck_sys)里"的保留股过滤掉，否则 lot_s.loc[keep_sys] KeyError。
            keep_sys = [c for c in keep_sys if c in lot_s.index]
            if keep_sys:
                lot_s = lot_s.copy()
                lot_s.loc[keep_sys, "score"] += 1e9
            alloc_sys = allocate_lots(lot_s.sort_values("score", ascending=False),
                                      max(0.0, capital * eq_w
                                          - sum(sv["bp_raw"] for sv in stuck_sys.values())),
                                      n, ind_cap=ind_cap, cost_rate=cost, floor_stocks=floor_stocks,
                                      rank_weighted=rank_weighted)
            picks = list(alloc["shares"].keys())
            picks_sys = list(alloc_sys["shares"].keys())
            n_effs.append(len(picks))
            idles.append(max(0.0, 1.0 - alloc["cost"] / capital))

            # 现金收益(按日历日)
            exec_b = dates[min(pos + 1, len(dates) - 1)]
            exec_s = dates[min(pos1 + 1, len(dates) - 1)]
            hold_days = (exec_s - exec_b).days
            if repo_s.empty:
                cash_r = FALLBACK_CASH / 100 * hold_days / 365
            else:
                seg = repo_s.loc[str(d):str(d1)]
                if seg.empty:
                    cash_r = FALLBACK_CASH / 100 * hold_days / 365
                else:
                    cash_r = seg.mean() / 100 * hold_days / 365

            # 组合收益: 结转仓位 + 本月持仓, 剩余现金计回购收益(满仓线与系统线各算各账)
            # R13-1(2026-09-06): stop_loss 非 None 时月内逐日模拟日频止损
            # R16-1(2026-09-06): 日频盯市前快照月初结转状态(_month_pnl 会 mutate stuck)
            stuck_pre_full = {c: {"shares": sv["shares"], "last_adj": sv["last_adj"],
                                  "last_raw": sv["last_raw"]}
                              for c, sv in stuck_full.items()}
            stuck_pre_sys = {c: {"shares": sv["shares"], "last_adj": sv["last_adj"],
                                 "last_raw": sv["last_raw"]}
                             for c, sv in stuck_sys.items()}
            # 结转占用资金只扣一次: _roll_stuck 返回的 invested 已含结转成本(2026-09-07
            # 第四处修复曾另加 occ 快照 → 同一笔钱在现金计息项被扣两次, 结转月逆回购利息
            # 少计; 现撤 occ, 现金项=capital-invested 单次扣除, 超资防御检查不变)
            invested, pnl, credit = _month_pnl(
                alloc, execs, prev_picks, stuck_full, m, dates, pos1, is_st, slip, cost,
                stop_loss=stop_loss, cash_r=cash_r, hold_days=hold_days, fees=fees_p,
                name_of=nl.name_at, prev_shares=prev_sh_full, exec_window=exec_window)
            invested_s, pnl_s, credit_s = _month_pnl(
                alloc_sys, execs, prev_picks_sys, stuck_sys, m, dates, pos1, is_st, slip, cost,
                stop_loss=stop_loss, cash_r=cash_r, hold_days=hold_days, fees=fees_p,
                name_of=nl.name_at, prev_shares=prev_sh_sys, exec_window=exec_window)
            if invested > capital * (1 + 1e-6):          # 防御: 任何 bug 下都不允许超资建仓
                log.error("回测 %s: 分配超资 (invested=%.0f > capital=%.0f), 本期按全现金处理", d, invested, capital)
                mret = cash_r
            else:
                mret = pnl / capital + (capital - invested) / capital * cash_r + credit / capital
            if invested_s > capital * (1 + 1e-6):
                mret_sys = cash_r
            else:
                mret_sys = (pnl_s / capital + (capital - invested_s) / capital * cash_r
                            + credit_s / capital)

            # 基准(同一执行日: 评分日后第一个交易日)。2026-09-20 B1: 报告腿改走**全收益**基准
            # (`bench_tr`) —— 持仓腿是 `close×adj_factor`(含税前分红再投资), 用价格指数比超额会
            # 凭空多出一个基准股息率(实测 2.22pp/年)。因子腿 `m["bench"]` 保持价格指数不动。
            bench0 = m["bench_tr"].iloc[min(pos + 1, len(m["bench_tr"]) - 1)]
            bench1 = m["bench_tr"].iloc[min(pos1 + 1, len(m["bench_tr"]) - 1)]
            bret = bench1 / bench0 - 1

            # R14-4/D7(2026-09-06 一致性核查): 月内日频盯市 —— 月频回撤低估真实回撤,
            # 日频序列记录月内坑底。口径: 持仓总收益路径(月内现金计息/止损事件不细化,
            # 月末点用精确月频值续接), 只观察不参与任何交易决策。
            # R16-1(2026-09-06): 补结转仓位盯市(与 _roll_stuck 同基数=月初 last_adj)+
            # 停牌日沿用最近复权收盘(与模拟盘 update_nav/_mtm_adj 同口径, 不制造幻影坑)
            b_idx = min(pos + 1, len(dates) - 1)
            last_px = {}
            for c in list(alloc["shares"]) + list(alloc_sys["shares"]):
                last_px.setdefault(c, execs[c][0])
            for c, sv in stuck_pre_full.items():
                last_px.setdefault(c, sv["last_adj"])
            for c, sv in stuck_pre_sys.items():
                last_px.setdefault(c, sv["last_adj"])

            def _px(c, dd):
                px = m["close"].loc[dd, c] * m["adj"].loc[dd, c]
                if pd.isna(px):
                    return last_px.get(c)  # 停牌: 沿用最近复权收盘
                last_px[c] = px
                return px

            for t in range(b_idx, pos1):
                # 2026-09-28 预注册修正(plans/metric-fix-batch-prereg-20260928.md §二):
                # 右端原为 s_idx(=pos1+1) ⇒ 锚点日 d1 在内存日频序列出现两行(先 :798 近似行
                # 未扣当月费用, 后 :808 精确行), mdd_daily 吃内存而落库表塌缩同键行 ⇒
                # 指标与 backtest_nav_daily 不可互审、回撤被"虚高峰值-扣费回落"人为放大。
                # 锚点日只保留精确行 ⇒ 内存=落库逐行同构; 原来当 pos1 为面板末日时
                # s_idx=min(pos1+1,len-1)=pos1 已天然排除, 本改动对两窗 month 锚仅去重。
                dd = dates[t]
                dv = 0.0
                dvs = 0.0
                # 总收益口径与 _month_return 一致: 原始投入×复权比(sh×bp_raw×(px/bp_adj-1)),
                # 不能用 股数×复权价差 —— 老股 adj 因子达几十倍, 价差会被放大到数十倍资本
                for c, sh in alloc["shares"].items():
                    px = _px(c, dd)
                    if px and execs[c][0]:
                        dv += sh * execs[c][1] * (px / execs[c][0] - 1)
                for c, sh in alloc_sys["shares"].items():
                    px = _px(c, dd)
                    if px and execs[c][0]:
                        dvs += sh * execs[c][1] * (px / execs[c][0] - 1)
                # 结转仓位: 月初未复权市值×复权环比(2026-09-07 与 _roll_stuck/正常持仓同口径;
                # 原"股数×(复权px-复权last_adj)"在 adj!=1 时同样被放大; 停牌 px 沿用 last_adj→0)
                for c, sv in stuck_pre_full.items():
                    px = _px(c, dd)
                    if px is not None and sv["last_adj"] is not None:
                        dv += sv["shares"] * sv["last_raw"] * (px / sv["last_adj"] - 1)
                for c, sv in stuck_pre_sys.items():
                    px = _px(c, dd)
                    if px is not None and sv["last_adj"] is not None:
                        dvs += sv["shares"] * sv["last_raw"] * (px / sv["last_adj"] - 1)
                daily_rows.append((run_date, dd.strftime("%Y%m%d"),
                                   nav * (1 + dv / capital),
                                   sys_nav * (1 + dvs / capital),
                                   bench_nav * (m["bench_tr"].loc[dd] / m["bench_tr"].iloc[b_idx])))
            # 系统净值(第五轮复查 P5-2): 直接按权益仓位预算整手分配的月度收益复利,
            # 与模拟盘同口径; 满仓线 nav 保持全额资金(纯只数择时参考线)
            nav *= (1 + mret)
            sys_nav *= (1 + mret_sys)
            bench_nav *= (1 + bret)
            nav_rows.append((run_date, d1, nav, sys_nav, bench_nav))
            daily_rows.append((run_date, d1, nav, sys_nav, bench_nav))  # 月末精确点续接
            monthly_model.append(mret)
            monthly_bench.append(bret)

            # 第十轮 N10-19 口径注: 此为"月度新进比例"(1-重叠数/当期数), 非双边换手率;
            # 首期无上期组合不计(少计一次建仓), 系统线不单独统计(见长期项 L6)
            # 2026-09-14 修(外部审查 P2-1): 原来 sum 只累加 n_periods-1 次、分母却写 n_periods
            # ⇒ **系统性低估 1/n_periods**(115 期 ≈ 0.9%, 30 期 ≈ 3.4%)。现在分开记"计入期数"。
            if prev_picks is not None:
                turnover_sum += 1 - len(set(prev_picks) & set(picks)) / max(1, len(picks))
                turnover_n += 1
            prev_picks = picks
            prev_picks_sys = picks_sys
            prev_sh_full = dict(alloc["shares"])
            prev_sh_sys = dict(alloc_sys["shares"])

            # 当期 IC
            f = fut.loc[pd.Timestamp(d)]
            sc = scores[scores["trade_date"] == d].set_index("ts_code")["score"]
            j = pd.concat([sc, f.rename("ret")], axis=1).dropna()
            if len(j) > 50:
                ics.append(j["score"].corr(j["ret"], method="spearman"))

    _period_loop()

    # ---------- 指标 ----------
    def _metrics():
        """P3-1 第 3 步(2026-09-20): 指标汇总(原 run_backtest 内联段)。

        与第 2 步同一手法(嵌套函数 + nonlocal): 段内代码零改动。
        nonlocal 只需 monthly_model / monthly_bench —— 它们被
        pd.Series(...) 重新赋值; 其余都是段内新定义。

        写库与日志**不在这里** —— 它们不是指标计算, 留在 run_backtest。
        """
        nonlocal monthly_model, monthly_bench

        monthly_model = pd.Series(monthly_model)
        monthly_bench = pd.Series(monthly_bench)
        n_periods = len(monthly_model)
        # 2026-09-21 周频化：年化频率**自适应** —— 旧口径写死 12(月频)。
        # 判据 = 相邻评分日的「间隔交易日数」中位数：月频 ≈21 ⇒ 12(与旧口径**逐位一致**，不动基线)；
        # 周频 ≈5 ⇒ 52。否则周频回测会被错误折算(持有期只有 1 周却按 12 期/年 年化)。
        _pos = {d: i for i, d in enumerate(dates)}
        # ⚠️ sd 是 'YYYYMMDD' 字符串、dates 是 DatetimeIndex ⇒ **必须转 Timestamp 才能查到位置**，
        # 否则 `d in _pos` 恒 False ⇒ _ix 为空 ⇒ gap 退化成默认 21 ⇒ 周频永远走月频口径(12)。
        _sd = pd.to_datetime(pd.Series(list(sd)))
        _ix = [_pos[d] for d in _sd if d in _pos]
        _gap = int(np.median(np.diff(_ix))) if len(_ix) > 1 else 21
        # 2026-09-27 双月锚(创新③ 持有期 A/B): 间隔 >35 交易日 ⇒ 半年频(~6 期/年)。
        # reporting 层修正 —— month(21~31 日)仍 12、week(≤15)仍 52, 既有产物不受影响。
        ppy = 52 if _gap <= 15 else (6 if _gap > 35 else 12)
        ann = (1 + monthly_model).prod() ** (ppy / n_periods) - 1
        bench_ann = (1 + monthly_bench).prod() ** (ppy / n_periods) - 1
        vol_ann = monthly_model.std() * np.sqrt(ppy)
        # 第十轮 N10-18: 无风险利率常量集中(现金端用回购实际利率, 夏普分母用固定 2% 假设)
        RF_ANNUAL = 0.02
        sharpe = (ann - RF_ANNUAL) / vol_ann if vol_ann > 0 else np.nan
        nav_s = pd.Series([r[2] for r in nav_rows])
        max_dd = float((nav_s / nav_s.cummax() - 1).min())
        win_rate = float((monthly_model > monthly_bench).mean())
        ic_arr = np.array(ics)
        metrics = {
            "run_date": run_date,
            "start": str(sd[0]), "end": str(sd[-1]), "periods": n_periods,
            "top_n": top_n, "cost": cost,
            "state_stocks": state_map if (not top_n_fixed and state_map) else None,
            # 第五轮复查 P5-3/P5-5(2026-09-05): 目标/实际只数、预算闲置、各状态生效行业上限
            "stocks_target": (state_map if (not top_n_fixed and state_map) else top_n),
            "avg_stocks": round(float(np.mean(n_effs)), 2),
            "avg_cash_idle": round(float(np.mean(idles)), 4),
            "eff_cap_by_state": ({st: round(max(ind_cap, floor_stocks / int(n)) if ind_cap < 1.0 else 1.0, 4)
                                  for st, n in state_map.items()} if state_map else None),
            "candidate_top": cand_top,
            "stop_loss": stop_loss,
            "hysteresis": hyst,
            # 2026-09-21 周频化：落盘年化频率，便于核对"这次回测按多少期/年折算"（月频 12 / 周频 52）
            "periods_per_year": ppy,
            # 2026-09-06 第十三轮 R13-3: 参数指纹 —— 任何 run 都能复现当时参数
            # (月度自动回测无 tag 用当时 config, 实验/配置漂移不再无法追溯)
            "cfg_fingerprint": {
                "industry_floor_stocks": floor_stocks, "state_stocks": state_map,
                "candidate_top": cand_top, "cost": cost, "slip": slip,
                "ind_cap": ind_cap,
                "size_pctl": cfg.get("stock_size_pctl", 0),
                "min_amt20": cfg.get("stock_min_amt20", 0),
                "hysteresis": hyst,
                "st_filter": st_filter,
            },
            "capital": capital, "lot_modeled": True,
            "ann_return": round(float(ann), 4),
            "bench_ann": round(float(bench_ann), 4),
            "excess_ann": round(float(ann - bench_ann), 4),
            "max_drawdown": round(max_dd, 4),
            "vol_ann": round(float(vol_ann), 4),
            "sharpe": round(float(sharpe), 3) if not np.isnan(sharpe) else None,
            "win_rate": round(win_rate, 4),
            # 分母 = **真正计入的期数**(2026-09-14 P2-1)；退化到单期回测时给 None(不是伪造 0)
            "avg_turnover": round(turnover_sum / turnover_n, 4) if turnover_n else None,
            "ic_mean": round(float(ic_arr.mean()), 4) if len(ic_arr) else None,
            "ic_std": round(float(ic_arr.std()), 4) if len(ic_arr) else None,
            "icir": round(float(ic_arr.mean() / ic_arr.std()), 3) if len(ic_arr) else None,
        }
        # 系统净值年化与回撤(由净值序列直接计算)
        sys_nav_s = pd.Series([r[3] for r in nav_rows])
        # 2026-09-28 预注册修正(plans/metric-fix-batch-prereg-20260928.md §一): 年化频率
        # 与满仓线同源自适应 ppy —— 原硬编码 12, 非月频锚(week/双月)的系统线年化会算错;
        # 现役 month 锚 ppy 恒 = 12 ⇒ 逐位不变(判据: 重跑 sys_ann_return 逐位相等)
        metrics["sys_ann_return"] = round(float(sys_nav ** (ppy / n_periods) - 1), 4)
        metrics["sys_max_drawdown"] = round(float((sys_nav_s / sys_nav_s.cummax() - 1).min()), 4)
        # R14-4/D7(2026-09-06): 日频盯市回撤 —— 实盘口径的诚实回撤(月频值低估月内坑底)
        daily_nav_s = pd.Series([r[2] for r in daily_rows])
        metrics["mdd_daily"] = round(float((daily_nav_s / daily_nav_s.cummax() - 1).min()), 4)
        daily_sys_s = pd.Series([r[3] for r in daily_rows])
        metrics["sys_mdd_daily"] = round(float((daily_sys_s / daily_sys_s.cummax() - 1).min()), 4)

        # 顺带返回 log 要用的三个标量 —— 它们在本函数内定义,
        # 留在 run_backtest 的收尾日志(年化/基准/超额/回撤)还要用。
        return metrics, ann, bench_ann, max_dd

    metrics, ann, bench_ann, max_dd = _metrics()

    with db.conn_ctx() as conn:
        # conn_ctx 已绑定连接(勿再 get_conn: 新开的那个无人关)
        df_nav = pd.DataFrame(nav_rows, columns=["run_date", "trade_date", "nav", "sys_nav", "bench_nav"])
        db.upsert_df(conn, df_nav, "backtest_nav")
        # R14-4/D7: 日频净值分表落库(与月频表并存, 历史对比不破坏)
        df_daily = pd.DataFrame(daily_rows,
                                columns=["run_date", "trade_date", "nav", "sys_nav", "bench_nav"])
        db.upsert_df(conn, df_daily, "backtest_nav_daily")
        conn.execute("INSERT OR REPLACE INTO backtest_summary (run_date, metrics) VALUES (?,?)",
                     (run_date, json.dumps(metrics, ensure_ascii=False)))
        conn.commit()
    log.info("回测完成: 年化 %.2f%% / 基准 %.2f%% / 超额 %.2f%% / 回撤 %.2f%%",
             ann * 100, bench_ann * 100, (ann - bench_ann) * 100, max_dd * 100)
    return metrics
