"""生产起点迁移到 2019-07-22(2026-09-10 用户拍板, 科创板开市日)

原指令: 「数据现在起进行剔除到2019年科创板上市, 回测起点, 模型起点都改」。
实测拆解见 app/config.py 的 data_start 注释与 scripts/oneoff/exp_start_2019.py。

本脚本把四处一次性切到位。**全部走「改名备份」, 零删除**, `rollback` 可完整还原:

  1. 表 model_score           → model_score_bak_pre2019
     model_importance        → model_importance_bak_pre2019
  2. 表 model_score_star19    → model_score        (73 个月, 20200831~20260831)
     model_importance_star19 → model_importance
  3. 目录 data/models/ens            → data/models/ens_bak_pre2019
     新建 data/models/ens  ← ens_star19/ 全部逐月模型 + 由最后一期(202608)派生
     lgb_latest_s{seed}.txt 与 lgb_latest_meta.txt
  4. 文件 data/models/lgb_latest.txt → lgb_latest_bak_pre2019.txt
     新建 lgb_latest.txt ← ens_star19/lgb_202608_s42.txt (SEED 默认 42, 与单模型路径同源)

第 3/4 步不可省: star19 训练因 `is_prod = (score_table == "model_score")` 为假**没有写
lgb_latest 系列**(trainer.py:380/392)。只换表不换模型文件, 会造成「月频分数来自 star19
模型、日频监控仍来自全历史模型」的静默错配 —— 看板与持仓监控会与方案不同源。

用法:
  python scripts/oneoff/migrate_start_2019.py check      # 只体检, 不改任何东西
  python scripts/oneoff/migrate_start_2019.py apply      # 执行迁移
  python scripts/oneoff/migrate_start_2019.py rollback   # 还原


# STATUS: DESTRUCTIVE / SUPERSEDED —— **不要重跑**(2026-09-12 独立审计 #10)。
# 本脚本会把 model_score 改名成 model_score_bak_pre2019、再把 model_score_star19 提为 model_score;
# 现状实测(只读): model_score_bak_pre2019 不存在, model_score 444,380 行 = 当前 pin
# `20260912_limitquant` 赖以测量的那张表。一旦重跑, 当前基线立即失效。迁移已应用并回滚
# (config.yaml 的 data_start: null)。保留本文件仅供追溯, 需要理解历史请读其 docstring。"""
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app import db
from app.config import DATA_DIR
from app.models.trainer import ENSEMBLE_SEEDS

MODELS = DATA_DIR / "models"
ENS = MODELS / "ens"
ENS_NEW = MODELS / "ens_star19"
ENS_BAK = MODELS / "ens_bak_pre2019"
LATEST = MODELS / "lgb_latest.txt"
LATEST_BAK = MODELS / "lgb_latest_bak_pre2019.txt"

TBL_MAP = [("model_score", "model_score_bak_pre2019"),
           ("model_importance", "model_importance_bak_pre2019"),
           ("model_score_star19", "model_score"),
           ("model_importance_star19", "model_importance")]


def _has_table(conn, t):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)).fetchone()


def _win(conn, t):
    # importance 表的日期列叫 train_date(见 trainer 建表), score 表叫 trade_date
    col = "train_date" if "importance" in t else "trade_date"
    return conn.execute(f"SELECT COUNT(DISTINCT {col}), MIN({col}), MAX({col}) "
                        f"FROM {t}").fetchone()


def check():
    conn = db.get_conn()
    print("--- 表 ---")
    for t in ("model_score", "model_importance", "model_score_star19",
              "model_importance_star19", "model_score_bak_pre2019"):
        if _has_table(conn, t):
            n, a, b = _win(conn, t)
            print("  {:<26s} {:>8d} 月  {} -> {}".format(t, n, a, b))
        else:
            print("  {:<26s} (无)".format(t))
    conn.close()
    print("--- 目录 ---")
    for p in (ENS, ENS_NEW, ENS_BAK):
        print("  {:<34s} {} 个文件".format(str(p.relative_to(DATA_DIR)),
                                           len(list(p.glob('*.txt'))) if p.exists() else "不存在/"))
    print("  {:<34s} {}".format("lgb_latest.txt", "存在" if LATEST.exists() else "不存在"))


def _precheck(conn, seq):
    """在表名集合上**模拟**整个改名序列再判定。

    不能对每一步单独判 `dst 是否已存在` —— 本迁移里 model_score 既是第 1 步的源又是
    第 3 步的目标, 单步判定会误报「已迁移过」并把正确的序列挡掉。模拟则天然正确:
    第 3 步执行时 model_score 这个名字已被第 1 步腾空。
    """
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    for src, dst in seq:
        if src not in names:
            raise SystemExit(f"缺表 {src} —— 终止, 未做任何修改")
        if dst in names:
            raise SystemExit(f"表名 {dst} 被占用, 无法把 {src} 改名为它 —— 终止")
        names.discard(src)
        names.add(dst)


def apply():
    conn = db.get_conn()
    # ---- 全部预检必须在任何破坏性动作之前 ----
    _precheck(conn, TBL_MAP)
    # 模型文件预检同样前置: 否则中途 SystemExit 会留下「表已换、文件未换」的半迁移
    # 状态(最坏 = 月频 star19 + 日频全历史), 正是本次迁移要消除的错配
    if ENS_BAK.exists():
        raise SystemExit(f"{ENS_BAK.name}/ 已存在 —— 疑似已迁移过, 终止")
    if not ENS_NEW.exists():
        raise SystemExit(f"{ENS_NEW.name}/ 不存在 —— 无法派生 lgb_latest, 终止")
    for s in ENSEMBLE_SEEDS:
        if not (ENS_NEW / f"lgb_202608_s{s}.txt").exists():
            raise SystemExit(f"缺 ens_star19/lgb_202608_s{s}.txt —— 无法生成 latest, 终止")

    # ---- 预检全过, 开始改名 ----
    for src, dst in TBL_MAP:
        conn.execute(f"ALTER TABLE {src} RENAME TO {dst}")
    conn.commit()
    conn.close()
    print("DB 表已改名:")
    for src, dst in TBL_MAP:
        print("  {} -> {}".format(src, dst))

    ENS.rename(ENS_BAK)
    ENS.mkdir(parents=True)
    copied = 0
    for f in ENS_NEW.glob("lgb_*_s*.txt"):
        shutil.copy2(f, ENS / f.name)
        copied += 1
    for s in ENSEMBLE_SEEDS:
        shutil.copy2(ENS_NEW / f"lgb_202608_s{s}.txt", ENS / f"lgb_latest_s{s}.txt")
    (ENS / "lgb_latest_meta.txt").write_text(",".join(map(str, ENSEMBLE_SEEDS)),
                                             encoding="utf-8")
    if LATEST.exists():
        shutil.copy2(LATEST, LATEST_BAK)
    shutil.copy2(ENS_NEW / f"lgb_202608_s{ENSEMBLE_SEEDS[0]}.txt", LATEST)
    print(f"模型文件: {copied} 个逐月模型 + {len(ENSEMBLE_SEEDS)} 个 latest 已写入 {ENS}")
    print(f"          旧目录备份于 {ENS_BAK.name}/, 旧 lgb_latest 备份于 {LATEST_BAK.name}")


def rollback():
    conn = db.get_conn()
    seq = [(dst, src) for src, dst in reversed(TBL_MAP)]
    _precheck(conn, seq)
    for src, dst in seq:
        conn.execute(f"ALTER TABLE {src} RENAME TO {dst}")
    conn.commit()
    conn.close()
    print("DB 表已还原")
    if ENS_BAK.exists():
        if ENS.exists():
            shutil.rmtree(ENS)
        ENS_BAK.rename(ENS)
        print(f"{ENS_BAK.name}/ -> {ENS.name}/")
    if LATEST_BAK.exists():
        shutil.copy2(LATEST_BAK, LATEST)
        print("lgb_latest.txt 已还原")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "check"
    if cmd == "check":
        check()
    elif cmd == "apply":
        apply()
        print()
        check()
    elif cmd == "rollback":
        rollback()
        print()
        check()
    else:
        raise SystemExit(__doc__)
