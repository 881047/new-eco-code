import copy
import logging
import os
import re
import tempfile
from pathlib import Path

import yaml
from dotenv import load_dotenv

log = logging.getLogger(__name__)


# ---------------------------------------------------------------- config.yaml 专用装载器
class _ConfigLoader(yaml.SafeLoader):
    """`config.yaml` 专用 YAML 装载器（2026-09-13）。

    为什么单开一个：PyYAML 走 **YAML 1.1** 解析，而 1.1 的浮点正则**要求小数点** ——
    `1e-05`（指数不带点）不被认成 float，会**静默落成字符串**。而 `save_config` 用 ruamel
    round-trip 写回时，恰好把 float `0.00001` 写成 `1e-05`
    ⇒ 「读 → 设置页保存 → 再读」这一步就把数字悄悄变成 str：
    `validate_config` 报 `fees.transfer_fee 必须是非负数字, 实际 '1e-05'`，
    且 ruamel 的绕写让**注释还在**，看上去像"没人动过"（2026-09-13 实测踩到，见 07 号 #21）。

    这里补一条隐式解析器（只挂在本 Loader 上，**不动全局 SafeLoader**），让
    `1e-05` / `1e-5` / `1.0e-05` 与标准写法一样解析成 float。
    """


_ConfigLoader.add_implicit_resolver(
    "tag:yaml.org,2002:float",
    re.compile(r"""^(?:[-+]?[0-9][0-9_]*(?:\.[0-9_]*)?[eE][-+]?[0-9]+   # 指数不带点: 1e-05
                    |[-+]?[0-9][0-9_]*\.[0-9_]*(?:[eE][-+]?[0-9]+)?    # 常规小数
                    |[-+]?\.[0-9_]+(?:[eE][-+]?[0-9]+)?                 # .5
                    |[-+]?\.(?:inf|Inf|INF)|\.(?:nan|NaN|NAN))$""",
               re.X),
    list("-+0123456789."),
)

BASE_DIR = Path(__file__).resolve().parent.parent
ENV_FILE = BASE_DIR / ".env"
CONFIG_FILE = BASE_DIR / "config.yaml"
DATA_DIR = BASE_DIR / "data"
# 日志目录可用环境变量改道（2026-09-13，07 号 #1 同类复发）：测试/一次性脚本要能跑**真 app**
# （TestClient 会走 lifespan → setup_logger）而**不往生产 app.log 里写**。必须是环境变量而不是
# 只 patch 模块属性 —— 子进程会继承环境变量，patch 不会（tests/test_logger_utf8.py 就起子进程）。
# 未设置时逐字等于原路径，生产行为不变。
LOG_DIR = Path(os.environ.get("NEWECO_LOG_DIR") or (BASE_DIR / "logs"))

load_dotenv(ENV_FILE)

TUSHARE_TOKEN = os.getenv("TUSHARE_TOKEN", "")
HITHINK_FINANCE_API_KEY = os.getenv("HITHINK_FINANCE_API_KEY", "")

#: 服务监听端口（2026-09-22）。**为什么要有这个常量**：8787 原先硬编码散在 8+ 处
#: （`app/notify.py` 的看板链接、`scripts/service_watchdog.py` 的探活与拉起命令、
#: `scripts/audit_engineering.py` 的探活、`scripts/diag_narrow_viewport.py` 的默认 URL、
#: `start.bat` 的 4 处、`frontend/vite.config.js` 的 dev proxy）⇒ 改端口要改一堆地方，
#: 漏一处的症状是**静默的**：探活探错端口（看门狗会误判"服务不在"并重复拉起）、
#: 或邮件里的看板链接打不开。收成**一个口径**：环境变量 `NEWECO_PORT`（与
#: `NEWECO_LOG_DIR` 同款），默认 8787 ⇒ **不设该变量时行为逐字不变**。
#:
#: ⚠️ 两点必须知道：
#:   1. **前端生产产物里没有端口** —— `frontend/src/api.js` 全走相对路径 `/api/...`，
#:      静态文件由同一个服务端出 ⇒ 改端口**不需要重建前端**。只有 dev server 的
#:      proxy（`frontend/vite.config.js`，仅开发时生效）要跟着读同一个变量。
#:   2. `scripts/service_watchdog.py` **刻意不 import 本模块**，而是自己读同一个环境变量 ——
#:      它是自愈层，**不能因为 app 侧 import 失败就跟着失效**。两边默认值由
#:      `tests/test_port_single_source.py` 钉住一致。
DEFAULT_SERVER_PORT = 8787


def server_port() -> int:
    """服务监听端口：`NEWECO_PORT` 优先，非法值**回落默认并告警**。

    刻意**不抛异常**：端口写错不该让整个服务起不来（这与 `validate_config` 只记日志同一条原则
    —— 拦在写入侧才是能真正防住的地方）。
    """
    raw = (os.environ.get("NEWECO_PORT") or "").strip()
    if not raw:
        return DEFAULT_SERVER_PORT
    try:
        p = int(raw)
    except ValueError:
        log.warning("NEWECO_PORT=%r 不是整数 ⇒ 回落 %d", raw, DEFAULT_SERVER_PORT)
        return DEFAULT_SERVER_PORT
    if not 1 <= p <= 65535:
        log.warning("NEWECO_PORT=%d 越界(1-65535) ⇒ 回落 %d", p, DEFAULT_SERVER_PORT)
        return DEFAULT_SERVER_PORT
    return p

# 默认模板(2026-09-04 审查 P1-9): load_config 深度合并, 手改 config.yaml 缺键不再各处 KeyError;
# 数值类默认与 README 保持一致(capital 10 万 / 成本 0.15%), 与用户实际配置无关(用户值优先)
DEFAULT_CONFIG = {
    "capital": 100000,
    "risk_profile": "balanced",
    "max_stocks": 12,
    "industry_max_weight": 0.25,
    "single_side_cost": 0.0015,
    "backtest_run": "latest",  # 看板生产基线 run_date("latest"=取最新, 实验 run 不覆盖基线)
    # ---- 训练起点 vs 评估起点(2026-09-11 拆开, 用户拍板) ----
    # 2026-09-10 曾用**同一个键** data_start='20190722' 同时收紧三处口径(训练数据、
    # 评分月、回测评估窗)。实测证明这三处**不是同一件事, 且效应方向相反**
    # (scripts/oneoff/exp_start_2019.py + exp_train_months.py, 资金 2 万, 逐笔真实费用):
    #   评估窗  run_backtest(start=)     只裁评估窗口, 一个分数都不改 —— 11.05%/-41.90%
    #                                    → 20.64%/-19.66%, 年化 +9.59pp(**纯口径, 见下警告**)
    #   训练起点 train_run(train_start=)  同时砍训练窗口与评分月 —— 同窗控制臂
    #                                    (20.64%) → 15.42%, 年化 **-5.22pp**、IC -0.0129
    # 原组合(两个都改)= 15.42%/-21.26%, 被「只改评估窗」完全支配。用户 2026-09-11 在
    # 知悉该支配关系后拍板:**训练恢复全历史, 评估窗保持 2019-07-22** ⇒ 两个键分开。
    #
    # 训练起点为何必然伤收益(算术, 非观点): 评分月 i 的训练窗口 = md_all[i-36:i-1]。
    # train_start 后 md_all[0]=2019-07-31, 于是**评分月 2022-07 起窗口完全落在 2019-07
    # 之后 → 与全历史模型逐股逐分完全相同**。实测 20220729~20260630 共 48 个月最大分数
    # 差 0.000e+00, 2022-08-01 起子窗口回测与全历史臂四位小数全等; 20260731/20260831
    # 秩相关 1.0000、top-6 选股 6/6 一致(纯刻度差)。⇒ 代价**全部**集中在
    # 2020-08~2022-06 那 23 个月(训练窗口被截到 6~23 个月, 不足 walk-forward 的 36 个月),
    # 而 2024-01 起两臂逐月全等 —— 即这次回滚**只影响 2020-2022, 不碰 2024-2026 的收益来源**。
    #
    # data_start = 训练数据与评分月的起点。None = 全历史(2026-09-10 前的原本行为)。
    "data_start": None,
    # eval_start = 回测**评估窗口**起点(只影响回测统计, 不喂给 trainer)。科创板开市日。
    # ⚠️ 口径警告(必须与成绩一起记录, 不得当作收益改善): 评估窗这一半的 +9.59pp
    # **没有一分钱来自策略变好**, 全部来自「不计 2017-10-11→2018-10-25」——
    # 那正是旧基线 -41.90% 最大回撤的所在地。这是看到结果之后选窗口。日后引用
    # 本基线成绩时, 必须写明「起点 2019-07-22, 不含 2018 崩盘」。
    #
    # 【2026-09-12 重钉 pin —— 上面 33-36 行的绝对值是「修复前口径」】
    # D06/D07 限幅修复进生产(7 个文件, A→B = -0.17pp/年)**没有重钉基线** ⇒ pin
    # `20260911_fullhist` 记的 23.97%/20.64% 变成现行代码产不出的数字, 且连咬两个预注册
    # 脚本(`ab_const_n_5v6.py` 第 1 轮据此作废、`exp_train_months.py` 的 VALIDATE 同形)。
    # 根因是 pin **没有再生路径**, 故补 `scripts/pin_baseline.py`(只测量、不自动改 config)。
    # 那次重钉 = prod 满仓 23.77%/-24.10%、系统 20.47%/-19.66%; 全史 12.36%/10.90%
    # (pin `20260912_fullhist`)。
    # ⚠️ **Δ 不受影响**(每个实验都自带同期控制臂, 两臂同代码); 受影响的只是**绝对水平**,
    #    且所有已判决实验的余量都在 3pp 以上 ⇒ 历史判决无需重跑。引用 09-12 之前的任何
    #    **绝对**成绩时须标注「D06/D07 修复前口径(约乐观 0.17~0.20pp/年)」。
    #
    # 【2026-09-12 当日第 2 次重钉 —— E5 限价入参量化(P0-03)】
    # 现行代码(逐位可复现) = prod 满仓 22.62%/-24.10%、系统 19.40%/-19.66%(日频 -31.72%);
    #                        全史 115 期 满仓 11.59%/-51.26%、系统 10.16%/-42.10%。pin
    #                        `20260912_limitquant`。**两窗回撤一格未动, 只动年化**。
    # 为什么: `_exec_side` 从 **float32** 市场矩阵取 `pre_close`, 而 `round2` 的 1e-9 容差
    # 按 float64 标定 ⇒ 全样本 16.2 万格限价被取低一分(**只向下**), 组合层代价 = 比真值
    # 乐观 1.07pp/年(002638.SZ 连续 5 个一字跌停被判成"卖得出去")。修法 = 调用侧量化回分。
    # 判决: `experiments/01_data/E5_float32_limit.py` 预注册, **P2 = PASS** —— 修复后两窗
    # 的日频+月频净值序列与「真值臂(读 `stk_limit` 公布值)」**位级相同**(最大绝对差 0.000e+00)。
    # ⭐ 三个口径都可再生(`app/backtest/engine.py:PRE_CLOSE_QUANTIZE` 翻转 / D06/D07 的
    #    `_legacy_ratio` 臂), 所以引用任一档的绝对成绩时**必须写明是哪一档**。
    # ⚠️ 本轮**仍未接线** `no_limit_days()`(上市初期无涨跌幅限制, 模拟盘有/回测无)——
    #    那是独立的一项, 别把它当成已被 E5 顺手修掉。
    "eval_start": "20190722",
    # factor_exclusions = 生产训练/评分时从 factor 表**剔除**的因子名单（2026-09-27 第 6 次
    #    重钉采纳，用户指令"继续"拍板；预注册 plans/excl-pool-ab-prereg-20260927.md +
    #    05 号台账十九/二十补）。依据 = S5 时效轴官方重测（十七补）：这 5 个因子执行延迟
    #    半衰期 1 个月（HL=1 < 2×调仓周期），组合层 A/B 证实剔除后 Δsys_ann prod
    #    +5.08/+8.39pp、full +5.20/+6.19pp（双种子集双窗全过 P1，padm_exclmin）。
    #    缺省 [] = 不过滤（测试/新装机旧行为）——与 chase.enabled 同款"DEFAULT 关"纪律。
    #    ⚠️ 改动本键 ⇒ factor 指纹变更 ⇒ 增量训练会被守卫拒绝，必须 train_model.py --full。
    "factor_exclusions": [],
    # 持仓只数(2026-09-10 改恒定 6, **用户拍板的选档纪律例外**): 原文为
    # 「6/5/4 是分散化折中, 档位已冻结, 固定6样本外虽高但按选档纪律选不到, 不得反向改投」。
    # 现改恒定 6 的理由**不是回测值更高**, 而是 6/5/4 内部不自洽 —— defense→只持 4 只在降
    # 仓的同时抬高集中度, 即防守动作本身在加风险: A/B v1_worse 多出的防守日把满仓线回撤
    # 从 -58.75% 推到 -60.54%; 而固定 6 单独就把满仓线改为 12.56%/-51.00%。关择时与固定
    # 只数各自独立地在年化/回撤两轴都改善, 合并后超叠加(见 scripts/oneoff/ab_style_guard.py
    # 的 v4 臂)。纪律例外已获用户 2026-09-10 明确同意。
    "state_stocks": {"attack": 6, "neutral": 6, "defense": 6},
    # 候选池补位(2026-09-05 第五轮复查 P5-1, 0=不补位): 被整手/行业上限挡掉后向下补位
    # 的候选数; 实证在 1 万本金下补位系统性恶化回撤(闲置现金是保护性的), 默认关闭
    "stock_candidate_top": 0,
    # 迟滞带(R11-5, 2026-09-08 A/B 证伪后默认关闭): 在册持仓未跌出 top(n+hyst) 不换仓
    # —— 纯语义(分配池=保留名单∪top-n, 无隐式补位)全期扫描 hyst 1~4: 回撤纹丝不动
    # (-39.36% 主坑是 2017-2018 市场性下跌, 组合层构造打不动), 换手仅降 ~4pp(月度
    # 排名持续度太低, 机制发挥空间小), 年化非单调 ±0.9pp(噪声级)。walk-forward:
    # 选档段 2017-2021 hyst=0 最优(29.87% vs 28.46/28.68) → 按选档纪律不采纳;
    # 样本外 hyst=3/4 虽优(+2.9/+1.9pp, 换手 -5pp)但不可选。机制保留(engine
    # --hysteresis), 默认关闭; 未来资金做大/换手成本显著上升时可复测
    "hysteresis_buffer": 0,
    # R11-4 波动率目标仓位(2026-09-08 深度验证后回退删除): 曾实现 w=eq_w×min(1,
    # vt/基准滚动已实现波动率) 仅压缩系统线。证据否决 —— ①5 折滚动 walk-forward
    # (vt0 vs vt20 系统线夏普): fold0 2017-2018 输(-1.448/-1.696)、fold1 2019-2020
    # 赢(1.498/1.733 但年化-13.9pp)、fold2 2021-2022 赢(0.643/0.960)、fold3
    # 2022-2024 平(1.764/1.779)、fold4 2024-10~2026-08 最近段输(1.750/1.588) ——
    # 效应符号折间不稳定; ②vt 15~25%×lookback 60/120 敏感性网格: 17.5~20% 平缓
    # 平台(非孤立峰值), 全期夏普 +0.08 但年化 -2.5~-4pp; ③月频回撤全档位纹丝不动
    # (-30.02→-29.98 最好, 仅 vt=15% 降 2.6pp 且年化 -4.5pp) —— "降回撤"目标从未
    # 达成。机制与影子实测(vol_shadow)已整体回退删除, 结论留档此处防重复立项
    # R11-3(2026-09-06): 市值/流动性硬过滤 —— 实验证伪后默认关闭(fail-safe, 第十七轮
    # R17-1): 年化 -6.9pp 且回撤未降, 小盘是模型核心 alpha。0=不过滤(注: NaN 数据仍
    # 保守剔除)
    "stock_size_pctl": 0,
    "stock_min_amt20": 0,
    # R11-2(2026-09-06): 行业上限自适应下界 —— eff_cap=max(industry_max_weight,
    # floor/只数), "至少容得下 floor 只同行业"; 2=历史现状, 1=让配置 25% 真正生效
    "industry_floor_stocks": 2,
    # R17-2(2026-09-06): regime 估值层开关固化(原每次实时探测接口可用性, 权限降级时
    # score_max 7→6 全史重写污染系统线)
    "regime_use_valuation": True,
    # 北交所纳入(2026-09-08, 默认关闭): 数据修复(lhb 全市场回填/上市锚定/训练窗口
    # 样本阈值 BSE_MIN_SAMPLES)后三轮实证 —— 主板+创业+科创 29.27%/夏普0.817/IC 0.0835
    # 最优; 纳入北交 23.80%/-39.4%/0.668/IC 0.0788 全面变差(收益也掉, 非仅回撤)。
    # 按证据默认排除, 机制与数据保留, 未来数据基础变化后可重开
    "bse_enabled": False,
    # 科创板开关(2026-09-08 用户拍板, 默认关闭, 训练与选股同开关): 归因实证两层——
    # ①选股侧"候选名额占位"(200 股整手×1万本金下高分科创占 top 名额买不起/行业上限
    # 挡, 累计拖累 -87%); ②全市场训练造成风格漂移(同口径下全市场训练模型在主板选股
    # 回撤 -44.65% vs 主板训练模型 -30.07% —— 20cm 高波动样本让模型风格偏离交易域)。
    # 训练域=选股域原则: 关闭时科创同时退出训练样本与选股截面。
    # 注(2026-09-08 全项目审查): 上述归因数字均为"回测未过滤 ST/退"时代的旧口径 ——
    # 当日修复回测候选池 ST/退 PIT 过滤(与 builder 对齐)后, 诚实基线大幅下修:
    # 全期 9.73%/-40.36%/夏普 0.361(系统线 6.63%/-31.32%), 差值来自 116 只次 ST/退
    # 候选(66% 正收益/月均 +8.5%, 2023-2025 密集) —— 实盘 builder 一直有过滤, 这些
    # 股票永远买不到, 旧基线虚高属回测口径缺陷而非策略损失
    "star_enabled": False,
    # R18-2(2026-09-06): 基准指数 —— 原 config.yaml 死键+factor_engine/regime 两处
    # 硬编码, 现统一从 config 读(见 benchmark_code())
    "benchmark": "000906.SH",
    # 基准**净值/超额**腿(2026-09-20 B1 采纳轮): 与上面那个键**故意不同源**。
    # 为什么必须两个键 —— `benchmark` 同时是 regime 择时均线、style 风格读数、模拟盘基准列
    # 与邮件"基准指数"文案的源, 换它 = 换策略输入; 而回测的超额要的是与持仓腿
    # (`close×adj_factor`, 全收益)同一把尺的价格指数**全收益版**(H00906.CSI, 税前分红再投资),
    # 否则超额被抬高一个基准股息率(实测 2.22pp/年, 两窗一致)。
    # 只供 `factor_engine.load_market()` 的 `bench_tr` 腿(回测报告 + 袖套 KPI)消费;
    # `m["bench"]`(=本键)继续喂 `compute_price_factors` 的 `beta_250` —— 那是生产特征, 动它要重训。
    # 缺省写全收益 ⇒ 删掉 config.yaml 那行不会静默退回旧尺(代价: 回滚要显式写 000906.SH)。
    "benchmark_nav_code": "H00906.CSI",
    # 权益仓位(2026-09-10 定档 0.873 恒定): 原 aggressive 按状态择时(1.0/0.9/0.8)被同平均
    # 仓位的恒定仓两轴支配(full +7.86%/-51.81% → +11.05%/-41.90%), regime 权益择时的贡献
    # 为负 —— 状态对池子有排序力(未来 1 月 attack +2.92% / neutral +1.55% / defense +1.37%,
    # 单调)但防守月平均仍是赚的, 降仓 = 少赚而非避险; 且 -53.84% 是 2017-10 → 2024-02 的
    # 6.3 年长坡, 不是可躲的崩盘。风格防守(换用等权全A 输入)经 A/B 证伪: 完全替换后系统
    # 回撤仍 -51.63%。证据 scripts/oneoff/ab_style_guard.py / scan_const_weight.py
    "regime_equity_weight": {
        "aggressive": {"attack": 0.873, "neutral": 0.873, "defense": 0.873},
        "balanced": {"attack": 0.8, "neutral": 0.65, "defense": 0.5},
        "conservative": {"attack": 0.5, "neutral": 0.35, "defense": 0.2},
    },
    # 风格防守(2026-09-10, 默认关闭待 A/B): 现行 regime 的均线/回撤/波动全算在
    # 中证800(大中盘)上, 而持仓是小盘 —— 2023-11-20→2024-02-07(系统线历史谷底)
    # 池子跌 30.4%、中证800 只跌 7.4%, 防守信号对真正的风险是瞎的。开启后池子分支
    # (等权全A, 见 app/models/pool_index.py)用同一套规则并行计算:
    #   mode=worse   取更防守的一路(只加防守, 不放松进攻)
    #   mode=replace 完全改用池子分支
    "regime_style_guard": {"enabled": False, "mode": "worse"},
    "email": {
        "enabled": False,
        "smtp_server": "",
        "smtp_port": 465,
        "sender": "",
        "auth_code": "",
        "recipients": [],
    },
    "dynamic": {
        # 2026-09-06 第十七轮 R17-1: 默认关闭(fail-safe) —— 固定 -15% 止损已被 A/B
        # 证伪(年化 -25.76%/回撤 -96.31%), 缺键/重置/换机时不得静默复活更差路径
        "enabled": False,
        "stop_loss": -0.15,
        "weak_pct": 40,
        "swap_drawdown": -0.08,
        "candidate_top": 20,
        # 2026-09-04 新增: 弱势替换保护与信号冷却
        "min_hold_days": 3,
        "weak_confirm_days": 2,
        "cooldown_days": 5,
    },
    # 2026-09-14 外部审查 P1-5：周频袖套段进**配置契约**。
    # 此前 `DEFAULT_CONFIG` 里没有 `weekly` ⇒ ① `weekly: "x"` 这类手滑不被拦、也不自愈
    # （周频档里的 `(cfg.get("weekly") or {}).get(...)` 会直接 AttributeError）；
    # ② `weekly.risk_halt.enabled` 之类"能写但没人校验"。下面的默认值 = **每一处代码兜底的原值**
    # （逐条对过），所以缺省行为一寸没变：袖套缺省**停用**（fail-safe）。
    # ⚠️ 代价（预期内，不是漂移）：`weekly/plan.py::_meta` 的 `config_sha16` 会变 —— 那个指纹
    #    把整个 `weekly` 子字典喂进 sha，新增键必然换值（与 2026-09-13 `fees` 那次同类）。
    "weekly": {
        "enabled": False,          # 与 scheduler 的 `_weekly_staged` 兜底一致(缺省=不跑)
        "email": False,            # 与 weekly/report.py 的 `w.get("email", False)` 一致
        "capital": None,           # None = 回落到顶层 capital(plan.weekly_capital 的兜底)
        "equity_weight": None,     # None = 回落到 regime_equity_weight.aggressive
        "max_lot_cost_mult": 1.5,  # plan.build 的兜底
        # auto_resume(2026-09-28 用户拍板"改成自动解除"): true 时健康档读到 healthy 自动
        # 解除停买(risk.maybe_auto_resume; 滞回: 触发要 degrading/dead, 解除要 healthy,
        # warning 不解除)。缺省 False = 旧"只人工 resume"口径(测试/新装机不变);
        # 生产 config.yaml 已开 true —— 与 chase.enabled 同款双轨。
        "risk_halt": {"enabled": True, "auto_resume": False},   # enabled=兜底默认开
        "shape": {},               # 显式覆盖 pick_shape(top_n/band/hold_bonus); 空=按资金自动
        # 2026-09-25: 袖套版回撤治理(app/weekly/risk.py::governor, 纯函数在
        # app/portfolio/governor.py) —— w=1.0 生产构型 MDD -36.6%(F52) ⇒ -20% 触发 =
        # 深回撤期周度重建仓停开新仓(退出照旧), 回升 ≥ -10% 自动解除。
        # ⚠️ 新增键必然换 plan.meta.config_sha16(整个 weekly 子字典进 sha) —— 与
        #    2026-09-14 weekly 段进契约那次同类, 预期内代价, 不是漂移。
        "governor": {"enabled": True, "dd_trip": -0.20, "dd_reset": -0.10},
    },
    "chase": {
        # 建仓追高剔除闸(2026-09-26 用户拍板采纳; 单一实现 app/portfolio/chase.py)。
        # 对最终 top-n 候选中 chase_z = max(z(mom5), z(devvwap)) > theta 的票**剔除、
        # 不补位** —— 三轮预注册 A/B(05 号台账 七补/九补): 85 期 +2.07pp/全史 +1.47pp
        # (θ=2.0), 剂量曲线 θ=1.5/2.0/2.5 → +0.80/+1.47/+1.10pp, min60 原信号方向不反。
        # ⚠️ DEFAULT 保持 False: 生产由 config.yaml 显式开启 —— 测试/新装机的
        # DEFAULT_CONFIG 路径保持旧行为, golden 回归不受影响。
        "enabled": False,
        "theta": 2.0,
    },
    "paper": {
        "slip": 0.001,
        # 2026-09-07 开盘档: 集合竞价后 09:31 按实时今开价执行昨日挂单(盘中可见持仓)。
        # 默认关(fail-safe, 晚间链 18:00 日线开盘价兜底, 两者成交价口径一致) ——
        # 本机 config.yaml 显式开启
        "open_exec": False,
        # 交易总闸(2026-09-12 审查 P0-07): 三处消费点(paper.trading_paused /
        # scheduler / web api)全用 .get(..., False), 而本键**原先不在 DEFAULT_CONFIG**
        # ⇒ 换机/回滚/手删该键时「暂停交易」静默失效(fail-open), 与 neighbors 的
        # fail-safe 取向相反。此处**只把键补成显式存在**, 缺省方向仍是 False ——
        # 「缺键=未暂停」是 tests/test_trading_pause.py:162 明确锁定过的既有语义,
        # 改方向要用户拍板, 不在本次修复范围。
        "paused": False,
        # 2026-09-25: 账户级回撤治理(决策函数, app/portfolio/governor.py) —— 净值自峰值
        # 回撤 ≤ dd_trip 触发「谨慎态」: 月度方案停补仓、候选替换买入停, 卖出照常;
        # 回升 ≥ dd_reset 自动解除(滞回带, 纯函数可复算, 无持久状态)。
        # ⚠️ 2026-09-25 A/B 判决后**月盘缺省停用**(experiments/governor_overlay_ab_20260925.json):
        # 月换手 89.5% 下「停买+退出照旧」≈ 清仓陷阱 —— 治理臂系统线 -12.76pp/年 且 MDD
        # 反而恶化; 时限版(gov3) MDD 仍恶化 ⇒ 两变体皆否。**weekly.governor 同日 A/B 采纳**
        # (-3.14pp/年 保费换 MDD 改善 13.75pp) —— 同一决策函数, 两盘结论相反, 差异在换手率。
        # band() 的代码级兜底(enabled 缺省 True)只服务"配置段缺失/类型损坏"的防御场景,
        # 生产缺省以本键为准 = false(证据取向, 与 risk_halt 的 fail-safe 取向是两回事:
        # 那是防"该停没停", 这里是防"实测有害的东西被意外打开")。
        "governor": {"enabled": False, "dd_trip": -0.20, "dd_reset": -0.10},
    },
    # 逐笔交易费用(2026-09-10): 回测/模拟盘的成本口径 —— 用户核实券商为「佣金单笔
    # 最低 5 元」, 原扁平 single_side_cost(0.3%) 在 2 万/4~6 只口径下高估(每笔约
    # 3000 元 → 5 元 = 单边 0.167%), 且无法反映**最低佣金对小仓位的非线性惩罚**
    # (1500 元一笔 = 0.334%) —— 本策略长期是小仓位组合, 扁平费率会系统性美化结果。
    # single_side_cost 自此仅作整手分配的预算预留(保守), 不代表成本假设。
    "fees": {
        "commission_rate": 0.00025,  # 佣金费率(万2.5)
        "min_commission": 5.0,       # 单笔最低佣金(元)
        "stamp_tax": 0.0005,         # 印花税(卖出, 2023-08-28 起 0.05%)
        "stamp_tax_before": 0.001,   # 印花税(卖出, 2023-08-28 之前 0.1%)
        "transfer_fee": 0.00001,     # 过户费(双向 0.001%)
    },
    # ---- 框架阶段接线(2026-09-11, 方案 `plans/abstract-sleeping-rainbow.md` §3 不变量 #7) ----
    # 把用户框架图的竖线(Alpha Factory → Pool → Critic → OOS → Bank → Ensemble →
    # Optimizer → Health/Decay)接进月链, 但**每个阶段都默认关闭**。
    #
    # `enabled=false` ⇒ `framework.run_stages()` 在任何 import 之前返回, 生产链路的
    # 执行路径**逐位不变**(不是"应该不变")。这是个开关, 不是行为改动。
    #
    # ⚠️ 三个字段必须一起配才有意义, 且**都必须显式写明**:
    #   alpha_tables  {alpha_id: 打分表名} —— 给 OOS runner。**不许猜表名**: 猜错等于
    #                 拿别的 alpha 的成绩判卷(pipeline 有硬检查, 查不到直接判否)
    #   die_tables    {die名: 打分表名}   —— 给融合器。空 = 无可融
    #   investable    可投资金(元)        —— 给 optimizer 解「这个账户撑得起几只」。
    #                 注意按 §10b: 它只解出**上界**, 实际持仓数仍归 builder。
    #                 **改账户规模是用户拍板的事, 不该由自动管线悄悄做掉。**
    #
    # 已开过的阶段与它们的判据(记录在此防重复立项):
    #   pipeline —— 判决只能是组合层两轴(exp_die_stage_b.py 的预注册, 4 个不等式全成立)。
    #               `expert.kgd` 是入场券(噪声筛)不是判决, 已有 4 个「KGD 通过却对策略
    #               无用」的反例: die_flow / head_vt / h10 / margin_net_share
    #   fusion   —— **等权秩平均已当场证伪**(活跃区 top6 +3.17% → +0.76%, 而活跃区是
    #               全部收益来源); 零参数路由(电源门控/相对动量)同样被否。开这个阶段
    #               只会重跑已证伪的融合器 —— 要开必须新写一个融合器并预注册
    #   health   —— **只许「退役」, 不许「加权」**。按滚动表现加权 = 追涨杀跌(R1 电源
    #               门控已证伪: die_flow 2023 年 +6.93%/月使路由器一路供电, 正好供电
    #               进它 2024-26 最差的那段)。alpha/health.py 的 weights() 结构性抛错
    "framework": {
        "enabled": False,
        "stages": {"factory": False, "pipeline": False, "fusion": False,
                   "optimizer": False, "health": False},
        "alpha_tables": {},
        "die_tables": {},
        "ens_table": "model_score_ens",
        "investable": 0,
        "round_trip_target": 0.005,
        # 上下文构造用的两个读数(health/pipeline/fusion 用):
        #   others_tables —— 冗余检验的对照表(与它们秩相关 ≥0.95 = 换皮, 结构性退役理由)
        #   market_since  —— 面板起点。缺省取 eval_start。**只影响框架阶段**, 不碰生产
        "others_tables": ["model_score"],
        "market_since": None,
    },
}


_MISSING = object()


def _is_date8(v) -> bool:
    return v is None or (isinstance(v, str) and len(v) == 8 and v.isdigit())


def _valid_profiles(cfg: dict) -> tuple:
    """合法 risk_profile 取值 = `regime_equity_weight` 的键(builder/backtest 直接拿它取权重),
    该键本身缺失/损坏时退回硬编码枚举。"""
    rew = cfg.get("regime_equity_weight")
    if isinstance(rew, dict) and rew:
        return tuple(rew)
    return ("aggressive", "balanced", "conservative")


def validate_config(cfg: dict) -> list:
    """配置契约校验(2026-09-12 审查 P2-16): 返回问题列表, 空 = 通过。**只读, 不改 cfg**。

    为什么需要: `load_config` 只做 _deep_merge + email 类型还原, 数值/枚举一律不查 ——
    `risk_profile: aggresive` 这种笔误会一路走到 builder 深处才 KeyError(或**静默**回退到
    别的档位权重, 更坏); `single_side_cost: 5`(把 5% 写成 5)也没人拦。手改 yaml 是
    本项目里最常见的操作, 而 config.yaml 是基线口径的唯一事实源。

    调用点: `save_config` **入口**(不合法的写入直接拒掉)与 `load_config` **出口**
    (只记日志 + 类型修复, **不抛** —— 一个手改错字不该让整个服务/月链起不来)。
    """
    p = []
    if not isinstance(cfg, dict):
        return ["配置根节点必须是对象"]

    def num(key, lo=None, hi=None, allow_none=False):
        v = cfg.get(key, _MISSING)
        if v is _MISSING:
            p.append(f"缺少必填键 {key}")
            return
        if v is None and allow_none:
            return
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            p.append(f"{key} 必须是数字, 实际 {type(v).__name__}={v!r}")
            return
        if lo is not None and v < lo:
            p.append(f"{key}={v} 小于下界 {lo}")
        if hi is not None and v > hi:
            p.append(f"{key}={v} 大于上界 {hi}")

    def sub(key):
        v = cfg.get(key)
        if not isinstance(v, dict):
            p.append(f"{key} 必须是对象, 实际 {type(v).__name__}")
            return None
        return v

    num("capital", lo=0)
    num("industry_max_weight", lo=0, hi=1)
    num("single_side_cost", lo=0, hi=0.05)

    ms = cfg.get("max_stocks")
    if not isinstance(ms, int) or isinstance(ms, bool) or ms <= 0:
        p.append(f"max_stocks 必须是正整数, 实际 {ms!r}")

    rp = cfg.get("risk_profile")
    profiles = _valid_profiles(cfg)
    if rp not in profiles:
        p.append(f"risk_profile={rp!r} 不在 {profiles}")

    for key in ("data_start", "eval_start"):
        if not _is_date8(cfg.get(key)):
            p.append(f"{key} 必须是 8 位日期字符串(YYYYMMDD)或 null, 实际 {cfg.get(key)!r}")

    if not isinstance(cfg.get("backtest_run"), str) or not cfg.get("backtest_run"):
        p.append("backtest_run 必须是非空字符串")
    if not isinstance(cfg.get("benchmark"), str) or not cfg.get("benchmark"):
        p.append("benchmark 必须是非空字符串")
    if not isinstance(cfg.get("benchmark_nav_code"), str) or not cfg.get("benchmark_nav_code"):
        p.append("benchmark_nav_code 必须是非空字符串(基准净值腿, 2026-09-20 B1)")

    ss = sub("state_stocks")
    if ss is not None:
        for st in ("attack", "neutral", "defense"):
            n = ss.get(st)
            if not isinstance(n, int) or isinstance(n, bool) or n <= 0:
                p.append(f"state_stocks.{st} 必须是正整数, 实际 {n!r}")

    rew = sub("regime_equity_weight")
    if rew is not None:
        for prof, w in rew.items():
            if not isinstance(w, dict):
                p.append(f"regime_equity_weight.{prof} 必须是对象")
                continue
            for st in ("attack", "neutral", "defense"):
                x = w.get(st)
                if isinstance(x, bool) or not isinstance(x, (int, float)) or not (0 <= x <= 1):
                    p.append(f"regime_equity_weight.{prof}.{st} 必须是 [0,1] 的数字, 实际 {x!r}")

    fees = sub("fees")
    if fees is not None:
        for k in ("commission_rate", "min_commission", "stamp_tax",
                  "stamp_tax_before", "transfer_fee"):
            v = fees.get(k)
            if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
                p.append(f"fees.{k} 必须是非负数字, 实际 {v!r}")
            elif k != "min_commission" and v >= 0.05:
                p.append(f"fees.{k}={v} 明显越界(费率应为小数, 如 0.00025)")

    # 2026-09-14 外部审查 P1-5: `weekly` 补进这张白名单 —— 周频袖套的配置段一直是"契约外"的,
    # 而 `weekly: "x"` 这种手滑会让 `(cfg.get("weekly") or {}).get(...)` 在**调度档里**
    # 抛 AttributeError（现在至少会被这里拦住并给出人话报错）。
    # ⚠️ 2026-09-18 审查 P2-01 更正：本注释原写「有意**不把** weekly 加进 DEFAULT_CONFIG」，
    # 但 2026-09-14 已**实际加入**（见 DEFAULT_CONFIG 的 "weekly" 段，且已同步登记其代价：
    # `plan.meta.config_sha16` 会变 —— 那个指纹把整个 weekly 子字典喂进 sha，新增键必然换值）。
    # 即：当初记录的取舍已被执行并付出代价，这里只做类型校验，不再重复决策。
    for key in ("email", "paper", "dynamic", "framework", "weekly"):
        sub(key)          # 只查类型: 这些是下游链路的第一跳, 非 dict 会 AttributeError
    return p


def _repair_cfg_types(cfg: dict) -> list:
    """把**类型明显错**的键还原成默认值(2026-09-12 审查 P2-16 的第二半)。

    这是 2026-09-08 审查给 email 做的那次修复的推广: 那里发现 `email: "x"` 会让
    `cfg["email"].get(...)` 全站 AttributeError 且设置页无法自愈, 于是在 load_config
    出口强制还原类型。同样的坑 `paper` / `dynamic` / `fees` / `state_stocks` 一个不差 ——
    键都在 DEFAULT_CONFIG 里, 用户手写错类型就会顶掉整个子字典。

    只在**类型不匹配**时动手(值合法但非默认的配置一律不碰); 返回值是改动说明, 供调用方
    记日志 —— 不静默改配置。
    """
    fixed = []
    for k, dflt in DEFAULT_CONFIG.items():
        if k not in cfg:
            continue
        v = cfg[k]
        if isinstance(dflt, dict):
            if not isinstance(v, dict):
                # deepcopy: 直接塞 DEFAULT_CONFIG[k] 会把**共享引用**放进调用方的 cfg,
                # 调用方一改就污染默认值(项目里已有同类教训: 缓存必须返回深拷贝)
                cfg[k] = copy.deepcopy(dflt)
                fixed.append(f"{k}: {type(v).__name__} → 默认对象")
        elif dflt is None:
            continue                      # None = "任意/未设"(如 data_start 走全历史)
        elif isinstance(dflt, bool):
            if not isinstance(v, bool):
                cfg[k] = dflt
                fixed.append(f"{k}: {v!r} → {dflt!r}")
        elif isinstance(dflt, (int, float)):
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                cfg[k] = dflt
                fixed.append(f"{k}: {v!r} → {dflt!r}")
        elif isinstance(dflt, str):
            if not isinstance(v, str):
                cfg[k] = dflt
                fixed.append(f"{k}: {v!r} → {dflt!r}")
    return fixed


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


_cfg_cache = {"key": None, "merged": None}


def load_config(inject_env: bool = True) -> dict:
    """inject_env=False 时不注入 .env 授权码 —— 设置页保存用它, 防止把 .env 注入的
    授权码明文回写 config.yaml(第二轮复查 N4)。
    2026-09-06 第十轮 N10-23: mtime 缓存合并结果 —— 原每次调用重读文件+解析 yaml,
    paper 成交循环里逐笔调用(_slip)会重复读盘; 缓存命中返回深拷贝, 调用方原地修改
    (如设置页保存)不会污染缓存, 保存后 mtime 变化自动失效"""
    st = CONFIG_FILE.stat()
    key = (st.st_mtime_ns, st.st_size)  # 大小入键: 同 mtime tick 内的快速改写也能失效
    if _cfg_cache["key"] != key or _cfg_cache["merged"] is None:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            # 用 _ConfigLoader（不是 safe_load）：指数不带点的浮点(1e-05)在 YAML 1.1 下会变 str,
            # 而 ruamel 写回恰好会产生这种写法 ⇒ 见 _ConfigLoader 的 docstring 与 07 号 #21。
            user = yaml.load(f, Loader=_ConfigLoader) or {}
        _cfg_cache["merged"] = _deep_merge(DEFAULT_CONFIG, user)
        _cfg_cache["key"] = key
    cfg = copy.deepcopy(_cfg_cache["merged"])
    # 2026-09-08 审查 P1: email 被手改成非 dict(null/字符串)时 _deep_merge 的 dict 保护
    # 失效, 原 cfg.get("email", {}).get(...) 直接 AttributeError → 全部 API 500 且设置页
    # 无法自愈 —— 出口处强制还原类型, 下游不再需要各自防
    if not isinstance(cfg.get("email"), dict):
        cfg["email"] = {}
    # 2026-09-12 审查 P2-16: 上面那条 email 修复推广到全部有默认模板的键(类型错 → 还原默认),
    # 再跑一遍契约校验**只记日志**。刻意不抛: 一个手改错字不该让服务/月链整条起不来 ——
    # 拦在写入侧(save_config)才是能真正防住的地方。
    _fixed = _repair_cfg_types(cfg)
    if _fixed:
        log.error("config.yaml 有 %d 处类型错误, 已按默认值还原: %s", len(_fixed), "; ".join(_fixed))
    _problems = validate_config(cfg)
    if _problems:
        log.error("config.yaml 校验未通过(共 %d 项): %s", len(_problems), "; ".join(_problems))
    # 2026-09-05 诊断报告修复: 邮箱授权码明文不再常驻 config.yaml, 存 .env(EMAIL_AUTH_CODE);
    # config.yaml 非空(历史遗留/手工改的码)优先, 为空时回退 .env。
    # 2026-09-12 审查 P0-06: 设置页新保存的码改走 set_env_secret 写 .env 并把 config.yaml
    # 的值清空 ⇒ 正常路径下 config.yaml 该键恒为空, 上面这条「非空优先」只是兼容旧文件的兜底
    if inject_env:
        env_code = os.getenv("EMAIL_AUTH_CODE", "")
        if env_code and not cfg.get("email", {}).get("auth_code"):
            cfg["email"]["auth_code"] = env_code
    return cfg


def benchmark_code() -> str:
    """基准指数代码(2026-09-06 R18-2): config.benchmark 唯一事实源,
    回测 load_market 的 `bench`(因子/`beta_250`)与 regime 价格层共用"""
    return str(load_config().get("benchmark", "000906.SH"))


def benchmark_nav_code() -> str:
    """基准**净值/超额**腿(2026-09-20 B1): 中证800 **全收益指数**(H00906.CSI, 税前分红再投资)。

    与 `benchmark_code()` 各取各的键、**互不回落** —— 缺省值写成全收益而不是"退回 `benchmark`",
    因为退回旧尺是**静默**错(超额凭空 +2.22pp 而无人报警); 而"代码配错"会显出空腿
    (`bench_tr` 全 NaN ⇒ 超额报 NaN + `load_market`/回测各一条告警), 属显式失败。
    """
    return str(load_config().get("benchmark_nav_code", "H00906.CSI"))


def set_env_secret(key: str, value: str) -> None:
    """把密钥写进 .env(.gitignore 覆盖), 不写 config.yaml —— 2026-09-12 审查 P0-06:
    设置页 POST /settings 原样把邮箱授权码存进 **受 git 跟踪** 的 config.yaml(明文,
    git add -A 即进仓库)。此处按 2026-09-05 已定的口径补完最后一段: 新保存的码只落 .env。

    原子写(同 save_config 思路); 只改目标键那一行, 其余行(含注释)原样保留; 键不存在则追加。
    写后同步 os.environ, 免得运行中的进程要等重启才看到新码。
    """
    lines: list[str] = []
    if ENV_FILE.exists():
        with open(ENV_FILE, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
    prefix = f"{key}="
    out, done = [], False
    for ln in lines:
        # 只认行首的 KEY=(不碰注释里的同名文本, 也不碰 KEY_EXTRA= 这类前缀相同的键)
        if not done and ln.startswith(prefix):
            out.append(prefix + value)
            done = True
        else:
            out.append(ln)
    if not done:
        out.append(prefix + value)
    fd, tmp = tempfile.mkstemp(dir=str(ENV_FILE.parent), prefix=".env.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("\n".join(out) + "\n")
        os.replace(tmp, ENV_FILE)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.environ[key] = value


def _roundtrip_yaml():
    """ruamel.yaml 的 round-trip 实例(保留注释); 缺库时返回 None。"""
    try:
        from ruamel.yaml import YAML
    except ImportError:
        return None
    y = YAML()
    y.preserve_quotes = True
    y.width = 4096          # 不折行: 长列表/长字符串保持一行, 便于人读与 diff
    return y


def save_config(cfg: dict) -> None:
    """原子写(2026-09-04 审查 P1-9): 临时文件 + os.replace, 写一半崩溃不损坏 config.yaml;
    并发保存时最后完成者胜出, 不再出现半截文件。

    2026-09-12 审查 P2-16/P2-17 两处修改:

    P2-16 —— 入口先 `validate_config`, 不合法就 ValueError(调用方转 400)。这是唯一能
    **真正拦住**坏配置的地方: 拦在写侧, 读侧就永远读不到坏文件。

    P2-17 —— 原实现 `yaml.safe_dump(cfg)` 把 `DEFAULT_CONFIG` 深合并后的整份配置重写一遍,
    safe_dump 不保留注释 ⇒ config.yaml 里 35 行基线口径注释(yaml 是基线唯一事实源)
    被设置页的一次保存抹掉, 同时约 50 个默认键被实体化(以后再改默认值也追不到这些键)。
    现改为: 用 ruamel round-trip 读回**现有文件**(注释原样保留), 只把"文件里已有的键"
    或"值确实不同于默认的键"patch 上去 —— 未动过的默认键不再凭空写进文件。
    缺 ruamel 时退回原行为并告警(注释会丢, 但功能不中断)。

    P0-06 **补洞**(2026-09-12, 由被改写成"真注入哨兵值"的用例发现): 授权码的唯一合法
    去处是 .env。这里做**硬拒**, 而不是"顺手搬去 .env" —— save_config 是个纯写入函数,
    悄悄改 .env 是惊喜; 更要紧的是静默改写会让"是谁把明文码写进来的"这个事实无从追查。
    唯一的调用方 `api.post_settings` 在调它之前就已把残留明文码搬走(见那里的清洗块)。
    """
    _em = (cfg or {}).get("email")
    _ac = _em.get("auth_code") if isinstance(_em, dict) else None
    if _ac:
        raise ValueError(
            "拒绝把 email.auth_code 写进 config.yaml —— 该文件受 git 跟踪, 明文授权码"
            "会随下一次提交永久留在 git 历史里(.env 的 ignore 规则对此毫无作用)。"
            "正确做法: cfg_mod.set_env_secret('EMAIL_AUTH_CODE', <码>), 并让写出去的 cfg "
            "里该字段留空; 读配置时用 load_config(inject_env=False) 就不会把 .env 的值带进来。")

    problems = validate_config(cfg)
    if problems:
        raise ValueError("配置校验未通过: " + "; ".join(problems))

    fd, tmp = tempfile.mkstemp(dir=str(CONFIG_FILE.parent), prefix="config.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            y = _roundtrip_yaml()
            if y is None:
                log.warning("缺少 ruamel.yaml, 退回 yaml.safe_dump —— config.yaml 的注释会丢失")
                yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)
            else:
                doc = None
                if CONFIG_FILE.exists():
                    with open(CONFIG_FILE, "r", encoding="utf-8") as src:
                        doc = y.load(src)
                if not isinstance(doc, dict):
                    doc = {}          # 文件损坏/为空: 退化成整份写入
                for k, v in cfg.items():
                    if k in doc or DEFAULT_CONFIG.get(k, _MISSING) != v:
                        doc[k] = v
                y.dump(doc, f)
        os.replace(tmp, CONFIG_FILE)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
