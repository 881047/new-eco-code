"""一次性补库: 分析师研报预测(`report_rc`) → 表 `report_rc`(2026-09-13)

**为什么是它**：第 12 个数据族（卖方盈利预测/评级/目标价），是"新信息"路线里文献依据最强的
一类；第 11 族（券商金股）已结清为"因子层显著、组合层不可利用"，需要更强的信息源。

**硬约束（2026-09-13 实测，两种文案都出现过）**：Tushare `report_rc` **限速 1 次/分钟 +
10 次/小时**。单次返回上限 5000 行，一个月约 5600 行（2026-07 实测 5000+644）⇒ 全史
（2020-08 起 74 个月）约 **83 次调用 ≈ 9 小时**，只能做成**可断点续跑的后台任务**。

**设计（每一环都能独立失败重来）**：
  · 任务粒度 = (月份, offset)：`offset=0` 起，返回行数 == 5000（撞上限）⇒ 继续 `offset+5000`；
    返回 < 5000 ⇒ 该月完成；
  · 检查点 `data/.report_rc_backfill.json` 记 `last_call_ts`（跨重启节流）+ `next_offset`
    （未完成月份）+ `done`（完成月份）⇒ 中断/重启后**从断点续跑，不重复烧配额**；
  · 每次调用前 sleep 到"距上次调用 ≥ 390 秒"（≈9.2 次/小时，留 1 次余量）；
  · 异常（限流/网络/接口变更）**不终止**：按限流退避后重试，仍失败则记日志继续下一个月份，
    下次运行再补；
  · **只写新表 `report_rc`**，不碰任何既有表（不变量 #7：不碰实盘链路）。

**PIT 口径**：`report_date` = 研报发布日（真实可交易日）⇒ 因子层必须按它对齐，不许用
`create_time` 或报告期 `quarter`（那是内容口径，会造成前视）。

运行（后台，建议 detached）：
    set ONEOFF_CONFIRM=backfill_report_rc.py
    D:\\Python313\\python.exe -X utf8 scripts\\oneoff\\backfill_report_rc.py
"""
import json
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE))

import pandas as pd                                                 # noqa: E402

from app import db                                                  # noqa: E402
from app import config as cfg_mod                                   # noqa: E402

STATE = cfg_mod.DATA_DIR / ".report_rc_backfill.json"
START_MONTH = "2020-08"
MIN_INTERVAL = 390.0          # 秒；10 次/小时 ⇒ 至少 360，留余量
PAGE = 5000                   # 单次返回上限（撞到它就说明还有下一页）
# 目标表列（必须与 `app/db.py` 的 DDL 逐字一致 —— upsert_df 是 INSERT OR REPLACE 整行重插，
# 缺列会把该列写成 NULL 覆盖旧值）。来源：Tushare 官方文档 doc_id=292「输出参数」表 +
# 2026-09-13 实测前 8 列一致。写入前逐次校验，上游变更会**当场报错**而不是静默丢列。
REPORT_RC_COLS = ["ts_code", "name", "report_date", "report_title", "report_type",
                  "classify", "org_name", "author_name", "quarter",
                  "op_rt", "op_pr", "tp", "np", "eps", "pe", "rd", "roe", "ev_ebitda",
                  "rating", "max_price", "min_price", "imp_dg", "create_time"]


def _sqlite_safe(df: pd.DataFrame) -> pd.DataFrame:
    """把 Timestamp/NaT 这类 sqlite3 **无法绑定**的对象转成字符串。

    `db.upsert_df` 只做 `astype(object)` + `where(notna, None)`，不认 pandas 的 Timestamp；
    而 report_rc 的 `create_time` 恰好是 datetime ⇒ 不转就会在 executemany 上抛
    "Error binding parameter"。日期统一 `YYYY-MM-DD HH:MM:SS`，与其它表口径一致。

    注意 NaT：它有 `strftime` 属性但**调用即抛** `ValueError: NaTType does not support strftime`
    —— 必须先判缺失再转（2026-09-13 实测踩到）。
    """
    def conv(v):
        try:
            if pd.isna(v) is True:
                return None
        except (TypeError, ValueError):
            pass
        return v.strftime("%Y-%m-%d %H:%M:%S") if hasattr(v, "strftime") else v

    out = df.copy()
    for c in out.columns:
        if out[c].dtype == object or str(out[c].dtype).startswith("datetime"):
            out[c] = out[c].map(conv)
    return out


def _load_state() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text(encoding="utf-8"))
        except Exception:
            print(f"[warn] 检查点损坏, 重建: {STATE}", flush=True)
    return {"last_call_ts": 0.0, "next_offset": {}, "done": []}


def _save_state(st: dict) -> None:
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")


def _wait(st: dict, interval: float = None) -> None:
    """等到"距上次调用 ≥ interval 秒"。interval 在此**运行时**读取模块常量。

    ⚠️ 不许写成 `def _wait(st, interval=MIN_INTERVAL)`：默认参数在**函数定义时**求值，
    之后改模块常量（测试里把节流调成 0）不会生效 —— 2026-09-13 实测：测试因此静默睡满
    390 秒/次，看起来像"卡住"。
    """
    interval = MIN_INTERVAL if interval is None else interval
    gap = time.time() - float(st.get("last_call_ts") or 0.0)
    if gap < interval:
        nap = interval - gap
        print(f"[pace] 等 {nap:.0f}s（10 次/小时限速）", flush=True)
        time.sleep(nap)


def main() -> int:
    # 护栏放在 main() 里(而不是模块顶层): 直接运行仍默认拒绝，但**测试可以按文件路径导入**
    # 而不触发 SystemExit(3) —— 见 tests/test_report_rc_backfill.py / oneoff 卫生测试。
    # `_guard` 也按**文件路径**加载: 不许把 scripts/oneoff 插进 sys.path（那会让几十个冻结脚本
    # 对整轮测试裸名可见；tests/test_oneoff_hygiene.py::test_oneoff_dir_is_not_on_sys_path 会红）。
    import importlib.util
    _spec = importlib.util.spec_from_file_location(
        "_oneoff_guard", Path(__file__).resolve().parent / "_guard.py")
    _guard = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_guard)
    _guard.require_confirm(__file__, "写入新表 report_rc(分析师研报预测; 不碰既有表; 断点续跑)")

    import tushare as ts
    pro = ts.pro_api(cfg_mod.TUSHARE_TOKEN)
    months = [str(p) for p in pd.period_range(START_MONTH, pd.Timestamp.today(), freq="M")]
    st = _load_state()
    done = set(st.get("done") or [])
    todo = [m for m in months if m not in done]
    print(f"[start] 月份 {months[0]}~{months[-1]} 共 {len(months)}；已完成 {len(done)}；"
          f"待补 {len(todo)}", flush=True)
    conn = db.get_conn()
    try:
        for m in todo:
            off = int((st.get("next_offset") or {}).get(m, 0))
            while True:
                _wait(st)
                lo = (pd.Period(m, freq="M").start_time).strftime("%Y%m%d")
                hi = (pd.Period(m, freq="M").end_time).strftime("%Y%m%d")
                try:
                    df = pro.query("report_rc", start_date=lo, end_date=hi,
                                   offset=off, limit=PAGE)
                    st["last_call_ts"] = time.time()
                    _save_state(st)
                except Exception as e:
                    st["last_call_ts"] = time.time()
                    _save_state(st)
                    msg = str(e)[:160]
                    print(f"[err] {m} offset={off}: {msg}", flush=True)
                    if "频率" in msg or "限流" in msg:
                        time.sleep(min(600, MIN_INTERVAL))       # 撞限速: 退避后重试同分片
                        continue
                    break                                        # 其它错误: 跳过本月, 下次再补
                n = 0 if df is None else len(df)
                if n:
                    miss = [c for c in REPORT_RC_COLS if c not in df.columns]
                    if miss:
                        print(f"[err] {m} 缺列 {miss} —— 先修 REPORT_RC_COLS(上游变了)", flush=True)
                        break
                    got = db.upsert_df(conn, _sqlite_safe(df[REPORT_RC_COLS]), "report_rc")
                    print(f"[ok] {m} offset={off} 写入 {got} 行（累计本月 {off + n}）", flush=True)
                else:
                    # 整月为空在真实数据里很可疑（每月都有研报）⇒ 记进 `empty` 留痕，
                    # **不要静默当成功**。事后核对：`empty` 非空就把该月从 done/empty 里删掉重拉。
                    print(f"[warn] {m} offset={off} 空页 ⇒ 记入 empty 并标完成（可疑，事后核对）",
                          flush=True)
                    if off == 0:
                        st["empty"] = sorted(set(st.get("empty") or []) | {m})
                        _save_state(st)
                if n < PAGE:
                    done.add(m)
                    (st.get("next_offset") or {}).pop(m, None)
                    st["done"] = sorted(done)
                    _save_state(st)
                    break
                off += PAGE
                st.setdefault("next_offset", {})[m] = off
                _save_state(st)
    finally:
        conn.close()
    total = conn_total = None
    with db.conn_ctx() as c2:
        conn_total = c2.execute("SELECT COUNT(*) FROM report_rc").fetchone()[0]
    print(f"[done] 表 report_rc 现有 {conn_total} 行；完成月份 {len(st.get('done') or [])}",
          flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
