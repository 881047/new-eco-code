"""NEW ECO 主入口: .venv\\Scripts\\python -m uvicorn app.main:app --port <NEWECO_PORT>

端口口径见 `app/config.py::server_port()`：环境变量 `NEWECO_PORT` 优先，默认 **8787**
（`start.bat` / `scripts/service_watchdog.py` 读同一个变量；三处默认值由
`tests/test_port_single_source.py` 钉住一致）。
"""
import atexit
import faulthandler
import os
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.logger import setup_logger
from app import db
from app.web import api


_SERVICE_LOCK = None      # SCH-09(2026-09-16): 服务实例级单实例锁(见 lifespan)
_CRASH_FH = None          # 见 _arm_crash_traces

#: 崩溃留痕目录。为什么要有：2026-09-14 服务死在 13:15~13:25，`server.log`/`server.err.log`
#: **零**退出痕迹、Windows 事件日志也没有 WER 记录 ⇒ 只能记"死因未明"（14 号三补十一）。
_CRASH_DIR = Path(__file__).resolve().parent.parent / "data" / "crash"
_CRASH_KEEP = 20


def _is_service_process() -> bool:
    """本进程是不是**作为服务**在跑（而不是被 import / 被测试加载）。

    ## 为什么要判断（2026-09-21 修了一处让留痕机制失效的缺陷）

    `_arm_crash_traces()` 原来在**导入期无条件**装配 ⇒ 任何 import `app.main` 的短命进程
    （测试、CLI 脚本）都会在 `data/crash/` 留一份 `started/atexit`，实测一天 **20 份**噪声，
    而 `_CRASH_KEEP=20` 正好把它们全留着 ⇒ **真崩溃的文件会被挤掉/淹没**。
    这个目录存在的唯一理由就是 09-14 那次"死因未明"（`server.log` 零痕迹）；留痕目录被
    噪声占满，等于这个机制在最需要它的时候失效。

    ⇒ 只在命令行确实是 `python -m uvicorn app.main:app` 时装配 —— `start.bat:46` 与
    `scripts/service_watchdog.py:295` 两条启动路径都长这样，判据可靠。
    注意 `run_weekly.py`、`pytest` 这些进程 import 本模块**不会**装配（正是要的效果）。
    """
    return any("uvicorn" in str(a).lower() for a in sys.argv)


def _arm_crash_traces() -> None:
    """给"没有痕迹的退出"补两层留痕（**导入期**就装，比 lifespan 更早 —— 起服务时崩也要有）。

    · `faulthandler`：native 崩溃(SIGSEGV/SIGABRT…)时把**全部线程的 Python 栈**直接写进 fd，
      绕过 logging —— 进程正在死的时候这是唯一还能落盘的通道；
    · `atexit`：正常退出/未捕获异常退出时记一行时刻，于是"这份文件最后一行是什么"就能把
      「走到 atexit 的退出」与「没走到 atexit 的硬杀」分开。
    被 SIGKILL/断电杀掉时 atexit 不会跑 —— 那正是"文件存在但只有 started 行"这个**形状**在说话。
    每次启动一份新文件（按 mtime 只留最近 _CRASH_KEEP 份，不无限长）。
    """
    global _CRASH_FH
    try:
        _CRASH_DIR.mkdir(parents=True, exist_ok=True)
        path = _CRASH_DIR / f"service_{time.strftime('%Y%m%d_%H%M%S')}.log"
        _CRASH_FH = open(path, "a", encoding="utf-8")      # 故意不关：进程一生持有，崩溃时才要它
        # 命令行一并记下：事后能回答"这份留痕是哪个进程留的"（2026-09-21 加，
        # 起因正是同一天 data/crash/ 里出现 20 份来源不明的短命进程留痕）
        _CRASH_FH.write(f"started pid={os.getpid()} "
                        f"{time.strftime('%Y-%m-%d %H:%M:%S')} "
                        f"cmd={' '.join(str(a) for a in sys.argv)}\n")
        _CRASH_FH.flush()
        faulthandler.enable(file=_CRASH_FH, all_threads=True)
        atexit.register(_note_exit)
        for old in sorted(_CRASH_DIR.glob("service_*.log"), key=lambda p: p.stat().st_mtime,
                          reverse=True)[_CRASH_KEEP:]:
            old.unlink(missing_ok=True)
    except Exception:                                           # 留痕绝不许挡住启动
        pass


def _note_exit() -> None:
    try:
        exc = sys.exc_info()[0]
        _CRASH_FH.write(f"atexit {time.strftime('%Y-%m-%d %H:%M:%S')}"
                        + (f" 未捕获异常类型={exc.__name__}" if exc else "") + "\n")
        _CRASH_FH.flush()
    except Exception:
        pass


# 2026-09-21: 只在**作为服务**运行时装配 —— 短命进程(测试/CLI 脚本)不再污染留痕目录,
# 否则真崩溃的痕迹会被噪声挤掉(详见 `_is_service_process` 的说明)。
if _is_service_process():
    _arm_crash_traces()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _SERVICE_LOCK
    setup_logger()
    # 2026-09-16 调度审查 SCH-09: 单实例锁 —— 看门狗"判端口空闲→Popen"的 TOCTOU 窗口、
    # 双击 start.bat、watchdog 与 bat 同时拉起, 都可能产生两个 uvicorn(两套调度器抢同一份
    # 账本)。第二个实例在此立即失败退出: 探活方(看门狗宽限重探/start.bat 探测循环)会探到
    # **第一个**实例并判成功, 行为正确。服务崩溃残留的锁由 FileLock 的 pid+创建滴数陈旧
    # 判据自愈(下个实例拿得到)。
    from app.filelock import FileLock
    _SERVICE_LOCK = FileLock(Path(__file__).resolve().parent.parent / "data" / "service.lock")
    if not _SERVICE_LOCK.acquire(retries=2):
        raise RuntimeError(
            "已有 NEW ECO 服务实例在运行(data/service.lock 被持有) —— 本实例退出。"
            "若确认没有别的实例, 删除 data/service.lock 后重试。")
    db.init_db()
    from app.scheduler import start_scheduler, shutdown_scheduler
    start_scheduler()
    yield
    shutdown_scheduler()  # 退出时关闭调度器 (2026-09-04 审查 P3-6)
    if _SERVICE_LOCK is not None:
        _SERVICE_LOCK.release()


app = FastAPI(title="NEW ECO", lifespan=lifespan)


_ALLOWED_ORIGIN_HOSTS = ("127.0.0.1", "localhost")
_SEC_HEADERS = {
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    # 2026-09-13: 本地单用户看板**禁用强缓存**。起因: 用真浏览器渲染验证时发现
    # 改了 app.js 但页面仍执行旧代码(静态资源被启发式缓存) —— 那会让"卡片已接线"
    # 这类结论在**实际使用中**变成假的。重验证成本在本机可忽略(几 KB 文件 + 304)。
    "Cache-Control": "no-cache",
    # 本地单页应用: 脚本/资源只允许本自身来源(阻断 iframe 嵌套/点击劫持/XSS 注入脚本);
    # style 放开内联(页面有 <style> 块与 style 属性, 样式无脚本执行能力, 2026-09-04 修复)
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; frame-ancestors 'none'"),
}


def _origin_host(origin: str):
    """从 Origin/Referer 提取 host(小写, 去端口), 非法格式返回 None"""
    try:
        return origin.split("://", 1)[1].split("/", 1)[0].split(":")[0].lower()
    except (IndexError, AttributeError):
        return None


@app.middleware("http")
async def local_guard(request: Request, call_next):
    """本机访问防护(2026-09-04 审查 P1-8):
    - Host 白名单: 阻断 DNS rebinding
    - 写请求(POST 等): Origin/Referer 必须同源; 无 Origin 的非浏览器客户端(脚本/curl)
      必须带 X-Neweco-Client 头 —— 恶意网页发起的跨站"简单请求"会被 Origin 校验拦下
    - 全部响应附加安全头: 防 iframe 嵌套(点击劫持)与内容嗅探
    """
    host = (request.headers.get("host") or "").split(":")[0].lower()
    if host not in _ALLOWED_ORIGIN_HOSTS:
        # 2026-09-05 审查修复: 403 早退响应也要带安全头(原只有放行路径附加)
        resp = JSONResponse(status_code=403, content={"detail": "forbidden"})
        resp.headers.update(_SEC_HEADERS)
        return resp
    if request.method in ("POST", "PUT", "DELETE", "PATCH"):
        origin = request.headers.get("origin")
        if origin is not None:
            if _origin_host(origin) not in _ALLOWED_ORIGIN_HOSTS:
                resp = JSONResponse(status_code=403, content={"detail": "cross-origin forbidden"})
                resp.headers.update(_SEC_HEADERS)
                return resp
        else:
            referer = request.headers.get("referer")
            ok = referer is not None and _origin_host(referer) in _ALLOWED_ORIGIN_HOSTS
            if not ok and request.headers.get("x-neweco-client") != "web":
                resp = JSONResponse(status_code=403,
                                    content={"detail": "missing X-Neweco-Client header"})
                resp.headers.update(_SEC_HEADERS)
                return resp
    response = await call_next(request)
    response.headers.update(_SEC_HEADERS)
    return response


app.include_router(api.router)
static_dir = Path(__file__).resolve().parent / "web" / "static"
app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")
