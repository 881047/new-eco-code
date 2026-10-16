"""周频袖套的类型化异常（2026-09-18 审查 Q-04）

**为什么要有这个文件**：自愈链原来靠**中文文案子串**识别「打分存档不足以服务该锚点」——
`pipeline.py` 里写 `archive_miss = ("不在打分存档", "没有周频打分存档")` 然后
`any(m in str(e) for m in archive_miss)`。生产者（`plan.py` / `scores.py`）只是内联
f-string，没有任何东西钉住那两个短语。于是任何人重构一句报错措辞（『存档』→『档案』、
或者顺手补个句号），`tests/` 全仓 **0 条测试会变红**，而后果是连锁的：

    ensure_plan 误判成"非存档原因" → 不补训 → `plan_<anchor>.json` 全周不生成
    → 袖套整周 0 成交 → 只有看门狗一封邮件

而生产 `config.yaml:115` 是 `weekly.enabled: true`。这条链 07 号 #50 修过一次，
2026-09-14 外部复核 D4 又把子串从宽松的『存档』收紧成两条 —— 都还停在**文案层**。
换成类型化通道才是把耦合断掉：措辞随便改，`isinstance` 不看字。

**为什么各自还继承原本的内建类型**：调用点已经有按内建类型抓的分支，改继承关系
会静默改变它们的走向 —— 那正是这里要修的同一类"静默失效"：

- `scores.latest()` 原抛 `FileNotFoundError`，而 `pipeline.py:104` 就写死
  `except FileNotFoundError` 把"一份存档都没有"当成"存档为空 ⇒ 去重训"。
  若只继承 `RuntimeError`，那一行会漏抓 ⇒ 冒到 :114 的 `except Exception`
  ⇒ 返回 `{"error": ...}` 且不补训 ⇒ **整周空仓**。这就是 Q-04 本身。
- `plan.build()` 原抛 `ValueError`，保留同理。

所以：新增类型只作**加法**，一个既有 `except` 的行为都不动。
"""


class ArchiveMiss(Exception):
    """打分存档不足以服务该锚点 ⇒ 自愈链应当补跑一次打分保鲜再重建方案。

    只表示这一件事，**别当通用"存档相关错误"用**：抛它 = 触发一次真重训（花钱、
    占时）。判据一律用 `isinstance(e, ArchiveMiss)`，不许再回去匹配 `str(e)`。
    """


class ArchiveAnchorNotCovered(ArchiveMiss, ValueError):
    """存档在，但**不含**请求的那个锚点。（`weekly/plan.py` 的生产者）"""


class ArchiveAbsent(ArchiveMiss, FileNotFoundError):
    """一份打分存档都没有。（`weekly/scores.py` 的生产者）"""
