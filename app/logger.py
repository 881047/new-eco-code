import logging
import logging.handlers   # 子模块不会随 `import logging` 自动导入, 类定义要用到
import sys
import time

from app.config import LOG_DIR

LOG_MAX_BYTES = 5 * 1024 * 1024   # 单文件上限(P3-6 起就是 5MB)
LOG_BACKUP = 3

_initialized = False
_handler = None                   # 唯一文件 handler: 重复调用复用, 不叠加


class _SafeRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """轮转失败时**不丢记录**的兜底 handler(仅当 concurrent-log-handler 缺失时启用)。

    2026-09-12 审查 P2-13 复现的故障链(Windows 上必然发生, 因为生产服务常开 app.log):

      1. 另一个进程也开着 app.log ⇒ `os.rename` 拿不到 FILE_SHARE_DELETE ⇒ PermissionError;
      2. `RotatingFileHandler.doRollover` 已把 `self.stream` 关掉并置 None, 然后才抛错;
      3. 异常被 `emit` 的 `except` 吞成 `handleError`(stderr 一行 "--- Logging error ---"),
         **这条记录丢了**;
      4. 下一次 emit 重开文件、大小仍 >= maxBytes ⇒ `shouldRollover` 恒为真
         ⇒ 此后**每一条**记录都在 doRollover 里抛错丢掉。

    第 4 步是关键: 不是"偶尔丢一条", 是**该进程从此永久失去文件日志**, 而 app.log 是
    证据台账的一部分 —— 静默丢日志就是丢证据。故兜底策略: 轮转失败只告警 + 5 分钟退避,
    记录照常写入(日志文件继续长大, 好过记录消失)。
    """

    _rollover_retry_after = 0.0
    _last_warn = 0.0

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if self.stream is None:
                self.stream = self._open()
            if self.maxBytes > 0 and time.monotonic() >= self._rollover_retry_after:
                msg = "%s\n" % self.format(record)
                self.stream.seek(0, 2)          # 非 POSIX 兼容写法, 与上游一致
                if self.stream.tell() + len(msg) >= self.maxBytes:
                    try:
                        self.doRollover()
                    except Exception as exc:
                        # 退避期内不再尝试; doRollover 可能已把 stream 关掉, 重新打开续写
                        self._rollover_retry_after = time.monotonic() + 300
                        try:
                            if self.stream is not None:
                                self.stream.close()
                        except Exception:
                            pass
                        self.stream = self._open()
                        if time.monotonic() - self._last_warn > 300:
                            self._last_warn = time.monotonic()
                            logging.getLogger(__name__).error(
                                "日志轮转失败(%s): %s —— 已退避, 记录继续写入原文件",
                                self.baseFilename, exc)
            logging.FileHandler.emit(self, record)
        except Exception:
            self.handleError(record)


def _make_handler(path=None) -> logging.Handler:
    """选文件 handler: 优先并发安全版, 缺依赖则退回带兜底的本地版。

    `RotatingFileHandler` 在多进程下**没有**任何互斥: 生产服务与脚本会同时写 app.log,
    轮转时互相改同一个文件(见 `_SafeRotatingFileHandler` 的故障链)。concurrent-log-handler
    用锁文件把轮转串行化, 进程内的写入也不再交错。

    `path` 缺省 = `LOG_DIR/app.log`; 仅供测试指向临时文件。
    """
    kwargs = dict(maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP, encoding="utf-8")
    target = LOG_DIR / "app.log" if path is None else path
    try:
        from concurrent_log_handler import ConcurrentRotatingFileHandler
        return ConcurrentRotatingFileHandler(target, **kwargs)
    except ImportError:
        return _SafeRotatingFileHandler(target, **kwargs)


def handler_kind() -> str:
    """当前实际生效的文件 handler 名 —— 供体检/测试核对「并发安全那版真的用上了」"""
    return type(_handler).__name__ if _handler is not None else "未初始化"


def enable_utf8_stdio() -> None:
    """把被**重定向/管道**的 stdout/stderr 切到 UTF-8 —— 只对非 tty 生效。

    2026-09-12 实测: `python scripts/oneoff/xxx.py > log.txt`(没有 `-X utf8`)时,
    Windows 下 Python 按 locale(cp936)编码被重定向的输出, 脚本打印 `✓`/`⇒`/`═`
    直接 `UnicodeEncodeError: 'gbk' codec can't encode character` **崩在打印阶段** ——
    A/B 第 2 轮 6 个回测臂全跑完、结果都已落库, 却因为这一行崩掉, 判决没能打出来。
    这类崩法很阴: 昂贵的那半(回测)已经完成, 挂掉的是最便宜的收尾段落, 且报错位置
    (打印语句)与真实原因(编码)毫无字面关系。

    **只在非 tty 时动手**: 交互式控制台仍是 cp936, 强切 UTF-8 反而会让中文变乱码。
    生产服务(`start.bat:25`)本来就带 `-X utf8`, 这里是空操作。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            if not stream.isatty():
                stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def setup_logger(name: str = None) -> logging.Logger:
    """配置**根** logger: 所有模块用 getLogger(__name__) (2026-09-04 审查 P3-4),
    可单独按模块调级别/过滤(如 logging.getLogger('app.datasource').setLevel(logging.DEBUG));
    `name` 参数仅为向后兼容保留、**忽略**, 返回根 logger。
    2026-09-05 第二轮复查 D3: 移除 StreamHandler —— cmd 重定向链的代码页与 UTF-8 文件
    不一致会产生第二写入通道, 中文日志乱码; 日志统一只落 UTF-8 的 app.log

    同时保证 stdout/stderr 能打出非 ASCII(见 `enable_utf8_stdio`) —— 放在 `_initialized`
    守卫**之前**, 这样即使 logger 早已初始化过, 本函数仍会把 stdio 修好。

    2026-09-12 审查 P2-14: 原实现是 `logging.getLogger(name)` —— 一旦有人写
    `setup_logger("app.xxx")`, handler 就挂到了那个**子** logger 上、根 logger 依旧空着,
    而 `_initialized=True` 又让后续调用全部早退 ⇒ 全项目 `getLogger(__name__)` 的日志
    (都靠 propagate 上浮到根)**一条都不落盘**, 且没有任何报错。文档早就写着"返回根 logger",
    代码却没这么做。现在改成无条件配根, 并把已挂的 handler 记在 `_handler` 上:
    若它被人从根上摘掉(测试常干的事), 下次调用会重新挂回去, 不留静默失效。"""
    enable_utf8_stdio()
    global _initialized, _handler
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    if _initialized and _handler in root.handlers:
        return root
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    if _handler is None:
        # 轮转: 5MB×3 备份, 防止日志无限增长 (P3-6); 多进程安全见 `_make_handler`
        _handler = _make_handler()
        _handler.setFormatter(fmt)
    if _handler not in root.handlers:
        root.addHandler(_handler)
    _initialized = True
    return root
