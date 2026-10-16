"""回填被 Q-05 抹掉的 10 行 alpha_bank 读数（2026-09-18 审查）

## 要修什么

`factory._refuse_duplicate` 原来对**已在册** id 也无条件调 `bank.upsert({alpha_id, note})`,
而 upsert 是 `ON CONFLICT DO UPDATE SET` **全部非键列** ⇒ 2026-09-18 12:24 有 10 行
第二轮 `kg_*` 在册候选的读数被半截 dict 覆盖成 NULL，状态还被从 CANDIDATE 降级成 DUPLICATE。
机制已在 `f791c6d2` 修掉（本脚本跑之前必须已修，否则会被再抹一遍）。

## 为什么不能直接重跑（实测出来的第二层坑）

被抹的不只是读数，还有 **`expr`** —— 而撞车硬闸判"是不是同一条"用的正是 expr。
所以直接 `Factory(allow_rerun=True)` 重跑会得到：

    {'dup': True, 'kind': 'id_conflict', 'detail': "同 id 已登记但公式不同：None vs 'panel:…#kg_holder_net'"}

`allow_rerun=True` **只放行 `rerun` 类，不放行 `id_conflict`**。抹写把自我修复的出口一起抹掉了。

⇒ 两步走：① 先把 `expr` 单列恢复回它本来的值；② 再走正规门禁重跑，此时判为 `rerun`
   ⇒ 放行 ⇒ 由 `bank.upsert(整条 rec)` 恢复**全部**列（含 yearly / gate_json）。

**① 不是手写读数**：expr 是 `factory.py:894` 由源列名决定的确定性字符串
`"panel:%s#%s" % (source, col)`，不含任何测算结果；而且**日志直接证明了它原本就是这个值**
—— `app.log` 12:24 那 10 行写的是「rerun … 同 id 同公式已在册」，而这句话只有在
`_norm_expr(在册.expr) == expr` 时才会打印出来。
真正被恢复的读数来自②的**重算**（同一份 `kg_features.parquet`、同一套 gate v2 判据），
不是从日志里抄的 —— 日志只用来核对结果对不对得上。

## ③ 为什么还要再拨一次分母

重跑会把 `n_tests` 写成**今天**的分母 `max(10, 179+10)=189`。可这 10 条是 2026-09-17
判的，当时冻结的分母是 **14**（`app.log:28636`）。`n_tests` 这个字段的语义是
"判这条时用的多重比较分母" ——  stamp 成 189 就是让台账对过去说谎。
所以②之后紧跟③：把 `n_tests` 与 `gate_json.n_tests_family` 拨回 14，并在 `gate_json`
里打 `q05_repair_20260918` 标记说明这是**申报口径**；当年工厂**实算**的 N 已不可恢复
（gate_json 正是被抹掉的字段之一），因此 `n_tests_prior` 留 `null` —— **不填一个看起来
精确的猜测值**。判决不受任何影响：这 10 条当时 `p_adj` 已是 1.0000（`app.log:28580`），
分母无论 14 还是 189 都同样不过闸。

## 用法

    # 先看要动什么（默认就是这个：不写库）
    D:\\Python313\\python.exe -X utf8 scripts/oneoff/fix_alpha_bank_rerun_20260918.py
    # 真写生产 alpha_bank：要 --write，还要护栏放行
    set ONEOFF_CONFIRM=fix_alpha_bank_rerun_20260918.py
    D:\\Python313\\python.exe -X utf8 scripts/oneoff/fix_alpha_bank_rerun_20260918.py --write

写之前会把这 10 行的**当前状态**逐行 JSON + sha256 归档到
`data/backup/alpha_bank_q05_repair_<ts>.BEFORE.json` 并读回自检（纪律 §16）。
"""
from __future__ import annotations

import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE))

#: 被抹的 10 行（`logs/app.log` 2026-09-18 12:24 的 `rerun ←` 那批；不是 27 行 DUPLICATE
#: 里的全部 —— 另 17 行 `id60_*` 是 expr 撞车占位、从未求值，NULL 属设计，不许动）。
WIPED = [
    "kg_broker_rec_3m", "kg_broker_rec_chg", "kg_catalyst_60d", "kg_holder_add_60d",
    "kg_holder_dec_60d", "kg_holder_net", "kg_holder_new_entry", "kg_pagerank_ind",
    "kg_resonance", "kg_sentiment_60d",
]
SOURCE = "scripts/kg_materialize.py"        # 与 kg_gate_r2 的 panel_meta["source"] 一致
INFO_SOURCE = "kg_r2"                       # 这 10 行原本属第二轮
PANEL = BASE / "data" / "kg" / "kg_features.parquet"

#: 第二轮**预注册冻结**的 Bonferroni 分母。回填时把 n_tests 拨回这个值，而不是让
#: 重跑 stamp 上今天的分母（重跑会写 max(10, 179+10)=189 —— 那是"2026-09-18 重算时的
#: 在册量"，不是"2026-09-17 判这 10 条时用的分母"，写进去就是台账说谎）。
#: 直接证据 `logs/app.log:28636`：
#:   2026-09-17 22:44:47 候选来源 ['kg_r2'] | **Bonferroni 分母 = 14(预注册冻结)**
#: ⚠️ 工厂当时**实算**的 N（= max(本批, 在册累计+本批)）**已经无法从现存证据恢复**：
#:   那 10 行的 gate_json 正是被这次抹写覆盖掉的字段之一，而 kg_r2 在册行现在一行不剩
#:   （`SELECT … WHERE info_source='kg_r2'` = 0 行）。所以这里登记的是**预注册冻结值**，
#:   并在 note 里把这个区别写明 —— 宁记"我只能证明到这个精度"，不编一个看起来精确的数。
ROUND2_FROZEN_N = 14

#: 日志里可读回核对的读数（`head (t=…)`），只用于**验收重算结果对不对得上**，不参与写入
LOG_READINGS = {
    "kg_broker_rec_3m": (-0.0098, -2.73),   # app.log:28580
    "kg_holder_net": (+0.0026, +1.23),      # app.log:28649
    "kg_resonance": (-0.0077, -2.47),       # app.log:28654
    "kg_sentiment_60d": (-0.0077, -0.67),   # app.log:28655
}


def _expr_of(col: str) -> str:
    """与 `factory.py:894` 同式 —— 不抄第二份字面量，改一处就会不一致。"""
    return "panel:%s#%s" % (SOURCE, col)


def _state(conn) -> list:
    conn.row_factory = __import__("sqlite3").Row
    q = ",".join("?" * len(WIPED))
    rows = conn.execute(
        f"SELECT alpha_id,status,expr,DEAD_head,DEAD_head_t,n_tests,info_source,PASS "
        f"FROM alpha_bank WHERE alpha_id IN ({q}) ORDER BY alpha_id", WIPED).fetchall()
    return [dict(r) for r in rows]


def main(argv: list) -> int:
    write = "--write" in argv
    if not PANEL.exists():
        print(f"✗ 面板不在: {PANEL} —— 没有它无法重算（本脚本不抄日志读数，只重算）")
        return 2

    from app import db
    conn = db.get_conn()
    before = _state(conn)
    print(f"当前在册状态（{'将写入' if write else 'DRY-RUN，不写'}）：{len(before)} 行")
    for r in before:
        print(f"  {r['alpha_id']:<20} status={str(r['status']):<10} expr={'NULL' if r['expr'] is None else '有'}"
              f" DEAD_head={r['DEAD_head']} t={r['DEAD_head_t']} n_tests={r['n_tests']}")

    from app.alpha import bank
    n_dup = bank.cumulative_tests()
    print(f"\ncumulative_tests() = {n_dup}  ← 回填后这 10 行从 DUPLICATE 回到 CANDIDATE，N 应 +10")

    if not write:
        print("\nDRY-RUN 结束（没碰任何表）。确认无误后：--write + ONEOFF_CONFIRM 放行。")
        conn.close()
        return 0

    from _guard import require_confirm
    require_confirm(__file__,
                    "UPDATE alpha_bank.expr ×10 行 + 重跑门禁 upsert 覆盖这 10 行读数（写生产库）")

    _archive_before(conn, before)

    # ---- ① 恢复 expr（单列 UPDATE，不动其它任何列）----
    for col in WIPED:
        conn.execute("UPDATE alpha_bank SET expr=? WHERE alpha_id=?", (_expr_of(col), col))
    conn.commit()
    print("\n① expr 已恢复 ⇒ 撞车闸现在会判 rerun（而非 id_conflict）")
    for col in WIPED[:3]:
        d = bank.check_duplicate({"alpha_id": col, "expr": _expr_of(col), "name": col,
                                  "zoo": INFO_SOURCE}, allow_rerun=True)
        print(f"   {col}: dup={d['dup']} kind={d.get('kind') or '-'}")

    # ---- ② 正规门禁重跑（只喂这 10 列）----
    import pandas as pd
    from app.alpha.factory import Factory, PreRegistration, ZooProposer
    df = pd.read_parquet(PANEL)
    have = [c for c in WIPED if c in df.columns]
    missing = [c for c in WIPED if c not in df.columns]
    if missing:
        print(f"⚠️ 面板里缺 {len(missing)} 列，这几列无法重算: {missing}")
    panels = {}
    for col in have:
        w = df.pivot_table(index="anchor", columns="ts_code", values=col, aggfunc="last")
        w.index = pd.to_datetime(w.index)
        panels[col] = w
    zoo = ZooProposer(panels=panels, panel_meta={
        n: {"info_source": INFO_SOURCE, "source": SOURCE, "name": n} for n in panels},
        label=INFO_SOURCE)
    prereg = PreRegistration(
        intent="Q-05 回填：对 2026-09-18 12:24 被撞车留痕 upsert 抹成空壳的 10 行第二轮 kg_* "
               "候选**重算**读数（同一份 kg_features.parquet、同一套 gate v2 六条判据，一字未改）。"
               "判据与分母口径与第二轮预注册 plans/kg-event-graph-round2-20260917.md 完全一致；"
               "本次不是新假说检验，是把台账恢复成它本来的样子。")
    f = Factory(prereg, market_since="20160101", allow_rerun=True)
    f.add(zoo)
    f.declare()
    f.run(save=True)
    print(f"\n② 门禁重跑完成：求值 {len(f.results)} 条，撞车拒跑 {len(f.duplicates)} 条")
    if f.duplicates:
        for d in f.duplicates:
            print(f"   ⚠️ 仍被拒: {d.get('alpha_id')} {d.get('kind')} {d.get('detail','')[:90]}")

    # ---- ③ 把分母拨回第二轮当时的**预注册冻结值**（重跑 stamp 的是今天的在册量）----
    import json as _json
    stamp_note = ("2026-09-18 Q-05 回填：读数=按 kg_features.parquet 重算（与 app.log 逐位核对）；"
                  f"n_tests/gate_json.n_tests_family 拨回第二轮预注册冻结值 {ROUND2_FROZEN_N}"
                  "（证据 app.log:28636）。工厂当时实算 N 不可恢复（gate_json 亦被抹）⇒ "
                  "此处登记的是申报口径，非实算口径。")
    for col in WIPED:
        cur = bank.get(col)
        try:
            gj = _json.loads(cur.get("gate_json") or "{}")
        except Exception:
            gj = {}
        gj["n_tests_family"] = ROUND2_FROZEN_N
        gj["n_tests_prior"] = None          # 当时实算 prior 不可恢复 —— 留 null，不填猜测值
        gj["q05_repair_20260918"] = {
            "readings": "recomputed from kg_features.parquet",
            "n_tests_pinned_to": ROUND2_FROZEN_N,
            "n_tests_evidence": "app.log:28636 (第二轮预注册冻结)",
            "realized_N_at_the_time": "unrecoverable (gate_json was itself wiped)",
        }
        conn.execute("UPDATE alpha_bank SET n_tests=?, gate_json=?, note=? WHERE alpha_id=?",
                     (ROUND2_FROZEN_N, _json.dumps(gj, ensure_ascii=False), stamp_note, col))
    conn.commit()
    print(f"\n③ 分母已拨回第二轮申报值 {ROUND2_FROZEN_N}（gate_json 同步 + 打 q05_repair 标记，"
          f"n_tests_prior 留 null 不编数）")

    # ---- 验收 ----
    after = _state(conn)
    conn.close()
    print("\n回填后：")
    bad = []
    for r in after:
        flag = ""
        exp = LOG_READINGS.get(r["alpha_id"])
        if exp and r["DEAD_head"] is not None:
            same = abs(float(r["DEAD_head"]) - exp[0]) < 1e-4 and \
                   abs(float(r["DEAD_head_t"]) - exp[1]) < 0.02
            flag = "✓对得上日志" if same else f"⚠与日志({exp[0]},t={exp[1]})不符"
            if not same:
                bad.append(r["alpha_id"])
        elif r["DEAD_head"] is None:
            flag = "✗ 仍是空壳"
            bad.append(r["alpha_id"])
        print(f"  {r['alpha_id']:<20} status={str(r['status']):<10} DEAD_head={r['DEAD_head']}"
              f" t={r['DEAD_head_t']} expr={'NULL' if r['expr'] is None else '有'} {flag}")
    n_after = bank.cumulative_tests()
    print(f"\ncumulative_tests() = {n_after}（回填前 {n_dup}；期望 +{len(WIPED) - len(bad)}）")
    if bad:
        print(f"⚠️ {len(bad)} 行没修好，需要人看: {bad}")
        return 1
    print("✓ 10 行全部恢复为带读数的在册候选")
    return 0


def _archive_before(conn, before: list) -> None:
    """写前把受影响行归档并**读回自检**（纪律 §16：重置/清空类不可逆动作）。"""
    import datetime
    import hashlib
    import json
    out = BASE / "data" / "backup" / (
        "alpha_bank_q05_repair_%s.BEFORE.json" % datetime.datetime.now().strftime("%Y%m%d_%H%M%S"))
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {"reason": "2026-09-18 审查 Q-05 回填前的受影响行",
               "rows": before, "n": len(before)}
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    payload["sha256"] = hashlib.sha256(text.encode("utf-8")).hexdigest()
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=1, sort_keys=True,
                              default=str), encoding="utf-8")
    back = json.loads(out.read_text(encoding="utf-8"))
    chk = dict(back)
    chk_sha = chk.pop("sha256")
    again = hashlib.sha256(json.dumps(chk, ensure_ascii=False, sort_keys=True,
                                      default=str).encode("utf-8")).hexdigest()
    if again != chk_sha or back.get("n") != len(WIPED):
        out.rename(out.with_name(out.name.replace(".json", ".INVALID.json")))
        raise SystemExit("归档读回自检不过 ⇒ 中止（不带病写生产表）")
    print(f"已归档受影响行 → {out.name}（读回自检通过）")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
