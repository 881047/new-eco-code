# STATUS: oneoff —— 一次性连接卫生改造工具(2026-09-13)。**不是生产脚本**:
# 把 `conn = db.get_conn() ... conn.close()` 机械改造成 `with db.conn_ctx() as conn:`,
# 由调用方(本轮改造)在改完后按需删除/留档。只做文本搬运, 不做任何逻辑改写:
#   1. `conn = db.get_conn()` 行 → `with db.conn_ctx() as conn:`
#   2. 中间语句块 → 原行原样 +4 空格缩进(不重排、不重写、不动注释/SQL)
#   3. `conn.close()` 行 → 删除(改由上下文管理器在 finally 里 close)
# 自检(每条都 fail-fast, 不确定就拒绝改):
#   · 只改"赋值行→close 行"之间没有 return 的段(有 return 的段需人工判断)
#   · 只在函数体内没有 try/finally close 保护时才改(已有保护的段跳过)
#   · 改完必须 `ast.parse` 通过, 且"去掉首尾空白行后的语句序列"逐条文本相同
import argparse
import ast
import difflib
import pathlib
import sys
import textwrap

BASE = pathlib.Path(__file__).resolve().parent.parent.parent

# 2026-09-13 护栏：本工具**会重写仓库里的源码文件**（受 git 跟踪）—— 与"会写生产表"同级的
# 不可逆动作。按本项目规矩（scripts/oneoff/_guard.py）：默认拒绝执行，须显式放行。
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from _guard import require_confirm        # noqa: E402

require_confirm(__file__, "重写 app/** 下的源码文件（就地替换连接管理写法）")

# 占掉被删掉的 `conn = db.get_conn()` 那一行。**必须是注释, 不能是赋值** ——
# `with ... as conn:` 已经绑定 conn_ctx 内部的连接; 再 get_conn() 一次 = 把被管理的
# 那个丢弃、另开一个没人关的(2026-09-13 实测: 每个站点多漏一个连接)。
BY_BINDING = "# conn_ctx 已绑定连接(勿再 get_conn: 新开的那个无人关)"

# 目标文件(相对仓库根)。app/factors/daily_engine.py 已于本日改造完, 不在列。
FILES = [
    "app/factors/factor_engine.py",
    "app/factors/flow_factors.py",
    "app/factors/holder_factors.py",
    "app/models/daily_score.py",
    "app/models/regime.py",
    "app/models/trainer.py",
    "app/portfolio/builder.py",
    "app/datasource/updater.py",
    "app/datasource/akshare_client.py",
    "app/backtest/engine.py",
]


def iter_funcs(tree):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def guarded_by_finally(fn, var):
    """函数体内是否有 try/finally 且 finally 里 close 了 var(与守卫测试同判据)。"""
    for node in ast.walk(fn):
        if isinstance(node, ast.Try) and node.finalbody:
            for sub in ast.walk(node):
                if (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute)
                        and sub.func.attr == "close"
                        and isinstance(sub.func.value, ast.Name) and sub.func.value.id == var):
                    return True
    return False


def _bare_close_lines(fn, var):
    """函数内所有 `var.close()` 裸语句的行号(含嵌在 if/try 里的)。"""
    out = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            c = node.value
            if (isinstance(c.func, ast.Attribute) and c.func.attr == "close"
                    and isinstance(c.func.value, ast.Name) and c.func.value.id == var):
                out.append(node.lineno)
    return sorted(out)


def _block_bounds(fn, node):
    """node 所在**最内层语句块**的行号区间 (lo, hi)。

    段不许越过它 —— 越界就等于把 try/except/finally、if/else 的边界切开了
    (updater.init_fina 的 conn2 就是这么被切坏的)。按**结构包含**逐层下钻,
    不能用行号区间: `if` 的 end_lineno 会把 else 分支也算进去。
    """
    best = None

    def walk_lists(parent):
        nonlocal best
        for _, v in ast.iter_fields(parent):
            if not isinstance(v, list):
                continue
            stmts = [x for x in v if isinstance(x, ast.stmt)]
            if any(x is node for x in stmts):
                best = (min(s.lineno for s in stmts),
                        max(getattr(s, "end_lineno", s.lineno) for s in stmts))
            for s in stmts:
                walk_lists(s)

    walk_lists(fn)
    return best if best else (1, 10 ** 9)


def _in_try_body(fn, node) -> bool:
    """node 是否落在某个 try 的 try-body 里(不限嵌套深度) —— 这类站点不能在原处套 with。"""
    ln = node.lineno
    for parent in ast.walk(fn):
        if isinstance(parent, ast.Try):
            head, tail = parent.body[0], parent.body[-1]
            if head.lineno <= ln <= getattr(tail, "end_lineno", tail.lineno):
                return True
    return False


def find_segments(src_lines, fn):
    """→ [(start_lineno, close_lineno, var)]。1-based 行号。

    每个 get_conn 赋值 → 该变量的**最后一个** close()。这一整段被 with 包住后:
      · 正常路径: 末尾 close() 变成冗余(conn_ctx 的 finally 也会关) —— sqlite3 close 幂等
      · 提前 return 路径: 路径上的 close() 仍是显式执行一次(带 try/finally 时语义不变),
        return 进出 with 由上下文管理器正常处理
    因此不再需要为"段内有 return"开特例。**被重新赋值**的同名变量则必须跳过(见下)。
    """
    # 收集函数内所有 get_conn 赋值(变量→行号)与裸 close(变量→行号)
    assigns, closes = {}, {}
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign) and node.targets and isinstance(node.targets[0], ast.Name):
            v = node.value
            if (isinstance(v, ast.Call) and isinstance(v.func, ast.Attribute)
                    and v.func.attr == "get_conn"):
                assigns.setdefault(node.targets[0].id, []).append(node.lineno)
    for node in ast.walk(fn):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            c = node.value
            if (isinstance(c.func, ast.Attribute) and c.func.attr == "close"
                    and isinstance(c.func.value, ast.Name)):
                closes.setdefault(c.func.value.id, []).append(node.lineno)

    segs = []
    for var, alines in sorted(assigns.items()):
        # 事件流(赋值/close)按行号走一遍, 切出"单条连接生命周期"的段:
        #   open  → 赋值; closed → close; 在已 open 状态下再赋值 = 同名变量复用(跳过)。
        # 段末取"下一条 close"(正常路径把连接关掉的那次); 若该 close 之后还有
        # 赋值/close, 说明变量被复用, 下一轮会重新开段, 不会互相交叉。
        # close 事件只保留与**该次赋值同属一个语句块**的: 跨块的 close(如 finally 里的)
        # 一旦被当成段末, 重排后 finally 会失去自己的 body。
        def _bounds_for(ln):
            return _block_bounds(fn, next(n for n in ast.walk(fn)
                                          if isinstance(n, ast.Assign) and n.lineno == ln))

        def _close_in_block(c):
            node = next((n for n in ast.walk(fn) if isinstance(n, ast.Expr) and n.lineno == c), None)
            return node is not None and any(_block_bounds(fn, node) == _bounds_for(a) for a in alines)


        events = sorted([(a, "assign") for a in alines] + [(c, "close") for c in closes.get(var, []) if _close_in_block(c)])
        state = "closed"
        cur = None
        for i, (ln, kind) in enumerate(events):
            if kind == "assign":
                if state == "open":
                    print(f"    [SKIP] {fn.name}: {var}@L{ln} 同变量上一条连接未关闭就重新赋值 —— 人工处理")
                    cur = None
                    continue
                node = next(n for n in ast.walk(fn) if isinstance(n, ast.Assign) and n.lineno == ln)
                if _in_try_body(fn, node):
                    print(f"    [SKIP] {fn.name}: {var}@L{ln} 位于既有 try 体内 —— 套 with 需重排 try 结构, 人工处理")
                    cur = None
                    continue
                state, cur = "open", ln
            else:
                state = "closed"
                # 段末取**下一个赋值之前**的最后一次 close: 提前 return 路径上的
                # close 只是首尾之间的一个事件, 正常路径的 close 才是段末。
                nxt = next((l for l, k in events[i + 1:] if k == "assign"), None)
                if cur is not None and not any(k == "close" for l, k in events[i + 1:] if nxt is None or l < nxt):
                    lo, hi = _bounds_for(cur)
                    if lo <= cur and ln <= hi:      # 段必须整体落在同一语句块内
                        segs.append((cur, ln, var))
                    else:
                        print(f"    [SKIP] {fn.name}: {var}@L{cur} 段越过语句块边界(L{lo}~L{hi}) —— 人工处理")
                    cur = None
        if cur is not None:
            print(f"    [SKIP] {fn.name}: {var}@L{cur} 找不到对应的 close() —— 人工处理")
    out = sorted(set(segs))
    for (a1, c1, _), (a2, c2, _) in zip(out, out[1:]):
        assert not (a1 < a2 <= c1), f"段交叉: L{a1}~L{c1} 与 L{a2}~L{c2}"
    return out


def wrap_segment(src_lines, assign_line, close_line, var):
    """→ 新行列表(替换 [assign_line-1 : close_line])。"""
    def line(i):
        return src_lines[i - 1].rstrip("\n").rstrip("\r")

    assign_src = line(assign_line)
    indent = assign_src[:len(assign_src) - len(assign_src.lstrip())]
    assert "db.get_conn()" in assign_src, f"L{assign_line} 不是预期的 get_conn 赋值: {assign_src!r}"
    assert line(close_line).strip() == f"{var}.close()", f"L{close_line} 不是 {var}.close(): {line(close_line)!r}"

    # ⚠️ 绝不在此补 `{var} = db.get_conn()`: `as {var}` 已经绑定 conn_ctx 内部的那个连接,
    # 再赋一次 = **把被管理的连接丢掉、另开一个没人关的**(实测: 每个站点多漏一个连接,
    # tests/test_updater_fix 等 40 条 PermissionError 就是它咬出来的)。正确形态见
    # app/factors/daily_engine.py（本日已改的参考站点）: with 块内直接用 {var}, 不重开。
    out = [f"{indent}with db.conn_ctx() as {var}:",
           f"{indent}    {BY_BINDING}"]
    # 中间语句块 = 1-based 行号 assign_line+1 .. close_line-1(含)，line(i) 取的是 1-based 行号
    for ln in range(assign_line + 1, close_line):
        raw = line(ln)
        out.append(("    " + raw) if raw.strip() else "")   # 空行保持全空(不留尾随空白)
    # 长度自洽: 新段 = with 行 + 备注行 + 原段中间语句块; 原段 = 赋值行 + 中间块 + close 行,
    # 故两段等长(= close_line - assign_line + 1)。除 with/备注两行外, 中间块必须逐行来自原文。
    span = close_line - assign_line + 1
    assert len(out) == span, f"段长 {len(out)} != {span}"
    for i, ln in enumerate(out[2:]):
        raw = line(assign_line + 1 + i)
        assert ln == (("    " + raw) if raw.strip() else ""), f"第 {i} 行搬运不一致: {ln!r} vs {raw!r}"
    return out


def _norm(src_lines, start, end):
    """把 [start, end] 行整体左移到 0 缩进(去掉**最小**公共前导空白)。

    ⚠️ 不能用 textwrap.dedent: 段首若是 `if` 而其 else 分支在段外, 最小缩进会算成 0。
    这里按"非空行的最小前导空白"逐行剥, 缩进差异被完全抹平, 但**相对缩进与行文本
    一字不动** —— 于是"改前 == 改后"就成了"只动了缩进"的证据。
    """
    block = [ln.rstrip("\n").rstrip("\r") for ln in src_lines[start - 1:end]]
    indents = [len(b) - len(b.lstrip()) for b in block if b.strip()]
    k = min(indents)
    return [b[k:] for b in block]


def prove_only_indent_changed(old_src, new_lines, assign_line, close_line, var) -> None:
    """证明某段"只改了连接管理": 剥掉 with 包裹后必须与原文逐字相同。"""
    wrapped = new_lines[assign_line - 1:close_line]
    indent = old_src[assign_line - 1][:len(old_src[assign_line - 1]) - len(old_src[assign_line - 1].lstrip())]
    assert wrapped[0].rstrip() == f"{indent}with db.conn_ctx() as {var}:", f"L{assign_line}: {wrapped[0]!r}"
    assert wrapped[1].strip() == BY_BINDING, f"L{assign_line+1} 应为备注行: {wrapped[1]!r}"
    old_block = _norm(old_src, assign_line + 1, close_line - 1)     # 原: 中间语句块
    new_block = _norm(wrapped, 3, len(wrapped))                     # 新: with+备注之后的语句块
    assert old_block == new_block, (
        "机械搬运改动了内容:\n  " + "\n  ".join(
            f"- {o!r}\n+ {n!r}" for o, n in zip(old_block, new_block) if o != n))


def process(path: pathlib.Path, apply: bool):
    text = path.read_text(encoding="utf-8")
    src_lines = text.splitlines(keepends=True)
    tree = ast.parse(text)
    segs = []
    for fn in iter_funcs(tree):
        print(f"  {fn.name}:")
        segs += find_segments(src_lines, fn)
    if not segs:
        print("    (无可改段)")
        return 0
    changed_ranges = []
    new_lines = list(src_lines)
    for assign_line, close_line, var in reversed(segs):
        new_lines[assign_line - 1:close_line] = [x + "\n" for x in wrap_segment(src_lines, assign_line, close_line, var)]
        changed_ranges.append((assign_line, close_line, var))
    new_text = "".join(new_lines)
    # ---- 自检 1: 语法可解析 ----
    ast.parse(new_text)
    # ---- 自检 2: 每段"剥掉 with 包裹后与原文逐字相同"(只动了缩进+删 close 行) ----
    for assign_line, close_line, var in reversed(changed_ranges):
        prove_only_indent_changed(src_lines, new_lines, assign_line, close_line, var)
    # ---- 自检 3: 总行数必须**不变** ----
    # 段长恒等于原段长: 原"赋值行 + close 行"两行, 新"with 行 + 赋值行"两行;
    # close 行则必然消失(这正是被上下文管理器接管的那一行)。
    assert len(new_lines) == len(src_lines), f"总行数变了: {len(src_lines)} → {len(new_lines)}"
    print(f"    → {len(segs)} 段: " + ", ".join(f"L{a}~L{c}" for a, c, _ in sorted(changed_ranges)) + "   [逐字不变 OK]")
    if apply:
        path.write_text(new_text, encoding="utf-8", newline="")
    else:
        rel = path.relative_to(BASE).as_posix()
        print("".join(difflib.unified_diff(src_lines, new_lines, f"a/{rel}", f"b/{rel}", n=2)))
    return len(segs)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    total = 0
    for rel in FILES:
        p = BASE / rel
        print(f"== {rel} ==")
        total += process(p, args.apply)
    print(f"\n{'已写入' if args.apply else 'DRY-RUN'}: 共 {total} 段")
    return 0


if __name__ == "__main__":
    sys.exit(main())
