"""ONEOFF(2026-09-20, 批次 4): alpha 因子面板**双尺**门禁 A/B —— RAW 裸价 vs ADJ 复权。

判据跑前冻结在 `plans/b1-q1-dual-scale-gate-ab-20260920.md`（G0 复现 / G1 主门 /
G2 临界门 / G3 构造自证 / G4 零写入自证）。本轮**只测"值不值得统一尺"，不做统一**：
`app/alpha/expr.py` 一个字都不改，`alpha_bank` 一个字节都不写。

两臂差在哪：唯一差别是 `expr.build_panel` 的输出 —— ADJ 臂把
`open/high/low/close/pre_close` 与 `vwap` 乘 `adj_factor`（vwap=amount*10/vol 是元/股，
与 close 同尺，不一起乘会"先修好又弄坏"）。PIT 可交易域与 `forward_return` 标签都从
**未缩放的 `m`** 构造 ⇒ 两臂必须完全相同（G3-③ 就是抓这个）。

⚠️ 一处**有意的方法偏离**（预注册 §3）：两臂对称地把 `bank.check_duplicate` 换成恒不重复的桩。
理由：门禁 v2 ③ 会把"与在册 RAW 值面板相关度高"的那批（恰是最不受尺子影响的表达式）
判成撞车而 `continue` ⇒ 翻动数系统性偏小；该闸保护的是登记簿，而 `save=False` 根本不写登记簿。
兜底：G3-④ 要求两臂各 158 条。

跑法（每臂一个子进程，面板 ~1.4GB + 中间帧 ~222MB，同机还有服务在跑）：
    D:\\Python313\\python.exe -X utf8 scripts\\oneoff\\ab_alpha_panel_scale_gate.py --arm raw
    D:\\Python313\\python.exe -X utf8 scripts\\oneoff\\ab_alpha_panel_scale_gate.py --arm adj
    D:\\Python313\\python.exe -X utf8 scripts\\oneoff\\ab_alpha_panel_scale_gate.py --arm raw2
    D:\\Python313\\python.exe -X utf8 scripts\\oneoff\\ab_alpha_panel_scale_gate.py --arm merge
产物：experiments/b_stage_20260920/alpha_panel_scale_gate.{json,md}（进 git）+ logs/ 同名。
"""
import argparse
import ast
import hashlib
import json
import sqlite3
import sys
import time
import types
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE))
TRACK = BASE / "experiments" / "b_stage_20260920"
LOGS = BASE / "logs"
DB = BASE / "data" / "neweco.db"
POOL = BASE / "data" / "alpha_pool"
ARMS = ("raw", "adj")            # merge 需要的两臂（raw2 只是 G0① 的确定性复跑）
PRICE_VARS = ("open", "high", "low", "close", "pre_close", "vwap")
TOL_HEAD = 1e-6                 # G3-① 容差（预注册里改正过：逐位在 float32 下不可达）
TOL_REL = 1e-6                  # G3-② 相对容差
N_EXPECT = 158                  # qlib158 条数（G3-④）
EVAL_SINCE = "2019-01-01"


def arm_file(which: str) -> Path:
    return TRACK / f"_arm_{which}.json"


def ro() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
    conn.execute("PRAGMA query_only=1")
    return conn


def _jsonable(x):
    """numpy 标量/时间戳 → 原生类型。**不能图省事用 `default=str`**：那会把 float 变成
    字符串，G0① 的"逐位相同"就退化成"打印精度相同"。"""
    if isinstance(x, dict):
        return {str(k): _jsonable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_jsonable(v) for v in x]
    if isinstance(x, (np.bool_, bool)):
        return bool(x)
    if isinstance(x, (np.integer, int)):
        return int(x)
    if isinstance(x, (np.floating, float)):
        return float(x)
    if isinstance(x, pd.Timestamp):
        return x.strftime("%Y-%m-%d")
    return x


# ---------------------------------------------------------------- G4 零写入指纹
def bank_fp() -> dict:
    conn = ro()
    try:
        r = conn.execute("SELECT COUNT(*), MAX(created), MAX(updated) FROM alpha_bank").fetchone()
        n_pass = conn.execute("SELECT COUNT(*) FROM alpha_bank WHERE PASS=1").fetchone()[0]
    finally:
        conn.close()
    return {"rows": int(r[0]), "max_created": str(r[1]), "max_updated": str(r[2]),
            "n_pass": int(n_pass)}


def pool_fp() -> dict:
    if not POOL.exists():
        return {"exists": False, "files": 0, "sum_mtime_ns": 0, "sum_size": 0}
    files = [p for p in POOL.rglob("*") if p.is_file()]
    st = [p.stat() for p in files]
    return {"exists": True, "files": len(files),
            "sum_mtime_ns": int(sum(s.st_mtime_ns for s in st)),
            "sum_size": int(sum(s.st_size for s in st))}


# ---------------------------------------------------------------- G3-① 尺度无关族
# 上一版用正则（"不含 Ref/窗口/Rank"）判族 —— **判错了**：`$close-$open` 也不含这些，
# 但它是**一次齐次**，乘 per-name 的 adj 会改变横截面秩。真正"逐位不受影响"的只有
# **零次齐次**（同日比值）表达式。故改用 AST 求齐次次数：价格变量=1、非价格变量/常数=0、
# 乘=相加、除=相减、加减与 Min/Max/Abs/Sign 要求两侧同次。任一处不满足 ⇒ None（不入族）。
PRICE_SET = set(PRICE_VARS)
_SAME_DEGREE_FUNCS = {"Abs", "Sign", "Min", "Max"}


def _degree(node):
    if isinstance(node, ast.Expression):
        return _degree(node.body)
    if isinstance(node, ast.Name):
        return 1 if node.id in PRICE_SET else 0
    if isinstance(node, ast.Constant):
        return 0 if isinstance(node.value, (int, float)) else None
    if isinstance(node, ast.UnaryOp):
        return _degree(node.operand) if isinstance(node.op, (ast.USub, ast.UAdd)) else None
    if isinstance(node, ast.BinOp):
        a, b = _degree(node.left), _degree(node.right)
        if a is None or b is None:
            return None
        if isinstance(node.op, (ast.Add, ast.Sub)):
            return a if a == b else None
        if isinstance(node.op, ast.Mult):
            return a + b
        if isinstance(node.op, ast.Div):
            return a - b
        return None
    if isinstance(node, ast.Call):
        f = node.func
        if not isinstance(f, ast.Name) or f.id not in _SAME_DEGREE_FUNCS:
            return None                     # Ref/Rank/ZScore/Delta… 一律排除
        ds = [_degree(a) for a in node.args]
        if any(d is None for d in ds) or len(set(ds)) != 1:
            return None
        return ds[0]
    return None


def scale_free_family(items) -> list:
    """**可证明**零次齐次（= 同尺无关）的表达式名。宁可少判，不可错判：入族者必须两臂逐位相同。"""
    out = []
    for row in items:
        if len(row) < 2:
            continue
        name, e = str(row[0]), str(row[1])
        try:
            d = _degree(ast.parse(e.replace("$", ""), mode="eval"))
        except SyntaxError:
            d = None
        if d == 0:
            out.append(name)
    return sorted(out)


# ---------------------------------------------------------------- ADJ 臂的面板
def make_adj_build(orig_build, diag: dict):
    def build(m):
        p = orig_build(m)
        ref = p["close"]
        adj = m["adj"].reindex(index=ref.index, columns=ref.columns)
        cr, ar = ref.to_numpy(), adj.to_numpy()
        diag["adj_shape"] = list(m["adj"].shape)
        diag["close_shape"] = list(ref.shape)
        diag["adj_columns_missing"] = int(len(set(ref.columns) - set(m["adj"].columns)))
        # 复权因子缺行 ⇒ 乘完把有效价格打成 NaN。本轮不写库，但这件事**必须可见**：
        # 真要统一尺，这里怎么处理是批次 5 的口径问题，不在本轮偷偷决定。
        diag["adj_missing_on_price_cells"] = int((np.isfinite(cr) & ~np.isfinite(ar)).sum())
        diag["adj_zero_on_price_cells"] = int((np.isfinite(cr) & (cr != 0) & (ar == 0)).sum())
        dtype0 = str(cr.dtype)                     # DataFrame 没有 .dtype ⇒ 取 numpy 的
        diag["dtype_before"] = dtype0
        scaled, before, after = [], {}, {}
        for k in PRICE_VARS:
            if k not in p:
                continue
            dt = str(p[k].to_numpy().dtype)      # **逐帧保 dtype**：两臂只能差在尺度上，
            before[k] = dt                        # 不能顺便把 float64 的 vwap 降成 float32
            p[k] = (p[k] * adj).astype(dt)      # 乘法同 factor_engine.py:252（pandas 3 下
            #                                              copy=False 已废弃，会刷警告）
            scaled.append(k)
            after[k] = str(p[k].to_numpy().dtype)
        diag["scaled"] = scaled
        diag["dtype_each"] = before
        diag["dtype_after"] = after
        return p
    return build


# ---------------------------------------------------------------- G3-② 除权抽样探针
def exdiv_probe(E, m, which: str) -> dict:
    """抽**一次真除权**（窗内相对步长最大的那格），把 `$close/$vwap/$total_mv` 的两臂
    比值交给 merge 判。

    样本选择只依赖 `m["adj"]`（两臂同一份、未被 patch）⇒ 两臂必然选中同一 (code, dates)，
    merge 里再断言一次；不一致就说明 patch 泄漏进了 `m`。

    ⚠️ 第一版按 `adj.diff().abs() > 1e-9` 找"除权"，实测选中的是
    `000001.SZ 20191231→20200102, adj 109.168999→109.169403` —— **相对步长 3.7e-6**，
    那就是 float32 在 109 附近的表示间隔（~7.6e-6），根本不是除权。
    探针照样"通过"，但它检验的是一个不存在的台阶 ⇒ **自证不会失败 = 自证无意义**。
    现按 `adj_t/adj_{t-1} − 1` 的**绝对值最大**取样本，并要求 ≥1e-3（0.1%）才算数。
    """
    adj = m["adj"]
    n = len(adj)
    if n < 120:
        raise RuntimeError(f"面板只有 {n} 行，不足以做除权抽样")
    # 区间按**比例**取（上一版写死 iloc[3000:4000]：全历史面板 3331 行时落在末段、
    # 而短面板只有 ~2050 行 ⇒ 切片为空，又是一个"自证自己坏掉"）
    lo, hi = int(n * 0.2), int(n * 0.6)
    close = m["close"]
    dev = (adj / adj.shift(1) - 1.0).abs()
    # 台阶要落在**前后两天都有成交价、且 `total_mv` 有值**的格子上：
    # ① `adj` 比 `close` 多 3 列（只出现在复权表里的代码）—— 第二版因此在 600069.SH
    #    20220905→06 选中 8.7 倍"台阶"，而那天没有行情（两臂 close 皆 NaN）⇒ 那是因子修订
    #    不是除权；
    # ② 第三版选中的 920729.BJ 有行情，但 `total_mv` 两日皆 NaN ⇒ "没误乘非价格变量"
    #    一项退化成 `NaN is NaN`（json 的 NaN 是同一个对象，list 比较走身份短路 ⇒ **假通过**）。
    pok = close.notna() & close.shift(1).notna()
    if "total_mv" in m:
        mv = m["total_mv"]
        pok = pok & mv.notna() & mv.shift(1).notna()
    pok = pok.reindex(index=dev.index, columns=dev.columns).fillna(False)
    dev = dev.where(pok).iloc[lo:hi]
    arr = np.where(np.isfinite(dev.to_numpy()), dev.to_numpy(), -1.0)
    i, j = np.unravel_index(int(np.argmax(arr)), arr.shape)
    if arr[i, j] < 1e-3:
        raise RuntimeError(f"窗 {lo}:{hi} 内最大相对步长只有 {arr[i, j]:.3g} <1e-3 ⇒ 无真除权可抽")
    code = adj.columns[j]
    d1 = dev.index[i]
    d0 = adj.index[int(adj.index.get_loc(d1)) - 1]
    panel = E.build_panel(m)                    # ← 本臂的版本（RAW=原函数，ADJ=乘过 adj 的）
    keys = ("close", "vwap", "total_mv")

    def val(df, d):
        if df is None or d not in df.index or code not in df.columns:
            return None
        return float(df.loc[d, code])

    return {"arm": which, "code": str(code),
            "dates": [d0.strftime("%Y%m%d"), d1.strftime("%Y%m%d")],
            "rel_step": float(arr[i, j]),
            "adj": [float(adj.loc[d0, code]), float(adj.loc[d1, code])],
            "panel": {k: [val(panel.get(k), d0), val(panel.get(k), d1)] for k in keys},
            "raw_close": [val(m["close"], d0), val(m["close"], d1)]}


# ---------------------------------------------------------------- G3-② 单独探针（一进程内两臂同一份 m）
def probe_file() -> Path:
    return TRACK / "_probe.json"


def mech_file() -> Path:
    return TRACK / "_mech.json"


def run_probe() -> int:
    """不求值、不进工厂主流程：只加载行情 + 用**两臂各自的 `build_panel`** 各抽一次样。

    为什么单开一臂：① 两臂在同一进程里比同一份 `m` ⇒ "patch 没泄漏到 m"这件事不再靠
    两个进程各算一遍来间接推断；② 发现探针取样规则写错时，不必重跑 2×6.5 分钟的求值。
    它与 ADJ 臂的绑定：`adj_diag` 必须与 `--arm adj` 那次的 `adj_diag` **逐字段相同**，
    merge 里查这一条（不同 = 探针检验的不是那条臂用的构造）。
    """
    from app.logger import setup_logger
    setup_logger()
    from app.alpha import expr as E
    from app.alpha import factory as F

    t0 = time.time()
    fp_before = {"bank": bank_fp(), "pool": pool_fp()}
    f = F.Factory(F.PreRegistration(
        intent="批次4 G3-② 探针（只构造面板，不求值、不写库、不进登记簿）",
        eval_since=EVAL_SINCE))
    m = f.market()[0]
    orig = E.build_panel
    diag: dict = {}
    ns_raw = types.SimpleNamespace(build_panel=orig)
    ns_adj = types.SimpleNamespace(build_panel=make_adj_build(orig, diag))
    out = {"raw": _jsonable(exdiv_probe(ns_raw, m, "raw")),
           "adj": _jsonable(exdiv_probe(ns_adj, m, "adj")),
           "adj_diag": _jsonable(diag), "elapsed_s": round(time.time() - t0, 1),
           "fp_before": fp_before, "fp_after": {"bank": bank_fp(), "pool": pool_fp()}}
    TRACK.mkdir(exist_ok=True)
    probe_file().write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8", newline="")
    print("== 探针完成 %.0fs ⇒ %s" % (out["elapsed_s"], probe_file()))
    print("   raw=%s adj=%s rel_step=%.4g" % (out["raw"].get("code"), out["adj"].get("code"),
                                              out["adj"].get("rel_step") or float("nan")))
    for k, v in out["fp_after"].items():
        print("   G4 %s %s ⇒ %s" % (k, "未变" if fp_before[k] == v else "**变了**", v))
    return 0


# ---------------------------------------------------------------- 单臂
def _no_dup(rec, values=None, md=None, allow_rerun=False, corr_threshold=0.0):
    return {"dup": False, "kind": "", "existing": None,
            "detail": "stub（批次4 预注册 §3，两臂对称）"}


def _fp_frame(tag: str, df: pd.DataFrame) -> dict:
    """整帧指纹（G3-③ 要求两臂**完全相同**）：形状 + 有限格数 + 字节 sha256。

    哈希的是**本次跑真正用到的那份**（在调用点包一层），不是事后另算的近似物。
    """
    arr = df.to_numpy()
    if arr.dtype == np.bool_:
        buf, n_fin = np.packbits(arr).tobytes(), int(arr.sum())
    else:
        buf = arr.astype(np.float32, copy=False).tobytes()
        n_fin = int(np.isfinite(arr.astype(np.float64)).sum())
    return {"tag": tag, "shape": list(arr.shape), "n_cells": int(arr.size),
            "n_finite_or_true": n_fin, "sha256": hashlib.sha256(buf).hexdigest()}


def run_arm(which: str) -> int:
    from app.logger import setup_logger
    setup_logger()          # 工厂的逐条判决日志 + "N_used=..." 那行是**证据**，不打开就看不见
    from app.alpha import expr as E
    from app.alpha import zoos
    from app.alpha import bank, gate
    from app.alpha import factory as F

    t_start = time.time()
    fp_before = {"bank": bank_fp(), "pool": pool_fp()}
    items = zoos.all_candidates()
    family = scale_free_family(items)

    diag: dict = {}
    orig_build = E.build_panel
    if which == "adj":
        E.build_panel = make_adj_build(orig_build, diag)

    # G3-③：把**真正被用到的**掩码与标签哈希下来
    hook = {}
    orig_tm = F.tradable_mask

    def _tm(*a, **k):
        mat = orig_tm(*a, **k)
        hook["mask"] = _fp_frame("pit_tradable_mask", mat)
        return mat

    F.tradable_mask = _tm
    orig_fr = gate.forward_return

    def _fr(*a, **k):
        fw = orig_fr(*a, **k)
        hook["label"] = _fp_frame("forward_return", fw)
        return fw

    gate.forward_return = _fr
    bank.check_duplicate = _no_dup                    # 预注册 §3 的对称偏离

    intent = ("批次4 Q1 双尺门禁 A/B | 臂=%s | 判据冻结 plans/"
              "b1-q1-dual-scale-gate-ab-20260920.md | save=False 只判不改 | "
              "撞车闸按 §3 换成对称桩" % which)
    f = F.Factory(F.PreRegistration(intent=intent, eval_since=EVAL_SINCE), allow_rerun=True)
    f.add(F.ZooProposer(items, label="qlib158"))
    n_declared = int(f.declare())
    f.run(eval_since=EVAL_SINCE, save=False)
    recs = f.results

    out = {"arm": which, "elapsed_s": round(time.time() - t_start, 1),
           "n_declared": n_declared, "n_records": len(recs),
           "note": {"n_items": len(items), "family": family},
           "adj_diag": diag, "fingerprints": hook, "fp_before": fp_before,
           "records": _jsonable(recs), "n_duplicates": len(f.duplicates)}
    TRACK.mkdir(exist_ok=True)
    # 先落盘再自证：上一版探针在结果之前抛异常 ⇒ 4 分钟的跑数全丢。
    # **自证不许有能力摧毁被它检验的东西。**
    arm_file(which).write_text(json.dumps(_jsonable(out), ensure_ascii=False),
                               encoding="utf-8", newline="")
    try:
        out["sample"] = _jsonable(exdiv_probe(E, f.market()[0], which))
    except Exception as e:
        out["sample"] = {"error": "%s: %s" % (type(e).__name__, e), "arm": which}
        print("⚠️ G3-② 探针失败（臂结果已先落盘）：%s" % out["sample"]["error"])
    out["fp_after"] = {"bank": bank_fp(), "pool": pool_fp()}
    arm_file(which).write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8", newline="")
    print("== 臂 %s 完成：%d 条 / %.0fs / 撞车桩下 duplicates=%d ⇒ %s"
          % (which, len(recs), out["elapsed_s"], out["n_duplicates"], arm_file(which)))
    for k, v in out["fp_after"].items():
        print("   G4 %s %s ⇒ %s" % (k, "未变" if out["fp_before"][k] == v else "**变了**", v))
    return 0


# ---------------------------------------------------------------- G3-① 机制归因
def run_mech() -> int:
    """回答"解析上不变的 7 条因子，两臂读数为什么还差到 3.2e-5"。

    同一进程、同一份 `m`，走工厂那一条链（`Engine` → `neutralize` → `_masked_panel` →
    `gate.evaluate`），**不求值门禁、不写库**。产出的是**诊断**，不改任何判据数值：
    - 值面板层面的两臂相对差（这才是"尺子乘对了没有"该问的问题）；
    - 逐月末的秩相关、头部选面是否变化；
    - 恒等式核对：`Δhead_观测 == 两臂各自选面在同一份前向收益上的均值差` ⇒
      如果成立，读数的全部差异都来自"**选到了哪些票**"这个**离散步骤**，
      而不是收益算错了 ⇒ 1e-6 那条容差对"秩选出的均值"本来就不成立。
    """
    from app.logger import setup_logger
    setup_logger()
    from app.alpha import expr as E
    from app.alpha import zoos, gate
    from app.alpha import factory as F
    from app.factors import factor_engine as fe

    t0 = time.time()
    fp_before = {"bank": bank_fp(), "pool": pool_fp()}
    all_items = zoos.all_candidates()
    fam = set(scale_free_family(all_items))
    items = [r for r in all_items if str(r[0]) in fam]
    print("== 机制归因：%d 条零次齐次因子 %s" % (len(items), sorted(fam)))

    f = F.Factory(F.PreRegistration(
        intent="批次4 G3-① 机制归因（同进程两臂面板，不求值门禁、不写库）",
        eval_since=EVAL_SINCE))
    m, dates_all, md, zones, fwd, ind = f._prepare(EVAL_SINCE)
    tradable = f._tradable("eval", m, md)
    orig_build = E.build_panel

    def build_all(patched: bool):
        eng_panel = None
        if patched:
            E.build_panel = make_adj_build(orig_build, {})
        try:
            eng_panel = E.build_panel(m)
            eng = E.Engine(eng_panel)
            out = {}
            for row in items:
                nm, e = str(row[0]), str(row[1])
                raw = eng.eval(e)
                if isinstance(raw, pd.Series):
                    raw = raw.to_frame()
                raw = raw.replace([np.inf, -np.inf], np.nan)
                out[nm] = F._masked_panel(fe.neutralize(raw, ind), tradable, md)
                eng.prune()
            return out
        finally:
            E.build_panel = orig_build
            del eng_panel

    P_R, P_A = build_all(False), build_all(True)
    hor = gate.HORIZON
    rows = []
    for nm in sorted(P_R):
        r, a = P_R[nm], P_A[nm]
        cols = r.columns.intersection(a.columns)
        R = r.loc[:, cols]
        A = a.loc[:, cols].reindex(index=R.index, columns=cols)
        both = R.notna() & A.notna()
        # 值差的尺度分母用**逐月末截面 RMS**，不能用 `|value|`：中性化后的因子有大量
        # 趋零格，除以 |value| 会爆炸（第一版就报出 49/292/1050 这种荒谬"相对差"）。
        diff = (A - R).abs().where(both)
        rms = np.sqrt((R.where(both) ** 2).mean(axis=1))
        rel = diff.div(rms.replace(0, np.nan), axis=0)
        det_R = gate.evaluate(R, fwd, md, zones)
        det_A = gate.evaluate(A, fwd, md, zones)
        n_dates = swap_dates = gap_tight = 0
        swap_sizes, rho, ident_err, gaps = [], [], [], []
        for d in R.index:
            fr = fwd.loc[d]
            # **照抄 `gate.evaluate` 的取数**：定义域 = 因子非空 ∩ 前向收益非空。
            # 第一版用"因子非空"当全集去回算 head 超额 ⇒ 分母集合不同 ⇒ 恒等式残差
            # 0.00135（看着像"有第二个通道"，其实是我自己算错）。
            cov = R.loc[d].notna() & fr.notna()
            n = int(cov.sum())
            if n < gate.MIN_NAMES or d not in det_R.index:
                continue
            rv, av, rc = R.loc[d][cov], A.loc[d][cov], fr[cov]
            rho.append(float(rv.corr(av, method="spearman")))
            k = max(1, int(n * gate.TOP_FRAC))
            ordr, orda = rv.sort_values(ascending=False), av.sort_values(ascending=False)
            sel_r, sel_a = list(ordr.index[:k]), list(orda.index[:k])
            if set(sel_r) != set(sel_a):
                swap_dates += 1
                swap_sizes.append(len(set(sel_r) ^ set(sel_a)))
            gaps.append(float((ordr.iloc[k - 1] - ordr.iloc[k]) / rms.loc[d])
                        if k < n else float("nan"))
            mean_all = float(rc.mean())
            e_r = max(abs(float(rc[sel_r].mean()) - mean_all - float(det_R.loc[d, "head_excess"])),
                      abs(float(rc[sel_a].mean()) - mean_all
                          - float(det_A.loc[d, "head_excess"])))
            ident_err.append(e_r)
        n_dates = len(rho)
        relmax = float(np.nanmax(rel.to_numpy())) if both.any().any() else float("nan")
        gap_tight = int(sum(1 for g in gaps if g == g and g < relmax)) if gaps else 0
        rows.append({
            "name": nm, "n_cells": int(both.to_numpy().sum()),
            "value_rel_max": float(np.nanmax(rel.to_numpy())) if both.any().any() else None,
            "value_rel_mean": float(np.nanmean(rel.to_numpy())) if both.any().any() else None,
            "value_abs_max": float(np.nanmax(diff.to_numpy())),
            "boundary_gap_min_rms": float(np.nanmin(gaps)) if gaps else None,
            "n_dates_gap_below_value_noise": gap_tight,
            "mean_spearman": float(np.mean(rho)) if rho else None,
            "min_spearman": float(np.min(rho)) if rho else None,
            "n_dates": n_dates, "n_dates_headset_diff": swap_dates,
            "swap_sizes": sorted(swap_sizes)[-6:],
            "ident_max_err": float(np.nanmax(ident_err)) if ident_err else None,
            "DEAD_head_raw": float(det_R["head_excess"].mean()) if len(det_R) else None,
            "DEAD_head_adj": float(det_A["head_excess"].mean()) if len(det_A) else None,
            "DEAD_top6_raw": float(det_R["top6_excess"].mean()) if len(det_R) else None,
            "DEAD_top6_adj": float(det_A["top6_excess"].mean()) if len(det_A) else None,
            "abs_d_top6": abs(float(det_R["top6_excess"].mean())
                              - float(det_A["top6_excess"].mean()))
            if len(det_R) and len(det_A) else None,
            "abs_d_head": abs(float(det_R["head_excess"].mean())
                              - float(det_A["head_excess"].mean()))
            if len(det_R) and len(det_A) else None})
    out = {"family": sorted(fam), "horizon": hor, "elapsed_s": round(time.time() - t0, 1),
           "per_factor": rows, "fp_before": fp_before,
           "fp_after": {"bank": bank_fp(), "pool": pool_fp()},
           "reading": "值差按**逐月末截面 RMS** 归一（除以 |value| 会在趋零格上爆炸）；"
                      "ident_max_err 是【照抄 gate.evaluate 的取数：因子非空 ∩ 前向收益非空】"
                      "回算 head 超额的残差 ⇒ ≈0 说明两臂读数差异的全部来源是 top-k 选面这个"
                      "离散步骤，收益没有第二个通道；boundary_gap_min_rms = 头部第 k 名与第 k+1 名"
                      "的间距（RMS 单位），它小于值噪声的那些月末就是「边界近并列被翻动」的月份"}
    TRACK.mkdir(exist_ok=True)
    mech_file().write_text(json.dumps(_jsonable(out), ensure_ascii=False), encoding="utf-8",
                           newline="")
    print(json.dumps(_jsonable(out["per_factor"]), ensure_ascii=False, indent=1)[:2500])
    print("== 机制归因完成 %.0fs ⇒ %s" % (out["elapsed_s"], mech_file()))
    return 0


# ---------------------------------------------------------------- 合并与判决
def merge() -> int:
    arms = {}
    for w in ARMS:
        p = arm_file(w)
        if not p.exists():
            print(f"缺臂 {w} ⇒ 先跑 --arm {w}")
            return 2
        arms[w] = json.loads(p.read_text(encoding="utf-8"))

    def by_id(a):
        return {r["alpha_id"]: r for r in a["records"]}

    R, A = by_id(arms["raw"]), by_id(arms["adj"])

    g3 = {}
    # ④ 条数
    g3["④条数"] = {"raw": len(R), "adj": len(A), "expect": N_EXPECT,
                   "declared": {w: arms[w]["n_declared"] for w in ARMS},
                   "pass": len(R) == N_EXPECT and len(A) == N_EXPECT}
    # ③ 掩码与标签
    g3["③掩码与标签"] = {"raw": arms["raw"]["fingerprints"], "adj": arms["adj"]["fingerprints"]}
    g3["③掩码与标签"]["pass"] = (arms["raw"]["fingerprints"] == arms["adj"]["fingerprints"])
    # ② 抽样 close 比 == adj 比 —— 以 `--arm probe`（单进程、同一份 m）为准；
    #    没有探针文件就退回各臂自带的 sample（两臂各一进程），并如实标注来源。
    pf = probe_file()
    prb = json.loads(pf.read_text(encoding="utf-8")) if pf.exists() else None
    sR = (prb or {}).get("raw") or arms["raw"].get("sample") or {}
    sA = (prb or {}).get("adj") or arms["adj"].get("sample") or {}
    try:
        d0, d1 = sA["dates"]
        ratio_adj = sA["adj"][1] / sA["adj"][0]
        per_date = {}
        for j, d in enumerate((d0, d1)):
            per_date[d] = {"close_ADJ_over_RAW": sA["panel"]["close"][j] / sR["panel"]["close"][j],
                           "vwap_ADJ_over_RAW": sA["panel"]["vwap"][j] / sR["panel"]["vwap"][j],
                           "adj_factor": sA["adj"][j]}

        def rel(x, y):
            return abs(x / y - 1.0) if y else float("inf")

        def same_finite(xs, ys):
            """逐格**有限且相等**。不能用 `xs == ys`：json 的 NaN 是同一个对象，
            `[nan] == [nan]` 会走身份短路判 True ⇒ 空证据冒充通过。"""
            return bool(len(xs) == len(ys)) and all(
                a is not None and b is not None and a == a and b == b and a == b
                for a, b in zip(xs, ys))

        rc_adj = sA["panel"]["close"][1] / sA["panel"]["close"][0]
        rc_raw = sR["panel"]["close"][1] / sR["panel"]["close"][0]
        chk = {"code": sA["code"], "dates": sA["dates"],
               "source": ("probe 单进程（同一份 m，两臂各自的 build_panel）" if prb
                          else "per-arm 两进程各抽一次（退化，缺 --arm probe）"),
               "rel_step": sA.get("rel_step"),
               "probe_diag_等于_ADJ臂_diag":
                   None if not prb else (prb.get("adj_diag") == arms["adj"].get("adj_diag")),
               "same_sample_picked": bool(sR.get("code") == sA.get("code")
                                          and sR.get("dates") == sA.get("dates")),
               "raw_close_identical_across_arms":
                   same_finite(sR.get("raw_close") or [], sA.get("raw_close") or []),
               "total_mv_identical": same_finite(sA["panel"]["total_mv"],
                                                 sR["panel"]["total_mv"]),
               "per_date": per_date,
               "ratio_adj_factor": ratio_adj,
               "ratio_close_ADJ_over_RAW": rc_adj / rc_raw,
               "rel_err_pointwise_d0": rel(per_date[d0]["close_ADJ_over_RAW"], sA["adj"][0]),
               "rel_err_pointwise_d1": rel(per_date[d1]["close_ADJ_over_RAW"], sA["adj"][1]),
               "rel_err_vwap_d1": rel(per_date[d1]["vwap_ADJ_over_RAW"], sA["adj"][1]),
               "rel_err_ratio": rel(rc_adj / rc_raw, ratio_adj)}
        if prb:
            chk["probe_G4_零写入"] = prb["fp_before"] == prb["fp_after"]
        chk["pass"] = bool(chk["same_sample_picked"] and chk["raw_close_identical_across_arms"]
                           and chk["total_mv_identical"]
                           and (chk["probe_diag_等于_ADJ臂_diag"] is not False)
                           and float(chk["rel_step"] or 0) >= 1e-3
                           and max(chk["rel_err_pointwise_d0"], chk["rel_err_pointwise_d1"],
                                   chk["rel_err_vwap_d1"], chk["rel_err_ratio"]) < TOL_REL)
    except Exception as e:
        chk = {"error": "%s: %s" % (type(e).__name__, e), "pass": False,
               "raw_sample": sR, "adj_sample": sA}
    g3["②除权样本"] = chk
    # ① 尺度无关族（零次齐次）两臂必须一致
    fam = arms["raw"]["note"]["family"]
    diffs = []
    for nm in fam:
        if nm in R and nm in A:
            diffs.append({"name": nm,
                          "abs_d_head": abs(float(R[nm]["DEAD_head"]) - float(A[nm]["DEAD_head"])),
                          "abs_d_t": abs(float(R[nm]["DEAD_head_t"]) - float(A[nm]["DEAD_head_t"])),
                          "abs_d_active": abs(float(R[nm]["ACTIVE_head"])
                                              - float(A[nm]["ACTIVE_head"]))})
    g3["①尺度无关族"] = {
        "rule": "AST 求齐次次数 == 0（同日比值族）—— 正则版判错过一次，见脚本内注释",
        "n_family": len(fam), "n_compared": len(diffs), "tolerance": TOL_HEAD,
        "max_abs_d_head": max([d["abs_d_head"] for d in diffs], default=None),
        "max_abs_d_t": max([d["abs_d_t"] for d in diffs], default=None),
        "worst": sorted(diffs, key=lambda d: -d["abs_d_head"])[:8],
        "status": ("INCONCLUSIVE" if len(diffs) < 3 else
                   "pass" if all(d["abs_d_head"] <= TOL_HEAD and d["abs_d_t"] <= TOL_HEAD
                                 and d["abs_d_active"] <= TOL_HEAD for d in diffs) else "FAIL")}

    # G0（改正版，见预注册 §2 顶部的更正）：① 同臂二次运行必须逐位相同；
    #    ② 与登记簿的差异只作**观测项**报告 —— 那 158 行写在 P0-05 裁域(09-12) 与
    #    门禁 v2(09-14) 之前，按构造不可能被今天的任何一次跑复现。
    p2 = arm_file("raw2")
    det = {"second_run_present": p2.exists(), "identical": False,
           "n_diff": None, "diff_names": [], "n_compared": 0}
    if p2.exists():
        R2 = {r["alpha_id"]: r for r in json.loads(p2.read_text(encoding="utf-8"))["records"]}
        bad = []
        for k in sorted(set(R) & set(R2)):
            for fld in ("DEAD_head", "DEAD_head_t", "ACTIVE_head", "ACTIVE_head_t",
                        "DEAD_top6", "FULL_head", "p_adj", "avg_names", "PASS",
                        "cost_per_month", "head_bar"):
                if R[k].get(fld) != R2[k].get(fld):
                    bad.append({"alpha_id": k, "field": fld,
                                "run1": R[k].get(fld), "run2": R2[k].get(fld)})
        det.update({"identical": (not bad) and len(R) == len(R2) == N_EXPECT,
                    "n_diff": len(bad), "diff_names": bad[:20],
                    "n_compared": len(set(R) & set(R2))})
    conn = ro()
    try:
        bk = pd.read_sql("SELECT name, DEAD_head, ACTIVE_head, DEAD_head_t, PASS, n_tests, "
                         "avg_names FROM alpha_bank WHERE zoo='qlib158'", conn)
    finally:
        conn.close()
    g0 = None
    if len(bk):
        bk = bk.set_index("name")
        pairs = [(n, R[n], bk.loc[n]) for n in bk.index if n in R]
        sign_ok = sum(1 for _n, r, b in pairs
                      if np.sign(float(r["DEAD_head"])) == np.sign(float(b["DEAD_head"])))
        close_ok = sum(1 for _n, r, b in pairs
                       if abs(float(r["DEAD_head"]) - float(b["DEAD_head"])) <= 0.003)
        dhs = np.array([abs(float(r["DEAD_head"]) - float(b["DEAD_head"])) for _n, r, b in pairs])
        g0 = {"n_bank_rows": int(len(bk)), "n_compared": len(pairs),
              "sign_match_pct": round(100 * sign_ok / max(1, len(pairs)), 3),
              "within_0p3pp_pct": round(100 * close_ok / max(1, len(pairs)), 3),
              "median_abs_diff_pp": round(float(np.median(dhs)) * 100, 4) if len(dhs) else None,
              "max_abs_diff_pp": round(float(dhs.max()) * 100, 4) if len(dhs) else None,
              "bank_n_tests": sorted({int(x) for x in bk["n_tests"].dropna()}),
              "arm_n_tests": sorted({int(r["n_tests"]) for r in R.values() if "n_tests" in r}),
              "bank_pass_n": int(sum(1 for _n, _r, b in pairs if b["PASS"])),
              "as_observation_only": True,
              "determinism_同臂二次": det,
              "register_deviation_is_expected": (
                  "登记簿 158 行 created=2026-09-11 20:37，早于 P0-05 裁域(09-12) 与门禁 v2(09-14)"
                  "⇒ head/p 与今天不可比；差异照实报告，不算通过也不算失败")}

    # G1 主门 + G2 临界门
    common = sorted(set(R) & set(A))
    pass_R = {k for k in common if _truth(R[k].get("PASS"))}
    pass_A = {k for k in common if _truth(A[k].get("PASS"))}
    CRIT = ("c_head_size", "c_t", "c_same_sign", "c_top6")
    flip = {}
    for k in common:
        r, a = R[k], A[k]
        why = []
        if np.sign(_f(r.get("DEAD_head"))) != np.sign(_f(a.get("DEAD_head"))):
            why.append("DEAD_head 符号")
        if np.sign(_f(r.get("ACTIVE_head"))) != np.sign(_f(a.get("ACTIVE_head"))):
            why.append("ACTIVE_head 符号")
        if _abs_ge(r.get("DEAD_head_t")) != _abs_ge(a.get("DEAD_head_t")):
            why.append("|t| 跨 GATE_MIN_T")
        for c in CRIT:
            if bool(r.get(c)) != bool(a.get(c)):
                why.append(c)
        if why:
            flip[k] = {"why": why,
                       "raw": {c: r.get(c) for c in ("DEAD_head", "DEAD_head_t", "ACTIVE_head",
                                                     "DEAD_top6", "cost_per_month", "PASS")},
                       "adj": {c: a.get(c) for c in ("DEAD_head", "DEAD_head_t", "ACTIVE_head",
                                                     "DEAD_top6", "cost_per_month", "PASS")}}
    # ---- 观测项（**不改判据**）：噪声地板 ⇒ 16 条翻动里有多少"超过尺子无关的数值噪声" ----
    # 地板来自 G3-① 那 7 条**解析上完全不变**的因子：它们的真值差 = 0，所以实测到的
    # |Δ读数| 全部是 float32 重排 + 秩选面离散化造成的噪声 ⇒ 拿它当"多大才算真差异"的尺。
    mp = mech_file()
    mech = json.loads(mp.read_text(encoding="utf-8")) if mp.exists() else None
    pf_rows = (mech or {}).get("per_factor") or []
    floor_head = max([d["abs_d_head"] for d in pf_rows if d.get("abs_d_head") is not None],
                     default=None) or g3["①尺度无关族"]["max_abs_d_head"]
    _t6 = [d["abs_d_top6"] for d in pf_rows if d.get("abs_d_top6") is not None]
    floor_top6 = max(_t6) if _t6 else None
    for k, d in flip.items():
        dh = abs(_f(d["raw"]["DEAD_head"]) - _f(d["adj"]["DEAD_head"]))
        da = abs(_f(d["raw"]["ACTIVE_head"]) - _f(d["adj"]["ACTIVE_head"]))
        dt = abs(_f(d["raw"]["DEAD_top6"]) - _f(d["adj"]["DEAD_top6"]))
        d["abs_d_head"] = dh
        d["abs_d_active"] = da
        d["abs_d_top6"] = dt
        d["超过噪声地板"] = bool((floor_head and max(dh, da) > floor_head)
                             or (floor_top6 and dt > floor_top6))
    n_above = sum(1 for d in flip.values() if d["超过噪声地板"])
    gates = {
        "G0": g0,
        "G1_PASS集合进出": {"raw_pass": sorted(pass_R), "adj_pass": sorted(pass_A),
                        "added": sorted(pass_A - pass_R), "removed": sorted(pass_R - pass_A),
                        "crossed": bool((pass_A - pass_R) or (pass_R - pass_A))},
        "G2_临界翻动": {"n_flipped": len(flip), "threshold": 5,
                     "names": sorted(flip)[:40], "detail": flip,
                     "观测_噪声地板_head": floor_head, "观测_噪声地板_top6": floor_top6,
                     "观测_其中幅度超过地板": n_above, "观测_地板来源":
                         "G3-① 那 7 条解析上不变的因子（真值差=0 ⇒ 实测差全为数值噪声）",
                     "crossed": len(flip) >= 5},
        "G3_构造自证": g3,
        "G3①机制归因_诊断非判据": mech if mech else {"present": False,
                                        "说明": "缺 _mech.json ⇒ 未跑 --arm mech；G3-① 的 FAIL 就无解释"},
        "G4_零写入": {w: {"before": arms[w]["fp_before"], "after": arms[w]["fp_after"],
                          "unchanged": arms[w]["fp_before"] == arms[w]["fp_after"]} for w in ARMS},
        "方法偏离": {"check_duplicate 桩": "两臂对称施加（预注册 §3）；两臂 duplicates 计数 %s "
                              "⇒ 应为 0" % {w: arms[w].get("n_duplicates") for w in ARMS},
                     "save": False,
                     "adj_diag": arms["adj"].get("adj_diag")},
    }
    if not det["second_run_present"]:
        print("⚠️ 缺 RAW 臂第二遍（--arm raw2）⇒ G0① 未满足，本轮结果不得引用")
    g3_all_ok = all(_g3_ok(g3)) and bool(det["identical"])
    g4_all_ok = all(v["unchanged"] for v in gates["G4_零写入"].values())
    gate_hit = gates["G2_临界翻动"]["crossed"] or gates["G1_PASS集合进出"]["crossed"]
    if not g3_all_ok or not g4_all_ok:
        verdict = "不可引用（G3 构造自证或 G4 零写入有不过项 ⇒ 先修实现，不解释结果）"
    elif gate_hit:
        verdict = "统一尺值得做（G1 或 G2 触发）⇒ 出批次 5 提案，本轮不动 expr.py"
    else:
        verdict = "维持现状：两臂判决无实质差异 ⇒ 关闭为\"已知不同尺\"，登记尺声明"
    gates["summary"] = {"两臂共同因子数": len(common), "qlib158 声明条数": items_count(arms),
                        "G3_全过": g3_all_ok, "G4_零写入成立": g4_all_ok,
                        "G2_翻动数": len(flip), "判决": verdict}

    TRACK.mkdir(exist_ok=True)
    body = json.dumps(gates, ensure_ascii=False, indent=2, default=str)
    for d in (TRACK, LOGS):
        (d / "alpha_panel_scale_gate.json").write_text(body, encoding="utf-8", newline="")
    sha = hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]
    famx = g3["①尺度无关族"]
    md = ["# 批次 4：alpha 面板双尺门禁 A/B（RAW 裸价 vs ADJ 复权）", "",
          f"产物 sha256(前16) `{sha}`；判据 `plans/b1-q1-dual-scale-gate-ab-20260920.md`。", "",
          f"- 两臂共同因子 **{len(common)}** 条；RAW 耗时 {arms['raw']['elapsed_s']}s、"
          f"ADJ {arms['adj']['elapsed_s']}s；Bonferroni N = {(g0 or {}).get('arm_n_tests')}"
          f"（登记簿那 158 行当年 N = {(g0 or {}).get('bank_n_tests')}）",
          f"- **判决：{gates['summary']['判决']}**",
          f"- G0①（同臂二次逐位确定）：{det['identical']}（比 {det['n_compared']} 条 × 11 字段，"
          f"差异 {det['n_diff']} 处）；G0② 对登记簿只作观测：符号一致 "
          f"{(g0 or {}).get('sign_match_pct')}%，0.3pp 内 {(g0 or {}).get('within_0p3pp_pct')}%",
          f"- G1 PASS 集合进出：加 {gates['G1_PASS集合进出']['added']}／删 "
          f"{gates['G1_PASS集合进出']['removed']}",
          f"- G2 临界翻动：**{len(flip)} 条**（阈值 5）⇒ crossed="
          f"{gates['G2_临界翻动']['crossed']}；观测：幅度超过噪声地板"
          f"（head {floor_head}／top6 {floor_top6}）的有 **{n_above}** 条",
          f"- G3 构造自证：①{famx['status']}（族 {famx['n_family']} 条，"
          f"max|Δhead|={famx['max_abs_d_head']}）②{g3['②除权样本'].get('pass')}"
          f"〔{g3['②除权样本'].get('source', '')}｜样本 "
          f"{g3['②除权样本'].get('code')}/{g3['②除权样本'].get('dates')}｜真步长 "
          f"{g3['②除权样本'].get('rel_step')}〕"
          f"③{g3['③掩码与标签']['pass']} ④{g3['④条数']['pass']}",
          f"- G4 零写入：登记簿 {gates['G4_零写入']['raw']['before']['bank']['rows']} 行、"
          f"值池 {gates['G4_零写入']['raw']['before']['pool']['files']} 文件未变 = {g4_all_ok}",
          "", "## 翻动明细", "",
          "| 因子 | 翻动项 | RAW head/t | ADJ head/t | \\|Δhead\\| / \\|Δtop6\\| | 超过地板 |",
          "|---|---|---|---|---|---|"]
    for k in sorted(flip)[:40]:
        d = flip[k]
        md.append(f"| `{k}` | {'、'.join(d['why'])} | {d['raw']['DEAD_head']!r} / "
                  f"{d['raw']['DEAD_head_t']!r} | {d['adj']['DEAD_head']!r} / "
                  f"{d['adj']['DEAD_head_t']!r} | {d['abs_d_head']:.3g} / "
                  f"{d['abs_d_top6']:.3g} | {d['超过噪声地板']} |")
    if mech:
        md += ["", "## G3-① 机制归因（诊断，不是判据）", "",
               mech["reading"], "", f"耗时 {mech['elapsed_s']}s；G4 在机制臂里同样未变 = "
               f"{mech['fp_before'] == mech['fp_after']}", "",
               "| 因子 | 值差/RMS max·mean | 秩相关 mean·min | 月末 | 选面有变 | 边界间距min(RMS) "
               "| 间距<噪声的月 | \\|Δhead\\| | \\|Δtop6\\| | 恒等式残差 |",
               "|---|---|---|---|---|---|---|---|---|---|"]
        for d in mech["per_factor"]:
            md.append(f"| `{d['name']}` | {_fmt(d['value_rel_max'])} · {_fmt(d['value_rel_mean'])} "
                      f"| {d['mean_spearman']:.6f} · {d['min_spearman']:.6f} | {d['n_dates']} "
                      f"| {d['n_dates_headset_diff']}（幅度 {d['swap_sizes']}） "
                      f"| {_fmt(d['boundary_gap_min_rms'])} "
                      f"| {d['n_dates_gap_below_value_noise']} "
                      f"| {_fmt(d['abs_d_head'])} | {_fmt(d['abs_d_top6'])} "
                      f"| {_fmt(d['ident_max_err'])} |")
    text = "\n".join(md) + "\n"
    for d in (TRACK, LOGS):
        (d / "alpha_panel_scale_gate.md").write_text(text, encoding="utf-8", newline="")
    print(text[:4500])
    print(f"\n== 产物: {TRACK / 'alpha_panel_scale_gate.json'}  sha={sha}")
    return 0


def items_count(arms) -> int:
    return max(a["n_declared"] for a in arms.values())


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return np.nan


def _fmt(x, prec=3):
    """报告用格式化：None/NaN 打成 `-`，不让一个缺读数的格子把整份产物崩掉。"""
    v = _f(x)
    return "-" if not np.isfinite(v) else "%.*g" % (prec, v)


def _truth(x):
    return bool(x) if x is not None else False


def _abs_ge(x):
    from app.alpha import gate as G
    v = _f(x)
    return bool(np.isfinite(v) and abs(v) >= G.GATE_MIN_T)


def _g3_ok(g3):
    yield g3["④条数"]["pass"]
    yield g3["③掩码与标签"]["pass"]
    yield bool(g3["②除权样本"].get("pass"))
    yield g3["①尺度无关族"]["status"] == "pass"      # INCONCLUSIVE 不算通过


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--arm", required=True,
                    choices=ARMS + ("raw2", "probe", "mech", "merge"))
    a = ap.parse_args()
    if a.arm == "merge":
        sys.exit(merge())
    if a.arm == "probe":
        sys.exit(run_probe())
    if a.arm == "mech":
        sys.exit(run_mech())
    sys.exit(run_arm(a.arm))
