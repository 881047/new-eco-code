"""阶段 5: 时序 die —— PatchTST 式 Transformer(`app/models/seq_die.py` 的薄壳)

对应用户框架图 `LightGBM | Transformer → Ensemble` 里的 **Transformer** 盒子。

=============================== 预注册(写于跑之前) ===============================
口粮 = **日频量价原始序列**(60 交易日 × 10 通道), 与 D1 的「31 个月频工程因子」
**同源**(都是行情), 所以这是**表示异构**, 不是 `die_flow` 那种信息源异构。

架构与训练超参全部冻结在 `seq_die.SeqConfig`(`frozen=True` dataclass), 本脚本
**不暴露任何调参开关** —— 想改一个数就得新开一轮并说明为什么上一轮不够。

判据(与其它 die **同一把尺子**, 不新造):
  ① `expert.kgd(table, n_tests=1)` 五项 —— 死区 head ≥0.5%/月、t≥2、两区同号、
     Bonferroni p<0.05、与基线秩相关 <0.95
  ② 组合层两轴(`scripts/oneoff/exp_die_stage_b.py`, 4 个不等式全成立)

⚠️ **①是入场券, ②才是判决。** 已有 4 个「KGD 通过却对策略无用」的反例
(`die_flow` / `head_vt` / `h10` / `margin_net_share`)。

=========================== 结果如何解释(同样写于跑之前) ===========================
方案 §5 对阶段 5 的价值定位原文是「**去相关, 不是更高精度** —— 若与 D1 秩相关 > 0.95
则直接退役」。

  PASS 且 ρ 低  → 时序表示确实带了月频因子没有的信息, 值得进融合器(仍需②判决)
  PASS 但 ρ≥0.95 → **换皮**, 按 §5 硬规则退役。这仍是有价值的读数: 它说明"换模型族"
                   同样产生不了独立性, 支持 §0 的核心论点(数据异构 > 算法异构)
  KGD 就不过     → 与 die_flow/head_vt/h10 一样, 记入反例清单
  **三种结局都算这一轮跑成功了** —— 本脚本的目的是把 §5 那句话从判断变成读数。
  只有"跑不出读数"算失败。

## 用法

    # 冒烟: 极小预算 + 只跑几期 + 不写正式表(冒烟读数不是证据)
    ./.venv/Scripts/python.exe scripts/oneoff/exp_seq_die.py \\
        --intent "管道冒烟" --smoke

    # 正式: 全期
    ./.venv/Scripts/python.exe scripts/oneoff/exp_seq_die.py \\
        --intent "日频序列表示能否提供与月频因子独立的 alpha"
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
# 本项目多个 oneoff 脚本栽过同一个坑, 重跑前记得确认这一行还在。
_ORIG_STDOUT = sys.stdout
if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(_ORIG_STDOUT.buffer, encoding="utf-8", errors="replace")

BASE = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE))

from app import db                                            # noqa: E402
from app.models import expert as ex, seq_die as S             # noqa: E402

LOG_PATH = r"D:/claude-home/tmp/exp_seq_die.log"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--intent", required=True,
                    help="**必填**: 这一轮想证明什么(供日后追责)")
    ap.add_argument("--name", default="seq", help="die 名 → model_score_die_<name>")
    ap.add_argument("--table", default="", help="覆盖打分表名(默认由 name 推出)")
    ap.add_argument("--since", default="20130101", help="行情面板起点(YYYYMMDD)")
    ap.add_argument("--start", default="2017-01-01", help="第一个评分月")
    ap.add_argument("--end", default="", help="最后一个评分月(空=到最新)")
    ap.add_argument("--months", type=int, default=0, help="只跑前 N 期(0=全部)")
    ap.add_argument("--epochs", type=int, default=0, help="覆盖轮数(**仅冒烟用**)")
    ap.add_argument("--smoke", action="store_true",
                    help="冒烟: 强制 3 轮 + 2 期 + 表名加 _smoke 后缀(冒烟读数不是证据)")
    args = ap.parse_args()

    if args.smoke:
        args.epochs = args.epochs or 3
        args.months = args.months or 2
        args.table = args.table or "model_score_die_seq_smoke"

    fmt = logging.Formatter("%(asctime)s %(message)s")
    fh = logging.FileHandler(LOG_PATH, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    # handler 挂 **root**(与本项目其它 oneoff 脚本一致): 挂到独立 logger 上会让
    # `app.models.seq_die` 里的 log.info 全部被丢弃, 症状是"跑两小时一行日志都没有"。
    logging.basicConfig(level=logging.INFO, handlers=[fh, sh], force=True)
    log = logging.getLogger("seq_die")
    db.init_db()

    # 预注册的配置是**默认值**, 只有冒烟才允许动 epochs —— 正式跑一律用 SeqConfig()
    sc = S.SeqConfig(epochs=args.epochs) if args.epochs else S.SeqConfig()
    log.info("=" * 88)
    log.info("INTENT: %s", args.intent)
    log.info("预注册配置: %s", sc)
    log.info("冒烟模式: %s", "是(**表名带 _smoke, 读数不是证据**)" if args.smoke else "否")
    log.info("=" * 88)

    t0 = time.time()
    res = S.run_die(name=args.name, sc=sc, table=args.table or None,
                    since=args.since, start=args.start,
                    end=args.end or None, max_months=args.months or None)
    if not res.get("n_rows"):
        log.error("没有产出任何分数 —— 是管道没通, 不是「未检出 alpha」")
        return 1

    log.info("=" * 88)
    log.info("落库 %s: %d 行 / %d 期 | %.0fs",
             res["table"], res["n_rows"], res["n_months"], time.time() - t0)
    log.info("-" * 88)
    try:
        rec = ex.kgd(res["table"], baseline="model_score", n_tests=1, label=args.name)
        for k in ("n_months", "DEAD_head", "DEAD_t", "ACTIVE_head", "ACTIVE_t",
                  "FULL_head", "FULL_t", "DEAD_top6", "ACTIVE_top6",
                  "corr_with_baseline", "p_adj", "PASS"):
            if k in rec:
                log.info("  %-20s %s", k, rec[k])
        log.info("-" * 88)
        log.info("⚠️ 以上是**入场券**, 不是判决。判决只能走组合层两轴:")
        log.info("   ./.venv/Scripts/python.exe scripts/oneoff/exp_die_stage_b.py")
        log.info("   方案 §5 对阶段 5 的定位是「去相关, 不是更高精度」—— 与基线秩相关")
        log.info("   ≥0.95 即按硬规则退役, 而那**同样是有效结论**(换模型族不产生独立性)。")
    except Exception:
        log.exception("KGD 体检失败(分数已落库, 可稍后单独跑)")
    log.info("=" * 88)
    return 0


if __name__ == "__main__":
    sys.exit(main())
