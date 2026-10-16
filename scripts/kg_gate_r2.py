"""知识图谱特征门禁 runner —— **同一个 runner 跨批复用**（文件名里的 r2 是历史名）

⚠️ 2026-09-18 审查 Q-08：这个文件的 docstring/输出原来一直自称"第二轮门禁（kg_r2）",
而实际跑的是**第五轮**（`info_source='kg_r5'`、`label='kg_r5'`）。台账
`docs/know/05_实验台账.md` 记的"kg_gate_r2 复用"是有意的，**文件名保留**（改它会打断
台账与 plans 里的路径引用）；但自称第二轮是把"跑的就是写的"这条纪律弄丢了的源头之一。

## 它做什么

把物化的图特征面板（data/kg/kg_features.parquet）经 `ZooProposer.panels` 直通进 Factory：
  - 死区/活跃区判据 = gate v2 六条，一字不改；
  - 面板列名即 alpha_id（`kg_*` 前缀）；
  - 分母 = `factory.declare()` 冻结的本批列数，prior 由 `bank.cumulative_tests()` 现算
    （工厂语义 `n_tests = max(本批, 在册累计 + 本批)`）。

## 申报列 vs 实取列（Q-08 的正面处理）

`BATCH.declared` 是**预注册冻结的列清单**，而下面取列是 `df.columns` 里**全部** `kg_` 列。
两者不等 ⇒ 当场把"申报 N / 实际 M / 多出来哪几列"打在 stdout 上（见 `main()`）。
**没有改成只跑申报列**，原因是：收窄取列会让 2026-09-18 12:24 被 Q-05 抹掉读数的
10 个 `kg_*` id 再也进不了重跑（`allow_rerun=True` 回填要靠这个 runner 喂到它们）。
所以这一轮只补"看得见的不一致"，动取列逻辑要等下一轮连同预注册一起改。

## 用法

    .venv/Scripts/python -X utf8 scripts/kg_gate_r2.py            # 当前 = 第五轮 kg_r5
    .venv/Scripts/python -X utf8 scripts/kg_gate_r2.py --round r5

⚠️ 图特征全部只读源表物化（kg_materialize.py），锚点日只见 ≤T 的事件（PIT）。
EPS 修正/目标价空间等 4 个研报缺口列曾缺席（源字段空值率高）。
"""
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import pandas as pd  # noqa: E402

from app.alpha.factory import Factory, PreRegistration, ZooProposer  # noqa: E402
from app.logger import setup_logger  # noqa: E402

setup_logger()

PANEL = BASE / "data" / "kg" / "kg_features.parquet"

#: 每轮的预注册出处 + **冻结时申报的列清单**（Q-08：申报与实际必须对账）
BATCHES = {
    "r2": {"info_source": "kg_r2",
           "prereg": "plans/kg-event-graph-round2-20260917.md",
           "declared": None,          # 第二轮申报即"全部 14 个图特征列"，无子集
           "title": "知识图谱第二轮（事件共振/PageRank）"},
    "r5": {"info_source": "kg_r5",
           "prereg": "plans/kg-unlock-insider-round5-20260918.md",
           "declared": ["kg_insider_buy_60d", "kg_insider_net", "kg_insider_sell_60d",
                        "kg_unlock_90d_cnt", "kg_unlock_90d_ratio"],
           "title": "知识图谱第五轮（解禁冲击 + 内部人行为）"},
}
CURRENT = "r5"
PRIOR = 18          # 第一轮 A 组条数（**历史申报口径**；真正用的 prior 由 alpha_bank 现算，
                    # 第五轮实跑 n_tests_prior=182 —— 两者不是一回事，见 :59 的打印）
# 第一轮已判等价/已测的 id60_* 族不在本批——B/C/D 的 13 条与图列 DUPLICATE 关系
# 由撞车闸在求值前自动拦（图列与第一轮无表达式重叠, 但 B/C/D 提案本身未入池,
# 故本批无撞车预期; 分母 = 本批列数）。


def _pick_round(argv: list) -> str:
    """`--round r5` / `--round=r5`；缺省 = CURRENT。未知轮次直接退出（不静默回退）。"""
    want = CURRENT
    for i, a in enumerate(argv):
        if a.startswith("--round="):
            want = a.split("=", 1)[1]
        elif a == "--round" and i + 1 < len(argv):
            want = argv[i + 1]
    if want not in BATCHES:
        print(f"未知轮次 --round {want!r}，可选: {', '.join(sorted(BATCHES))}")
        raise SystemExit(2)
    return want


def main(argv: list = None) -> int:
    rnd = _pick_round(argv if argv is not None else sys.argv[1:])
    batch = BATCHES[rnd]
    t0 = time.time()
    df = pd.read_parquet(PANEL)
    kgcols = sorted(c for c in df.columns if c.startswith("kg_"))
    print(f"[{batch['title']} · info_source={batch['info_source']}] "
          f"图特征 {len(kgcols)} 列 × {len(df):,} 行 ({df['anchor'].nunique()} 锚点)")

    # ---- Q-08：申报列 vs 实取列，不等就大声说出来（"跑的就是写的"要能对账）----
    declared = batch.get("declared")
    if declared is not None:
        extra = [c for c in kgcols if c not in declared]
        missing = [c for c in declared if c not in kgcols]
        print(f"分母申报: {len(declared)} 列（{batch['prereg']}）｜ 实取: {len(kgcols)} 列")
        if extra:
            print(f"⚠️ 多捞 {len(extra)} 列（面板里有、预注册没申报）: {', '.join(extra)}")
        if missing:
            print(f"⚠️ 缺 {len(missing)} 列（申报了、面板里没有）: {', '.join(missing)}")
        if not extra and not missing:
            print("✓ 申报与实际一致")
        print("   （本 runner 有意仍按**实取列**跑：收窄到申报子集会挡住 Q-05 那 10 行"
              "被抹读数的 allow_rerun 回填。改取列逻辑要连同预注册在下一轮做。）")
    else:
        print(f"分母申报: 全部图特征列（与实取同集）｜ 实取: {len(kgcols)} 列")

    # panels: {alpha_id: date×code 宽表}
    panels = {}
    for col in kgcols:
        w = df.pivot_table(index="anchor", columns="ts_code", values=col, aggfunc="last")
        w.index = pd.to_datetime(w.index)
        panels[col] = w
    zoo = ZooProposer(panels=panels, panel_meta={
        n: {"info_source": batch["info_source"], "source": "scripts/kg_materialize.py",
            "name": n} for n in panels}, label=batch["info_source"])

    prereg = PreRegistration(
        intent=f"{batch['title']}: {len(kgcols)} 个图特征列（事件计数/共振/带衰减"
               f"PageRank 行业传导）死区头部超额是否≥门禁; 数据=第 11~13 族+研报文本边"
               f"(llm_title_extract, deepseek-flash); **prior 以 alpha_bank 实际累计为准"
               f"（本 intent 冻结申报），申报列清单见 {batch['prereg']}**")
    f = Factory(prereg, market_since="20160101")   # 60d 窗口预热只需几个月, 2016 起留余量
    f.add(zoo)
    denom = f.declare()
    print(f"分母冻结: {denom}（本批）| 常量 PRIOR={PRIOR} 只是第一轮 A 组的历史申报口径，"
          f"工厂求值时按 alpha_bank 在册累计现算（第五轮实跑 n_tests_prior=182）")
    f.run(save=True)
    print(f"✓ {batch['title']}完成 ({time.time()-t0:.0f}s) "
          f"→ alpha_bank(info_source={batch['info_source']!r})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
