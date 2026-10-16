"""LLM 网关测试（2026-09-16 前端重构新增）—— **全程打桩，不联网**

钉住的事实：
  ① status 永不回传 key（只回 bool/model）；
  ② 未配置 key 时 ask 返回 400（前端退回本地规则问答，不是 500）；
  ③ _chat 打桩成功时 ask 正常组装（snapshot 里数字来自真库/真配置 —— 只读）；
  ④ factor_review：无文件 → available=False；有文件 → 原样带 available=True；
  ⑤ /ask 是 POST 端点且挂在 app 上（路由覆盖，防"前端调了后端没有"）；
  ⑥ app/ 运行时代码不许 import unittest.mock（进程级替身会污染并发请求，见 A3）。

运行: python -m pytest tests/test_web_llm.py -q
"""
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from app.main import app
from app import config as cfg_mod
from app.web import llm as llm_mod
from fastapi import HTTPException


def _env(**kw):
    """构造 LLM 环境变量 patch（默认=已配置假 key）"""
    vals = {"LLM_API_KEY": "sk-test", "LLM_BASE_URL": "https://api.test/v1",
            "LLM_MODEL": "test-model"}
    vals.update(kw)
    return mock.patch.dict("os.environ", vals)


class TestStatus(unittest.TestCase):
    def test_status_never_leaks_key(self):
        with _env():
            out = llm_mod.status()
        self.assertEqual(out["configured"], True)
        self.assertEqual(out["model"], "test-model")
        blob = json.dumps(out, ensure_ascii=False)
        self.assertNotIn("sk-test", blob, "status 不得回传 key")

    def test_status_unconfigured(self):
        with _env(LLM_API_KEY=""):
            out = llm_mod.status()
        self.assertEqual(out["configured"], False)


class TestAsk(unittest.TestCase):
    def test_ask_400_when_unconfigured(self):
        with _env(LLM_API_KEY=""):
            with self.assertRaises(Exception) as cm:
                llm_mod.ask(llm_mod.AskBody(question="今天该做什么"))
            self.assertIn("400", str(cm.exception.__dict__.get("status_code", "")))

    def test_ask_happy_path_stubbed_chat(self):
        """_chat 打桩 ⇒ 不发网络请求；snapshot 组装走真库（只读）。"""
        called = {}

        # thinking 形参(2026-09-30 DeepSeek 适配): _chat 新增可选参数, 桩须接纳
        def fake_chat(messages, max_tokens=900, thinking=None):
            called["messages"] = messages
            return "测试回答：市场中性。仅供研究参考，不构成投资建议。"

        with _env(), mock.patch.object(llm_mod, "_chat", side_effect=fake_chat):
            out = llm_mod.ask(llm_mod.AskBody(question="今天该做什么"))
        self.assertEqual(out["answer"], "测试回答：市场中性。仅供研究参考，不构成投资建议。")
        self.assertEqual(out["model"], "test-model")
        self.assertIn("snapshot_at", out)
        # 上下文必须带系统提示与数据快照（不联网、不含 key）
        blob = json.dumps(called["messages"], ensure_ascii=False)
        self.assertNotIn("sk-test", blob, "发给 LLM 的上下文不得含 key")

    def test_ask_rejects_empty_and_overlong(self):
        with _env():
            for q in ("", "x" * 501):
                with self.assertRaises(Exception):
                    llm_mod.ask(llm_mod.AskBody(question=q))

    def test_ask_route_mounted(self):
        paths = app.openapi()["paths"]
        self.assertIn("/api/llm/ask", paths)
        self.assertIn("post", paths["/api/llm/ask"])


class TestFactorReview(unittest.TestCase):
    def test_no_file_reports_unavailable(self):
        with mock.patch.object(llm_mod, "REVIEW_FILE", BASE / "data" / "llm" / "_no_such_.json"):
            out = llm_mod.factor_review()
        self.assertFalse(out["available"])
        self.assertIn("reason", out)

    def test_reads_file_and_marks_available(self):
        import tempfile
        payload = {"generated_at": "2026-09-16 12:00:00", "model": "m",
                   "reviews": [{"name": "f1", "meaning": "x", "flags": [], "verdict": "mid"}]}
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
            path = Path(f.name)
        try:
            with mock.patch.object(llm_mod, "REVIEW_FILE", path):
                out = llm_mod.factor_review()
        finally:
            path.unlink()
        self.assertTrue(out["available"])
        self.assertEqual(out["reviews"][0]["name"], "f1")
        json.dumps(out, allow_nan=False)   # 过得了 FastAPI 的 JSON 契约

    def test_review_rows_reads_bank_safely(self):
        """review_rows_from_bank 只读 alpha_bank（表可能为空，但不许抛）。"""
        rows = llm_mod.review_rows_from_bank("CANDIDATE", 5)
        self.assertIsInstance(rows, list)


class TestRateLimit(unittest.TestCase):
    def test_rate_limit_10_per_60s(self):
        """审查 SEC-02：/ask 内存限速（60 秒 10 次）—— 直接单测 _rate_ok（纯函数，快）。"""
        llm_mod._ASK_TIMES.clear()
        results = [llm_mod._rate_ok() for _ in range(11)]
        self.assertEqual(results[:10], [True] * 10)
        self.assertFalse(results[10], "第 11 次必须被限速")
        # 窗口滑出后恢复
        llm_mod._ASK_TIMES.clear()
        self.assertTrue(llm_mod._rate_ok())


class TestSaveConfig(unittest.TestCase):
    def test_save_writes_env_never_returns_key(self):
        """前端 key 输入通道：set_env_secret 打桩（不写真 .env）；响应永不回传 key。"""
        written = {}
        with mock.patch.object(cfg_mod, "set_env_secret",
                               side_effect=lambda k, v: written.__setitem__(k, v)), \
                _env(LLM_API_KEY="", LLM_BASE_URL="", LLM_MODEL=""):
            out = llm_mod.save_config({"api_key": "sk-abc", "base_url": "https://api.deepseek.com",
                                       "model": "deepseek-chat"})
        self.assertEqual(written.get("LLM_API_KEY"), "sk-abc")
        self.assertEqual(written.get("LLM_BASE_URL"), "https://api.deepseek.com")
        self.assertEqual(written.get("LLM_MODEL"), "deepseek-chat")
        self.assertNotIn("sk-abc", json.dumps(out), "响应不得回传 key")

    def test_clear_key(self):
        written = {}
        with mock.patch.object(cfg_mod, "set_env_secret",
                               side_effect=lambda k, v: written.__setitem__(k, v)), \
                _env(LLM_API_KEY="", LLM_BASE_URL="", LLM_MODEL=""):
            llm_mod.save_config({"clear_key": True})
        self.assertEqual(written.get("LLM_API_KEY"), "", "清除=写空值")

    def test_validation(self):
        with mock.patch.object(cfg_mod, "set_env_secret", side_effect=lambda k, v: None), \
                _env(LLM_API_KEY="", LLM_BASE_URL="", LLM_MODEL=""):
            for bad in ({"api_key": "   "}, {"base_url": "ftp://x"}, {"base_url": "not-a-url"},
                        {"model": ""}, {"model": "x" * 101}):
                with self.assertRaises(Exception, msg=repr(bad)):
                    llm_mod.save_config(bad)

    def test_base_url_host_allowlist_closes_the_exfil_channel(self):
        """Q-03：`base_url` 只校验**格式**不校验**主机** ⇒ 这是一条 LLM_API_KEY 外传通道。

        两次本机请求即可静默拿走 key：`POST /api/llm/config {base_url: 攻击者}` →
        `POST /api/llm/test`，而 `_chat` 向 `base_url + /chat/completions` 发
        `Authorization: Bearer {api_key}`。不落日志、不告警。`local_guard` 不缓解：
        它管的是**入站**，这一跳是**服务端主动出站**。
        同一条链在 `api.py` 对 smtp_server 已经防过（授权码外泄链）。

        ⚠️ 原来的格式正则 `https?://[A-Za-z0-9.\\-]+` 放得过**任意**主机 —— 所以必须
        是主机名白名单，且比较必须是**全等**：`api.deepseek.com.evil.com` 这类后缀伪装
        用 endswith 判断会照样放行。
        """
        ok_hosts = ["https://api.deepseek.com",          # .env 当前实际用量
                    "https://api.openai.com/v1",          # 代码默认值
                    "http://127.0.0.1:8080/v1", "http://localhost:11434/v1"]   # 本地代理/ollama
        evil = ["https://evil.example.com",
                "https://api.deepseek.com.evil.com",      # 后缀伪装
                "https://evil.com/?x=https://api.deepseek.com",
                "https://api.deepseek.com@evil.com",      # userinfo 藏真主机
                "http://169.254.169.254/latest/v1",       # 云元数据
                "https://attacker.cn"]
        with mock.patch.object(cfg_mod, "set_env_secret", side_effect=lambda k, v: None), \
                _env(LLM_API_KEY="", LLM_BASE_URL="", LLM_MODEL=""):
            for good in ok_hosts:
                try:
                    llm_mod.save_config({"base_url": good})
                except HTTPException as e:
                    self.fail(f"白名单内的 {good} 被误杀: {e.detail}")
            for bad in evil:
                with self.assertRaises(HTTPException, msg=f"外传通道没关: {bad}") as cm:
                    llm_mod.save_config({"base_url": bad})
                self.assertEqual(cm.exception.status_code, 400)

    def test_allowlist_rejects_before_writing_env(self):
        """白名单必须在落盘**之前**判：先写 .env 再报错等于攻击者已经改好了配置。"""
        written = []
        with mock.patch.object(cfg_mod, "set_env_secret",
                               side_effect=lambda k, v: written.append((k, v))), \
                _env(LLM_API_KEY="", LLM_BASE_URL="", LLM_MODEL=""):
            with self.assertRaises(HTTPException):
                llm_mod.save_config({"base_url": "https://evil.example.com"})
        self.assertEqual(written, [], "被拒的 base_url 一个字都不许落盘")

    def test_test_endpoint(self):
        """测试连接端点：_chat 打桩；未配置时 400。"""
        with _env(), mock.patch.object(llm_mod, "_chat", return_value="连接正常"):
            out = llm_mod.test_conn()
        self.assertTrue(out["ok"])
        self.assertIn("latency_ms", out)
        with _env(LLM_API_KEY=""):
            with self.assertRaises(Exception) as cm:
                llm_mod.test_conn()
            self.assertEqual(cm.exception.__dict__.get("status_code"), 400)


class TestAgentMode(unittest.TestCase):
    """智能体模式（2026-09-16 A+C）：工具注册表 + 调用循环（全程打桩，不联网）。"""

    def test_tools_all_readonly_and_runnable(self):
        """9 个工具直跑不抛（真实库只读；行情/健康度打桩）。"""
        from unittest import mock
        with _env(), \
                mock.patch("app.datasource.quote_client.get_quotes", return_value={}), \
                mock.patch("app.web.api.weekly_health", return_value={"level": "healthy"}):
            for name, (fn, _desc) in llm_mod.TOOLS.items():
                with self.subTest(tool=name):
                    out = fn({})                       # 不许抛；返回可序列化
                    json.dumps(out, ensure_ascii=False, default=str)
        self.assertEqual(len(llm_mod._TOOLS_SPEC), len(llm_mod.TOOLS))

    def test_agent_loop_calls_tool_then_answers(self):
        """_run_agent 循环：第一轮返回 tool_calls → 执行工具 → 第二轮给出最终回答。"""
        calls = {"n": 0}

        def fake_post(url, headers=None, json=None, timeout=None):
            class R:
                def raise_for_status(self):
                    pass

                def json(self):
                    calls["n"] += 1
                    if calls["n"] == 1:      # 第一轮：模型决定查工具
                        return {"choices": [{"message": {
                            "content": None,
                            "tool_calls": [{"id": "c1", "function": {
                                "name": "get_overview", "arguments": "{}"}}]}}]}
                    return {"choices": [{"message": {"content": "基于检索的最终回答"}}]}  # 第二轮：收笔
            return R()

        with _env(), mock.patch.object(llm_mod.requests, "post", side_effect=fake_post):
            answer, model, n_calls, at = llm_mod._run_agent("现在什么状态", research=False)
        self.assertEqual(answer, "基于检索的最终回答")
        self.assertEqual(n_calls, 1)
        self.assertEqual(model, "test-model")
        self.assertTrue(at)

    def test_agent_unknown_tool_and_loop_cap(self):
        """未知工具回 error 不炸；连环上限 4 轮封顶。"""
        def fake_post(url, headers=None, json=None, timeout=None):
            class R:
                def raise_for_status(self):
                    pass

                def json(self):
                    return {"choices": [{"message": {
                        "content": None,
                        "tool_calls": [{"id": "cx", "function": {
                            "name": "no_such_tool", "arguments": "{}"}}]}}]}
            return R()

        with _env(), mock.patch.object(llm_mod.requests, "post", side_effect=fake_post):
            answer, _m, n_calls, _at = llm_mod._run_agent("q", research=False)
        self.assertEqual(n_calls, llm_mod._MAX_TOOL_ROUNDS, "每轮 1 次未知工具调用, 触顶退出")

    def test_agent_loop_cap_makes_final_no_tools_call(self):
        """连环检索触顶 ⇒ **不传 tools 收笔一次**，基于已检索数据作答。
        2026-09-29 审查：原实现只返回"（已达连环检索上限，以下为基于已检索数据的回答）"
        占位文案 —— 承诺了"以下"却没有任何下文，用户看到一句自相矛盾的话。"""
        calls = []

        def fake_post(url, headers=None, json=None, timeout=None):
            calls.append(json)

            class R:
                def raise_for_status(self):
                    pass

                def json(self):
                    if len(calls) <= llm_mod._MAX_TOOL_ROUNDS:   # 前 4 轮: 模型一直要调工具
                        return {"choices": [{"message": {
                            "content": None,
                            "tool_calls": [{"id": "cx", "function": {
                                "name": "get_overview", "arguments": "{}"}}]}}]}
                    return {"choices": [{"message": {"content": "已检索数据的综合回答"}}]}
            return R()

        with _env(), mock.patch.object(llm_mod.requests, "post", side_effect=fake_post):
            answer, _m, n_calls, _at = llm_mod._run_agent("q", research=False)
        self.assertEqual(n_calls, llm_mod._MAX_TOOL_ROUNDS)
        self.assertEqual(len(calls), llm_mod._MAX_TOOL_ROUNDS + 1, "触顶后必须补一次收笔调用")
        self.assertNotIn("tools", calls[-1], "收笔调用不许再提供工具(否则永远收不了笔)")
        self.assertEqual(answer, "已检索数据的综合回答")

    def test_agent_loop_cap_fallback_on_final_failure(self):
        """收笔调用失败 ⇒ 退回**如实描述**的提示文案（不许抛、不许再承诺没有的下文）。"""
        calls = []

        def fake_post(url, headers=None, json=None, timeout=None):
            calls.append(json)

            class R:
                def raise_for_status(self):
                    if len(calls) > llm_mod._MAX_TOOL_ROUNDS:
                        raise RuntimeError("network down")

                def json(self):
                    return {"choices": [{"message": {
                        "content": None,
                        "tool_calls": [{"id": "cx", "function": {
                            "name": "get_overview", "arguments": "{}"}}]}}]}
            return R()

        with _env(), mock.patch.object(llm_mod.requests, "post", side_effect=fake_post):
            answer, _m, n_calls, _at = llm_mod._run_agent("q", research=False)
        self.assertEqual(n_calls, llm_mod._MAX_TOOL_ROUNDS)
        self.assertIn("已达连环检索上限", answer)
        self.assertNotIn("以下为", answer, "不许再承诺没有下文的'以下'")

    def test_ask_agent_mode_flag(self):
        """agent_mode=True 走 _run_agent；False 走快照路径（_chat 打桩）。"""
        with _env(), mock.patch.object(llm_mod, "_run_agent",
                                       return_value=("agent 答", "m", 3, "t")):
            out = llm_mod.ask(llm_mod.AskBody(question="q", agent_mode=True, history=[]))
        self.assertEqual(out["answer"], "agent 答")
        self.assertEqual(out["tool_calls"], 3)
        self.assertTrue(out["agent"])

    def test_history_is_actually_consumed(self):
        """多轮记忆：历史问答必须真的进入 messages。
        2026-09-18 审查：_run_agent 收下了 history 参数但函数体从不使用 ⇒ 追问失忆。"""
        seen = []

        def fake_post(url, headers=None, json=None, timeout=None):
            seen.append(json)

            class R:
                def raise_for_status(self):
                    pass

                def json(self):
                    return {"choices": [{"message": {"content": "答"}}]}
            return R()

        hist = [{"role": "q", "text": "002674 最近怎么样"},
                {"role": "a", "text": "有股东减持"},
                {"role": "q", "text": "那它所在行业呢"},
                {"role": "a", "text": "纺织服饰"}]
        with _env(), mock.patch.object(llm_mod.requests, "post", side_effect=fake_post):
            llm_mod._run_agent("为什么减持是坏消息", research=False, history=hist)
        msgs = seen[0]["messages"]
        self.assertEqual([m["role"] for m in msgs],
                         ["system", "user", "assistant", "user", "assistant", "user"],
                         "历史必须按 user/assistant 顺序进 messages，当前问题在最后")
        self.assertIn("002674 最近怎么样", [m["content"] for m in msgs])
        self.assertIn("有股东减持", [m["content"] for m in msgs])
        self.assertEqual(msgs[-1]["content"], "为什么减持是坏消息")

    def test_history_messages_caps_and_truncates(self):
        """上限在后端兜底：最多 6 条；单条截断 400 字（前端只做 slice(-6)，没有长度截断）。"""
        hist = []
        for i in range(10):
            hist.append({"role": "q", "text": "q%d" % i})
            hist.append({"role": "a", "text": "a%d" % i})
        msgs = llm_mod._history_messages(hist)
        self.assertEqual(len(msgs), 6, "最多取 6 条")
        self.assertEqual(msgs[-1]["content"], "a9")
        long_hist = [{"role": "q", "text": "x" * 900}, {"role": "a", "text": "   "}]
        out = llm_mod._history_messages(long_hist)
        self.assertEqual([m["role"] for m in out], ["user"], "空白历史必须跳过")
        self.assertEqual(len(out[0]["content"]), 400, "单条必须截断到 400 字")


class TestNoRuntimeModulePatching(unittest.TestCase):
    """⑥ `app/` 运行时代码里**不许** import `unittest.mock`（2026-09-19 审查 A3）。

    在 HTTP 请求处理函数里 `mock.patch("某个模块属性", …)` 是**进程级**改动：长驻服务
    并发处理别的请求时会被一起打桩，而 `patch` 的 enter/exit 不抗并发 —— 嵌套退出后
    桩能永久留在进程里。`app/web/llm.py` 曾用它跳过实时行情，现在换成
    `quote_client.suppress_live_quotes()`（ContextVar，请求级）。
    需要"这次换个实现"就把它写成**显式参数**或**上下文开关**，别用测试工具改生产对象。
    """

    def test_no_unittest_import_under_app(self):
        import ast
        hits = []
        for p in sorted((BASE / "app").rglob("*.py")):
            tree = ast.parse(p.read_text(encoding="utf-8"), filename=str(p))
            for n in ast.walk(tree):
                if isinstance(n, ast.ImportFrom) and (n.module or "").split(".")[0] == "unittest":
                    hits.append(f"{p.relative_to(BASE)}:{n.lineno} from {n.module} import …")
                elif isinstance(n, ast.Import):
                    for a in n.names:
                        if a.name.split(".")[0] == "unittest":
                            hits.append(f"{p.relative_to(BASE)}:{n.lineno} import {a.name}")
        self.assertFalse(
            hits, "app/ 里出现了运行时 mock（进程级替身，会污染并发请求）：\n  "
                  + "\n  ".join(hits))

    def test_llm_snapshot_uses_the_request_scoped_switch(self):
        src = (BASE / "app" / "web" / "llm.py").read_text(encoding="utf-8")
        self.assertIn("suppress_live_quotes()", src,
                      "llm.py 又回到 mock.patch 了？请改用 quote_client.suppress_live_quotes()")


if __name__ == "__main__":
    unittest.main()
