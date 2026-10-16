"""测试会话的全局隔离（2026-09-13；实现已于 2026-09-14 搬到 `tests/_isolation.py`）

为什么需要它：`setup_logger()` 在 FastAPI lifespan 里被调用，而本项目有大量测试**跑真 app**
（`TestClient`、调度档、流水线），于是根 logger 被挂上 `logs/app.log` 的 FileHandler ——
**测试自己的异常会混进生产证据文件**：2026-09-13 19:05~19:08 那次审计 `--tests` 就把
"实时行情获取失败: boom / LightGBM 训练失败 / daily 表为空" 写进了真 app.log。

这是 07 号台账 #1（测试往生产日志写）的同类复发，而 app.log 是事故取证的第一手台账：
真事故与其噪声混在一起，等于取证成本被无限抬高。规则很简单 —— **测试的日志只许落在临时目录**。

⚠️ 2026-09-14 收口（外部审查 P0-1）：隔离原来**只有 pytest 这一条路径** ——
`scripts/ci_run_tests.py` 走 `unittest discover`，不加载 conftest ⇒ 那个入口跑一次就把
76KB 测试噪声写进生产 app.log。现在实现统一在 `tests/_isolation.py`，pytest 与 CI 入口
共用同一份语义；本文件只剩"什么时候调"的 pytest 钩子。
"""
from pathlib import Path

import _isolation

#: 兼容旧引用（有测试/脚本直接读 conftest.SENT_IN_TESTS）
SENT_IN_TESTS = _isolation.SENT_IN_TESTS

# 模块导入即改道：`app.config.LOG_DIR` 读环境变量，必须赶在任何测试模块 import app 包之前。
# ⚠️ conftest 每次跑测试都会被 pytest **重新导入**（模块名唯一）——行内注释说明为什么不能
# 只在 pytest_configure 里改道：早期 import（收集阶段就 import app 的测试）会先拿到生产路径。
_isolation.setup()


def pytest_configure(config):
    """兜底：万一有插件/更早的 import 已经把 app.config / app.logger 拉起来了，就地改道。"""
    try:
        _isolation.setup()
        _isolation.assert_active()
    except RuntimeError:
        raise
    except Exception:                                    # 环境缺依赖时不影响收集
        pass


def pytest_sessionfinish(session, exitstatus):
    """清理测试残渣：临时日志目录 + data/weekly 下的 `_test_*`。"""
    _isolation.cleanup()
