"""CI 测试入口(2026-09-12 审查 P1-08; 2026-09-14 外部审查 P0-1 收口): 跑全量测试,
失败/**跳过过多**/**隔离没生效**即非零退出。

为什么要专门的入口: `python -m unittest discover` 在依赖缺失时仍然返回 0 ——
只要被 skip 掉就"全绿"。上一轮 CI 缺 deap/cvxpy 却没人发现, 正是这种绿灯假象。
这里把「跳过数」变成显式指标, 超出额度直接失败, 逼着人去看是真跳过还是被静默阉割。

⚠️ 2026-09-14 收口（外部审查 §9.4 环 5）: 本入口原来是 `unittest discover` ⇒
**`tests/conftest.py` 不加载**（那是 pytest 专属机制），于是测试隔离**根本没建立**：
按文档在本机跑这个入口，跑一次就把 76KB 测试噪声写进生产 `logs/app.log`
（实测 22:26~22:28，探针 `NE-TEST-LOG-PROBE-8f3c1d` 落盘 2 次）。修法不是再抄一份隔离，
而是**让隔离只有一个实现**：`tests/_isolation.py`（pytest 的 conftest 也调它）。

额度怎么定的: 目前唯一的预期跳过源是 tests/test_seq_die.py 的 torch 分支(2 条)
(seq_die 已判否, torch 是 1~2GB 的 CPU 轮子, 刻意不入 requirements-lock.txt)。
超出这个额度 = 有别的东西被悄悄跳过了, 应该当成回归。

注意 deap/cvxpy 缺失时**不是跳过而是 ERROR**(test_factory 的 setUpClass 裸 import),
所以那种情形本来就会让 CI 变红 —— 这条守卫防的是"有人给 test 加了 try/skipTest 兜底,
从此它再也不跑"的滑坡。

本机运行: ./.venv/Scripts/python.exe scripts/ci_run_tests.py
"""
import io
import sys
import unittest
import unittest.runner as ur
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# 让 `import _isolation`（tests/ 不是包，conftest 也是这么导入的）与 app 包都可导入
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import _isolation                                             # noqa: E402

# 允许跳过的上限(见模块 docstring)。两条**已登记**的来源：
#   ① tests/test_seq_die.py 的 torch 分支 —— 2 条（torch 刻意不入锁）；
#   ② tests/test_market_cache.py::TestCacheRoundtrip —— **5 条**（2026-09-28 实点:
#      save_then_load/missing_nav_leg/missing_meta/shape_mismatch/only_two_entries_kept；
#      旧注释写 4 条是过期的），前提是 parquet 引擎真能把面板落盘。
#      该模块顶部用**真实往返**探测（不是 import）：2026-09-14 本机 venv 里 pyarrow 曾被
#      Windows Smart App Control 拦（CodeIntegrity 3033/3077）⇒ 整组跳过；2026-09-15 起
#      DLL 已放行、5 条真跑并通过。
# 预算 6 = 2 + 4(旧值), 现 2+5=7 > 6 ⇒ parquet 一旦再坏 CI 会**红** —— 这是有意的:
# 宁可红也不让"行情缓存悄悄不可写"被静默跳过（守卫本意）。改预算须用户拍板。
SKIP_BUDGET = 6


def main() -> int:
    # CI 里 stdout 是管道(非 tty), 而用例输出含 ⇒ / ✓ 这类字符 —— Windows 下按 cp936
    # 编码会崩在**收尾打印**段(昂贵部分已跑完才挂, 报错位置与真实原因毫无关系)。
    # 复用项目里现成的修法, 不另造轮子。
    from app.logger import enable_utf8_stdio
    enable_utf8_stdio()

    # ① 隔离必须**先于任何测试模块 import** 建立（test_local_guard 等在模块级 import app）
    try:
        _isolation.setup()
        _isolation.assert_active()
    except Exception as e:                                    # 隔离建不起来就别跑
        print("::error::测试隔离未生效, 拒绝开跑(否则会污染生产 app.log): %s" % e,
              file=sys.stderr)
        return 2
    print("测试隔离已生效: 日志 → %s" % _isolation._TMP_LOGS)

    loader = unittest.TestLoader()
    # 不传 top_level_dir: tests/ 没有 __init__.py, 与
    # `python -m unittest discover -s tests` 保持同一套导入语义
    suite = loader.discover(str(ROOT / "tests"))
    # 用 runner 自带的 stream 收集 verbose 输出(它要求 writeln, 不能直接塞 StringIO);
    # 跳过数走 res.skipped 这个结构化字段, 不解析文本 —— 另加 -v 输出便于定位
    buf = io.StringIO()
    runner = unittest.TextTestRunner(stream=ur._WritelnDecorator(buf), verbosity=2)
    res = runner.run(suite)
    sys.stdout.write(buf.getvalue())

    skipped = list(res.skipped)
    print("\n" + "=" * 72)
    print("CI 汇总: 运行 %d / 失败 %d / 错误 %d / 跳过 %d(额度 %d)"
          % (res.testsRun, len(res.failures), len(res.errors),
             len(skipped), SKIP_BUDGET))
    for t, reason in skipped:
        print("  - 跳过 %s: %s" % (t, reason))

    ok = res.wasSuccessful()
    if ok and len(skipped) > SKIP_BUDGET:
        print("::error::跳过 %d 个用例, 超出额度 %d —— 有依赖缺失或被静默阉割的测试"
              % (len(skipped), SKIP_BUDGET), file=sys.stderr)
        ok = False
    # ② 跑完再自证一次：测试期间 LOG_DIR 不许被谁改回生产 logs/
    try:
        _isolation.assert_active()
    except Exception as e:
        print("::error::测试结束后隔离检查失败: %s" % e, file=sys.stderr)
        ok = False
    _isolation.cleanup()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
