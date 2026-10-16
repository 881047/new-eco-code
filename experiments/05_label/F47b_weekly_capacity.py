"""F47b · 周频超参的**反方向单点**：提高容量（补 F47 网格的单边缺口）—— 预注册

## 为什么要补这一刀（诚实说明我自己的网格缺陷）

F47 的预注册网格（C1/C2/C3）**只探了一个方向**：我当时的前提是"5 日标签噪声更大 ⇒
最优模型更小、更强正则"。结果：

| 配置 | 全史（SA/SB） | ACTIVE | MDD |
|---|---|---|---|
| BASE（现役） | +18.00 / +17.30pp | 10.38 / 11.80 | −36.9% |
| C1_small（更小更强正则） | +18.04 / +17.70pp | 10.71 / 10.87 | −37.1% |
| C2_mid（lr0.03/100 轮） | +16.81 / +16.92pp | **11.70 / 11.40** | −36.4% |
| C3_strong（最强正则） | +15.84 / +16.69pp | 10.24 / 10.38 | −36.4% |

⇒ **往"更强正则"方向不是杠杆**（现役已在该方向的平台上）。但"不更大"不等于"太大"：
**反方向（容量更大）从未测过**。要回答"模型是否已经训练到最优"，这一格必须补 ——
否则结论只能是"在更正则那一侧是平台"，而不是"在最优附近"。

## 本实验只测**一个**点（不扩展成搜索）

    C4_capacity : num_leaves 127（4×现役） / min_data_in_leaf 100（0.5×） / lr 0.05 / 50 轮
                  其余全部与现役一致 —— **只动容量**，不同时动学习率/轮数/采样（F47 的 C2
                  就是多因素同改，混杂了；这次单轴）

判据、对照、口径与 F47 **逐字一致**（本脚本直接复用 F47 的评估代码，只替换网格与产物路径）：
    ② 全史两套种子 ≥ BASE + 1.0pp；② ACTIVE ≥ BASE；③ MDD ≥ BASE×1.10；④ 对照必须复现。

## 结果怎么用（事前定）

- **过** ⇒ 容量是杠杆 ⇒ 另开 F48 在容量方向做两点细化（仍然先预注册）；
- **不过** ⇒ 结合 F47 的结论，"周频超参已在这套特征/标签附近达到平台"成为**有证据的结论**，
  超参这条线**结清**，不再扫（避免变成搜索机器）。

用法：
    ./.venv/Scripts/python.exe -X utf8 experiments/05_label/F47b_weekly_capacity.py
"""
import importlib.util
import logging
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
BASE = HERE.parent.parent
sys.path.insert(0, str(BASE))


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


f47 = _load("f47", HERE / "F47_weekly_hyperparams.py")

# 只替换网格与产物路径，其余(面板/训练/评估/判据/对照)全部复用 F47 —— 不写第二份
f47.GRID = {
    "C4_capacity": {"params": {"num_leaves": 127, "min_data_in_leaf": 100},
                    "rounds": 50},
}
f47.OUT = HERE / "F47b_weekly_capacity_result.json"

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        datefmt="%H:%M:%S", force=True)
    f47.log = logging.getLogger("f47b")
    f47.main()
