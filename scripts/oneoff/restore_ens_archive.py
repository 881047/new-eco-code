"""查 `data/models/ens/` 的 464 个月度归档能否**忠实复原**（2026-09-11）

## 背景（我造成的问题）

`trainer.py:526-533` 的归档分支是 `if archive_dir is not None: <实验目录> else: models/ens/`，
而**按月归档的 `lgb_YYYYMM_s*.txt` 不受 `is_prod` 保护**（只有 `lgb_latest_s*` 受保护）。
`expert.run_expert` 原先不传 `archive_dir` ⇒ 每次训练 die 都逐月覆盖生产归档。
实测：`ens/` 469 个文件里 **464 个（116 月 × 4 种子）被 h5/h10 实验覆盖**。

**交易链未受影响**（`daily_score.py` 只读 `lgb_latest_s*`，完好），**审计链断裂**。
根因已修（`run_expert` 现在默认 `archive_dir=data/models/die_<name>`）。

## 本脚本做什么

在**临时目录**里按**生产口径**全量重训一次，然后与**未受影响的 `model_score` 表**逐位比对：
  · 口径 = `train_model.py` 不传 `--score-table` 时的那一套（全历史训练 `train_start=None`、
    31 因子从 `factor` 表读、50 轮 × 4 种子、36 月窗、holdout 6、label_mode=full、horizon=21）
  · **不写 `model_score`、不写 `ens/`** —— 打分表与归档都去临时名字

**判据（写在跑之前）**：逐月比对 `model_score_restore_check` vs `model_score`。
  · 全部 116 月**逐位相同** ⇒ `ens/` 可忠实复原 ⇒ 才允许把 `_restore_probe/` 拷进 `ens/`
  · 有任何一月不同 ⇒ **不拷**，如实登记「`ens/` 不可复原」及差异量级
    （**不允许**"差不多就拷" —— 那会造出一个看起来对、其实不权威的审计归档，比明摆着错更糟）

## 实测结果（2026-09-11）

**判据 = 否 —— 不可复原。** 116/116 期、444,380 条记录中不同 6,694 条（1.5064%），
max|Δ|=4.903079e+00；**有差异的评分月只有 2 个：20260731、20260831**（其余 114 月
max|Δ|=0，逐位相同），差异月秩相关 ≥ 0.999968。
**原因不是本脚本的口径**：日链在这两个月之后改写过 `factor` 表 —— 与
`verify_d1_replica.py` 当年遇到的是同一个原因（换种子会让每一只都变，而这里只有
28/4385、101/4396 只不同，是特征值被改写，不是模型/配置差）。

## 用户授权（2026-09-11）

用户在知悉「114/116 逐位相同、其余 2 月秩相关 0.99997」后明确选择
**「复原 114 月 + 标注 2 月为例外」**。因此本脚本提供一个**具名的、记录了授权的**开关
`--apply-user-approved`（与 `state_stocks 6/6/6` 同一模式）：**这不是放宽 `--apply` 的
预注册判据**（`--apply` 的行为一字未改，判据为否时依旧拒绝），而是一次有记录的授权决定。
该开关必然会在 `ens/` 留下三样东西：README、两个例外月的标记文件、以及逐月明细。

用法: ./.venv/Scripts/python.exe scripts/oneoff/restore_ens_archive.py
      ./.venv/Scripts/python.exe scripts/oneoff/restore_ens_archive.py --compare-only
      ./.venv/Scripts/python.exe scripts/oneoff/restore_ens_archive.py --apply   # 比对干净才有效
      ./.venv/Scripts/python.exe scripts/oneoff/restore_ens_archive.py --apply-user-approved
"""
import argparse
import io
import shutil
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

if hasattr(sys.stdout, "buffer"):
    _ORIG_STDOUT = sys.stdout
    sys.stdout = io.TextIOWrapper(_ORIG_STDOUT.buffer, encoding="utf-8", errors="replace")

BASE = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE))

from app import db                                       # noqa: E402
from app.config import DATA_DIR                          # noqa: E402
from app.logger import setup_logger                      # noqa: E402
from app.models import expert as ex                      # noqa: E402
from app.models import trainer                           # noqa: E402

PROBE_T = "model_score_restore_check"
PROBE_I = "model_importance_restore_check"
PROBE_DIR = DATA_DIR / "models" / "_restore_probe"
ENS_DIR = DATA_DIR / "models" / "ens"


DETAIL = {}          # {YYYYMMDD: (n_diff, n_total, max_abs, rank_corr)}


def compare(log) -> bool:
    conn = db.get_conn()
    try:
        a = pd.read_sql(f"SELECT ts_code, trade_date, score FROM model_score "
                        f"ORDER BY trade_date", conn)
        b = pd.read_sql(f"SELECT ts_code, trade_date, score FROM {PROBE_T} "
                        f"ORDER BY trade_date", conn)
    finally:
        conn.close()
    a["trade_date"] = a["trade_date"].astype(str)
    b["trade_date"] = b["trade_date"].astype(str)

    ma, mb = set(a["trade_date"]), set(b["trade_date"])
    log.info("生产 %d 期 / 探针 %d 期 | 只在生产 %s | 只在探针 %s",
             len(ma), len(mb), sorted(ma - mb)[:5], sorted(mb - ma)[:5])

    j = a.merge(b, on=["ts_code", "trade_date"], how="inner",
                suffixes=("_prod", "_probe"))
    log.info("共同评分记录 %d 条 (生产 %d / 探针 %d)", len(j), len(a), len(b))
    if j.empty:
        log.info("**无法比对 —— 无交集**")
        return False

    j["d"] = (j["score_prod"] - j["score_probe"]).abs()
    bad = int((j["d"] > 0).sum())
    log.info("逐位比对: 不同 %d / %d 条 (%.4f%%) | max|Δ| = %.6e",
             bad, len(j), 100.0 * bad / len(j), float(j["d"].max()))

    per = j.groupby("trade_date")["d"].agg(["max", "size"])
    nbad = int((per["max"] > 0).sum())
    log.info("有差异的评分月: %d / %d", nbad, len(per))
    # 逐月明细: 差异条数 / 总条数 / max|Δ| / 秩相关(排序是否被换掉)
    for d, sub in j.groupby("trade_date"):
        rc = 1.0
        if sub["d"].max() > 0:
            x = sub["score_prod"].rank().to_numpy(dtype=float)
            y = sub["score_probe"].rank().to_numpy(dtype=float)
            if x.std() > 0 and y.std() > 0:
                rc = float(np.corrcoef(x, y)[0, 1])
        DETAIL[d] = (int((sub["d"] > 0).sum()), int(len(sub)), float(sub["d"].max()), rc)
    if nbad:
        log.info("  差异最大的 6 个月:")
        for d, r in per.sort_values("max", ascending=False).head(6).iterrows():
            log.info("    %s  max|Δ|=%.6e", d, r["max"])
        cs = [v[3] for v in DETAIL.values() if v[2] > 0]
        log.info("  逐月秩相关(差异月): 最小 %.6f / 中位 %.6f", float(np.min(cs)),
                 float(np.median(cs)))

    ok = (bad == 0 and ma == mb)
    log.info("**判据: %s**（要求 116 月逐位相同且期数一致）", "PASS — 可复原" if ok else "**否 — 不可复原**")
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--compare-only", action="store_true")
    ap.add_argument("--apply", action="store_true", help="比对干净时把 probe 拷进 ens/")
    ap.add_argument("--apply-user-approved", action="store_true",
                    help="经用户 2026-09-11 授权: 复原 114 月 + 标注 2 月为例外"
                         "（见 docstring；不放宽 --apply 的预注册判据）")
    args = ap.parse_args()
    setup_logger()
    db.init_db()
    log = __import__("logging").getLogger("restore")

    if not args.compare_only:
        log.info("== 按生产口径全量重训到 %s (归档 %s) ==", PROBE_T, PROBE_DIR)
        log.info("   **不写 model_score, 不写 ens/** —— 先验证再决定是否复原")
        # trainer.run 只校验表名合法性、**不建表**(建表在 expert.ensure_score_table);
        # 直接调 trainer.run 必须先自己建好打分表与重要性表。
        ex.ensure_score_table(PROBE_T)
        conn = db.get_conn()
        try:
            conn.execute("CREATE TABLE IF NOT EXISTS {} (train_date TEXT, factor TEXT, "
                         "importance REAL, PRIMARY KEY (train_date, factor))"
                         .format(PROBE_I))
            conn.commit()
        finally:
            conn.close()
        t0 = time.time()
        trainer.run(train_months=36, backtest_start="2017-01-01", full=True,
                    train_start=None,
                    score_table=PROBE_T, importance_table=PROBE_I,
                    archive_dir=PROBE_DIR,
                    num_boost_round=trainer.ENSEMBLE_ROUNDS,
                    early_stopping_rounds=0,
                    ensemble_seeds=trainer.ENSEMBLE_SEEDS,
                    holdout_recent=6, label_mode="full", horizon=trainer.HORIZON)
        log.info("训练完成 %.0fs", time.time() - t0)

    ok = compare(log)

    n_probe = len(list(PROBE_DIR.glob("lgb_*_s*.txt")))
    log.info("探针归档 %d 个文件; ens/ 现有 %d 个", n_probe, len(list(ENS_DIR.glob("lgb_*"))))
    if args.apply and not ok:
        log.info("**拒绝拷入 ens/** —— 比对未通过(判据写在脚本 docstring 里, 不放宽)")
        return
    if not (args.apply or args.apply_user_approved):
        if ok:
            log.info("比对干净; 要落地请加 --apply")
        else:
            log.info("比对不干净; 若要按用户 2026-09-11 的授权复原, 加 --apply-user-approved")
        return

    # ---- 拷贝 -------------------------------------------------------------
    exceptions = sorted(d for d, v in DETAIL.items() if v[2] > 0)
    n = 0
    for f in sorted(PROBE_DIR.glob("lgb_*_s*.txt")):
        shutil.copy2(f, ENS_DIR / f.name)
        n += 1
    log.info("已从 %s 拷入 ens/ %d 个文件", PROBE_DIR, n)
    if args.apply_user_approved:
        log.info("  **授权路径**: 判据为否, 例外月 %s —— 必须写标记(否则是静默伪造)",
                 exceptions or "无")

    # ---- 例外月标记 -------------------------------------------------------
    for d in exceptions:
        # 归档文件名用的是 YYYYMM(trade_date 是 YYYYMMDD)
        ym = d[:6]
        files = sorted(ENS_DIR.glob(f"lgb_{ym}_s*.txt"))
        rc = DETAIL[d][3]
        p = ENS_DIR / f"EXCEPTION_{ym}_RESTORED.md"
        p.write_text(
            f"# ⚠️ 例外：{ym} 这一个月不是当时实际所用的模型\n\n"
            f"（评分日 `trade_date = {d}`）\n\n"
            f"`ens/` 的月度归档在 2026-09-11 被实验覆盖（464/469 个文件），本次按生产口径\n"
            f"全量重训后逐月比对复原。**{len(DETAIL) - len(exceptions)} 个月与生产 `model_score`\n"
            f"逐位相同（max|Δ| = 0）**；**本月的 {len(files)} 个文件（`lgb_{ym}_s*.txt`）是重推的**：\n\n"
            f"| 项 | 值 |\n|---|---|\n"
            f"| 与该月生产分数不同的股票 | {DETAIL[d][0]} / {DETAIL[d][1]} |\n"
            f"| max\\|Δ\\| | {DETAIL[d][2]:.6e} |\n"
            f"| 与该月生产分数的秩相关 | {rc:.6f} |\n\n"
            f"**排序近乎不变（秩相关 {rc:.6f}），但不是当时实际落盘的那份权重** —— 原因是\n"
            f"该评分月之后日链改写过 `factor` 表（同 `verify_d1_replica.py` 当年的原因），\n"
            f"重训只能复现「今天的数据 + 当时的口径」，无法复现「当时的数据 + 当时的模型」。\n"
            f"审计上请以 `model_score` 表为准。\n", encoding="utf-8")
        log.info("  写标记 %s", p.name)

    # ---- README -----------------------------------------------------------
    total = len(DETAIL)
    n_ident = total - len(exceptions)
    lines = [
        "# `data/models/ens/` 复原说明（2026-09-11）",
        "",
        "## 发生了什么",
        "",
        "`app/models/trainer.py` 的归档分支是 `if archive_dir is not None: <实验目录>`",
        "`else: DATA_DIR/models/ens/`，而按月归档的 `lgb_YYYYMM_s*.txt` **不受 `is_prod` 保护**",
        "（只有 `lgb_latest_s*` 受保护）。`expert.run_expert` 当时不传 `archive_dir`，",
        f"于是每次训练实验 die 都逐月覆盖生产归档 —— `ens/` 469 个文件里 **464 个（116 月 × 4 种子）",
        "被 h5/h10 实验覆盖**。",
        "",
        "**交易链未受影响**（`daily_score.py` 只读 `lgb_latest_s*`，5 个文件完好）；**审计链断裂**。",
        "根因已修两层：`run_expert` 默认 `archive_dir=data/models/die_<name>`，",
        "`trainer.run` 对实验表且未指定 `archive_dir` 直接 `raise`。",
        "",
        "## 怎么复原的",
        "",
        "`scripts/oneoff/restore_ens_archive.py` 按**生产口径**全量重训一次",
        "（全历史训练、31 因子、50 轮 × 4 种子、36 月窗、holdout 6、label_mode=full、horizon=21），",
        "打分表与归档都去临时名字（`model_score_restore_check` / `data/models/_restore_probe/`），",
        "然后与**未受影响的 `model_score` 表**逐月、逐股比对。",
        "",
        "## 结果与判据",
        "",
        f"- **预注册判据（跑之前写死）= 要求 116 月逐位相同。实测 = 否 —— 不可复原。**",
        f"- 逐位比对：不同 6,694 / 444,380 条（1.5064%），max\\|Δ\\| = 4.903079e+00",
        f"- **有差异的月份只有 {len(exceptions)} 个：{', '.join(e[:6] for e in exceptions)}**"
        f"（评分日 {', '.join(exceptions)}）；",
        f"  其余 **{n_ident} 个月 max\\|Δ\\| = 0（逐位相同）**",
        f"- 差异月秩相关：最小 {min(v[3] for v in DETAIL.values() if v[2] > 0):.6f}",
        "",
        "原因是该 2 个评分月之后日链改写过 `factor` 表（同 `verify_d1_replica.py` 当年：",
        "换种子会让每一只都变，而这里只有 28/4385、101/4396 只不同 ⇒ 是特征值被改写，",
        "不是模型/配置差）。重训只能复现「今天的数据 + 当时的口径」，**无法复现「当时的数据 + 当时的模型」**。",
        "",
        "## 本次落地的决定（有记录的授权，非放宽判据）",
        "",
        "用户在知悉上述读数后选择 **「复原 114 月 + 标注 2 月为例外」**。",
        "因此本次用 `--apply-user-approved` 拷贝，而**没有**放宽 `--apply` 的预注册判据",
        "（`--apply` 在判据为否时依旧拒绝，行为一字未改）。这与 `state_stocks 6/6/6` 是同一模式：",
        "**一次具名的、记录了授权的例外，而不是把判据改成能通过。**",
        "",
        f"⇒ 使用 `ens/` 做审计时：**{n_ident} 个月可信（与生产逐位相同）**；",
        f"**{len(exceptions)} 个月见同目录 `EXCEPTION_*_RESTORED.md`**。",
        "任何情况下**以 `model_score` 表为准**。",
        "",
        "## 逐月明细",
        "",
        "| 评分月 | 不同条数 | 总条数 | max\\|Δ\\| | 秩相关 |",
        "|---|---|---|---|---|",
    ]
    for d in sorted(DETAIL):
        nd, nt, ma, rc = DETAIL[d]
        if ma == 0:
            continue
        lines.append(f"| **{d[:6]}** ⚠️ | {nd} | {nt} | {ma:.6e} | {rc:.6f} |")
    lines.append(f"| 其余 {n_ident} 个月 | 0 | — | 0 | 1.000000 |")
    (ENS_DIR / "README_RESTORE_20260911.md").write_text("\n".join(lines) + "\n",
                                                        encoding="utf-8")
    log.info("已写 ens/README_RESTORE_20260911.md（%d 月逐位相同, 例外 %s）",
             n_ident, exceptions)
    log.info("**ens/ 月度归档已复原** —— 文件数 %d", len(list(ENS_DIR.glob("lgb_*"))))


if __name__ == "__main__":
    main()
