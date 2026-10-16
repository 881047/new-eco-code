"""阶段 B 判决: `margin_net_share` 零参数 die 值不值得封装?(2026-09-11)

`exp_die_stage_b.py` 是通用骨架(单 die vs 基线)。本轮是它第一次遇到一个**新问题**:
候选因子的覆盖域天生比基线窄 —— 必须先把这件事说清楚, 否则判决不可解释。

================================================================================
预注册(写在这里, 写于跑任何组合层读数之前)
================================================================================

**候选 = `margin_net_share` 的零参数打分表**(表 `model_score_die_margin_net_share`):
score = 该因子的中性化月末面板值 **原样** —— 不调方向、不调阈值、不做任何变换、
不搜索任何超参。**唯一变量 = 分数怎么产生**, 其余全部是现行生产 config。

## 本轮唯一的、必须在跑之前说清楚的新设计点: 覆盖域

`margin_net_share` 只对**两融标的**有定义(约 2300/5806), 其余为 NaN。引擎的候选池是
`pool.nlargest(..., "score")`(`engine.py:400`), **pandas 的 nlargest 丢弃 NaN**
⇒ 「NaN = 这个 die 对该股无意见 = 不买」。故 die 的选股域天然比基线窄,
**这不是纯排序实验, 域也变了**。所以本轮跑**三臂**, 但判决只用其中两臂:

| 臂 | 表 | 选股域 | 分数列 | 它回答什么 |
|---|---|---|---|---|
| BASE | `model_score` | 生产域 | 生产模型 | 现行基线 |
| **COV** | `model_score_margincov_ctl` | **die 域** | **生产模型原样** | **只缩域**(归因用) |
| **DIE** | `model_score_die_margin_net_share` | **die 域** | `margin_net_share` | **只换排序**(判决) |

**主判决 = DIE vs BASE**, 判据照抄 `exp_die_stage_b.py` 的预注册, **一字不放宽**:
两个窗口(`prod`=20190722 起 / `full`=20170101 起)**都**要在两轴(系统年化 + 系统回撤)
上不劣于各自同窗基线 ⇒ **4 个不等式全成立才算 PASS**。任一破 = 否。

**COV 不参与判决, 且不构成第二次机会** —— 若 DIE 输给 BASE, 不许改口说
"DIE 其实赢 COV 所以算赢"。COV 只用于把总差拆成两项, 否则「是因子不行还是域不行」
不可分辨:

  · `DIE − COV` = **纯排序差**(同一批股票, 只换谁排前面)
  · `COV − BASE` = **纯覆盖域差**(同一个模型, 只把域缩到两融标的)

⚠️ 这一条写在跑之前, 不是看到结果之后补的。

## 「die 域」的精确定义(不留解释空间)

对每个评分月 m:

    域(m) = { model_score 在 m 月的全部 ts_code } ∩ { margin_net_share 在 m 月非 NaN 的 ts_code }

前一个集合 = 生产 `tradable_mask` 的输出。**依据**: `trainer.py:507` 先
`ok = tradable_mask(d)`, 再 `X_d = X_d[ok.reindex(X_d.index).fillna(False)]`,
写库的 `out` 用的就是 `X_d.index`(`trainer.py:577`)。⇒ 用基线自己的表覆盖域当域,
**不重写 mask = 不会漂移**, 且自动继承「训练域 = 选股域」这条既有纪律。
后一个集合 = 该因子的天然覆盖域。两臂写的是**完全相同的 (ts_code, trade_date) 格集合**,
只有 `score` 列不同。

## 全窗口(`20170101` 起)的分数起点 —— 必须重算

`alpha_bank` 里的值面板只到 2019-01(工厂的 `EVAL_SINCE`)。直接拿它跑 full 窗会让
2017-01~2018-12 两年**无分**(引擎静默跳过该期 ⇒ die 臂空仓两年)——那不是实验, 是错配。
故用同一个 `ff.compute_flow_factors` 在更早的行情面板(2016-01 起)上**重算**
`margin_net_share`。它无滚动窗(`(rzmre−rzche)/(amount·1e3)`, 逐格同日的量),
`neutralize` 又是逐截面的 ⇒ 面板变长不应改变任何一格。

**保真前提(abort 条件, 先于任何回测)**: 重算面板与 `alpha_bank` 已登记的值面板,
在**重叠月份上必须逐位相同**(`rtol=0, atol=0`)。不一致 ⇒ **停**, 先查清为什么,
不跑回测。(若真变了, 说明我对该因子的理解有错, 那份理解不能拿去判决。)

### ⚠️ 上述 abort 条件第一次就触发了 —— 记录全过程(2026-09-11, 仍未跑任何回测)

第一次跑就报 `2019-02-28 000009.SZ 重算=-0.10530440509319305 已登记=-0.10530439764261246`,
差 **7.45e-9 = float32 在 0.1 附近的 1 个 ulp**。逐层拆开后确认**不是语义差**:

| 检查 | 读数 | 结论 |
|---|---|---|
| 因子本体 `raw`(neutralize 之前)重叠格 | **0 / 5,129,664 不同** | 重算的因子与原版**逐位相同** |
| `neutralize` 之后重叠格 | 242,228 / 5,129,664 不同, 全部 ≤2 ulp | 差异**全部产生于 `neutralize` 内部** |
| 换回工厂窗(5806 列)重算 | **0 不同**(逐位相同) | 差异由**面板宽度**引起 |
| 合成面板: 同一批行、600 行 vs 300 行 | **0 不同** | `neutralize` 对**行数不敏感** |
| 长面板在 `neutralize` **之前**把列集对齐到工厂的列集 | **0 不同** | ✅ 修复 |

**机制**: 长面板多出 3 列(`000033.SZ` / `300372.SZ` / `600656.SH`, 来自 `margin` 表、
只在 2017-11 之前有行, 在 `raw` 里**全场 NaN**)。这 3 列自身没有值, 但把 frame 宽度
从 6853 改成 6856 ⇒ `neutralize` 里 `df.T.groupby(inds).transform("mean")` 这条
**float32 累加路径的分块方式跟着变** ⇒ 每个截面被平移 1~2 个 ulp。
**即: `fe.neutralize` 在 float32 下不是宽度不变的。** 这是个容易踩的坑 ——
「换个面板窗口重算因子」会得到 1~2 ulp 不同的值, 与数据无关。

**处置**: 修的是**面板构造**(在 `neutralize` 之前把列集对齐到工厂那次跑的列集),
**不是判据** —— 预注册的「逐位相同」一个字没改, 现在原样通过。
整个诊断与修复发生在**跑任何回测之前**, 没有看过任何组合层读数。

## 出口声明

两轴全过 **≠** 可接生产。按既有预注册, 下一步是独立的 walk-forward 验证
(有效样本只有 24 个月 —— 超随机 edge +13.15pp 全部来自 2024-25)。

用法:
    ./.venv/Scripts/python.exe scripts/oneoff/exp_stage_b_flow_die.py --build-only
    ./.venv/Scripts/python.exe scripts/oneoff/exp_stage_b_flow_die.py
"""
import argparse
import io
import json
import sys
import time
from pathlib import Path

# ⚠️ 必须留住原 wrapper 的引用: io.TextIOWrapper(sys.stdout.buffer, ...) 之后,
# 旧 TextIOWrapper 一旦被 GC 就会**关掉底层 buffer**(症状=日志文件正常, 控制台全是
# "I/O operation on closed file")。2026-09-11 在 exp_die_weekly.py 上踩过。
if hasattr(sys.stdout, "buffer"):
    _ORIG_STDOUT = sys.stdout
    sys.stdout = io.TextIOWrapper(_ORIG_STDOUT.buffer, encoding="utf-8", errors="replace")

BASE = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE))

import numpy as np                                        # noqa: E402
import pandas as pd                                       # noqa: E402

from app import db                                        # noqa: E402
from app.alpha import bank                                # noqa: E402
from app.backtest import engine as E                      # noqa: E402
from app.factors import factor_engine as fe               # noqa: E402
from app.factors import flow_factors as ff                # noqa: E402
from app.logger import setup_logger                       # noqa: E402

ALPHA = "margin_net_share"
DIE_T = "model_score_die_margin_net_share"
COV_T = "model_score_margincov_ctl"
BASE_T = "model_score"
MKT_SINCE = "20160101"          # 只需覆盖 full 窗首月 2017-01-26 即可
WINDOWS = [("prod", "20190722", None), ("full", "20170101", None)]
_DDL = """CREATE TABLE IF NOT EXISTS {} (
    ts_code TEXT, trade_date TEXT, score REAL, pred_ret REAL,
    PRIMARY KEY (ts_code, trade_date)) WITHOUT ROWID"""


# ---------------------------------------------------------------------------
# 第一步: 重算因子 + 保真校验 + 落三张表
# ---------------------------------------------------------------------------
def build_tables() -> bool:
    """返回 True 表示建成, False 表示保真校验失败(调用方必须中止)。"""
    m = fe.load_market(since=MKT_SINCE)
    dates_all = pd.to_datetime(m["close"].index)
    print("行情面板 {} 天 × {} 只".format(len(dates_all), m["close"].shape[1]), flush=True)

    raw = ff.compute_flow_factors(m, dates_all, include_research=False)[ALPHA]
    conn = db.get_conn()
    ind = fe.industry_map(conn)
    conn.close()

    # ---- 保真前提的第 0 步: 在 neutralize **之前**把列集对齐到工厂那次跑的列集 ----
    # `neutralize` 在 float32 下对**面板宽度**敏感(见模块 docstring 的完整诊断):
    # 长面板多出的 3 列是 margin 表里 2017-11 前才有的死列(全场 NaN), 自身无值, 但会把
    # df.T.groupby(...).transform("mean") 的 float32 累加分块改掉 ⇒ 每个截面平移 1~2 ulp。
    # 对齐列集后重叠月份**逐位相同**; 不对齐则 12,027 格差 1~2 ulp。
    old = bank.load_values(ALPHA)
    keep = [c for c in raw.columns if c in set(old.columns)]
    dropped = [c for c in raw.columns if c not in set(old.columns)]
    if dropped:
        print("列集对齐: 丢弃 {} 个工厂面板没有的列 {} (其中非 NaN {} 格)".format(
            len(dropped), dropped,
            int(raw[dropped].notna().sum().sum())), flush=True)
    raw = raw.loc[:, keep]

    neu = fe.neutralize(raw, ind).astype("float64")
    md = fe.month_end_dates(dates_all)
    md = md[md >= pd.Timestamp("2017-01-01")]
    wide = neu.loc[neu.index.isin(md)]
    print("重算面板: {} 个月末截面 ({} ~ {}), 平均非 NaN {:.1f} 只".format(
        len(wide), wide.index[0].date(), wide.index[-1].date(),
        wide.notna().sum(axis=1).mean()), flush=True)

    # ---- 保真前提(abort 条件, 判据原文未改) ----
    common_d = wide.index.intersection(old.index)
    common_c = wide.columns.intersection(old.columns)
    a = wide.loc[common_d, common_c].to_numpy(dtype="float64")
    b = old.loc[common_d, common_c].to_numpy(dtype="float64")
    both = ~(np.isnan(a) & np.isnan(b))
    bad = both & ~np.isclose(a, b, rtol=0.0, atol=0.0, equal_nan=True)
    print("保真校验: 重叠 {} 月 × {} 只 | 双方有值的格 {} | 逐位不同 {} 格".format(
        len(common_d), len(common_c), int(both.sum()), int(bad.sum())), flush=True)
    if bad.sum():
        i, j = np.argwhere(bad)[0]
        print("❌ 保真校验失败: {} {} 重算={!r} 已登记={!r} —— 停下, 不跑回测".format(
            common_d[i].date(), common_c[j], a[i, j], b[i, j]), flush=True)
        return False
    if not len(common_d):
        print("❌ 重算面板与已登记面板零重叠 —— 无法证明口径一致, 停下", flush=True)
        return False
    print("✓ 保真校验通过(逐位相同) —— 重算面板与工厂登记值是同一个东西", flush=True)

    # ---- 基线表(取 ts_code/score/pred_ret) ----
    conn = db.get_conn()
    bs = pd.read_sql(
        "SELECT ts_code, trade_date, score, pred_ret FROM {} WHERE trade_date >= ?".format(BASE_T),
        conn, params=("20170101",))
    conn.close()
    bs["trade_date"] = bs["trade_date"].astype(str)

    # ---- 逐月取交集域, 两臂写完全相同的格集合 ----
    die_rows, cov_rows, stats = [], [], []
    for d in wide.index:
        key = d.strftime("%Y%m%d")
        bm = bs[bs["trade_date"] == key]
        if bm.empty:
            continue                                   # 该月基线无分 ⇒ 不构造(两臂一致缺席)
        f = wide.loc[d]
        covered = set(f.index[f.notna()])
        uni = [c for c in bm["ts_code"] if c in covered]
        if not uni:
            continue
        bmap = bm.set_index("ts_code")
        bmap = bmap[bmap.index.isin(uni)]
        die_rows.append(pd.DataFrame({
            "ts_code": bmap.index, "trade_date": key,
            "score": f.reindex(bmap.index).astype("float64").values,
            "pred_ret": f.reindex(bmap.index).astype("float64").values}))
        cov_rows.append(pd.DataFrame({
            "ts_code": bmap.index, "trade_date": key,
            "score": bmap["score"].astype("float64").values,
            "pred_ret": bmap["pred_ret"].astype("float64").values}))
        stats.append((key, len(bm), len(uni)))

    die = pd.concat(die_rows, ignore_index=True)
    cov = pd.concat(cov_rows, ignore_index=True)
    st = pd.DataFrame(stats, columns=["trade_date", "base_n", "uni_n"])
    print("域统计: {} 期 | 基线平均 {:.0f} 只 → die 域平均 {:.0f} 只 ({:.1%} 保留)".format(
        len(st), st["base_n"].mean(), st["uni_n"].mean(),
        st["uni_n"].sum() / st["base_n"].sum()), flush=True)
    for w, s, _ in WINDOWS:
        k = st[st["trade_date"] >= s]
        if len(k):
            print("  {} 窗({} 起): {} 期, 基线均 {:.0f} → 域均 {:.0f} ({:.1%})".format(
                w, s, len(k), k["base_n"].mean(), k["uni_n"].mean(),
                k["uni_n"].sum() / k["base_n"].sum()), flush=True)

    assert set(map(tuple, die[["ts_code", "trade_date"]].values)) == \
           set(map(tuple, cov[["ts_code", "trade_date"]].values)), "两臂格集合不一致"
    print("✓ 两臂格集合逐格相同({} 格) —— 唯一变量是 score 列".format(len(die)), flush=True)

    conn = db.get_conn()
    try:
        for t in (DIE_T, COV_T):
            conn.execute(_DDL.format(t))
            conn.execute("DELETE FROM {}".format(t))
        conn.commit()
        db.upsert_df(conn, die, DIE_T)
        db.upsert_df(conn, cov, COV_T)
        for t, df_ in ((DIE_T, die), (COV_T, cov)):
            db.set_status(conn, t, df_["trade_date"].max(), len(df_))
    finally:
        conn.close()
    print("✓ 已落表 {} ({} 行) / {} ({} 行)".format(DIE_T, len(die), COV_T, len(cov)),
          flush=True)
    return True


# ---------------------------------------------------------------------------
# 第二步: 三臂 × 两窗
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--build-only", action="store_true", help="只建表+保真校验, 不跑回测")
    ap.add_argument("--tag", default="flowdie_b")
    ap.add_argument("--skip-build", action="store_true")
    args = ap.parse_args()

    setup_logger()
    db.init_db()

    if not args.skip_build:
        print("=" * 100, flush=True)
        print("=== 第一步: 重算 {} + 保真校验 + 落表 ===".format(ALPHA), flush=True)
        if not build_tables():
            print("\n**中止: 保真校验未通过 —— 按预注册不跑回测**", flush=True)
            sys.exit(1)
        if args.build_only:
            print("\n(--build-only: 建表完成, 未跑回测)", flush=True)
            return

    print("\n" + "=" * 100, flush=True)
    print("阶段 B 判决: {} | 主判 = DIE vs BASE (4 不等式) | COV 仅归因".format(DIE_T),
          flush=True)

    out, tags = [], []
    t0 = time.time()
    for wname, start, end in WINDOWS:
        for arm, table in (("base", BASE_T), ("cov", COV_T), ("die", DIE_T)):
            mt = E.run_backtest(score_table=table,
                                run_tag="{}_{}_{}".format(args.tag, arm, wname),
                                start=start, end=end)
            tags.append(mt["run_date"])
            row = {"arm": arm, "win": wname, "run": mt["run_date"], "n": mt.get("periods"),
                   "sys_ann": mt["sys_ann_return"], "sys_mdd": mt["sys_max_drawdown"],
                   "sys_mdd_daily": mt["sys_mdd_daily"], "idle": mt["avg_cash_idle"],
                   "n_avg": mt["avg_stocks"], "sharpe": mt.get("sharpe"),
                   "full_ann": mt.get("ann_return"), "full_mdd": mt.get("max_drawdown")}
            out.append(row)
            print("[{:>5.0f}s] {}".format(time.time() - t0,
                                          json.dumps(row, ensure_ascii=False)), flush=True)

    print("\n" + "=" * 104)
    print("=== 预注册主判据: **两个窗口都要** DIE 两轴不劣于同窗 BASE(4 个不等式全成立) ===")
    verdict = True
    for wname, _, _ in WINDOWS:
        r = {x["arm"]: x for x in out if x["win"] == wname}
        b, c, a = r["base"], r["cov"], r["die"]
        d_ann = (a["sys_ann"] - b["sys_ann"]) * 100
        d_mdd = (a["sys_mdd"] - b["sys_mdd"]) * 100
        ok = (a["sys_ann"] >= b["sys_ann"]) and (a["sys_mdd"] >= b["sys_mdd"])
        verdict = verdict and ok
        print("\n-- {} ({} 期) --".format(wname, b["n"]))
        for tag, x in (("BASE 生产域", b), ("COV  die域·生产分", c), ("DIE  die域·该因子", a)):
            print("  {:20s} 系统年化 {:7.2f}%  回撤(月) {:7.2f}%  回撤(日) {:7.2f}%  "
                  "满仓年化 {:7.2f}%  满仓回撤 {:7.2f}%  闲置 {:5.1%}  只数 {:.2f}  夏普 {:+.3f}"
                  .format(tag, x["sys_ann"] * 100, x["sys_mdd"] * 100, x["sys_mdd_daily"] * 100,
                          x["full_ann"] * 100, x["full_mdd"] * 100, x["idle"], x["n_avg"],
                          x["sharpe"] or float("nan")))
        print("  ⇒ **DIE − BASE** Δ年化 {:+.2f}pp | Δ回撤 {:+.2f}pp | 本窗 {}".format(
            d_ann, d_mdd, "PASS" if ok else "**否**"))
        print("     (归因, 非判据) DIE − COV(纯排序) Δ年化 {:+.2f}pp | Δ回撤 {:+.2f}pp"
              .format((a["sys_ann"] - c["sys_ann"]) * 100, (a["sys_mdd"] - c["sys_mdd"]) * 100))
        print("     (归因, 非判据) COV − BASE(纯缩域) Δ年化 {:+.2f}pp | Δ回撤 {:+.2f}pp"
              .format((c["sys_ann"] - b["sys_ann"]) * 100, (c["sys_mdd"] - b["sys_mdd"]) * 100))

    print("\n" + "=" * 104)
    print("**总判决: {}**".format(
        "PASS —— 有资格进入下一步(walk-forward 独立验证)"
        if verdict else "**否 —— die '{}' 不封装**".format(DIE_T)))
    print("run 已保留(不自清), TAGS = {}".format(tags))
    print("读法: 两轴全过 ≠ 可接生产; 且 COV 不是第二次机会(预注册明写)。")


if __name__ == "__main__":
    main()
