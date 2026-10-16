"""Alpha Factory 表达式引擎(2026-09-11)

## 出处与许可

算子语义**端口自 microsoft/qlib 的 Alpha158**(`qlib/contrib/data/loader.py`,
Apache-2.0)。移植的是**算子的定义**, 不是它的代码: qlib 的表达式跑在它自己的
`.bin` 数据格式上, 本项目跑在 `fe.load_market()` 的 (日期 × 股票) 面板上。
按 Apache-2.0, 保留署名: `microsoft/qlib`, Copyright (c) Microsoft Corporation。

**为什么移植而不是依赖 qlib**: 本项目的硬性不变量是 `engine.run_backtest()` 一行不改、
打分表契约不变。引入 qlib 会带来它自己的数据格式(.bin)、自己的回测器与自己的工作流,
与现有链路冲突。表达式引擎是**自包含**的一层, 移植成本远低于适配成本。

## 语义(与 qlib 对齐的关键点)

| 算子 | 语义 | 易错点 |
|---|---|---|
| `Ref(y, n)` | n 个交易日前的值 | 只允许 **n ≥ 0**(负值 = 前视, 引擎直接报错) |
| `Mean/Sum/Std/Var/Max/Min(y, n)` | 含当日的最近 n 日窗口 | `min_periods=n` —— 窗口不满出 NaN, 不拿半截窗口出数 |
| `Quantile(y, n, q)` | 窗口内 q 分位 | |
| `Rank(y, n)` | 当日值在窗口内的分位 ∈ (0,1] | 定义: `#{k<n : y[t-k] ≤ y[t]} / n`, ties 记 ≤ |
| `Slope/Rsquare/Resi(y, n)` | 窗口内 y 对时间做 OLS | 用**闭式解 + 滚动和**实现(不是 rolling.apply, 快两个数量级) |
| `Corr(a, b, n)` | 窗口内 Pearson 相关 | |
| `IdxMax/IdxMin(y, n)` | 窗口内极值的时间位置, 取值 **1..n** | **n = 当日即极值**, 1 = 极值在最旧端(与 qlib 代码一致) |
| `Greater(a,b)` / `Less(a,b)` | **逐元素取大 / 取小**(不是比较!) | qlib 里 `Greater`=max, `Less`=min |
| `>` `<` `==` | **真比较**, 返回 0/1 | 用于 `Mean($close>Ref($close,1), d)` 这类计数 |
| `Abs / Log / Sign / Add / Sub / Mul / Div` | 逐元素 | `Div` 的分母 0 由调用方加 1e-12 兜底 |

## 与参考实现的对齐审计(2026-09-11, 补齐移植时漏掉的一步)

移植时只对照了 `loader.py` 的**表达式字符串**, 没有对照 `qlib/data/ops.py` 的**算子实现**。
事后把 `ops.py`(jsdelivr 镜像, 45446 字节)拉下来逐算子核对, 发现三处偏差, 全部按参考
实现修正(修正前跑出的读数已快照为证据, 见下):

| 算子 | 原实现 | qlib 参考实现(`qlib/data/ops.py` 行号) | 影响 |
|---|---|---|---|
| `Rank` | 数"过去值 ≥ 当日" | `rank(pct=True)`(:1133) ⇒ **值越大分位越高** | **bug**(与本节文档自相矛盾); 5 个候选 `RANK5..RANK60` 的读数反号 |
| `IdxMax/IdxMin` | 距今天数(0..n−1) | `argmax()+1`(:971/:1033) ⇒ **n = 当日即极值** | 与 qlib **代码**互为镜像(qlib 自己的注释却写的是"距今天数"); 15 个候选的读数反号 |
| `Mad` | 对**中位数**的平均绝对偏差 | 对**均值**的平均绝对偏差(:1118) | Alpha158 未用到 ⇒ 对候选读数**零影响** |

**为什么以代码为准而不是以注释为准**: 本库的用途是"当公开标尺用"(`zoos.py` 的定位),
对齐的必须是**能复现 qlib 数字的那个东西**。三处都只影响**方向**(仿射镜像), 对
GBDT 模型的信息量**为零**(单调变换不改树的分裂结构) —— 受影响的只有门禁的定向判据
(`head_excess` 的符号)。故修正后**全量 158 条重跑**, 门禁阈值与 Bonferroni 分母不变。

## Look-ahead 守卫(三层, 2026-09-11)

1. **静态**: `Ref` 的 n 必须 ≥ 0; 未知算子/未知变量直接抛错
2. **动态**: `verify_causal()` —— 用「截断重算」比对: 取一段历史, 分别在全量面板与
   截止到该日的面板上算同一个表达式, 要求逐位相同。任何前视都会被这条抓住
   (前视的定义就是"值依赖于未来数据", 截断后必然改变)
3. **口径**: 面板本身来自 `fe.load_market()`, 与生产因子同源; 复权用 `adj_factor`
   (后复权), 停牌日 NaN 保持 NaN, 不 forward-fill(forward-fill 会造出"停牌期间也有价格"
   的假截面, 是回测虚高的常见来源)
"""
from __future__ import annotations

import re

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# 变量: %-量价面板的列名映射。`vwap` 由 amount/vol 现算(Tushare: amount 千元, vol 手)
# ---------------------------------------------------------------------------
VARS = {
    "open": "open",
    "high": "high",
    "low": "low",
    "close": "close",
    "volume": "vol",
    "amount": "amount",
    "vwap": "vwap",
    "pre_close": "pre_close",
    "turnover": "turnover",
    "total_mv": "total_mv",
    "circ_mv": "circ_mv",
    "pb": "pb",
    "pe_ttm": "pe_ttm",
}

# 滚动窗口算子(第二参数是窗口长度)
ROLLING_OPS = {"Ref", "Delta", "Mean", "Sum", "Std", "Var", "Max", "Min", "Med",
               "Mad", "Quantile", "Rank", "Slope", "Rsquare", "Resi", "IdxMax",
               "IdxMin"}
# 双序列滚动算子
PAIR_ROLLING_OPS = {"Corr", "Cov"}
# 逐元素算子
ELEM_UNARY = {"Abs", "Log", "Sign", "Sqrt", "Exp"}
ELEM_BINARY = {"Add", "Sub", "Mul", "Div", "Pow", "Greater", "Less", "GreaterEqual",
               "LessEqual"}
COMPARE_OPS = {">", "<", "==", "!=", ">=", "<="}
MAX_ARGS = 8


class ExprError(ValueError):
    """表达式非法(语法/未知算子/前视)"""


# ===========================================================================
# 解析: 递归下降 → 元组 AST
# ===========================================================================
_TOKEN = re.compile(r"""
    \s*(?:
        (?P<num>\d+\.\d*(?:[eE][-+]?\d+)?|\.\d+|\d+(?:[eE][-+]?\d+)?)
      | (?P<var>\$[A-Za-z_][A-Za-z0-9_]*)
      | (?P<name>[A-Za-z_][A-Za-z0-9_]*)
      | (?P<op>[<>!=]=|[+\-*/(),<>])
    )""", re.X)


def _tokenize(s: str) -> list:
    out, i = [], 0
    while i < len(s):
        m = _TOKEN.match(s, i)
        if not m:
            raise ExprError(f"无法解析的位置 {i}: {s[i:i + 20]!r} (表达式: {s[:80]})")
        i = m.end()
        kind = m.lastgroup
        out.append((kind, m.group(kind)))
    return out


class _Parser:
    """文法(优先级由低到高): cmp → add → mul → unary → atom"""

    def __init__(self, expr: str):
        self.expr = expr
        self.toks = _tokenize(expr)
        self.i = 0

    def peek(self):
        return self.toks[self.i] if self.i < len(self.toks) else (None, None)

    def take(self):
        t = self.peek()
        self.i += 1
        return t

    def expect(self, ch):
        k, v = self.take()
        if v != ch:
            raise ExprError(f"期望 {ch!r} 实得 {v!r} (表达式: {self.expr[:80]})")

    def parse(self):
        node = self.cmp()
        if self.i != len(self.toks):
            raise ExprError(f"表达式尾部有多余记号: {self.toks[self.i:]} ({self.expr[:80]})")
        return node

    def cmp(self):
        node = self.add()
        while True:
            k, v = self.peek()
            if k == "op" and v in COMPARE_OPS:
                self.take()
                node = ("cmp", v, node, self.add())
            else:
                return node

    def add(self):
        node = self.mul()
        while True:
            k, v = self.peek()
            if k == "op" and v in "+-":
                self.take()
                node = ("bin", v, node, self.mul())
            else:
                return node

    def mul(self):
        node = self.unary()
        while True:
            k, v = self.peek()
            if k == "op" and v in "*/":
                self.take()
                node = ("bin", v, node, self.unary())
            elif k == "op" and v == "(":      # 隐式函数调用已在 unary 处理
                return node
            else:
                return node

    def unary(self):
        k, v = self.peek()
        if k == "op" and v == "-":
            self.take()
            return ("neg", self.unary())
        if k == "op" and v == "+":
            self.take()
            return self.unary()
        return self.atom()

    def atom(self):
        k, v = self.take()
        if k == "num":
            return ("num", float(v))
        if k == "var":
            name = v[1:].lower()
            if name not in VARS:
                raise ExprError(f"未知变量 ${v[1:]} —— 可用: {sorted(VARS)}")
            return ("var", name)
        if k == "name":
            if self.peek()[1] != "(":
                raise ExprError(f"标识符 {v} 后面不是 '(' —— 本引擎无裸常量名(表达式: {self.expr[:80]})")
            self.expect("(")
            args = []
            if self.peek()[1] != ")":
                args.append(self.cmp())
                while self.peek()[1] == ",":
                    self.take()
                    args.append(self.cmp())
            self.expect(")")
            if len(args) > MAX_ARGS:
                raise ExprError(f"{v} 参数过多")
            return ("call", v, tuple(args))
        if k == "op" and v == "(":
            node = self.cmp()
            self.expect(")")
            return node
        raise ExprError(f"意外的记号 {v!r} (表达式: {self.expr[:80]})")


def parse(expr: str):
    """表达式字符串 → AST(可缓存, 见 `parse_cached`)"""
    return _Parser(expr).parse()


_PARSE_CACHE: dict = {}


def parse_cached(expr: str):
    if expr not in _PARSE_CACHE:
        _PARSE_CACHE[expr] = parse(expr)
    return _PARSE_CACHE[expr]


# ===========================================================================
# 面板
# ===========================================================================
def build_panel(m: dict, since: str = None) -> dict:
    """`fe.load_market()` 的返回 → 表达式引擎的变量面板。

    vwap 由 amount/vol 现算: Tushare `daily.amount` 单位千元、`vol` 单位手,
    故 vwap = amount*1000 / (vol*100) = amount*10/vol(元/股), 与 close 同量纲。
    分母为 0(停牌)出 NaN, 不 ffill。
    ⚠️ 这份量纲假设在 app/portfolio/chase.py 的 vwap5(5 日聚合版 Σamount/Σvol)
    有**第二个使用点** —— 换数据源(单位变化)必须两处同步,
    静态钉在 tests/test_chase.py::TestVwapUnitCrossReference。
    """
    panel = {k: m[k] for k in ("open", "high", "low", "close", "pre_close",
                               "vol", "amount", "turnover", "total_mv", "circ_mv",
                               "pb", "pe_ttm") if k in m}
    vwap = (m["amount"] * 10.0) / m["vol"].replace(0, np.nan)
    panel["vwap"] = vwap
    if since:
        ts = pd.Timestamp(since)
        panel = {k: v.loc[v.index >= ts] for k, v in panel.items()}
    return panel


# ===========================================================================
# 求值
# ===========================================================================
class Engine:
    """在固定面板上求值一组表达式。**子表达式结果全局缓存** —— Alpha158 里
    `$close` 被引用上百次, `Ref($close,1)` 被引用几十次, 不缓存会重复算几十遍。"""

    def __init__(self, panel: dict):
        self.panel = panel
        self.cache: dict = {}
        self._n_dates = len(next(iter(panel.values())))

    # -- 工具 ---------------------------------------------------------------
    def _key(self, node) -> str:
        return repr(node)

    def prune(self):
        """只保留变量节点(面板本身)的缓存。

        ⚠️ 必须每算完一个表达式就调一次: 一个 (T,N) 中间帧在 4700×5900 上是 222MB,
        而 Alpha158 有 158 个表达式 —— 全部缓存会直接 OOM。跨表达式仍共享 `$close`
        这类变量(它们本来就是面板本身, 不额外占内存)。
        """
        self.cache = {k: v for k, v in self.cache.items() if k.startswith("('var'")}

    def eval(self, expr_or_ast) -> pd.DataFrame:
        ast = parse_cached(expr_or_ast) if isinstance(expr_or_ast, str) else expr_or_ast
        self.prune()
        try:
            return self._eval(ast)
        finally:
            self.prune()

    def _eval(self, node) -> pd.DataFrame:
        k = self._key(node)
        hit = self.cache.get(k)
        if hit is not None:
            return hit
        out = self._eval_uncached(node)
        self.cache[k] = out
        return out

    def _eval_uncached(self, node) -> pd.DataFrame:
        kind = node[0]
        if kind == "num":
            return node[1]                       # 标量, 由上层广播
        if kind == "var":
            return self.panel[VARS[node[1]]]
        if kind == "neg":
            return -self._eval(node[1])
        if kind == "bin":
            op, a, b = node[1], self._eval(node[2]), self._eval(node[3])
            if op == "+":
                return a + b
            if op == "-":
                return a - b
            if op == "*":
                return a * b
            return a / b
        if kind == "cmp":
            op, a, b = node[1], self._eval(node[2]), self._eval(node[3])
            r = {">": a > b, "<": a < b, "==": a == b,
                 "!=": a != b, ">=": a >= b, "<=": a <= b}[op]
            return r.astype("float64")
        if kind == "call":
            return self._call(node[1], [self._eval(x) for x in node[2]])
        raise ExprError(f"未知 AST 节点: {kind}")

    def _call(self, fn: str, a: list) -> pd.DataFrame:
        # -- 逐元素 --------------------------------------------------------
        if fn in ELEM_UNARY:
            if len(a) != 1:
                raise ExprError(f"{fn} 需要 1 个参数")
            x = a[0]
            return {"Abs": abs, "Sign": np.sign, "Log": np.log,
                    "Sqrt": np.sqrt, "Exp": np.exp}[fn](x)
        if fn in ELEM_BINARY:
            if len(a) != 2:
                raise ExprError(f"{fn} 需要 2 个参数")
            x, y = a
            return {"Add": lambda: x + y, "Sub": lambda: x - y,
                    "Mul": lambda: x * y, "Div": lambda: x / y,
                    "Pow": lambda: x ** y,
                    # qlib 语义: Greater = 逐元素取大, Less = 逐元素取小(不是比较!)
                    "Greater": lambda: np.maximum(x, y),
                    "Less": lambda: np.minimum(x, y),
                    "GreaterEqual": lambda: np.maximum(x, y),
                    "LessEqual": lambda: np.minimum(x, y)}[fn]()

        # -- 对数(*必须*已按 VARS 解析成 DataFrame; 标量只允许作窗口/分位参数) ----
        if fn in ROLLING_OPS:
            return self._rolling(fn, a)
        if fn in PAIR_ROLLING_OPS:
            return self._pair_rolling(fn, a)
        raise ExprError(f"未知算子 {fn} —— 可用: "
                        f"{sorted(ROLLING_OPS | PAIR_ROLLING_OPS | ELEM_UNARY | ELEM_BINARY)}")

    # -- 滚动 ---------------------------------------------------------------
    @staticmethod
    def _win(a, i) -> int:
        """取第 i 个参数作窗口长度(必须是正整数常量)"""
        v = a[i]
        if not np.isscalar(v) and np.ndim(v) == 0:
            v = float(v)
        if not isinstance(v, (int, float)) or float(v) != int(v) or int(v) <= 0:
            raise ExprError(f"窗口长度必须是正整数常量, 实得 {v!r}(本引擎不支持动态窗口)")
        return int(v)

    def _rolling(self, fn: str, a: list) -> pd.DataFrame:
        if fn == "Ref":
            if len(a) != 2:
                raise ExprError("Ref 需要 2 个参数")
            n = self._win(a, 1)
            if n < 0:
                raise ExprError(f"Ref 的窗口为负({n}) —— 那是前视, 本引擎禁止")
            return a[0].shift(int(n))

        if fn in ("Delta",):
            n = self._win(a, 1)
            return a[0] - a[0].shift(n)

        if fn in ("Quantile",):
            x, n, q = a[0], self._win(a, 1), float(a[2])
            return x.rolling(n, min_periods=n).quantile(q)

        if fn in ("Slope", "Rsquare", "Resi"):
            return self._linreg(fn, a[0], self._win(a, 1))

        if fn in ("IdxMax", "IdxMin"):
            return self._idx_extreme(fn, a[0], self._win(a, 1))

        if fn == "Rank":
            return self._rolling_rank(a[0], self._win(a, 1))

        if fn == "Mad":
            # ⚠️ qlib 的 `Mad`(`qlib/data/ops.py:1118`)是**对均值的**平均绝对偏差
            # (`np.mean(np.abs(x - x.mean()))`), 不是名字暗示的中位数绝对偏差 ——
            # 2026-09-11 修: 原实现用了 rolling.median, 且结构上**恒为 NaN**: 外层
            # `rolling(n, min_periods=n)` 需要窗口内 n 个残差非 NaN, 而残差的前 n−1 个
            # 恒为 NaN(内层均值还没满窗) ⇒ 一行都出不来。mad 里的 |·| 是非线性,
            # 没有滚动和闭式解, 只能 apply(qlib 自己也是 apply)。
            x, n = a[0], self._win(a, 1)
            return x.rolling(n, min_periods=n).apply(
                lambda w: float(np.mean(np.abs(w - w.mean()))), raw=True)

        x, n = a[0], self._win(a, 1)
        r = x.rolling(n, min_periods=n)
        return {"Mean": r.mean, "Sum": r.sum, "Std": r.std, "Var": r.var,
                "Max": r.max, "Min": r.min, "Med": r.median}[fn]()

    def _pair_rolling(self, fn: str, a: list) -> pd.DataFrame:
        if len(a) != 3:
            raise ExprError(f"{fn} 需要 3 个参数 (a, b, n)")
        x, y, n = a[0], a[1], self._win(a, 2)
        if fn == "Corr":
            return x.rolling(n, min_periods=n).corr(y)
        cov = x.rolling(n, min_periods=n).cov(y)
        return cov

    # -- 线性回归(闭式解, 只用滚动和 —— 比 rolling.apply 快两个数量级) -------
    def _linreg(self, fn: str, x: pd.DataFrame, n: int) -> pd.DataFrame:
        """窗口内 x 对 x 轴(该窗口最旧=0 … 最新=n-1)做 OLS。

        Σxy 的滚动化: 设 m 为行号, base = 当前行号 − n + 1, 则窗口内
        Σ_{k=0..n-1} k·y[n-n+1+k] = Σ m·y[m] − base·Σ y[m]
        —— 两个都只是普通的滚动和, 故用 cumsum 即可, 无需逐窗口 apply。
        """
        idx = x.index
        m = pd.Series(np.arange(len(idx), dtype="float64"), index=idx)
        mp = m.to_frame().values * np.ones((1, x.shape[1]))   # (T, N) 行号面板
        mp = pd.DataFrame(mp, index=idx, columns=x.columns)

        r = x.rolling(n, min_periods=n)
        A = r.sum()                                  # Σy
        B = (x * mp).rolling(n, min_periods=n).sum()  # Σ m·y
        base = m - (n - 1)
        Sxy_raw = B.sub(A.mul(base, axis=0))          # Σ x·y   (x = 0..n-1)
        sx = n * (n - 1) / 2.0
        sxx = n * (n * n - 1) / 12.0                  # Σ(x−x̄)²
        sxy = Sxy_raw - A * (sx / n)
        slope = sxy / sxx
        ybar = A / n
        if fn == "Slope":
            return slope
        if fn == "Resi":
            # 最后一点(位置 n−1)的残差 = y − (ȳ + slope·((n−1) − x̄))
            return x - (ybar + slope * ((n - 1) - (n - 1) / 2.0))
        Syy = (x * x).rolling(n, min_periods=n).sum() - A * A / n
        return (sxy * sxy) / (sxx * Syy)

    # -- 滚动极值位置 -------------------------------------------------------
    def _idx_extreme(self, fn: str, x: pd.DataFrame, n: int) -> pd.DataFrame:
        """窗口内最高(IdxMax)/最低(IdxMin)价的**时间位置**, 取值 1..n。

        对上 qlib `IdxMax` = `rolling(N).apply(lambda x: x.argmax() + 1)`(`qlib/data/ops.py:971`);
        窗口按**时间正序**(最旧在前), 故 **n = 当日就是最高点, 1 = 最高点在窗口最旧端**。
        ⚠️ qlib 自己的注释写的是 "days between current date and previous highest price date"
        (= 距今天数), 与它的代码**互为镜像**。本移植以**代码**为准(见模块头 "对齐" 一节)。

        并列时取**最旧**位置: 与前值严格更优才替换(等价于 `argmax` 取首个出现的语义)。
        n 最大 60, 故 O(n) 帧比较(累积式, 不做 rolling.apply —— 那在 4700×5900 面板上是小时级)。
        """
        better = (lambda v, b: v > b) if fn == "IdxMax" else (lambda v, b: v < b)
        best = x.shift(n - 1).copy()
        pos = pd.DataFrame(np.where(best.notna().values, n - 1, np.nan),
                           index=x.index, columns=x.columns)   # pos = 距今天数
        for k in range(n - 2, -1, -1):
            v = x.shift(k)
            m = better(v, best)
            best = best.where(~m, v)
            pos = pos.where(~m, float(k))
        return n - pos                                           # 距今天数 → qlib 的位置

    # -- 滚动分位 -----------------------------------------------------------
    def _rolling_rank(self, x: pd.DataFrame, n: int) -> pd.DataFrame:
        """当日值在窗口内的分位 = #{k<n : y[t−k] ≤ y[t]} / n ∈ (0, 1]。

        对上 qlib `Rank` = `rolling(N).rank(pct=True)`(`qlib/data/ops.py:1133`):
        **值越大分位越高**(窗口内最高 → 1.0)。

        ⚠️ 2026-09-11 修: 原实现写的是 `x <= x.shift(k)`(= 数"过去值 ≥ 当日"的个数),
        与本节文档和 qlib 都**反号**(单调递减映射 ⇒ 截面排序整个反转, 头部变成选
        "窗口内最便宜"的票)。修前读数已快照到 `tmp/alpha_factory_prefix_rank.json`。
        逐元素相等时的处理: 计入(等价于 `rank(pct=True)` 的平均秩), 不做逐位复刻。

        n 帧比较, 同 `_idx_extreme` 的理由不用 rolling.apply。
        """
        cnt = pd.DataFrame(np.where(x.notna().values, 1.0, np.nan),
                           index=x.index, columns=x.columns)      # k=0: x ≤ x 恒真
        for k in range(1, n):
            cnt = cnt + (x.shift(k) <= x).astype("float64")
        # 窗口不满(min_periods)的位置: 前 n-1 行的计数不完整 ⇒ 置 NaN
        return (cnt / n).where(x.rolling(n, min_periods=n).count() > 0)


# ===========================================================================
# Look-ahead 守卫
# ===========================================================================
def ref_windows(ast) -> list:
    """AST 里所有 Ref/Delta 的窗口(用于静态检查)"""
    out = []
    if node_is_call(ast, "Ref") or node_is_call(ast, "Delta"):
        out.append(int(ast[2][1][1]))
    for ch in node_children(ast):
        out.extend(ref_windows(ch))
    return out


def node_is_call(node, name: str) -> bool:
    return isinstance(node, tuple) and node[0] == "call" and node[1] == name


def node_children(node):
    if not isinstance(node, tuple):
        return []
    if node[0] == "call":
        return list(node[2])
    return [x for x in node[1:] if isinstance(x, tuple)]


def verify_causal(expr: str, m: dict, cutoff: str = "2024-06-28",
                  probe: int = 3, since: str = "2019-01-01") -> dict:
    """**动态**前视检验: 截断重算。

    前视的定义就是「当日的值依赖未来的数据」。故取截止日 D, 分别用
    (a) 全量面板 与 (b) 只到 D 的面板 算同一个表达式, 比较 D 之前的若干截面 ——
    前视一旦存在, 截断后必然改变。返回读数字典(不抛错, 由调用方判定)。
    """
    full = build_panel(m, since=since)
    ts = pd.Timestamp(cutoff)
    cut = build_panel(m, since=since)
    cut = {k: v.loc[v.index <= ts] for k, v in cut.items()}

    a = Engine(full).eval(expr)
    b = Engine(cut).eval(expr)
    check_dates = [d for d in a.index if d <= ts][-probe:]
    diffs = []
    for d in check_dates:
        if d not in b.index:
            continue
        x, y = a.loc[d], b.loc[d]
        common = x.notna() & y.notna()
        if int(common.sum()) == 0:
            continue
        diffs.append(float((x[common] - y[common]).abs().max()))
    return {"expr": expr, "cutoff": cutoff, "n_dates": len(diffs),
            "max_abs_diff": max(diffs) if diffs else np.nan,
            "causal": bool(diffs) and max(diffs) == 0.0}


__all__ = ["Engine", "ExprError", "build_panel", "parse", "parse_cached",
           "verify_causal", "ref_windows", "VARS", "ROLLING_OPS"]
