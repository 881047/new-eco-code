r"""生成 `docs/db_table_index.md` —— 生产库的表清单索引（**只读，不碰数据库内容**）。

═══════════════════════════════════════════════════════════════════════════════
为什么需要它（2026-09-24 "一轮结清"轮）
═══════════════════════════════════════════════════════════════════════════════
生产库 `data/neweco.db` 已到 **194 张表**，其中 135 张是 `model_*` 实验表。风险不是"占空间"
（实验表实测仅占 18.9%，全库约 81% 是正当生产数据），而是**误删**：

  · `model_score_oldm`（48.5 万行）—— 名字像"旧垃圾"，实际被 `D:\know` 正文引用，
    是**已登记结论的证据**；
  · `model_score_weekly_chk`（354 万行，全库最大实验表）—— 名字像临时校验表，
    实际是"周频化 A/B 判否"（05 号 §四）的**证据隔离表**；
  · 全部 65 张"看似零引用"的表，其实被生成脚本以 **f-string 模板**引用
    （如 `model_score_cblk_{b}_{sid}` ← `experiments/05_label/C_block_fusion.py:100`）
    —— 用字面名扫描会得出"它们是孤儿"的**假结论**。

⇒ 所以本轮**不删任何表**，改为把"哪张表是什么、谁生成、能不能再生"变成可查的。

═══════════════════════════════════════════════════════════════════════════════
方法（可复核）
═══════════════════════════════════════════════════════════════════════════════
1. 表清单 = `sqlite_master` 实读；
2. **引用识别** = 在三处语料里找 **字面名** 或 **f-string 模板**（`model_score_x_{a}` 形式
   还原成正则去匹配表名）—— 只做字面匹配会漏掉 f-string 拼名（这就是"65 张孤儿"的由来）；
3. 分类：`model_*` / `backtest_*` 等实验前缀 ⇒ 实验表；其余 ⇒ 生产/系统表；
4. "可否再生" = 能否找到生成脚本；找不到 ⇒ 标 **不可再生**（那才是最该保护的）。

用法：
  ./.venv/Scripts/python.exe -X utf8 scripts/gen_db_table_index.py            # 写 docs/db_table_index.md
  ./.venv/Scripts/python.exe -X utf8 scripts/gen_db_table_index.py --check     # 只比对，不写
"""
from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
OUT = BASE / "docs" / "db_table_index.md"
DB = BASE / "data" / "neweco.db"

#: 扫引用的语料目录（与"三重引用扫描"一致）
SCAN_DIRS = ("app", "scripts", "tests", "frontend", "experiments", "plans")

#: 实验/派生表的前缀（判定"生产表 vs 实验表"）
EXPERIMENT_PREFIXES = ("model_score", "model_importance", "backtest_")

#: 已知的"生产表"白名单（名字不带实验前缀，但要显式确认它们是被生产的）
PRODUCTION_TABLES = (
    "daily", "daily_basic", "adj_factor", "index_daily", "stock_basic", "trade_cal",
    "fund_basic", "fund_daily", "fund_adj", "fund_nav", "fina_indicator", "factor",
    "model_score", "model_meta", "plan", "signal", "regime", "pool_index",
    "paper_account", "paper_position", "paper_order", "paper_trade", "paper_nav",
    "paper_interest", "notify_log", "pipeline_run", "alpha_bank", "data_status",
    "moneyflow", "margin", "block_trade", "holder_num", "hk_hold", "lhb", "namechange",
)

_TMPL_RE = re.compile(r"(model_(?:score|importance)[A-Za-z0-9_]*\{[^}\n]{1,60}\}[A-Za-z0-9_{}]*)")


#: SQLite 内部表（自动维护，不属"业务表"，也不该出现在"最该保护"清单里）
SQLITE_INTERNAL = ("sqlite_sequence", "sqlite_stat1", "sqlite_stat4", "sqlite_master")

#: **本生成器自身必须排除**（2026-09-24 第二版踩到）：它的 docstring 里举例写了
#: `model_score_oldm` / `model_score_cblk_{b}_{sid}` 这些表名 ⇒ 若不排除，它会
#: **把自己匹配成"引用出处"**，于是每一张表都"找得到生成脚本" ⇒ "0 张不可再生"
#: 是个**自我污染的假结论**。凡是"扫全库找引用"的工具都要防这一条。
SELF_EXCLUDE = ("scripts/gen_db_table_index.py",)


def _corpus() -> dict:
    """{相对路径: 文本} —— 用于找引用。"""
    out = {}
    for d in SCAN_DIRS:
        root = BASE / d
        if not root.is_dir():
            continue
        for dp, dn, fn in os.walk(root):
            if any(x in dp for x in (".venv", "__pycache__", ".git")):
                continue
            for f in fn:
                if not f.endswith((".py", ".md", ".json", ".js", ".vue", ".yaml")):
                    continue
                p = Path(dp) / f
                rel = p.relative_to(BASE).as_posix()
                if rel in SELF_EXCLUDE:
                    continue
                try:
                    out[rel] = p.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    pass
    return out


def _templates(corpus: dict) -> dict:
    """{正则: 首个出处} —— 从 f-string 模板还原出的匹配器。

    ⚠️ **必须剔掉 catch-all 模板**（2026-09-24 第三版踩到）：`model_score_{tag}` 这类
    模板还原成正则后是 `^model_score_.*?$` ⇒ 会匹配**任意** `model_score_*` 表名
    （实测：`scripts/llm_crowding_excluder.py` 的 `model_score_{tag}` 把
    `model_score_oldm` 也算成了它的产物）。判据：**占位符之前的字面前缀必须比家族名更长**
    —— `model_score_cblk_{b}_{sid}`（前缀 `model_score_cblk_`）是**具体的**；
    `model_score_{tag}`（前缀 `model_score_`）是**泛化的**，不采信。
    """
    out = {}
    for rel, txt in corpus.items():
        for m in _TMPL_RE.finditer(txt):
            t = m.group(1)
            head = t.split("{", 1)[0]
            family = "model_importance_" if head.startswith("model_importance_") else "model_score_"
            if head == family:            # 泛化模板：不采信
                continue
            parts = re.split(r"\{[^}]*\}", t)
            out.setdefault(re.compile("^" + ".*?".join(re.escape(p) for p in parts) + "$"), rel)
    return out


def _find_reference(table: str, corpus: dict, tmpls: dict) -> tuple:
    """返回 (出处 或 None, 匹配方式)。

    `匹配方式` ∈ {`代码·字面名`, `代码·f-string 模板`, `文档提及`, `—`}。

    ⚠️ 两条都在这里被踩过（见 `_templates` 与本函数 docstring）：
    ① **必须按"全名 token"匹配，不能用裸子串** —— `"model_score" in txt` 会让每一张
       `model_score_*` 都"命中"（前缀出现在同一批文件里）；
    ② **"被文档提到" ≠ "有生成脚本"** —— `.md`/`.json` 里的提及是**证据指针**，
       不能当作"这张表可再生"。所以只把 **`.py`**（`app/`/`scripts/`/`experiments/`）里的
       引用算作**生成脚本**。
    """
    code_dirs = ("app/", "scripts/", "experiments/")
    rx = re.compile(r"(?<![A-Za-z0-9_])" + re.escape(table) + r"(?![A-Za-z0-9_])")
    doc_hit = None
    for rel, txt in corpus.items():
        if rx.search(txt):
            if rel.endswith(".py") and rel.startswith(code_dirs):
                return rel, "代码·字面名"
            doc_hit = doc_hit or rel
    for rx_t, rel in tmpls.items():
        if rx_t.match(table):
            return rel, "代码·f-string 模板"
    return doc_hit, ("文档提及" if doc_hit else "—")


def collect() -> list:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        con.execute("PRAGMA query_only=ON")
        rows = con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
        corpus, tmpls = _corpus(), _templates(_corpus())
        out = []
        for (name,) in rows:
            try:
                n = int(con.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0])
            except Exception:                                     # noqa: BLE001
                n = -1
            ref, how = _find_reference(name, corpus, tmpls)
            is_exp = name.startswith(EXPERIMENT_PREFIXES)
            internal = name in SQLITE_INTERNAL
            out.append({
                "table": name, "rows": n, "ref": ref, "how": how,
                "kind": "SQLite 内部" if internal else ("实验/派生" if is_exp else "生产/系统"),
                # 「可再生」= 有**代码**引用（生成脚本）；文档提及不算
                "regen": True if internal else how.startswith("代码·"),
            })
        return out
    finally:
        con.close()


def render(items: list) -> str:
    prod = [x for x in items if x["kind"] == "生产/系统"]
    exp = [x for x in items if x["kind"] == "实验/派生"]
    internal = [x for x in items if x["kind"] == "SQLite 内部"]
    no_regen = [x for x in items if not x["regen"]]
    L = []
    A = L.append
    A("# 生产库表清单索引（`data/neweco.db`）")
    A("")
    A("> **本文件由 `scripts/gen_db_table_index.py` 生成，不要手改**；改完代码请重跑生成器。")
    A("> 生成于 2026-09-24（「一轮结清」轮）。用途：**防误删** —— 见文件头「为什么需要它」。")
    A("")
    A("## 一、总览")
    A("")
    A("| 项 | 值 |")
    A("|---|---|")
    A("| 表总数 | **{}** |".format(len(items)))
    A("| 生产/系统表 | **{}** |".format(len(prod)))
    A("| 实验/派生表 | **{}** |".format(len(exp)))
    A("| SQLite 内部表 | **{}** |".format(len(internal)))
    A("| 找不到生成脚本（**最该保护**） | **{}** |".format(len(no_regen)))
    A("")
    A("> ⚠️ **本索引的用途是「看清楚」，不是「删」**。2026-09-24 审查曾把 65 张表判为「零引用孤儿」，后经 f-string 模板重扫**推翻**（它们全被生成脚本引用）。"
      "任何删除都必须先按本索引逐表核对「生成脚本 + 结论产物」两项。")
    A("")
    A("## 二、生产/系统表（{} 张）".format(len(prod)))
    A("")
    A("| 表 | 行数 | 被谁引用 | 备注 |")
    A("|---|---:|---|---|")
    for x in sorted(prod, key=lambda y: -y["rows"]):
        ref = x["ref"] or "**⚠️ 未找到引用**"
        A(f"| `{x['table']}` | {x['rows']:,} | `{ref}` | |")
    A("")
    A("## 三、实验/派生表（{} 张）".format(len(exp)))
    A("")
    A("按**家族**分组（同一脚本的多个臂/种子）。`引用` 列里的 `*` 表示"
      "**靠 f-string 模板匹配到**（字面名扫不到，这正是「孤儿」假结论的来源）。")
    A("")
    A("| 表 | 行数 | 生成脚本 | 匹配方式 |")
    A("|---|---:|---|---|")
    for x in sorted(exp, key=lambda y: (-y["rows"], y["table"])):
        ref = x["ref"] or "**⚠️ 未找到生成脚本**"
        A(f"| `{x['table']}` | {x['rows']:,} | `{ref}` | {x['how']} |")
    A("")
    if no_regen:
        A("## 四、⚠️ 找不到生成脚本的表（**不可再生，最该保护**）")
        A("")
        A("| 表 | 行数 | 类型 |")
        A("|---|---:|---|")
        for x in sorted(no_regen, key=lambda y: -y["rows"]):
            A(f"| `{x['table']}` | {x['rows']:,} | {x['kind']} |")
        A("")
        A("⇒ 这些表**没有可追的生成脚本** ⇒ 删了就真没了。若确认无用，"
          "也请先**导出 JSON 归档 + 记 sha256**（同 `reset_paper.py` 的归档纪律），再谈删除。")
        A("")
        A("⚠️ **这一节是「保守结果」，不等于「确定不可再生」** —— 本生成器**有意不采信泛化模板**"
          "（`model_score_{tag}` 之类，见 `_templates` 的注记：采信它会把**任意** `model_score_*` "
          "都算成它的产物）。所以其中 `model_score_excl_theta*` / `model_score_excl4_theta*` "
          "很可能**确实**由 `scripts/llm_crowding_excluder.py` / `scripts/negative_excluder_r4.py` "
          "的 catch-all 模板生成（`tag = f\"excl_theta{theta}\"`），只是**机器证不出来**。"
          "⇒ 处置时按「先当不可再生处理」（更保守），人工确认后再下调。")
        A("")
    A("## 五、纪律（引用本索引前先读）")
    A("")
    A("1. **实验表不是垃圾**：它们的结论多已落 `experiments/*.json`（入库），"
      "但**逐行分数不可再生除非重跑训练**（每张 ~44 万行、数百秒）；")
    A("2. **`scripts/oneoff/` 是证据**：其中脚本的输出表不得删除、不许批量搬家；")
    A("3. **删除前必须**：① 按本索引核对「生成脚本 + 结论产物」；② 全量备份（`scripts/backup_db.py`）；"
      "③ 逐表行数 + 内容 sha256 留痕；④ 经用户确认。")
    A("")
    return "\n".join(L) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只比对，不写")
    args = ap.parse_args()
    items = collect()
    text = render(items)
    if args.check:
        cur = OUT.read_text(encoding="utf-8") if OUT.exists() else ""
        if cur != text:
            print("✗ docs/db_table_index.md 与现算不一致 ⇒ 跑 gen_db_table_index.py")
            return 1
        print(f"✓ 表索引一致（{len(items)} 张表）")
        return 0
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(text, encoding="utf-8")
    print(f"已写 {OUT.relative_to(BASE).as_posix()}：{len(items)} 张表"
          f"（生产 {sum(1 for x in items if x['kind'] == '生产/系统')} / "
          f"实验 {sum(1 for x in items if x['kind'] == '实验/派生')} / "
          f"无生成脚本 {sum(1 for x in items if not x['regen'])}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
