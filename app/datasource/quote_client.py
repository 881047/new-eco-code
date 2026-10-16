"""腾讯行情批量接口(qt.gtimg.cn): 模拟盘盘中实时估值用, 仅展示、不落库

- 免费无鉴权、GBK 编码、`~` 分隔; 字段: 3=现价 4=昨收 5=今开 30=时间(YYYYMMDDHHMMSS) 31=涨跌 32=涨跌幅
- 仅 A 股交易时段(工作日 09:25-11:35 / 12:55-15:05, 覆盖午休冻结价展示与收盘缓冲)外呼;
  时段外一律 None → 调用方回退最近收盘价口径
- 节假日/停牌由行情时间戳兜底: 时间日期≠今天 或 价格≤0 的行丢弃
  (停牌股返回昨收+冻结时间戳+vol=0, 只靠价格判断拦不住, 必须校验时间戳日期)
- 进程内 TTL 缓存(成功 8s / 失败 30s), 失败↔恢复状态变化才记日志, 避免 15s 轮询刷屏

## 怎么"这次不要外呼"（2026-09-19 审查 A3）

`app/web/llm.py` 组装问答上下文时不想为 LM 外呼行情（省一次外网请求，且 LLM 不需要盘中价）。
原先它用 `unittest.mock.patch("…get_quotes", return_value={})` —— 那是**进程级**改模块属性：
uvicorn 线程池并发服务时，同一时刻前端轮询 `/api/paper` 会静默丢掉实时价；更糟的是
`mock.patch` 的 enter/exit 不抗并发，两次嵌套会让后进入者把**已打桩的**实现当成"原值"，
退出后桩**永久留在进程里**，直到重启都取不到实时价。
⇒ 现在用 `ContextVar`：`with suppress_live_quotes():` 只在**当前上下文**内生效，
其他线程/请求一律照常。这是"请求级开关"，不是"进程级替身"。
"""
import contextlib
import contextvars
import datetime
import logging
import math
import re
import threading
import time

import requests

log = logging.getLogger(__name__)

_NO_LIVE = contextvars.ContextVar("neweco_suppress_live_quotes", default=False)


@contextlib.contextmanager
def suppress_live_quotes():
    """本上下文内的 `get_quotes()` 一律不外呼、返回 `{}`（调用方按"无实时价"回退收盘口径）。

    ⚠️ 只影响当前 contextvar 上下文（≈ 当前请求线程），**不影响**其他并发请求。
    需要"这次不要实时价"的调用方请用它，不要用 `unittest.mock.patch` 改本模块。
    """
    tok = _NO_LIVE.set(True)
    try:
        yield
    finally:
        _NO_LIVE.reset(tok)

QUOTE_URL = "http://qt.gtimg.cn/q="
TIMEOUT = 5
TTL_OK = 8.0
TTL_FAIL = 30.0
_TZ = datetime.timezone(datetime.timedelta(hours=8))  # 中国无夏令时, 固定 UTC+8
_SESSIONS = ((9 * 60 + 25, 11 * 60 + 35), (12 * 60 + 55, 15 * 60 + 5))
_LINE_RE = re.compile(r'^v_([a-z]{2})(\d{6})="')
_SUFFIX2PFX = {"SH": "sh", "SZ": "sz", "BJ": "bj"}
_PFX2SUFFIX = {"sh": "SH", "sz": "SZ", "bj": "BJ"}

_cache = {}          # key=符号元组 -> (结果或 None, 写入时刻)
_lock = threading.Lock()
_live_state = [None]  # 上次外呼结果: None 未知 / True 成功 / False 失败


def _now() -> float:
    return time.monotonic()  # 独立封装, 便于测试注入假时间


def _in_session(now=None) -> bool:
    """now: 带时区 datetime(默认当前时刻); 交易日历不在此校验, 节假日由行情时间戳兜底"""
    now = now or datetime.datetime.now(_TZ)
    if now.weekday() >= 5:
        return False
    mins = now.hour * 60 + now.minute
    return any(lo <= mins <= hi for lo, hi in _SESSIONS)


def to_tencent_symbol(ts_code: str) -> str | None:
    """600000.SH → sh600000; 缺后缀/未知后缀返回 None(不请求, 由调用方回退收盘价)"""
    parts = str(ts_code).strip().split(".")
    if len(parts) != 2:
        return None
    pfx = _SUFFIX2PFX.get(parts[1].upper())
    return f"{pfx}{parts[0]}" if pfx else None


def _f(s) -> float | None:
    """安全转 float: 空串/非数字/NaN/inf → None (FastAPI allow_nan=False, NaN 会 500)"""
    try:
        v = float(s)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _parse_line(line: str) -> dict | None:
    """解析单行 v_sh600000="1~浦发银行~600000~现价~昨收~...~时间~涨跌~涨跌%~...";
    返回 {ts_code, price, prev_close, pct_chg, time, date}; 非法行/价格≤0/时间缺失 → None"""
    m = _LINE_RE.match(line)
    if not m:
        return None
    body = line[m.end():].strip()  # 正则已含 =" 前缀, 此处从字段 0 开始
    if body.endswith('";'):
        body = body[:-2]
    f = body.split("~")
    if len(f) < 33:
        return None
    price = _f(f[3]) if f[3] else None
    ts = f[30].strip()
    if not price or price <= 0:  # 停牌/无效价
        return None
    if len(ts) != 14 or not ts.isdigit():
        return None
    op = _f(f[5]) if f[5] else None
    if op is not None and op <= 0:  # 集合竞价前今开为 0 → 视为无今开
        op = None
    return {
        "ts_code": f"{m.group(2)}.{_PFX2SUFFIX[m.group(1)]}",
        "price": price,
        "prev_close": _f(f[4]) if f[4] else None,
        "open": op,  # 今开(集合竞价定盘价), 开盘档撮合用
        "pct_chg": _f(f[32]) if f[32] else None,
        "time": ts,
        "date": ts[:8],
    }


def fetch_quotes(codes: list[str]) -> dict | None:
    """一次性 HTTP 批量拉取(不做时段/缓存判断); 行情日期≠今天的行丢弃(节假日/长期停牌);
    部分无效返回部分结果, 全部无效返回 None(调用方按持仓回退)"""
    syms = sorted({s for c in codes if (s := to_tencent_symbol(c))})
    if not syms:
        return None
    resp = requests.get(QUOTE_URL + ",".join(syms), timeout=TIMEOUT,
                        headers={"User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()
    text = resp.content.decode("gbk", errors="ignore")  # 名称字段乱码无所谓, 只用数字字段
    today = datetime.datetime.now(_TZ).strftime("%Y%m%d")
    out = {}
    for line in text.splitlines():
        q = _parse_line(line)
        if q and q["date"] == today:
            out[q.pop("ts_code")] = q
    return out or None


def _note_result(ok: bool) -> None:
    """状态变化才记日志(失败日志已在 except 中带异常信息输出, 这里只记恢复)"""
    global _live_state
    prev = _live_state[0]
    _live_state[0] = ok
    if prev is ok:
        return
    if ok:
        log.info("实时行情恢复: 腾讯行情接口可用")


def get_quotes(codes: list[str]) -> dict | None:
    """带时段门+TTL 缓存的入口(api 层只调这个):
    - 时段外/无有效代码 → None(不做任何外呼)
    - 缓存命中(成功 8s / 失败 30s)直接返回
    - 外呼失败 → None 并进入失败缓存, 下次 30s 内不再外呼(5s 超时最多堵一个线程池线程)
    不做 single-flight: 本机单用户 15s 轮询, 并发重复请求最多多一次小 GET
    """
    if _NO_LIVE.get():
        return {}                     # 调用方显式要求"这次不外呼"(见 suppress_live_quotes)
    if not _in_session():
        return None
    syms = sorted({s for c in codes if (s := to_tencent_symbol(c))})
    if not syms:
        return None
    key = tuple(syms)
    t = _now()
    with _lock:
        hit = _cache.get(key)
        if hit and t - hit[1] < (TTL_OK if hit[0] is not None else TTL_FAIL):
            return hit[0]
        if len(_cache) > 8:  # 持仓集合基本不变, 防御性清空
            _cache.clear()
    try:
        out = fetch_quotes(codes)
    except Exception as e:
        out = None
        log.warning("实时行情获取失败, 模拟盘回退收盘价口径: %s", e)
    _note_result(out is not None)
    with _lock:
        _cache[key] = (out, _now())
    return out
