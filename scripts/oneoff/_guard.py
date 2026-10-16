"""一次性脚本护栏（2026-09-13）—— 防止"重跑一次就毁掉生产数据/基线"

## 为什么需要它（本项目的真实教训）

`scripts/oneoff/` 是历史实验与一次性修数据的堆积地。计划文档里早就写着
`migrate_start_2019.py` **重跑即毁基线**，但那只是**注释**——注释拦不住手滑。
精确扫描（2026-09-13，只认真正的库写语句、排除注释与内存 monkeypatch）确认：
**5 个脚本会写生产表**：

    migrate_daily_score.py  DELETE/INSERT 生产 model_score/daily_score + UPDATE paper_position
    reset_paper.py          DELETE 模拟盘各表 + INSERT paper_account（重置账户）
    ab_style_guard.py       UPDATE 生产 regime.state（**平铺脚本：导入即执行**）
    fix_ann_dates.py        UPDATE 生产 fina_indicator.ann_date
    exp_die_flow.py         DELETE FROM {--table 指定的表}（表名来自命令行）

（注：另有若干脚本只在自己带后缀的临时表上 DELETE/DROP，如 `model_score_{SUFFIX}`，
不属危险集；我第一轮用"关键词命中"扫出 39 个，那是**假阳性**，已纠正。）

## ⚠️ 2026-09-22：**同一类假阳性第三次出现** —— 记在这里免得再犯

当天的结项审计用"含写语句且无护栏"做关键词扫描，扫出 **26 个候选**，其中
`scan_const_weight.py:91-92` 与 `exp_start_2019.py:157-158` 被列为"**含 `DELETE FROM
backtest_summary/backtest_nav/backtest_nav_daily`（生产表名、无后缀）、却无护栏 ⇒
有误跑改生产回测表的风险**"。

**复核结论：两条都是假阳性，且这两个脚本早已在
`tests/test_oneoff_danger_guards.py::KNOWN_DB_WRITERS` 的已复核名单里。** 判据：

```python
TAGS = []                                  # 本脚本**自己**刚跑出来的 run_date
...
m = E.run_backtest(top_n=6, run_tag="sw_{:.3f}".format(w))
TAGS.append(m["run_date"])                 # ← 只装自己那次的 tag
...
for t in TAGS:                             # DELETE 被 WHERE 限死在自己的 run 上
    conn.execute("DELETE FROM {} WHERE run_date=?".format(tbl), (t,))
```

即：`DELETE` 确实写在**生产表名**上，但 `WHERE run_date IN (自己刚建的 tag)` ⇒
清的是自己本次回测的行，**碰不到任何别人的 run**（现行 pin 是 `20260920_trbench`，
与 `sw_0.500` 这类 tag 不可能撞）。这正是本模块开头说的"只动自己那摊"。

**教训（可复用的判据）**：判"危险"**不能只看表名**，必须看 **WHERE 子句的取值来源** ——
表名是生产的、但 `WHERE` 绑在自己刚生成的 tag 上，就不构成破坏生产数据的路径。
下次再扫，请先看 `WHERE`，别只 grep `DELETE`。
（本轮 26 个候选的其余 24 个多为自带后缀临时表，同属此类。）

## 用法

    from _guard import require_confirm
    require_confirm(__file__, "会 DELETE 生产 model_score 与 daily_score")

默认**拒绝执行**（`SystemExit(3)`），除非显式给出确认：

    set ONEOFF_CONFIRM=migrate_daily_score.py   （或 =ALL 表示明确知道自己在做什么）

设计原则：**默认拒绝、显式放行、把"要动什么"写在报错信息里**。它不替代备份，
只保证"没人会在不知情的情况下按一下回车就动生产数据"。

⚠️ **2026-09-14（外部复核 O10）**：原来还接受了 `ONEOFF_CONFIRM=1` —— 一个**没写进任何文档**
的全放行口令，而它恰好是运维凭直觉会敲的值（"1 = 是"）。这等于给护栏开了个后门：
`set ONEOFF_CONFIRM=1` 会同时解锁 5 个会**毁基线/清空模拟盘**的脚本。已移除：
只认**脚本名**或 `ALL`（两者都要求打字时把"我要动什么"想清楚）。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ENV = "ONEOFF_CONFIRM"


def require_confirm(script, reason: str, env: str = ENV) -> None:
    """危险脚本的显式闸门。`script` 传 `__file__`。"""
    name = Path(str(script)).name
    token = (os.environ.get(env) or "").strip()
    if token in (name, "ALL"):
        print(f"[oneoff 护栏] 已显式确认: {name} —— 将要: {reason}")
        return
    msg = (
        f"\n[oneoff 护栏] 拒绝执行 {name}\n"
        f"  它会: {reason}\n"
        f"  这是不可逆的生产写操作。确认要跑就显式放行:\n"
        f"      set {env}={name}        (PowerShell: $env:{env}=\"{name}\")\n"
        f"  跑之前请先备份: D:\\Python313\\python.exe -X utf8 scripts\\backup_db.py\n")
    print(msg, file=sys.stderr)
    raise SystemExit(3)
