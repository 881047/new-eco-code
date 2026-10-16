"""「芯片式异构架构」接进月链的接线盒(2026-09-11)

对应用户框架图从 `Alpha Factory` 到 `Alpha Health/Decay` 的那一整条竖线。方案 §3
不变量 #7 写的是「**不碰实盘链路**」—— 本模块就是那条不变量在代码里的样子:

1. **全部默认关闭** ⇒ 生产行为**逐位不变**(不是"应该不变")。`run_stages` 在所有开关
   都关时立即返回, **连一个模块都不 import**。
2. **任何阶段的失败都不许传播** —— 每个阶段各跑各的 try/except, 出错只记日志。
   框架是为了给生产**加**东西, 不是给生产**加风险**。
3. **一个新阶段要接进来, 必须同时改三处**: `DEFAULT_CONFIG["framework"]["stages"]`、
   本模块的 `_STAGE_FUNCS`、以及 `tests/test_framework.py` 的守卫测试。三处不一致
   会当场测红 —— 防止有人"顺手"加一个默认开启的阶段。

## 各阶段的副作用边界

| 阶段 | 写什么 | 会不会动生产 |
|---|---|---|
| `factory` | `alpha_bank`(新登记) | 否 |
| `pipeline` | `alpha_bank` 的 **status**(经 `commit` 的 OOS 判决) | 否 —— 不写任何打分表 |
| `fusion` | `model_score_ens`(实验表) | 否 —— `fusion.write_scores` 结构性拒绝写 `model_score` |
| `optimizer` | 无(只产出权重对象) | 否 —— 生产方案仍由 `builder.build_plan` 生成 |
| `health` | 无(**只出退役候选**) | 否 —— 绝不自动退役 |

⇒ **即使把全部开关打开, 生产打分表与生产方案也不会被改写。** 这条性质由
`tests/test_framework.py` 里对 `model_score` / `bank.retire` 的守卫测试守住。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import pandas as pd

from app import config as cfg_mod
from app import timeutil

log = logging.getLogger(__name__)

# 阶段名。**加一个就要同时改 `_STAGE_FUNCS` 与 DEFAULT_CONFIG**, 否则守卫测试会红。
STAGES = ("factory", "pipeline", "fusion", "optimizer", "health")

# OOS 判决的两个窗口 —— **逐字复制自 `exp_die_stage_b.py`**(预注册冻结)。
# 改动这里等于改判据: `pipeline.OOS_WINDOWS` 会当场对不上而拒绝出判决。
WINDOWS = (("prod", "20190722"), ("full", "20170101"))
BASE_TABLE = "model_score"

TRACE_DIR = Path(cfg_mod.__file__).resolve().parent.parent / "data" / "framework_runs"


# ---------------------------------------------------------------------------
# 开关
# ---------------------------------------------------------------------------
def _fw(cfg: dict) -> dict:
    return (cfg or {}).get("framework") or {}


def enabled_stages(cfg: dict = None) -> list:
    """当前开启的阶段列表。总开关关 ⇒ 一个都不开(不是"按子开关算")。"""
    fw = _fw(cfg if cfg is not None else cfg_mod.load_config())
    if not fw.get("enabled"):
        return []
    st = fw.get("stages") or {}
    return [s for s in STAGES if st.get(s)]


# ---------------------------------------------------------------------------
# OOS runner —— `pipeline.Pipeline.oos()` 需要的那块拼图
# ---------------------------------------------------------------------------
def make_oos_runner(tables: dict):
    """给定 `{alpha_id: 打分表名}`, 产出一个可以直接喂给 `Pipeline.runner` 的函数。

    判决逻辑**逐字复刻** `exp_die_stage_b.py` 的三臂做法: 每个窗口各跑基线
    (`model_score`)与候选两臂, 取 `sys_ann_return` / `sys_max_drawdown` 两个轴。

    ⚠️ **表名必须显式给出, 不许猜。** 猜错表名 = 拿另一条 alpha 的成绩给这一条判卷,
    那是**假证据** —— 比"缺证据判否"坏得多。所以查不到就抛错, 由 `Pipeline.oos`
    捕获并判否。
    """
    def _run(aid: str) -> dict:
        if aid not in tables:
            raise KeyError(
                "alpha_id %r 没有登记对应的打分表 —— 拒绝猜测表名"
                "(猜错会拿别的 alpha 的成绩判卷, 那是假证据)。"
                "请在 config 的 framework.alpha_tables 里写明。" % aid)
        from app.backtest import engine as E
        table = tables[aid]
        res = {"alpha_id": aid, "table": table, "windows": {}}
        for wname, start in WINDOWS:
            arms = {}
            for arm, tab in (("base", BASE_TABLE), ("cand", table)):
                m = E.run_backtest(score_table=tab, start=start,
                                   run_tag="fw_%s_%s_%s" % (aid, arm, wname))
                arms[arm] = {"sys_ann": m["sys_ann_return"],
                             "sys_mdd": m["sys_max_drawdown"],
                             "run": m["run_date"], "periods": m.get("periods")}
            res["windows"][wname] = arms
        return res
    return _run


# ---------------------------------------------------------------------------
# 上下文 —— **惰性**
# ---------------------------------------------------------------------------
def build_ctx(cfg: dict = None) -> dict:
    """阶段上下文的构造器。**故意做成一个函数**, 这样传给 `run_stages(ctx=build_ctx)`
    时只有真的有阶段被启用才会被调用 —— 全关时连行情面板都不会加载。

    ⚠️ 这里**不建 `Factory`**。`_stage_factory` 需要 `ctx["factory"]` 显式注入, 由 CLI
    (`exp_gp_factory.py`)带着人写的 `--intent` 启动。理由见方案 §10a: GP 自动搜索的候选数
    巨大 ⇒ Bonferroni 分母不可知 ⇒ **它天生是过拟合机器**。把 GP 放进无人值守的月链里
    自动跑, 正好制造出「先搜、后用同一段数据判卷」的循环。所以工厂阶段只能被显式触发;
    这个阶段的开关存在, 是为了给"已用 CLI 挖好、登记进 bank 的候选"留一条月链通路。
    """
    cfg = cfg if cfg is not None else cfg_mod.load_config()
    fw = _fw(cfg)
    from app.alpha import gate
    from app.factors import factor_engine as fe
    from app.models import expert as ex

    since = str(fw.get("market_since") or cfg.get("eval_start") or "20190722")
    log.info("[框架] 加载行情面板 since=%s (构造阶段上下文) …", since)
    market = fe.load_market(since=since)
    dates = pd.to_datetime(market["close"].index)
    ctx = {
        "market": market,
        # 2026-09-12 审查 P2-18: "今天"按东八区(交易日语义), 与 scheduler/timeutil 同一个钟
        "md": fe.month_end_dates(dates, today=timeutil.today()),
        "fwd": gate.forward_return(market),
        "others": {},
    }
    ctx["zones"] = gate.zone_of(ctx["md"])
    for t in (fw.get("others_tables") or ["model_score"]):
        w = ex.load_scores(t)
        if len(w):
            ctx["others"][t] = w
    # shape[0]=交易日数, shape[1]=股票数(别写反 —— 2026-09-11 首跑就把这两个数标反了)
    log.info("[框架] 上下文就绪: 面板 %d 交易日 × %d 只 | 月末 %d 期 | 对照 %d 张表",
             market["close"].shape[0], market["close"].shape[1], len(ctx["md"]),
             len(ctx["others"]))
    return ctx


# ---------------------------------------------------------------------------
# 各阶段
# ---------------------------------------------------------------------------
def _stage_factory(cfg: dict, ctx: dict) -> dict:
    """Alpha Factory 挖候选并登记。**需要 `ctx["factory"]` 已建好**(见 CLI 用法)。"""
    f = ctx.get("factory")
    if f is None:
        return {"skipped": "ctx 里没有建好的 Factory 实例"}
    df = f.run(save=True)
    return {"candidates": int(len(df)),
            "passed": int((df["PASS"] == 1).sum()) if len(df) else 0}


def _stage_pipeline(cfg: dict, ctx: dict) -> dict:
    """Pool → Critic → OOS → Bank。**只有 OOS 能改状态**。"""
    from app.alpha import bank, pipeline as P
    fw = _fw(cfg)
    ids = list((fw.get("alpha_tables") or {}).keys())
    if not ids:
        return {"skipped": "framework.alpha_tables 为空 —— 没有候选可验"}
    pl = P.Pipeline(runner=make_oos_runner(fw["alpha_tables"]))
    # ⚠️ 参数名是 `known`(对照表), **不是 `others`**。2026-09-11 这里曾写成 `others=` ——
    # 因为测试只验了 `make_oos_runner` 的产出形状、从没真正跑过这个函数, 全绿也照样带着
    # 这个 TypeError 上线。补了 `TestPipelineStageIsActuallyWired` 守它。
    crit = pl.critic(ids, ctx.get("market"), ctx.get("md"), ctx.get("zones"),
                     known=ctx.get("others"))
    verdicts = pl.oos(ids)
    committed = pl.commit(verdicts)
    return {"critic": int(len(crit)), "oos": int(len(verdicts)),
            "passed": int(sum(v.passed for v in verdicts)),
            "committed": int(len(committed)),
            "statuses": sorted(set(bank.list_alphas()["status"]))
            if len(bank.list_alphas()) else []}


def _stage_fusion(cfg: dict, ctx: dict) -> dict:
    """把多个 die 融成一个 Pure Alpha Score, 写 `model_score_ens`。"""
    from app.models import fusion
    fw = _fw(cfg)
    die = fw.get("die_tables") or {}
    if not die:
        return {"skipped": "framework.die_tables 为空 —— 无 die 可融"}
    md = ctx.get("md")
    if md is None or ctx.get("fwd") is None:
        return {"skipped": "ctx 缺 md/fwd"}
    wide, diag = fusion.stack(die, ctx["fwd"], md)
    info = fusion.write_scores(wide, table=fw.get("ens_table", "model_score_ens"))
    return {"ensemble": info, "diag_months": int(len(diag))}


def _stage_optimizer(cfg: dict, ctx: dict) -> dict:
    """凸优化出力所能及的权重。**只产出, 不落方案** —— 方案仍归 `builder`。

    这一步刻意不接进 `build_plan`: 按 §10b, 持仓数由**账户规模与 ¥5 佣金地板**解出,
    而账户规模的变更是一个需要用户拍板的决定, 不该由一条自动管线悄悄做掉。
    """
    from app.portfolio import optimizer as O
    fw = _fw(cfg)
    eq = float((fw.get("investable") or 0) or 0)
    if eq <= 0:
        return {"skipped": "framework.investable 未配置(可投资金, 元)"}
    # 2026-09-13（独立审查 #8）：原来没传 `params` ⇒ `feasible_n` 用**默认费率**算"该持几只"，
    # 而生产费率以 `config.yaml` 的 fees 段为准（万2.5/最低5元/印花税/过户费都可能被改过）。
    # 费率一变，"该持几只"的解就变 —— 这条接线以前会说假话。现在显式传配置费率。
    from app.portfolio import fees as fees_mod
    plan = O.feasible_n(eq, target_rate=fw.get("round_trip_target", 0.005),
                        params=fees_mod.fee_params(cfg))
    return {"feasible_n": plan["n"], "per_position": plan["per_position"],
            "round_trip": plan["round_trip"],
            "note": "只解出持仓数上界; 实际权重仍由 builder 生成"}


def _stage_health(cfg: dict, ctx: dict) -> dict:
    """体检 + **退役候选**。`auto_retired` 恒为 0, 且它是这个返回值的一部分 ——
    这样"没有自动退役"是被**观测**到的, 而不是靠读代码相信的。"""
    from app.alpha import health
    reps = health.audit_all(md=ctx.get("md"), zones=ctx.get("zones"),
                            others=ctx.get("others"), fwd=ctx.get("fwd"))
    cands = health.retire_candidates(reps)
    for r in cands:
        log.warning("[健康] %s 建议退役(结构性): %s", r.alpha_id,
                    "; ".join(x.detail for x in r.structural()))
    return {"audited": len(reps), "candidates": [r.alpha_id for r in cands],
            "auto_retired": 0,
            "descriptive_only": [r.alpha_id for r in reps
                                 if r.descriptive() and not r.retire_worthy()]}


_STAGE_FUNCS = {
    "factory": _stage_factory,
    "pipeline": _stage_pipeline,
    "fusion": _stage_fusion,
    "optimizer": _stage_optimizer,
    "health": _stage_health,
}


# ---------------------------------------------------------------------------
# 总入口
# ---------------------------------------------------------------------------
def run_stages(cfg: dict = None, ctx=None, only: list = None) -> dict:
    """跑所有**已启用**的阶段。**永不抛异常。**

    `ctx` 可以是 dict, 也可以是**无参函数**(惰性构造)。惰性很重要: 构造上下文要加载
    全市场行情面板(几分钟到几十分钟), 而月链里调它时**默认一个阶段都没开** ——
    传函数的写法让"没开"真的等于"零成本", 而不是"先花 20 分钟建好再丢掉"。

    返回 `{"enabled": [...], "ran": {阶段: 结果}, "failed": {阶段: 错误}}`。

    全关时在任何 import 之前返回 ⇒ 对生产链路是严格的 no-op。
    """
    cfg = cfg if cfg is not None else cfg_mod.load_config()
    want = enabled_stages(cfg)
    if only is not None:
        want = [s for s in want if s in set(only)]
    out = {"enabled": want, "ran": {}, "failed": {}}
    if not want:
        return out
    if callable(ctx):                    # 惰性: 只有真的要跑阶段时才建上下文
        try:
            ctx = ctx()
        except Exception as e:
            out["failed"]["_ctx"] = "%s: %s" % (type(e).__name__, e)
            log.exception("[框架] 上下文构造失败 —— 所有阶段跳过(不影响生产链路)")
            _trace(out)
            return out
    ctx = ctx or {}
    for name in want:
        fn = _STAGE_FUNCS.get(name)
        if fn is None:
            out["failed"][name] = "没有实现"
            log.error("[框架] 阶段 %s 没有实现 —— 开关与 _STAGE_FUNCS 不一致", name)
            continue
        try:
            out["ran"][name] = fn(cfg, ctx)
            log.info("[框架] 阶段 %-9s 完成: %s", name,
                     json.dumps(out["ran"][name], ensure_ascii=False, default=str)[:300])
        except Exception as e:
            out["failed"][name] = "%s: %s" % (type(e).__name__, e)
            log.exception("[框架] 阶段 %s 失败(不影响生产链路)", name)
    _trace(out)
    return out


def _trace(out: dict) -> None:
    """留痕。失败写不进去也绝不影响生产。"""
    try:
        TRACE_DIR.mkdir(parents=True, exist_ok=True)
        # P2-18: 文件名时间戳也走东八区, 与 pipeline_run 的 started_at 对得上
        p = TRACE_DIR / (timeutil.now().strftime("%Y%m%d_%H%M%S") + ".json")
        p.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str),
                     encoding="utf-8")
    except Exception:
        log.debug("[框架] 留痕失败(忽略)", exc_info=True)
