"""风格读数(2026-09-10): 池子(等权全A) vs 中证800 —— 看板「风格」块的数据源

**定位: 描述性指标, 不是可交易信号。** 建立它的直接原因(2026-09-10 A/B):
  1. 持仓是小盘, 而生产 regime 的均线/回撤/波动全算在中证800(大中盘)上。2023-11-20
     →2024-02-07(系统线历史谷底)池子跌 30.4%、中证800 只跌 7.4% —— 生产的防守信号
     对真实风险是瞎的。看板需要把「正在发生什么」直接摆出来。
  2. 但把它做成**预警**被证据否掉: 60日相对 ≤ -15% 这条规则 9.5 年只触发 9 段(约每年
     1 次), 触发后系统线的后续走向是 -6.8%/-0.4%/+8.4%/-0.4%/-5.3%/+1.0% —— 没有
     预测力(见 memory: 0910 晚风格防守 A/B)。风格信号是**事后描述**, 不构成动作依据。
  3. 同一 A/B 还否掉了「regime 看错指数」这一个假设: 把整条 regime 换成池子分支
     (100% replace), 系统回撤只从 -51.81% 动到 -51.63%(0.18pp)。

所以本模块只负责**如实读数**, 不产生信号、不写库、不触发任何动作。别在它上面加
"if rel60 < x: 减仓" 之类的分支 —— 那正是被证伪的那条路。

口径: 相对强弱 = (池子累计收益)/(中证800 累计收益) - 1, 两序列先按共同交易日对齐
(任一缺当日数据则整日剔除, 防错位比较)。池子状态复用 regime._pool_state_frame
(同一套规则、同一份代码, 避免看板与防守逻辑两处漂移)。
"""
import logging

import pandas as pd

from app import config as cfg_mod
from app import db
from app.models import pool_index, regime

log = logging.getLogger(__name__)

# 相对强弱的展示/判读窗口(交易日)
WINDOWS = (20, 60, 250)
# 单边判读阈值: |60日相对| 超过该值才在文字里说「顺风/逆风」, 否则一律说中性
# (取 5% 是描述性取舍, 不是优化出来的阈值 —— 别据此调整)
_BIAS_BAND = 0.05


def _rel_strength(pc: pd.Series, bc: pd.Series, n: int):
    """近 n 日相对强弱(累计口径)。不足 n+1 个对齐交易日返回 None。"""
    if len(pc) <= n or len(bc) <= n:
        return None
    base_p, base_b = pc.iloc[-1 - n], bc.iloc[-1 - n]
    if not base_p or not base_b:
        return None
    return float((pc.iloc[-1] / base_p) / (bc.iloc[-1] / base_b) - 1)


def style_frame(pc: pd.Series, bc: pd.Series, lookback: int = 250) -> dict:
    """纯函数: 池子收盘 pc + 基准收盘 bc → 风格读数 dict(无 IO, 便于单测)。"""
    both = pd.DataFrame({"pool": pc, "bench": bc}).dropna()
    if len(both) < 21:
        return {"available": False, "reason": "对齐后的共同交易日不足 21 天"}
    p, b = both["pool"], both["bench"]
    out = {"available": True, "as_of": str(both.index[-1]), "n_days": len(both),
           "pool_close": round(float(p.iloc[-1]), 2),
           "bench_close": round(float(b.iloc[-1]), 2)}
    for n in WINDOWS:
        r = _rel_strength(p, b, n)
        out[f"rel_{n}"] = round(r, 4) if r is not None else None
    # 池子分支状态(与 regime 风格防守同一份代码, score_max=6 无估值层)
    pf = regime._pool_state_frame(p)
    if not pf.empty:
        last = pf.iloc[-1]
        out["pool_state"] = str(last["pool_state"])
        out["pool_score"] = float(last["pool_score"])
        out["pool_drawdown"] = (round(float(last["pool_drawdown"]), 4)
                                if pd.notna(last["pool_drawdown"]) else None)
        out["pool_vol"] = (round(float(last["pool_vol"]), 4)
                           if pd.notna(last["pool_vol"]) else None)
    else:
        out.update({"pool_state": None, "pool_score": None,
                    "pool_drawdown": None, "pool_vol": None})
    # 相对强弱曲线: 以窗口起点归一, >1 表示池子跑赢(前端画线用)
    tail = both.tail(lookback)
    if len(tail) >= 2 and tail["pool"].iloc[0] and tail["bench"].iloc[0]:
        ratio = (tail["pool"] / tail["pool"].iloc[0]) / (tail["bench"] / tail["bench"].iloc[0])
        out["series"] = [{"d": str(d), "v": round(float(v), 4)}
                         for d, v in ratio.items()]
    else:
        out["series"] = []
    return out


def _bias(rel60) -> str:
    if rel60 is None:
        return "未知"
    if rel60 > _BIAS_BAND:
        return "顺风"
    if rel60 < -_BIAS_BAND:
        return "逆风"
    return "中性"


def load_style(lookback: int = 250) -> dict:
    """读库 → 风格读数(供 /api/style 与月度方案邮件)。表缺失时 available=False。"""
    conn = db.get_conn()
    try:
        pc = pool_index.load_pool_close(conn)
        bench = cfg_mod.benchmark_code()
        if db.table_exists(conn, "index_daily"):
            bc = pd.read_sql(
                "SELECT trade_date, close FROM index_daily WHERE ts_code=? ORDER BY trade_date",
                conn, params=(bench,))
            bc = (bc.set_index("trade_date")["close"].astype(float)
                  if not bc.empty else pd.Series(dtype=float))
        else:
            bc = pd.Series(dtype=float)
        # 生产线正在用的基准状态(生产口径含估值层 → score_max=7, 与池子分支的 6 分制
        # **不可直接比分数**, 只比对状态档位), 供前端显示「谁更防守」
        row = None
        if db.table_exists(conn, "regime"):
            r = pd.read_sql("SELECT trade_date, state, score FROM regime "
                            "ORDER BY trade_date DESC LIMIT 1", conn)
            if not r.empty:
                row = r.iloc[0]
    finally:
        conn.close()
    if pc.empty or bc.empty:
        return {"available": False,
                "reason": "池子指数或基准指数为空(池子需先跑 build_pool_index)"}
    out = style_frame(pc, bc, lookback)
    if not out.get("available"):
        return out
    out["bench_code"] = bench
    out["bench_state"] = str(row["state"]) if row is not None else None
    out["bench_state_date"] = str(row["trade_date"]) if row is not None else None
    out["bias"] = _bias(out.get("rel_60"))
    # 风格分歧: 池子分支与生产线基准状态档位不一致 = 生产防守信号此刻是「瞎的」
    out["diverged"] = (out["pool_state"] is not None and out["bench_state"] is not None
                       and out["pool_state"] != out["bench_state"])
    log.info("风格读数: %s 池子 %s / 基准 %s, 60日相对 %s",
             out["as_of"], out["pool_state"], out["bench_state"],
             f"{out['rel_60']:+.2%}" if out.get("rel_60") is not None else "n/a")
    return out
