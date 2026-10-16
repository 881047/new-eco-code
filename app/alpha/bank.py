"""Alpha Bank —— 因子资产登记处(2026-09-11)

对应用户框架图的 `Alpha Bank`。**核心区别: 因子是资产, 不是一次性实验。**
本项目此前把因子当实验跑完就散(`factor` 表里 31 个 + 若干 `model_score_die_*` 表),
没有一个地方能回答「我手里有哪些 alpha、它们各自什么状态、什么时候该退役」。

## 状态机(生命周期)

    CANDIDATE ──门禁通过──> POOLED ──组合层两轴通过──> VALIDATED ──> (生产)
        │                      │                            │
        └──门禁不过──> RETIRED  └──衰减/拥挤──> RETIRED <────┘

  · `CANDIDATE` 候选: 已算出读数, 未过门禁
  · `POOLED`    入池: 过了预注册门禁(`gate.py` 五条), **有资格**进组合层回测。
                ⚠️ **入池 ≠ 可用** —— 门禁是入场券(噪声筛), 判决只能是组合层两轴
  · `VALIDATED` 已验: 组合层两轴(系统年化 + 系统回撤)都不劣于同窗基线
  · `RETIRED`   退役: **只进不出** —— 退役可回滚(状态能改回), 但**退役的原因必须留痕**。
                ⚠️ 退役是**防御性**动作。**绝不允许「按近期表现加权/复活」**(见下)

## 两条硬规则(从已有证据里学来的, 写进代码而不是写在文档里)

1. **衰减是「退役信号」, 不是「加权信号」。**
   `die_flow` 在 2023 打出 +6.93%/月 ⇒ R1 电源门控一路给它供电 ⇒ 正好供电进 2024-26
   它最差的那段。**按滚动表现路由 = 追涨杀跌**。故本模块只提供 `retire()`,
   **不提供也不实现任何"按近期表现调权重"的接口** —— 那件事已被实验证伪
   (`scripts/oneoff/exp_fusion_router.py` 的 R1)。

2. **`alpha_id` 一经登记不得改名。** 它是表达式、读数、退役理由的锚点; 改名等于
   把一条证据变成没有主语的话。

## 存储

- 读数(统计量) → SQLite `alpha_bank` 表(可查询、可 join)
- 值面板(月末截面) → `data/alpha_pool/<alpha_id>.npz`(float32, 可复算、可重评)

分工的理由: 读数要能 SQL 查询与批量比较, 值面板要能快速整体加载重算 ——
塞进同一个介质会让两边都别扭。
"""
from __future__ import annotations

import datetime
import json

import numpy as np
import pandas as pd

from app import db
from app.config import DATA_DIR

POOL_DIR = DATA_DIR / "alpha_pool"

#: 撞车硬闸(门禁 v2 ③, 判据见 KB 14 号 三补十四)
DUP_CORR = 0.999        # 值面板逐截面秩相关 ≥ 此值 ⇒ 判为"同一个因子"
DUP_YEARLY_TOL = 1e-9   # 值级复算前的**预筛**容差(逐年头部读数几乎相同才算嫌疑)

# 读数列(SQLite 列名, 与 gate.summarize 的键一一对应)
_NUM_COLS = [
    "DEAD_head", "DEAD_head_t", "DEAD_hit", "DEAD_n", "DEAD_top6", "DEAD_ic", "DEAD_ls",
    "ACTIVE_head", "ACTIVE_head_t", "ACTIVE_hit", "ACTIVE_n", "ACTIVE_top6",
    "ACTIVE_ic", "ACTIVE_ls",
    "FULL_head", "FULL_head_t", "FULL_n", "FULL_top6",
    "p_adj", "n_tests", "avg_names",
]
_TEXT_COLS = ["name", "zoo", "source", "expr", "info_source", "pit_version",
              "status", "note", "retire_reason", "yearly", "gate_json"]
_BOOL_COLS = ["PASS", "c_same_sign", "c_head_size", "c_t", "c_p_adj", "c_top6"]

_DDL = """
CREATE TABLE IF NOT EXISTS alpha_bank (
    alpha_id TEXT PRIMARY KEY,
    name TEXT, zoo TEXT, source TEXT, expr TEXT, info_source TEXT, pit_version TEXT,
    status TEXT, note TEXT, retire_reason TEXT,
    yearly TEXT, gate_json TEXT,
    {nums},
    {bools},
    created TEXT, updated TEXT
)
"""


def ensure_table() -> None:
    conn = db.get_conn()
    try:
        conn.execute(_DDL.format(
            nums=", ".join(f"{c} REAL" for c in _NUM_COLS),
            bools=", ".join(f"{c} INTEGER" for c in _BOOL_COLS)))
        conn.commit()
    finally:
        conn.close()


def _now() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# 读数登记
# ---------------------------------------------------------------------------
def _as_json_text(v) -> str:
    """已经是 JSON 字符串的原样返回。

    2026-09-12 审查 P1-09: `get()` 读出来的 `yearly` 是 TEXT, 直接喂回 `upsert` 会被
    再 dumps 一遍 —— 存进去变成 `"{\\"2020\\": ...}"`(双重编码), 证据表的归因读数就烂了。
    `set_note` 的 docstring 早就点过这个坑, 但流水线走的正是 `upsert(get(...))` 这条路。
    入口兜住比逐个调用方改稳妥: 字符串=已编码, 其余照旧 dumps。
    """
    return v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=float)


def upsert(rec: dict, status: str = None, note: str = None) -> str:
    """登记/更新一条 alpha 读数。status 缺省按门禁结果自动定(CANDIDATE / POOLED)。

    入参既可以是门禁刚算出来的 rec, 也可以是 `get()` 读回的整行(流水线就是这么用的)
    —— 两者字段口径不同(见 `_as_json_text` 与下面 `created` 的处理)。

    ⚠️ **整行替换语义, 不是局部更新**：`rec` 里没给的列会被写成 NULL。
    只想改一列(状态/备注)请走 `set_note` 或单列 UPDATE, **别拿半截 dict 调这里**。
    (2026-09-18 审查 Q-05: `factory._refuse_duplicate` 就这么把 10 行在册读数抹成空壳;
     2026-09-12 审查 P1-09 是同一模式的第一个盲点 —— 它把 `created` 刷成了 now())
    """
    ensure_table()
    aid = rec["alpha_id"]
    st = status or ("POOLED" if rec.get("PASS") else "CANDIDATE")
    row = {c: rec.get(c) for c in _NUM_COLS}
    row.update({c: rec.get(c) for c in _TEXT_COLS})
    row.update({c: int(bool(rec.get(c))) for c in _BOOL_COLS})
    row["yearly"] = _as_json_text(rec.get("yearly"))
    # 门禁 v2(2026-09-14)新增的字段一律进 gate_json, **不动 DDL** ——
    # `ensure_table` 用的是 `CREATE TABLE IF NOT EXISTS`, 往 _DDL 里加列对**已存在的表**
    # 不会生效(会静默少列), 而这里没有迁移步骤。放进 JSON 反而可读、可复算。
    row["gate_json"] = json.dumps(
        {k: rec.get(k) for k in ("c_same_sign", "c_head_size", "c_t", "c_p_adj",
                                 "c_top6", "c_cost", "PASS",
                                 "head_bar", "cost_per_month",
                                 "n_tests_family", "n_tests_prior")}, ensure_ascii=False)
    row["status"] = st
    row["note"] = note if note is not None else rec.get("note")
    row["alpha_id"] = aid
    row["created"] = _now()
    row["updated"] = _now()

    cols = ["alpha_id"] + _NUM_COLS + _TEXT_COLS + _BOOL_COLS + ["created", "updated"]
    cols = [c for c in cols if c in row]
    ph = ",".join("?" * len(cols))
    # created 只在首次 INSERT 时落值; 冲突更新不得覆盖它
    # (2026-09-12 审查 P1-09: 原 DO UPDATE 把 created 也刷成 now(), 每次状态回写
    #  都把「首次登记时间」抹掉 —— 归因时看不出这条 alpha 是什么时候入册的)
    sets = ",".join(f"{c}=excluded.{c}" for c in cols if c not in ("alpha_id", "created"))
    conn = db.get_conn()
    try:
        conn.execute(f"INSERT INTO alpha_bank ({','.join(cols)}) VALUES ({ph}) "
                     f"ON CONFLICT(alpha_id) DO UPDATE SET {sets}",
                     [row[c] for c in cols])
        conn.commit()
    finally:
        conn.close()
    return aid


def get(alpha_id: str) -> dict:
    ensure_table()
    conn = db.get_conn()
    try:
        cur = conn.execute("SELECT * FROM alpha_bank WHERE alpha_id=?", (alpha_id,))
        cols = [d[0] for d in cur.description]
        r = cur.fetchone()
    finally:
        conn.close()
    return dict(zip(cols, r)) if r else {}


def list_alphas(status: str = None, limit: int = None) -> pd.DataFrame:
    ensure_table()
    conn = db.get_conn()
    try:
        q = "SELECT * FROM alpha_bank"
        p = []
        if status:
            q += " WHERE status=?"
            p.append(status)
        q += " ORDER BY (DEAD_head_t IS NULL), DEAD_head_t DESC"
        if limit:
            q += f" LIMIT {int(limit)}"
        df = pd.read_sql(q, conn, params=p or None)
    finally:
        conn.close()
    return df


# ---------------------------------------------------------------------------
# 门禁 v2 ②: 累计试验数(多重比较的真实分母)
# ---------------------------------------------------------------------------
def cumulative_tests() -> int:
    """**跑本批之前**登记簿里的候选数 = 历史累计试验数（门禁 v2 ②）。

    为什么要它：Bonferroni 分母原来只看**本批**的预注册规模，于是每一批都像一个"全新的
    研究程序"。而多重比较的正确分母是**整体研究程序试过多少次** —— 本项目自己测过，
    卡点正是 N 而不是样本长度（F48：MinTRL 只需 129~136 期，N=7/25 时 DSR=0.876/0.597）。

    `status='DUPLICATE'` 的行**不计**：那是撞车硬闸的留痕，它被拒绝在求值之前，
    根本不是一次检验（把它算进去会凭空抬高 N，与"数真做过的检验"口径相反）。

    ⚠️ 这是**下界**（只数真登记了读数的候选）；本批规模由 `Factory.declare()` 另行冻结，
    两者在 `factory.run` 里合成 `N_used = max(B, P + B)`。
    """
    ensure_table()
    conn = db.get_conn()
    try:
        return int(conn.execute(
            "SELECT COUNT(*) FROM alpha_bank WHERE COALESCE(status,'') <> 'DUPLICATE'"
        ).fetchone()[0])
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 门禁 v2 ③: 撞车硬闸(撞车 ⇒ 拒跑 + 留痕, 不登记读数)
# ---------------------------------------------------------------------------
def _norm_expr(e) -> str:
    """公式规范化：去空白 + 转小写 + 全角括号/逗号转半角。

    **故意不做**代数化简（`($c-$o)/$o` 与 `$c/$o-1` 化归成同一个式子需要符号计算，
    判断错一次就会把两个不同的因子当成同一个）—— 那种情况交给值级检查（`DUP_CORR`）。
    """
    s = "" if e is None else str(e)
    for a, b in (("（", "("), ("）", ")"), ("，", ","), ("　", " ")):
        s = s.replace(a, b)
    return "".join(s.lower().split())


def _panel_rank_corr(a: pd.DataFrame, b: pd.DataFrame, md=None) -> float:
    """两个值面板的**逐截面** Spearman 均值（同 `expert.rank_corr_with` 口径）。"""
    from app.alpha import gate as gate_mod
    if md is None:
        md = a.index.intersection(b.index)
    return _rank_corr_impl(a, b, md, gate_mod.MIN_NAMES)


def _rank_corr_impl(a: pd.DataFrame, b: pd.DataFrame, md, min_names: int) -> float:
    cs = []
    for d in md:
        if d not in a.index or d not in b.index:
            continue
        x, y = a.loc[d], b.loc[d]
        m = x.notna() & y.notna()
        if int(m.sum()) < min_names:
            continue
        xv, yv = x[m].rank().to_numpy(float), y[m].rank().to_numpy(float)
        if xv.std() == 0 or yv.std() == 0:
            continue
        cs.append(float(np.corrcoef(xv, yv)[0, 1]))
    return float(np.mean(cs)) if cs else float("nan")


def _yearly_head_close(rec: dict, row: dict, tol: float) -> bool:
    """值级复算前的**预筛**：逐年头部读数是否几乎逐位相同（相同公式必相同）。"""
    a = rec.get("yearly")
    b = row.get("yearly")
    if isinstance(a, str):
        try:
            a = json.loads(a)
        except Exception:
            return False
    if isinstance(b, str):
        try:
            b = json.loads(b)
        except Exception:
            return False
    if not isinstance(a, dict) or not isinstance(b, dict) or not a or not b:
        return False
    common = set(a) & set(b)
    if not common:
        return False
    for k in common:
        ha, hb = (a[k] or {}).get("head"), (b[k] or {}).get("head")
        if ha is None or hb is None:
            continue
        if abs(float(ha) - float(hb)) > tol:
            return False
    return True


def check_duplicate(rec: dict, values: pd.DataFrame = None, md=None,
                    allow_rerun: bool = False, corr_threshold: float = DUP_CORR) -> dict:
    """撞车硬闸：**跑之前先查登记簿**（门禁 v2 ③，判据见 KB 14 号 三补十四）。

    返回 `{"dup": bool, "kind": str, "existing": str|None, "detail": str}`；`kind`：

    | kind | 含义 |
    |---|---|
    | `id_conflict` | 同 `alpha_id` 已登记且**公式不同** ⇒ 一个 id 只能对应一个公式 |
    | `rerun` | 同 id + 同公式已登记 ⇒ 默认拒跑（复现性核验请显式 `allow_rerun=True`） |
    | `expr` | 公式（规范化后）与在册任何一条相同 ⇒ 换名字重测 |
    | `name` | 同 `name` 而 `zoo`/`source` 不同 ⇒ 疑似同一假说换皮 |
    | `values` | 值面板与在册候选逐截面秩相关 ≥ `corr_threshold` ⇒ 代数等价的不同写法 |

    前三类只看元数据（便宜，可在求值**前**调用）；`values` 类需要值面板（求值后调用），
    并且先用 `yearly` 指纹预筛、只对嫌疑行真算秩相关（171 条在册面板不可能逐个 load）。

    ⚠️ 设计取舍：撞车**不计入**本批分母（它根本没被当成一次新检验），也**不改**在册那一行。
    """
    aid = rec.get("alpha_id")
    if not aid:
        return {"dup": False, "kind": "", "existing": None, "detail": "无 alpha_id, 跳过撞车检查"}
    ensure_table()
    conn = db.get_conn()
    try:
        cur = conn.execute("SELECT * FROM alpha_bank")
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        conn.close()

    expr = _norm_expr(rec.get("expr"))
    name = str(rec.get("name") or aid).strip()
    zoo = str(rec.get("zoo") or rec.get("source") or "").strip()
    for r in rows:
        rid = r.get("alpha_id")
        if rid == aid:
            if _norm_expr(r.get("expr")) != expr:
                return {"dup": True, "kind": "id_conflict", "existing": rid,
                        "detail": "同 id 已登记但公式不同：%r vs %r"
                                  % (r.get("expr"), rec.get("expr"))}
            if not allow_rerun:
                return {"dup": True, "kind": "rerun", "existing": rid,
                        "detail": "同 id 同公式已在册；重测不会带来新信息，"
                                  "复现性核验请显式 allow_rerun=True（会留日志）"}
            continue
        if expr and _norm_expr(r.get("expr")) == expr:
            return {"dup": True, "kind": "expr", "existing": rid,
                    "detail": "公式与在册 %s 规范化后相同：%s" % (rid, rec.get("expr"))}
        rname = str(r.get("name") or rid or "").strip()
        rzoo = str(r.get("zoo") or r.get("source") or "").strip()
        if name and rname == name and zoo and rzoo and zoo != rzoo:
            return {"dup": True, "kind": "name", "existing": rid,
                    "detail": "同名 %r 但来源不同（%s vs %s）：疑似同一假说换皮，"
                              "请改名或人工确认" % (name, zoo, rzoo)}

    if values is not None and corr_threshold is not None:
        suspects = [r["alpha_id"] for r in rows
                    if r.get("alpha_id") != aid and _yearly_head_close(rec, r, DUP_YEARLY_TOL)]
        for sid in suspects:
            if not has_values(sid):
                continue
            try:
                other = load_values(sid)
            except Exception:
                continue
            rho = _panel_rank_corr(values, other, md)
            if rho == rho and abs(rho) >= corr_threshold:
                return {"dup": True, "kind": "values", "existing": sid,
                        "detail": "与在册 %s 的值面板秩相关 %.4f ≥ %.3f：不是新因子"
                                  % (sid, rho, corr_threshold)}
    return {"dup": False, "kind": "", "existing": None, "detail": "未撞车"}


def set_note(alpha_id: str, note: str) -> None:
    """只改 `note`(Critic 读数回写用)。

    **不要用 `upsert(get(aid), note=...)` 做这件事** —— `get` 出来的 `yearly` 已经是
    JSON **字符串**, 再走一遍 `upsert` 会被 `json.dumps` 二次编码成 `"{\\"2020\\": ...}"`,
    读数就烂在库里了。只改一列是最小的破坏面。
    """
    ensure_table()
    conn = db.get_conn()
    try:
        conn.execute("UPDATE alpha_bank SET note=?, updated=? WHERE alpha_id=?",
                     (note, _now(), alpha_id))
        if conn.total_changes == 0:
            raise KeyError(f"alpha_bank 里没有 {alpha_id}")
        conn.commit()
    finally:
        conn.close()


def query_top6_negative() -> pd.DataFrame:
    """死区 top6 为负但门禁其余四条通过 —— **门禁盲点的直接清单**(必须为空)"""
    ensure_table()
    conn = db.get_conn()
    try:
        return pd.read_sql(
            "SELECT alpha_id, name, DEAD_head, DEAD_head_t, DEAD_top6, status "
            "FROM alpha_bank WHERE c_top6=0 AND c_same_sign=1 AND c_head_size=1 "
            "AND c_t=1 AND c_p_adj=1", conn)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 退役(只此一条方向, 没有"复活/加权")
# ---------------------------------------------------------------------------
def retire(alpha_id: str, reason: str) -> None:
    """退役。`reason` 必填 —— 没有理由的退役与删除无异。"""
    if not reason or not str(reason).strip():
        raise ValueError("退役必须写明 reason(退役理由是证据的一部分)")
    ensure_table()
    conn = db.get_conn()
    try:
        conn.execute("UPDATE alpha_bank SET status='RETIRED', retire_reason=?, updated=? "
                     "WHERE alpha_id=?", (reason, _now(), alpha_id))
        if conn.total_changes == 0:
            raise KeyError(f"alpha_bank 里没有 {alpha_id}")
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 值面板
# ---------------------------------------------------------------------------
def _path(alpha_id: str):
    return POOL_DIR / f"{alpha_id}.npz"


def save_values(alpha_id: str, wide: pd.DataFrame) -> str:
    """存月末截面值面板(index=日期, columns=ts_code, float32)。"""
    POOL_DIR.mkdir(parents=True, exist_ok=True)
    p = _path(alpha_id)
    np.savez(p,
             dates=np.array([d.strftime("%Y%m%d") for d in wide.index]),
             codes=np.array([str(c) for c in wide.columns]),
             values=wide.to_numpy(dtype="float32"))
    return str(p)


def load_values(alpha_id: str) -> pd.DataFrame:
    p = _path(alpha_id)
    if not p.exists():
        raise FileNotFoundError(f"值面板不存在: {p}(先跑工厂算出它)")
    z = np.load(p, allow_pickle=False)
    return pd.DataFrame(z["values"],
                        index=pd.to_datetime([str(x) for x in z["dates"]]),
                        columns=[str(x) for x in z["codes"]])


def has_values(alpha_id: str) -> bool:
    return _path(alpha_id).exists()


# ---------------------------------------------------------------------------
# 晋升到生产因子表(只有 VALIDATED 才允许)
# ---------------------------------------------------------------------------
def promote_to_factor(alpha_id: str, month_dates=None, dry_run: bool = True) -> dict:
    """把已验 alpha 的值写进 `factor` 表(模型读的就是这张表)。

    ⚠️ 守卫: 只允许 `status='VALIDATED'` 的 alpha 晋升 —— 门禁通过(POOLED)也不够,
    必须过了组合层两轴。这条守卫的存在理由: `factor` 表是**生产模型的口粮**,
    往里写一个只过了噪声筛的因子, 等于把实验泄漏进生产。
    """
    rec = get(alpha_id)
    if not rec:
        raise KeyError(f"alpha_bank 里没有 {alpha_id}")
    if rec["status"] != "VALIDATED":
        raise PermissionError(
            f"{alpha_id} 状态是 {rec['status']}, 不是 VALIDATED —— 拒绝晋升。"
            f"(门禁通过只是 POOLED; 晋升要求组合层两轴也过)")
    wide = load_values(alpha_id)
    if month_dates is not None:
        wide = wide.loc[wide.index.isin(pd.to_datetime(month_dates))]
    long = (wide.stack().rename("value").reset_index()
            .rename(columns={"level_0": "trade_date", "level_1": "ts_code"}))
    long["trade_date"] = pd.to_datetime(long["trade_date"]).dt.strftime("%Y%m%d")
    long["factor"] = rec["name"] or alpha_id
    out = {"alpha_id": alpha_id, "factor": rec["name"] or alpha_id,
           "rows": int(len(long)), "months": int(wide.shape[0]), "dry_run": dry_run}
    if dry_run:
        return out
    conn = db.get_conn()
    try:
        conn.execute("DELETE FROM factor WHERE factor=?", (long["factor"].iloc[0],))
        long[["ts_code", "trade_date", "factor", "value"]].to_sql(
            "factor", conn, if_exists="append", index=False)
        conn.commit()
    finally:
        conn.close()
    return out


__all__ = ["ensure_table", "upsert", "get", "list_alphas", "retire", "set_note",
           "query_top6_negative", "save_values", "load_values", "has_values",
           "promote_to_factor", "POOL_DIR"]
