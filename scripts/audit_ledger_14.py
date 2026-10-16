r"""14 号台账的机器可校验守卫（只读；审计第 10 项 + tests/test_ledger_14_pointers.py）

为什么需要它：2026-09-20 的审核发现，台账一边写着"这张表就是全部开放项"，一边漏着 20+ 条
"登记/待办/下一轮第一件事"；同时有 8+ 处判决用了自订四词之外的措辞，还有指到**不存在的守卫**
（`clock-exempt` 棘轮）与**过期计数**（"13 键""11 项""171 条"）的行。手工清扫只能管好一轮
—— 本脚本把三件事变成红灯：

  (a) 指针存在性：§三 与 §三补三十一 每行"证据指针"里的仓库路径必须**在盘上存在**，
      且**不许指向 `logs/`**（`logs/` 被 .gitignore:56 忽略 ⇒ 只落 logs 的证据会消失，
      本轮已因此撤回过一整张读数表）。
  (b) 判决措辞：两张表的"结论"列必须只出现 已做 / 不采纳 / 关闭 / 待跑 四词之一。
  (c) 计数漂移：从代码与库里**现算**四个计数（doctor 体检项数、调度档位数、`alpha_bank` 行数、
      现行 pin 的 `metrics` 键数），与 §二/§三/§三补三十一 里写着的数字比对；不符 ⇒ 红。
      历史段不参与比对（那些数字是当时那次运行的记录，按纪律不改写）。

四词措辞与"历史段不参与"是刻意的：本库是追加式证据链，历史段的旧读数是判决的一部分。
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]          # scripts/ 下的一级 = 仓库根本身
KB = Path(r"D:\know")
LEDGER = KB / "14_未结清单与接手起点.md"
WORDS = ("已做", "不采纳", "关闭", "待跑")
PATH_RE = re.compile(r"(?:tests|experiments|scripts|plans|app|data|logs|docs)/[\w./\-]*[\w]")
SECTIONS = ("## 二、", "## 三、", "### 三补三十·续2", "## 三补三十一")


def _current_sections(text: str) -> str:
    """只取"现行"段：§二、§三、三补三十一（历史段允许保留当时的数字与措辞）。

    段落只在 `## `（二级标题）处开合 —— 第一版把 `### ` 也当重置，结果判决总表里
    `### ⑤ 防复发` 那一小节被整段丢掉，"注入计数漂移"的阴性对照因此**假绿**。
    """
    out, keep = [], False
    for line in text.splitlines():
        if line.startswith("## "):
            keep = line.startswith(("## 二、", "## 三、", "## 三补三十一"))
        if keep:
            out.append(line)
    return "\n".join(out)


def _tables(text: str):
    """产出 (行号, 单元格列表) —— 只取 markdown 表格行。"""
    for n, line in enumerate(text.splitlines(), 1):
        s = line.strip()
        if s.startswith("|") and s.endswith("|") and set(s) - set("|-: "):
            yield n, [c.strip() for c in s.strip("|").split("|")]


# ---------------------------------------------------------------- 现算的四个计数
def live_counts() -> dict:
    out = {}
    doc = (ROOT / "app" / "weekly" / "doctor.py").read_text("utf-8", errors="replace")
    # 2026-09-22 修正：块级 IC 是**一个 check 函数实例化多次**（P0 每块一项，见
    # `app/weekly/blocks.py::DOCTOR_BLOCKS`）⇒ 数 `def check_` 会少算 3 项（14→18 时实测）。
    # 这里按 P0 块数补上多出来的实例；真值仍由 `tests/test_weekly_doctor.py` 的名单钉死。
    blk = (ROOT / "app" / "weekly" / "blocks.py").read_text("utf-8", errors="replace")
    n_p0 = len(re.findall(r'\("B\d+",\s*"P0"', blk))
    out["doctor 体检项"] = len(re.findall(r"^\s*def check_\w+", doc, re.M)) + 1 \
        + max(0, n_p0 - 1)                        # +「下个执行档」insert + 其余块实例
    sch = (ROOT / "app" / "scheduler.py").read_text("utf-8", errors="replace")
    out["调度档位(粗数,不参与比对)"] = len(re.findall(r"^\s*(?:_add|scheduler\.add_job)\(", sch, re.M)) or \
        len(re.findall(r"\.add_job\(", sch))
    db = "file:" + str(ROOT / "data" / "neweco.db").replace("\\", "/") + "?mode=ro"
    try:
        con = sqlite3.connect(db, uri=True)
        con.execute("PRAGMA query_only=ON")
        out["alpha_bank 行数"] = int(con.execute("SELECT COUNT(*) FROM alpha_bank").fetchone()[0])
        pin = re.search(r"^backtest_run:\s*(\S+)",
                        (ROOT / "config.yaml").read_text("utf-8"), re.M).group(1)
        row = con.execute("SELECT metrics FROM backtest_summary WHERE run_date=?", (pin,)).fetchone()
        out["现行 pin 键数"] = len(json.loads(row[0])) if row else None
        out["pin"] = pin
        con.close()
    except Exception as ex:                                     # noqa: BLE001
        out["db_error"] = f"{type(ex).__name__}: {ex}"
    return out


# ---------------------------------------------------------------- 三项检查
def check_pointers(cur: str):
    bad, logs_hits = [], []
    for n, cells in _tables(cur):
        row_all = " ".join(cells)
        deleted_ok = "git show" in row_all          # 已删文件 + 行内写明取回命令 = 合法指针
        for cell in cells:
            for m in PATH_RE.finditer(cell):
                p = m.group(0).rstrip(".,;")
                tail = cell[m.end():m.end() + 1]
                if tail in ("*", "…", "-", "<"):    # 通配/省略号写法（`plans/mirror-*`）不是路径
                    continue
                if p.startswith("logs/"):
                    if not deleted_ok:
                        logs_hits.append((n, p))
                    continue
                if "*" in p or "?" in p or len(p) < 6:
                    continue
                if not (ROOT / p).exists() and not deleted_ok:
                    bad.append((n, p, "盘上不存在"))
    return bad, logs_hits


def check_words(cur: str):
    """两张表的"结论"列必须只用四词。列号按**表头形状**认，不按出现顺序猜。

    §三 开放项表 = `# | 事项 | 结论 | 证据指针 | …` ⇒ 结论在 index 2
    §三补三十一 判决总表 = `项 | 结论 | 理由 | …` ⇒ 结论在 index 1
    （第一版用两个布尔位记状态，结果两表混在一起时全表按 index 2 判 ⇒ 20 多条假红；
    第二版改成"看首格是不是 #/项"，而 §⑥ 那张 `| 文件 | 改了什么 |` 同步表不被认成表头
    ⇒ 沿用上一张表的列号 ⇒ 又假红。现在按"**下一行是不是 |---| 分隔行**"认表头，与列名无关。）
    """
    lines = cur.splitlines()
    sep = re.compile(r"^\|[\s\-:|]+\|$")
    bad, verdict_col = [], None
    for n, cells in _tables(cur):
        nxt = lines[n].strip() if n < len(lines) else ""     # _tables 的 n 是 1 基 ⇒ lines[n] 即下一行
        if sep.match(nxt):                                    # 表头行：按有没有"结论"列决定
            verdict_col = cells.index("结论") if "结论" in cells else None
            continue
        if verdict_col is None or set("".join(cells)) <= set("-: "):
            continue
        if len(cells) > verdict_col and not any(w in cells[verdict_col] for w in WORDS):
            bad.append((n, cells[verdict_col][:40], ""))
    return bad


def count_scope(cur: str) -> str:
    """计数门只扫 §二 + §三（"现行值"该出现的地方）。

    §三补三十一 的 §⑥ 是**变更日志表**，里面按格式写着"旧值 ⇒ 新值"
    （如"体检 `11 项` ⇒ `13 项`"）—— 那些旧数字是记录，不是断言，扫它们必然假红。
    """
    i, j = cur.find("## 二、"), cur.find("## 三补")
    return cur[i:j] if i >= 0 else cur


def check_counts(cur: str, live: dict):
    """文档里写计数 places 与现算值比对，**两个方向都认**（"13 项体检"与"体检项 = 13"都要抓到）。

    ⚠️ 有意不做"调度档位数"：朴素数 `.add_job(` 会把重试自排（`_retryable` 的 3/6/12 分那几处）
    一起算进来 ⇒ 现算 14、真值 12 ⇒ 一条会假红的门不如没有。档位数由
    `tests/test_scheduler_schedule_contract.py` 逐条钉住，那份契约测试才是它的守卫。
    """
    bad = []
    pairs = [(r"(\d+)\s*(?:项)?体检|体检[^0-9\n]{0,8}(\d+)\s*项", "doctor 体检项"),
             (r"alpha_bank[^0-9\n]{0,14}(\d+)\s*行|(\d+)\s*行[^0-9\n]{0,10}alpha_bank",
              "alpha_bank 行数"),
             (r"(\d+)\s*个?键|键数\s*[=为]?\s*(\d+)", "现行 pin 键数")]
    for pat, key in pairs:
        want = live.get(key)
        if want is None:
            continue
        for m in re.finditer(pat, cur):
            got = next((g for g in m.groups() if g), None)
            if got is None:
                continue
            if int(got) != want:
                bad.append((key, want, int(got), cur[:m.start()].count("\n") + 1))
    return bad


def check_negative_controls(cur: str, live: dict):
    """守卫自己的阴性对照：三种坏行必须都能被抓住（否则这套东西是恒绿的假门）。"""
    fake_cur = cur + """
| # | 事项 | 结论 | 证据指针 | 量级或代价 | 触发 / 复活条件 | 台账原位置 |
|---|---|---|---|---|---|---|
| Z1 | 虚构指针 | 待跑 | tests/test_definitely_not_a_real_file.py | 0 | never | 审核 |
| Z2 | 日志产物 | 关闭 | logs/ab_bench_tr_real_index.json | 0 | never | 审核 |
| Z3 | 措辞越界 | 留案结清 | tests/test_db_conn_hygiene.py | 0 | never | 审核 |
"""
    # 注：分隔行 `|---|` 必须有 —— 表头是按"下一行是不是分隔行"认的，漏了就整表不识别。
    p_bad, l_bad = check_pointers(fake_cur)
    w_bad = check_words(fake_cur)
    fake_live = dict(live)
    if "现行 pin 键数" in fake_live:
        fake_live["现行 pin 键数"] = 999
    c_bad = check_counts("指标 `metrics` 共 32 个键；键数 = 32。\n", fake_live)
    # ↑ 对照用自己的最小文本：机制要验的是"数字不符 ⇒ 报红"，不能依赖台账当下提没提到那个计数。
    return {
        "抓虚构指针": any("test_definitely_not_a_real_file" in p for _, p, _ in p_bad),
        "抓 logs 指针": any(p.startswith("logs/") for _, p in l_bad),
        "抓越界措辞": any("留案结清" in v for _, v, _ in w_bad),
        "抓计数漂移": bool(c_bad),
    }


def main(argv: list[str]) -> int:
    text = LEDGER.read_text("utf-8")
    cur = _current_sections(text)
    live = live_counts()
    p_bad, l_bad = check_pointers(cur)
    w_bad = check_words(cur)
    c_bad = check_counts(count_scope(cur), live)
    ctrl = check_negative_controls(cur, live)
    fail = []
    if not all(ctrl.values()):
        fail.append("守卫自己抓不住坏行 ⇒ 恒绿假门：" + str({k: v for k, v in ctrl.items() if not v}))
    for n, p, why in p_bad:
        fail.append(f"§三/判决表 第{n}行指针{why}: {p}")
    for n, p in l_bad:
        fail.append(f"§三/判决表 第{n}行指向不可跟踪的 logs/（证据必须落被跟踪目录）: {p}")
    for n, v, _ in w_bad:
        fail.append(f"§三/判决表 第{n}行结论用了四词之外的措辞: {v}")
    for key, want, got, n in c_bad:
        fail.append(f"第{n}行 {key} 文档写 {got}，现算 {want}")
    print("现算计数:", {k: v for k, v in live.items()})
    print("阴性对照:", ctrl)
    if fail:
        print("✗ 台账守卫 %d 条红" % len(fail))
        for f in fail[:40]:
            print("   -", f)
        return 1
    print("✓ 14 号台账：指针全存在、措辞合规、计数与现算一致")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
