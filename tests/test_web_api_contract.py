"""看板**所有读接口**的契约守卫（2026-09-14，KB 14 号"关键路径测试第三批"）

为什么要有它：`tests/test_web_weekly.py` 只盯 `/api/weekly`，而首页有 **12 个读接口**。
历史上这一层的坏法都很安静（07 号 #26/#28 就是"显示值 ≠ 来源"与"端点没挂上"）：
  · 后端改了 / 删了字段名 ⇒ 前端拿 `undefined`，页面那块只剩空白，console 里才看得到；
  · 返回值里混进 NaN/Inf ⇒ FastAPI 用 `allow_nan=False` 序列化，整个请求 **500**；
  · 前端改了 URL 而路由没有 ⇒ 同上。
这里钉三件事（都在**不启动服务、不联网**的前提下做）：
  1. **前端调用的每个 URL 都真的挂在 app 上**（从 `app.js` 正则抽 URL，与 OpenAPI paths 比对）；
  2. 每个读接口**返回该接口被前端逐字读取的顶层字段**，且**过得了 FastAPI 的 JSON 契约**
     （`json.dumps(..., allow_nan=False)` —— 这就是 500 的复现条件）；
  3. **只读**：跑完一轮读接口不许改动 `data/weekly/**`（指纹比对，与 `test_web_weekly.py` 同法）。

字段清单来源：`app/web/static/app.js` 各 loader 里逐字读的键（例如 `o.state`、`d.rows`），
不是凭印象写的 —— 改前端读别的键时，这里同步改。

运行: python -m pytest tests/test_web_api_contract.py -q
"""
import json
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from app.main import app
from app.web import api
from app.web import llm as llm_mod

APP_JS = BASE / "app" / "web" / "static" / "app.js"   # 旧版单文件（已删，仅存档路径常量）
FRONTEND_SRC = BASE / "frontend" / "src"
WEEKLY_DIR = BASE / "data" / "weekly"

# endpoint → 调用方式 → 前端逐字读的顶层字段（不要求全给，只要求这些在）
READ_ENDPOINTS = {
    "/api/overview": (lambda: api.overview(),
                      ("state", "data_date", "plan_date", "state_score",
                       "backtest", "equity_weight")),
    "/api/signal": (lambda: api.signal(), ("rows", "history")),
    "/api/style": (lambda: api.style(lookback=60), ("available",)),
    "/api/lhb": (lambda: api.lhb(), ("rows", "source")),
    "/api/paper": (lambda: api.paper(),
                   ("paused", "created_at", "positions", "orders", "trades",
                    "nav", "cash", "market_value", "total", "is_live")),
    "/api/nav": (lambda: api.nav(), ("rows", "run_date")),
    "/api/plan": (lambda: api.plan(), ("rows",)),
    "/api/scores?limit=30": (lambda: api.scores(limit=30), ("date", "rows")),
    "/api/importance": (lambda: api.importance(), ("rows", "train_date")),
    "/api/status": (lambda: api.status(), ("rows", "open_days_since_daily")),
    "/api/notify": (lambda: api.notify_log(), ("rows",)),
    "/api/settings": (lambda: api.get_settings(),
                      ("capital", "risk_profile", "max_stocks", "industry_max_weight",
                       "single_side_cost", "email")),
    # 2026-09-14：周频三端点原先**不在**这张表里 ⇒ `/api/weekly/health` 的
    # `turnover_est=NaN`（有买无卖的首个周期）一路走到线上才炸出 500（07 #46）。
    # 补进来之后，"载荷过不了 allow_nan=False" 这条类级守卫就是 15 个读接口全覆盖。
    "/api/weekly": (lambda: api.weekly(),
                    ("enabled", "kpi", "ops", "plan", "positions", "trades",
                     "n_positions_total", "n_trades_total")),
    "/api/weekly/health": (lambda: api.weekly_health(window=12), ("level",)),
    "/api/weekly/attrib": (lambda: api.weekly_attrib(), ("available",)),
    # 2026-09-16 前端重构：LLM 网关三端点（ask 是 POST 不在此表；这两个只读不外呼）
    "/api/llm/status": (lambda: llm_mod.status(), ("configured",)),
    "/api/llm/factor_review": (lambda: llm_mod.factor_review(), ("available",)),
    # 2026-09-18 审查：该端点曾**静默只返回前 120 只持仓**（界面看不出被截断），
    # 现在显式给 n_total/truncated —— 这条把"截断必须说出来"钉在契约上。
    # 同日 Q-02：截断透明了但**失败**不透明（逐股 `except: continue` 无计数），
    # 整接口全失败会塌缩成空结果、被前端渲染成「无拥挤风险」⇒ 补 n_failed。
    "/api/kg/positions_story": (lambda: api.kg_positions_story(days=90),
                                ("positions", "days", "n_total", "truncated", "n_failed")),
}


def _weekly_fingerprint() -> dict:
    """data/weekly 下所有文件的 (名字, 大小, mtime_ns) —— 读接口不许碰它们。"""
    out = {}
    if WEEKLY_DIR.exists():
        for p in sorted(WEEKLY_DIR.rglob("*")):
            if p.is_file():
                st = p.stat()
                out[str(p.relative_to(WEEKLY_DIR))] = (st.st_size, st.st_mtime_ns)
    return out


class TestFrontendRouteCoverage(unittest.TestCase):
    def test_frontend_calls_only_mounted_routes(self):
        """从**前端源码树**（frontend/src/**，2026-09-16 起 Vue 多文件）抽
        `getJSON("/api/...")` 的 URL，逐个对 OpenAPI 的 paths。

        红了说明：前端改了路径而后端没有（或后端删了路由）⇒ 页面对应区块**静默空白**。
        """
        js = "\n".join(p.read_text(encoding="utf-8")
                       for p in FRONTEND_SRC.rglob("*") if p.suffix in (".vue", ".js"))
        urls = sorted(set(re.findall(r'getJSON\("(/api/[^"?#]+)', js)))
        self.assertGreaterEqual(len(urls), 12, "从前端源码抽到的接口太少 ⇒ 正则或前端结构变了")
        paths = set(app.openapi()["paths"])
        missing = [u for u in urls if u not in paths]
        self.assertEqual(missing, [], f"前端调了不存在的接口: {missing}")


class TestReadEndpointsContract(unittest.TestCase):
    def test_status_rows_carry_an_explicit_unit(self):
        """2026-09-14：`data_status.rows` 的含义**按表不同**（日线族=天、其余=行）。

        前端原来一律渲染"行" ⇒ `daily: 2`（=重拉 2 个交易日）被读成"只写了 2 行"。
        服务端现在显式给 `unit`，前端按它渲染 —— 这里钉住服务端那一半。
        """
        out = api.status()
        rows = out["rows"]
        self.assertTrue(rows, "data_status 不该是空的")
        for r in rows:
            self.assertIn(r["unit"], ("day", "row"), r)
        units = {r["name"]: r["unit"] for r in rows}
        for t in ("daily", "adj_factor", "daily_basic"):
            if t in units:
                self.assertEqual(units[t], "day", f"{t} 的增量是**天数**")
        for t in ("factor", "fina_indicator"):
            if t in units:
                self.assertEqual(units[t], "row", f"{t} 的增量是**行数**")

    def test_required_keys_and_json_contract(self):
        stub_ic = {"n": 0, "ic": None, "last": None, "neg_streak": 0, "path": "stub.npz"}
        for url, (call, keys) in READ_ENDPOINTS.items():
            with self.subTest(url=url):
                # 行情客户端是**唯一**会外呼的东西 ⇒ 统一打桩，保证测试不联网、可复现
                # 健康度里的滚动 IC 要打桩：否则测试会去读真库（慢且不确定）
                with mock.patch("app.datasource.quote_client.get_quotes", return_value={}), \
                        mock.patch("app.weekly.health.rolling_ic", return_value=stub_ic):
                    out = call()
                self.assertIsInstance(out, dict, f"{url} 返回值不是对象")
                missing = [k for k in keys if k not in out]
                self.assertEqual(missing, [], f"{url} 缺字段 {missing}（app.js 逐字读它们）")
                # FastAPI 用 allow_nan=False 序列化 ⇒ NaN/Inf 会让整个请求 500
                json.dumps(out, allow_nan=False, ensure_ascii=False)

    def test_read_endpoints_are_read_only(self):
        before = _weekly_fingerprint()
        with mock.patch("app.datasource.quote_client.get_quotes", return_value={}):
            for call, _keys in READ_ENDPOINTS.values():
                call()
        self.assertEqual(before, _weekly_fingerprint(),
                         "看一眼接口不该改动 data/weekly 里任何文件（不能因为刷新页面就建仓/换方案）")


class TestPositionsStoryFailureVisible(unittest.TestCase):
    """逐股求值失败必须可数、可查（2026-09-18 审查 Q-02）

    坏法很安静：`app/web/api.py` 的循环是 `except Exception: continue`，无计数无日志。
    任一股失败 ⇒ 该股事件静默消失；**全部**失败（如 `days` 超大让 `kg_story.py:39`
    的 `pd.Timedelta(days=days)` 抛 OverflowError）⇒ 200 + `positions: []`，
    而 `TradingView.vue` 的 crowdRisk 把空列表算成 hotCount=0，拥挤度卡显示
    「无拥挤风险」—— 看板与智能体拿到的是**假象而不是错误**。
    失败与空必须同形可辨（07 号事故台账的同一纪律）。
    """

    CODES = ("000001.SZ", "000002.SZ", "600000.SH")

    def _conn(self):
        conn = mock.MagicMock()
        conn.execute.return_value.fetchall.return_value = [(c,) for c in self.CODES]
        return conn

    @staticmethod
    def _story(code, days=90):
        return {"industry": "银行", "n_events": 1, "crowd": 0.1,
                "events": [{"d": "20260901", "k": "解禁", "t": "x"}], "summary": "s"}

    def test_every_failure_is_counted_in_the_response(self):
        with mock.patch.object(api.db, "get_conn", return_value=self._conn()), \
                mock.patch("app.kg_story.get_stock_story",
                           side_effect=RuntimeError("存档缺该列")) as gm:
            r = api.kg_positions_story(days=90)
        self.assertEqual(r["positions"], [], "全失败不该伪装成「没有拥挤」")
        self.assertGreaterEqual(r["n_failed"], len(self.CODES),
                                "失败数必须报出来（n_failed 缺席 = 静默塌缩回来了）")
        self.assertEqual(r["n_failed"] + len(r["positions"]), min(r["n_total"], 120),
                         "成功 + 失败 必须等于本轮实际尝试的行数")
        self.assertTrue(gm.called)

    def test_partial_failure_keeps_the_good_rows_and_reports_the_bad(self):
        def pick(code, days=90):
            if code == "000002.SZ":
                raise RuntimeError("单股炸")
            return self._story(code, days)
        with mock.patch.object(api.db, "get_conn", return_value=self._conn()), \
                mock.patch("app.kg_story.get_stock_story", side_effect=pick):
            r = api.kg_positions_story(days=90)
        self.assertEqual(r["n_failed"], 1, "部分失败也必须计数")
        self.assertEqual(sorted(p["ts_code"] for p in r["positions"] if p["ts_code"] in self.CODES),
                         ["000001.SZ", "600000.SH"], "一颗老鼠屎不该坏掉整锅")

    def test_failure_is_logged_not_swallowed(self):
        """光有计数不够：运维得能在日志里看到**是哪只**、**为什么**（原来零日志）。"""
        with mock.patch.object(api.db, "get_conn", return_value=self._conn()), \
                mock.patch("app.kg_story.get_stock_story",
                           side_effect=RuntimeError("存档缺该列")), \
                self.assertLogs("app.web.api", level="WARNING") as lg:
            api.kg_positions_story(days=90)
        blob = "\n".join(lg.output)
        self.assertIn("000001.SZ", blob, "日志要点名失败的股")
        self.assertIn("存档缺该列", blob, "日志要带上原因")

    def test_days_is_clamped_server_side(self):
        """`days` 必须在服务端钳住：这是**双出口**（看板启动链 + 智能体工具），
        只钳前端等于没钳。对照同文件 `/weekly/health` 早就用了 `Query(12, ge=1, le=52)`。"""
        from fastapi.testclient import TestClient
        cli = TestClient(app, base_url="http://127.0.0.1:8787")   # 不进 lifespan ⇒ 不起调度器
        for bad in ("0", "-1", "999999999999999999999"):
            r = cli.get(f"/api/kg/positions_story?days={bad}")
            self.assertEqual(r.status_code, 422, f"days={bad} 必须被拒（原来直通 kg_story）")
        self.assertEqual(cli.get("/api/kg/positions_story?days=90").status_code, 200)


class TestNoCwdRelativeDataPaths(unittest.TestCase):
    """2026-09-18 审查 P1-02 的**防复发**守卫

    `app/web/api.py` 曾用 `Path("data/weekly/ledger.json")` —— 依赖进程 cwd。非项目根启动
    （服务/CLI/测试）时 `.exists()` 恒假 ⇒ 袖套持仓在看板里**静默消失**且无报错：空 dict
    与"确实没有持仓"不可区分。同类事故还有 `GET /api/overview 500 FileNotFoundError:
    config.yaml`。

    单点测试只能钉住那一行，钉不住"下次再写一行"。这里改成全量扫描：`app/**` 里不得出现
    以 `data/` 开头的相对路径（要写就用 `db.DATA_DIR` / `ledger.DEFAULT_PATH` 这类锚定
    仓库根的绝对常量）。
    """

    def test_no_relative_data_path_in_app(self):
        # 两种写法都要抓住：`Path("data/weekly/ledger.json")` 与 `Path("data")/"llm"`
        # （后者是 app/web/llm.py 当初的真实写法）
        pat = re.compile(r"""Path\(\s*['"]data(?:['"]|[\\/])""")
        hits = []
        for p in sorted((BASE / "app").rglob("*.py")):
            try:
                s = p.read_text(encoding="utf-8")
            except Exception:
                continue
            for i, line in enumerate(s.splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue          # 注释里引用旧写法（如"原为 Path("data/...")"）不算违规
                if pat.search(line):
                    hits.append(f"{p.relative_to(BASE)}:{i}: {stripped[:80]}")
        self.assertEqual(hits, [],
                         "app/** 里出现了依赖 cwd 的相对 data 路径 —— 非项目根启动会静默读不到"
                         "（用 db.DATA_DIR / ledger.DEFAULT_PATH 等绝对常量）")

    def test_ledger_default_path_is_absolute(self):
        from app.weekly import ledger
        self.assertTrue(ledger.DEFAULT_PATH.is_absolute(),
                        "周频账本路径必须是绝对路径（锚定仓库根）")


if __name__ == "__main__":
    unittest.main()
