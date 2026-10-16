"""种子因子库(zoos) —— Alpha Factory 的输入口粮(2026-09-11)

## 出处与许可

`qlib158()` 是 **microsoft/qlib `Alpha158`** 的表达式移植, 逐条对应
`qlib/contrib/data/loader.py::Alpha158DL.get_feature_config()` 的默认参数
(`{"kbar": {}, "price": {"windows": [0], "feature": [OPEN,HIGH,LOW,VWAP]}, "rolling": {}}`)。
microsoft/qlib 以 **Apache-2.0** 发布, 移植保留署名:
`microsoft/qlib`, Copyright (c) Microsoft Corporation。
名字(字段名)与 qlib 保持一致(`KMID` / `ROC5` / `BETA20` …), 以便与文献/基准对齐。

**算的是价量信息源**。这与本项目"数据异构 > 算法异构"的论点是冲突的 ——
Alpha158 全部由 OHLCV 派生, 与存量 31 因子的**信息源重合**。把它放进工厂的理由不是
"它能提升收益", 而是: ①工厂需要**足够大的候选池**才能验证门禁有没有分辨率;
②它是公开、被大量文献检验过的基线, 拿它当**标尺**比拿自家因子当标尺更干净。
**真正的异构口粮是 `flow()` / `holder()` 那条线**(两融 / 资金流 / 股东户数), 见阶段 0c。
"""
from __future__ import annotations

# 滚动窗口(与 qlib 默认一致)
WINDOWS = [5, 10, 20, 30, 60]

# 每个算子的 (名字, 表达式模板, 参考链接)
# `{w}` 会被替换成窗口。逐条对应 qlib loader.py 里 `if use("X")` 分支的 fields。
_ROLLING = [
    ("ROC{w}", "Ref($close, {w})/$close",
     "https://www.investopedia.com/terms/r/rateofchange.asp"),
    ("MA{w}", "Mean($close, {w})/$close",
     "https://www.investopedia.com/ask/answers/071414/whats-difference-between-moving-average-and-weighted-moving-average.asp"),
    ("STD{w}", "Std($close, {w})/$close", "过去 w 日收盘价标准差 / 当日收盘"),
    ("BETA{w}", "Slope($close, {w})/$close", "过去 w 日收盘价对时间的 OLS 斜率"),
    ("RSQR{w}", "Rsquare($close, {w})", "过去 w 日收盘价线性回归的 R²(趋势线性度)"),
    ("RESI{w}", "Resi($close, {w})/$close", "过去 w 日线性回归残差"),
    ("MAX{w}", "Max($high, {w})/$close", "过去 w 日最高价 / 当日收盘"),
    ("MIN{w}", "Min($low, {w})/$close", "过去 w 日最低价 / 当日收盘"),
    ("QTLU{w}", "Quantile($close, {w}, 0.8)/$close", "过去 w 日收盘价 80% 分位"),
    ("QTLD{w}", "Quantile($close, {w}, 0.2)/$close", "过去 w 日收盘价 20% 分位"),
    ("RANK{w}", "Rank($close, {w})", "当日收盘在过去 w 日中的分位"),
    ("RSV{w}", "($close-Min($low, {w}))/(Max($high, {w})-Min($low, {w})+1e-12)",
     "随机指标 RSV"),
    ("IMAX{w}", "IdxMax($high, {w})/{w}",
     "https://www.investopedia.com/terms/a/aroon.asp"),
    ("IMIN{w}", "IdxMin($low, {w})/{w}",
     "https://www.investopedia.com/terms/a/aroon.asp"),
    ("IMXD{w}", "(IdxMax($high, {w})-IdxMin($low, {w}))/{w}", "Aroon 高/低点时间差"),
    ("CORR{w}", "Corr($close, Log($volume+1), {w})", "收盘价与对数成交量的相关"),
    ("CORD{w}", "Corr($close/Ref($close, 1), Log($volume/Ref($volume, 1)+1), {w})",
     "涨跌幅与量变化率的相关"),
    ("CNTP{w}", "Mean($close>Ref($close, 1), {w})", "过去 w 日上涨天数占比"),
    ("CNTN{w}", "Mean($close<Ref($close, 1), {w})", "过去 w 日下跌天数占比"),
    ("CNTD{w}", "Mean($close>Ref($close, 1), {w})-Mean($close<Ref($close, 1), {w})",
     "涨跌天数占比之差"),
    ("SUMP{w}", "Sum(Greater($close-Ref($close, 1), 0), {w})"
                "/(Sum(Abs($close-Ref($close, 1)), {w})+1e-12)",
     "https://www.investopedia.com/terms/r/rsi.asp"),
    ("SUMN{w}", "Sum(Greater(Ref($close, 1)-$close, 0), {w})"
                "/(Sum(Abs($close-Ref($close, 1)), {w})+1e-12)", "RSI 下行分量"),
    ("SUMD{w}", "(Sum(Greater($close-Ref($close, 1), 0), {w})"
                "-Sum(Greater(Ref($close, 1)-$close, 0), {w}))"
                "/(Sum(Abs($close-Ref($close, 1)), {w})+1e-12)", "RSI 净分量"),
    ("VMA{w}", "Mean($volume, {w})/($volume+1e-12)",
     "https://www.barchart.com/education/technical-indicators/volume_moving_average"),
    ("VSTD{w}", "Std($volume, {w})/($volume+1e-12)", "过去 w 日成交量标准差"),
    ("WVMA{w}", "Std(Abs($close/Ref($close, 1)-1)*$volume, {w})"
                "/(Mean(Abs($close/Ref($close, 1)-1)*$volume, {w})+1e-12)",
     "量加权价格波动率"),
    ("VSUMP{w}", "Sum(Greater($volume-Ref($volume, 1), 0), {w})"
                 "/(Sum(Abs($volume-Ref($volume, 1)), {w})+1e-12)", "放量占比"),
    ("VSUMN{w}", "Sum(Greater(Ref($volume, 1)-$volume, 0), {w})"
                 "/(Sum(Abs($volume-Ref($volume, 1)), {w})+1e-12)", "缩量占比"),
    ("VSUMD{w}", "(Sum(Greater($volume-Ref($volume, 1), 0), {w})"
                 "-Sum(Greater(Ref($volume, 1)-$volume, 0), {w}))"
                 "/(Sum(Abs($volume-Ref($volume, 1)), {w})+1e-12)", "量能 RSI"),
]

_KBAR = [
    ("KMID", "($close-$open)/$open", "K 线实体占比"),
    ("KLEN", "($high-$low)/$open", "K 线长度占比"),
    ("KMID2", "($close-$open)/($high-$low+1e-12)", "实体/全长"),
    ("KUP", "($high-Greater($open, $close))/$open", "上影线"),
    ("KUP2", "($high-Greater($open, $close))/($high-$low+1e-12)", "上影线/全长"),
    ("KLOW", "(Less($open, $close)-$low)/$open", "下影线"),
    ("KLOW2", "(Less($open, $close)-$low)/($high-$low+1e-12)", "下影线/全长"),
    ("KSFT", "(2*$close-$high-$low)/$open", "重心偏移"),
    ("KSFT2", "(2*$close-$high-$low)/($high-$low+1e-12)", "重心偏移/全长"),
]

_PRICE = [("OPEN0", "$open/$close", "开盘/收盘"),
          ("HIGH0", "$high/$close", "最高/收盘"),
          ("LOW0", "$low/$close", "最低/收盘"),
          ("VWAP0", "$vwap/$close", "均价/收盘")]

SOURCE_QLIB158 = "microsoft/qlib Alpha158 (Apache-2.0)"


def qlib158() -> list:
    """Alpha158 的 158 个表达式 → [(name, expr, meta), ...]"""
    out = []
    for name, expr, note in _KBAR:
        out.append((name, expr, {"info_source": "price_volume",
                                 "source": SOURCE_QLIB158, "note": note}))
    for name, expr, note in _PRICE:
        out.append((name, expr, {"info_source": "price_volume",
                                 "source": SOURCE_QLIB158, "note": note}))
    for tmpl_name, tmpl, note in _ROLLING:
        for w in WINDOWS:
            out.append((tmpl_name.format(w=w), tmpl.format(w=w),
                        {"info_source": "price_volume", "source": SOURCE_QLIB158,
                         "note": f"{note} (窗口 {w})"}))
    return out


def all_candidates() -> list:
    """工厂的默认候选池。**候选数是 Bonferroni 的分母, 加库要一起改门禁。**"""
    return qlib158()
