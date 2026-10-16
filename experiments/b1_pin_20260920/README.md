# B1/B2 基准尺与 alpha 面板证据产物（被跟踪副本，2026-09-20）

**为什么在 git 里**：`logs/` 被 `.gitignore:56` 整个忽略 ⇒ 下面这六份产物此前**不在版本控制内**，
而其中 `ab_bench_tr_real_index.json` 是现行 pin `20260920_trbench` 那个"抬高量 2.22pp/年"的
**唯一**支撑，`b1_pin_evidence_{before,after}.json` 是换 pin 三道门（P-a/P-b/P-c）的唯一留证。
本目录是它们的**副本**（源文件仍在 `logs/` 原地未动）；正本 = `logs/`，本目录 = 可提交的那一份。

- 生成/搬运：`scripts/oneoff/track_b1_evidence.py`（不读库、不改数值）
- 复算与校验：`scripts/oneoff/track_b1_evidence.py --verify`（不一致 ⇒ 退出码 1）
- **哈希与逐份说明的单一真源 = 本目录 `MANIFEST.json`**（含每份的 `cited_in` 台账位置与 `what` 用途）

## 哈希口径（写死一次，别再手抄）

```
canonical_sha16 = sha256(json.dumps(json.load(f), indent=2, ensure_ascii=False, default=str))[:16]
```

先解析再重序列化 ⇒ 与盘上 CRLF / BOM / 缩进漂移无关。`MANIFEST.json` 里另存 `bytes_sha16`
供人核对原文件字节。**为什么不沿用"文件字节 sha256"**：`.gitattributes:2 * text=auto`
让 Windows 检出成 CRLF，于是"提交时的字节"与"盘上字节"两种合法读法会给出不同值 ——
14 号 1488 行那个怎么都复现不出的 `66bf3049a59b8704` 就是这么混出来的（见下一节）。

## 顺带查清的一条：批次 3 Q5 产物的哈希为什么对不上

| 产物 | 台账/README 记的 | 用上面的口径重算 | 判定 |
|---|---|---|---|
| `audit_b_stage_first_step.json` | `354cae920b94ddf6` | `354cae920b94ddf6` | ✓ 精确复现 |
| `alpha_panel_scale_gate.json` | `af2d2e12e1eac04e` | `af2d2e12e1eac04e` | ✓ 精确复现 |
| `per_year_52_sensitivity.json` | `66bf3049a59b8704` | `8aeba915161c19cf` | ✗ 复现不出 |

⇒ **不是产物被篡改，也不是哈希算法不同**：三份里两份用同一口径逐字复现，说明口径本来就是这个。
对不上的那份，14 号 1503-1505 行自己记着原因 —— Q5 的判据逻辑被作者修过两次
（① 把"相对 %" 与"pp 距离"直接比 ⇒ 假的 `crossed=True`；② 把"本来就在线下方"的负余量算成翻档风险），
**重跑覆盖了同名文件，但台账与 `scripts/oneoff/README.md` 里的 sha 没跟着重记**。
⇒ 现行值以本表为准：**`8aeba915161c19cf`**（14 号 1488 行已同步更正）。

**这一类缺陷的形状**（写给自己也写给下一轮）：产物**可覆盖** + 哈希**手抄进文档** ⇒
文档里留下的可能是一次已经不存在的运行。要根治是"重跑即换文件名（带时刻）"或"哈希由脚本现算进清单"，
本轮先立后者（`MANIFEST.json` + `--verify`），前者等下一次重跑批次时再定。
