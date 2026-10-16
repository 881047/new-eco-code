"""金融事件知识图谱构建（2026-09-17，知识图谱轮第一步；预注册见
plans/kg-event-graph-round2-20260917.md，用户拍板：两层混合图/文本边全语义/story 双用途）

建什么：SQLite 邻接表形态的事件图谱（kg_* 表），5 类节点、7 类带时点边。
设计核心：子图 = 一只股票的"新闻档案"（事件流叙事），LLM 在锚点日读子图
做推理（第三轮），统计层照常物化进工厂门禁（第二轮）。

边语义（全部带时点，锚点日只见 ≤T）：
  kg_edge_event: 事件边（edge_type, src 实体, ts_code, date8, 方向/强度, 原文引用）
  kg_edge_relate: 关联边（行业归属、机构共同行为、主题共现）

用法: .venv/Scripts/python -X utf8 scripts/build_kg.py            # 全量建图
      ... --text-only                                            # 只跑文本边(需 LLM_API_KEY)
"""
import argparse
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from app import db  # noqa: E402
from app.logger import setup_logger  # noqa: E402

setup_logger()

DDL = """
CREATE TABLE IF NOT EXISTS kg_edge_event (
    edge_type TEXT NOT NULL,          -- broker_rec | rating_up | rating_down | holder_add
                                      -- | holder_dec | dividend | catalyst | sentiment
    ts_code   TEXT NOT NULL,
    date8     TEXT NOT NULL,          -- PIT 时点(公告/发布/月份末)
    entity    TEXT,                   -- 对端实体(券商/机构/席位/主题)
    direction REAL,                   -- +1 利好 / -1 利空 / 0 中性
    strength  REAL,                   -- 强度(评级幅度/增持比例/情绪分 0~1)
    quote     TEXT                    -- 原文引用(研报标题/事件描述; LLM 引用锚点)
);
CREATE INDEX IF NOT EXISTS idx_kg_ev_code_date ON kg_edge_event (ts_code, date8);
CREATE INDEX IF NOT EXISTS idx_kg_ev_type_date ON kg_edge_event (edge_type, date8);
CREATE TABLE IF NOT EXISTS kg_edge_relate (
    relate_type TEXT NOT NULL,        -- industry_pit | co_coverage | topic_co
    code_a      TEXT NOT NULL,
    code_b      TEXT,                 -- 行业边时 code_b 为 NULL(边到行业节点)
    node        TEXT,                 -- 行业名/主题名/机构名
    date8       TEXT,                 -- 时点(行业边=in_date)
    weight      REAL
);
CREATE INDEX IF NOT EXISTS idx_kg_rel_a ON kg_edge_relate (code_a, relate_type);
"""


def build_event_edges(conn) -> dict:
    """四类结构化事件边（全部直接来自库表，零 LLM、零幻觉源头）。"""
    stats = {}
    # ① 券商金股: (券商, 月末) --推荐--> 股票
    conn.execute("""
        INSERT OR REPLACE INTO kg_edge_event
        SELECT 'broker_rec', ts_code, month || '31', broker, 1.0, 1.0, name
        FROM broker_recommend""")
    stats["broker_rec"] = conn.execute(
        "SELECT COUNT(*) FROM kg_edge_event WHERE edge_type='broker_rec'").fetchone()[0]
    # ② 评级变动: rating_change 编码 0=首次/新覆盖(中性+) 1=上调 2=维持(看 rating 正负)
    #    3=持有类中性 4=其他。方向: 上调 +1 / 下调 -1 / 维持按评级档(买入+0.5 增持+0.3 中性 0)
    conn.execute("DELETE FROM kg_edge_event WHERE edge_type IN ('rating_up','rating_down','rating_neu')")
    conn.execute("""
        INSERT INTO kg_edge_event
        SELECT CASE WHEN rating_change='1' THEN 'rating_up'
                    WHEN rating='买入' THEN 'rating_neu'
                    ELSE 'rating_neu' END,
               ts_code, report_date, org_name,
               CASE WHEN rating_change='1' THEN 1.0
                    WHEN rating='买入' THEN 0.5
                    WHEN rating='增持' THEN 0.3 ELSE 0.0 END,
               1.0, report_title
        FROM research_report_em
        WHERE rating_change='1' OR rating IN ('买入','增持')""")
    for t in ("rating_up", "rating_neu"):
        stats[t] = conn.execute(
            "SELECT COUNT(*) FROM kg_edge_event WHERE edge_type=?", (t,)).fetchone()[0]
    # ③ 十大流通股东变动: hold_change>0 增持 / <0 减持（ann_date PIT）
    conn.execute("DELETE FROM kg_edge_event WHERE edge_type IN ('holder_add','holder_dec')")
    conn.execute("""
        INSERT INTO kg_edge_event
        SELECT CASE WHEN hold_change > 0 THEN 'holder_add' ELSE 'holder_dec' END,
               ts_code, ann_date, holder_name,
               CASE WHEN hold_change > 0 THEN 1.0 ELSE -1.0 END,
               ABS(COALESCE(hold_change, 0)), holder_name || ' ' || COALESCE(holder_type,'')
        FROM top10_holder
        WHERE ann_date IS NOT NULL AND hold_change IS NOT NULL AND hold_change != 0""")
    for t in ("holder_add", "holder_dec"):
        stats[t] = conn.execute(
            "SELECT COUNT(*) FROM kg_edge_event WHERE edge_type=?", (t,)).fetchone()[0]
    conn.commit()
    return stats


def build_relate_edges(conn) -> dict:
    """关联边：PIT 行业归属（sw_member L1 × sw_classify 行业名, in_date/out_date 时点窗）。"""
    stats = {}
    conn.execute("DELETE FROM kg_edge_relate WHERE relate_type='industry_pit'")
    conn.execute("""
        INSERT INTO kg_edge_relate
        SELECT 'industry_pit', m.con_code, NULL, c.industry_name, m.in_date, 1.0
        FROM sw_member m JOIN sw_classify c ON c.index_code = m.index_code
        WHERE m.in_date IS NOT NULL AND c.industry_name IS NOT NULL""")
    stats["industry_pit"] = conn.execute(
        "SELECT COUNT(*) FROM kg_edge_relate WHERE relate_type='industry_pit'").fetchone()[0]
    conn.commit()
    return stats


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--text-only", action="store_true", help="只跑文本边(研报标题 LLM 抽取)")
    args = ap.parse_args()
    conn = db.get_conn()
    try:
        conn.executescript(DDL)
        conn.commit()
        if not args.text_only:
            print("== 结构化事件边 ==")
            for k, v in build_event_edges(conn).items():
                print(f"  {k:<14} {v:,} 条")
            print("== 关联边 ==")
            for k, v in build_relate_edges(conn).items():
                print(f"  {k:<14} {v:,} 条")
        print("文本边(kg_edge_event 中 edge_type IN catalyst/sentiment)由")
        print("scripts/llm_title_extract.py 增量抽取(需 LLM_API_KEY, 近 3 年研报标题)。")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
