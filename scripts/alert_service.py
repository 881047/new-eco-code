"""看门狗告警出口（2026-09-13）—— 由 `service_watchdog.py` 以**子进程**方式调用。

为什么单开一个脚本、而不是让看门狗直接 import 邮件模块：

    `service_watchdog.py` 的定位是"最后一道防线"，它只依赖标准库 —— 服务挂了的时候，
    它要能在**任何东西都不正常**的环境里跑完探活与拉起。把 app 侧的重依赖（config/db/日志/
    邮件）搬进它，等于给最后一道防线加一堆它会跟着一起挂的东西。

所以分工是：探活/拉起留在看门狗（纯标准库）→ 真要发信时另起一个进程走**生产邮件实现**
（`app.notify.send_ops_alert`：同一套 SMTP 口径 + 落 `notify_log` 可查）。

用法（看门狗内部调用，也可手动演练）：

    D:\\Python313\\python.exe -X utf8 scripts\\alert_service.py "标题" "正文(可多行)"

退出码：0 已发出 / 1 没发出（未配置、发送失败都算）。
"""
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))


def main() -> int:
    if len(sys.argv) < 2:
        print("用法: alert_service.py <标题> [正文]", file=sys.stderr)
        return 1
    title = sys.argv[1]
    body = sys.argv[2] if len(sys.argv) > 2 else ""
    try:
        from app.notify import send_ops_alert
    except Exception as e:                       # 依赖坏了也不许把看门狗弄挂
        print(f"告警模块导入失败: {e}", file=sys.stderr)
        return 1
    return 0 if send_ops_alert(title, body) else 1


if __name__ == "__main__":
    sys.exit(main())
