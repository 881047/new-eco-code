"""板块交易规则(2026-09-07 全市场扩展): 涨跌停限幅、上市初期无涨跌幅窗口、整手规则
集中一处 —— paper 执行引擎/回测引擎/builder/monitor/daily_score/sizing 全链同一口径。

规则依据(A股交易规则), **限幅档位按下列优先级**:
1. **S股**(名称以 S 开头且非 ST, 未完成股权分置改革): ±5%
2. **创业板注册制改革前**(2020-08-24 前, `_CHINEXT_REFORM`): ±10%(ST ±5%)
3. **风险警示**(ST/*ST): 主板 ±5%(**2026-07-06 起改为 ±10%** —— 见 `_MAIN_ST_REFORM`);
   创业板/科创板 ±20%、北交所 ±30%(与普通档一致, 不另收窄)
4. 普通档: 沪深主板 ±10%, 创业板/科创板 ±20%, 北交所 ±30%

⚠️ **退市整理期(名称含"退")没有独立档位** —— 它走的就是板块常态档。2026-09-12 D06/D07
验收先误加了"退 ⇒ 一律 ±10%", 当日实测后被推翻(见下), 故此处**不设该分支**。

⚠️ **限幅规则改过两次, 且部分档位与"名称"而非仅"代码"有关, 故 `limit_ratio` 接受
`dstr`(日期)、`name`(时点名称)与 `name_of`(历史名称查询回调)** —— 2026-09-12 D06/D07 验收:
- **2020-08-24 创业板注册制改革**: 创业板由 ±10%(ST ±5%) 改为 ±20%(`_CHINEXT_REFORM`)
- **2026-07-06 主板风险警示股**: 由 ±5% 放宽至 ±10%(`_MAIN_ST_REFORM`)。交易所
  `stk_limit` 实测: 最后一批 5% 档落在 20260703(周五), 20260706(周一)起 142~173 只
  全量同步切到 10%, **无过渡日、无残留**; 创业板/科创板 ST 全程 20% 未变。
- **S股**的档位由**当日名称**决定, 不是代码前缀 —— 不传 `name` 时拿不到
- **创业板退市整理期的新老划断**(交易所规则, `stk_limit` 逐格实测): 改革**后**进入
  整理期的创业板股按新的 ±20%(2021+ 实测 28 只无一例外); 改革**前**已进入整理期的
  **存量股沿用 ±10% 直至整理期结束**(`300216.SZ` 千山退: 整理期 20200805~20200915
  跨越改革日, 全程 lu/pc=1.1000)。判定"是否存量"需要**回查改革日当天的名称** ⇒
  这就是 `name_of` 存在的唯一理由; 不传则退化为"一律按改革后的 ±20%"

⚠️ 不传 `dstr` = 按**现行**规则。实盘链路(paper/builder/monitor/daily_score 的日更)
只看"今天", 故不传等价; **回测/补建历史必须传**, 否则就是把今天的规则套到历史上。

- 上市初期不设涨跌幅限制: 主板(注册制)/创业板/科创板 上市前 5 个交易日, 北交所仅首日
- 整手: 主板/创业板 100 股整数倍; 科创板 最低 200 股后 1 股递增; 北交所 最低 100 股后
  1 股递增
- 已知近似(与旧口径一致, 注释披露): 长期停牌复牌首日/退市整理期首日的无涨跌幅限制
  未建模 —— 这类日子的限幅按普通档执行, 订单顺延至恢复限幅后成交(保守方向, 不虚高)
- 已知近似: **689xxx(科创板 CDR)已按 star 处理**; 北交所取整规则与沪深不同(实测上轨
  floor、下轨 ceil, 非半进位) —— **2026-10-05 已接线**(`limit_price(board="bse")`,
  回测/模拟盘撮合 + builder/monitor/daily_score 涨停剔除全链同一口径), 逐格验证见
  `scripts/oneoff/diag_stklimit_window_check.py`(2026 年 98,750 个 BSE 格与交易所
  `stk_limit` 公布值位级零失配; 另见 BSE 无限制日哨兵变体 up=99999.99/dn=0.0);
  对策略零影响(`model_score`/`daily_score` 里没有任何北交所股票, 2026-10-05 实查)
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

# 普通股涨跌幅(占前收盘比例) —— **现行档**
_LIMIT = {"main": 0.10, "chinext": 0.20, "star": 0.20, "bse": 0.30, "other": 0.10}
# 风险警示(ST/*ST)涨跌幅: 仅主板曾收窄到 5%(**2026-07-06 前的历史档**), 创业/科创/北交
# 维持普通档(与 _LIMIT 同值, 保留以便逐板取用, 不写分支)
_LIMIT_ST = {"main": 0.05, "chinext": 0.20, "star": 0.20, "bse": 0.30, "other": 0.05}
# S股(未完成股权分置改革)档位 —— 由**当日名称**决定, 与板块无关。
# ⚠️ 此处**没有**"退市整理期档": 整理期股票走板块常态档, 见模块 docstring
_DELIST_LEGACY_CHINEXT = 0.10   # 创业板退市整理期新老划断的**存量股**档(改革前的 10%)
_S_SHARE_RATIO = 0.05
# 上市初期不设涨跌幅限制的交易日数(含上市首日)
_NO_LIMIT_DAYS = {"main": 5, "chinext": 5, "star": 5, "bse": 1, "other": 0}
# 整手规则: ("multiple", N)=N 股整数倍; ("min", N)=最低 N 股、其后 1 股递增
_LOT = {"main": ("multiple", 100), "chinext": ("multiple", 100),
        "star": ("min", 200), "bse": ("min", 100), "other": ("multiple", 100)}


def board_of(code: str) -> str:
    """ts_code → 板块: main(沪深主板)/chinext(创业板)/star(科创板)/bse(北交所)/other
    (调用方索引可能为整型代码, 统一转字符串判定)"""
    code = str(code)
    if code.startswith(("688", "689")):
        # 689xxx = **科创板存托凭证(CDR)**, 与 688xxx 同规则(限幅 20%、上市前 5 交易日
        # 无限制、整手最低 200 股)。2026-09-12 D06/D07 验收补: 原只认 688 ⇒ 689009.SH
        # (九号公司-WD) 落到 "other"(10%、0 个无限制日), 而交易所 `stk_limit` 实测它
        # 前 5 日无限制、第 6 日起 lu/pc=1.2000。该股**在 model_score 里**(其余 688/北交所
        # 均不在) ⇒ 是这轮唯一真正影响回测的板块归类错误。
        return "star"
    if code.startswith("30"):
        return "chinext"
    if code.startswith(("4", "8")) or code.startswith("920"):
        # 2026-09-28 预注册修正(plans/metric-fix-batch-prereg-20260928.md §三): 原
        # ("4","8","9") 把**沪市 B 股 900xxx** 也归 bse(±30%/最低 100 股后 1 股递增)——
        # B 股实际 ±10%/100 股整数倍, 应落 "other"(与深 B 200xxx 同档)。920(北交所新段)
        # 判定前置, 920xxx 行为逐位不变。实测 model_score/daily_score 无 9 开头行 ⇒
        # 对现役回测/选股 Δ=0, 纯潜伏修正。
        return "bse"
    if code.startswith(("60", "000", "001", "002", "003")):
        return "main"
    return "other"


def is_star(code: str) -> bool:
    """是否**科创板**（含 689xxx 存托凭证 CDR）。

    2026-09-13 加：`star_enabled=false` 时，"选股/回测/推分/池子指数"四处都要排除科创板，
    而它们用的是**字面 `688` 前缀** —— `board_of` 早已把 689 归为 star（D06/D07），于是
    **689009.SH 被放进了选股域**（库里就这一只：daily 1427 行 / model_score 68 行 /
    daily_score 30 行 / 池子指数含它），而 2026-09-08 用户拍板的是"训练全市场、**选股排除
    科创板**"（起因就是科创占位在 1 万本金下拖累 −87% 累计）。⇒ 判据只有一处：`board_of`。
    """
    return board_of(str(code)) == "star"


# ---------------------------------------------------------------------------
# 限幅规则的**生效日**历史 —— `board_of()` 回答不了这件事
# ---------------------------------------------------------------------------
# ⚠️ 2026-09-12 D06/D07 验收发现: `limit_ratio` 原先**没有日期参数**, 于是把**今天的**
# 板块规则套到全部历史上。唯一的受害板块是创业板 ——
#
#   创业板(300xxx/301xxx) **2020-08-24 注册制改革之前是 ±10%(ST ±5%)**,
#   改革后才 ±20%(ST 并入 20%)。`board_of` 只按代码前缀判板块, 对 2017 年的
#   300xxx 返回 "chinext" ⇒ 限幅 20%, 而交易所当年是 10%。
#
# 后果方向与「旧基线买生产买不到的 ST/退市股」完全同类, 但**相反**: 引擎的涨停价
# 被算高了一倍幅度 ⇒ 一只当天已经封死 +10% 涨停(生产根本买不到)的创业板股票,
# 在回测里 `close(19.07) >= limit_up(20.81) - 0.001` 不成立 ⇒ **照样成交**。
# 用交易所公布的 `stk_limit` 对照实测: 2017/2018/2019 年创业板 **99.9~100% 是
# 10% 档、0% 是 20% 档**, 而同期引擎一律按 20% 判。
#
# 修法是**给限幅加日期**, 而不是改 `board_of`: 板块归属和「那天适用哪一档」是两件事,
# 混在一个函数里正是这个 bug 的成因。`dstr=None` 保持现行规则 ⇒ 模拟盘/看板/下单
# 这些只看「今天」的路径**逐位不变**(今天 ≥ 2020-08-24, 历史规则与现行规则同值)。
_CHINEXT_REFORM = "20200824"   # 创业板注册制改革生效日(涨跌幅 10%→20%)
# 主板风险警示股(ST/*ST)涨跌幅 5%→10% 生效日。2026-09-12 D06/D07 验收实测交易所
# `stk_limit` 定档: 5% 档最后一天 20260703, 10% 档首日 20260706, 无过渡、无残留
# (原代码把"主板 ST 恒为 5%"写死 ⇒ 20260706 起 142~173 只/日 判错, 共 7,142 格)。
_MAIN_ST_REFORM = "20260706"


def limit_ratio(code: str, is_st: bool = False, dstr: str = None,
                name: str = None, name_of=None) -> float:
    """该股**在 dstr 当日**的涨跌幅幅度(占前收盘比例)。判定优先级见模块 docstring。

    - `dstr=None` ⇒ 按**现行**规则。实盘链路(paper/builder/monitor/daily_score)只看
      "今天", 传不传等价, 故保持可选以免扰动生产。
    - `dstr="YYYYMMDD"` ⇒ 按**当日历史规则**。**回测/补建历史必须传** —— 否则就是把
      今天的规则套到历史上(2026-09-12 前正是如此, 创业板虚高一倍)。
    - `name` = **当日时点名称**(PIT)。`"S"` 前缀且非 ST ⇒ S股 ±5%。**没有"退"档** ——
      退市整理期股票走板块常态档。
    - `name_of` = `(code, dstr) -> str` 的历史名称回调(通常直接传 `NameLookup.name_at`)。
      **唯一用途**是创业板退市整理期的新老划断: 回查改革日当天该股名称是否已含"退"
      (即改革前已进入整理期的存量股 ⇒ 沿用 10%)。只在 `name` 含"退" 且板块为创业板
      且 `dstr >= 改革日` 这一窄分支里被调用, 不对普通格子增加查询开销。

    is_st 由评分/执行日的时点名称判定(PIT), 与 trainer/paper/回测同口径。
    """
    b = board_of(code)
    nm = str(name) if name else ""
    # 2026-09-15（外部审查 P2-5）：`_norm_date` 不可解析时返回空串 ⇒ 空串 < 改革日 ⇒
    # **静默降级**到"历史保守档"。方向是安全的（限幅收窄），但调用方传错日期格式时
    # 不会有任何信号。这里就地归一化一次并留痕：每次调用最多一条 warning（不逐股刷屏，
    # 因为回测/补建是逐格调用的），同时把下面两处重复的 `_norm_date` 收敛到这一次。
    dd_parsed = None
    if dstr is not None:
        dd_parsed = _norm_date(dstr)
        if not dd_parsed:
            # ⚠️ 消息里的百分号必须写成 `%%` —— logging 会拿 msg 做 %-格式化，
            # 单个 `%` 会抛 "not enough arguments for format string"（test_board 抓到）
            log.warning("limit_ratio: 日期 %r 不可解析 ⇒ 按历史保守档(±10%%/±5%%)处理", dstr)
    # (1) S股: 未完成股权分置改革。全市场仅存 600182.SH(S佳通), 交易所实测 ±5%(1789 格)
    if nm[:1] == "S" and "ST" not in nm:
        return _S_SHARE_RATIO
    if b == "chinext" and dstr is not None:
        dd = dd_parsed or ""
        # (2) 创业板注册制改革前: ±10%(ST ±5%)
        if dd < _CHINEXT_REFORM:
            return 0.05 if is_st else 0.10
        # (3) 新老划断: 改革前已进入退市整理期的存量股, 整理期内沿用 ±10%。
        #    `name_of` 缺失 ⇒ 退化为改革后规则(±20%), 是**已知且被登记**的近似;
        #    生产链路恒有 NameLookup, 只有纯函数测试/袖珍调用方可能不传。
        if "退" in nm and name_of is not None:
            if "退" in (name_of(code, _CHINEXT_REFORM) or ""):
                return _DELIST_LEGACY_CHINEXT
    # (4) 主板风险警示: 2026-07-06 起由 ±5% 放宽至 ±10%
    if is_st and b == "main":
        if dd_parsed is not None and dd_parsed < _MAIN_ST_REFORM:
            return _LIMIT_ST["main"]
        return _LIMIT["main"]
    return _LIMIT_ST[b] if is_st else _LIMIT[b]


def _norm_date(d) -> str:
    """YYYYMMDD / YYYY-MM-DD / Timestamp → YYYYMMDD(str)。比较用, 不可解析则返回空串
    (空串 < _CHINEXT_REFORM ⇒ 走历史档 = 保守方向, 不会把限幅放宽)"""
    s = str(d).strip().replace("-", "").replace("/", "")[:8]
    return s if s.isdigit() else ""


def limit_ratio_hist(dstr: str, name_of=None):
    """返回一个 `(code, is_st, name=None) -> ratio` 的**绑定当日**的取值器。

    `_exec_side` 那类逐股循环里, 用它比每次重复传日期少一次参数拼装; 语义与
    `limit_ratio(code, is_st, dstr, name, name_of)` 逐位相同。

    2026-09-12: 补 `name` / `name_of` 两个入参 —— 原实现只绑日期, 于是 S股与创业板
    退市整理期新老划断这两档**在它里面永远是死的**(拿不到当日名称), 传与不传日期
    得到同一个板块档。逐格比对时它是唯一没有修复的路径。
    """
    def _f(code: str, is_st: bool = False, name: str = None) -> float:
        return limit_ratio(code, is_st, dstr, name, name_of)
    return _f


def round2(x: float) -> float:
    """四舍五入到分(半进位) —— 交易所涨跌停价取整规则。

    2026-09-08 全项目审查: 原各处用 Python round() 是银行家舍入(half-to-even),
    在 .x5 分位边界比交易所规则差 1 分(如 3.685 → 交易所 3.69, round() 给 3.68)。

    ⚠️ 2026-09-12 D06/D07 验收再修: **光换成 half-up 不够**。浮点表示会让 `.x5`
    边界悄悄落到下方。实例: `17.15 × 1.1` 在 float64 里是 `18.864999999999998`
    (打印成 18.865, 骗过眼睛), ×100 得 `1886.4999999999998`, `floor(+0.5)` = 1886
    ⇒ 18.86, 而交易所公布 **18.87**。实测 2 位小数价格 × 四档限幅的 16 万种组合里
    **0.83% 差 1 分** —— 与 D06/D07 逐格比对 0.68% 的残差分歧同源。

    容差取 1e-9 的依据: `x*100` 的量级 ≤ ~1e5, float64 几个 ulp 的误差约 1e-11;
    而 A 股价格是 2 位小数 ⇒ `x*100` 到真实分界(.5)的距离要么是 0、要么 ≥ 0.5。
    1e-9 比浮点误差大 100 倍、比真实分界小 5 亿倍, 两侧都有巨大余量, 不可能跨越。
    (前提: 入参 x 是「2 位小数的价格 × 百分数限幅」这类**十进制上有限位**的量。
    `round2` 是本项目专用的涨跌停价取整器, 不是通用 round —— 通用四舍五入请别调它。)

    **★ 2026-09-12 D06/D07 L3 再挖出: 上面那条「前提」会被 float32 打破, 而且只向下破。**

    回测的市场矩阵 `factor_engine.load_market()` 是 **float32**(省内存), 所以
    `backtest.engine._exec_side` 里的 `pre_close` 是 `np.float32`:

        round2(2.10 × 0.95)             = 2.00   ← float64, 与交易所一致
        round2(np.float32(2.10) × 0.95) = 1.99   ← 引擎运行时, 低一分

    `np.float32(2.10) = 2.0999999046`, ×0.95 = `1.99499990 < 1.995` ⇒ `+0.5` 后
    `199.99999 < 200` ⇒ floor 掉到 199。**1e-9 的容差是按 float64 标定的:
    在 x*100 单位下它只有 1e-7, 而 float32 的表示误差是 5e-5 —— 小 500 倍。**

    全样本实测(`scripts/oneoff/diag_float32_limit.py`, 10.37M 格): **162,414 格被移动,
    方向 100% 取低一分**(上轨↓ 162,414 / 上轨↑ 0; 下轨↓ 162,414 / 下轨↑ 0), 其中
    **156,409 格**是「float64 公式与交易所一致、float32 运行时不一致」, 反向 **0 格**
    ⇒ **float32 路径严格劣于 float64 路径**。100% 向下是 `floor(x+0.5)` 在十进制半整数
    边界上的结构性后果(这些格正是半进位要处理的那批), 不是 float32 的偏置 —— 误差
    为负才掉 1, 为正则不动。

    **后果已在组合层量出**: 真值臂比推导臂低 **1.07pp/年**(系统线, prod 85 期),
    全部来自一只股票 —— `002638.SZ *ST勤上` 2020-07-01~07 连续 5 个一字跌停, 引擎拿算低的
    `dn=1.99` 去比 `close=2.00` ⇒ 判「卖得出去」并成交; 真值口径下那 5 天封死, 最终
    20200803 才以 **1.51** 卖出(−28%)。

    ✅ **已修, 但不在这个函数里。** 修法在**调用侧** `backtest.engine._exec_side` 的
    `PRE_CLOSE_QUANTIZE`(`pv = round(float(pv), 2)`, 默认 True): 2026-09-12 E5 预注册
    (`experiments/01_data/E5_float32_limit.py`, 判据跑数前冻结)判决 **P2 = PASS** ——
    量化后两窗净值序列与「真值臂(读 `stk_limit` 公布值)」**位级一致**(最大绝对差
    0.000e+00)。代价方向**变差** 满仓 -1.15pp / 系统 -1.07pp 年化、两窗回撤一格未动,
    pin 随之重钉为 `20260912_limitquant`(旧档用开关置 False 复现)。

    ⚠️ **别为了 float32 输入去动 `round2` 自己。** 那个 `1e-9` 是给 float64 路径标定的
    数值容差, 全项目的 `round()` 语义都挂在它上面; 按输入精度去改它 = 按调用方状态改
    数学库。修复的正确位置是**让喂进来的数先回到它本来的刻度**(价格是 2 位小数)。
    且**实盘从未受影响**: `app/paper/engine.py:140` 从 DB 标量(float64)算限价、用
    **开盘价**判一字板、并有 `_no_limit_day` —— 本缺陷一直是**纯回测口径**的。

    NaN 透传(与旧 round() 行为一致): 上游过滤条件对 NaN 比较判 False(保守剔除)"""
    import math
    if isinstance(x, float) and math.isnan(x):
        return x
    return math.floor(x * 100 + 0.5 + 1e-9) / 100


def limit_price(pre_close: float, ratio: float, board: str = None) -> float:
    """交易所涨跌停价 = 前收盘×(1±幅度) 取整到分。

    board 缺省/非 "bse" ⇒ 沪深规则 half-up(round2), 与既有口径**逐位相同**;
    board="bse" ⇒ **北交所取整规则(实测): 上轨 floor、下轨 ceil** —— 即限价向
    pre_close 方向收, 非半进位。2026-10-05 逐格验证(diag_stklimit_window_check):
    2026 年 98,750 个 BSE 格与交易所公布值位级零失配; half-up 下 up 失配率 ~50%/
    dn ~38%, 与 floor/ceil-vs-half-up 的理论差异率吻合。ratio>0 ⇒ 涨停(floor),
    ratio<0 ⇒ 跌停(ceil), ratio==0 ⇒ round2(中性)。对策略 Δ=0(打分表无 BSE,
    与 2026-09-28 沪 B 900xxx 归类修正同类纯潜伏修正, 不换 pin)。
    ±1e-9 守卫理由同 round2(x*100 量级 ≤1e5 时 float64 ulp ~1e-11, 真实 .0 分界
    距离 ≥1, 两侧余量巨大)。None/NaN 透传为 NaN(旧 .map(round2) 路径里 pandas 把
    None 先归 NaN 再比, 比较恒 False ⇒ 行保留 —— 逐位一致)。"""
    import math
    if pre_close is None:
        return float("nan")
    raw = pre_close * (1 + ratio)
    if board == "bse" and not (isinstance(raw, float) and math.isnan(raw)):
        cents = raw * 100
        if ratio > 0:
            return math.floor(cents + 1e-9) / 100
        if ratio < 0:
            return math.ceil(cents - 1e-9) / 100
    return round2(raw)


def no_limit_trading_days(code: str) -> int:
    """上市初期不设涨跌幅限制的交易日数(0=始终有限幅)。
    调用方负责计算"上市后已过多少个交易日"(交易日历序号差), 判定当日是否在窗口内。"""
    return _NO_LIMIT_DAYS[board_of(code)]


def lot_shares(code: str, affordable: int) -> int:
    """按板块整手规则计算可买股数; affordable=预算可负担的最大股数(已含费用)。
    multiple 板块向下取整到整数倍; min 板块不足最低起买量返回 0(买不起)。"""
    rule, n = _LOT[board_of(code)]
    if affordable < n:
        return 0
    if rule == "multiple":
        return (affordable // n) * n
    return affordable
