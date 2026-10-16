"""LLM 网关（2026-09-16 前端重构新增，与 Alpha Factory 的 LLMProposer 同一定位）

**LLM 是研究辅助，不是判决者** —— 本项目的一切结论以门禁/回测/预注册为准
（见 app/alpha/factory.py 模块注释）。这里提供三个只读端点：

- GET  /api/llm/status         是否配置（只回 bool 与 model 名，**永不回传 key**）
- POST /api/llm/ask            问答：服务端组装**看板已可见**的读数快照作上下文
- GET  /api/llm/factor_review  读 scripts/llm_factor_review.py 的**离线**产出
                               （本模块不因看板请求而在线调 LLM 审因子）

配置**全住 .env**（不进 config.yaml —— 该文件受 git 跟踪，参考 EMAIL_AUTH_CODE 的口径）：

    LLM_API_KEY=sk-...                            # 必需
    LLM_BASE_URL=https://api.openai.com/v1        # 可选，**限 _ALLOWED_LLM_HOSTS**
    LLM_MODEL=gpt-4o-mini                         # 可选

安全与确定性口径：
- 未配置时 /ask 返回 400（前端自动退回本地规则问答，不白屏）；
- 上下文只用 /api/* 已暴露的读数（不新增任何数据面），不含任何密钥；
- 回答带模型名与免责口径，前端同时展示"LLM 生成"标识；
- **base_url 走主机白名单，不是"任意 OpenAI 兼容接口"**（2026-09-18 审查 Q-03）：
  `_chat` 会向 base_url 发 `Authorization: Bearer {LLM_API_KEY}`，只校验格式等于
  给 key 开了一条静默外传通道。要换供应商就改 `_ALLOWED_LLM_HOSTS`（一行），
  并同步知识库 03 号「本机安全姿态」。
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from urllib.parse import urlsplit

import requests
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app import timeutil

log = logging.getLogger(__name__)
# 注意前缀只写 /llm：本 router 由 api.py 的 /api router include，前缀会拼接
router = APIRouter(prefix="/llm")

# 2026-09-18 审查 P2-02: 原为相对路径 `Path("data")/"llm"`, 依赖进程 cwd —— 非项目根启动
# (服务/CLI/测试) 时读写会落到错误位置或静默失败。改为锚定仓库根的绝对路径。
DATA_DIR = Path(__file__).resolve().parents[2] / "data" / "llm"
REVIEW_FILE = DATA_DIR / "factor_review.json"
# DeepSeek 官方适配(2026-09-30): deepseek-flash 默认思考模式, 思考+作答完整生成在
# 非流式下读超时≈总时长, 30s 会挡掉长推理回答(前端只看到 504)。默认抬到 90s,
# LLM_TIMEOUT 环境变量仍可覆盖; 引用方(挖矿脚本 max(300, ASK_TIMEOUT))不受影响。
ASK_TIMEOUT = float(os.getenv("LLM_TIMEOUT", "90"))
# 简单内存限速（审查 SEC-02）：本机任意进程可循环 /ask 烧 key —— 60 秒窗口最多 10 次。
# 单进程内存级即可（本机单用户威胁模型），重启自然清零。
_ASK_TIMES: list[float] = []
_ASK_RATE = (10, 60.0)       # (次数, 窗口秒)

#: base_url **主机名白名单**（2026-09-18 审查 Q-03）。
#: `_chat` 向 base_url 发 `Authorization: Bearer {LLM_API_KEY}` ⇒ 只校验格式
#: （`https?://[A-Za-z0-9.\-]+` 放得过任意主机）等于给 key 开一条静默外传通道：
#: 本机任意进程 / 浏览器里一个同源页面，POST /api/llm/config 改 base_url
#: 再 POST /api/llm/test，key 就落到攻击者的可读日志里，全程不落本地日志。
#: `local_guard` 不缓解这一跳 —— 它管入站，这里是**服务端主动出站**。
#: 与 `api.py` 对 smtp_server 防的"授权码外泄链"是同构问题，那边已有先例。
#: 换供应商：加一行，并同步知识库 03 号「本机安全姿态」。
_ALLOWED_LLM_HOSTS = ("api.deepseek.com",       # .env 当前实际用量
                      "api.openai.com",         # 代码默认值
                      "127.0.0.1", "localhost")  # 本地代理 / ollama
_ALLOWED_LLM_SCHEMES = ("http", "https")


def _check_base_url(url: str) -> str:
    """校验 base_url 的主机在白名单内，返回规范化后的字符串。

    主机比较必须**全等**，不能 endswith —— `api.deepseek.com.evil.com` 是经典后缀伪装。
    用 `urlsplit().hostname` 取主机：它会自动剥掉 userinfo 与端口，
    所以 `https://api.deepseek.com@evil.com` 解析出的主机是 evil.com 而不是 deepseek。
    """
    u = (url or "").strip()
    try:
        parts = urlsplit(u)
    except ValueError as e:
        raise HTTPException(400, f"base_url 无法解析: {e}") from e
    if parts.scheme not in _ALLOWED_LLM_SCHEMES:
        raise HTTPException(400, "base_url 须为 http(s):// 形式")
    host = (parts.hostname or "").lower()
    if host not in _ALLOWED_LLM_HOSTS:
        raise HTTPException(400, "base_url 主机不在白名单内（防 LLM_API_KEY 外传）。"
                                 f"允许：{'、'.join(_ALLOWED_LLM_HOSTS)}；新增供应商请改 "
                                 "app/web/llm.py 的 _ALLOWED_LLM_HOSTS 并同步知识库 03 号")
    return u


def _rate_ok() -> bool:
    now = time.time()
    while _ASK_TIMES and now - _ASK_TIMES[0] > _ASK_RATE[1]:
        _ASK_TIMES.pop(0)
    if len(_ASK_TIMES) >= _ASK_RATE[0]:
        return False
    _ASK_TIMES.append(now)
    return True
_SYSTEM_PROMPT = (
    "你是 A股多因子量化系统 NEW ECO 的看板助手。用户是系统唯一使用者（个人研究用途）。\n"
    "规则：1) 只依据提供的数据快照回答，数字必须带时点/口径，不得编造；\n"
    "2) 系统口径：月频=月度调仓模拟盘，周频袖套=独立账本的周频策略，两者物理隔离；\n"
    "3) 历史回测含口径限制（评估窗 2019-07-22 起、不含 2018 崩盘段），引用回测成绩必须说明；\n"
    "4) 结尾加一行：以上为系统读数解读，仅供研究参考，不构成投资建议。\n"
    "5) 用中文，简洁分点。"
)


def _llm_cfg() -> dict:
    return {
        "api_key": os.getenv("LLM_API_KEY", "").strip(),
        "base_url": (os.getenv("LLM_BASE_URL") or "https://api.openai.com/v1").strip().rstrip("/"),
        "model": (os.getenv("LLM_MODEL") or "gpt-4o-mini").strip(),
    }


def _save_llm_env(api_key: str | None, base_url: str | None, model: str | None,
                  clear_key: bool = False) -> None:
    """把 LLM 配置写进 .env（set_env_secret：只改目标行、原子替换、同步本进程 os.environ
    ⇒ 服务内即时生效，无需重启）。key 绝不写 config.yaml（受 git 跟踪——邮箱授权码
    P0-06 同款红线；且 config.yaml 变更会动周频方案的 config_sha16 指纹）。"""
    from app import config as cfg_mod
    if clear_key:
        cfg_mod.set_env_secret("LLM_API_KEY", "")
    elif api_key:
        cfg_mod.set_env_secret("LLM_API_KEY", api_key.strip())
    if base_url is not None:
        cfg_mod.set_env_secret("LLM_BASE_URL", base_url.strip().rstrip("/"))
    if model is not None:
        cfg_mod.set_env_secret("LLM_MODEL", model.strip())


@router.post("/config")
def save_config(body: dict):
    """看板设置页保存 LLM 配置（key 输入的通道；与邮箱授权码同一威胁模型——
    本机同源 POST，local_guard 保护）。key 只进 .env，响应永不回传。"""
    if not isinstance(body, dict):
        raise HTTPException(400, "body 必须为对象")
    clear_key = bool(body.get("clear_key"))
    api_key = body.get("api_key")
    base_url = body.get("base_url")
    model = body.get("model")
    if api_key is not None and (not isinstance(api_key, str) or not api_key.strip()):
        raise HTTPException(400, "api_key 不能为空白（清除请用 clear_key）")
    if len(api_key or "") > 200:
        raise HTTPException(400, "api_key 过长")
    if base_url is not None:
        # 先卡格式（挡明显畸形输入），再卡**主机白名单**（Q-03：格式正则挡不住任意主机）
        if not isinstance(base_url, str) or not re.fullmatch(
                r"https?://[A-Za-z0-9.\-]+(:\d{1,5})?(/[A-Za-z0-9.\-/]*)?", base_url.strip()):
            raise HTTPException(400, "base_url 须为 http(s)://host[:port][/path] 形式")
        _check_base_url(base_url)
    if model is not None:
        if not isinstance(model, str) or not (1 <= len(model.strip()) <= 100):
            raise HTTPException(400, "model 须为 1~100 字符")
    _save_llm_env(api_key, base_url, model, clear_key=clear_key)
    c = _llm_cfg()
    return {"ok": True, "configured": bool(c["api_key"]),
            "base_url": c["base_url"] if c["api_key"] else None,
            "model": c["model"] if c["api_key"] else None}


@router.post("/test")
def test_conn():
    """测试连接：发一条极小消息验证 base_url/model/key 可用（花费可忽略）。"""
    if not is_configured():
        raise HTTPException(400, "LLM 未配置：先保存 api_key")
    t0 = time.time()
    try:
        reply = _chat([{"role": "user", "content": "回复『连接正常』四个字，不要其他内容。"}],
                      max_tokens=16, thinking=False)
    except HTTPException as e:
        raise HTTPException(e.status_code, f"连接失败: {e.detail}") from e
    except requests.RequestException as e:
        raise HTTPException(502, f"连接失败: {e}") from e
    return {"ok": True, "model": _llm_cfg()["model"],
            "latency_ms": int((time.time() - t0) * 1000),
            "reply": (reply or "")[:50]}


def is_configured() -> bool:
    return bool(_llm_cfg()["api_key"])


@router.get("/status")
def status():
    """LLM 配置状态（不回传 key —— 它只住 .env）"""
    c = _llm_cfg()
    return {"configured": bool(c["api_key"]),
            "base_url": c["base_url"] if c["api_key"] else None,
            "model": c["model"] if c["api_key"] else None}


def _chat(messages: list, max_tokens: int = 900,
          thinking: bool | None = None) -> str:
    """OpenAI 兼容 chat 调用。唯一外呼点（测试打桩这里，保证不联网）。

    thinking: None=交给 API 默认（deepseek-flash 默认**开启**思考）；
              False=显式关闭（{"type":"disabled"}）、True=显式开启。
    DeepSeek 官方适配(2026-09-30): **思考 token 计入 max_tokens 预算**（实测
    max_tokens=16 的连通性测试全部进了 reasoning_content、content 恒空）——
    不需要推理的调用（连通性测试/快问快答）必须显式关思考；需要推理的调用
    （研究模式/agent）保留思考并按推理型放大预算（见各调用点）。
    """
    c = _llm_cfg()
    if not c["api_key"]:
        raise HTTPException(400, "LLM 未配置：在 .env 写 LLM_API_KEY（可选 LLM_BASE_URL/LLM_MODEL）")
    payload = {"model": c["model"], "messages": messages, "max_tokens": max_tokens,
               "temperature": 0.2}
    if thinking is not None:
        payload["thinking"] = {"type": "enabled" if thinking else "disabled"}
    r = requests.post(
        c["base_url"] + "/chat/completions",
        headers={"Authorization": f"Bearer {c['api_key']}"},
        json=payload,
        timeout=ASK_TIMEOUT)
    r.raise_for_status()
    return r.json()["choices"][0]["message"].get("content") or ""


def _snapshot() -> dict:
    """问答上下文：把看板各读接口的载荷压成一份小快照（只读，不外呼行情——
    paper 盘中会拉实时价，这里刻意跳过 quotes：LLM 不需要盘中价，省一次外呼）。

    ⚠️ 用 `quote_client.suppress_live_quotes()`（请求级开关），**不要**用
    `unittest.mock.patch` 去改 `get_quotes`：那是进程级替换，并发的前端 `/api/paper`
    轮询会被一起打成"没有实时价"，且 patch 嵌套退出后桩会永久留在进程里（2026-09-19 审查 A3）。
    """
    from app.web import api as web_api
    from app.datasource.quote_client import suppress_live_quotes
    out = {}
    try:
        out["overview"] = web_api.overview()
    except Exception as e:                       # 单块失败不拖垮问答
        out["overview_error"] = str(e)
    try:
        with suppress_live_quotes():
            paper = web_api.paper()
        out["paper"] = {k: v for k, v in paper.items()
                        if k in ("paused", "cash", "market_value", "total",
                                 "is_live", "data_date", "initial_capital")}
        out["paper"]["n_positions"] = len(paper.get("positions", []))
    except Exception as e:
        out["paper_error"] = str(e)
    try:
        wk = web_api.weekly()
        out["weekly"] = {k: wk.get(k) for k in ("enabled", "kpi", "ops", "kpi_error", "ops_error")}
        p = wk.get("plan") or {}
        out["weekly"]["plan_brief"] = {k: p.get(k) for k in
                                       ("anchor", "top_n", "band", "n_target", "invested_ratio",
                                        "expectation", "expectation_multiplicity")}
    except Exception as e:
        out["weekly_error"] = str(e)
    try:
        sg = web_api.signal()
        out["signal"] = {"date": sg.get("date"),
                         "rows": (sg.get("rows") or [])[:10]}
    except Exception as e:
        out["signal_error"] = str(e)
    try:
        st = web_api.status()
        out["data_status_daily"] = next((r for r in st.get("rows", []) if r.get("name") == "daily"), None)
    except Exception as e:
        out["status_error"] = str(e)
    out["today"] = timeutil.now().strftime("%Y-%m-%d %H:%M")
    return out


class HistoryItem(BaseModel):
    role: str                     # "q" | "a"
    text: str


class AskBody(BaseModel):
    question: str
    agent_mode: bool = False      # True = 工具调用模式（LLM 自主检索，A+C 方案）
    research_mode: bool = False   # True = 研究模式（放开金融常识推理，允许给观点+确定性标注）
    history: list[HistoryItem] = []   # 多轮记忆：最近若干轮问答（前端裁剪后传）


# ---------------------------------------------------------------------------
# 智能体模式：只读工具注册表（2026-09-16，用户方案 A+C）
# 纪律：全部只读（复用 /api/* 同一批读函数）；每个工具自带 max_rows 防上下文爆炸；
# LLM 只能选工具+传参，不能注入 SQL。
# ---------------------------------------------------------------------------
def _t_overview(_args: dict) -> dict:
    from app.web import api as web_api
    o = web_api.overview()
    bt = o.get("backtest") or {}
    return {"市场状态": o.get("state"), "状态分": o.get("state_score"),
            "月频建议仓位": o.get("equity_weight"), "数据截至": o.get("data_date"),
            "方案日期": o.get("plan_date"),
            "基线指标": {k: bt.get(k) for k in
                         ("ann_return", "sys_ann_return", "max_drawdown", "sharpe",
                          "ic_mean", "icir", "win_rate", "start", "end", "periods")},
            "基线缺失标记": o.get("backtest_pin_missed")}


def _t_positions(_args: dict) -> dict:
    from app.web import api as web_api
    from app.datasource.quote_client import suppress_live_quotes
    with suppress_live_quotes():
        p = web_api.paper()
    out = {"账户": {k: p.get(k) for k in ("total", "cash", "market_value", "paused",
                                          "is_live", "data_date")},
           "持仓": [{"代码": x.get("ts_code"), "名称": x.get("name"), "股数": x.get("shares"),
                      "成本": x.get("avg_cost"), "盈亏pct": x.get("pnl_pct")}
                     for x in (p.get("positions") or [])]}
    return out


def _t_weekly(_args: dict) -> dict:
    from app.web import api as web_api
    d = web_api.weekly()
    k = d.get("kpi") or {}
    p = d.get("plan") or {}
    ex = p.get("expectation") or {}
    return {"enabled": d.get("enabled"), "ops": d.get("ops"), "kpi": k,
            "方案": {x: p.get(x) for x in ("anchor", "top_n", "band", "n_target",
                                            "invested_ratio", "dropped")},
            "诚实预期": ex, "预期多重比较": p.get("expectation_multiplicity"),
            "样本外": p.get("expectation_oos"),
            "持仓数": d.get("n_positions_total"), "成交笔数": d.get("n_trades_total")}


def _t_signals(args: dict) -> dict:
    from app.web import api as web_api
    sg = web_api.signal()
    code = (args.get("code") or "").strip()
    rows = sg.get("rows") or []
    hist = sg.get("history") or []
    if code:
        rows = [r for r in rows if code in str(r.get("ts_code", ""))]
        hist = [r for r in hist if code in str(r.get("ts_code", ""))]
    return {"最新信号日": sg.get("date"), "最新信号": rows[:20],
            "历史信号(近60日)": hist[:40]}


def _t_scores(args: dict) -> dict:
    from app.web import api as web_api
    code = (args.get("code") or "").strip()
    top = web_api.scores(limit=100)
    rows = top.get("rows") or []
    if code:
        hit = [r for r in rows if code in str(r.get("ts_code", ""))]
        return {"查询代码": code, "命中": hit,
                "在榜": bool(hit), "榜内日期": top.get("date")}
    return {"打分日": top.get("date"),
            "Top20": [{k: r.get(k) for k in ("ts_code", "name", "industry", "score",
                                               "pred_ret", "pct_chg")} for r in rows[:20]]}


def _t_lhb(args: dict) -> dict:
    from app.web import api as web_api
    d = web_api.lhb()
    code = (args.get("code") or "").strip()
    rows = d.get("rows") or []
    if code:
        rows = [r for r in rows if code in str(r.get("ts_code", ""))]
    return {"榜单日": d.get("date"), "来源": d.get("source"),
            "rows": rows[:25],
            "持仓上榜标记数": sum(1 for r in (d.get("rows") or []) if r.get("in_plan"))}


def _t_nav_history(args: dict) -> dict:
    from app.web import api as web_api
    which = (args.get("account") or "paper").strip()
    if which == "weekly":
        d = web_api.weekly()
        nav = d.get("nav") or []
        return {"账户": "周频袖套", "点数": len(nav),
                "序列": nav}
    from app.datasource.quote_client import suppress_live_quotes
    with suppress_live_quotes():
        p = web_api.paper()
    nav = p.get("nav") or []
    return {"账户": "月频模拟盘", "点数": len(nav),
            "序列": [{"日期": r.get("trade_date"), "净值": r.get("nav"),
                      "基准": r.get("bench_nav")} for r in nav[-120:]]}


def _t_data_status(_args: dict) -> dict:
    from app.web import api as web_api
    return {"各表状态": (web_api.status().get("rows") or [])[:30]}


def _t_health(_args: dict) -> dict:
    from app.web import api as web_api
    h = web_api.weekly_health(window=12)
    keep = {k: h.get(k) for k in ("level", "rolling_ic", "frozen_ic", "neg_streak",
                                  "turnover_est", "deployed_ratio", "reasons", "note")}
    return keep


def _t_story(args: dict) -> dict:
    """该股的事件流叙事（知识图谱）。code 必填。"""
    code = (args.get("code") or "").strip()
    if not code:
        return {"error": "code 必填"}
    from app.kg_story import get_stock_story
    # 兼容 6 位数字/带后缀。920(北交所新段)判定必须在 "9"→沪B 之前 ——
    # 原顺序 920xxx 先命中 ("6","9") ⇒ .SH, 920→.BJ 分支永不可达(2026-09-28 审查,
    # 与 lhb_client 同型; 原分支里的 `or code.startswith("920")` 是死代码)
    if len(code) == 6 and code.isdigit():
        suf = (".BJ" if code.startswith("920") or code[0] in ("4", "8")
               else ".SH" if code[0] in ("6", "9") else ".SZ")
        code = code + suf
    return get_stock_story(code)


def _t_all_accounts(_args: dict) -> dict:
    """三模型 7 纸面账户聚合（/api/all_accounts 同源只读；2026-10-06 问答补缺口——
    此前智能体检索只能看到 NEW ECO 两账户，fterm/ECO-W2 的账本读不进来）。"""
    from app.all_accounts import build_all
    d = build_all()
    out = {"生成时间": d.get("generated_at"), "totals": d.get("totals"), "账户": []}
    for a in d.get("accounts") or []:
        ex = a.get("extra") or {}
        item = {"名称": a.get("name"), "模型": a.get("model"), "id": a.get("id"),
                "权益": a.get("equity"), "自建仓pct": a.get("pct"),
                "回撤pct": a.get("dd_pct"), "现金": a.get("cash"),
                "持仓市值": a.get("pos_value"), "数据日": a.get("asof"),
                "数据滞后": a.get("stale"), "可读": a.get("ok"),
                "持仓数": len(a.get("positions") or [])}
        if str(a.get("id") or "").startswith("fterm-"):
            item["挂单数"] = len(ex.get("orders") or [])
            item["心跳"] = ex.get("heartbeat")
            if ex.get("halt_line") is not None and a.get("equity") is not None:
                item["距熔断线pct"] = round((a["equity"] / ex["halt_line"] - 1) * 100, 2)
        if str(a.get("id") or "").startswith("ecow2-"):
            item["熔断状态"] = ex.get("circuit")
        out["账户"].append(item)
    return out


TOOLS = {
    "get_overview":    (_t_overview,    "市场状态/月频基线指标（年化/回撤/夏普/IC/胜率/回测区间）"),
    "get_positions":   (_t_positions,   "月频模拟盘账户与全部持仓明细（代码/股数/成本/盈亏）"),
    "get_weekly":      (_t_weekly,      "周频袖套全量：执行档状态/KPI/方案/诚实预期/样本外警示"),
    "get_signals":     (_t_signals,     "动态信号：今日信号与近 60 日历史；可选参数 code 过滤单只股票"),
    "get_scores":      (_t_scores,      "模型打分榜：Top20 或按 code 查单股排名/打分/预测超额"),
    "get_lhb":         (_t_lhb,         "龙虎榜：当日榜单+游资动向；可选参数 code 过滤单股，含持仓上榜标记"),
    "get_nav_history": (_t_nav_history, "净值序列：参数 account='paper'(月频,默认)|'weekly'(袖套)，返回逐日净值"),
    "get_data_status": (_t_data_status, "数据新鲜度：各数据表最新日期/更新时间/增量"),
    "get_weekly_health": (_t_health,    "周频健康度：滚动 IC/换手/投入率/四档评级"),
    "get_stock_story": (_t_story,       "单股事件流叙事（知识图谱）：该股近 90 天的券商推荐/评级/股东增减持/研报催化剂事件流+行业热度；参数 code=6位代码"),
    "get_all_accounts": (_t_all_accounts, "三模型 7 纸面账户聚合：各账户权益/自建仓/回撤/挂单/心跳/熔断状态（含 fterm 双轨与 ECO-W2 三账本，跨账户问题必查）"),
}

_TOOLS_SPEC = [{"type": "function",
                "function": {"name": name, "description": desc,
                             "parameters": {"type": "object",
                                            "properties": {c: {"type": "string"} for c in ("code", "account")},
                                            "required": []}}}
               for name, (_fn, desc) in TOOLS.items()]

_AGENT_SYSTEM = (
    "你是「资产看板」（NEW ECO 主引擎 + fterm + ECO-W2 三模型 **7 个纸面账户**的全账户"
    "聚合系统）的**研究分析助手**。你有一组只读查询工具，可以自主检索系统的真实数据来回答问题。\n"
    "工作方式：\n"
    "1. 先判断回答问题需要哪些数据，**主动调用工具**检索（可连环多次）；不要凭空猜数字。\n"
    "2. 拿到数据后做**综合分析**：交叉对比、指出异常、给出你的解读——这是你比规则引擎强的地方。\n"
    "3. 系统口径：本系统覆盖三模型——NEW ECO（月频调仓模拟盘 2 万 + 周频袖套独立账本 100 万，"
    "物理隔离）、Final（dlv2 双轨：w2 2 万/top2 + t3 100 万/top10，t+1 开盘价撮合，每日 20:05 落账）、"
    "W2（B0 双臂，锚定周调仓，20:30 落账）。**凡问题涉及全账户、"
    "Final、W2、跨账户对比或「其他账户」，先调 get_all_accounts**；月频/袖套专属细节再用对应工具深挖。\n"
    "4. 回测基线 IC 0.0959/超额 +18.8pp/年（2019-07-22 起、不含 2018 崩盘段，引用须带口径）；"
    "袖套不足 8 周不报年化（样本守则）。Final/W2 的 pct 是自建仓累计口径（非当日），窗口很短，"
    "引用时如实说明。\n"
    "5. 观点纪律：可以给出判断与观点，但必须①区分『数据事实』与『你的推断』（推断标注"
    "『我的解读』），②不确定性高时明说，③结尾一行：以上仅供研究参考，不构成投资建议。\n"
    "6. 中文，先结论后依据。"
)

_MAX_TOOL_ROUNDS = 4          # 连环调用上限（防循环烧钱）
_MAX_HISTORY = 6              # 多轮记忆：最多带 6 条历史问答
_HISTORY_CHAR_CAP = 400       # 单条历史上限（前端只做 slice(-6)，长度上限在后端兜底）


def _history_messages(history) -> list:
    """历史问答 → chat messages（role='q'→user / 'a'→assistant）。

    2026-09-18 审查修：此前 _run_agent 收下 history 却从不使用，追问等于失忆。
    空文本跳过；只取末尾 _MAX_HISTORY 条；单条截断 _HISTORY_CHAR_CAP 字。
    接受 HistoryItem（pydantic）与 dict 两种形状。
    """
    out = []
    for h in list(history or [])[-_MAX_HISTORY:]:
        role = getattr(h, "role", None) or (h.get("role") if isinstance(h, dict) else None)
        text = getattr(h, "text", None) or (h.get("text") if isinstance(h, dict) else "")
        if role not in ("q", "a"):
            continue
        t = (text or "").strip()
        if t:
            out.append({"role": "user" if role == "q" else "assistant",
                        "content": t[:_HISTORY_CHAR_CAP]})
    return out


def _run_agent(question: str, research: bool, history: list = None) -> tuple:
    """工具调用循环。返回 (answer, model, n_tool_calls, snapshot_at)。"""
    c = _llm_cfg()
    # 2026-09-28 审查: agent 路径原缺"未配置"闸 —— 空 key 会真打到缺省 base_url
    # (api.openai.com)拿 401, 再被包装成 502"LLM 调用失败", 与"未配置 ⇒ 400"契约不符
    if not is_configured():
        raise HTTPException(400, "LLM 未配置：先保存 api_key")
    messages = [
        {"role": "system", "content": _AGENT_SYSTEM + (
            "\n6. 研究模式已开启：可运用组合投资与行为金融常识做更深推理（因子有效性、"
            "风格暴露、成本冲击），推断必须与工具检索到的数据挂钩。" if research else "")},
        *_history_messages(history),
        {"role": "user", "content": question},
    ]
    n_calls = 0
    for _round in range(_MAX_TOOL_ROUNDS):
        r = requests.post(
            c["base_url"] + "/chat/completions",
            headers={"Authorization": f"Bearer {c['api_key']}"},
            json={"model": c["model"], "messages": messages, "tools": _TOOLS_SPEC,
                  # DeepSeek 官方适配(2026-09-30): 思考 token 计入 max_tokens 预算 ——
                  # 原 2500/1800 档会被思考吃光 → content/tool_calls 截断空转。轮内 8k。
                  "max_tokens": 8192, "temperature": 0.3},
            timeout=ASK_TIMEOUT)
        r.raise_for_status()
        msg = r.json()["choices"][0]["message"]
        tool_calls = msg.get("tool_calls")
        if not tool_calls:
            return (msg.get("content") or ""), c["model"], n_calls, \
                timeutil.now().strftime("%Y-%m-%d %H:%M")
        messages.append({"role": "assistant",
                         "content": msg.get("content") or "",
                         "tool_calls": tool_calls})
        for tc in tool_calls:
            fn = (tc.get("function") or {})
            name = fn.get("name")
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except ValueError:
                args = {}
            impl = TOOLS.get(name)
            if impl is None:
                result = {"error": f"未知工具 {name}"}
            else:
                try:
                    result = impl[0](args if isinstance(args, dict) else {})
                except Exception as e:            # 工具失败不拖垮对话
                    result = {"error": f"{type(e).__name__}: {e}"}
            n_calls += 1
            messages.append({"role": "tool", "tool_call_id": tc.get("id"),
                             "content": json.dumps(result, ensure_ascii=False, default=str)[:6000]})
    # 连环检索触顶（2026-09-29 审查修复）：原实现只返回占位文案"（已达连环检索上限，
    # 以下为基于已检索数据的回答）"——承诺了"以下"却没有任何下文，用户看到的是一句
    # 自相矛盾的话。现在**不传 tools 再收笔一次**，让模型基于已检索的工具数据真正作答；
    # 收笔调用失败/返回空 ⇒ 退回提示文案（如实描述，不再承诺下文）。
    if messages[-1]["role"] != "tool":
        return "", c["model"], n_calls, timeutil.now().strftime("%Y-%m-%d %H:%M")
    fallback = "（已达连环检索上限，未能生成综合回答 —— 请基于上方工具已检索的数据直接追问）"
    try:
        messages.append({"role": "user",
                         "content": "连环检索已达上限。请只依据上方已检索到的工具数据，"
                                    "直接回答用户最初的问题，不要再调用工具。"})
        r = requests.post(
            c["base_url"] + "/chat/completions",
            headers={"Authorization": f"Bearer {c['api_key']}"},
            json={"model": c["model"], "messages": messages,
                  "max_tokens": 32768 if research else 8192, "temperature": 0.3},
            timeout=ASK_TIMEOUT)
        r.raise_for_status()
        text = (r.json()["choices"][0]["message"].get("content") or "").strip()
    except Exception:
        log.exception("agent 收笔调用失败（退回提示文案）")
        text = ""
    return (text or fallback), c["model"], n_calls, \
        timeutil.now().strftime("%Y-%m-%d %H:%M")


@router.post("/ask")
def ask(body: AskBody):
    if not _rate_ok():
        raise HTTPException(429, "请求过于频繁（60 秒内最多 10 次），稍后再试")
    q = (body.question or "").strip()
    if not q:
        raise HTTPException(400, "question 不能为空")
    if len(q) > 500:
        raise HTTPException(400, "question 过长（限 500 字）")
    if body.agent_mode:
        try:
            answer, model, n_calls, at = _run_agent(q, body.research_mode, body.history)
        except requests.Timeout:
            raise HTTPException(504, "LLM 响应超时（智能体模式需多轮调用，约 10-60 秒）")
        except Exception as e:
            log.exception("LLM agent 失败")
            raise HTTPException(502, f"LLM 调用失败: {e}")
        return {"answer": answer, "model": model, "agent": True,
                "tool_calls": n_calls, "snapshot_at": at}
    snap = _snapshot()
    sys_prompt = _SYSTEM_PROMPT + (
        "\n6. 研究模式：可运用金融常识做更深推理，推断须标注『我的解读』并与数据挂钩。"
        if body.research_mode else "")
    messages = [
        {"role": "system", "content": sys_prompt},
        *_history_messages(body.history),
        {"role": "user", "content": f"数据快照（JSON）：\n{json.dumps(snap, ensure_ascii=False, default=str)}\n\n用户问题：{q}"},
    ]
    try:
        # DeepSeek 官方适配(2026-09-30): 快问快答关思考(900 预算全给正文, 秒级返回);
        # 研究模式要真推理 ⇒ 思考开着 + 预算按推理型抬到 32k(思考 token 计入 max_tokens,
        # 2500 档会被思考吃光 → content 空 —— 2026-09-30 前的空回复即此因)。
        answer = _chat(messages, max_tokens=32768 if body.research_mode else 900,
                       thinking=bool(body.research_mode))
    except HTTPException:
        raise
    except requests.Timeout:
        raise HTTPException(504, "LLM 响应超时")
    except Exception as e:
        log.exception("LLM ask 失败")
        raise HTTPException(502, f"LLM 调用失败: {e}")
    return {"answer": answer, "model": _llm_cfg()["model"],
            "snapshot_at": snap.get("today")}


@router.get("/factor_review")
def factor_review():
    """离线审读产物（scripts/llm_factor_review.py 生成）。这里只读文件，绝不在线调 LLM。"""
    if not REVIEW_FILE.exists():
        return {"available": False, "reason": "尚无审读结果：运行 scripts/llm_factor_review.py 生成"}
    try:
        data = json.loads(REVIEW_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        return {"available": False, "reason": f"审读文件损坏: {e}"}
    data["available"] = True
    return data


def review_rows_from_bank(status_filter: str = "CANDIDATE", limit: int = 40) -> list:
    """alpha_bank → 待审读行（scripts/llm_factor_review.py 用）。
    放在这里而不是脚本里：端点测试与脚本共用同一取数逻辑。
    表不存在（新库/测试库没建过）时返回 []，不抛 —— 审读是研究辅助，缺表=无事可做。"""
    import pandas as pd
    from app import db
    try:
        conn = db.get_conn()
        try:
            df = pd.read_sql(
                "SELECT alpha_id, name, zoo, source, expr, note, PASS, p_adj, FULL_head "
                "FROM alpha_bank WHERE status=? ORDER BY created DESC LIMIT ?",
                conn, params=(status_filter, int(limit)))
        finally:
            conn.close()
    except Exception as e:
        log.warning("alpha_bank 读取失败（%s）—— 审读跳过", e)
        return []
    return df.to_dict("records")
