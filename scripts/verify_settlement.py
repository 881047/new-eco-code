#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""NEW ECO 审查结清校验器（只读，零副作用）

用途：把报告里每条「可判定标准」变成一条能跑出 PASS/FAIL 的检查。
用法：
    .venv\\Scripts\\python.exe -X utf8 scripts\\verify_settlement.py
退出码：0 = 全部结清；1 = 仍有未结清项（逐条打印）。

约定阈值（如与你的纪律冲突，改 THRESHOLDS 即可）：
"""
import os
import re
import subprocess
import sys
from pathlib import Path

# 仓库根：本脚本在 scripts/ 下 ⇒ parents[1] 即仓库根。
# 不硬编码 D:\NEW ECO —— 换机器 / 换路径也能跑（项目 scripts/oneoff/README 的路径纪律）。
ROOT = str(Path(__file__).resolve().parents[1])
PY = os.path.join(ROOT, ".venv", "Scripts", "python.exe")
# 静态分析工具（pyflakes/vulture/radon）装在**隔离环境**，不在项目 .venv 里。
# 2026-09-22：原先是**硬编码**一个用户目录路径（`C:\Users\27817\.workbuddy-ai\...`）
# ⇒ 换机 / 换用户名 / 环境重建后该路径不存在，会**静默回退到项目 .venv**，
# 而那些工具并不在那里 ⇒ C1（未使用导入/死变量）会因"工具跑不起来"而失去判据，
# 却看起来只是"0 条"（假绿）。改成：环境变量 `NEWECO_ANALYSIS_PY` 优先 →
# 已知默认路径 → 最后才回退 `PY`，并**把实际用的解释器打出来**，便于事后分辨。
ANALYSIS_PY = os.environ.get("NEWECO_ANALYSIS_PY") or (
    r"C:\Users\27817\.workbuddy-ai\binaries\python\envs\default\Scripts\python.exe")
if not os.path.exists(ANALYSIS_PY):
    ANALYSIS_PY = PY

THRESHOLDS = {
    # （原 orphan_assets_max 已于 2026-09-20 移除：C3 不再作为结清判定项，见下方 C3 段）
    "backup_gb_max": 24.0,       # data/backup 体积上限（GB）
    "cc_max_functions": 0,       # CC>=21(F 级) 的目标数量：0 表示全部降到 E 级以下
    "mi_min": 20.0,              # 单文件可维护性指数下限
    "max_skip": 2,               # 允许的跳过数（仅 torch 分支）
}

results = []


def check(cid, name, ok, detail):
    results.append((cid, name, "PASS" if ok else "FAIL", detail))


def run(cmd, cwd=ROOT, timeout=900):
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                           encoding="utf-8", errors="ignore", timeout=timeout)
        return p.returncode, p.stdout or "", p.stderr or ""
    except Exception as e:
        return -1, "", str(e)


# --- C1 未使用导入 / 死变量（app/ + scripts 顶层；oneoff 属受保护区不计） ---
# 2026-09-22：**先证明分析器真的可用**。上面的路径回退（找不到就退到项目 .venv）
# 会造成一个假绿：pyflakes 不在那个解释器里 ⇒ 命令失败、输出为空 ⇒ `lines` 为空
# ⇒ C1 报"0 条"通过。那是"工具没跑起来"，不是"没有未使用变量"。
# 所以这里把"分析器可用"变成 C1 的前置条件：不可用就判 C1 **失败**并说明原因。
_arc, _aout, _aerr = run([ANALYSIS_PY, "-X", "utf8", "-c", "import pyflakes"])
ANALYZER_OK = (_arc == 0)
if not ANALYZER_OK:
    print(f"   [C1 前置失败] {ANALYSIS_PY} 里没有 pyflakes ⇒ C1 无法判定"
          f"（**不等于** 0 条）：{(_aerr or _aout).strip().splitlines()[-1][:120] if (_aerr or _aout).strip() else 'rc=%s' % _arc}")

rc, out, err = run([ANALYSIS_PY, "-X", "utf8", "-m", "pyflakes", "app/"])
rc2, out2, err2 = run([ANALYSIS_PY, "-X", "utf8", "-m", "pyflakes"] +
                      __import__("glob").glob(os.path.join(ROOT, "scripts", "*.py")))
allp = (out + err + "\n" + out2 + err2).splitlines()
lines = [l for l in allp if "imported but unused" in l
         or "assigned to but never used" in l
         or "is unused: name is never assigned in scope" in l]
# 有意保留项（删除会破坏行为/掩盖意图），不计入结清标准：
#  · trainer.py name_at_hist —— 带 noqa:F401，tests/test_review_fixes_0905.py 经
#    trainer.name_at_hist() 调用，删则 4 条断言失败
#  · quote_client.py global _live_state —— 改的是列表元素，技术上不需要 global，
#    但它表达"修改模块级状态"的意图，保留
ALLOW = ("trainer.py",        # noqa:F401 兼容测试的导入路径，删则 4 条断言失败
         "quote_client.py",   # global _live_state：表达"改模块级状态"的意图
         # kg_materialize.py 原在此豁免（草稿残留），2026-09-20 已实际删除草稿，不再需要
         "supply_excluder_r6.py",  # _cols 是元组解包占位，5 元素必须全接
         )
lines = [l for l in lines if not any(a in l for a in ALLOW)]
check("C1", "app/ + scripts/*.py 无未使用导入与死变量", ANALYZER_OK and len(lines) == 0,
      (f"{len(lines)} 条" + (": " + "; ".join(lines[:2]) if lines else ""))
      if ANALYZER_OK else f"分析器不可用（{ANALYSIS_PY}）⇒ 无法判定，不是 0 条")

# --- C2 生产区零引用定义（排除路由/注册式） --------------------------------
DEAD_TRUE = [  # 报告中确认的真死代码（不含 FastAPI 路由 run_monthly）
    ("app/datasource/event_client.py", "sync_restricted_release"),
    ("app/datasource/event_client.py", "sync_insider_paged"),
    ("app/datasource/event_client.py", "sync_insider"),
    ("app/datasource/event_client.py", "sync_repurchase"),
    ("app/datasource/event_client.py", "sync_seo_issue"),
    ("app/alpha/zoos.py", "qlib158_subset"),
    ("app/datasource/updater.py", "_periods"),
]
WARN_ATTR = re.compile(r"#\s*STATUS:\s*(frozen|keep|wired)", re.I)
still = []
for rel, name in DEAD_TRUE:
    p = os.path.join(ROOT, rel.replace("/", os.sep))
    if not os.path.exists(p):
        continue  # 已删除
    src = open(p, encoding="utf-8", errors="ignore").read()
    # 允许缩进：_periods 是类内 @staticmethod，不是顶层 def
    m = re.search(r"^\s*def\s+" + re.escape(name) + r"\b.*$", src, re.M)
    if m:
        head = src[:m.start()].splitlines()[-3:]
        # 允许两种结清方式：①真的删掉 ②保留但带 STATUS 标注 + 有测试引用
        annotated = any(WARN_ATTR.search(h) for h in head)
        if not annotated:
            still.append(f"{rel}:{name}")
check("C2", "7 个真死函数已删除或已标注 STATUS", len(still) == 0,
      "已结清" if not still else "未处理: " + ", ".join(still))

# --- C3 构建孤儿资产（2026-09-20：由「判定项」降级为「纯观测」，不再 check）----
# 降级理由（四条，均已实测）：
#   ① 收益≈0：孤儿 2.75 MB，而 data/ 是 62 GB ⇒ 占比 0.004%，磁盘角度可忽略
#   ② 删了也不减小仓库：196/196 资产**全部已入库**，删工作区副本对 git 体积零影响
#   ③ .gitignore:75 明写「构建产物 app/web/static/assets/ 入库」—— 项目有意决定
#   ④ 风险非零：vite 的 emptyOutDir:false 正是为防旧标签页切视图 404 卡死
# ⇒ 「≤24 个」这个阈值是本次审查自建的（非项目标准）。既然已判定不回收，
#   保留它只会让 C3 **恒定 FAIL 却不驱动任何行动** —— 一个永远红的假门禁
#   和「永远绿的假闸」同样有害（都会训练人忽略信号）。故仅统计、不判定。
assets = os.path.join(ROOT, "app", "web", "static", "assets")
static = os.path.join(ROOT, "app", "web", "static")
have, refd = set(), set()
if os.path.isdir(assets):
    have = set(os.listdir(assets))
    pat = re.compile(r"assets/([A-Za-z0-9._-]+\.[a-z0-9]+)")
    for root, _d, fns in os.walk(static):
        for fn in fns:
            if fn.endswith((".html", ".js", ".css")):
                try:
                    refd |= set(pat.findall(open(os.path.join(root, fn),
                                encoding="utf-8", errors="ignore").read()))
                except OSError:
                    pass
orphans = sorted(have - refd)
# 只观测、不判定：数字供日后若改变决定时参考（回收器见 scripts/oneoff 或 reclaim_assets.py）
print("   [观测·非结清项] 构建孤儿资产 %d 个（已判定不回收，见上方注释）"
      % len(orphans))

# --- C4 备份体积 ------------------------------------------------------------
bdir = os.path.join(ROOT, "data", "backup")
size = sum(os.path.getsize(os.path.join(bdir, f))
           for f in os.listdir(bdir) if os.path.isfile(os.path.join(bdir, f))) \
    if os.path.isdir(bdir) else 0
gb = size / 1e9
check("C4", f"data/backup ≤ {THRESHOLDS['backup_gb_max']} GB",
      gb <= THRESHOLDS["backup_gb_max"], f"当前 {gb:.1f} GB")

# --- C5 全量测试 ------------------------------------------------------------
# -rs 才会打印跳过原因，否则无法判断"跳的是不是 torch 分支"
rc, out, err = run([PY, "-X", "utf8", "-m", "pytest", "tests/", "-q", "-rs"])
m = re.search(r"(\d+) passed", out + err)
ms = re.search(r"(\d+) skipped", out + err)
npass = int(m.group(1)) if m else 0
nskip = int(ms.group(1)) if ms else 999
reasons = re.findall(r"^SKIPPED \[\d+\] (\S+):(\d+): (.*)$", out + err, re.M)
non_torch = [r for r in reasons if "torch" not in r[2]]
torch_only = len(non_torch) == 0
check("C5", f"全量测试通过且跳过 ≤ {THRESHOLDS['max_skip']}（仅 torch）",
      rc == 0 and nskip <= THRESHOLDS["max_skip"] and torch_only,
      f"passed={npass} skipped={nskip}; 非 torch 跳过 {len(non_torch)} 条")

# --- C6 文档与实测一致（跳过数） -------------------------------------------
claude = os.path.join(ROOT, "CLAUDE.md")
drift = False
if os.path.exists(claude):
    txt = open(claude, encoding="utf-8", errors="ignore").read()
    # 文档若仍宣称"现在是 0"而实测跳过 2 条 ⇒ 漂移
    if re.search(r"现在是\s*0", txt) and nskip > 0:
        drift = True
check("C6", "CLAUDE.md 跳过数描述与实测一致", not drift,
      "一致" if not drift else "文档仍写'现在是 0'，实测 2 条（torch 不入锁）")

# --- C7 锁陈旧判定单一实现 -------------------------------------------------
# 精确匹配"调用"而非全词：docstring/注释里提到 GetProcessTimes 不算实现
IMPLEMENT_RE = re.compile(r"kernel32\.GetProcessTimes\s*\(")
cnt = 0
where = []
for dp, dns, fns in os.walk(os.path.join(ROOT, "app")):
    dns[:] = [d for d in dns if d != "__pycache__"]
    for fn in fns:
        if fn.endswith(".py"):
            p = os.path.join(dp, fn)
            try:
                for i, line in enumerate(open(p, encoding="utf-8",
                                              errors="ignore"), 1):
                    if IMPLEMENT_RE.search(line):
                        cnt += 1
                        where.append(os.path.relpath(p, ROOT).replace("\\", "/")
                                     + f":{i}")
            except OSError:
                pass
check("C7", "锁陈旧判定单一实现（kernel32.GetProcessTimes( 调用仅 1 处）",
      cnt == 1, f"当前 {cnt} 处实现: {', '.join(where) or '无'}")

# --- C8 草稿残留清理（kg_materialize）-------------------------------------
# 2026-09-20 定性更正：rr30/rr90/latest/last2 不是"未完成功能"，而是早期基于事件表
# ev 的实现草稿 —— 两个因子本身早已在 base["kg_eps_revision_mom"] /
# base["kg_holder_conc_chg"] 物化（走 rr_tbl / conc 路径），草稿只是没清理。
#   last2 = conc_by_code.nlargest if False else None 是恒 None 的死逻辑。
# 三态判定（原逻辑 count<=1 会把"已删除"误判为未完成，导致 C8 永远无法结清）：
#   0  = 已删除      ⇒ 结清
#   1  = 存在但仅赋值 ⇒ 死变量，未结清
#   >1 = 有真实使用  ⇒ 结清
# 2026-09-22 复核（结项审计）：实测 `scripts/kg_materialize.py` 里 rr30/rr90/latest/last2
# **四个变量已全部不存在**（grep 零命中）⇒ 三态判定落到 0 = 已删除 ⇒ **C8 已结清**。
# 下面的字典**刻意保留、不删**：它不是"待办清单"而是**回归守卫** —— 若哪天有人把这四段
# 草稿改回来，状态会回到 1 ⇒ C8 立刻红。删掉字典 = 丢掉这个守卫（且丢掉上面那段
# "为什么不是未完成功能"的定性记录）。
KM = os.path.join(ROOT, "scripts", "kg_materialize.py")
DEAD_DRAFT = {
    "rr30": "EPS 动量草稿（事件表旧路径）",
    "rr90": "EPS 动量草稿（事件表旧路径）",
    "latest": "集中度草稿（未跟聚合的 groupby）",
    "last2": "集中度草稿（if False 恒 None）",
}
unfinished, removed = [], []
if os.path.exists(KM):
    txt = open(KM, encoding="utf-8", errors="ignore").read()
    for name in DEAD_DRAFT:
        n = len(re.findall(r"\b" + re.escape(name) + r"\b", txt))
        if n == 0:
            removed.append(name)
        elif n == 1:
            unfinished.append(name)
check("C8", "kg_materialize 草稿残留已清理（删除或启用）",
      len(unfinished) == 0,
      (f"已删除 {len(removed)}/{len(DEAD_DRAFT)}: {', '.join(removed)}"
       if not unfinished else "仍是死变量: " + ", ".join(unfinished)))

# --- 汇总 ------------------------------------------------------------------
print("=" * 78)
print("NEW ECO 审查结清校验")
print("=" * 78)
for cid, name, st, detail in results:
    flag = "[PASS]" if st == "PASS" else "[FAIL]"
    print(f"{flag} {cid} {name}\n        {detail}")
npass = sum(1 for r in results if r[2] == "PASS")
print("-" * 78)
print(f"结清 {npass}/{len(results)} 项")
sys.exit(0 if npass == len(results) else 1)
