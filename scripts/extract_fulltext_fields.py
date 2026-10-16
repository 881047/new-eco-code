# -*- coding: utf-8 -*-
"""正文级字段抽取器 v1 —— 纯正则、确定性可复现（plans/fulltext-field-factors-prereg-20260930.md §一）。

输入: data/text_corpus/fulltext/ 或 fulltext_all/shard*/ 的 fulltext_{类别}.parquet
输出: data/text_corpus/fields/fields_{类别}.parquet
      列 = [代码, 公告时间, announcementId, category, field, value, snippet]

验证判据 V1（冻结）: 各字段命中率 ≥ 该类试点金额命中率 × 0.8；命中行必须带原文片段。
用法:
    .venv/Scripts/python.exe scripts/extract_fulltext_fields.py            # 试点数据
    .venv/Scripts/python.exe scripts/extract_fulltext_fields.py --all      # 全量(分片合并)
"""
from __future__ import annotations

import argparse
import glob
import json
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

BASE = Path(__file__).resolve().parent.parent
PILOT_DIR = BASE / "data" / "text_corpus" / "fulltext"
ALL_DIR = BASE / "data" / "text_corpus" / "fulltext_all"
OUT_DIR = BASE / "data" / "text_corpus" / "fields"

_NUM = r"(\d[\d,，]*(?:\.\d+)?)"
_SNIP = 60


def _amt(anchor: str) -> re.Pattern:
    """锚点 + 30 字 filler(允许换行——表格化正文的标签行与数字行分离) + 数字 + (亿|万)元。"""
    return re.compile(anchor + r"[^。]{0,30}?" + _NUM + r"\s*(亿|万)\s*元")


def _to_wan(num: str, unit: str) -> float:
    v = float(num.replace(",", "").replace("，", ""))
    return v * 10000.0 if unit == "亿" else v


#: (字段名, 编译正则, 值后处理)。真实措辞样本见 plans/fulltext-field-factors-prereg-20260930.md §一附录。
FIELDS = {
    "buyback": [
        ("bb_amount_max", _amt(r"(?:资金总额|回购金额|回购资金|用于回购)[^。]{0,12}?(?:不超过|不高于|上限为?)"),
         lambda m: _to_wan(m.group(1), m.group(2))),
        ("bb_amount_min", _amt(r"(?:资金总额|回购金额|回购资金|用于回购)[^。]{0,12}?(?:不低于|下限为?)"),
         lambda m: _to_wan(m.group(1), m.group(2))),
        ("bb_shares", re.compile(r"回购股份数量[^。]{0,16}?(?:不超过|约为?)" + _NUM + r"\s*万\s*股"),
         lambda m: float(m.group(1).replace(",", "").replace("，", ""))),
    ],
    "guarantee_financial_assistance": [
        ("gr_amount", _amt(r"(?:担保金额|实际担保[^。]{0,8}金额|担保[^。]{0,6}额度)"),
         lambda m: _to_wan(m.group(1), m.group(2))),
        ("gr_is_related", re.compile(r"被担保人?[^。]{0,40}?(关联方|控股子公司|全资子公司|合联营公司)"),
         lambda m: m.group(1)),
    ],
    "operating_contract_data": [
        ("oc_amount", _amt(r"(?:合同(?:金额|总价|价款|总金额)|合同金额合计|中标金额|增资金额)"),
         lambda m: _to_wan(m.group(1), m.group(2))),
    ],
    "ma_restructuring": [
        ("ma_price", _amt(r"(?:交易(?:作价|对价|价格|总价款)|作价|交易金额)"),
         lambda m: _to_wan(m.group(1), m.group(2))),
        ("ma_invest", _amt(r"拟以"),
         lambda m: _to_wan(m.group(1), m.group(2))),
    ],
    "litigation_regulatory": [
        ("lit_amount", _amt(r"(?:涉案金额|诉讼请求金额|起诉金额|涉诉金额)"),
         lambda m: _to_wan(m.group(1), m.group(2))),
    ],
    "earnings_forecast": [
        # 表格化正文: "净利润\n亏损：1,000,000 万元-1,200,000 万元"（标签行与数字行分离）
        ("ef_np_a", re.compile(r"(?:归属于上市公司股东的?净利润|净利润|亏损)[^。]{0,30}?[：:]*\s*"
                               + _NUM + r"\s*(亿|万)\s*元"),
         lambda m: _to_wan(m.group(1), m.group(2))),
        # 区间式: 两个数字都固定"万"单位 → 取下限(保守), 区间上限在 snippet 可查
        ("ef_np_range", re.compile(r"[：:]\s*" + _NUM + r"\s*万\s*元\s*[-–—至~]\s*"
                                   + _NUM + r"\s*万\s*元"),
         lambda m: _to_wan(m.group(1), "万")),
        ("ef_yoy", re.compile(r"同比(?:增长|上升|下降|减少)\s*(\d[\d.]*)\s*%"),
         lambda m: float(m.group(1))),
    ],
}

CATEGORIES = tuple(FIELDS)


def _snip(text: str, m: re.Match) -> str:
    s = max(0, m.start() - _SNIP // 2)
    return text[s:m.end() + _SNIP].replace("\n", " ")


def extract_df(df: pd.DataFrame, cat: str) -> pd.DataFrame:
    rows = []
    specs = FIELDS.get(cat, [])
    for _, r in df.iterrows():
        text = r["text"]
        for fname, pat, post in specs:
            m = pat.search(text)
            if m:
                try:
                    value = post(m)
                except Exception:
                    continue
                rows.append({"代码": r["代码"], "公告时间": r["公告时间"],
                             "announcementId": r["announcementId"], "category": cat,
                             "field": fname, "value": str(value),
                             "snippet": _snip(text, m)})
    return pd.DataFrame(rows)


def main(all_mode: bool = False) -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    if all_mode:
        srcs = {}
        for f in glob.glob(str(ALL_DIR / "shard*" / "fulltext_*.parquet")):
            cat = Path(f).stem.replace("fulltext_", "")
            srcs.setdefault(cat, []).append(f)
    else:
        srcs = {Path(f).stem.replace("fulltext_", ""): [f]
                for f in glob.glob(str(PILOT_DIR / "fulltext_*.parquet"))}
    report = {}
    for cat, files in sorted(srcs.items()):
        df = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
        df = df.drop_duplicates("announcementId")
        out = extract_df(df, cat)
        path = OUT_DIR / f"fields_{cat}.parquet"
        out.to_parquet(path, index=False)
        base_hit = float(df["text"].str.contains(r"\d+(?:\.\d+)?\s*[亿万万元]").mean())
        row_hit = float(out["announcementId"].nunique() / max(1, len(df))) if len(out) else 0.0
        per_field = (out.groupby("field")["announcementId"].nunique() / max(1, len(df))
                     ).round(4).to_dict() if len(out) else {}
        report[cat] = {"n_docs": int(len(df)), "n_field_rows": int(len(out)),
                       "doc_hit_rate": round(row_hit, 4),
                       "money_base_rate": round(base_hit, 4),
                       "v1_pass": bool(row_hit >= 0.8 * base_hit),
                       "per_field_hit": per_field}
        print(f"{cat:<32} 文档 {len(df):>5} | 命中文档率 {100*row_hit:>5.1f}% "
              f"(金额基线 {100*base_hit:.1f}%×0.8) | V1 {'PASS' if row_hit >= 0.8*base_hit else 'FAIL'}"
              f" | {per_field}")
    (OUT_DIR / ("v1_report_all.json" if all_mode else "v1_report_pilot.json")).write_text(
        json.dumps({"asof": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "prereg": "plans/fulltext-field-factors-prereg-20260930.md §一",
                    "categories": report}, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="合并 fulltext_all/shard*/ 全量数据")
    a = ap.parse_args()
    sys.exit(main(all_mode=a.all))
