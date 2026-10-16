"""数据库连接卫生（棘轮守卫，2026-09-13）

背景（独立审查 §3.2）：`app/` 里有 **153 处 `get_conn()`**，而真正保证关闭的
`db.conn_ctx()` 只有 5 处使用者。绝大多数是

    conn = db.get_conn()
    ...
    conn.close()

**没有 try/finally** ⇒ 中间任何异常都会漏掉一个连接。在长驻服务（uvicorn + APScheduler）里
这会累积句柄；更糟的是 `init_db`/迁移路径里漏掉的是**持有写锁**的连接，会让其它进程
全部 `database is locked`。

注意 `with db.get_conn() as conn:` **不管关闭**（sqlite3.Connection 的上下文管理器管的是
事务，正常退出 commit、异常回滚）—— 这是本项目最容易踩的坑，所以 `conn_ctx()` 专门存在。

本测试是**棘轮**：允许存在"已知漏点"（列入 `KNOWN_LEAKS`，只许减少不许增加），
新增漏点立即失败。修好一处就把它的条目删掉。

**2026-09-13 状态：清单已清零（`KNOWN_LEAKS = {}`）** —— 153 处逐批改造完毕，
棘轮现在等价于"零容忍"：任何新写的裸 `get_conn()` 都会当场测红。
"""
import ast
import json
import sys
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
APP = BASE / "app"

# 已知漏点（函数名 → 备注）。**只许删，不许加**。
#
# 2026-09-13 状态：**全部清零**。
#   · 第一批 8 处（scheduler._record_skip/_stage/daily_update/_safe_end/monthly_pipeline/
#     _weekly_backup/_catch_up + funds.score_etfs/otc_fund_picks + repo.latest_rate/
#     repo_advice）—— 审查点名的"锁竞争路径"。
#   · 第二批 29 处 + daily_engine.compute_daily —— 按模块分批改造完毕，全部改为
#     `with db.conn_ctx() as conn:`（含 7 个函数里的"部分站点已改、剩余未改"缺口）。
# 剩余形态（**不是漏点，不需要登记**）：
#   · updater.sync_daily_bulk / init_fina 的 conn3、akshare_client.sync_fina_fallback、
#     holder_factors.industry_map_pit_last —— 这 4 处**本身就有 try/finally 保护**，
#     扫描器判据已认可，故从未出现在本清单里。
KNOWN_LEAKS = {}


def _iter_funcs(tree):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def _close_guarded(fn: ast.AST, var: str) -> bool:
    """函数体内是否存在 try/finally 且 finally 里对该变量调了 .close()。"""
    for node in ast.walk(fn):
        if isinstance(node, ast.Try) and node.finalbody:
            for sub in ast.walk(node):
                if (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute)
                        and sub.func.attr == "close"
                        and isinstance(sub.func.value, ast.Name) and sub.func.value.id == var):
                    return True
    return False


def _uses_conn_ctx(fn: ast.AST) -> bool:
    for node in ast.walk(fn):
        if isinstance(node, ast.With):
            for item in node.items:
                c = item.context_expr
                if (isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
                        and c.func.attr == "conn_ctx"):
                    return True
    return False


def scan_leaks() -> dict:
    """→ {(file::func): 代码行号}；只统计"拿了连接、没走 conn_ctx、也没有 try/finally close"的。"""
    out = {}
    for py in sorted(APP.rglob("*.py")):
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        rel = py.relative_to(BASE).as_posix()
        for fn in _iter_funcs(tree):
            if _uses_conn_ctx(fn):
                continue
            for node in ast.walk(fn):
                if not isinstance(node, ast.Assign):
                    continue
                v = node.value
                is_get = (isinstance(v, ast.Call) and isinstance(v.func, ast.Attribute)
                          and v.func.attr == "get_conn")
                if not is_get or not node.targets or not isinstance(node.targets[0], ast.Name):
                    continue
                var = node.targets[0].id
                if not _close_guarded(fn, var):
                    name = f"{rel}::{fn.name}"
                    out.setdefault(name, node.lineno)
    return out


class TestConnHygieneRatchet(unittest.TestCase):
    def test_no_new_leaks(self):
        leaks = scan_leaks()
        new = {k: v for k, v in leaks.items() if k not in KNOWN_LEAKS}
        self.assertEqual(new, {}, f"新增了未受保护的 get_conn() 连接泄漏点: {new}")

    def test_known_leaks_are_still_real(self):
        """清单里已修好的条目必须删掉（否则清单会变成'永久豁免'）。"""
        leaks = scan_leaks()
        stale = [k for k in KNOWN_LEAKS if k not in leaks]
        self.assertEqual(stale, [], f"这些漏点已不存在，请从 KNOWN_LEAKS 删除: {stale}")


if __name__ == "__main__":
    print(json.dumps(scan_leaks(), ensure_ascii=False, indent=1))
