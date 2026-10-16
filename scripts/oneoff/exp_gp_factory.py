"""Alpha Factory 薄 CLI —— `app/alpha/factory.py` 的命令行壳(2026-09-11)

## 与 `alpha_factory_v1.py` 的关系(为什么不是改写它)

`alpha_factory_v1.py` 是**产出冻结证据的那个脚本**: 它的 158 条 qlib158 读数与 15 条
flow 读数已经写进 `alpha_bank` 并进了证据台账(记忆 `new-money-evidence-ledger`)。
**改写它 = 让已登记的证据失去可复现性。** 故保留原样, 本脚本是新的壳:

| | `alpha_factory_v1.py` | 本脚本 |
|---|---|---|
| 候选来源 | 写死 qlib158 / flow | 可插拔(池子 / **GP 符号回归** / 离线 LLM) |
| 分母 | 靠脚本里的常量记住 | **`declare()` 冻结**, 断言强制 |
| 预注册 | docstring 里写 | **`--intent` 必填**, 整份写进每条登记 |
| 活跃区 | 直接算 | GP 的适应度**物理上拿不到**活跃区 |

## 用法

    # 只跑公开池子(标尺), 复算 v1 的读数
    ./.venv/Scripts/python.exe scripts/oneoff/exp_gp_factory.py \\
        --intent "复算 qlib158 标尺" --zoo qlib158

    # GP 符号回归(死区搜索, 活跃区一次性开封)
    ./.venv/Scripts/python.exe scripts/oneoff/exp_gp_factory.py \\
        --intent "符号回归能否找到 31 因子没覆盖的组合" --gp 60 15

    # 两路一起(分母相加)
    ... --zoo qlib158 --gp 60 15

    # 冒烟: 极小预算 + 不写库(只验证管道通)
    ... --intent "冒烟" --gp 8 2 --smoke

## 强制纪律

- **`--intent` 必填**, 没有默认值 —— 说不出"想证明什么"就不该烧预算
- **`--smoke` 不写 alpha_bank** —— 冒烟读数不是证据, 不许混进证据表
- 分母 = `zoo 候选数 + population × generations`(GP 用**声明值**而非实际求值数)
"""
import argparse
import io
import logging
import sys
import time
from pathlib import Path

import pandas as pd

# ⚠️ 先留住原 wrapper 的引用: TextIOWrapper 接管 sys.stdout.buffer 后, 旧 wrapper 一旦被 GC
# 就会关掉底层 buffer(症状=日志文件正常, 控制台全 "I/O operation on closed file")。
_ORIG_STDOUT = sys.stdout
if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(_ORIG_STDOUT.buffer, encoding="utf-8", errors="replace")

BASE = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE))

from app import db                                            # noqa: E402
from app.alpha import bank, factory as F, gate, zoos          # noqa: E402
from app.factors import factor_engine as fe                   # noqa: E402

LOG_PATH = r"D:/claude-home/tmp/gp_factory.log"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--intent", required=True,
                    help="**必填**: 这一轮想证明什么(整份写进每条登记, 供日后追责)")
    ap.add_argument("--zoo", default="", choices=["", "qlib158", "flow"],
                    help="公开池子: qlib158(标尺) / flow(资金类异构口粮)")
    ap.add_argument("--gp", nargs=2, type=int, metavar=("POP", "GEN"), default=None,
                    help="GP 符号回归的冻结预算 = POP × GEN(即 Bonferroni 分母)")
    ap.add_argument("--llm-json", default="", help="离线 LLM 产出的候选表达式 JSON")
    ap.add_argument("--eval-since", default="2019-01-01")
    ap.add_argument("--smoke", action="store_true",
                    help="冒烟: **不写 alpha_bank**(冒烟读数不是证据)")
    args = ap.parse_args()
    if not (args.zoo or args.gp or args.llm_json):
        ap.error("至少要有一个候选来源(--zoo / --gp / --llm-json)")

    fmt = logging.Formatter("%(asctime)s %(message)s")
    fh = logging.FileHandler(LOG_PATH, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    # ⚠️ handler 必须挂到 **root**(与本项目其它 oneoff 脚本一致)。此前的写法是给一个
    # 独立的 `"gp_factory"` logger 挂 handler ⇒ `app.alpha.factory` 等模块里所有
    # `log.info` **全部被丢弃**(它们冒泡到 root, 而 root 是默认 WARNING 且没有 handler)。
    # 症状 = **跑 20 分钟一行日志都没有**: 行情面板尺寸、GP 每代进度、提议器耗时全看不见。
    logging.basicConfig(level=logging.INFO, handlers=[fh, sh], force=True)
    log = logging.getLogger("gp_factory")
    db.init_db()

    prereg = F.PreRegistration(intent=args.intent, eval_since=args.eval_since)
    f = F.Factory(prereg)
    t0 = time.time()

    # ---- 提议器登记(**必须在 declare() 之前**) -----------------------------
    if args.zoo == "qlib158":
        f.add(F.ZooProposer(zoos.all_candidates(), label="qlib158"))
    if args.llm_json:
        f.add(F.LLMProposer(args.llm_json))

    # flow 与 GP 都要先有行情面板才算得出候选。`market()` 是**幂等**的,
    # 且它不算"求值"(没有碰任何判据), 故可以在 declare() 之前调。
    if args.zoo == "flow" or args.gp:
        m, dates_all, md_all = f.market()
        if args.zoo == "flow":
            f.add(F.ZooProposer.flow_proposer(m, dates_all))
        if args.gp:
            pop, gen = args.gp
            dead = md_all[(md_all >= pd.Timestamp(gate.DEAD[0]))
                          & (md_all <= pd.Timestamp(gate.DEAD[1]))]
            f.add(F.GPProposer(m, dead, budget=F.GPBudget(population=pop,
                                                          generations=gen)))

    n = f.declare()
    log.info("=" * 88)
    log.info("INTENT: %s", args.intent)
    log.info("**Bonferroni 分母已冻结 = %d**(=%s)", n,
             " + ".join("%s:%d" % (p.label, p.declared()) for p in f._proposers))
    log.info("冒烟模式: %s", "是(**不写库**)" if args.smoke else "否")
    log.info("=" * 88)

    df = f.run(save=not args.smoke)
    if not len(df):
        log.error("无有效读数")
        return 1
    passed = df[df["PASS"] == 1].sort_values("DEAD_head_t", ascending=False)
    log.info("=" * 88)
    log.info("完成 %d 条 | 门禁通过 %d 条 | %.0fs", len(df), len(passed), time.time() - t0)
    for _, r in passed.head(15).iterrows():
        log.info("  PASS %-12s DEAD head %+.4f t=%+.2f top6 %+.4f | ACTIVE head %+.4f "
                 "| p_adj %.4f", r["alpha_id"], r["DEAD_head"], r["DEAD_head_t"],
                 r["DEAD_top6"], r["ACTIVE_head"], r["p_adj"])
    log.info("⚠️ PASS = POOLED = 有资格进组合层回测, **不等于可用**")
    log.info("   判决只能是组合层两轴(`exp_die_stage_b.py` 的预注册); 本项目已有 4 个")
    log.info("   「门禁通过却对策略无用」的反例(die_flow / head_vt / h10 / "
             "margin_net_share)")
    log.info("=" * 88)
    return 0


if __name__ == "__main__":
    sys.exit(main())
