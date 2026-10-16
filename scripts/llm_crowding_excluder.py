"""LLM 拥挤度剔除器 · 第三轮（预注册 plans/llm-crowding-excluder-round3-20260917.md）

阶段 1（sample）: 采样死区+评估窗内月末锚点的候选池，为每个候选股组装共振子图摘要。
阶段 2（score）:  DeepSeek 批量打拥挤分 1~10（引用边校验，不实计零）。
阶段 3（backtest θ）: 按阈值剔除候选 → 组合层回测 → 两轴读数。

用法:
  python scripts/llm_crowding_excluder.py sample     # 阶段 1
  python scripts/llm_crowding_excluder.py score      # 阶段 2（需 LLM_API_KEY）
  python scripts/llm_crowding_excluder.py backtest 7 # 阶段 3（θ=7 单臂）
"""
import json
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import pandas as pd  # noqa: E402

from app import db  # noqa: E402
from app.logger import setup_logger  # noqa: E402

setup_logger()

KG_DIR = BASE / "data" / "kg"
SAMPLES = KG_DIR / "excluder_samples.jsonl"
SCORES = KG_DIR / "excluder_scores.jsonl"
DEAD = ("2020-09-01", "2023-12-31")


def _plan_candidates_per_anchor() -> dict:
    """每个锚点日的候选池（模拟生产 top100/带宽8× 的入选股）。

    用 model_score 的月末截面按打分取 top100（生产构型的候选语义近似——
    精确的生产候选要走完整方案管线，研究侧用打分 top100 是冻结的近似口径）。
    """
    conn = db.get_conn()
    try:
        df = pd.read_sql("""
            SELECT trade_date, ts_code, score FROM model_score
            WHERE trade_date >= '20200901'
            ORDER BY trade_date, score DESC""", conn)
    finally:
        conn.close()
    out = {}
    for d, g in df.groupby("trade_date"):
        out[d] = list(g["ts_code"].head(100))
    return out


def stage_sample() -> int:
    t0 = time.time()
    conn = db.get_conn()
    try:
        conn.execute("PRAGMA query_only=1")
        anchors = {k: v for k, v in _plan_candidates_per_anchor().items()
                   if DEAD[0] <= k <= DEAD[1]}
        print(f"死区锚点 {len(anchors)} 个, 候选 {sum(len(v) for v in anchors.values()):,} 股次")
        ev = pd.read_sql("SELECT edge_type, ts_code, date8, entity, direction, strength, quote "
                         "FROM kg_edge_event", conn)
        ev["dt"] = pd.to_datetime(ev["date8"], format="%Y%m%d", errors="coerce")
    finally:
        conn.close()
    SAMPLES.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with SAMPLES.open("w", encoding="utf-8") as f:
        for anchor, codes in sorted(anchors.items()):
            at = pd.Timestamp(anchor)
            lo = at - pd.Timedelta(days=60)
            w = ev[(ev["dt"] > lo) & (ev["dt"] <= at)]
            for code in codes:
                sub = w[w["ts_code"] == code]
                if not len(sub):
                    continue
                by_type = {t: g for t, g in sub.groupby("edge_type")}

                def _col(t, col):
                    g = by_type.get(t)
                    if g is None or len(g) == 0 or col not in g.columns:
                        return pd.Series(dtype=object)
                    return g[col].dropna()
                sample = {
                    "anchor": anchor, "ts_code": code,
                    "broker": sorted(_col("broker_rec", "entity").unique().tolist())[:8],
                    "rating_up": int(len(by_type.get("rating_up", pd.DataFrame()))),
                    "rating_cov": int(len(by_type.get("rating_neu", pd.DataFrame()))),
                    "holder_add": int(len(by_type.get("holder_add", pd.DataFrame()))),
                    "holder_dec": int(len(by_type.get("holder_dec", pd.DataFrame()))),
                    "catalyst": sorted(_col("catalyst", "entity").unique().tolist())[:6],
                    "sent_mean": round(float((_col("sentiment", "direction")
                                              * _col("sentiment", "strength")).mean() or 0), 2),
                    "titles": _col("catalyst", "quote").head(3).tolist()
                              or _col("rating_neu", "quote").head(2).tolist(),
                }
                f.write(json.dumps(sample, ensure_ascii=False) + "\n")
                n += 1
    print(f"✓ 采样 {n:,} 股次 → {SAMPLES} ({time.time()-t0:.0f}s)")
    return 0


def stage_score() -> int:
    from app.web.llm import _llm_cfg, ASK_TIMEOUT  # noqa: E402
    import requests  # noqa: E402
    c = _llm_cfg()
    SYSTEM = """你是 A 股拥挤度分析师。输入是一只股票近 60 天的事件子图摘要
（券商金股推荐/评级覆盖/股东增减持/研报催化剂/标题情绪）。
输出 JSON: {"crowd": 1~10 的拥挤分, "refs": ["你引用的关键证据, 如 'XX券商 202608 金股'"]}
拥挤分维度: 券商推荐密度(多家共同推荐=拥挤)、评级一致性(全是买入=拥挤)、
事件多样性(多类型事件共振=拥挤)、标题情绪热度(集体唱多=拥挤)。
refs 必须逐字引用输入里的券商名/事件描述——引用不到的打分无效。只输出 JSON。"""
    samples = [json.loads(l) for l in SAMPLES.open(encoding="utf-8")]
    done = set()
    if SCORES.exists():
        for l in SCORES.open(encoding="utf-8"):
            try:
                r = json.loads(l)
                done.add((r["anchor"], r["ts_code"]))
            except Exception:
                pass
    todo = [s for s in samples if (s["anchor"], s["ts_code"]) not in done]
    print(f"待打分 {len(todo):,}/{len(samples):,}（断点 {len(done):,} 已完成）")
    jl = SCORES.open("a", encoding="utf-8")
    n_ok = n_zero = 0
    t0 = time.time()
    for i in range(0, len(todo), 20):
        chunk = todo[i:i + 20]
        for s in chunk:
            brief = (f"股票 {s['ts_code']} 近 60 天: 券商金股推荐={s['broker'] or '无'}; "
                     f"评级上调 {s['rating_up']} 次, 覆盖 {s['rating_cov']} 次; "
                     f"股东增持 {s['holder_add']} / 减持 {s['holder_dec']}; "
                     f"催化剂类型={s['catalyst'] or '无'}; 标题情绪均值={s['sent_mean']}; "
                     f"原文标题: {' | '.join(s['titles'][:2]) or '无'}")
            try:
                r = requests.post(
                    c["base_url"].rstrip("/") + "/beta" + "/chat/completions",
                    headers={"Authorization": f"Bearer {c['api_key']}"},
                    json={"model": c["model"],
                          "messages": [{"role": "system", "content": SYSTEM},
                                       {"role": "user", "content": brief},
                                       {"role": "assistant", "content": "{\"crowd\":", "prefix": True}],
                          "max_tokens": 3000, "temperature": 0.1},
                    timeout=ASK_TIMEOUT)
                text = '{"crowd":' + r.json()["choices"][0]["message"]["content"].strip()
                sem = json.loads(text.split("}")[0] + "}")
                crowd = int(sem.get("crowd", 0))
                refs = sem.get("refs", [])
                # 引用锚定校验: refs 里的词必须真的出现在摘要里（不实 ⇒ 计 0）
                brief_l = brief
                valid_refs = [x for x in refs if any(w and w in brief_l for w in
                                                     [str(x)[:12], str(x)[:8]])]
                if crowd > 0 and not valid_refs:
                    crowd = 0          # 幻觉锚定: 引用不到证据的打分计零
                    n_zero += 1
                jl.write(json.dumps({"anchor": s["anchor"], "ts_code": s["ts_code"],
                                     "crowd": crowd, "refs": valid_refs,
                                     "hallucination_zeroed": bool(refs) and not valid_refs},
                                    ensure_ascii=False) + "\n")
                n_ok += 1
            except Exception as e:
                print(f"  {s['ts_code']} 失败: {str(e)[:80]}")
        jl.flush()
        done_n = len(done) + n_ok
        rate = n_ok / max(1e-9, time.time() - t0) * 60
        print(f"进度 {done_n:,}/{len(samples):,} | {rate:.0f} 股次/分 | "
              f"幻觉计零 {n_zero}")
    jl.close()
    print(f"✓ 打分完成: 成功 {n_ok}, 幻觉计零 {n_zero}")
    return 0


def stage_backtest(theta: int) -> int:
    """θ 臂回测: 打分 ≥θ 的候选剔除 → 组合层回测 → 两轴读数。

    剔除语义: 候选池 top100 中 crowd≥θ 的股从买入名单剔除, 槽位由排名后续补足;
    简化口径: 直接取打分后的 top100-crowd 剩余股作为当月持仓名单回测（冻结近似）。
    """
    from app.backtest import engine as E
    from app import config as cfg_mod
    from app import db
    scores = {}
    for l in SCORES.open(encoding="utf-8"):
        r = json.loads(l)
        scores.setdefault(r["anchor"], {})[r["ts_code"]] = r["crowd"]
    # 生成剔除后的分数表: 被剔除股的 score 置 NaN（回测引擎自然跳过）
    conn = db.get_conn()
    try:
        ms = pd.read_sql("SELECT trade_date, ts_code, score, pred_ret FROM model_score "
                         "WHERE trade_date >= '20200601' ORDER BY trade_date, score DESC", conn)
    finally:
        conn.close()
    kept, excluded = 0, 0
    out_rows = []
    for d, g in ms.groupby("trade_date"):
        anchor_scores = scores.get(d)
        if not anchor_scores:
            out_rows.append(g)
            continue
        g = g.sort_values("score", ascending=False)
        hot = {c for c, s in anchor_scores.items() if s >= theta}
        g["rank"] = range(1, len(g) + 1)
        excl = set()
        keep_n = 0
        for _, row in g.iterrows():
            if row["ts_code"] in hot and keep_n < 100:
                excl.add(row["ts_code"])
                excluded += 1
                continue
            keep_n += 1
            if keep_n >= 100:
                break
        kept += 100 - len(excl)
        out_rows.append(g[~g["ts_code"].isin(excl)])
    out = pd.concat(out_rows, ignore_index=True)
    tag = f"excl_theta{theta}"
    st = f"model_score_{tag}"
    conn = db.get_conn()
    try:
        conn.execute(f"DROP TABLE IF EXISTS {st}")
        conn.execute(f"CREATE TABLE {st} (ts_code TEXT, trade_date TEXT, score REAL, "
                     "pred_ret REAL, PRIMARY KEY (ts_code, trade_date)) WITHOUT ROWID")
        conn.executemany(f"INSERT OR REPLACE INTO {st} VALUES (?,?,?,?)",
                         out[["ts_code", "trade_date", "score", "pred_ret"]].itertuples(index=False, name=None))
        conn.commit()
    finally:
        conn.close()
    print(f"剔除表 {st}: 保留 {kept:,}, 剔除 {excluded:,} 股次")
    _orig = cfg_mod.load_config

    def _cap(*a, **k):
        c = _orig(*a, **k)
        c["capital"] = 20000.0
        return c

    cfg_mod.load_config = _cap
    try:
        m = E.run_backtest(score_table=st, run_tag=tag, start="20190722")
    finally:
        cfg_mod.load_config = _orig
    print(json.dumps({"run": m["run_date"], "sys_ann": m["sys_ann_return"],
                      "sys_mdd": m["sys_max_drawdown"]}, ensure_ascii=False))
    return 0


def main() -> int:
    stage = sys.argv[1] if len(sys.argv) > 1 else "sample"
    if stage == "sample":
        return stage_sample()
    if stage == "score":
        return stage_score()
    if stage == "backtest":
        return stage_backtest(int(sys.argv[2]) if len(sys.argv) > 2 else 7)
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main())
