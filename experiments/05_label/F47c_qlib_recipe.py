"""F47c · 超参线**最后一个**实验：直接试**外部成熟实现（microsoft/qlib）的官方配方**

## 为什么这一格必须试（而不是继续自己扫）

F47 扫了"更小树/更强正则"那一侧（C1/C2/C3）⇒ 平台；F47b 试了"更大树"单点（leaves 127）⇒
全史持平、留出集改善但判据不过。两轮都在**我们自己猜的区间**里。

按用户要求"动手前先检索外部成熟做法"，实测拉取了 **microsoft/qlib**（本项目一直拿它当标尺）
Alpha158 的官方 LightGBM 配置：

    num_leaves 210~250 / max_depth 8 / learning_rate 0.2(或 0.1)
    lambda_l1 205.70 / **lambda_l2 580.98** / subsample 0.8789

—— 那是一个**与本项目相反的角**："大树 + 极强 L2 收缩"，而现役是"小树(31) + 近乎无正则(λ2=1)"。
来源（可复核）：
  https://raw.githubusercontent.com/microsoft/qlib/main/examples/benchmarks/LightGBM/
      workflow_config_lightgbm_Alpha158.yaml   （leaves 210 / lr 0.2）
      workflow_config_lightgbm_Alpha158_csi500.yaml（leaves 250 / lr 0.1）

## 本实验只测**一个**点（qlib 配方原样，不做参数微调）

    C5_qlib : num_leaves 210 / max_depth 8 / learning_rate 0.2 / lambda_l1 205.7 /
              lambda_l2 580.98 / feature_fraction 0.9 / bagging_fraction 0.8789 /
              bagging_freq 1 / rounds 50（轮数与现役一致 —— 只换模型形态，不同时换预算）

判据、对照、口径与 F47/F47b **逐字一致**（复用同一套评估代码）：两套种子同时
① 全史 ≥ BASE+1.0pp ② ACTIVE ≥ BASE ③ 全史 MDD ≥ BASE×1.10；④ 对照必须复现。

## 事前声明（写在看到任何数字之前）

**无论结果如何，超参这条线到此结清**：
  · 过 ⇒ 采纳 qlib 配方（另开"换 pin + 重算存档 + 更新方案"流程）；
  · 不过 ⇒ "在本项目的特征/标签/训练窗下，超参不是杠杆"成为**有证据的结论**，
    写进证伪登记，不再扩大网格（避免变成搜索机器）。

用法：
    ./.venv/Scripts/python.exe -X utf8 experiments/05_label/F47c_qlib_recipe.py
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

f47.GRID = {
    "C5_qlib": {"params": {"num_leaves": 210, "max_depth": 8, "learning_rate": 0.2,
                           "lambda_l1": 205.7, "lambda_l2": 580.98,
                           "feature_fraction": 0.9, "bagging_fraction": 0.8789,
                           "bagging_freq": 1},
                "rounds": 50},
}
f47.OUT = HERE / "F47c_qlib_recipe_result.json"

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        datefmt="%H:%M:%S", force=True)
    f47.log = logging.getLogger("f47c")
    f47.main()
