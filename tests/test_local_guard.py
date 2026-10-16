"""`main.local_guard` 中间件 —— 本机访问防护（2026-09-13，独立审查 §4.1）

CLAUDE.md §6 自己承认这块**零覆盖**。它守三件事，每一件漏了都是真实风险：

1. **Host 白名单** ⇒ 阻断 DNS rebinding（恶意页面把自己的域名解析到 127.0.0.1，
   再借浏览器同源策略去打本地服务）；
2. **写请求同源校验** ⇒ 恶意网页发起的跨站"简单请求"（表单 POST）必须被挡；
   非浏览器客户端（curl/脚本）须带 `X-Neweco-Client` 头；
3. **安全响应头**（CSP / X-Frame-Options / nosniff / Cache-Control）—— 含 **403 早退响应
   也要带**（2026-09-05 修过一次：原只有放行路径附加）。

用法说明：`TestClient` **不进 lifespan**（否则会真的拉起调度器）；这些断言只关心中间件。
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from fastapi.testclient import TestClient                       # noqa: E402

from app.main import _SEC_HEADERS, app                          # noqa: E402

_CONFIG = BASE / "config.yaml"


class _NoProdConfigWrite(unittest.TestCase):
    """本模块的写请求会**真的走进端点函数**（中间件测试必须如此），于是
    `POST /api/settings` 会调 `cfg_mod.save_config` —— 不挡住就是
    "**测试改写生产 config.yaml**"（2026-09-13 实证：全量测试把 `fees.transfer_fee`
    的类型从 float 写成字符串，见 07 号 #21）。

    ⇒ 挡住写盘，并在**每个用例结束后按字节比对**：文件必须一个字节都没动。
    """

    def setUp(self):
        self._before = _CONFIG.read_bytes()
        self._p = mock.patch("app.web.api.cfg_mod.save_config")
        self._p.start()
        self.addCleanup(self._p.stop)

    def tearDown(self):
        self.assertEqual(_CONFIG.read_bytes(), self._before,
                         "测试改写了生产 config.yaml（对外副作用）")


def _client() -> TestClient:
    # base_url 必须是白名单内的 host —— TestClient 默认 `http://testserver`，
    # 那会被 Host 白名单当场 403（中间件本身工作正常，是测试没走对地址）。
    # 不加 `with` ⇒ 不触发 lifespan(不启动调度器)。
    return TestClient(app, base_url="http://127.0.0.1:8787")


class TestHostAllowlist(unittest.TestCase):
    def test_foreign_host_is_rejected_with_sec_headers(self):
        r = _client().get("/api/overview", headers={"Host": "evil.example.com"})
        self.assertEqual(r.status_code, 403)
        # 403 早退路径也必须带安全头（曾经只有放行路径带）
        for k, v in _SEC_HEADERS.items():
            self.assertEqual(r.headers.get(k), v, f"403 响应缺安全头 {k}")

    def test_localhost_and_loopback_pass(self):
        for host in ("127.0.0.1", "localhost", "127.0.0.1:8787"):
            with self.subTest(host=host):
                r = _client().get("/api/overview", headers={"Host": host})
                self.assertNotEqual(r.status_code, 403, "本机 Host 不该被拦")


class TestWriteOriginGuard(_NoProdConfigWrite):
    def test_cross_origin_post_is_rejected(self):
        r = _client().post("/api/settings", json={"capital": 20000},
                           headers={"Origin": "http://evil.example.com"})
        self.assertEqual(r.status_code, 403)
        self.assertIn("cross-origin", r.json()["detail"])

    def test_same_origin_post_is_allowed(self):
        r = _client().post("/api/settings", json={},
                           headers={"Origin": "http://127.0.0.1:8787"})
        self.assertNotEqual(r.status_code, 403, "同源写请求不该被拦")

    def test_script_without_origin_needs_client_header(self):
        bare = _client().post("/api/settings", json={})
        self.assertEqual(bare.status_code, 403)
        self.assertIn("X-Neweco-Client", bare.json()["detail"])
        withhdr = _client().post("/api/settings", json={},
                                 headers={"X-Neweco-Client": "web"})
        self.assertNotEqual(withhdr.status_code, 403, "带显式客户端头应放行")

    def test_referer_also_counts_as_same_origin(self):
        r = _client().post("/api/settings", json={},
                           headers={"Referer": "http://localhost:8787/index.html"})
        self.assertNotEqual(r.status_code, 403, "同源 Referer 应放行")


class TestSecurityHeadersOnSuccess(unittest.TestCase):
    def test_get_carries_sec_headers(self):
        r = _client().get("/api/overview")
        self.assertNotEqual(r.status_code, 403)
        for k, v in _SEC_HEADERS.items():
            self.assertEqual(r.headers.get(k), v, f"正常响应缺安全头 {k}")


if __name__ == "__main__":
    unittest.main()
