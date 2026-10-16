"""Web 看板「周频袖套」卡片的接口守卫（2026-09-13）

为什么要有这个文件: 卡片是本项目第一次把周频袖套暴露到网络层, 而历史上多次出现
「看着装好了, 实际不工作」(端点没挂上 / 字段名对不上 / NaN 让序列化 500)。
这里把三件最容易静默坏掉的事钉死:

1. 路由真的挂在 app 上（不是只写了个函数）；
2. 返回体对 FastAPI 的 JSON 契约合规（`allow_nan=False`, 即 NaN/Inf 会 500）,
   且方案行的字段名与前端逐字一致（少了就是页面上显示 undefined）；
3. **只读**：调完接口不得改动/新建 data/weekly 下任何文件（不能因为看一眼就建仓）。

运行: .venv\\Scripts\\python.exe -X utf8 -m pytest tests/test_web_weekly.py -q
"""
import hashlib
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from app.main import app  # noqa: E402
from app.web import api  # noqa: E402
from app.weekly import health as wk_health  # noqa: E402

WEEKLY_DIR = BASE / "data" / "weekly"
# 前端 app.js 的 loadWeekly() 逐字读这些键, 少一个就是 undefined
PLAN_ROW_KEYS = {"ts_code", "name", "rank", "score", "shares", "close", "weight", "held"}


class TestPlanPayloadPrefersLiveExpectation(unittest.TestCase):
    """**预期以实时口径为准**（2026-09-13 自查评审）：

    方案文件里那份 expectation 是"写方案当时"的快照 —— 若之后改了 `weekly.equity_weight`，
    或修了口径标注（如"数值来自 F52、标签还写 F25"），看板不能继续挂旧的那份。
    现在 API 用 `expectation_at(当前配置)` 覆盖显示值，快照挪到 `expectation_at_plan_time` 溯源。
    """

    PLAN = {"anchor": "20260911", "capital": 1_000_000.0, "top_n": 100, "band": 8.0,
            "n_target": 2, "n_held": 0, "invested": 12345.0, "cash_left": 987655.0,
            "invested_ratio": 0.0123, "dropped": {"no_price": 0, "unaffordable": 1},
            "rows": [{"ts_code": "600000.SH"}, {"ts_code": "000001.SZ"}],
            "expectation": {"source": "F25 实测点 w=1", "excess_pp": 17.63}}

    def test_live_expectation_wins_and_snapshot_is_kept(self):
        live = {"source": "F52 实测点 w=1.00（生产构型 hold_bonus=0.0…）", "excess_pp": 17.63}
        out = api._plan_payload(self.PLAN, live)
        self.assertEqual(out["expectation"], live, "显示值必须是实时口径")
        self.assertEqual(out["expectation_at_plan_time"]["source"], "F25 实测点 w=1",
                         "方案当时的快照要保留作溯源，不能丢")

    def test_payload_keeps_the_fields_the_frontend_reads(self):
        out = api._plan_payload(self.PLAN, {})
        for key in ("anchor", "capital", "top_n", "band", "n_target", "n_held",
                    "invested", "cash_left", "invested_ratio", "dropped",
                    "expectation", "expectation_at_plan_time"):
            self.assertIn(key, out, f"看板字段缺失: {key}")
        self.assertEqual(out["n_rows_total"], 2)
        self.assertEqual(len(out["rows"]), 2)


def _fingerprint():
    out = {}
    for p in sorted(WEEKLY_DIR.glob("*")):
        if p.is_file():
            out[p.name] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


class TestWeeklyRoutesRegistered(unittest.TestCase):
    """用 FastAPI 自己的 OpenAPI 规格做契约 —— app.routes 只含一个 _IncludedRouter
    包装对象(本版 FastAPI 不把子路由摊平), 直接读它反而测不到真实的服务面。"""

    @classmethod
    def setUpClass(cls):
        cls.spec = app.openapi()

    def test_routes_mounted(self):
        self.assertIn("/api/weekly", self.spec["paths"])
        self.assertIn("/api/weekly/health", self.spec["paths"])

    def test_weekly_route_takes_no_request_body(self):
        """`/api/weekly` 是**无参 GET**：契约里不许出现 requestBody / 必填 body 参数。

        2026-09-13 自查评审实测踩到：把 helper 插到 `@router.get("/weekly")` 与
        `def weekly()` 之间 ⇒ 装饰器挂到了 helper 上，端点变成"要 body 的 _plan_payload"
        （线上直接 422）。原来的 `test_routes_mounted` 只查路径在不在 ⇒ **测不出**。
        """
        get = self.spec["paths"]["/api/weekly"]["get"]
        self.assertNotIn("requestBody", get, "这个端点不该要求请求体（否则线上就是 422）")
        self.assertEqual([p for p in get.get("parameters", []) if p.get("required")], [],
                         "无参 GET 不该有必填参数")

    def test_health_window_is_bounded(self):
        """window 是外部可传参, 不设上限就是一个能被拉长的 SQL 循环。"""
        params = self.spec["paths"]["/api/weekly/health"]["get"]["parameters"]
        w = next(p for p in params if p["name"] == "window")["schema"]
        self.assertEqual((w["minimum"], w["maximum"]), (1, 52))


class TestWeeklyPayload(unittest.TestCase):
    def test_payload_shape_and_json_contract(self):
        out = api.weekly()
        self.assertIn("enabled", out)
        self.assertNotIn("plan_error", out)
        self.assertNotIn("kpi_error", out)
        # FastAPI 用 allow_nan=False 序列化 ⇒ 这里用同一契约复现它的 500
        json.dumps(out, allow_nan=False, ensure_ascii=False)

    def _contract(self, plan: dict, label: str):
        """前端 loadWeekly() 逐字读的那 8 个键 + 截断上限 + 总数一致性。"""
        self.assertTrue(plan["rows"], f"{label}: 方案行不应为空")
        self.assertTrue(PLAN_ROW_KEYS.issubset(plan["rows"][0]),
                        f"{label}: 缺字段 {PLAN_ROW_KEYS - set(plan['rows'][0])}")
        self.assertLessEqual(len(plan["rows"]), 20, f"{label}: 展示截断上限变了要同步前端文案")
        on_disk = json.loads(Path(plan["file"]).read_text(encoding="utf-8"))["rows"]
        self.assertEqual(plan["n_rows_total"], len(on_disk),
                         f"{label}: n_rows_total 与文件里的行数不一致（前端'显示前 N / 共 M'会撒谎）")

    def test_plan_rows_match_frontend_contract(self):
        """**新检出/CI 上也必须真跑**（2026-09-20 改掉原来的 skipTest）。

        以前"本地没跑过 plan 就跳过" ⇒ `data/weekly/` 被 gitignore（.gitignore:24），
        于是 CI 上这条契约**从来没执行过**，而它盯的正是"后端改了字段名而前端拿 undefined、
        页面那块静默空白"这一类无声故障（07 #26/#28 同族）。现在用合成产物无条件跑，
        真库里有历史方案时再顺手把**真实产物**也过一遍同一契约。
        """
        from app.weekly import plan as wk_plan

        tmp = Path(tempfile.mkdtemp(prefix="plan_contract_"))
        rows = [{"ts_code": f"6000{i:02d}.SH", "name": f"N{i}", "rank": i + 1,
                 "score": 90.0 - i, "shares": 100 * (i + 1), "close": 10.0 + i,
                 "weight": 0.01, "held": False} for i in range(25)]
        payload = {"anchor": "20991231", "capital": 1_000_000.0, "top_n": 100, "band": 8.0,
                   "n_target": 25, "n_held": 0, "invested": 12345.0, "cash_left": 987655.0,
                   "invested_ratio": 0.0123, "dropped": {"no_price": 0, "unaffordable": 1},
                   "rows": rows, "expectation": {"source": "F52", "excess_pp": 17.63}}
        art = tmp / "plan_20991231.json"
        art.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        with mock.patch.object(wk_plan, "PLAN_DIR", tmp):
            plan = api.weekly().get("plan")
            self.assertIsNotNone(plan, "指向有产物的目录却没返回 plan 块")
            plan["file"] = str(art)
            self._contract(plan, "合成产物")

        real = sorted(WEEKLY_DIR.glob("plan_*.json")) if WEEKLY_DIR.exists() else []
        for p in real[-1:]:                      # 只复查最新那份真实产物（省时且最容易坏）
            with mock.patch.object(wk_plan, "PLAN_DIR", p.parent):
                rp = api.weekly().get("plan") or {}
                rp["file"] = str(p) if rp else None
            if rp.get("rows"):
                self._contract(rp, f"真实产物 {p.name}")
        shutil.rmtree(tmp, ignore_errors=True)

    def test_payload_has_ops_status(self):
        """看板要能一眼看出"下个执行档什么时候、今天该不该管"（与 doctor 同源）。"""
        out = api.weekly()
        ops = out.get("ops") or {}
        for k in ("today", "next_exec", "next_anchor", "exec_ok_today", "exec_detail"):
            self.assertIn(k, ops, f"运维状态缺 {k}")
        json.dumps(out, allow_nan=False, ensure_ascii=False)   # 仍要过 FastAPI 的 JSON 契约

    def test_payload_carries_the_honesty_fields(self):
        """F28/F38 的两条"别自欺"读数必须出现在卡片数据里（老方案文件也要由 API 补上）。"""
        plan = api.weekly().get("plan") or {}
        self.assertIn("expectation_oos", plan, "样本外那一档必须带上")
        self.assertIn("expectation_multiplicity", plan, "多重比较打折必须带上")
        mul = plan["expectation_multiplicity"] or {}
        self.assertFalse(mul.get("pass_95"), "F38 判决是**未通过**，不能被写成通过")

    def test_endpoint_is_read_only(self):
        """看一眼不能产生任何副作用: 文件集合与内容指纹必须一模一样。"""
        before = _fingerprint()
        api.weekly()
        api.weekly_health(window=1)           # 里层有 try/except, 库缺失也只返回 unknown
        self.assertEqual(before, _fingerprint())


class TestWeeklyHealthEndpoint(unittest.TestCase):
    def test_health_payload_levels(self):
        stub = {"n": 12, "ic": 0.09, "last": 0.09, "neg_streak": 0, "path": "stub.npz"}
        with mock.patch.object(wk_health, "rolling_ic", return_value=stub):
            out = api.weekly_health(window=12)
        self.assertIn("level", out)
        self.assertIn(out["level"], wk_health.LEVELS + ("unknown",))
        if out["level"] in wk_health.LEVELS:  # 账本为空时走 unknown 分支, 不报健康度
            self.assertEqual(out["rolling_ic"], 0.09)
            self.assertEqual(out["frozen_ic"], wk_health.FROZEN_IC)

    def test_health_never_raises(self):
        with mock.patch.object(wk_health, "rolling_ic", side_effect=RuntimeError("boom")):
            out = api.weekly_health(window=12)
        self.assertEqual(out["level"], "unknown")
        self.assertIn("boom", out["error"])


if __name__ == "__main__":
    unittest.main()
