# 生产库表清单索引（`data/neweco.db`）

> **本文件由 `scripts/gen_db_table_index.py` 生成，不要手改**；改完代码请重跑生成器。
> 生成于 2026-09-24（「一轮结清」轮）。用途：**防误删** —— 见文件头「为什么需要它」。

## 一、总览

| 项 | 值 |
|---|---|
| 表总数 | **194** |
| 生产/系统表 | **56** |
| 实验/派生表 | **137** |
| SQLite 内部表 | **1** |
| 找不到生成脚本（**最该保护**） | **14** |

> ⚠️ **本索引的用途是「看清楚」，不是「删」**。2026-09-24 审查曾把 65 张表判为「零引用孤儿」，后经 f-string 模板重扫**推翻**（它们全被生成脚本引用）。任何删除都必须先按本索引逐表核对「生成脚本 + 结论产物」两项。

## 二、生产/系统表（56 张）

| 表 | 行数 | 被谁引用 | 备注 |
|---|---:|---|---|
| `factor` | 22,113,655 | `app/db.py` | |
| `adj_factor` | 13,407,875 | `app/config.py` | |
| `daily` | 12,849,141 | `app/db.py` | |
| `daily_basic` | 12,771,690 | `app/db.py` | |
| `moneyflow` | 12,565,911 | `app/db.py` | |
| `stk_limit` | 10,637,395 | `app/config.py` | |
| `margin` | 6,753,057 | `app/db.py` | |
| `hk_hold` | 5,601,815 | `app/db.py` | |
| `fund_adj` | 3,130,628 | `app/db.py` | |
| `fund_daily` | 2,925,107 | `app/db.py` | |
| `top10_holder` | 1,261,663 | `app/db.py` | |
| `kg_edge_event` | 594,586 | `app/kg_story.py` | |
| `block_trade` | 352,078 | `app/db.py` | |
| `fina_indicator` | 264,621 | `app/db.py` | |
| `index_weight` | 252,100 | `app/db.py` | |
| `holder_num` | 190,622 | `app/db.py` | |
| `insider_trade` | 171,344 | `app/db.py` | |
| `daily_score` | 168,726 | `app/db.py` | |
| `lhb` | 167,211 | `app/config.py` | |
| `forecast` | 121,110 | `app/db.py` | |
| `research_report_em` | 105,304 | `app/db.py` | |
| `lhb_org` | 75,155 | `app/db.py` | |
| `restricted_release` | 25,620 | `app/db.py` | |
| `fund_rank` | 20,251 | `app/db.py` | |
| `lhb_ths` | 20,241 | `app/db.py` | |
| `index_daily` | 20,009 | `app/db.py` | |
| `fund_basic` | 17,924 | `app/db.py` | |
| `lhb_ths_hm` | 16,539 | `app/db.py` | |
| `broker_recommend` | 16,162 | `app/db.py` | |
| `kg_edge_relate` | 15,910 | `app/kg_story.py` | |
| `sw_member` | 15,910 | `app/db.py` | |
| `namechange` | 14,217 | `app/db.py` | |
| `stock_basic` | 5,908 | `app/db.py` | |
| `seo_issue` | 5,834 | `app/db.py` | |
| `repurchase` | 5,508 | `app/db.py` | |
| `trade_cal` | 5,478 | `app/db.py` | |
| `pool_index` | 3,335 | `app/config.py` | |
| `regime` | 3,086 | `app/config.py` | |
| `valuation_pe` | 3,014 | `app/db.py` | |
| `repo_rate` | 763 | `app/db.py` | |
| `pipeline_run` | 285 | `app/db.py` | |
| `alpha_bank` | 206 | `app/framework.py` | |
| `sw_classify` | 165 | `app/db.py` | |
| `model_meta` | 153 | `app/db.py` | |
| `data_status` | 111 | `app/db.py` | |
| `signal` | 107 | `app/db.py` | |
| `notify_log` | 57 | `app/db.py` | |
| `plan` | 45 | `app/config.py` | |
| `paper_nav` | 9 | `app/db.py` | |
| `paper_interest` | 8 | `app/db.py` | |
| `paper_order` | 5 | `app/db.py` | |
| `paper_position` | 5 | `app/db.py` | |
| `paper_trade` | 5 | `app/db.py` | |
| `paper_account` | 1 | `app/db.py` | |
| `fund_nav` | 0 | `app/db.py` | |
| `report_rc` | 0 | `app/db.py` | |

## 三、实验/派生表（137 张）

按**家族**分组（同一脚本的多个臂/种子）。`引用` 列里的 `*` 表示**靠 f-string 模板匹配到**（字面名扫不到，这正是「孤儿」假结论的来源）。

| 表 | 行数 | 生成脚本 | 匹配方式 |
|---|---:|---|---|
| `model_score_weekly_chk` | 3,545,598 | `scripts/oneoff/weekly_freq_baseline.py` | 代码·字面名 |
| `model_score_oldm` | 485,163 | `**⚠️ 未找到生成脚本**` | — |
| `model_score_ab0` | 444,380 | `**⚠️ 未找到生成脚本**` | — |
| `model_score_ab1` | 444,380 | `**⚠️ 未找到生成脚本**` | — |
| `model_score_ab2` | 444,380 | `**⚠️ 未找到生成脚本**` | — |
| `model_score_die_h10` | 444,380 | `scripts/oneoff/exp_die_stage_b.py` | 代码·字面名 |
| `model_score_die_h5` | 444,380 | `app/models/expert.py` | 代码·f-string 模板 |
| `model_score_die_head_cls` | 444,380 | `scripts/oneoff/diag_head_paired.py` | 代码·字面名 |
| `model_score_die_head_vt` | 444,380 | `scripts/oneoff/diag_head_paired.py` | 代码·字面名 |
| `model_score_fused_r0` | 444,380 | `scripts/oneoff/exp_fusion_router.py` | 代码·f-string 模板 |
| `model_score_fused_r1` | 444,380 | `scripts/oneoff/exp_fusion_router.py` | 代码·f-string 模板 |
| `model_score_fused_r2` | 444,380 | `scripts/oneoff/exp_fusion_router.py` | 代码·f-string 模板 |
| `model_score_restore_check` | 444,380 | `scripts/oneoff/restore_ens_archive.py` | 代码·字面名 |
| `model_score` | 444,312 | `app/config.py` | 代码·字面名 |
| `model_score_ab_g0_a` | 444,312 | `experiments/05_label/F77_group_ablation.py` | 代码·字面名 |
| `model_score_ab_g0_b` | 444,312 | `experiments/05_label/F77_group_ablation.py` | 代码·f-string 模板 |
| `model_score_ab_g1_a` | 444,312 | `experiments/05_label/F77_group_ablation.py` | 代码·f-string 模板 |
| `model_score_ab_g1_b` | 444,312 | `experiments/05_label/F77_group_ablation.py` | 代码·f-string 模板 |
| `model_score_ab_g2_a` | 444,312 | `experiments/05_label/F77_group_ablation.py` | 代码·f-string 模板 |
| `model_score_ab_g2_b` | 444,312 | `experiments/05_label/F77_group_ablation.py` | 代码·f-string 模板 |
| `model_score_ab_g3_a` | 444,312 | `experiments/05_label/F77_group_ablation.py` | 代码·f-string 模板 |
| `model_score_ab_g3_b` | 444,312 | `experiments/05_label/F77_group_ablation.py` | 代码·f-string 模板 |
| `model_score_ab_loo_amount_log` | 444,312 | `experiments/05_label/F77_group_ablation.py` | 代码·f-string 模板 |
| `model_score_ab_loo_bias_20` | 444,312 | `experiments/05_label/F77_group_ablation.py` | 代码·f-string 模板 |
| `model_score_ab_loo_lhb_cnt20` | 444,312 | `experiments/05_label/F77_group_ablation.py` | 代码·f-string 模板 |
| `model_score_ab_loo_size` | 444,312 | `experiments/05_label/F77_group_ablation.py` | 代码·f-string 模板 |
| `model_score_ab_loo_turnover_20` | 444,312 | `experiments/05_label/F77_group_ablation.py` | 代码·f-string 模板 |
| `model_score_ab_pathprobe` | 444,312 | `experiments/05_label/F77_group_ablation.py` | 代码·字面名 |
| `model_score_ab_uw_a` | 444,312 | `experiments/05_label/F76b_uniqueness_ab.py` | 代码·字面名 |
| `model_score_ab_uw_b` | 444,312 | `experiments/05_label/F76b_uniqueness_ab.py` | 代码·字面名 |
| `model_score_ab_uw_baseb` | 444,312 | `experiments/05_label/F76b_uniqueness_ab.py` | 代码·字面名 |
| `model_score_cbase_s1` | 444,312 | `experiments/05_label/C_block_fusion.py` | 代码·f-string 模板 |
| `model_score_cbase_s2` | 444,312 | `experiments/05_label/C_block_fusion.py` | 代码·f-string 模板 |
| `model_score_cblk_b1_s1` | 444,312 | `experiments/05_label/C_block_fusion.py` | 代码·f-string 模板 |
| `model_score_cblk_b1_s2` | 444,312 | `experiments/05_label/C_block_fusion.py` | 代码·f-string 模板 |
| `model_score_cblk_b5_s1` | 444,312 | `experiments/05_label/C_block_fusion.py` | 代码·f-string 模板 |
| `model_score_cblk_b5_s2` | 444,312 | `experiments/05_label/C_block_fusion.py` | 代码·f-string 模板 |
| `model_score_f78_a1` | 444,312 | `experiments/05_label/F78_seed_variance_calibration.py` | 代码·f-string 模板 |
| `model_score_f78_a10` | 444,312 | `experiments/05_label/F78_seed_variance_calibration.py` | 代码·f-string 模板 |
| `model_score_f78_a11` | 444,312 | `experiments/05_label/F78_seed_variance_calibration.py` | 代码·f-string 模板 |
| `model_score_f78_a12` | 444,312 | `experiments/05_label/F78_seed_variance_calibration.py` | 代码·f-string 模板 |
| `model_score_f78_a2` | 444,312 | `experiments/05_label/F78_seed_variance_calibration.py` | 代码·f-string 模板 |
| `model_score_f78_a3` | 444,312 | `experiments/05_label/F78_seed_variance_calibration.py` | 代码·f-string 模板 |
| `model_score_f78_a4` | 444,312 | `experiments/05_label/F78_seed_variance_calibration.py` | 代码·f-string 模板 |
| `model_score_f78_a5` | 444,312 | `experiments/05_label/F78_seed_variance_calibration.py` | 代码·f-string 模板 |
| `model_score_f78_a6` | 444,312 | `experiments/05_label/F78_seed_variance_calibration.py` | 代码·f-string 模板 |
| `model_score_f78_a7` | 444,312 | `experiments/05_label/F78_seed_variance_calibration.py` | 代码·f-string 模板 |
| `model_score_f78_a8` | 444,312 | `experiments/05_label/F78_seed_variance_calibration.py` | 代码·f-string 模板 |
| `model_score_f78_a9` | 444,312 | `experiments/05_label/F78_seed_variance_calibration.py` | 代码·f-string 模板 |
| `model_score_f78_b1` | 444,312 | `experiments/05_label/F78_seed_variance_calibration.py` | 代码·f-string 模板 |
| `model_score_f78_b2` | 444,312 | `experiments/05_label/F78_seed_variance_calibration.py` | 代码·f-string 模板 |
| `model_score_f78_b3` | 444,312 | `experiments/05_label/F78_seed_variance_calibration.py` | 代码·f-string 模板 |
| `model_score_f78_p1` | 444,312 | `experiments/05_label/diag_score_divergence_2021.py` | 代码·字面名 |
| `model_score_f78_p2` | 444,312 | `experiments/05_label/F78_seed_variance_calibration.py` | 代码·f-string 模板 |
| `model_score_die_flow` | 444,239 | `scripts/oneoff/exp_die_flow.py` | 代码·字面名 |
| `model_score_cblk_b3_s1` | 444,200 | `experiments/05_label/C_block_fusion.py` | 代码·f-string 模板 |
| `model_score_cblk_b3_s2` | 444,200 | `experiments/05_label/C_block_fusion.py` | 代码·f-string 模板 |
| `model_score_die_pit` | 443,754 | `app/factors/factor_engine.py` | 代码·字面名 |
| `model_score_cblk_b4_s1` | 443,415 | `experiments/05_label/C_block_fusion.py` | 代码·f-string 模板 |
| `model_score_cblk_b4_s2` | 443,415 | `experiments/05_label/C_block_fusion.py` | 代码·f-string 模板 |
| `model_score_cblk_b6_s1` | 442,326 | `experiments/05_label/C_block_fusion.py` | 代码·f-string 模板 |
| `model_score_cblk_b6_s2` | 442,326 | `experiments/05_label/C_block_fusion.py` | 代码·f-string 模板 |
| `model_score_die_seq` | 441,308 | `app/models/seq_die.py` | 代码·字面名 |
| `model_score_cblk_b2_s1` | 439,982 | `experiments/05_label/C_block_fusion.py` | 代码·f-string 模板 |
| `model_score_cblk_b2_s2` | 439,982 | `experiments/05_label/C_block_fusion.py` | 代码·f-string 模板 |
| `model_score_die_snap` | 432,471 | `scripts/oneoff/diag_pit_arms.py` | 代码·字面名 |
| `backtest_nav_daily` | 383,761 | `app/db.py` | 代码·字面名 |
| `model_score_excl_theta8` | 311,215 | `**⚠️ 未找到生成脚本**` | — |
| `model_score_excl_theta7` | 311,204 | `**⚠️ 未找到生成脚本**` | — |
| `model_score_excl4_theta20` | 311,198 | `**⚠️ 未找到生成脚本**` | — |
| `model_score_excl_theta6` | 311,187 | `**⚠️ 未找到生成脚本**` | — |
| `model_score_excl_theta5` | 311,167 | `**⚠️ 未找到生成脚本**` | — |
| `model_score_excl4_theta15` | 311,160 | `**⚠️ 未找到生成脚本**` | — |
| `model_score_excl4_theta10` | 311,058 | `**⚠️ 未找到生成脚本**` | — |
| `model_score_star19` | 304,240 | `scripts/oneoff/ab3_ic.py` | 代码·字面名 |
| `model_score_stylen` | 304,240 | `**⚠️ 未找到生成脚本**` | — |
| `model_score_ens` | 299,844 | `app/config.py` | 代码·字面名 |
| `model_score_die_margin_net_share` | 222,270 | `scripts/oneoff/exp_stage_b_flow_die.py` | 代码·字面名 |
| `model_score_margincov_ctl` | 222,270 | `scripts/oneoff/exp_stage_b_flow_die.py` | 代码·字面名 |
| `model_score_die_d1replica` | 52,513 | `scripts/oneoff/diag_d1_diff.py` | 代码·字面名 |
| `backtest_nav` | 19,424 | `app/db.py` | 代码·字面名 |
| `model_score_die_d1replica_run1` | 13,145 | `app/models/expert.py` | 代码·f-string 模板 |
| `model_score_die_seq_smoke` | 8,790 | `scripts/oneoff/exp_seq_die.py` | 代码·字面名 |
| `model_importance` | 3,596 | `app/db.py` | 代码·字面名 |
| `model_importance_ab_g0_a` | 3,596 | `experiments/05_label/F77_group_ablation.py` | 代码·字面名 |
| `model_importance_ab_g0_b` | 3,596 | `experiments/05_label/F77_group_ablation.py` | 代码·f-string 模板 |
| `model_importance_ab_pathprobe` | 3,596 | `**⚠️ 未找到生成脚本**` | — |
| `model_importance_ab_uw_a` | 3,596 | `experiments/05_label/F76b_uniqueness_ab.py` | 代码·字面名 |
| `model_importance_ab_uw_b` | 3,596 | `experiments/05_label/F76b_uniqueness_ab.py` | 代码·字面名 |
| `model_importance_ab_uw_baseb` | 3,596 | `experiments/05_label/F76b_uniqueness_ab.py` | 代码·字面名 |
| `model_importance_cbase_s1` | 3,596 | `experiments/05_label/C_block_fusion.py` | 代码·f-string 模板 |
| `model_importance_cbase_s2` | 3,596 | `experiments/05_label/C_block_fusion.py` | 代码·f-string 模板 |
| `model_importance_f78_a1` | 3,596 | `experiments/05_label/F78_seed_variance_calibration.py` | 代码·f-string 模板 |
| `model_importance_f78_a10` | 3,596 | `experiments/05_label/F78_seed_variance_calibration.py` | 代码·f-string 模板 |
| `model_importance_f78_a11` | 3,596 | `experiments/05_label/F78_seed_variance_calibration.py` | 代码·f-string 模板 |
| `model_importance_f78_a12` | 3,596 | `experiments/05_label/F78_seed_variance_calibration.py` | 代码·f-string 模板 |
| `model_importance_f78_a2` | 3,596 | `experiments/05_label/F78_seed_variance_calibration.py` | 代码·f-string 模板 |
| `model_importance_f78_a3` | 3,596 | `experiments/05_label/F78_seed_variance_calibration.py` | 代码·f-string 模板 |
| `model_importance_f78_a4` | 3,596 | `experiments/05_label/F78_seed_variance_calibration.py` | 代码·f-string 模板 |
| `model_importance_f78_a5` | 3,596 | `experiments/05_label/F78_seed_variance_calibration.py` | 代码·f-string 模板 |
| `model_importance_f78_a6` | 3,596 | `experiments/05_label/F78_seed_variance_calibration.py` | 代码·f-string 模板 |
| `model_importance_f78_a7` | 3,596 | `experiments/05_label/F78_seed_variance_calibration.py` | 代码·f-string 模板 |
| `model_importance_f78_a8` | 3,596 | `experiments/05_label/F78_seed_variance_calibration.py` | 代码·f-string 模板 |
| `model_importance_f78_a9` | 3,596 | `experiments/05_label/F78_seed_variance_calibration.py` | 代码·f-string 模板 |
| `model_importance_f78_b1` | 3,596 | `experiments/05_label/F78_seed_variance_calibration.py` | 代码·f-string 模板 |
| `model_importance_f78_b2` | 3,596 | `experiments/05_label/F78_seed_variance_calibration.py` | 代码·f-string 模板 |
| `model_importance_f78_b3` | 3,596 | `experiments/05_label/F78_seed_variance_calibration.py` | 代码·f-string 模板 |
| `model_importance_f78_p1` | 3,596 | `experiments/05_label/F78_seed_variance_calibration.py` | 代码·f-string 模板 |
| `model_importance_f78_p2` | 3,596 | `experiments/05_label/F78_seed_variance_calibration.py` | 代码·f-string 模板 |
| `model_importance_restore_check` | 3,596 | `scripts/oneoff/restore_ens_archive.py` | 代码·字面名 |
| `model_importance_ab_loo_amount_log` | 3,480 | `experiments/05_label/F77_group_ablation.py` | 代码·f-string 模板 |
| `model_importance_ab_loo_bias_20` | 3,480 | `experiments/05_label/F77_group_ablation.py` | 代码·f-string 模板 |
| `model_importance_ab_loo_lhb_cnt20` | 3,480 | `experiments/05_label/F77_group_ablation.py` | 代码·f-string 模板 |
| `model_importance_ab_loo_size` | 3,480 | `experiments/05_label/F77_group_ablation.py` | 代码·f-string 模板 |
| `model_importance_ab_loo_turnover_20` | 3,480 | `experiments/05_label/F77_group_ablation.py` | 代码·f-string 模板 |
| `model_importance_ab_g3_a` | 3,248 | `experiments/05_label/F77_group_ablation.py` | 代码·f-string 模板 |
| `model_importance_ab_g3_b` | 3,248 | `experiments/05_label/F77_group_ablation.py` | 代码·f-string 模板 |
| `model_importance_ab_g2_a` | 2,900 | `experiments/05_label/F77_group_ablation.py` | 代码·f-string 模板 |
| `model_importance_ab_g2_b` | 2,900 | `experiments/05_label/F77_group_ablation.py` | 代码·f-string 模板 |
| `model_importance_star19` | 2,263 | `scripts/oneoff/migrate_start_2019.py` | 代码·字面名 |
| `model_importance_stylen` | 2,263 | `**⚠️ 未找到生成脚本**` | — |
| `model_importance_ab_g1_a` | 1,044 | `experiments/05_label/F77_group_ablation.py` | 代码·f-string 模板 |
| `model_importance_ab_g1_b` | 1,044 | `experiments/05_label/F77_group_ablation.py` | 代码·f-string 模板 |
| `model_importance_cblk_b5_s1` | 1,044 | `experiments/05_label/C_block_fusion.py` | 代码·f-string 模板 |
| `model_importance_cblk_b5_s2` | 1,044 | `experiments/05_label/C_block_fusion.py` | 代码·f-string 模板 |
| `model_importance_cblk_b2_s1` | 812 | `experiments/05_label/C_block_fusion.py` | 代码·f-string 模板 |
| `model_importance_cblk_b2_s2` | 812 | `experiments/05_label/C_block_fusion.py` | 代码·f-string 模板 |
| `model_importance_cblk_b3_s1` | 812 | `experiments/05_label/C_block_fusion.py` | 代码·f-string 模板 |
| `model_importance_cblk_b3_s2` | 812 | `experiments/05_label/C_block_fusion.py` | 代码·f-string 模板 |
| `model_score_excl_verify` | 479 | `scripts/verify_excluder_production.py` | 代码·字面名 |
| `model_importance_cblk_b1_s1` | 348 | `experiments/05_label/C_block_fusion.py` | 代码·f-string 模板 |
| `model_importance_cblk_b1_s2` | 348 | `experiments/05_label/C_block_fusion.py` | 代码·f-string 模板 |
| `model_importance_cblk_b6_s1` | 348 | `experiments/05_label/C_block_fusion.py` | 代码·f-string 模板 |
| `model_importance_cblk_b6_s2` | 348 | `experiments/05_label/C_block_fusion.py` | 代码·f-string 模板 |
| `model_importance_cblk_b4_s1` | 232 | `experiments/05_label/C_block_fusion.py` | 代码·f-string 模板 |
| `model_importance_cblk_b4_s2` | 232 | `experiments/05_label/C_block_fusion.py` | 代码·f-string 模板 |
| `backtest_summary` | 212 | `app/db.py` | 代码·字面名 |

## 四、⚠️ 找不到生成脚本的表（**不可再生，最该保护**）

| 表 | 行数 | 类型 |
|---|---:|---|
| `model_score_oldm` | 485,163 | 实验/派生 |
| `model_score_ab0` | 444,380 | 实验/派生 |
| `model_score_ab1` | 444,380 | 实验/派生 |
| `model_score_ab2` | 444,380 | 实验/派生 |
| `model_score_excl_theta8` | 311,215 | 实验/派生 |
| `model_score_excl_theta7` | 311,204 | 实验/派生 |
| `model_score_excl4_theta20` | 311,198 | 实验/派生 |
| `model_score_excl_theta6` | 311,187 | 实验/派生 |
| `model_score_excl_theta5` | 311,167 | 实验/派生 |
| `model_score_excl4_theta15` | 311,160 | 实验/派生 |
| `model_score_excl4_theta10` | 311,058 | 实验/派生 |
| `model_score_stylen` | 304,240 | 实验/派生 |
| `model_importance_ab_pathprobe` | 3,596 | 实验/派生 |
| `model_importance_stylen` | 2,263 | 实验/派生 |

⇒ 这些表**没有可追的生成脚本** ⇒ 删了就真没了。若确认无用，也请先**导出 JSON 归档 + 记 sha256**（同 `reset_paper.py` 的归档纪律），再谈删除。

⚠️ **这一节是「保守结果」，不等于「确定不可再生」** —— 本生成器**有意不采信泛化模板**（`model_score_{tag}` 之类，见 `_templates` 的注记：采信它会把**任意** `model_score_*` 都算成它的产物）。所以其中 `model_score_excl_theta*` / `model_score_excl4_theta*` 很可能**确实**由 `scripts/llm_crowding_excluder.py` / `scripts/negative_excluder_r4.py` 的 catch-all 模板生成（`tag = f"excl_theta{theta}"`），只是**机器证不出来**。⇒ 处置时按「先当不可再生处理」（更保守），人工确认后再下调。

## 五、纪律（引用本索引前先读）

1. **实验表不是垃圾**：它们的结论多已落 `experiments/*.json`（入库），但**逐行分数不可再生除非重跑训练**（每张 ~44 万行、数百秒）；
2. **`scripts/oneoff/` 是证据**：其中脚本的输出表不得删除、不许批量搬家；
3. **删除前必须**：① 按本索引核对「生成脚本 + 结论产物」；② 全量备份（`scripts/backup_db.py`）；③ 逐表行数 + 内容 sha256 留痕；④ 经用户确认。

