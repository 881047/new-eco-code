"""把承载 B1/B2 判决的 `logs/*.json` 搬进被跟踪目录，并生成可复算的哈希清单。

为什么要这个脚本：`logs/` 被 `.gitignore:56` 整个忽略 ⇒ 台账这些年引用的
`logs/ab_bench_tr_real_index.json`（现行 pin 抬高量 2.22pp 的唯一产物）、
`logs/b1_pin_evidence_{before,after}.json`（换 pin 三道门 P-a/P-b/P-c 的证据）
**从来不在版本控制里**。本脚本只做一件事：复制到 `experiments/b1_pin_20260920/`
并记下哈希 —— 不改任何数值、不读数据库、不碰生产表。

哈希口径（单一真源，别再手抄）：
  canonical = sha256(json.dumps(json.load(f), indent=2, ensure_ascii=False, default=str))[:16]
先解析再重序列化 ⇒ 与盘上 CRLF/BOM/缩进漂移无关（`.gitattributes:2 * text=auto`
会让"文件字节 sha"在检出与提交两侧不一致，14 号 1488 行那个对不上的
`66bf3049a59b8704` 就是拿两种口径混着记造成的）。同时附 `bytes_sha16` 供人核对原文件。

用法
  D:\\Python313\\python.exe -X utf8 scripts/oneoff/track_b1_evidence.py          # 复制 + 写清单
  D:\\Python313\\python.exe -X utf8 scripts/oneoff/track_b1_evidence.py --verify  # 只校验，不写
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEST = ROOT / "experiments" / "b1_pin_20260920"

# (被跟踪文件名, logs 里的源文件, 台账引用位置, 这份产物是什么)
ITEMS = [
    ("ab_bench_tr_real_index.json", "ab_bench_tr_real_index.json",
     "14 号三补二十七 §⑤ / 三补二十八 ① / 三补二十九·补「留在台账里的可信部分」",
     "真尺 A/B（价格指数 vs `H00906.CSI` 全收益）两窗四臂 ⇒ 抬高量 2.22pp/年、C3 决策与回撤逐位不变"),
    ("b1_pin_evidence_before.json", "b1_pin_evidence_before.json",
     "14 号三补二十八 §① 三道门",
     "换 pin 之前的留证：基准腿指纹 + 金标准未动（P-a/P-b 的改前侧）"),
    ("b1_pin_evidence_after.json", "b1_pin_evidence_after.json",
     "14 号三补二十八 §① 三道门",
     "换 pin 之后的留证：与改前逐位比对（P-a/P-b 的改后侧）"),
    ("ab_bench_total_return.json", "ab_bench_total_return.json",
     "14 号三补二十七 §① / 02 号 §二 基准尺注",
     "合成全收益尺的四臂 A/B（抬高量估成 2.37pp/年，后被实测指数取代）"),
    ("audit_bench_total_return_gap.json", "audit_bench_total_return_gap.json",
     "14 号三补二十五 §② B1",
     "B1 一阶估计：中证800 成分股 dv_ttm 算术 2.24pp/年、复利上界 2.27pp/年"),
    ("audit_alpha_panel_adj.json", "audit_alpha_panel_adj.json",
     "14 号三补二十五 §② B2 / 三补三十 §③ Q1 行",
     "B2 静态量级：Alpha158 里 133/158 条踩价格水平、代表 14 条除权窗秩相关 ≈0.85"),
]


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def measure(path: Path) -> dict:
    raw = path.read_bytes()
    body = json.loads(raw.decode("utf-8-sig"))
    canon = json.dumps(body, indent=2, ensure_ascii=False, default=str)
    return {
        "file": path.name,
        "bytes": len(raw),
        "canonical_sha16": _sha(canon),
        "bytes_sha16": hashlib.sha256(raw).hexdigest()[:16],
    }


def main(argv: list[str]) -> int:
    verify = "--verify" in argv
    entries, problems = [], []
    for tracked, src, cited, what in ITEMS:
        srcp = ROOT / "logs" / src
        dstp = DEST / tracked
        if not verify:
            if not srcp.exists():
                problems.append(f"源文件不存在: {srcp}")
                continue
            DEST.mkdir(parents=True, exist_ok=True)
            dstp.write_bytes(srcp.read_bytes())
        if not dstp.exists():
            problems.append(f"缺被跟踪副本: {dstp}")
            continue
        m = measure(dstp)
        m.update({"source_in_logs": f"logs/{src}", "cited_in": cited, "what": what})
        entries.append(m)

    manp = DEST / "MANIFEST.json"
    if verify:
        if not manp.exists():
            print("MANIFEST.json 不存在"); return 1
        want = json.loads(manp.read_text("utf-8"))["artifacts"]
        bad = [e["file"] for e in entries
               if next((w for w in want if w["file"] == e["file"]), None)
               and next(w for w in want if w["file"] == e["file"])["canonical_sha16"]
               != e["canonical_sha16"]]
        for b in bad:
            problems.append(f"哈希与清单不符（产物被改过？）: {b}")
        for w in want:
            if not any(e["file"] == w["file"] for e in entries):
                problems.append(f"清单里有、盘上没有: {w['file']}")
        if problems or len(entries) != len(ITEMS):
            print("\n".join(problems)); print(f"✗ 校验失败（{len(entries)}/{len(ITEMS)}）")
            return 1
        print(f"✓ {len(entries)} 份证据产物存在且哈希与 MANIFEST.json 一致")
        return 0

    man = {"sha_scheme": "canonical = sha256(json.dumps(json.load(f), indent=2, "
                         "ensure_ascii=False, default=str))[:16]",
           "why": "logs/ 被 .gitignore:56 忽略 ⇒ 承载 pin 决策的证据此前不在版本控制内；"
                  "本目录是被跟踪副本。哈希先解析再重序列化，避免 CRLF/BOM 口径混用"
                  "（14 号 1488 行 `66bf3049a59b8704` 就是这么对不上的）。",
           "generator": "scripts/oneoff/track_b1_evidence.py",
           "artifacts": entries}
    manp.write_text(json.dumps(man, indent=2, ensure_ascii=False) + "\n", "utf-8")
    print(f"复制 {len(entries)} 份 → {DEST}")
    for e in entries:
        print("  {:<34} {:>7}B  {}".format(e["file"], e["bytes"], e["canonical_sha16"]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
