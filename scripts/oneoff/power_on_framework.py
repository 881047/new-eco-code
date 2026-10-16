"""通电 —— 第一次把 `framework.run_stages` 的五个阶段真的打开跑一遍 (2026-09-11)

用户 2026-09-11 指令: 「**通电**」。此前所有阶段开关**从未在生产打开过**,
`_stage_pipeline` 甚至带着一个 TypeError(`others=` vs `known=`)从未被执行过一次。

=============================== 预注册(写于跑之前) ===============================

## 这一轮是什么、不是什么

**是**: 验证「框架图 → 可执行链路」—— `failed` 是否为空、产出物 schema 是否合法、
`model_score_ens` 能不能真的进 `engine.run_backtest`。

**不是**: 融合器 alpha 的判决轮。但 —— **诚实地说** —— `_stage_fusion` 会**顺带**产生
stacking 融合的第一份 alpha 读数(`app/models/fusion.py` 的 OOF LambdaRank 元学习器
从未被评估过, 已被否的等权/门控/相对动量是 `exp_fusion_router.py` 里**另一套**实现)。
**既然它会产生产生读数, 判据就必须先写死**, 而不是"先看看数再说"。

## 判据(在跑之前写死, 不新造)

1. **链路判据(本轮的唯一目的)**: `run_stages()["failed"]` 必须为空;
   且 `model_score_ens` 能被 `engine.run_backtest` 正常消费(不报错、有 NAV)。
2. **顺带的 alpha 读数**: 判据**原样沿用** `scripts/oneoff/exp_die_stage_b.py` 的
   **4 个不等式**(prod/full 两窗 × sys_ann/sys_mdd 两轴都不劣于同窗基线), **全成立才算过**。
   这是本项目**冻结**的判决口径, **不因为"这轮主要是测管道"而放宽或收紧**。

## 先说清楚局限(写在看到结果之前)

- **可用的真 die 只有 2 个**: `model_score`(D1 量价 die)与 `model_score_die_flow`
  (D2 资金流 die)。其余 `model_score_*` 表**都不是可用的异构信息源**:
  `head_cls`/`head_vt`/`h5`/`h10` 是被证伪的实验臂; `snap`/`pit`/`fused_r*` 是实验中间产物;
  `die_seq_smoke` 只有 2 期冒烟; `star19`/`tm*`/`stylen`/`ab*`/`oldm` 是 **D1 的变体**
  (算法异构), 不是**数据异构**。
  ⇒ **方案里说的"4 个 die 表"在库里并不存在**。这是**设计缺口**, 不是本轮能补的。
- 因此这个集成 = 50% 基线 + 50% flow die。**2 个成员的 stacking 是很薄的集成** ——
  **即使两轴全过, 也不构成"融合器有效"的结论**, 只能算"有资格进下一轮"。
- **无论结果如何, 都不据此改任何配置、不接生产。** 接生产须走 `bank.promote_to_factor`
  的显式动作 + 用户拍板。

## 结果如何解释(同样写于跑之前)

    failed 空 + 4 个不等式全成立 → 链路通; 融合器**有资格**进下一轮(≠可以上)
    failed 空 + 有不等式不成立   → 链路通(本轮目的达成); 融合按冻结判据**否**
    failed 非空                  → 链路**仍未通**, 报出断在哪一段

**三种结局都算这一轮跑成功了** —— 本轮的目的是"知道链能不能通电"。
只有"跑不出结论"算失败。

⚠️ **本脚本绝不写 `config.yaml`**: 开关只在内存里覆盖(见 `_cfg`)。生产链路一字不动。

## 用法

    # 全链通电
    ./.venv/Scripts/python.exe scripts/oneoff/power_on_framework.py \\
        --intent "验证五阶段链路可执行性"

    # 只跑某几段
    ./.venv/Scripts/python.exe scripts/oneoff/power_on_framework.py \\
        --intent "只验融合段" --only fusion
"""
import argparse
import io
import json
import logging
import sys
import time
from pathlib import Path

# ⚠️ 先留住原 wrapper 的引用(本项目多个 oneoff 栽过同一个坑, 见方案 §11d)
_ORIG_STDOUT = sys.stdout
if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(_ORIG_STDOUT.buffer, encoding="utf-8", errors="replace")

BASE = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE))

from app import config as cfg_mod, db, framework as FW      # noqa: E402

LOG_PATH = r"D:/claude-home/tmp/power_on_framework.log"

#: 可用的**真 die**(见 docstring 的局限说明)。键 = 元学习器看到的特征名。
DIE_TABLES = {"d1": "model_score", "flow": "model_score_die_flow"}

#: alpha_id → 打分表。**必须逐条对应真实资产, 不许猜表名**
#: (`make_oos_runner` 会拒绝未登记的表名 —— 猜错 = 拿别的 alpha 的成绩判卷 = 假证据)。
#: 这里只登记一条: bank 里 `margin_net_share` 这个 alpha 对应的就是这张零参数打分表。
#: 它当前是 RETIRED ⇒ 顺带验证「流水线不自动复活退役 alpha」这条分支。
ALPHA_TABLES = {"margin_net_share": "model_score_die_margin_net_share"}


def _cfg(only: list, investable: float) -> dict:
    """**在内存里**打开开关 —— 不碰 `config.yaml`。"""
    cfg = json.loads(json.dumps(cfg_mod.load_config()))       # 深拷贝, 防污染
    st = {s: (not only or s in only) for s in FW.STAGES}
    cfg["framework"] = dict(cfg.get("framework") or {})
    cfg["framework"].update({
        "enabled": True,
        "stages": st,
        "die_tables": DIE_TABLES,
        "alpha_tables": ALPHA_TABLES,
        "ens_table": "model_score_ens",
        "investable": investable,
        "round_trip_target": 0.005,
        "others_tables": ["model_score"],
    })
    return cfg


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--intent", required=True, help="**必填**: 这一轮想证明什么")
    ap.add_argument("--only", default="", help="逗号分隔的阶段名(空=全部)")
    ap.add_argument("--investable", type=float, default=12_000.0,
                    help="可投资金(元), 给 optimizer 段解持仓数")
    args = ap.parse_args()
    only = [s.strip() for s in args.only.split(",") if s.strip()]

    fmt = logging.Formatter("%(asctime)s %(message)s")
    fh = logging.FileHandler(LOG_PATH, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    # handler 挂 **root**(挂独立 logger 会让子模块 log.info 全丢, 本项目栽过)
    logging.basicConfig(level=logging.INFO, handlers=[fh, sh], force=True)
    log = logging.getLogger("power_on")
    db.init_db()

    cfg = _cfg(only, args.investable)
    log.info("=" * 88)
    log.info("INTENT: %s", args.intent)
    log.info("阶段开关(内存覆盖, 不写 config.yaml): %s",
             {k: v for k, v in cfg["framework"]["stages"].items() if v})
    log.info("die_tables   : %s", DIE_TABLES)
    log.info("alpha_tables : %s", ALPHA_TABLES)
    log.info("=" * 88)

    t0 = time.time()
    res = FW.run_stages(cfg, ctx=FW.build_ctx)
    dt = time.time() - t0

    log.info("=" * 88)
    log.info("通电完成 | %.0fs | 启用 %s", dt, res["enabled"])
    for name, out in res["ran"].items():
        log.info("  [%-9s] %s", name, json.dumps(out, ensure_ascii=False, default=str)[:400])
    for name, err in res["failed"].items():
        log.error("  [%-9s] **失败**: %s", name, err)
    log.info("-" * 88)

    # ---- 链路判据 ①: failed 必须为空 ----
    if res["failed"]:
        log.error("链路判据 ① **不通过** —— 有阶段失败, 断点: %s", sorted(res["failed"]))
    else:
        log.info("链路判据 ① 通过: 五个阶段无失败")

    # ---- 链路判据 ②: 融合产出物能不能真的进引擎 ----
    ens = "model_score_ens"
    try:
        from app.backtest import engine as E
        m = E.run_backtest(score_table=ens, start="20190722", run_tag="fw_poweron_ens")
        log.info("链路判据 ② 通过: %s 进了引擎 | 系统年化 %.4f | 系统回撤 %.4f | %d 期",
                 ens, m["sys_ann_return"], m["sys_max_drawdown"], m.get("periods", 0))
        log.info("   ⚠️ 这条读数**不是证据** —— 它只是「引擎能吃下这张表」的收据。")
        log.info("   融合器的 alpha 判决在下一段(两窗两轴), 判据冻结于 exp_die_stage_b.py")
    except Exception as e:
        log.exception("链路判据 ② **不通过**: %s 进不了引擎", ens)

    log.info("=" * 88)
    log.info("留痕: %s / %s", LOG_PATH, FW.TRACE_DIR)
    return 0 if not res["failed"] else 1


if __name__ == "__main__":
    sys.exit(main())
