"""一次性脚本的**危险操作必须显式确认**（2026-09-13）

背景（独立审查 §4.3 + 本人精确复扫）：`scripts/oneoff/` 里真正会写**生产表**的脚本是：

    migrate_daily_score.py  DELETE/INSERT 生产 model_score/daily_score + UPDATE paper_position
    reset_paper.py          DELETE 模拟盘各表 + INSERT paper_account
    ab_style_guard.py       UPDATE 生产 regime.state（**平铺脚本：导入即执行**）
    fix_ann_dates.py        UPDATE 生产 fina_indicator.ann_date
    exp_die_flow.py         DELETE FROM {--table}（表名来自命令行）

⚠️ 我第一轮用"关键词命中"扫出"39 个破坏性脚本"，**那是假阳性** —— 大量命中来自注释里的
`config.yaml` 字样与"在内存里 monkeypatch `load_config`"（根本没写文件）。本测试用
**精确判据**：只认真正的写语句，且排除 `*_tmp` / 带后缀的隔离表。

守卫三条：① 这 5 个脚本必须调用 `_guard.require_confirm`；② `_guard` 默认拒绝、显式放行；
③ 新出现的"写生产表"脚本没护栏就失败（棘轮）。
"""
import importlib.util
import re
import subprocess
import sys
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
ONEOFF = BASE / "scripts" / "oneoff"

# 已知需要护栏的脚本（真·生产表写操作）→ 它动什么
DANGEROUS = {
    "migrate_daily_score.py": "model_score/daily_score/paper_position",
    "reset_paper.py": "模拟盘各表",
    "ab_style_guard.py": "regime.state",
    "fix_ann_dates.py": "fina_indicator.ann_date",
    "exp_die_flow.py": "命令行指定的打分表",
    # 2026-09-18 审查 Q-05 回填：UPDATE alpha_bank.expr/gate_json/n_tests + 门禁重跑 upsert
    # 默认 DRY-RUN，要 --write 且 ONEOFF_CONFIRM 放行；写前把受影响行归档并读回自检
    "fix_alpha_bank_rerun_20260918.py": "alpha_bank（回填被抹掉的 10 行读数/expr/分母）",
    # 不写库、但**会重写仓库源码文件**（同级的不可逆动作）—— 2026-09-13 由本轮改造工具带入
    "_conn_ctx_wrap.py": "重写 app/** 源码（连接管理写法）",
    # 2026-09-28 补登记（公告文本线交接）：materialize 写**生产 factor 表**
    # （DELETE text_n1m/novel3m 全部行再 INSERT）——自带 _guard.require_confirm，
    # 默认拒绝执行。⇒ 归"危险"。
    "text_factor_materialize_20260927.py": "生产 factor 表（text_n1m/novel3m materialize）",
}

# **已人工复核**的"会写库但只动自己那摊"的脚本：逐条看过 SQL，模式是
#   `DELETE FROM {} WHERE run_date=?`（清自己本次回测 run 的行，表名来自 backtest_* 元组）
#   或 `DELETE/DROP` 自己的 `*_tmp` / 带后缀（如 model_score_{SUFFIX}）表。
# 它们**不需要**护栏，但必须在这份清单里 —— 新脚本要么进 DANGEROUS 加护栏，要么进这里说明理由。
KNOWN_DB_WRITERS = {
    # 2026-09-29 补登记（B1 多头端加权 A/B）：训练体只写自建隔离表
    # model_score_hw*_*/model_importance_hw*_*（显式 DDL 同构建表 + DELETE 清表，
    # 同 nf/excl 先例机制），生产 model_score/model_importance/data/models/ens
    # 全程只读；E.run_backtest 只写 run_tag=20260929_abhw_* 回测留痕。⇒ "只动自己那摊"。
    "head_weight_ab_20260929.py",
    # 2026-09-29 补登记（E2E-DL A/B）：训练体只写自建隔离表 model_score_e2e_ts1_*（同
    # nf/hw 先例机制），生产表全程只读；E.run_backtest 只写 run_tag=20260929_abe2e_*
    # 回测留痕；prod_B 臂复用 B1 轮 model_score_hw_base_b（已登记）只读。⇒ "只动自己那摊"。
    "e2e_ab_20260929.py",
    # 2026-10-02 补登记（第 14 轮 lambdarank A/B，预注册 plans/lambdarank-prereg-20261002.md）：
    # 写库 = 自建隔离表 model_score_ltr_a/b（ensure_score_table + DELETE 清表后 upsert，
    # 同 nf/hw/e2e 先例机制），生产 model_score 全程只读；E.run_backtest 只写
    # run_tag=ltr_* 回测留痕。⇒ "只动自己那摊"。
    "lambdarank_ab_20261002.py",

    "ab_const_n.py", "ab_const_n_2x2.py", "ab_const_n_fees.py", "ab_equity_const.py",
    "ab_equity_weight.py", "analyze_model_attrib.py", "diag_alloc_reject.py", "diag_exposure.py",
    "exp_capital_real_fees.py", "exp_die_head.py", "exp_die_pit.py", "exp_die_weekly.py",
    "exp_fb_window.py", "exp_fee_floor.py", "exp_fusion_router.py", "exp_label_style.py",
    "exp_null_random.py", "exp_slot_recycle.py", "exp_stage_b_flow_die.py", "exp_start_2019.py",
    "exp_stoploss.py", "exp_weight_rate.py", "scan_const_weight.py", "smoke_022_trainer_inert.py",
    # 只重建自己的专用表 model_score_die_d1replica（D1 复刻验证用），不碰生产表
    "verify_d1_replica.py",
    # 2026-09-18 补登记（PIN-A）：`_db_writers()` 命中的是 `DROP TABLE IF EXISTS
    # fina_indicator_v2` —— 那是**副本库**里的自建表（`data/tmp/ab_fina_pk_<ts>.db`，
    # 由 `_export_fina_table` 单表导出），生产库全程只读（ATTACH 到临时库再 CREATE，
    # 写的是挂载文件不是主库）。`--write` 分支也已接 `_guard.require_confirm`，
    # 且它只是打印"落地按报告手工执行"、**不自动改生产表**。⇒ 归"只动自己那摊"。
    "ab_fina_pk_ann_date.py",
    # 2026-09-21 补登记（周频化 A/B）：这两个脚本只写**自己建的隔离表**
    # `model_score_weekly_chk`（`DELETE FROM {ST}` 清的是这张自建表 —— 每次重跑先清再导入，
    # 生产 `model_score` 全程只读）+ 回测产物（`run_tag` 标记，新 `run_date` 不覆盖现役读数）。
    # ⇒ 归"只动自己那摊"。
    "weekly_freq_baseline.py", "weekly_freq_hyst_scan.py",
    # 2026-09-27 补登记（新因子构型 A/B + 组合层准入判决器）：写库全部经被调模块
    # （trainer.run 写自建隔离表 nfab_*/nfimp_*；E.run_backtest 只写 run_tag 标记的
    # 回测留痕，新 run_date 不覆盖现役读数），生产 `model_score`/`model_importance`/
    # `data/models/ens` 全程只读。⇒ 归"只动自己那摊"。
    "fullretrain_nf_ab_20260926.py", "nf_ab_backtest_20260927.py",
    # 2026-09-27 补登记（池剔除 A/B）：训练体只写自建隔离表 excl_*/exclimp_*（同 nf 机制，
    # trainer.run 经被调模块写表），生产表全程只读；判决复用 portfolio_admission_gate.py
    # （已在册）。⇒ 归"只动自己那摊"。
    "excl_pool_train_20260927.py",
    # 2026-09-27 补登记（成本感知评分平滑 A/B）：写库 = 自建隔离表
    # slv_smooth30/slv_smooth50（CREATE+DELETE+append，同 nf/weekly 先例），生产
    # model_score 只读；回测 run_tag=slvsm_ 留痕不自清。⇒ 归"只动自己那摊"。
    "costaware_smooth_20260927.py",
    # 2026-09-27 补登记（公告文本因子线）：写库 = 自建隔离表 nfab2_*/nfimp2_*
    # （trainer.run 经被调模块写表）+ 回测 run_tag=nfab2_ 留痕；生产表全程只读。
    "text_factors_ab_20260927.py", "text_factors_dose_20260927.py",
    # 2026-09-27 补登记（26+2 文本因子增量 A/B）：写库 = 自建隔离表 txt2_trt_*/txt2imp_*
    # （trainer.run 经被调模块写表，同 excl_pool_train 先例），生产表全程只读。
    "txt2_excl_train_20260927.py",
    # 2026-09-28 补登记（拥挤度暴露约束 A/B）：写库 = 自建隔离表 crwd_cap
    # （CREATE+DELETE+append），生产 model_score 只读；回测 run_tag=crwd_ 留痕。
    "crowding_cap_20260928.py",
    # 2026-09-30 补登记（低谷加仓探针 A/B，L3 镜像假设）：写库 = 自建隔离表
    # trough_t1/trough_t2（CREATE+DELETE+append，同 crowding_cap 先例机制），
    # 生产 model_score 只读；回测 run_tag=tt_ 留痕。--calibrate-only/--diag 只读。
    "trough_tilt_20260930.py",
    # 2026-09-28 补登记（oi_spread 剔除器 A/B，异构门禁的分支转向）：写库 = 自建隔离表
    # model_score_oiex_w20/w60_t15/t20/t25（先清同表旧行再 append，表名由
    # expert.ensure_score_table 正则把守）；回测 run_tag=<tname>_prod/full 留痕不自清。
    # 生产 model_score / model_importance 全程只读。⇒ 归"只动自己那摊"。
    "ab_oi_excluder_20260928.py",
    "portfolio_admission_gate.py",
}


def _db_writers():
    """会写库的脚本（宽松匹配 `DELETE FROM x` / `UPDATE x SET`）。"""
    out = set()
    for p in ONEOFF.glob("*.py"):
        if p.name == "_guard.py":
            continue
        t = p.read_text(encoding="utf-8", errors="ignore")
        if re.search(r"DELETE\s+FROM|UPDATE\s+\S+\s+SET|DROP\s+TABLE", t, re.I):
            out.add(p.name)
    return out


def _spec(name):
    return importlib.util.spec_from_file_location("oneoff_" + name.replace(".py", ""),
                                                 ONEOFF / name)


class TestDangerousScriptsAreGuarded(unittest.TestCase):
    def test_known_dangerous_scripts_call_guard(self):
        for name in DANGEROUS:
            with self.subTest(script=name):
                txt = (ONEOFF / name).read_text(encoding="utf-8")
                self.assertIn("require_confirm", txt,
                              f"{name} 会写生产表却没有 require_confirm 护栏")

    def test_no_new_unguarded_writer(self):
        """棘轮：**每一个**会写库的脚本都必须在两张清单之一（危险=有护栏 / 已复核=只动自己）。"""
        unknown = _db_writers() - set(DANGEROUS) - KNOWN_DB_WRITERS
        self.assertEqual(unknown, set(),
                         f"这些脚本会写库但没被复核归类 —— 危险就加 require_confirm 并登记到 "
                         f"DANGEROUS，只动自己那摊就加进 KNOWN_DB_WRITERS 并写清理由: {sorted(unknown)}")


class TestGuardBehaviour(unittest.TestCase):
    def test_guard_blocks_by_default_and_allows_with_env(self):
        # 注意：用 `-c` 执行时没有 __file__，所以这里传名字而不是 __file__
        code = ("import sys; sys.path.insert(0, r'%s');"
                "from _guard import require_confirm; require_confirm('demo_script.py', '测试')"
                % ONEOFF)
        # Windows 上子进程默认按 cp936 输出中文 ⇒ 显式要求 UTF-8，父进程也按 UTF-8 解
        env = {"PATH": "", "SYSTEMROOT": "C:\\Windows", "PYTHONIOENCODING": "utf-8"}
        blocked = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                                 encoding="utf-8", errors="replace", env=env)
        self.assertEqual(blocked.returncode, 3, "默认必须拒绝执行")
        self.assertIn("拒绝执行", blocked.stderr)
        allowed = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                                 encoding="utf-8", errors="replace",
                                 env={**env, "ONEOFF_CONFIRM": "ALL"})
        self.assertEqual(allowed.returncode, 0, "显式确认后应放行")


if __name__ == "__main__":
    unittest.main()
