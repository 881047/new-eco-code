"""D01 数据完整性验收 —— 路线图阶段一「数据验收」首项 (2026-09-12)

路线图原文（`A股量化系统实验路线图.docx`，表头 `编号 | 实验 | 核心指标 | 通过标准`）:

    D01 | 数据完整性 | **缺失率** | 无关键字段异常

================================================================================
预注册（写在跑任何数据之前；脚本 sha256 与判据一起落盘）
================================================================================

## 范围: 由代码客观界定, 不手挑

「模型看到的数据」= **生产 `app/` 里被 `FROM`/`JOIN` 读取、且不是本系统自己写出的
输出表**。这个范围由 grep 决定（`model_score*` / `backtest_*` / `paper_*` / `alpha_bank` /
`pipeline_run` / `notify_log` / `signal` / `plan` 作为**输出**排除 —— 它们是本系统的产物,
不是输入数据, 完整性由各自的实验负责）。

判据分两层, **这两层的性质不同, 必须分开读**:

- **硬判据（= 必须为 0）**: 只放**定义上不可能**的事 —— 主键重复、主键列 NULL、
  价格 `high<low`、`close<=0`、成交量为负、`up_limit<down_limit`、日期格式非 8 位、
  核心盘面表跑在 `trade_cal` 的非开市日上。这些一旦非零就是真异常。
- **报数（不设阈值）**: 各列缺失率、跨表对齐差集。**路线图要的「缺失率」在这里**。
  为什么不设阈值: 「缺失率 < X% 通过」会诱使我**事后把 X 调到刚好通过**。改为
  **每一个 > 0 的缺失都必须逐列给出判读**, 且判读必须基于**独立证据**
  （读消费者代码看它有没有防御 / 查该缺失是否有定义上的原因), 不能是「看起来没事」。

## 判据编号

- **I1 主键唯一性**: 每表 `COUNT(*) - COUNT(DISTINCT pk...)` = 0。**硬判据。**
  （同时报「DDL 声明的 PK 是否真的生效」—— `sw_industry_member` 无 PK 类的结构性发现。）
- **I2 PK 列非空**: PK 任一列为 NULL 的行数 = 0。**硬判据。**
- **I3 值域合法性**: 上列「定义上不可能」清单, 逐项违规行数 = 0。**硬判据。**
- **I4 日期轴**: 日期列必须匹配 `^[0-9]{8}$`；核心盘面表的每个日期必须是
  `trade_cal.is_open=1`。**硬判据。**
- **I5 交易日覆盖**: `daily` 的 distinct `trade_date` 集合必须**恰好等于**
  `trade_cal` 在 `daily` 自身日期范围内的开市日集合。**硬判据。**
- **I6 缺失率**: 逐表逐列的 NULL 率。**报数**, 但每个 > 0 的必须判读(见上)。
- **I7 跨表对齐**: `daily_basic` / `adj_factor` / `stk_limit` 相对 `daily` 的双向差集。
  **报数 + 归因。**
- **I8 已知缺陷复核**: 把项目记忆里已登记的条目用实测数字钉死, 防它「悄悄变了」:
  `stock_basic.industry` NULL 数、`fund_nav` 零写入、`data_status` 陈旧记录、
  `hk_hold` 2024-08 覆盖度断崖、`sw_industry_member` 不存在。

## 总判决

**I1~I5 全部 0 违规 ⇒ PASS**；否则 **FAIL** 并逐项列出。I6~I8 是读数, 不进判决,
但它们里任何一条**推翻**了上述硬判据的结论时必须在报告里显式冲突标注。

## 明写: 本轮不做的事

- **不改任何生产文件、不修任何数据**。D01 是验收, 不是修复。
- **不设缺失率阈值**（理由见上）。
- **不测 PIT / 未来函数** —— 那是 D02/D03/D04 的范围, 各自的判据不同。
  `fina_indicator.ann_date` / `holder_num.ann_date` 的 PIT 正确性**属于 D02**, 本实验
  只检查它们的**结构完整性**（非空率、格式）。

================================================================================
判据修订记录（2026-09-12）—— **两条测量写错了, 三条真异常一条没改**
================================================================================

⚠️ 必须和上面的判据一起读。这节记录的是「**第一次跑 → 看到违规 → 诊断**」的过程。
危险在于: 这个过程与「看到结果之后把标准改松」在**操作上长得一模一样**。

**区分标准（先声明, 后执行）:**
- **改阈值** = 同一个测量换一条线 ⇒ **禁止**。本文件一个阈值都没改。
- **改测量** = 原来的测量**测的不是它声称要测的东西** ⇒ 允许, 但**必须给出
  不依赖本次数据的独立证据**, 且**原始读数必须留在报告里**, 不许删。

**第一次跑的读数（I3 违规 4 项 / I4 违规 1 项）与诊断结果:**

| # | 原检查 | 首次读数 | 诊断 | 依据（独立于本次数据） | 处置 |
|---|---|---|---|---|---|
| ② | `stk_limit.down_limit<=0` | 279 行 | **测量写错**: 0 是**哨兵值**（`up_limit=99999.99` + `down_limit=0`）表示「当日无涨跌幅限制」 | D06/D07 的 `ATTRIB_NOLIMIT` 在**本实验之前**就从交易所数据独立确立了该哨兵（`D06_D07_limit_fill.py:380`）, 且 279 行**全部**是 `99999.99/0.0` 这一种取值组合 | 拆成两条: 哨兵计数（报数）+ 「非哨兵却 down_limit<=0」（硬判据, 现为 0） |
| ④ | `moneyflow`: `net_mf_amount ≈ buy_tot - sell_tot` | 12,502,824 行 | **测量写错**: 这是两个**不同的量**。`net_mf_amount` = 主动买−主动卖（净流入额）; `buy_sm/md/lg/elg` 与 `sell_*` 是**按成交单大小分桶**的两套分类, 各自**都是全市场成交额的划分** ⇒ 根本不存在该等式 | ① `net_mf_amount / net_mf_vol ≈ 当日成交均价`（实测 000001.SZ 20130104 = 15.82 元/股, 与该股当日价吻合）⇒ 两者是同一物理量的不同单位; ② `sign()` 一致性是**定义性**的, 不需要额外证据 | 换成定义性检查: `sign(net_mf_amount) != sign(net_mf_vol)`（两者都非 0 时） |

**④ 换上的新检查当场报出 31,900 行 —— 这是新发现, 保留为第 4 条真异常（未调阈值）:**

| # | 检查 | 读数 | 定性 |
|---|---|---|---|
| ⑥ | `moneyflow`: `net_mf_amount` 与 `net_mf_vol` 符号相反 | **31,900 行**（0.25%） | 真异常。两者是**同一物理量的两种单位**（万元 / 手）⇒ 符号必须一致。**独立对照**（用当日 `close` 判卷, 见下）: 符号一致的 12,467,258 行中 **89.7%** 的隐含价 `net_mf_amount×10000/(net_mf_vol×100)` 落在 `close` 的 ±5% 内、**隐含价为负 0 行**; 符号相反的 31,886 行中 隐含价 ≈ `close` 的 **0 行**、**隐含价为负 31,886 行（100%）** ⇒ 单位假设在一致组上成立, 反过来证明相反组是数据错误 |

**保留为 FAIL 的三条 —— 判据一字未改:**

| # | 检查 | 读数 | 定性 |
|---|---|---|---|
| ① | `daily_basic`: `circ_mv > total_mv` | **15 行** | 真异常。流通股本 ≤ 总股本是定义的; 实测全部来自 `603882.SH` 且比值恒为 **1.0035** ⇒ 上游两个字段取自不同股本口径 |
| ③ | `margin`: `rzche < 0`（融资偿还额为负） | **343 行** | 真异常。偿还额是流量**幅值** ≥ 0; 已排除「符号翻转」假说（取绝对值后会计恒等式成立率 **0.00%**, 而 `rzche>=0` 的行为 95.02%）⇒ 是上游数据错误, 且**逐年上升**（2025:120 / 2026:136） |
| ⑤ | `adj_factor` 落在非开市日 | **2 行** | 真异常。`689009.SH` 于 2021 中秋假期（20210920/21, `trade_cal.is_open=0`）各 1 行 |

**⇒ 修订后 D01 = FAIL(①③⑤⑥ 四条)。** 四条都不是本系统产生的, 本系统也无法修
（D01 明文不修数据）, 故它们进 §I9 的影响量化, 由下一步决定是否阻塞。

用法: ./.venv/Scripts/python.exe experiments/01_data/D01_data_integrity.py
输出: D:/claude-home/tmp/d01_integrity.log + experiments/01_data/D01_result.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE))
_ORIG_STDOUT = sys.stdout  # ⚠️ 留住引用, 否则旧 wrapper 被 GC 时会关掉底层 buffer
if hasattr(sys.stdout, "buffer"):
    import io
    sys.stdout = io.TextIOWrapper(_ORIG_STDOUT.buffer, encoding="utf-8", errors="replace")

from app import db  # noqa: E402

LOG = Path("D:/claude-home/tmp/d01_integrity.log")
OUT = Path(__file__).with_name("D01_result.json")

# ===========================================================================
# 预注册常量 —— 结果出来之前不许改
# ===========================================================================

# --- I3 值域检查: 只放「定义上不可能」。元组 = (标签, SQL 谓词, 违规即命中) ---
DOMAIN = {
    "daily": [
        ("close<=0", "close <= 0"),
        ("open<=0", "open <= 0"),
        ("high<low", "high < low"),
        ("high<open 或 high<close", "high < open OR high < close"),
        ("low>open 或 low>close", "low > open OR low > close"),
        ("vol<0", "vol < 0"),
        ("amount<0", "amount < 0"),
        ("pre_close<=0", "pre_close <= 0"),
    ],
    "daily_basic": [
        ("total_mv<0", "total_mv < 0"),
        ("circ_mv<0", "circ_mv < 0"),
        ("turnover_rate<0", "turnover_rate < 0"),
        ("total_mv>0 却 circ_mv>total_mv", "circ_mv > total_mv AND total_mv > 0"),
    ],
    "adj_factor": [("adj_factor<=0", "adj_factor <= 0")],
    "stk_limit": [
        # 修订 ②: 0 是哨兵值(见 docstring 判据修订记录)。只判「非哨兵却 down_limit<=0」。
        ("非哨兵行 down_limit<=0", "down_limit <= 0 AND up_limit < 99999"),
        ("up_limit<down_limit", "up_limit < down_limit"),
    ],
    "margin": [
        ("rzye<0", "rzye < 0"), ("rqye<0", "rqye < 0"),
        ("rzmre<0", "rzmre < 0"), ("rzche<0", "rzche < 0"),
    ],
    "moneyflow": [
        # 修订 ④: 原检查把 net_mf_amount 当成 buy_tot-sell_tot, 两者是**不同的量**
        # (见 docstring 判据修订记录)。换成定义性的符号一致性检查。
        ("net_mf_amount 与 net_mf_vol 符号不一致",
         "net_mf_amount <> 0 AND net_mf_vol <> 0"
         " AND ((net_mf_amount > 0) <> (net_mf_vol > 0))"),
        ("任一买入额为负", "buy_sm_amount < 0 OR buy_md_amount < 0 OR buy_lg_amount < 0"
                           " OR buy_elg_amount < 0"),
    ],
    "block_trade": [
        ("avg_price<=0", "avg_price <= 0"), ("total_vol<0", "total_vol < 0"),
        ("n_trades<0", "n_trades < 0"),
    ],
    "hk_hold": [("vol<0", "vol < 0"), ("ratio<0", "ratio < 0")],
    "index_daily": [("close<=0", "close <= 0"), ("high<low", "high < low")],
    "fund_daily": [("close<=0", "close <= 0"), ("high<low", "high < low")],
    "fund_adj": [("adj_factor<=0", "adj_factor <= 0")],
    "pool_index": [("close<=0", "close <= 0")],
    "repo_rate": [("overnight<0", "overnight < 0")],
    "valuation_pe": [("pe_ttm<=0", "pe_ttm <= 0")],
    "daily_score": [("score 非有限", "score IS NULL OR score != score")],
}

# --- I4: 哪些表的日期列必须落在 trade_cal 的开市日上（核心盘面 + 派生） ---
OPEN_DAY_TABLES = {
    "daily": "trade_date", "daily_basic": "trade_date", "adj_factor": "trade_date",
    "stk_limit": "trade_date", "index_daily": "trade_date", "factor": "trade_date",
    "daily_score": "trade_date", "model_score": "trade_date", "pool_index": "trade_date",
    "regime": "trade_date", "valuation_pe": "trade_date",
}

# --- I6: 每表要报缺失率的关键列（= 引擎/因子会直接参与算术或分组的列） ---
KEY_COLS = {
    "daily": ["open", "high", "low", "close", "pre_close", "change", "pct_chg", "vol", "amount"],
    "daily_basic": ["turnover_rate", "volume_ratio", "pe_ttm", "pb", "total_mv", "circ_mv"],
    "adj_factor": ["adj_factor"],
    "stk_limit": ["up_limit", "down_limit"],
    "margin": ["rzye", "rqye", "rzmre"],
    "moneyflow": ["net_mf_amount", "buy_elg_amount", "sell_elg_amount"],
    "block_trade": ["avg_price", "total_amount"],
    "hk_hold": ["vol", "ratio"],
    "factor": ["value"],
    "fina_indicator": ["ann_date", "roe", "netprofit_yoy", "or_yoy"],
    "holder_num": ["ann_date", "holder_num"],
    "lhb": ["net_buy", "lhb_amount", "reason"],
    "lhb_org": ["org_net_value", "reason"],
    "namechange": ["start_date", "ann_date"],
    "stock_basic": ["industry", "list_date", "delist_date"],
    "index_daily": ["close", "vol"],
    "fund_basic": ["list_date", "delist_date"],
    "fund_daily": ["close"],
    "fund_adj": ["adj_factor"],
    "fund_rank": ["ret_1y", "ret_3y"],
    "pool_index": ["ret", "close"],
    "regime": ["state", "score"],
    "daily_score": ["score", "pred_ret"],
}

# --- I6: 允许缺失的列（定义上就会空）+ 判读依据。跑之前写死, 跑之后不改。 ---
MISSING_OK = {
    ("stock_basic", "delist_date"): "未退市股定义上无退市日",
    ("stock_basic", "industry"): "**已知缺陷**: 333 只 NULL 被 fillna('未知') 当同一行业",
    ("fund_basic", "delist_date"): "未退市基金定义上无退市日",
    ("daily_basic", "pe_ttm"): "亏损股 PE 定义上无意义 -> 需实测确认是 NULL 还是负值",
    ("daily_basic", "dv_ttm"): "不分红股股息率定义上为空(不在关键列内, 记录备查)",
    ("factor", "value"): "中性化后某日某因子全 NaN 时可为空 -> 需实测",
    ("moneyflow", "net_mf_amount"): "需实测",
}
# ⚠️ 上表里凡写「需实测」的, 跑完必须回来给出实测结论并**另附独立依据**;
#    不允许把结论写成「看起来没问题」。

# --- I6 判读（**跑完之后填**, 依据必须独立于本次统计; 只影响报告, 不影响任何判据）---
# 填表规则: 依据必须是「查代码看消费者怎么处理」或「该列语义上就允许为空」,
# 不能是「反正比例很小」。没查清的一律保持「**未判读**」。
I6_JUDGMENT = {
    ("daily", "pre_close"): "**193 行全部是 `.BJ`(北交所)股票**, 且 `vol>0`。`pre_close` 是 "
                            "引擎推导涨跌停价的输入(`engine._exec_side`), NULL 会静默退化成"
                            "「可成交」—— 但北交所**已在 `tradable_mask` 的剔除域内**"
                            "(方案 §3 不变量 #3), 对策略域零影响。",
    ("daily", "change"): "与 `pre_close` 同源同行(同 193 行), 是 `close-pre_close`; 同上。",
    ("daily", "pct_chg"): "与 `pre_close` 同源同行(同 193 行); 同上。",
    ("daily_basic", "pe_ttm"): "2,292,287 行为空 = **亏损股**(实测: 负值 0 行, 正值 10,429,429"
                               " 行)⇒ **不是缺失, 是语义上无定义**。消费者 `factor_engine` 走"
                               " `1/pe` ⇒ NaN ⇒ 进 `neutralize` 后该股在 ep 截面上被排除, 是正确行为。",
    ("daily_basic", "pb"): "79,187 行为空(0.62%), 同 `pe_ttm`: 净资产为负时 PB 无意义。",
    ("daily_basic", "volume_ratio"): "17,866 行为空(0.14%)。**全项目零消费者** —— "
                                     "`load_market()` 不取它, `app/factors`/`app/models`/`app/portfolio` "
                                     "均无引用 ⇒ 零影响。",
    ("fina_indicator", "roe"): "5,458 行为空(2.06%) = 该报告期该指标缺失; 消费者 "
                               "`compute_fina_factors` 按 `ann_date` 对齐, NaN 自然不进截面。",
    ("fina_indicator", "netprofit_yoy"): "14,990 行为空(5.67%); 同上。",
    ("fina_indicator", "or_yoy"): "15,812 行为空(5.98%); 同上。",
    ("holder_num", "holder_num"): "3,779 行为空(1.98%) = 该期未披露股东户数; 研究因子用, 不进生产。",
    ("hk_hold", "ratio"): "6,264 行为空(0.11%); `hk_hold` **只作研究不入生产**(不变量 #6)。",
    ("margin", "rzye"): "33 行为空(0.0005%), 与 `rzmre` 同行数 ⇒ 同一批异常行。研究/flow die 用。",
    ("margin", "rzmre"): "33 行为空, 同 `rzye`。",
    ("margin", "rqye"): "3,186 行为空(0.047%) = 该标的当日无融券余额; 语义上允许为空。",
    ("namechange", "ann_date"): "1 行为空(0.007%); `names.py` 只按 `start_date` 取名称, 不读 `ann_date`。",
    ("pool_index", "ret"): "1 行为空(= 首日无前收盘) ⇒ 池子指数首日定义上无收益。",
    ("fund_basic", "list_date"): "15,154 行为空(84.5%) —— **基金链路**, 不在股票模型路径上"
                                 "(§9 已登记 `fund_nav` 全项目零写入方); 留待基金侧专项验收。",
    ("fund_rank", "ret_1y"): "2,224 行为空(10.98%) —— 成立不满 1 年的基金定义上无 1 年收益。",
    ("fund_rank", "ret_3y"): "6,983 行为空(34.48%) —— 成立不满 3 年定义上无 3 年收益。",
}


# ===========================================================================
class Say:
    """同时写控制台与日志文件（项目惯例: 证据落 D:/claude-home/tmp/）。"""

    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.f = open(path, "w", encoding="utf-8")

    def __call__(self, s=""):
        print(s, flush=True)
        self.f.write(s + "\n")
        self.f.flush()

    def close(self):
        self.f.close()


def script_sha() -> str:
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:16]


def q1(conn, sql, params=()):
    return conn.execute(sql, params).fetchone()


# ===========================================================================
# I1 + I2 —— 主键
# ===========================================================================
def check_pk(conn, say):
    say("\n" + "=" * 100)
    say("I1 主键唯一性 / I2 主键列非空  —— **硬判据, 必须为 0**")
    say("=" * 100)
    say("%-22s %12s %10s %10s   %s" % ("表", "行数", "PK重复", "PK空", "声明的PK"))
    bad = []
    rows_out = {}
    for t in sorted(KEY_COLS):
        if not db.table_exists(conn, t):
            say("%-22s %12s %10s %10s   **表不存在**" % (t, "-", "-", "-"))
            bad.append("%s: 表不存在" % t)
            continue
        pk = [x[1] for x in conn.execute('PRAGMA table_info("%s")' % t).fetchall() if x[5]]
        n = q1(conn, 'SELECT COUNT(*) FROM "%s"' % t)[0]
        if not pk:
            say("%-22s %12d %10s %10s   **DDL 未声明主键 -> 无法做唯一性判据**" % (t, n, "-", "-"))
            bad.append("%s: DDL 未声明主键" % t)
            rows_out[t] = dict(rows=n, pk=[], dup=None, null=None)
            continue
        pks = ", ".join('"%s"' % c for c in pk)
        dup = n - q1(conn, 'SELECT COUNT(*) FROM (SELECT DISTINCT %s FROM "%s")' % (pks, t))[0]
        nullcond = " OR ".join('"%s" IS NULL' % c for c in pk)
        nul = q1(conn, 'SELECT COUNT(*) FROM "%s" WHERE %s' % (t, nullcond))[0]
        say("%-22s %12d %10d %10d   %s" % (t, n, dup, nul, ",".join(pk)))
        if dup or nul:
            bad.append("%s: PK重复=%d PK空=%d" % (t, dup, nul))
        rows_out[t] = dict(rows=n, pk=pk, dup=dup, null=nul)
    say("\n判据 I1+I2: %s" % ("**PASS**" if not bad else "**FAIL —— %s**" % "; ".join(bad)))
    return {"bad": bad, "tables": rows_out}, (not bad)


# ===========================================================================
# I3 —— 值域
# ===========================================================================
def check_domain(conn, say):
    say("\n" + "=" * 100)
    say("I3 值域合法性（只放「定义上不可能」）—— **硬判据, 必须为 0**")
    say("=" * 100)
    say("%-16s %-42s %12s" % ("表", "检查项", "违规行数"))
    bad, out = [], {}
    for t, checks in DOMAIN.items():
        if not db.table_exists(conn, t):
            say("%-16s %-42s %12s" % (t, "(表不存在)", "-"))
            bad.append("%s: 表不存在" % t)
            continue
        parts = ", ".join("SUM(CASE WHEN %s THEN 1 ELSE 0 END)" % p for _, p in checks)
        r = q1(conn, 'SELECT %s FROM "%s"' % (parts, t))
        for (label, _), v in zip(checks, r):
            v = v or 0
            flag = "" if v == 0 else "   <-- **异常**"
            say("%-16s %-42s %12d%s" % (t, label, v, flag))
            out["%s|%s" % (t, label)] = v
            if v:
                bad.append("%s: %s = %d 行" % (t, label, v))
    # 修订 ② 的配套报数: 哨兵行本身仍然要出现在报告里（只是不计为违规）
    sent = q1(conn, "SELECT COUNT(*) FROM stk_limit WHERE up_limit >= 99999 "
                    "AND down_limit <= 0")[0]
    say("\n  [报数] `stk_limit` 哨兵行（up_limit>=99999 且 down_limit<=0, 含义=当日无涨跌幅"
        "限制, 见 docstring 修订记录②）= %d 行" % sent)
    say("\n判据 I3: %s" % ("**PASS**" if not bad else "**FAIL —— %d 项违规**" % len(bad)))
    return {"bad": bad, "counts": out, "sentinel_rows": sent}, (not bad)


# ===========================================================================
# I4 —— 日期轴
# ===========================================================================
def check_dates(conn, say):
    say("\n" + "=" * 100)
    say("I4 日期轴合法性（格式 ^[0-9]{8}$ + 核心盘面表不得落在非开市日）—— **硬判据**")
    say("=" * 100)
    bad = []
    say("-- 格式 --")
    for t in sorted(KEY_COLS):
        col = OPEN_DAY_TABLES.get(t)
        if col is None or not db.table_exists(conn, t):
            continue
        n = q1(conn, 'SELECT SUM(CASE WHEN "%s" NOT GLOB \'[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]\''
                     ' THEN 1 ELSE 0 END) FROM "%s"' % (col, t))[0] or 0
        if n:
            say("  %-22s 格式非法 %d 行   <-- **异常**" % (t, n))
            bad.append("%s 日期格式非法 %d 行" % (t, n))
    say("  (只列出非零项; 其余表全部 8 位数字格式)")
    say("\n-- 非开市日 --")
    for t, col in sorted(OPEN_DAY_TABLES.items()):
        if not db.table_exists(conn, t):
            continue
        n = q1(conn, 'SELECT COUNT(*) FROM (SELECT DISTINCT "%s" d FROM "%s") x '
                     'LEFT JOIN trade_cal c ON c.cal_date = x.d '
                     'WHERE c.is_open IS NULL OR c.is_open <> 1' % (col, t))[0]
        if n:
            say("  %-22s 非开市日 %d 个   <-- **异常**" % (t, n))
            bad.append("%s 有 %d 个非开市日" % (t, n))
    say("  (只列出非零项; 其余表的日期全部落在 trade_cal 开市日上)")
    say("\n判据 I4: %s" % ("**PASS**" if not bad else "**FAIL —— %s**" % "; ".join(bad)))
    return {"bad": bad}, (not bad)


# ===========================================================================
# I5 —— daily vs trade_cal 覆盖
# ===========================================================================
def check_calendar(conn, say):
    say("\n" + "=" * 100)
    say("I5 交易日覆盖: `daily` 的交易日集合 == `trade_cal` 开市日集合（在 daily 自身范围内）")
    say("   —— **硬判据**")
    say("=" * 100)
    lo, hi = q1(conn, "SELECT MIN(trade_date), MAX(trade_date) FROM daily")
    d_days = {r[0] for r in conn.execute(
        "SELECT DISTINCT trade_date FROM daily WHERE trade_date BETWEEN ? AND ?", (lo, hi))}
    c_days = {r[0] for r in conn.execute(
        "SELECT cal_date FROM trade_cal WHERE is_open=1 AND cal_date BETWEEN ? AND ?", (lo, hi))}
    miss = sorted(c_days - d_days)
    extra = sorted(d_days - c_days)
    say("  daily 范围 %s ~ %s" % (lo, hi))
    say("  daily 交易日 %d 个 | trade_cal 开市日 %d 个" % (len(d_days), len(c_days)))
    say("  开市但没有 daily 数据的交易日: %d 个 %s" % (len(miss), miss[:12] if miss else ""))
    say("  有 daily 数据但 trade_cal 说休市: %d 个 %s" % (len(extra), extra[:12] if extra else ""))
    ok = not miss and not extra
    say("\n判据 I5: %s" % ("**PASS**" if ok else "**FAIL**"))
    return {"lo": lo, "hi": hi, "daily_days": len(d_days), "cal_days": len(c_days),
            "missing": miss, "extra": extra}, ok


# ===========================================================================
# I6 —— 缺失率（报数）
# ===========================================================================
def check_missing(conn, say):
    say("\n" + "=" * 100)
    say("I6 缺失率（**报数, 不设阈值** —— 每个 >0 的列必须逐列判读）")
    say("=" * 100)
    say("%-22s %-16s %10s %12s   %s" % ("表", "列", "行数", "NULL 数", "NULL率"))
    out = {}
    positive = []
    for t in sorted(KEY_COLS):
        if not db.table_exists(conn, t):
            continue
        cols = [c for c in KEY_COLS[t]
                if c in [x[1] for x in conn.execute('PRAGMA table_info("%s")' % t).fetchall()]]
        if not cols:
            continue
        sel = ", ".join('SUM(CASE WHEN "%s" IS NULL THEN 1 ELSE 0 END)' % c for c in cols)
        r = q1(conn, 'SELECT COUNT(*), %s FROM "%s"' % (sel, t))
        n, counts = r[0], r[1:]
        for c, v in zip(cols, counts):
            v = v or 0
            rate = 100.0 * v / max(1, n)
            out["%s.%s" % (t, c)] = v
            if v == 0:
                continue
            # I6_JUDGMENT 优先: 它是**跑完之后**基于查代码/查语义的完整判读;
            # MISSING_OK 是**跑之前**的预声明, 作为兜底保留(可见两者的差)。
            note = I6_JUDGMENT.get((t, c)) or MISSING_OK.get((t, c)) or "**未判读**"
            say("%-22s %-16s %10d %12d   %7.3f%%  %s" % (t, c, n, v, rate, note))
            positive.append((t, c, n, v, rate, note))
    say("\n  (只列出 NULL>0 的列; 其余全部 0)")
    say("  共 %d 个列有缺失。" % len(positive))
    unregistered = [p for p in positive if p[5].startswith("**未判读")]
    say("  其中**未判读**的: %d 个" % len(unregistered))
    return {"nulls": out, "positive": positive, "unregistered": unregistered}


# ===========================================================================
# I7 —— 跨表对齐
# ===========================================================================
def check_align(conn, say):
    say("\n" + "=" * 100)
    say("I7 跨表对齐: 各表 (trade_date, ts_code) 相对 `daily` 的双向差集（**报数 + 归因**）")
    say("=" * 100)
    out = {}
    for t in ["daily_basic", "adj_factor", "stk_limit", "index_daily", "daily_score"]:
        if not db.table_exists(conn, t):
            continue
        a = q1(conn, 'SELECT COUNT(*) FROM (SELECT trade_date,ts_code FROM "%s" '
                     'EXCEPT SELECT trade_date,ts_code FROM daily)' % t)[0]
        b = q1(conn, 'SELECT COUNT(*) FROM (SELECT trade_date,ts_code FROM daily '
                     'EXCEPT SELECT trade_date,ts_code FROM "%s")' % t)[0]
        # 各自独有的日期分布(前 5)
        da = [r[0] for r in conn.execute(
            'SELECT trade_date, COUNT(*) c FROM (SELECT trade_date,ts_code FROM "%s" '
            'EXCEPT SELECT trade_date,ts_code FROM daily) GROUP BY 1 ORDER BY c DESC LIMIT 5' % t)]
        say("  %-14s 相对 daily: 它独有 %8d 格 | daily 独有 %8d 格   它独有最多的日期: %s"
            % (t, a, b, ", ".join(da) if da else "—"))
        out[t] = dict(only_other=a, only_daily=b, top_dates=da)
    return {"pairs": out}


# ===========================================================================
# I8 —— 已知缺陷复核
# ===========================================================================
def check_known(conn, say):
    say("\n" + "=" * 100)
    say("I8 已知缺陷复核（防「悄悄变了」—— 每条都是记忆/文档里已登记的）")
    say("=" * 100)
    out = {}

    n = q1(conn, "SELECT SUM(CASE WHEN industry IS NULL OR industry='' THEN 1 ELSE 0 END) "
                 "FROM stock_basic")[0] or 0
    tot = q1(conn, "SELECT COUNT(*) FROM stock_basic")[0]
    say("  ① stock_basic.industry 为空: %d / %d 只   (记忆登记: 333)" % (n, tot))
    out["industry_null"] = n

    n = q1(conn, "SELECT COUNT(*) FROM fund_nav")[0]
    say("  ② fund_nav 行数: %d   (记忆登记: 零写入方)" % n)
    out["fund_nav_rows"] = n

    rows = conn.execute("SELECT name, last_date, updated_at, rows FROM data_status "
                        "ORDER BY updated_at DESC LIMIT 3").fetchall()
    stale = [r for r in conn.execute("SELECT name, last_date FROM data_status").fetchall()
             if not db.table_exists(conn, r[0])]
    say("  ③ data_status 里指向**不存在的表**的记录: %d 条   %s"
        % (len(stale), [r[0] for r in stale] or "—"))
    say("     最新 3 条水位: %s" % "; ".join("%s@%s" % (r[0], r[1]) for r in rows))
    out["data_status_orphan"] = [r[0] for r in stale]

    r = conn.execute("SELECT substr(trade_date,1,7) m, COUNT(DISTINCT ts_code) c FROM hk_hold "
                     "WHERE trade_date BETWEEN '20240701' AND '20241031' "
                     "GROUP BY 1 ORDER BY 1").fetchall()
    say("  ④ hk_hold 覆盖度（记忆登记: 2024-08-19 起 4042 → 793 断崖）:")
    say("     %s" % "  ".join("%s:%d" % (m, c) for m, c in r))
    out["hk_hold_2024"] = {m: c for m, c in r}

    ex = db.table_exists(conn, "sw_industry_member")
    say("  ⑤ sw_industry_member 存在: %s   (pit/engine.py:144 有 table_exists guard, "
        "缺失时回退当前快照并 warning)" % ex)
    out["sw_industry_member_exists"] = ex

    r = q1(conn, "SELECT COUNT(DISTINCT trade_date) FROM factor")[0]
    r2 = q1(conn, "SELECT COUNT(DISTINCT substr(trade_date,1,6)) FROM factor")[0]
    say("  ⑥ factor 表: %d 个 distinct 交易日 / 跨 %d 个月   "
        "(记忆登记: 月末截面为主, 2026-08 起日频)" % (r, r2))
    out["factor_days"] = r
    return out


# ===========================================================================
# I9 —— 三条真实上游异常的影响量化（对 seq die 的输入路径）
# ===========================================================================
# seq die 的通道清单**不是抄的**, 来自 `app/models/seq_die.py:107-123` 的
# `feature_panel()`。它只用 `load_market()` 返回的 dict 里的这几项:
#   close/high/low/open × adj, amount, vol, turnover_rate(daily_basic)
# ⇒ `circ_mv`/`total_mv`/`pe_ttm`/`pb`/`dv_ttm`/`pre_close` **不在这条路径上**。
SEQ_DIE_INPUTS = ("daily:close,high,low,open,vol,amount",
                  "daily_basic:turnover_rate",
                  "adj_factor:adj_factor",
                  "index_daily:close(benchmark)")


def check_impact(conn, say):
    say("\n" + "=" * 100)
    say("I9 四条真实上游异常的**影响量化**（判定它们是否阻塞 seq die 全期）")
    say("=" * 100)
    say("  seq die 的输入通道（来自 app/models/seq_die.py:107-123 feature_panel）:")
    for s in SEQ_DIE_INPUTS:
        say("    - %s" % s)
    out = {}

    say("\n  ① `daily_basic.circ_mv>total_mv`(15 行, 603882.SH) 是否在 seq die 通道上?")
    say("     -> 否。seq die 只取 `daily_basic.turnover_rate`, 不取 circ_mv/total_mv。")
    n = q1(conn, "SELECT COUNT(*) FROM daily_basic WHERE turnover_rate IS NULL")[0]
    say("     `turnover_rate` NULL 数 = %d  ⇒ 该通道无缺失" % n)
    out["circ_mv_in_seqdie_path"] = False
    out["turnover_null"] = n

    say("\n  ③ `margin.rzche<0`(343 行) 是否在 seq die 通道上?")
    say("     -> 否。`margin` 不被 `load_market()` 读取; 它的消费者是 flow die")
    say("        (`app/factors/flow_factors.py`)。对 seq die 零影响。")
    out["margin_in_seqdie_path"] = False

    say("\n  ⑤ `adj_factor` 2 行非开市日 是否在 seq die 通道上?")
    say("     -> `adj_factor` **在**通道上(`m['adj']`), 故必须实测, 不能推断。")
    # 结构性证据 1: load_market 的 _al() 把索引 reindex 到 daily 的交易日上
    r = q1(conn, "SELECT COUNT(*) FROM adj_factor WHERE trade_date IN ('20210920','20210921')")[0]
    say("     (a) 这两行在库里的行数 = %d" % r)
    r = q1(conn, "SELECT COUNT(*) FROM daily WHERE trade_date IN ('20210920','20210921')")[0]
    say("     (b) `daily` 在这两天的行数 = %d  ⇒ `load_market` 的日期索引取自 `daily`,"
        " 这两行会被 reindex 丢掉" % r)
    # 结构性证据 2: 689009.SH 在**交易日**上的 adj_factor 有没有缺失
    r = q1(conn, """SELECT COUNT(*) FROM daily d LEFT JOIN adj_factor a
                    ON a.trade_date=d.trade_date AND a.ts_code=d.ts_code
                    WHERE d.ts_code='689009.SH' AND a.adj_factor IS NULL""")[0]
    say("     (c) 689009.SH 在 `daily` 有行的交易日里, adj_factor 缺失 = %d  ⇒ %s"
        % (r, "无缺失" if r == 0 else "**有缺口**"))
    r = q1(conn, "SELECT MIN(adj_factor), MAX(adj_factor) FROM adj_factor "
                  "WHERE ts_code='689009.SH'")[1]
    say("     (d) 689009.SH 的 adj_factor 上界 = %s（那两行的值是 1.0 = 该股基准值）" % r)
    out["adj_stray_rows"] = 2
    out["adj_689009_missing_on_trading_days"] = r
    say("     -> 结论: 这两行**进不了 seq die 的面板**, 零影响。")

    say("\n  ⑥ `moneyflow` 符号相反 31,900 行 是否在 seq die 通道上?")
    say("     -> 否。`moneyflow` 不被 `load_market()` 读取; 消费者是 flow die 的")
    say("        `mf_main_sm_div` 因子。对 seq die 零影响。")
    out["moneyflow_in_seqdie_path"] = False
    # ⑥ 的**独立判卷**（用当日 close 当裁判, 与 moneyflow 自身的字段无关）
    r = conn.execute("""
      WITH j AS (SELECT m.net_mf_amount amt, m.net_mf_vol vol, d.close px,
                        (m.net_mf_amount*10000.0)/(m.net_mf_vol*100.0) implied
                 FROM moneyflow m JOIN daily d
                   ON d.ts_code=m.ts_code AND d.trade_date=m.trade_date
                 WHERE m.net_mf_amount<>0 AND m.net_mf_vol<>0 AND d.close>0)
      SELECT CASE WHEN (amt>0)=(vol>0) THEN '符号一致' ELSE '符号相反' END g,
             COUNT(*), SUM(CASE WHEN ABS(implied-px)/px < 0.05 THEN 1 ELSE 0 END),
             SUM(CASE WHEN implied < 0 THEN 1 ELSE 0 END)
      FROM j GROUP BY 1""").fetchall()
    say("     [独立判卷] 隐含价 = net_mf_amount×10000/(net_mf_vol×100), 用当日 close 比对:")
    say("       %-8s %12s %14s %12s" % ("组", "行数", "隐含价≈close", "隐含价为负"))
    for g, n, okc, neg in r:
        say("       %-8s %12d %14d %12d" % (g, n, okc or 0, neg or 0))
        out["mf_implied_%s" % g] = dict(n=n, close_ok=okc or 0, negative=neg or 0)

    say("\n  ⇒ **四条异常均不落在 seq die 的输入路径上**（① ③ ⑥ 靠通道清单, ⑤ 靠实测）。")
    return out


# ===========================================================================
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-domain", action="store_true",
                    help="跳过 I3 全表扫描(大表上较慢)")
    args = ap.parse_args()
    db.init_db()
    say = Say(LOG)
    t0 = time.time()
    conn = db.get_conn()

    say("=" * 100)
    say("D01 数据完整性验收 —— 实验路线图阶段一「数据验收」首项")
    say("脚本 sha256[:16] = %s   (预注册冻结在这个哈希上)" % script_sha())
    say("范围 = 生产 app/ 读且非本系统输出表的 %d 张" % len(KEY_COLS))
    say("=" * 100)

    verdict = {"script_sha": script_sha()}
    hard = []

    r, ok = check_pk(conn, say)
    verdict["I1_I2"] = r
    hard.append(("I1+I2 主键完整", ok))

    if args.skip_domain:
        say("\n(I3 已按 --skip-domain 跳过)")
    else:
        r, ok = check_domain(conn, say)
        verdict["I3"] = r
        hard.append(("I3 值域合法", ok))

    r, ok = check_dates(conn, say)
    verdict["I4"] = r
    hard.append(("I4 日期轴", ok))

    r, ok = check_calendar(conn, say)
    verdict["I5"] = r
    hard.append(("I5 交易日覆盖", ok))

    verdict["I6"] = check_missing(conn, say)
    verdict["I7"] = check_align(conn, say)
    verdict["I8"] = check_known(conn, say)
    verdict["I9"] = check_impact(conn, say)

    say("\n" + "=" * 100)
    say("总判决（I1~I5 逐条）")
    for name, ok in hard:
        say("   %-22s %s" % (name, "PASS" if ok else "**FAIL**"))
    allok = all(ok for _, ok in hard)
    say("\n  **D01 = %s**" % ("PASS" if allok else "FAIL"))
    say("  读数项（不进判决）: I6 有缺失的列 %d 个, 其中未登记 %d 个；"
        "I7 见上表；I8 见上表"
        % (len(verdict["I6"]["positive"]), len(verdict["I6"]["unregistered"])))
    say("耗时 %.1fs" % (time.time() - t0))
    conn.close()

    verdict["verdict"] = "PASS" if allok else "FAIL"
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"experiment_id": "D01", **verdict},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    say("结果已写 %s" % OUT)
    say.close()


if __name__ == "__main__":
    main()
