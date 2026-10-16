"""A 股交易费用逐笔模型(2026-09-10)

背景: 回测与模拟盘原用扁平 `single_side_cost`(0.3%)折算成本。用户核实券商实际为
「佣金有单笔最低 5 元」后, 扁平费率在 2 万 / 4~6 只口径下明显高估 —— 每笔约 3000 元
时 5 元 = 单边 0.167%, 而**最低佣金对小仓位的惩罚是非线性的**(1500 元一笔 = 0.33%),
本策略长期是小仓位组合, 扁平费率会系统性美化结果。故改为逐笔真实费率。

费率(可在 config `fees` 段按券商实际修改):
- 佣金: max(成交额 × 佣金率, 单笔最低佣金)
- 印花税: 仅卖出, 2023-08-28 起 0.05%, 此前 0.1%(当日生效, 按成交日判定)
- 过户费: 双向 0.001%(2022-04-29 起沪深统一; 此前 0.002%, 差异在 1e-5 量级, 不另建模)

口径说明: 本模块只负责「真实费用」。config 的 `single_side_cost` 保留作**预算预留**
(整手分配时的保守估计), 与实盘下单前预留资金同义, 不再代表成本假设。
"""
import datetime

# 默认费率(用户券商口径; config 缺失/非法时回退, fail-safe 不静默改变成本口径)
_DEFAULTS = {
    "commission_rate": 0.00025,   # 佣金费率 万2.5
    "min_commission": 5.0,        # 单笔最低佣金(元)
    "stamp_tax": 0.0005,          # 印花税(卖出) 2023-08-28 起
    "stamp_tax_before": 0.001,    # 印花税(卖出) 2023-08-28 之前
    "transfer_fee": 0.00001,      # 过户费(双向)
}
_STAMP_CHANGE = "20230828"        # 印花税减半生效日

_NUM_KEYS = tuple(_DEFAULTS)


def _as_date_str(date) -> str:
    """统一成 YYYYMMDD 字符串; None/非法返回空串(走当前税率)"""
    if date is None:
        return ""
    if isinstance(date, str):
        return date.replace("-", "")[:8]
    if isinstance(date, (datetime.date, datetime.datetime)):
        return date.strftime("%Y%m%d")
    try:  # pandas.Timestamp 等
        return date.strftime("%Y%m%d")
    except Exception:
        return ""


def fee_params(cfg: dict = None) -> dict:
    """取费率参数: config `fees` 段优先, 缺键/非数值回退默认。"""
    p = dict(_DEFAULTS)
    if cfg:
        raw = cfg.get("fees") or {}
        if isinstance(raw, dict):
            for k in _NUM_KEYS:
                v = raw.get(k)
                try:
                    if v is not None and float(v) >= 0:
                        p[k] = float(v)
                except (TypeError, ValueError):
                    pass
    return p


def stamp_tax_rate(date, params: dict = None) -> float:
    """印花税率(卖出单边): 按成交日判定, 2023-08-28 起减半"""
    p = params or _DEFAULTS
    d = _as_date_str(date)
    if d and d < _STAMP_CHANGE:
        return float(p.get("stamp_tax_before", _DEFAULTS["stamp_tax_before"]))
    return float(p.get("stamp_tax", _DEFAULTS["stamp_tax"]))


def trade_cost(amount: float, side: str, date=None, params: dict = None) -> float:
    """单笔交易费用(元) = 佣金 + 印花税(卖出) + 过户费

    amount: 成交额(元, 未扣费); side: 'buy' | 'sell'; date: 成交日(定印花税率)
    """
    p = params or _DEFAULTS
    amt = float(amount or 0)
    if amt <= 0:
        return 0.0
    comm = max(amt * float(p.get("commission_rate", _DEFAULTS["commission_rate"])),
               float(p.get("min_commission", _DEFAULTS["min_commission"])))
    stamp = amt * stamp_tax_rate(date, p) if side == "sell" else 0.0
    transfer = amt * float(p.get("transfer_fee", _DEFAULTS["transfer_fee"]))
    return comm + stamp + transfer


def effective_rate(amount: float, side: str, date=None, params: dict = None) -> float:
    """单边实际费率 = 费用 / 成交额(供回测按比率口径复用 _month_return 结构)。

    小仓位被最低佣金放大是本模型的关键非线性: 3000 元买 = 0.167%, 1500 元买 = 0.334%。
    amount<=0 视为无成交, 返回 0(不产生费用)。
    """
    amt = float(amount or 0)
    if amt <= 0:
        return 0.0
    return trade_cost(amt, side, date, params) / amt
