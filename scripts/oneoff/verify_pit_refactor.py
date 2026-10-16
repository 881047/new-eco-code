"""PIT Engine 重构的**逐位验收** —— 任务 #32(2026-09-11)

## 在验什么

`app/models/trainer.py` 的 `tradable_mask` 从「内嵌闭包」改成「委托 `app.pit.engine.PITEngine`」。
重构的**唯一可接受后果是"什么也没发生"**; 任何口径漂移都是回归。

## 为什么不能只看代码

口径有六层(上市满60日 / 停牌 / 时点ST / 板块 / 市值分位 / 均额下限), 每一层都容易在
搬运时"顺手写得更合理" —— 而本项目最贵的一次错误(旧基线年化高估 **28.78pp**)正是
时点口径写错。**逐字搬运不是证据, 对拍才是证据。**

## 判据(预注册, 写在跑之前)

**基准 = 生产表 `model_score` 的行集合本身。**

理由: `trainer.py:507` 是「先 `mask` 再写 `X_d.index`」⇒ 每个月的行集合 = 当月的
`tradable_mask(dt)` 输出域。该表是**旧内联代码**写出的 ⇒ 它就是旧口径的完整快照,
不需要去 git 或 transcript 里恢复源码(没提交过, 恢复成本高且不可靠)。

判据: 对 `model_score` 的**全部 116 个月末**, 要求

    set(model_score 在 dt 的 ts_code) == set(PITEngine.tradable_mask(dt) 为 True 的代码)

**逐期对称差必须为 0**。任何一期不为 0 ⇒ 委托引入了口径漂移, 打印差异样本(代码 + 名称 +
层别)后以非零码退出。

## 免责边界(写在跑之前)

- 表是**旧代码**写的, 但它是**同一批行情数据 + 同一份 config** 下写的; 若 config 或行情
  在两次写入之间变过, 本对拍会把那份漂移**误记在重构头上**。故脚本同时打印 config 快照,
  并在差异非 0 时**先报告 config/数据是否动过**, 再下结论。
- 本脚本**只读**。不写任何表, 不碰 `model_score`。
- 板块层: 生产 config `star_enabled=False` / `bse_enabled=False`, 与 `tradable_mask(dt)` 的
  默认参数一致(`allow_bse=False`, `star_enabled` 取 config)。

用法:
    ./.venv/Scripts/python.exe scripts/oneoff/verify_pit_refactor.py
    ./.venv/Scripts/python.exe scripts/oneoff/verify_pit_refactor.py --limit 12   # 抽查
"""
import argparse
import io
import sys
import time
from pathlib import Path

import pandas as pd

# ⚠️ 先留住原 wrapper 的引用: TextIOWrapper 接管 sys.stdout.buffer 后, 旧 wrapper 一旦被 GC
# 就会关掉底层 buffer(症状=日志文件正常, 控制台全 "I/O operation on closed file")。
_ORIG_STDOUT = sys.stdout
if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(_ORIG_STDOUT.buffer, encoding="utf-8", errors="replace")

BASE = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE))

from app import config as C, db                                   # noqa: E402
from app.factors import factor_engine as fe                       # noqa: E402
from app.models.names import NameLookup                           # noqa: E402
from app.pit.engine import PITEngine                              # noqa: E402

SCORE_T = "model_score"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--table", default=SCORE_T, help="基准表(默认生产 model_score)")
    ap.add_argument("--limit", type=int, default=0, help="只抽查前 N 期(默认全量)")
    args = ap.parse_args()

    cfg = C.load_config()
    print("=" * 92)
    print("PIT Engine 重构逐位验收 | 基准表 %s | config: star=%s bse=%s size_pctl=%s "
          "min_amt20=%s data_start=%r" % (
              args.table, cfg.get("star_enabled"), cfg.get("bse_enabled"),
              cfg.get("stock_size_pctl"), cfg.get("stock_min_amt20"),
              cfg.get("data_start")))
    print("=" * 92)

    conn = db.get_conn()
    rows = pd.read_sql(
        "SELECT trade_date, ts_code FROM {} ORDER BY trade_date, ts_code".format(args.table),
        conn)
    conn.close()
    if not len(rows):
        print("基准表为空, 无法对拍")
        return 2
    by_date = {d: set(g["ts_code"]) for d, g in rows.groupby("trade_date")}
    dates = sorted(by_date)
    if args.limit:
        dates = dates[:args.limit]
    print("基准: %d 期 (%s ~ %s), 共 %d 行" % (
        len(dates), dates[0], dates[-1], sum(len(by_date[d]) for d in dates)))

    t0 = time.time()
    # 生产 `data_start=None` ⇒ 全历史面板; 必须同源, 否则 first_trade 锚点会错
    m = fe.load_market(since=None)
    pit = PITEngine(m, cfg=cfg)
    print("行情面板 %d 天 × %d 只 (%.0fs)" % (
        m["close"].shape[0], m["close"].shape[1], time.time() - t0))
    cal = set(pd.to_datetime(m["close"].index).strftime("%Y%m%d"))
    missing = [d for d in dates if d not in cal]
    if missing:
        print("⚠️ %d 个基准日在行情日历里找不到(前 5: %s) —— 对拍会失真, 先查数据"
              % (len(missing), missing[:5]))

    nl = NameLookup()
    bad, n_examples = [], 0
    t1 = time.time()
    for i, d in enumerate(dates, 1):
        dt = pd.Timestamp(d)
        got = set(pit.tradable_mask(dt).pipe(lambda s: s.index[s.fillna(False)]))
        want = by_date[d]
        extra, lack = got - want, want - got
        flag = "" if not (extra or lack) else "  ✗ 多 %d / 缺 %d" % (len(extra), len(lack))
        print("[%3d/%3d] %s  基准 %4d  重算 %4d%s" % (i, len(dates), d, len(want), len(got), flag))
        if extra or lack:
            bad.append((d, extra, lack))
            if n_examples < 3:
                n_examples += 1
                for tag, s in (("重构多出", extra), ("重构缺失", lack)):
                    for c in sorted(s)[:6]:
                        nm = nl.name_at(c, d)
                        print("      %s: %s %s (首现 %s)" % (
                            tag, c, nm, pit._first_trade.get(c)))
    print("-" * 92)
    print("耗时 %.0fs" % (time.time() - t1))
    if not bad:
        print("✅ **逐期对称差全为 0** —— 委托 PITEngine 后生产口径逐位不变")
        print("   (判据写在脚本 docstring: 116 期全等; 基准 = 旧内联代码写出的生产行集合)")
        return 0
    print("❌ %d/%d 期不一致 —— 委托引入了口径漂移" % (len(bad), len(dates)))
    print("   先排查: config 是否变过 / 行情是否重拉过 / 基准表是否是旧口径写的;")
    print("   三者都排除后, 差异才算在重构头上。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
