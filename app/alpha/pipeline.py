"""Alpha 流水线 —— `Alpha Pool → Devil/Critic → OOS验证 → Alpha Bank`(2026-09-11)

对应用户框架图的那一段。**它是一条状态机, 不是一个评分器。**

    Alpha Factory ──产出+门禁──> CANDIDATE / POOLED
                                      │
                    Devil/Critic ─────┤  写 note(**只留痕, 不改状态**)
                                      │
                    OOS 验证 ─────────┤  组合层两轴(唯一有权改状态的证据)
                                      │
                    Alpha Bank ───────┘  POOLED → VALIDATED / RETIRED

## 三段各自的权力(写进代码, 不是写进文档)

| 段 | 能做什么 | **不能**做什么 |
|---|---|---|
| Critic | 产出否证读数, 写 `bank.note` | **不能改状态** —— 它的签名里根本没有写状态的路径 |
| OOS | 跑组合层两轴, 给出 4 个不等式 | 不能自己定判据 —— 判据由 `OOS_CRITERIA` 冻结 |
| Bank | 按 OOS 的读数流转状态 | 不能凭空 PROMOTE —— 没跑过 OOS 的一律拒绝 |

### 为什么 Critic 必须被"结构性地"剥夺权力

`app/alpha/critic.py` 的硬规则写着「Critic 的输出不得直接当过滤器用」。但**一条写在
注释里的规则迟早会被绕过**(本项目已有 4 个「门禁通过却对策略无用」的反例,
其中 `margin_net_share` 就是在知道全部读数之后仍然进了阶段 B)。所以这里把它做成
签名约束: `critic()` 的返回值**进不了** `_transition()` 的入参。想拿它做过滤器,
必须显式改动本模块 —— 那是一个能被 code review 看见的动作。

### 「OOS 验证」在本项目里的准确含义

不是"样本外再算一遍 IC", 而是 **`scripts/oneoff/exp_die_stage_b.py` 的那套**:
把候选作为打分表接进**真实的回测引擎**, 在**两个评估窗**上各比两个量:

| 窗 | 起点 | 为什么 |
|---|---|---|
| `prod` | `20190722` | 现行 pin `20260912_limitquant` 的口径(85 期; 2026-09-12 两次重钉, 见 config.yaml 的 pin 注释) |
| `full` | `20170101` | 含 2018 崩盘, 防止"靠选窗口赢"(115 期) |

判据 = **两个窗的「系统年化」与「系统回撤」都不劣于同窗基线** ⇒ **4 个不等式全成立**。
少一个都不行 —— 这是 `exp_die_stage_b.py` 已经预注册过的判据, 本模块**原样冻结**,
不新增、不放宽。

### 为什么 OOS 必须靠注入的 runner

流水线**不自己跑回测** —— 回测很贵(每窗约 2 分钟)、且各候选的打分表构造方式不同
(有的要重训、有的直接写表)。故 OOS 段接收一个 `runner(alpha_id) -> dict` 回调,
由调用方注入。本模块只负责: **调用它、校验它给的读数、按冻结判据流转状态**。

## 用法

    pl = Pipeline(prereg, runner=my_runner)      # runner 见下
    pl.run(factory)                              # 跑完整条链

    # runner 契约: 返回
    # {"windows": {"prod": {"cand": {"sys_ann": …, "sys_mdd": …},
    #                       "base": {"sys_ann": …, "sys_mdd": …}}, "full": {...}},
    #  "run_tags": [...], "note": "…"}
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Callable

import pandas as pd

from app.alpha import bank, critic, factory, gate
from app.config import DATA_DIR

log = logging.getLogger(__name__)

TRACE_DIR = DATA_DIR / "alpha_pipeline"

#: OOS 判据(**冻结**)。键 = 窗口名, 值 = 该窗必须成立的不等式。
#: 与 `scripts/oneoff/exp_die_stage_b.py` 的预注册逐字相同 —— 改这里 = 换判据,
#: 必须另开一轮并写明为什么上一轮不够(见 `app/config.py` 已证伪登记处的纪律)。
OOS_WINDOWS = ("prod", "full")
OOS_METRICS = ("sys_ann", "sys_mdd")


@dataclass
class Verdict:
    alpha_id: str
    passed: bool
    detail: dict
    reason: str

    def as_dict(self) -> dict:
        return {"alpha_id": self.alpha_id, "passed": self.passed,
                "reason": self.reason, "detail": self.detail}


def judge_oos(res: dict) -> Verdict:
    """按冻结判据判一个候选的 OOS 读数。

    **4 个不等式全成立才 PASS**(2 个窗口 × 2 个轴)。缺任何一个窗口的读数 ⇒ 直接否,
    不给"用另一个窗替代"的机会 —— 那正是"看到结果之后选窗口"。
    """
    aid = res.get("alpha_id", "?")
    wins = res.get("windows") or {}
    missing = [w for w in OOS_WINDOWS if w not in wins]
    if missing:
        return Verdict(aid, False, res, f"缺窗口读数 {missing} —— 判否(不允许用单窗替代)")
    detail = {}
    ok = True
    for w in OOS_WINDOWS:
        blk = wins[w] or {}
        c, b = blk.get("cand") or {}, blk.get("base") or {}
        row = {}
        for m in OOS_METRICS:
            cv, bv = c.get(m), b.get(m)
            if cv is None or bv is None:
                row[m] = {"cand": cv, "base": bv, "pass": False, "delta": None}
                ok = False
                continue
            d = float(cv) - float(bv)
            p = d >= 0                      # 年化与回撤(负数)都是"越大越好"
            row[m] = {"cand": float(cv), "base": float(bv), "delta": d, "pass": bool(p)}
            ok = ok and bool(p)
        detail[w] = row
    n_pass = sum(1 for w in detail for m in detail[w] if detail[w][m]["pass"])
    reason = ("4 个不等式全部成立" if ok else
              f"只有 {n_pass}/4 个不等式成立 —— 判否(判据要求全成立)")
    return Verdict(aid, ok, detail, reason)


class Pipeline:
    """状态机驱动器。**所有状态变更都从这里过**, 且每次变更都带证据留痕。"""

    def __init__(self, prereg=None, conn=None,
                 runner: Callable[[str], dict] = None, trace: bool = True):
        self.prereg = prereg
        self._conn = conn
        self.runner = runner
        self.trace = trace
        self.log_rows: list[dict] = []

    # ------------------------------------------------------------------
    # ① Pool: 产生 + 门禁
    # ------------------------------------------------------------------
    def pool(self, factory) -> pd.DataFrame:
        """跑 `Factory`(产生 + 门禁 + 登记)。返回读数表。

        Factory 已经负责了预注册与分母冻结 —— 本方法**不再碰判据**。
        """
        df = factory.run()
        if len(df):
            n_pass = int((df["PASS"] == 1).sum())
            log.info("[① Pool] %d 条候选, 门禁通过(POOLED) %d 条", len(df), n_pass)
        return df

    # ------------------------------------------------------------------
    # ② Critic: 否证清单(**只写 note, 不改状态**)
    # ------------------------------------------------------------------
    def critic(self, alpha_ids: list[str], m: dict, md, zones,
               known: dict = None, style: dict = None) -> dict:
        """对每个候选跑否证清单, 结果写进 `bank.note`。

        ⚠️ 返回值**只用于留痕与下一轮预注册**, 不参与任何状态判定 —— 见模块 docstring。
        """
        fwd = gate.forward_return(m, gate.HORIZON)
        # 2026-09-13（ruff F841 + 独立审查 §3.4 死代码）：原来这里查了一次
        # `fe.industry_map(conn)` 但**从未使用**（纯浪费一次 DB 查询 + 一个连接），已删。
        out = {}
        # 2026-09-13（独立审查 #4）：留痕必须与门禁同域 —— 原来 critic 读未裁域面板，
        # 与 gate 的 PIT 可交易域读数系统性不同。这里造一次域（唯一实现），全候选复用。
        # 防御式：行情/日期不足（单测里的空面板）时**退回未裁域**，但那时 attack_all 会在
        # 报告里写 `_mask_warning` —— 降级可以，静默不行。
        try:
            mask = factory.tradable_mask(m, md) if len(md) else None
        except Exception:
            log.exception("构建 PIT 可交易域失败 ⇒ 本次留痕退化为未裁域（报告里会自曝）")
            mask = None
        for aid in alpha_ids:
            try:
                wide = bank.load_values(aid).loc[lambda x: x.index.isin(md)]
                rep = critic.attack_all(aid, wide, fwd, md, zones,
                                        known=known, style=style, mask=mask)
            except Exception as e:
                rep = {"alpha_id": aid, "error": f"{type(e).__name__}: {e}"}
            out[aid] = rep
            bank.set_note(aid, json.dumps(rep, ensure_ascii=False)[:4000])
            self._log("critic", aid, "留痕", rep.get("_disclaimer", ""))
            log.info("[② Critic] %-14s 否证读数已写 note (%d 项)",
                     aid, len([k for k in rep if not k.startswith("_")]))
        return out

    # ------------------------------------------------------------------
    # ③ OOS: 组合层两轴(唯一有权改状态的证据)
    # ------------------------------------------------------------------
    def oos(self, alpha_ids: list[str]) -> list[Verdict]:
        if self.runner is None:
            raise RuntimeError("OOS 段需要注入 runner(见模块 docstring 的契约); "
                               "没有它就无法产生唯一有权改状态的证据")
        out = []
        for aid in alpha_ids:
            t0 = time.time()
            try:
                res = self.runner(aid) or {}
            except Exception as e:
                log.exception("[③ OOS] %s runner 抛错", aid)
                res = {"alpha_id": aid, "error": f"{type(e).__name__}: {e}"}
            res.setdefault("alpha_id", aid)
            v = judge_oos(res)
            out.append(v)
            self._log("oos", aid, "PASS" if v.passed else "否", v.reason, res)
            log.info("[③ OOS] %-14s %s | %s | %.0fs", aid,
                     "**PASS**" if v.passed else "否", v.reason, time.time() - t0)
        return out

    # ------------------------------------------------------------------
    # ④ Bank: 状态流转
    # ------------------------------------------------------------------
    def commit(self, verdicts: list[Verdict]) -> pd.DataFrame:
        """按 OOS 判决流转状态。**没跑过 OOS 的候选不会到这里** —— 见 `run()` 的结构。"""
        rows = []
        for v in verdicts:
            rec = bank.get(v.alpha_id)
            if not rec:
                log.warning("[④ Bank] %s 不在 alpha_bank, 跳过", v.alpha_id)
                continue
            cur = rec["status"]
            if v.passed:
                if cur == "RETIRED":
                    # 退役是"只进不出"的防守动作; 复活需要**显式**的新预注册轮次,
                    # 不由流水线自动做
                    log.warning("[④ Bank] %s 当前 RETIRED, 流水线不自动复活", v.alpha_id)
                    rows.append({"alpha_id": v.alpha_id, "from": cur,
                                 "to": cur, "action": "拒绝自动复活"})
                    continue
                bank.upsert(rec, status="VALIDATED",
                            note=(rec.get("note") or "")[:3000])
                self._log("bank", v.alpha_id, "POOLED→VALIDATED", v.reason)
                rows.append({"alpha_id": v.alpha_id, "from": cur,
                             "to": "VALIDATED", "action": "PASS"})
            else:
                # OOS 不通过 ⇒ 不自动退役(POOLED 仍可作研究资产), 只回写判据结果。
                # 退役是 `bank.retire()` 的显式动作, 需要人写明 reason。
                self._log("bank", v.alpha_id, "保持", v.reason)
                rows.append({"alpha_id": v.alpha_id, "from": cur,
                             "to": cur, "action": "OOS 未通过, 状态不变"})
        return pd.DataFrame(rows)

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------
    def run(self, factory) -> dict:
        """跑完整条链。**结构上保证**: 状态变更只可能由 OOS 触发。"""
        t0 = time.time()
        df = self.pool(factory)
        if not len(df):
            log.error("工厂无产出, 流水线终止")
            return {"pooled": 0}
        pooled = df[df["PASS"] == 1]["alpha_id"].tolist()
        log.info("=" * 88)
        log.info("流水线 | 候选 %d | POOLED %d", len(df), len(pooled))
        if not pooled:
            log.info("无候选入池 —— Critic 与 OOS 段无事可做(这是正常结果, 不是故障)")
            self._flush()
            return {"pooled": 0, "validated": 0}
        log.info("=" * 88)

        m, dates_all, md_all = factory.market()
        md = md_all[md_all >= pd.Timestamp(factory.prereg.eval_since)]
        zones = gate.zone_of(md)
        self.critic(pooled, m, md, zones)

        verdicts = self.oos(pooled)
        summary = self.commit(verdicts)
        self._flush()
        n_val = int((summary["to"] == "VALIDATED").sum()) if len(summary) else 0
        log.info("=" * 88)
        log.info("流水线结束 | POOLED %d → VALIDATED %d | %.0fs",
                 len(pooled), n_val, time.time() - t0)
        if n_val:
            log.info("⚠️ VALIDATED 只代表**在本次两个窗口上不劣于同窗基线**; "
                     "接生产还要过 `bank.promote_to_factor` 的显式动作")
        log.info("=" * 88)
        return {"pooled": len(pooled), "validated": n_val,
                "verdicts": [v.as_dict() for v in verdicts]}

    # ------------------------------------------------------------------
    def _log(self, stage, alpha_id, action, reason, extra=None):
        self.log_rows.append({
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"), "stage": stage,
            "alpha_id": alpha_id, "action": action, "reason": reason,
            "extra": extra})

    def _flush(self):
        if not self.trace or not self.log_rows:
            return
        TRACE_DIR.mkdir(parents=True, exist_ok=True)
        p = TRACE_DIR / ("%s.json" % time.strftime("%Y%m%d_%H%M%S"))
        p.write_text(json.dumps(self.log_rows, ensure_ascii=False, indent=1,
                                default=str), encoding="utf-8")
        log.info("流水线留痕: %s (%d 条)", p, len(self.log_rows))
