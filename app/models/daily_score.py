"""每日推分: 用最新月频模型 + 当日日频因子, 给全市场重新打分并存入 daily_score

与月度训练分 model_score 分表存储 (P1-11/P2-2): 月度方案/回测/补跑判定只看 model_score,
日频推分不再污染月度语义。

2026-09-04 集成(4种子): 推分用 ens/ 下多个 lgb_latest_s*.txt 模型, 分位排名取均值
(与 trainer 月度集成口径一致); 无集成文件时回退单模型 lgb_latest.txt。
"""
import logging
from pathlib import Path

import numpy as np
import pandas as pd
import lightgbm as lgb

from app import db
from app.config import DATA_DIR

log = logging.getLogger(__name__)

MODEL_FILE = DATA_DIR / "models" / "lgb_latest.txt"          # 单模型回退
ENS_DIR = DATA_DIR / "models" / "ens"                        # 集成模型目录
ENS_PREFIX = "lgb_latest_s"

# 模型训练的因子全集(与 trainer 一致)
ALL_FACTORS = ["rev_5", "mom_20", "mom_60", "mom_120", "mom_250", "vol_20", "vol_60",
               "skew_20", "ret_max20", "turnover_20", "turnover_chg", "amount_log",
               "illiq", "bias_20", "bias_60", "high_52w", "low_52w", "beta_250",
               "size", "ep", "bp", "dv_ttm",
               "roe", "netprofit_yoy", "or_yoy", "grossprofit_margin",
               "debt_to_assets", "eps_yoy",
               "lhb_net20", "lhb_org_net20", "lhb_cnt20"]


def _model_files():
    """推分模型文件列表: 集成(多种子)优先, 无集成文件时回退单模型。
    2026-09-05 审查修复: 原只要 ens/ 有任意文件就永远优先且不校验数量 —— 单模型重训
    后日频推分仍用旧集成、残缺集成静默求均值; 改为文件数与训练端写入的元信息一致才用。
    第三轮复查 M2: 期望数量不再写死 4 —— 训练端种子数可用 --ensemble-seeds 配置,
    lgb_latest_meta.txt 由 trainer 在保存最新模型时同步写入。
    2026-09-08 审查 P2: 校验改按 meta 内容逐种子核对存在性 —— 原按文件总数, 换种子
    重训后孤儿 lgb_latest_s*.txt 让总数永不匹配, 集成被永久回退

    2026-09-12 审查 P1-05: 支持训练端的两段式换批标记(meta 第二行)。`pending` = 另一进程
    正在换模型文件, 此刻盘上可能一半新一半旧 —— **必须**回退单模型, 绝不能用混合集成。
    (换批协议见 trainer._swap_latest_ensemble。)"""
    meta = ENS_DIR / "lgb_latest_meta.txt"
    seeds, state = [], ""
    try:
        if meta.exists():
            lines = meta.read_text(encoding="utf-8").strip().splitlines()
            if lines:
                seeds = [s.strip() for s in lines[0].split(",") if s.strip()]
            if len(lines) > 1:
                state = lines[1].strip()
    except OSError:
        seeds, state = [], ""
    if state.startswith("pending"):
        log.error("集成模型正在换批(meta=pending), 本次回退单模型 %s; "
                  "若长时间如此说明上次训练中途失败了", MODEL_FILE)
        return [MODEL_FILE]
    if seeds and len(seeds) >= 2:
        want = [ENS_DIR / f"{ENS_PREFIX}{s}.txt" for s in seeds]
        if all(p.exists() for p in want):
            return want
        # ERROR 而非 WARNING: 残缺集成是坏状态(训练中途崩过/文件被删), 日频推分会悄悄降级
        # 到单模型 —— 与月度方案不同源, 打分口径两套, 必须让人看见
        log.error("集成模型缺文件(种子 %s), 回退单模型 %s", seeds, MODEL_FILE)
    return [MODEL_FILE]


def score_today(trade_date: str = None) -> dict:
    files = _model_files()
    if not files:
        raise RuntimeError("缺少模型文件, 请先运行 scripts/train_model.py")
    models = []
    for f in files:
        if not Path(f).exists():
            raise RuntimeError(f"缺少模型文件: {f}")
        models.append(lgb.Booster(model_file=str(f)))
    with db.conn_ctx() as conn:
        # conn_ctx 已绑定连接(勿再 get_conn: 新开的那个无人关)
        if trade_date is None:
            trade_date = conn.execute("SELECT MAX(trade_date) FROM factor").fetchone()[0]
        # 当日因子
        fac = pd.read_sql(
            "SELECT ts_code, factor, value FROM factor WHERE trade_date=?", conn, params=(trade_date,))
        # 当日可交易性
        d = pd.read_sql(
            "SELECT ts_code, close, pre_close, vol, pct_chg FROM daily WHERE trade_date=?",
            conn, params=(trade_date,))
        # 上市日(2026-09-09 审查项 ⑤ 上市满 60 交易日锚定用; 原 basic 载入后未被使用)
        lb = pd.read_sql("SELECT ts_code, list_date FROM stock_basic", conn)
        # 60 交易日锚定的截止日(与 trainer.tradable_mask 的 first_trade <= dates[pos-60] 同口径):
        # 取 ≤ 评分日的最近 61 个交易日, 其最早那个即 dates[pos-60]
        cal60 = pd.read_sql("SELECT DISTINCT trade_date FROM daily WHERE trade_date<=? "
                            "ORDER BY trade_date DESC LIMIT 61", conn,
                            params=(trade_date,))["trade_date"]
        # R11-3(2026-09-06): 市值/流动性硬过滤(与 trainer/builder 同口径, 防监控信号推微盘);
        # 数据缺失时降级跳过
        mv = pd.DataFrame()
        amt = pd.DataFrame()
        mv_err = None
        try:
            if db.table_exists(conn, "daily_basic"):
                mv = pd.read_sql("SELECT ts_code, total_mv FROM daily_basic WHERE trade_date=?",
                                 conn, params=(trade_date,))
            last20 = pd.read_sql(
                "SELECT DISTINCT trade_date FROM daily WHERE trade_date<=? "
                "ORDER BY trade_date DESC LIMIT 20", conn, params=(trade_date,))["trade_date"]
            amt = pd.read_sql(
                f"SELECT ts_code, AVG(amount) AS amt20 FROM daily WHERE trade_date IN "
                f"({','.join('?' * len(last20))}) GROUP BY ts_code",
                conn, params=tuple(last20))
        except Exception as ex:      # 取数失败: 下面按"缺失"处理并报错(不再静默跳过整层过滤)
            mv, amt, mv_err = pd.DataFrame(), pd.DataFrame(), ex
    if fac.empty:
        raise RuntimeError(f"{trade_date} 无日频因子, 请先运行日频因子计算")

    X = fac.pivot(index="ts_code", columns="factor", values="value")
    d = d.set_index("ts_code")
    # 2026-09-06 第十三轮 R13-4: ST 过滤改时点名 NameLookup(原 stock_basic 当前名;
    # 生产只打当天两者等价, 补算历史日频分时当前名会有偏) —— 与全链其余 6 处同口径
    from app.models.names import NameLookup
    _nl = NameLookup()
    st_now = pd.Series([_nl.is_st(c, trade_date) for c in d.index], index=d.index)
    # 只剔涨停(买不进)/停牌/ST; 跌停股保留打分(持仓监控需要看到它, 买侧在 builder/monitor 另行剔除)
    # 2026-09-07 全市场扩展: 涨停判定按板块限幅用前收盘计算(创业/科创 20%、北交 30%)
    # 注意: d 已 set_index("ts_code"), 限幅从索引取代码
    from app.portfolio.board import board_of, limit_price, limit_ratio
    # 2026-09-12: 传 trade_date + 当日名称 —— 创业板 2020-08-24 前 ±10%; 主板 ST
    # 2026-07-06 起 ±10%; S股 ±5%; 退市整理期走板块常态档。日更恒为今天, 故对生产
    # 逐位无影响; 补算历史日频分才是修正。(ST 股随后即被 st_now 整列剔除, 此处取
    # 名称主要服务 "S" 股与创业板退市整理期新老划断的正确涨停判定)
    _lim = pd.Series([limit_ratio(str(c), False, trade_date, _nl.name_at(str(c), trade_date),
                                  _nl.name_at)
                      for c in d.index], index=d.index)
    # 2026-09-12: 改交易所四舍五入 round2 —— 这是全项目最后一处漏改的裸 round()
    # (2026-09-08 统一改 round2 时漏掉这里)。银行家舍入只在 .x5 分界差 1 分, 但它
    # 方向单边: round ≤ round2 ⇒ 涨跌停价取低 ⇒ `close >= limit_up - 0.001` **多命中**
    # ⇒ 约 0.4% 的股票被误判为涨停而漏打分。改后与 paper/回测/builder/monitor 同口径。
    # 2026-10-05: 北交所取整接线(上轨 floor/下轨 ceil, 见 board.limit_price) ——
    # 非 BSE 板内部就是 round2, 与旧口径逐位相同。
    _limit_up = pd.Series(
        [limit_price(p, r, board_of(str(c))) for p, r, c in
         zip(d["pre_close"], _lim, d.index)], index=d.index, dtype="float64") - 0.001
    _is_lim = (d["pre_close"] > 0) & (d["close"] >= _limit_up)
    ok = (d["vol"] > 0) & ~_is_lim & ~st_now
    # 2026-09-09 修复(审查项 ⑤): 上市满 60 交易日锚定 —— 与 trainer.tradable_mask 同口径。
    # 原日频推分无此过滤(实测每日 35~42 只次新股被打分), 而训练集把它们整体排除 →
    # 分布外样本进入推分/监控信号。补上后「上市前 5 日无涨跌幅限制」在选股侧自然不可达
    # (候选必然已上市 >= 60 个交易日), paper._exec_price_open 的无限制窗保留为兜底。
    # list_date 缺失按「早已上市」处理 —— ⚠️ 2026-09-28 审查注记(36补): 这与 PIT 引擎
    # 的口径**不同**(PIT._build_first_trade 对缺 list_date 用面板首现日锚定, 更保守)。
    # 生产 stock_basic 5908 只 list_date 零缺失 ⇒ 现行 Δ=0, 仅测试/新装机构型分叉;
    # 收紧须走预注册(改的是推分域), 此处如实登记不一致。
    if len(cal60) >= 61:
        cutoff60 = str(cal60.iloc[-1])
        ld = lb.set_index("ts_code")["list_date"].reindex(d.index).fillna("00000000").astype(str)
        ok = ok & (ld <= cutoff60)
    else:
        log.warning("交易日历不足 61 日, 跳过上市满 60 交易日锚定(%s)", trade_date)
    # 2026-09-08 用户拍板: 训练全市场、选股排除科创板(与 builder/回测同口径)
    from app import config as _cfg_mod
    if not _cfg_mod.load_config().get("star_enabled", False):
        # 2026-09-13: 判据只许有一处 —— 原来写死 "688" 前缀, 漏掉 689(科创板 CDR)，于是
        # "训练全市场、选股排除科创板"这条 2026-09-08 拍板的分叉进了生产。改用 board.is_star。
        from app.portfolio.board import is_star
        ok = ok & ~pd.Series([is_star(c) for c in d.index], index=d.index)
    # 2026-09-09 修复(与 ⑤ 同族的一致性缺口, 实跑核查发现): 北交所排除 —— trainer 的
    # tradable_mask 在 bse_enabled 关闭时排除北交(训练域不含北交), 但日频推分原无此过滤
    # (实测每日 317 只北交被打分), 模型对北交是纯外推且评分系统性虚高 → 分位排名被整体
    # 压低, 监控 weak_pct 阈值与看板分布失真。数据留库, 仅推分侧与训练域对齐。
    if not _cfg_mod.load_config().get("bse_enabled", False):
        from app.portfolio.board import board_of
        ok = ok & ~pd.Series([board_of(str(c)) == "bse" for c in d.index], index=d.index)
    # R11-3(2026-09-06): 市值/流动性硬过滤(NaN 保守剔除), 与 trainer/builder 同口径。
    # 2026-09-12 审查 P1-04: 原写 `if not mv.empty and not amt.empty:` —— 数据缺失就**整层
    # 跳过**, 于是那天推分域比训练域宽(微盘/低流动性股重新进来), 且无声无息。改为缺失即报错:
    # 宁可当天不出分(链路上可见、当班能处置), 也不要静默按另一种口径选股。
    # 注意"缺"有两种形态: 结果集为空(表不存在/该日无行), 以及有行但值全为 NULL(数据源
    # 当天返回空列)。后者 `df.empty` 是 False —— 只看 empty 会让全 NaN 的比较静默淘汰
    # **整个截面**(NaN >= x 恒为 False), 最后死在 LightGBM 的 "non empty" 里, 报错看不出
    # 是数据问题。两种形态在此一并按缺失处理。
    def _all_missing(df, col):
        return df.empty or col not in df.columns or bool(df[col].isna().all())

    if _all_missing(mv, "total_mv") or _all_missing(amt, "amt20"):
        raise RuntimeError(
            f"{trade_date} 缺市值/流动性数据(daily_basic 无当日 total_mv 或近 20 日成交额"
            f"全为空; 取数异常={mv_err!r}), 无法按训练域过滤推分截面 —— "
            f"请先补齐数据再推分(拒绝放宽口径)")
    from app import config as cfg_mod
    _cfg = cfg_mod.load_config()
    mv_q = mv["total_mv"].quantile(float(_cfg.get("stock_size_pctl", 0)))
    ok = ok & (mv.set_index("ts_code")["total_mv"].reindex(d.index) >= mv_q) \
        & (amt.set_index("ts_code")["amt20"].reindex(d.index) >= float(_cfg.get("stock_min_amt20", 0)))
    X = X[X.index.isin(ok[ok].index)]
    # 过滤后一只不剩 = 推分截面为空。行为与"数据缺失"一致(当天不出分), 但必须先在这里
    # 说清楚: 否则异常抛到 model.predict 里只剩 LightGBM 的 "Input data must be 2
    # dimensional and non empty", 当班无法判断是数据缺失、口径过紧还是行情异常。
    if X.empty:
        raise RuntimeError(
            f"{trade_date} 市值/流动性过滤后推分截面为空(过滤前 {len(ok)} 只) —— "
            f"请检查当日 daily_basic/daily 数据与 stock_size_pctl/stock_min_amt20 配置")

    # 特征按模型训练时的列序对齐(2026-09-04 修复): ALL_FACTORS 是计算序, 训练按字母序,
    # Booster.predict 纯按列位置取特征, 列序不一致会静默错位(旧28特征模型甚至直接报错)
    raw_preds, pct_ranks = [], []
    for model in models:
        feat_names = model.feature_name()
        # 2026-09-12 审查 P1-04: 原 `X.reindex(columns=feat_names)` 对缺失列**直接变全 NaN**,
        # LightGBM 照样吐出近似常数分, 全市场打分退化成按序排列的噪声却无任何报错 ——
        # 实盘选股静默失真。改为一缺即 raise: 缺的哪个因子要写在错误里, 当班能直接处置。
        missing = [f for f in (feat_names or ALL_FACTORS) if f not in X.columns]
        if missing:
            raise RuntimeError(
                f"模型需要但当日因子表缺少: {missing} —— 缺列会被 reindex 成全 NaN, "
                f"LightGBM 会照常输出近似常数分(静默失真), 故拒绝推分。"
                f"请先补齐这些因子的计算, 或回退到与当前因子口径配套的模型")
        Xm = X.reindex(columns=feat_names if feat_names else ALL_FACTORS)
        p = model.predict(Xm.astype(np.float32), num_iteration=model.best_iteration or -1)
        raw_preds.append(p)
        pct_ranks.append(pd.Series(p, index=X.index).rank(pct=True))
    # 集成口径与 trainer 一致: 分位排名均值 × 100; pred_ret 取原始预测均值(仅展示)
    score = (sum(pct_ranks) / len(pct_ranks)) * 100
    pred_ret = np.mean(np.array(raw_preds), axis=0)
    out = pd.DataFrame({
        "ts_code": X.index, "trade_date": trade_date,
        "score": score.values,
        "pred_ret": pred_ret,
    })
    with db.conn_ctx() as conn:
        # conn_ctx 已绑定连接(勿再 get_conn: 新开的那个无人关)
        conn.execute("DELETE FROM daily_score WHERE trade_date=?", (trade_date,))  # 先删后写, 防止被新过滤剔除的旧行残留
        n = db.upsert_df(conn, out, "daily_score")
        conn.commit()
    log.info("每日推分完成: %s, %d 只 (%d 模型)", trade_date, n, len(models))
    return {"trade_date": trade_date, "n": n}
