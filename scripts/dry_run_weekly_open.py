"""开盘前**彩排**周频袖套执行档（只读生产数据 + 临时账本，2026-09-13）

为什么要有它：`doctor` 只能证明"闸门会放行"，证明不了"代码跑起来不炸"。2026-09-14 是本项目
**第一个纸面执行档**，所以在真跑之前，用**同一份生产代码 + 同一份生产方案**在**临时账本**上
走一遍 `run_open_slot()`：解析锚点、读方案、取价、落账、快照。任何环境问题（表结构漂移、
接口异常、路径权限）都会在彩排里暴露，而不是在开盘那 1 分钟里。

**安全边界（脚本自己会验）**：
  · 账本走 `tempfile` 临时目录 ⇒ **绝不创建/改动** `data/weekly/ledger.json`；
  · 执行档**没有邮件出口**（2026-09-18 起袖套读数并入 18:30 日终总结）⇒ 不会发信，
    输出里的 `email_outlet_present` 就是这条的自证；
  · 只读方案文件与行情库，不写任何生产产物；
  · 跑完自检"生产账本指纹未变"，变了就报错退出（宁可吵，不许静默）。

用法::

    D:\\Python313\\python.exe -X utf8 scripts\\dry_run_weekly_open.py               # 今天
    D:\\Python313\\python.exe -X utf8 scripts\\dry_run_weekly_open.py --trade-date 20260914

注意：非交易时段取价必然返回空（行情接口只在 09:25-11:35 / 12:55-15:05 外呼），
此时的预期读数是 **trades=0 + "顺延晚间档"** —— 这恰好也验证了降级路径。
"""
import argparse
import atexit
import datetime
import json
import shutil
import sys
import tempfile
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

PROD_LEDGER = BASE / "data" / "weekly" / "ledger.json"


def _ledger_fingerprint():
    """生产账本的指纹（不存在 → None）：用来证明彩排没碰它。"""
    if not PROD_LEDGER.exists():
        return None
    st = PROD_LEDGER.stat()
    return (st.st_size, st.st_mtime_ns)


def main() -> int:
    ap = argparse.ArgumentParser(description="开盘前彩排周频袖套执行档（只读 + 临时账本）")
    ap.add_argument("--trade-date", default=None, help="YYYYMMDD；缺省=今天")
    args = ap.parse_args()
    date = args.trade_date or datetime.date.today().strftime("%Y%m%d")

    before = _ledger_fingerprint()
    tmpdir = Path(tempfile.mkdtemp(prefix="neweco_dryrun_"))
    tmp = tmpdir / "ledger.json"
    # 2026-09-25 补：临时目录原来**从不清理** ⇒ 本机 `%TEMP%` 已留 4 个 `neweco_dryrun_*`。
    # 这是本仓"mkdtemp 了不 rmtree"缺陷族的**第 7 处**（07 号 #64）—— 而且它在 `scripts/`
    # 里，**上一轮的横向扫描只扫了 `tests/` 所以漏掉了它**（教训：扫描范围要含 `scripts/`）。
    # 用 `atexit` 而非 try/finally：本函数有**两条 return 路径**（正常 0 / 账本被改 2）、
    # 还可能抛异常 —— `atexit` 一次覆盖全部，且不必给函数体再加一层缩进。
    atexit.register(shutil.rmtree, tmpdir, ignore_errors=True)
    from app.weekly import pipeline as pl
    from app.weekly import report as wk_report
    # 2026-09-18 邮件优化：执行档**已经没有任何邮件出口**（袖套 KPI 那封并入 18:30 日终总结），
    # 所以这里不再需要给发信打桩。留一行自证：将来谁把出口加回来，这个字段就会变 true。
    res = pl.run_open_slot(trade_date=date, ledger_path=tmp)
    after = _ledger_fingerprint()
    if before != after:
        print("!! 彩排改动了生产账本 —— 立即停止并排查", file=sys.stderr)
        return 2
    led = json.loads(tmp.read_text(encoding="utf-8")) if tmp.exists() else {}
    print(json.dumps({"trade_date": date, "result": res,
                      "temp_ledger": {"path": str(tmp), "exists": tmp.exists(),
                                      "cash": led.get("cash"),
                                      "positions": len(led.get("positions") or {}),
                                      "nav_points": len(led.get("nav_history") or [])},
                      "email_outlet_present": hasattr(wk_report, "send_kpi_email"),
                      "prod_ledger_untouched": True},
                     ensure_ascii=False, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
