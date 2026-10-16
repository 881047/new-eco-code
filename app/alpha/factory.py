"""Alpha Factory —— 框架图的「alpha 产生」层(2026-09-11)

    A股数据湖 → Point-in-Time Engine → ┬ Market State
                                       └ **Alpha Factory** ─┬ LLM Agent
                                                            └ GP/ML
                                            → Alpha Pool → Devil/Critic → OOS验证 → Alpha Bank

## 这个模块解决的问题

此前「产生候选 → 评估 → 登记」这三步散在 `scripts/oneoff/alpha_factory_v1.py` 里,
每次加一个候选来源就复制一遍脚本, **而 Bonferroni 分母(测了几个)是靠人记住的** ——
分母说不清, 门禁就形同虚设。本模块把这三步收成一个**带预注册闸门的程序对象**:

    f = Factory(prereg=PreRegistration(...))     # ① 先声明前提出去
    f.add(ZooProposer(zoos.all_candidates()))
    f.add(GPProposer(budget=GPBudget(60, 15)))   # ② 声明搜索空间大小
    f.declare()                                  # ③ **在任何求值之前**冻结分母
    f.run()                                      # ④ 才允许碰数据

`declare()` 之前不许 `run()`, `run()` 之后不许再加 proposer —— 这两条是断言, 不是文档。

## 三种提议器(Proposer)

| 提议器 | 语义 | 是否异构口粮 |
|---|---|---|
| `ZooProposer` | 公开表达式池(qlib158 标尺)或 `flow_factors` 已声明因子 | qlib158 否 / flow 是 |
| `GPProposer` | **符号回归**在 `expr.py` 的 DSL 上搜索新表达式 | 否(量价派生), 但可能找到 31 因子没覆盖的**组合** |
| `LLMProposer` | 读一份**离线**产出的表达式 JSON(本模块不调用任何在线 LLM) | 取决于人喂什么 |

## GP 为什么不自带「随便搜」的接口

符号回归天生是**过拟合机器**: 它会自动搜索的候选数 N 巨大且**事先不可知** ⇒
Bonferroni 分母不可知 ⇒ 显著性检验退化成装饰。故本模块把预算做成**强制参数**
(`GPBudget.population/gen`), 分母 = `population × generations` 在 `declare()` 时冻结。
再加三条硬闸:

1. **只在死区搜索**(`DEAD`), 活跃区**不参与适应度** —— 否则就是在 24 个月上挖噪声
2. **适应度 = 门禁的同一个量**(死区 head 超额), 不另立指标 —— 不许"搜的时候用 A, 判的时候用 B"
3. **多目标(NSGA-II)**: 同时最小化 `-head` 与**表达式复杂度**, 用 Pareto 前沿交付 ——
   单一适应度下 GP 必然膨胀成不可解释的巨树

## 交付物与「门禁通过 ≠ 可用」

`run()` 把**每一个求过值的候选**(不是只有通过的那些)写进 `alpha_bank`,
状态 CANDIDATE / POOLED。**PASS = POOLED = 有资格进组合层回测, 不等于可用** ——
本项目已有 4 个「KGD/门禁通过却对策略无用」的反例(`die_flow`/`head_vt`/`h10`/
`margin_net_share`)。判决只能是**组合层两轴**(`exp_die_stage_b.py` 的预注册)。

## 与其他模块的关系

- **上游**: `app.pit.PITEngine` 提供可交易域(本模块用 `Factory._tradable` 取一次
  `tradable_matrix`, 门禁与 GP 适应度**都在这个域上读数**; 2026-09-12 审查 P0-05
  之前这里只是句文档承诺 —— 全库无一处真的调用 PIT 裁域)
- **门禁**: `app.alpha.gate`(五条判据, 一字不改)
- **登记**: `app.alpha.bank`(状态机; 本模块**只写不删**)
- **下游**: `app.alpha.pipeline`(Pool → Critic → OOS → Bank 的自动流转)
"""
from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Callable, Sequence

import numpy as np
import pandas as pd

from app import db
from app.alpha import bank, critic, expr as E, gate
from app.factors import factor_engine as fe

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 预注册: 跑之前先写死的东西
# ---------------------------------------------------------------------------
SCHEMA_VERSION = "factory_v1"


@dataclass(frozen=True)
class PreRegistration:
    """**在任何求值之前**必须写下来的前提出去。`Factory.run()` 会把它整份写进每条
    登记的 `note`, 使「事后改判据」在证据里留下痕迹。

    字段刻意做成不可变(frozen) —— 跑完再改要新建对象, 而新建对象会在 `declare()`
    的断言处被发现(`_prereg_hash` 不一致)。
    """
    intent: str                       # 一句话: 这一轮到底想证明什么
    eval_since: str = "2019-01-01"    # 与冻结的阶段 0c 门禁同口径
    warmup_days: int = 400            # 最长滚动窗 60 交易日, 400 自然日远超所需
    dead: tuple = gate.DEAD
    active: tuple = gate.ACTIVE
    version: str = SCHEMA_VERSION

    def to_note(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, sort_keys=True)


@dataclass(frozen=True)
class GPBudget:
    """符号回归的**冻结预算**。`population × generations` 就是这一轮的分母。

    只增不减地计入分母(缓存命中的重复个体**仍然计入**) —— 刻意保守:
    分母只能被高估, 不能被低估, 否则 Bonferroni 校正偏松。
    """
    population: int = 60
    generations: int = 15
    crossover_p: float = 0.6
    mutation_p: float = 0.3
    seed: int = 20260911
    max_depth: int = 3                # 树深上限(与复杂度的二次判据共同压制膨胀)

    @property
    def n_declared(self) -> int:
        return int(self.population) * int(self.generations)


# ---------------------------------------------------------------------------
# 候选
# ---------------------------------------------------------------------------
@dataclass
class Candidate:
    name: str
    expr: str | None = None          # 表达式候选(走 Engine 求值)
    panel: pd.DataFrame | None = None  # 预算好的面板候选(如 flow_factors 的产出)
    meta: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.expr is None and self.panel is None:
            raise ValueError(f"候选 {self.name} 既无表达式也无面板")


# ---------------------------------------------------------------------------
# 提议器
# ---------------------------------------------------------------------------
class Proposer:
    """候选提议器契约。

    `propose()` 返回候选列表; `n_evaluated` 是**实际求过值的候选数** —— 它是
    Bonferroni 分母的**下界**, 调用方取 `max(n_declared, n_evaluated)` 以保守。
    """

    label = "proposer"

    def __init__(self):
        self.n_evaluated = 0

    def propose(self) -> list[Candidate]:     # pragma: no cover - 接口
        raise NotImplementedError

    def declared(self) -> int:
        """**求值之前**就能给出的分母(搜索空间大小)。无法预知的提议器返回 0。"""
        return 0

    def bind_factory(self, factory) -> None:
        """可选钩子：提议器若需要**工厂级服务**（PIT 可交易域、共享缓存），在这里取。

        默认无操作。加它是因为一个真实缺陷（2026-09-13 独立审查 + 本人复核）：
        `GPProposer._fitness_mask()` 直接去摸 `self._mask_cache` / `self._tradable` ——
        那两个名字**只存在于 `Factory` 上**，而 GP 继承的是 `Proposer` ⇒ 每次适应度求值都抛
        `AttributeError`，被 `_fitness` 的 `except Exception` 吞成"最差适应度" ⇒
        **整个符号回归退化成随机搜索且日志无痕**（DB 实证：`alpha_bank` 171 行里 0 行来自 GP）。
        与其让下游去猜工厂的内部名，不如让工厂**显式把服务交出去**。
        """
        return None


class ZooProposer(Proposer):
    """公开表达式池。`zoos.qlib158()` 是**标尺**(与存量 31 因子同信息源);
    `flow_factors` 是**异构口粮**(资金类, 已在模块里声明过, 原样不做变换)。

    两类候选: `items`(表达式, 走 `Engine` 求值)与 `panels`(已预算好的面板,
    如资金类因子)。同一提议器里两者按名字合并。
    """

    def __init__(self, items: Sequence[tuple] = None, label: str = "zoo",
                 panels: dict = None, panel_meta: dict = None):
        super().__init__()
        self.label = label
        self.items = list(items) if items is not None else []
        self.panels = dict(panels) if panels else {}
        self.panel_meta = dict(panel_meta) if panel_meta else {}
        dup = {n for n, _, _ in self.items} & set(self.panels)
        if dup:
            raise ValueError(f"同名候选既是表达式又是面板: {sorted(dup)}")

    def declared(self) -> int:
        return len(self.items) + len(self.panels)

    def propose(self) -> list[Candidate]:
        out = [Candidate(name=n, expr=e, meta=dict(meta or {}))
               for n, e, meta in self.items]
        out += [Candidate(name=n, panel=p, meta={
            "info_source": "flow", "source": "app/factors/flow_factors.py",
            "name": n, **self.panel_meta.get(n, {})})
            for n, p in sorted(self.panels.items())]
        self.n_evaluated = len(out)
        return out

    @staticmethod
    def flow_proposer(m: dict, dates, label: str = "flow") -> "ZooProposer":
        """资金类提议器: `flow_factors` 已声明因子**原样**取用(不顺手加变换 ——
        一旦"顺手再加个 20 日均值", 就是在扩张搜索空间, 分母再也说不清)。

        `hk_*` 按方案 §3 不变量 #6(2024-08 覆盖度断崖)在这里**硬排除** ——
        写在代码里, 不是写在注释里等着被忘掉。
        """
        from app.factors import flow_factors as ff
        panels, meta = {}, {}
        for nm, wide in sorted(ff.compute_flow_factors(
                m, dates, include_research=False).items()):
            if nm.startswith("hk_"):
                log.warning("跳过 %s: hk_* 覆盖度断崖(不变量 #6), 不得进生产/门禁", nm)
                continue
            panels[nm] = wide
            meta[nm] = {"note": "资金类因子(原样, 无变换; hk_* 按不变量#6 排除)"}
        return ZooProposer(label=label, panels=panels, panel_meta=meta)


class LLMProposer(Proposer):
    """离线 LLM 提议器的**装载器** —— 它**不调用任何在线 LLM**。

    为什么做成装载器而不是调用器(方案 §6, 硬性不变量 #4):
    ① 全库无新闻/公告/财报文本(`news`/`major_news`/`anns_d` 无权限);
    ② 更根本 —— 今天的 LLM 参数里含 2020 年后的一切, 让它给 2020 年打分 =
       **必然虚假但极好看的回测**。唯一允许的形态是: LLM **离线**在纯数值 PIT 面板上
       提出候选表达式, 人审过后落成 JSON, 由本类装载, 产出照样走 `gate`。

    JSON 格式: `[{"name": "...", "expr": "Mean($close, 20)", "why": "..."}, ...]`
    """

    def __init__(self, path: str | Path, label: str = "llm"):
        super().__init__()
        self.path = Path(path)
        self.label = label

    def _load(self) -> list[dict]:
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            raise ValueError(f"{self.path} 顶层必须是数组")
        out = []
        for i, r in enumerate(raw):
            if not isinstance(r, dict) or "name" not in r or "expr" not in r:
                raise ValueError(f"{self.path} 第 {i} 条缺 name/expr")
            out.append(r)
        return out

    def declared(self) -> int:
        return len(self._load())

    def propose(self) -> list[Candidate]:
        rows = self._load()
        out = []
        for r in rows:
            # 静态守卫: 非法/前视表达式在**装载时**就报错, 不拖到求值烧预算
            _static_check(r["expr"])
            out.append(Candidate(name=r["name"], expr=r["expr"], meta={
                "info_source": "llm_offline", "source": str(self.path),
                "note": r.get("why", "")}))
        self.n_evaluated = len(out)
        return out


# ---------------------------------------------------------------------------
# GP 符号回归(deap, NSGA-II)
# ---------------------------------------------------------------------------
class GPProposer(Proposer):
    """在 `expr.py` 的 DSL 上做符号回归。

    **唯一的求值域是死区**(2020-09~2023-12)。活跃区在这个类里**没有任何入口** ——
    不是"记得别看", 是构造函数只接收死区日期。活跃区一次性开封发生在 `Factory.run()`
    的最后一步(对 Pareto 前沿成员算完整读数), 且**不影响任何成员的入选与否** ——
    前沿在选择阶段就冻结了。

    适应度 = `gate` 的同一个量(死区 head 超额), 多目标第二维 = 表达式复杂度。
    """

    label = "gp"

    def __init__(self, m: dict, dead_dates: pd.DatetimeIndex, budget: GPBudget = None,
                 ind=None, universe=None, mask_fn=None,
                 fitness_mode: str = "head", ortho_controls: dict = None):
        super().__init__()
        self.budget = budget or GPBudget()
        self.dead_dates = pd.DatetimeIndex(dead_dates)
        self._ind = ind
        self._universe = universe
        self._n_eval = 0
        self._cache: dict = {}
        self._n_errors = 0
        self._panel = None
        self._eng = None
        self._fwd = None
        self._zones = None
        self._m = m
        # 可交易域来源：由 Factory.add() 绑定（见 bind_factory）。**为 None 时不许静默降级**
        self._mask_fn = mask_fn
        self._mask_gp = None
        # H0 正交增量适应度(plans/h0-ortho-fitness-prereg-20261003.md): 缺省 "head"
        # = 现行路径一字不动(位级审计锚点); "ortho" 才在行业中性化后插入截面秩回归
        # 残差化。控制面板由运行脚本装配传入, 本模块不自行读库。
        if fitness_mode not in ("head", "ortho"):
            raise ValueError(f"unknown fitness_mode: {fitness_mode}")
        self._fitness_mode = fitness_mode
        self._ortho_controls = ortho_controls

    # -- 求值域: 只到死区为止 ------------------------------------------------
    def _prepare(self):
        """切片行情面板 **到死区最后一天为止** ⇒ 活跃区物理上不可达。

        (这是比"注释里写别偷看"更强的保证: 适应度函数拿不到 2024 年之后的任何一格。)
        """
        if self._panel is not None:
            return
        cut = pd.Timestamp(gate.DEAD[1])
        warm = pd.Timestamp(gate.DEAD[0]) - pd.Timedelta(days=400)   # 滚动窗预热
        m = {k: v.loc[warm:cut] for k, v in self._m.items()
             if isinstance(v, pd.DataFrame)}
        self._m_gp = m          # P0-05: 适应度也要裁 PIT 域, 得留着行情建掩码
        # 2026-09-26(GP 首跑 alpha_factory_gp1 实证; 与 09-13 bind_factory 同族的**第二处**
        # 静默退化): _fitness 的行业中性化用 self._ind, 但构造默认 None 且 Factory.add()
        # 只绑 PIT 域不绑行业 ⇒ 每次适应度抛 AttributeError 被吞成最差适应度(1e9) ⇒
        # 符号回归退化成随机搜索(指纹: gen 行"最好 -head"恒为 -1e9)。惰性加载**同一实现**
        # (fe.industry_map, 与 Factory._prepare 的 eval 路径同源同值)。
        if self._ind is None:
            conn = db.get_conn()
            try:
                self._ind = fe.industry_map(conn)
            finally:
                conn.close()
        self._panel = E.build_panel(m)
        self._eng = E.Engine(self._panel)
        self._fwd = gate.forward_return(m, gate.HORIZON)
        self._dead = self.dead_dates[(self.dead_dates >= pd.Timestamp(gate.DEAD[0]))
                                     & (self.dead_dates <= pd.Timestamp(gate.DEAD[1]))]
        self._zones = gate.zone_of(self._dead)

    def _fitness_mask(self) -> pd.DataFrame:
        """GP 适应度用的 PIT 掩码(死区截面 × 全代码)。惰性算一次、全体复用。

        掩码**只有一个来源**：`Factory._tradable`（经 `Factory.add()` 绑定进来）。
        没绑定就**报错**，不许退回全市场 —— 那正是 P0-05 要修的错（门禁/搜索在买不到的票上优化）。
        """
        if self._mask_fn is None:
            raise RuntimeError(
                "GPProposer 没有可交易域来源：必须经 Factory.add() 装配"
                "（它会把 Factory._tradable 绑进来）。P0-05 要求适应度只在 PIT 域上算。")
        if self._mask_gp is None:
            self._mask_gp = self._mask_fn("gp", self._m_gp, self._dead)
        return self._mask_gp

    def bind_factory(self, factory) -> None:
        """拿到工厂的 PIT 域构造器（单一来源，不另写一份掩码逻辑）。"""
        self._mask_fn = factory._tradable

    # -- 适应度 --------------------------------------------------------------
    def _fitness(self, expr_str: str) -> tuple:
        """→ (head_excess 均值, 复杂度)。**任何异常都返回最差值**, 不跳过 ——
        跳过的个体会被 GP 反复重新生成, 白烧预算。"""
        worst = (1e9, 1e9)
        if expr_str in self._cache:
            return self._cache[expr_str]
        self._n_eval += 1
        try:
            raw = self._eng.eval(expr_str)
            if isinstance(raw, pd.Series):
                raw = raw.to_frame()
            raw = raw.replace([np.inf, -np.inf], np.nan)
            if raw.notna().sum().sum() < 1000:      # 几乎全 NaN ⇒ 无信息
                self._cache[expr_str] = worst
                return worst
            neu = fe.neutralize(raw, self._ind)
            if self._fitness_mode == "ortho":
                neu = _ortho_residual(neu, self._ortho_controls, gate.MIN_NAMES)
            # P0-05: 适应度也要在 PIT 域上算 —— GP 是**搜索**环节, 这里放行等于
            # 整个搜索都在"买不到的票"上做优化, 后面再裁门禁也补不回来。
            # 必须放在 <1000 非 NaN 的初筛**之后**: 那一步判的是因子本身有没有信息,
            # 与可交易域无关, 提前裁会让稀疏但有效的因子被误杀。
            det = gate.evaluate(_masked_panel(neu, self._fitness_mask(), self._dead),
                                self._fwd, self._dead, self._zones)
            if det.empty or len(det) < 8:
                self._cache[expr_str] = worst
                return worst
            head = float(np.nanmean(det["head_excess"]))
            if not math.isfinite(head):
                self._cache[expr_str] = worst
                return worst
        except RuntimeError:
            # **结构性问题不是我算不出来，是接线错了** —— 吞成"最差适应度"会让整个搜索
            # 静默退化成随机（上面那条真实事故就是这么发生的）。所以这里必须炸。
            raise
        except Exception as e:
            self._n_errors += 1
            if self._n_errors <= 3:            # 只报到能看见为止，不刷屏
                log.exception("GP 适应度求值异常(第 %d 次): %s", self._n_errors, e)
            self._cache[expr_str] = worst
            return worst
        out = (-head, float(len(expr_str)))
        self._cache[expr_str] = out
        return out

    def propose(self) -> list[Candidate]:
        import deap.creator as creator
        import deap.gp as gp
        from deap import base, tools

        self._prepare()
        b = self.budget
        pset = _build_pset(gp)
        if not hasattr(creator, "FitnessMin2"):
            creator.create("FitnessMin2", base.Fitness, weights=(-1.0, -1.0))
        if not hasattr(creator, "IndividualGP"):
            creator.create("IndividualGP", gp.PrimitiveTree, fitness=creator.FitnessMin2)

        tb = base.Toolbox()
        # ⚠️ 用自己的 `_gen_tree` 而不是 `gp.genFull` —— 后者对纯终结符类型(窗口/分位)
        # 会去找不存在的原语并崩。理由见 `_gen_tree` 的 docstring。
        tb.register("expr", _gen_tree, pset=pset, min_=1, max_=b.max_depth)
        tb.register("individual", tools.initIterate, creator.IndividualGP, tb.expr)
        tb.register("population", tools.initRepeat, list, tb.individual)

        def evaluate(ind):
            return self._fitness(_tree_to_expr(ind, gp))
        tb.register("evaluate", evaluate)
        tb.register("select", tools.selNSGA2)
        tb.register("mate", gp.cxOnePoint)
        tb.register("expr_mut", _gen_tree, min_=0, max_=2)
        tb.register("mutate", gp.mutUniform, expr=tb.expr_mut, pset=pset)
        tb.register("shrink", gp.mutShrink)
        tb.decorate("mate", gp.staticLimit(key=len, max_value=40))
        tb.decorate("mutate", gp.staticLimit(key=len, max_value=40))

        import random as _rnd                          # deap 全程用全局 random
        _rnd.seed(b.seed)

        pop = tb.population(n=b.population)
        hof = tools.ParetoFront()
        log.info("GP 开始 | 种群 %d × %d 代 = 声明分母 %d | 死区 %d 个截面 | 时间预算未知",
                 b.population, b.generations, b.n_declared, len(self._dead))
        t0 = time.time()
        for ind in pop:
            ind.fitness.values = tb.evaluate(ind)
        # ⚠️ 这一步不可省: `_mating_selection` 要读 `fitness.crowding_dist`, 而它**只**由
        # `selNSGA2` 赋值。少了它, 第一代的交配选择就 `AttributeError` —— 2026-09-11 冒烟
        # 首次暴露(此前单测只测了树生成器与打印器, 没跑过完整的代循环)。
        pop = tb.select(pop, len(pop))

        for gen in range(b.generations):
            offspring = _mating_selection(pop, len(pop), _rnd)
            offspring = [tb.clone(ind) for ind in offspring]
            for c1, c2 in zip(offspring[::2], offspring[1::2]):
                if _rnd.random() <= b.crossover_p:
                    tb.mate(c1, c2)
                    del c1.fitness.values, c2.fitness.values
            for mut in offspring:
                if _rnd.random() <= b.mutation_p:
                    tb.mutate(mut)
                    del mut.fitness.values
            invalid = [i for i in offspring if not i.fitness.valid]
            for ind in invalid:
                ind.fitness.values = tb.evaluate(ind)
            pop = tb.select(pop + offspring, b.population)
            hof.update(pop)
            best = min(pop, key=lambda i: i.fitness.values[0])
            log.info("  gen %2d/%d | 已求值 %d | 前沿 %d | 最好 -head %.4f | %.0fs",
                     gen + 1, b.generations, self._n_eval, len(hof),
                     -best.fitness.values[0], time.time() - t0)

        self.n_evaluated = self._n_eval
        fronts = sorted(hof, key=lambda i: i.fitness.values[0])
        out, seen = [], set()
        for rank, ind in enumerate(fronts):
            s = _tree_to_expr(ind, gp)
            if s in seen:
                continue
            seen.add(s)
            try:
                _static_check(s)                      # 静态守卫: 本模块产出的也必须合法
            except E.ExprError:
                continue
            out.append(Candidate(
                name="gp_%03d" % rank, expr=s, meta={
                    "info_source": "price_volume_gp",
                    "source": "GPProposer(NSGA-II)",
                    "note": "死区适应度 %.4f / 复杂度 %d / Pareto 前沿第 %d" % (
                        -ind.fitness.values[0], ind.fitness.values[1], rank)}))
        log.info("GP 结束 | 求值 %d 个(声明分母 %d) | 前沿交付 %d 条 | %.0fs",
                 self._n_eval, b.n_declared, len(out), time.time() - t0)
        return out

    def declared(self) -> int:
        return self.budget.n_declared


class SeriesT:
    """类型标记: 面板(日期 × 股票)"""


class WindowT:
    """类型标记: 滚动窗口(交易日), 正整数"""


class QuantileT:
    """类型标记: 分位数, 取值 (0, 1)"""


# 搜索空间**显式排除**的 DSL 算子(其余全部纳入)。写在代码里而不是注释里,
# 因为它是 Bonferroni 分母的一部分 —— 分母只能被高估, 不能被"记错了"。
_CMP_WHY = ("中缀比较符, 无法表达成 Name(args) 形式(树的打印器只产出函数调用形), "
            "GP 里等价的语义由 Greater/Less 提供")
GP_EXCLUDED_OPS = {
    "Pow": "极易溢出成 inf(底数>1 且指数大), 适应度会被 NaN 淹掉, 白白烧预算",
    "GreaterEqual": "逐元素 max 的别名, 与 Greater 完全同义, 徒增搜索宽度不加信息",
    "LessEqual": "逐元素 min 的别名, 与 Less 完全同义, 徒增搜索宽度不加信息",
    ">": _CMP_WHY, "<": _CMP_WHY, ">=": _CMP_WHY, "<=": _CMP_WHY,
    "==": _CMP_WHY, "!=": _CMP_WHY,
}


def _build_pset(gp):
    """DSL 的**类型化**原语集。三型: `SeriesT` / `WindowT` / `QuantileT`。

    搜索空间在这里被**显式枚举**: `expr.py` 的算子表减去 `GP_EXCLUDED_OPS`。
    类型类提到模块级(不是函数内)是为了让测试与下游模块能**核对**这份枚举 ——
    搜索空间一旦"偷偷加料", 在 `declare()` 时冻结的分母立刻失真。
    """
    S, W, Q = SeriesT, WindowT, QuantileT
    ps = gp.PrimitiveSetTyped("MAIN", [], S)
    for v in ("close", "open", "high", "low", "volume", "amount", "vwap",
              "turnover", "total_mv", "circ_mv", "pb", "pe_ttm", "pre_close"):
        ps.addTerminal("$" + v, S)
    for w in (3, 5, 10, 20, 30, 60):
        ps.addTerminal(w, W)
    for q in (0.1, 0.25, 0.5, 0.75, 0.9):
        ps.addTerminal(q, Q)

    def add(name, arity_in, ret=S):
        fn = getattr(E, "op_" + name, None)
        if fn is None:                    # 求值实现在 Engine 内, 这里只需要**签名**
            fn = _stub(name)
        ps.addPrimitive(fn, arity_in, ret, name=name)

    for nm in E.ELEM_UNARY - set(GP_EXCLUDED_OPS):
        add(nm, [S])
    for nm in E.ELEM_BINARY - set(GP_EXCLUDED_OPS):
        add(nm, [S, S])
    for nm in E.ROLLING_OPS - set(GP_EXCLUDED_OPS):
        if nm == "Quantile":
            add(nm, [S, W, Q])
        else:
            add(nm, [S, W])
    for nm in E.PAIR_ROLLING_OPS:
        add(nm, [S, S, W])
    return ps


def _static_check(expr: str) -> None:
    """装载时的**静态**合法性守卫, 不合法就抛 `E.ExprError`。

    ⚠️ 这里必须显式查 `ref_windows`, 不能只 `E.parse` —— 本项目的静态前视守卫
    实际装在**求值路径**上(`expr.py:367-372` 的 `Ref` 分支), 而不是解析器里。
    只 parse 的话 `Ref($close, -1)` 会一路通过装载、进工厂、再在求值时炸,
    白白烧掉一次预算。`ref_windows()` 是引擎为静态检查准备好的钩子, 用它。
    """
    ast = E.parse(expr)
    try:
        wins = E.ref_windows(ast)
    except Exception as exc:
        raise E.ExprError(f"窗口参数不是字面量常量, 无法静态检查: {expr[:80]!r}") from exc
    neg = [w for w in wins if w < 0]
    if neg:
        raise E.ExprError(f"窗口为负 {neg} —— 那是前视, 本引擎禁止 ({expr[:80]!r})")


def _gen_tree(pset, min_=1, max_=3, type_=None):
    """**类型驱动的树生成器** —— 替代 `gp.genFull`。

    为什么不能直接用 `gp.genFull`(2026-09-11 冒烟实测崩掉):
    deap 的 `generate()` 在 `depth < height` 时**无条件**去 `pset.primitives[type_]`
    里取原语。而本 DSL 里 `WindowT`/`QuantileT` 是**纯终结符类型**(窗口只能是字面量、
    分位只能是字面量, 引擎的 `_win()` 也要求"正整数常量"), 没有也不该有原语 ⇒
    生成 `Mean($close, <?>)` 的孩子时当场 `IndexError: ... of type WindowT, but there
    is none available`。

    本生成器把"该类型有没有原语"变成分支条件: 没有原语就**只能**取终结符。
    语义与 `genFull` 一致(够深就落地, 不够深就按概率生长), 只是不再假设每个类型都可派生。

    签名 `(pset, min_, max_, type_=None)` 与 deap 兼容 ⇒ 可直接喂给
    `tools.register("expr", ...)` 与 `gp.mutUniform(expr=...)`。
    """
    import random as _r

    import deap.gp as gp

    t0 = type_ or pset.ret

    def build(t, depth):
        prims, terms = pset.primitives[t], pset.terminals[t]
        grow = bool(prims) and (depth < min_ or
                                (depth < max_ and _r.random() < 0.7))
        if grow:
            p = _r.choice(prims)
            out = [p]
            for at in p.args:
                out.extend(build(at, depth + 1))
            return out
        if not terms:                      # 既无原语又无终结符 = 类型定义漏了
            raise ValueError(f"类型 {t!r} 既无原语也无终结符, 搜索空间不完整")
        return [_r.choice(terms)]

    return gp.PrimitiveTree(build(t0, 0))


def _stub(name):                          # pragma: no cover - 只用于满足 deap 的签名需求
    def f(*a):
        raise NotImplementedError(name)
    f.__name__ = name
    return f


def _tree_to_expr(tree, gp) -> str:
    """deap PrimitiveTree → DSL 表达式串。

    PrimitiveTree 按**深度优先前缀序**存储 ⇒ 用游标递归展开即可还原。
    """
    nodes = list(tree)
    pos = [0]

    def rec():
        node = nodes[pos[0]]
        pos[0] += 1
        if not isinstance(node, gp.Primitive):
            return str(node.value)
        args = [rec() for _ in range(node.arity)]
        return "%s(%s)" % (node.name, ", ".join(args))

    return rec()


def _tourn(a, b, rnd):
    """Deb NSGA-II 的二元锦标赛: 先比非支配等级, 同级比拥挤距离, 全平才抛硬币。"""
    fa, fb = a.fitness, b.fitness
    if fa.dominates(fb):
        return a
    if fb.dominates(fa):
        return b
    da, db = fa.crowding_dist, fb.crowding_dist
    if da != db:
        return a if da > db else b
    return a if rnd.random() <= 0.5 else b


def _mating_selection(pop, k, rnd):
    """交配选择: 洗牌后相邻两两配对, 每对产出两个子代(互为对方的锦标赛对手)。

    **不用 `deap.tools.selTournamentDCD`**, 有两个理由:

    1. 它在 `k == len(pop)` 时要求种群大小能被 4 整除, 于是 `--gp 10 5` 这种完全合法的
       预算会在**开跑之后**当场 `ValueError`(白烧掉前面的求值)。这里实现同一条 Deb 规则
       但不设整除限制。
    2. 本函数把"必须先赋拥挤距离"写进了**前置条件**并配了回归测试 —— 原来的写法漏了
       这一步, 在 2026-09-11 冒烟时才以 `AttributeError` 暴露。

    配对的理由与 deap 一致: 逐个独立抽样会抽到"自己和自己杂交", 而 `cxOnePoint` 在同源
    树上等于白跑一次 —— 配对保证每个个体至多参与一次锦标赛。

    **前置条件**: `pop` 必须已由 `selNSGA2` 赋过 `crowding_dist`
    (见 `propose()` 里 `pop = tb.select(pop, len(pop))` 那一步)。
    """
    order = list(range(len(pop)))
    rnd.shuffle(order)
    kids = []
    for t in range(0, len(order) - 1, 2):
        i, j = order[t], order[t + 1]
        kids.append(_tourn(pop[i], pop[j], rnd))
        kids.append(_tourn(pop[j], pop[i], rnd))
    if not kids:
        return [pop[0]] * k
    if len(kids) < k:                  # 种群为奇数时最后一个个体本轮不参与, 补齐
        kids = (kids * (k // len(kids) + 1))[:k]
    return kids[:k]


def _ortho_residual(panel: pd.DataFrame, controls: dict, min_names: int) -> pd.DataFrame:
    """H0 正交增量适应度的残差化(plans/h0-ortho-fitness-prereg-20261003.md §一, 冻结)。

    逐截面: y = rank_pct(候选), X = [1] + [rank_pct(各控制)]; 控制与候选重叠
    < min_names 的该控制当期剔除; 无控制存留的日期整行丢弃; lstsq 残差(秩残差)。
    controls 为空属**接线错误**(会把 ortho 搜索静默退化成 head 搜索) —— RuntimeError
    走 _fitness 的既定"必炸"语义, 不吞。纯函数, 无副作用。
    """
    if not controls:
        raise RuntimeError("ortho 模式需要 ortho_controls 非空(由运行脚本装配传入)")
    ctrl_dfs = list(controls.values())
    out = {}
    for d, y_row in panel.iterrows():
        y = y_row.astype(float)
        cols = []
        for c in ctrl_dfs:
            if d not in c.index:
                continue
            x = c.loc[d].astype(float)
            if len(y.dropna().index.intersection(x.dropna().index)) < min_names:
                continue
            cols.append(x)
        if not cols:
            continue  # 该日期无控制 ⇒ 整行丢弃
        valid = y.dropna().index
        for x in cols:
            valid = valid.intersection(x.dropna().index)
        if len(valid) < min_names:
            continue
        yv = y.loc[valid].rank(pct=True).to_numpy(dtype=float)
        Xv = np.column_stack([np.ones(len(valid))] + [
            x.loc[valid].rank(pct=True).to_numpy(dtype=float) for x in cols])
        beta, *_ = np.linalg.lstsq(Xv, yv, rcond=None)
        out[d] = pd.Series(yv - Xv @ beta, index=valid)
    if not out:
        return pd.DataFrame(columns=panel.columns)
    return pd.DataFrame.from_dict(out, orient="index").reindex(columns=panel.columns)


def _masked_panel(panel: pd.DataFrame, mask: pd.DataFrame,
                  dates: pd.DatetimeIndex) -> pd.DataFrame:
    """把面板裁到 PIT 可交易域(2026-09-12 审查 P0-05)。

    先对齐到 `dates` 再 `where`: 门禁只读 `dates` 这几行, 而 `mask` 与 `panel` 的
    index 粒度不同(月末 vs 日频)—— 直接 where 会让 pandas 做外连接对齐, 结果虽然
    一样(多余行门禁不读), 但白白多算一大片, 且依赖"where 把缺失的 cond 当 False"
    这条不显眼的语义。
    """
    m = mask.reindex(index=dates, columns=panel.columns).fillna(False)
    return panel.reindex(index=dates).where(m)


def tradable_mask(m: dict, dates) -> pd.DataFrame:
    """PIT 可交易域（`dates × 全代码`）—— **唯一实现**。

    2026-09-13（独立审查 §4）：门禁、GP 适应度、留痕（critic）三处都要"生产买得到的票"
    这个域，此前只有工厂内部 `Factory._tradable` 会造它 ⇒ 留痕那条路只能读**未裁域**的
    面板，导致"门禁判决"与"留痕复查"不是同一个域。现在抽到模块级：谁要域都调它，
    `Factory._tradable` 只在外面加缓存与日志。
    """
    from app.pit import PITEngine
    return PITEngine(m=m).tradable_matrix(dates)


# ---------------------------------------------------------------------------
# 工厂
# ---------------------------------------------------------------------------
class Factory:
    """产生 → 门禁 → 登记。**预注册闸门由断言强制**, 不靠自觉。

    用法:
        f = Factory(PreRegistration(intent="..."))
        f.add(ZooProposer(zoos.all_candidates(), label="qlib158"))
        f.declare()          # 冻结分母
        f.run()              # 求值 + 写 alpha_bank
    """

    def __init__(self, prereg: PreRegistration, market_since: str = None,
                 conn=None, on_result: Callable[[dict], None] = None,
                 allow_rerun: bool = False):
        self.prereg = prereg
        self.market_since = market_since
        self._conn = conn
        self._proposers: list[Proposer] = []
        self._declared = False
        self._ran = False
        self._denominator: int | None = None
        # 门禁 v2 ③（2026-09-14）：撞车硬闸。`allow_rerun=True` 是**唯一**逃逸阀，
        # 只用于"复现性核验"（同 id 同公式再登记一次），默认关。
        self.allow_rerun = bool(allow_rerun)
        self.duplicates: list[dict] = []
        self.on_result = on_result
        self.results: list[dict] = []
        self._m = None
        self._dates_all = None
        # PIT 可交易掩码缓存(P0-05): {槽名: {"key": …, "mat": bool DataFrame}}
        # 缓存的理由: 掩码只取决于 (日, 股), 与因子无关 —— 不能每个候选重算一遍
        self._mask_cache: dict = {}

    # -- 预注册闸门 ---------------------------------------------------------
    def add(self, proposer: Proposer) -> "Factory":
        assert not self._declared, "declare() 之后不许再加 proposer —— 分母已被冻结"
        self._proposers.append(proposer)
        # 把工厂级服务(尤其 PIT 可交易域)显式交给提议器 —— 见 Proposer.bind_factory 的说明：
        # GP 原来靠摸 self._tradable 取域，而那个名字在 Factory 上 ⇒ 静默退化成随机搜索。
        proposer.bind_factory(self)
        return self

    def declare(self) -> int:
        """**在任何求值之前**冻结 Bonferroni 分母。分母 = 声明值的**保守和**:
        GP 用 `population × generations`(不是实际求值数), 缓存命中的重复个体照样计入。
        """
        assert self._proposers, "没有 proposer 可声明"
        self._denominator = int(sum(p.declared() for p in self._proposers))
        assert self._denominator > 0, (
            "声明分母为 0 —— 有 proposer 无法预先给出搜索空间大小。"
            "把它改成能声明规模的提议器(见 GPProposer 的预算设计理由)")
        self._declared = True
        log.info("预注册完成 | 分母冻结 = %d | 提议器 %s", self._denominator,
                 {p.label: p.declared() for p in self._proposers})
        return self._denominator

    # -- 主流程 -------------------------------------------------------------
    def run(self, eval_since: str = None, save: bool = True) -> pd.DataFrame:
        assert self._declared, "必须先 declare() —— 不允许先跑再看测了几个"
        assert not self._ran, "同一次预注册只能跑一次; 要复跑请新建 Factory"
        self._ran = True
        eval_since = eval_since or self.prereg.eval_since
        n_tests = self._denominator

        m, dates_all, md, zones, fwd, ind = self._prepare(eval_since)
        panel = E.build_panel(m)
        eng = E.Engine(panel)
        log.info("=" * 88)
        log.info("Alpha Factory | INTENT: %s", self.prereg.intent)
        log.info("候选来源 %s | **Bonferroni 分母 = %d(预注册冻结)** | 评估窗 %s 起",
                 [p.label for p in self._proposers], n_tests, eval_since)
        log.info("死区 %d 期 / 活跃区 %d 期", int((zones == "DEAD").sum()),
                 int((zones == "ACTIVE").sum()))
        log.info("=" * 88)

        cands: list[Candidate] = []
        for p in self._proposers:
            t0 = time.time()
            got = p.propose()
            cands.extend(got)
            log.info("提议器 %-8s 产出 %d 条 (%.0fs)", p.label, len(got), time.time() - t0)

        # 2026-09-13（独立审查 #4）：`Proposer.n_evaluated` 的 docstring 写着"调用方取
        # max(n_declared, n_evaluated) 以保守"，但这里原来直接用冻结的 declared —— 承诺没兑现。
        # 若某提议器**实际求值数超过**它自己声明的搜索空间（GP 重提议/缓存未命中重算等），
        # 只按 declared 算 Bonferroni 就是**偏松**（更容易 PASS）。取 max 只会更严, 不可能
        # 被用来制造显著性, 所以这里是安全的保守修正；同时打日志, 让"超预算"这件事可见。
        n_eval_total = sum(int(getattr(p, "n_evaluated", 0) or 0) for p in self._proposers)
        if n_eval_total > n_tests:
            log.warning("实际求值 %d > 预注册分母 %d ⇒ Bonferroni 分母取 max(=实际值), "
                        "本批 p 值比预注册更严；同时说明有提议器超过了自己的声明预算，"
                        "请核对 proposer.declared() 的口径", n_eval_total, n_tests)
            n_tests = n_eval_total

        # 门禁 v2 ②（2026-09-14，判据见 KB 14 号 三补十四）：Bonferroni 分母 =
        # **累计试验数 + 本批声明规模**，不再只看本批。理由：多重比较的分母是"整个研究程序
        # 试过多少次"，而 alpha_bank 里已经躺着 171 条（见 F48：卡点是 N、不是样本长度）。
        # 单调 ⇒ 同样的读数在更晚的批次里更难通过；**不回算**存量行的判决。
        n_batch = int(n_tests)
        n_prior = bank.cumulative_tests()
        n_tests = max(n_batch, n_prior + n_batch)
        log.info("门禁 v2 ② 分母 | 本批 %d + 累计 %d ⇒ **N_used = %d**（跑之前读登记簿）",
                 n_batch, n_prior, n_tests)

        records = self._evaluate(cands, eng, m, dates_all, md, zones, fwd, ind,
                                 n_tests, eval_since, save,
                                 n_tests_family=n_batch, n_tests_prior=n_prior)
        return pd.DataFrame(records)

    # -- 行情: 幂等加载 -----------------------------------------------------
    def market(self):
        """`(m, dates_all, md_all)` —— **幂等**, 供需要行情才能建候选的提议器
        (flow / GP)在 `declare()` 之前调用。它不是"求值", 故不违反预注册闸门。

        起点 = `eval_since − warmup`: `data_start` 生产已设为全历史, 这里只需覆盖
        评估窗 + 滚动窗预热。
        """
        if self._m is None:
            warm = (pd.Timestamp(self.prereg.eval_since)
                    - pd.Timedelta(days=self.prereg.warmup_days))
            # ⚠️ `market_since` 此前是个**死参数**: `__init__` 收下、这里从不读 ⇒ CLI 传什么
            # 都静默无效。现已生效, 但把语义写死为"**只能把起点往前推**"(加载更多数据):
            # 往后推 = 悄悄缩掉滚动窗预热 ⇒ 同一份预注册在两次运行里看到不同面板, 正是本项目
            # 「含义静默改变」那一类陷阱。真要缩小范围请改 `eval_since`(那是预注册字段)。
            if self.market_since:
                ms = pd.Timestamp(self.market_since)
                if ms > warm:
                    raise ValueError(
                        "market_since=%s 晚于 eval_since−warmup=%s ⇒ 会缩掉滚动窗预热。"
                        "要缩小评估范围请改 PreRegistration.eval_since(预注册字段), "
                        "不要用 market_since 绕过去。" % (ms.date(), warm.date()))
                warm = ms
            t0 = time.time()
            # 先打日志再加载: 全市场面板要几分钟到几十分钟, 没有这一行就是全程静默。
            log.info("加载行情面板 since=%s (eval_since %s − %d 天预热) …",
                     warm.strftime("%Y%m%d"), self.prereg.eval_since,
                     self.prereg.warmup_days)
            self._m = fe.load_market(since=warm.strftime("%Y%m%d"))
            self._dates_all = pd.to_datetime(self._m["close"].index)
            log.info("行情面板 %d 天 × %d 只 (%.0fs)", self._m["close"].shape[0],
                     self._m["close"].shape[1], time.time() - t0)
        md_all = fe.month_end_dates(self._dates_all)
        return self._m, self._dates_all, md_all

    # -- 内部 ---------------------------------------------------------------
    def _tradable(self, tag: str, m, dates) -> pd.DataFrame:
        """`dates × 全代码` 的 PIT 可交易掩码(bool)。tag 只是缓存槽名(eval / gp)。

        2026-09-12 审查 P0-05: 门禁原先在**全市场**上算 head_excess —— 而策略买不到
        一半的票(次新/ST/北交所/微盘/低流动性), 于是门禁 PASS 名单与生产 universe
        不是一回事(旧基线 28.78pp 那个错误的同类)。可交易域只取决于 (日, 股), 与因子
        无关 ⇒ 每个批次只算一次, 所有候选/GP 个体复用。

        用**评估窗自己的行情面板**建 PITEngine: 六层里有两层是截面分位(市值)与
        滚动 20 日均额, 拿别的窗口的数据算会得到不同的域。
        """
        slot = self._mask_cache.setdefault(tag, {})
        key = (id(m), len(dates), pd.Timestamp(dates[0]) if len(dates) else None)
        if slot.get("key") != key:
            mat = tradable_mask(m, dates)          # 单一实现，见模块级 tradable_mask
            slot["key"] = key
            slot["mat"] = mat
            n_ok, tot = int(mat.to_numpy().sum()), int(mat.size)
            log.info("[%s] PIT 可交易域: %d/%d 格可交易 (%.1f%%) —— 门禁只在这个域上读数",
                     tag, n_ok, tot, 100.0 * n_ok / max(tot, 1))
        return slot["mat"]

    def _prepare(self, eval_since):
        m, dates_all, md_all = self.market()
        md = md_all[md_all >= pd.Timestamp(eval_since)]
        zones = gate.zone_of(md)
        fwd = gate.forward_return(m, gate.HORIZON)
        own = self._conn is None
        conn = self._conn or db.get_conn()
        try:
            ind = fe.industry_map(conn)
        finally:
            if own:
                conn.close()
        log.info("月末截面 %d (%s ~ %s) | 死区 %d / 活跃区 %d", len(md),
                 md[0].date() if len(md) else "-", md[-1].date() if len(md) else "-",
                 int((zones == "DEAD").sum()), int((zones == "ACTIVE").sum()))
        return m, dates_all, md, zones, fwd, ind

    def _evaluate(self, cands, eng, m, dates_all, md, zones, fwd, ind,
                  n_tests, eval_since, save,
                  n_tests_family: int = None, n_tests_prior: int = None):
        records = []
        meta_note = self.prereg.to_note()
        t0 = time.time()
        # P0-05: 门禁的读数域 = 生产买得到的票。掩码与因子无关 ⇒ 整批候选共用一份
        tradable = self._tradable("eval", m, md)
        for i, c in enumerate(cands, 1):
            t1 = time.time()
            mid = c.meta.get("name", c.name)
            if c.expr is not None:
                expr_txt = c.expr
            else:
                # 2026-09-17 图谱轮修正: 面板候选的撞车身份 = 文件路径 + **列名**。
                # 原来只拼文件路径 ⇒ 同一 parquet 的多列被判"同公式"全部拒跑
                #（kg_r2 14 列只放行 1 列的实证）。列才是身份, 路径只是出处。
                expr_txt = "panel:%s#%s" % (c.meta.get("source", "?"), mid)
            meta = {"alpha_id": mid, "expr": expr_txt,
                    "name": c.meta.get("name") or mid,
                    "zoo": c.meta.get("zoo") or c.meta.get("source")}
            # 门禁 v2 ③ 快速道（求值**前**）：撞车就拒跑, 一分算力都不花
            dup = bank.check_duplicate(meta, allow_rerun=self.allow_rerun)
            if dup["dup"]:
                self._refuse_duplicate(i, len(cands), meta, dup, save)
                continue
            try:
                if c.panel is not None:
                    raw = c.panel.reindex(index=dates_all)
                else:
                    raw = eng.eval(c.expr)
                if isinstance(raw, pd.Series):
                    raw = raw.to_frame()
                raw = raw.replace([np.inf, -np.inf], np.nan)
                neu = fe.neutralize(raw, ind)
            except Exception:
                log.exception("[%3d/%3d] %-12s 求值失败", i, len(cands), c.name)
                continue
            # P0-05: 先中性化再裁域 —— 顺序不能反。中性化按行业做回归, 先裁域会让
            # 回归样本变少、中性化后的值跟着变(那是另一处口径改动); 门禁只该决定
            # "在哪些票上读 head_excess", 不该顺手改因子值。
            # 2026-09-13（独立审查 #4）：**判决与落库必须是同一个域**。原来门禁读裁域面板、
            # 而 `bank.save_values` 存的是**未裁域** ⇒ 留痕/复查（critic）与门禁看的不是一回事。
            # 现在先裁一次、两处共用（顺便省掉一次重复计算）。
            panel_md = _masked_panel(neu, tradable, md)
            det = gate.evaluate(panel_md, fwd, md, zones)
            if det.empty:
                log.warning("[%3d/%3d] %-12s 无有效截面", i, len(cands), c.name)
                continue
            # 门禁 v2 ①：门槛 = 0.5%/月 + 该因子的自换手成本（费率现算, 不写常量）。
            # 成本算不出（无有效期数/无费率）时 `cost_per_month=None` ⇒ 门禁判否（fail-closed）。
            cost = critic.churn_cost(panel_md, md)
            rec = gate.summarize(mid, det, n_tests, extra={
                "avg_names": float(det["n"].mean()),
                "expr": expr_txt,
                "info_source": c.meta.get("info_source"),
                "source": c.meta.get("source"),
                "pit_version": "pit_v1_%s" % eval_since,
                "note": "%s | %s" % (meta_note, c.meta.get("note", "")),
            }, cost_per_month=cost.get("cost_per_month"),
                n_tests_family=n_tests_family, n_tests_prior=n_tests_prior)
            # 门禁 v2 ③ 慢速道（求值**后**）：值面板撞车（"代数等价但写法不同"）
            dup2 = bank.check_duplicate({**meta, "yearly": rec.get("yearly")},
                                        values=panel_md, md=md,
                                        allow_rerun=self.allow_rerun)
            if dup2["dup"]:
                self._refuse_duplicate(i, len(cands), rec, dup2, save)
                continue
            if save:
                bank.save_values(mid, panel_md)          # 存"被判决的那一版"（PIT 可交易域）
                bank.upsert(rec)
            records.append(rec)
            self.results.append(rec)
            if self.on_result:
                self.on_result(rec)
            log.info("[%3d/%3d] %-12s DEAD head %+.4f (t=%+.2f) top6 %+.4f | ACTIVE head "
                     "%+.4f | p_adj %.4f | 线 %.4f | %s | %.1fs", i, len(cands), mid,
                     rec["DEAD_head"], rec["DEAD_head_t"], rec["DEAD_top6"],
                     rec["ACTIVE_head"], rec["p_adj"], rec["head_bar"],
                     "PASS" if rec["PASS"] else "否", time.time() - t1)
        if self.duplicates:
            log.warning("撞车硬闸拒跑 %d 条（门禁 v2 ③; 明细见 self.duplicates 与 "
                        "alpha_bank 里 status='DUPLICATE' 的留痕行）", len(self.duplicates))
        log.info("-" * 88)
        log.info("完成 %d/%d | %.0fs", len(records), len(cands), time.time() - t0)
        log.info("⚠️ PASS = POOLED = 有资格进组合层回测, **不等于可用** —— "
                 "判决只能是组合层两轴(`exp_die_stage_b.py` 预注册)")
        return records

    def _refuse_duplicate(self, i: int, n: int, rec: dict, dup: dict, save: bool) -> None:
        """撞车留痕：日志 + 内存 `self.duplicates` +（仅当 id 不在册）一行 `status='DUPLICATE'`
        占位（**不写读数、不计数**）。

        2026-09-18 审查 Q-05：已在册的 id **一次都不许碰**。原来这里无条件 upsert,
        而 `bank.upsert` 的 `ON CONFLICT DO UPDATE` 覆盖全部非键列 ⇒ 新行没给的读数
        一律变 NULL —— 等于把证据台账主动降级成空壳。实测 09-18 12:24 有 10 行 `kg_*`
        以 `rerun ←` 被这样抹掉（round-2 唯一显著读数只剩日志一行）。
        占位行只对"从未登记过"的 id 有意义（那 17 行 `id60_*` 属此类, NULL 是设计）。
        """
        log.warning("[%3d/%3d] %-12s 撞车拒跑（%s ← %s）: %s", i, n, rec.get("alpha_id"),
                    dup["kind"], dup["existing"], dup["detail"])
        self.duplicates.append({"alpha_id": rec.get("alpha_id"), **dup})
        if not save:
            return
        aid = rec.get("alpha_id")
        try:
            if bank.get(aid):            # 已在册 ⇒ 那一行是证据, 一个字都不改
                log.info("       └ 已在册, 仅留痕不覆盖（Q-05: upsert 会把读数抹成 NULL）")
                return
            bank.upsert({"alpha_id": aid,
                         "note": "撞车硬闸(门禁 v2 ③, KB 14 号 三补十四): "
                                 "%s ← %s | %s" % (dup["kind"], dup["existing"], dup["detail"])},
                        status="DUPLICATE")
        except Exception:      # 留痕失败不该打断整批（读数仍在日志里）
            log.exception("撞车留痕写入失败: %s", aid)
