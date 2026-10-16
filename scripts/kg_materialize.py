"""知识图谱 → 月度面板物化（2026-09-17，图谱轮第二步；预注册
plans/kg-event-graph-round2-20260917.md）

产出（每月末锚点 × 股票的横截面列, 写入 factor 风格的 parquet:
data/kg/kg_features.parquet, 供第二轮门禁经 ZooProposer.panels 直通）:

一阶统计（窗口 60 天, 全部只看 ≤锚点日）:
  kg_rating_up_60d    评级上调次数
  kg_rating_cov_60d   机构覆盖次数(买入/增持)
  kg_broker_rec_3m    被券商金股新纳入的不同券商数(近 3 个月)
  kg_holder_add_60d   股东增持事件数
  kg_holder_dec_60d   股东减持事件数
  kg_holder_net       增持-减持净事件
  kg_catalyst_60d     催化剂事件数(研报标题抽取)
  kg_sentiment_60d    情绪×确定性 加权和
  kg_dividend_90d     分红/送转事件计数
共振:
  kg_resonance        多类型事件共振计数(评级↑/金股/股东增持/催化剂 四类中 ≥2 类为 1,
                      列值=该股近 60 天命中的类型数)
传导（带衰减 PageRank, 沿 PIT 行业边+机构共同行为边）:
  kg_pagerank_ind     行业图传导强度: 种子=当期事件股(权重=事件数),
                      沿同行业边扩散(λ=0.5), 输出每只股的传导接收分
⚠️ 全部只读源表；输出独立 parquet, 不碰生产 factor 表。
用法: .venv/Scripts/python -X utf8 scripts/kg_materialize.py
"""
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from app import db  # noqa: E402
from app.logger import setup_logger  # noqa: E402

setup_logger()

OUT = BASE / "data" / "kg" / "kg_features.parquet"
WIN_60D = 60          # 一阶统计窗口(自然日)
WIN_90D = 90
WIN_3M = 3            # 金股窗口(月)
LAMBDA = 0.5          # 传导衰减


def month_end_anchors(conn) -> list:
    """月末锚点 = factor 表已有的月末截面日（与工厂评估窗一致）。"""
    return [r[0] for r in conn.execute(
        "SELECT DISTINCT trade_date FROM factor ORDER BY trade_date").fetchall()]


def main() -> int:
    t0 = time.time()
    conn = db.get_conn()
    try:
        conn.execute("PRAGMA query_only=1")
        anchors = month_end_anchors(conn)
        # 只物化 2020-01 之后（id60/研报文本边覆盖窗），历史锚点无新信息
        anchors = [a for a in anchors if a >= "20200101"]
        print(f"锚点 {len(anchors)} 个 ({anchors[0]}~{anchors[-1]})")

        ev = pd.read_sql("SELECT edge_type, ts_code, date8, direction, strength, entity "
                         "FROM kg_edge_event", conn)
        ev["dt"] = pd.to_datetime(ev["date8"], format="%Y%m%d", errors="coerce")
        ev = ev.dropna(subset=["dt"])
        # PIT 行业边: (股票, 行业, in_date) —— 锚点日生效的行业
        ind = pd.read_sql(
            "SELECT code_a, node, date8 FROM kg_edge_relate WHERE relate_type='industry_pit'",
            conn)
        ind["in_dt"] = pd.to_datetime(ind["date8"], format="%Y%m%d", errors="coerce")
        print(f"事件边 {len(ev):,} | 行业边 {len(ind):,} ({time.time()-t0:.0f}s)")

        rows = []
        for a in anchors:
            at = pd.Timestamp(a)   # a 可能是 str(尾部混入), 统一转 Timestamp
            w60_lo, w90_lo = at - pd.Timedelta(days=WIN_60D), at - pd.Timedelta(days=WIN_90D)
            w3m_lo = at - pd.DateOffset(months=WIN_3M)
            w = ev[(ev["dt"] > w60_lo) & (ev["dt"] <= at)]
            feat = {}
            def cnt(t, lo=None):
                x = w[w["edge_type"] == t]
                if lo is not None:
                    x = x[x["dt"] > lo]
                return x.groupby("ts_code").size()
            feat["kg_rating_up_60d"] = cnt("rating_up")
            feat["kg_rating_cov_60d"] = cnt("rating_neu") + cnt("rating_up")
            # 金股: 近 3 个月不同券商数
            br = w[(w["edge_type"] == "broker_rec") & (w["dt"] > w3m_lo)]
            feat["kg_broker_rec_3m"] = br.groupby("ts_code")["entity"].nunique()
            feat["kg_holder_add_60d"] = cnt("holder_add")
            feat["kg_holder_dec_60d"] = cnt("holder_dec")
            feat["kg_holder_net"] = feat["kg_holder_add_60d"] - feat["kg_holder_dec_60d"]
            feat["kg_catalyst_60d"] = cnt("catalyst")
            sent = w[w["edge_type"] == "sentiment"]
            feat["kg_sentiment_60d"] = (sent["direction"] * sent["strength"]).groupby(sent["ts_code"]).sum()
            feat["kg_dividend_90d"] = cnt("dividend", w90_lo)

            base = pd.DataFrame(feat)
            if base.empty:
                continue
            base = base.fillna(0)
            # 共振: 四类正向事件中命中的类型数
            res = ((base["kg_rating_up_60d"] > 0).astype(int)
                   + (base["kg_broker_rec_3m"] > 0).astype(int)
                   + (base["kg_holder_add_60d"] > 0).astype(int)
                   + (base["kg_catalyst_60d"] > 0).astype(int))
            base["kg_resonance"] = res
            # 传导: 种子=有事件的股(权重=1+log 事件数), 沿同行业边扩散 λ
            seed = (base.sum(axis=1) > 0).astype(float)
            seed = seed + np.log1p(base[[c for c in base.columns if c != "kg_resonance"]].sum(axis=1))
            pr_out = {}
            if seed.sum() > 0:
                # 行业分组传播: 每个行业内, 种子按 λ^1 均匀扩散给同行业其它成员
                ind_now = ind[ind["in_dt"] <= at].groupby("code_a")["node"].last()
                for industry, members in ind_now.groupby(ind_now).groups.items():
                    mates = list(members)   # 扩散域 = 全行业成员(2026-09-28 注: 原 `c in base.index or True` 恒真, 如实化)
                    in_seed = [c for c in mates if c in seed.index and seed[c] > 0]
                    if not in_seed:
                        continue
                    total = sum(seed[c] for c in in_seed)
                    give = LAMBDA * total / max(1, len(mates) - len(in_seed))
                    for c in mates:
                        if c not in seed.index or seed[c] == 0:
                            pr_out[c] = pr_out.get(c, 0.0) + give
            base["kg_pagerank_ind"] = pd.Series(pr_out)

            # ---- 第二轮缺口列（B/C/D 提案对齐后 NEED/PARTIAL 的物化, 2026-09-17）----
            # 研报: EPS 修正动量 / 目标价隐含空间 / 评级离散度 / 覆盖变化
            # EPS 修正动量: 近 30 天 vs 30-90 天的标题内数字承诺 eps 均值差（数字承诺在 jsonl,
            # 这里用研报表的 fy1_eps 作为 PIT 代理: 同一股近窗报告的 fy1_eps 均值差）
            try:
                rr_tbl = pd.DataFrame(conn.execute(
                    "SELECT ts_code, report_date, fy1_eps, aim_price_t, rating FROM research_report_em "
                    "WHERE report_date > ? AND report_date <= ? AND fy1_eps IS NOT NULL",
                    ((at - pd.Timedelta(days=90)).strftime("%Y%m%d"), at.strftime("%Y%m%d"))).fetchall(),
                    columns=["ts_code", "report_date", "fy1_eps", "aim_price_t", "rating"])
            except Exception:
                rr_tbl = pd.DataFrame()
            if len(rr_tbl):
                rr_tbl["dt"] = pd.to_datetime(rr_tbl["report_date"], format="%Y%m%d", errors="coerce")
                r30 = rr_tbl[rr_tbl["dt"] > at - pd.Timedelta(days=30)]
                r90 = rr_tbl[(rr_tbl["dt"] > at - pd.Timedelta(days=90)) & (rr_tbl["dt"] <= at - pd.Timedelta(days=30))]
                eps30 = r30.groupby("ts_code")["fy1_eps"].mean()
                eps90 = r90.groupby("ts_code")["fy1_eps"].mean()
                base["kg_eps_revision_mom"] = (eps30 - eps90) / eps90.abs().replace(0, np.nan)
                aim30 = r30.groupby("ts_code")["aim_price_t"].mean()
                # 目标价隐含空间需要现价: 从 daily 取锚点日收盘
                close_at = pd.DataFrame(conn.execute(
                    "SELECT ts_code, close FROM daily WHERE trade_date=?", (a,)).fetchall(),
                    columns=["ts_code", "close"])
                close_at = close_at.set_index("ts_code")["close"]
                base["kg_target_upside"] = (aim30 / close_at.reindex(aim30.index) - 1)
                rating_map = {"买入": 5, "增持": 4, "持有": 3, "中性": 3, "卖出": 1}
                rr_all = pd.concat([r30, r90])
                rr_all["rv"] = rr_all["rating"].map(rating_map)
                base["kg_rating_dispersion"] = rr_all.groupby("ts_code")["rv"].std()
                base["kg_coverage_chg"] = r30.groupby("ts_code").size() - r90.groupby("ts_code").size()
            # 股东: 集中度变化 / 新进席位计数（按 ann_date 最近一期 vs 前一期）
            th = pd.DataFrame(conn.execute(
                "SELECT ts_code, ann_date, end_date, holder_name, hold_ratio, hold_change FROM top10_holder "
                "WHERE ann_date IS NOT NULL AND ann_date > ?",
                ((at - pd.Timedelta(days=180)).strftime("%Y%m%d"),)).fetchall(),
                columns=["ts_code", "ann_date", "end_date", "holder_name", "hold_ratio", "hold_change"])
            if len(th):
                th["adt"] = pd.to_datetime(th["ann_date"], format="%Y%m%d", errors="coerce")
                conc = th[th["adt"] <= at].groupby(["ts_code", "end_date"])["hold_ratio"].sum()
                # 每股最近两期集中度差
                conc_df = conc.reset_index()
                conc_df = conc_df.sort_values(["ts_code", "end_date"])
                g = conc_df.groupby("ts_code")["hold_ratio"]
                base["kg_holder_conc_chg"] = g.diff()
                # 新进席位计数: 最近一期 hold_change != 0 且上一期无此席位——用 hold_change>0 且 is_new 近似
                newcnt = th[(th["adt"] <= at)].groupby("ts_code")["hold_change"].apply(
                    lambda s: int((s > 0).sum()))
                base["kg_holder_new_entry"] = newcnt
            # 金股窗口差: 近 1 月 vs 2-3 月券商数
            br1 = br[br["dt"] > at - pd.DateOffset(months=1)]
            feat_b1 = br1.groupby("ts_code")["entity"].nunique()
            base["kg_broker_rec_chg"] = feat_b1 - (feat["kg_broker_rec_3m"] - feat_b1)

            # ---- 第五轮（预注册 plans/kg-unlock-insider-round5-20260918.md）----
            # 解禁冲击: 未来 90 天解禁股数/流通股本（release_date 是已公告安排, 未来已知=PIT 合法）
            try:
                rl = pd.DataFrame(conn.execute(
                    "SELECT ts_code, release_date, release_shares FROM restricted_release "
                    "WHERE release_date > ? AND release_date <= ?",
                    (at.strftime("%Y%m%d"), (at + pd.Timedelta(days=90)).strftime("%Y%m%d"))).fetchall(),
                    columns=["ts_code", "release_date", "release_shares"])
                if len(rl):
                    agg = rl.groupby("ts_code")["release_shares"].sum()
                    # 流通股本分母: circ_mv(万元)/收盘价 —— daily_basic 无 close 列(20260918 修),
                    # 收盘价 join daily; 当日无行时回退最近可得日
                    basic = pd.read_sql(
                        "SELECT ts_code, circ_mv FROM daily_basic WHERE trade_date=?",
                        conn, params=(at.strftime("%Y%m%d"),))
                    if basic.empty:
                        basic = pd.read_sql(
                            "SELECT ts_code, circ_mv FROM daily_basic WHERE trade_date="
                            "(SELECT MAX(trade_date) FROM daily_basic WHERE trade_date<?)",
                            conn, params=(at.strftime("%Y%m%d"),))
                    close = pd.read_sql(
                        "SELECT ts_code, close FROM daily WHERE trade_date=?",
                        conn, params=(at.strftime("%Y%m%d"),))
                    basic = basic.merge(close, on="ts_code", how="left")
                    # 量纲(2026-09-18 审查修): daily_basic.circ_mv 单位是**万元**
                    # (实测 600519/000001/601398/000895 四只票 total_mv×1e4/close 与真实总股本逐一对上),
                    # ×1e4 → 元; /收盘价(元/股) = 流通股本(股)。release_shares 本身就是**股**,
                    # 不得再乘 1e4 —— 上一版写成 ×1e3 且注释说"万股×1e4"，两者都错 ⇒ 整列放大 10 倍。
                    basic["float_shares"] = np.where(
                        basic["close"] > 0, basic["circ_mv"] * 1e4 / basic["close"], np.nan)
                    fs = basic.set_index("ts_code")["float_shares"]
                    ratio = agg / fs.reindex(agg.index)
                    ratio = ratio.dropna()
                    if len(ratio):
                        base["kg_unlock_90d_ratio"] = ratio
                        base["kg_unlock_90d_cnt"] = rl.groupby("ts_code").size()
            except Exception as e:
                print(f"  解禁列失败 {a}: {type(e).__name__}: {str(e)[:80]}")
            # 内部人: 高管增减持 60 天窗（change_date PIT）
            try:
                it = pd.DataFrame(conn.execute(
                    "SELECT ts_code, change_date, delta_shares FROM insider_trade "
                    "WHERE change_date > ? AND change_date <= ? AND delta_shares IS NOT NULL",
                    ((at - pd.Timedelta(days=60)).strftime("%Y%m%d"), at.strftime("%Y%m%d"))).fetchall(),
                    columns=["ts_code", "change_date", "delta_shares"])
                if len(it):
                    it["dt"] = pd.to_datetime(it["change_date"], errors="coerce")
                    g = it.dropna(subset=["dt"]).groupby("ts_code")["delta_shares"]
                    base["kg_insider_buy_60d"] = it[it["delta_shares"] > 0].groupby("ts_code").size()
                    base["kg_insider_sell_60d"] = it[it["delta_shares"] < 0].groupby("ts_code").size()
                    base["kg_insider_net"] = base["kg_insider_buy_60d"].fillna(0) - base["kg_insider_sell_60d"].fillna(0)
            except Exception as e:
                print(f"  内部人列失败 {a}: {type(e).__name__}: {str(e)[:80]}")
            base["anchor"] = a
            rows.append(base.reset_index().rename(columns={"index": "ts_code"}))
        conn.close()
    except Exception:
        raise

    df = pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(OUT, index=False)
    print(f"✓ 物化 {len(df):,} 行 × {len(df.columns)} 列 → {OUT}")
    print("列:", [c for c in df.columns if c.startswith("kg_")])
    print(f"总耗时 {time.time()-t0:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
