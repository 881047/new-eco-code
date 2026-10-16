"""Tushare 探测缓存单测: 权限降级落盘/条目续期 (2026-09-04 审查修复)

运行: python -m unittest tests.test_tushare_probe -v
"""
import datetime
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.datasource import tushare_client as tc_mod


class _ClientBase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        tmp.close()
        self.cache_path = Path(tmp.name)
        self.patcher = mock.patch.object(tc_mod, "PROBE_CACHE", self.cache_path)
        self.patcher.start()
        # 不走 __init__(避免真实 token/pro_api)
        self.tc = tc_mod.TushareClient.__new__(tc_mod.TushareClient)
        self.tc.pro = mock.Mock()
        self.tc.min_interval = 0.0
        self.tc._last = 0.0
        self.tc._probe = {}

    def tearDown(self):
        self.patcher.stop()
        if self.cache_path.exists():
            self.cache_path.unlink()

    def _cache(self):
        return json.loads(self.cache_path.read_text(encoding="utf-8"))


class TestPermPersist(_ClientBase):
    def test_perm_error_cached_as_unavailable(self):
        # 注意: 文案不能含「抱歉/每分钟」等限流词(限流文案优先判定)
        self.tc.pro.query.side_effect = Exception("您没有该接口访问权限，积分不足")
        out = self.tc.call("fina_indicator", period="20231231")
        self.assertTrue(out.empty)
        self.assertIs(self.tc._probe["fina_indicator"], False)
        today = datetime.date.today().isoformat()
        self.assertEqual(self._cache()["fina_indicator"], [False, today])

    def test_perm_does_not_restamp_other_entries(self):
        # 另一接口 3 天前的探测记录不得因本次保存被续期
        old = (datetime.date.today() - datetime.timedelta(days=3)).isoformat()
        self.cache_path.write_text(json.dumps({"daily": [True, old]}), encoding="utf-8")
        self.tc.pro.query.side_effect = Exception("权限不足")
        self.tc.call("fina_indicator", period="20231231")
        self.assertEqual(self._cache()["daily"], [True, old])  # 原日期保留

    def test_available_returns_cached_false_without_calling(self):
        self.tc._probe["fina_indicator"] = False
        self.assertFalse(self.tc.available("fina_indicator", period="20231231"))
        self.tc.pro.query.assert_not_called()

    def test_perm_message_without_rate_keyword_is_perm(self):
        # 2026-09-05 审查修复: "抱歉"从限流文案移除(权限错误恰以"抱歉"开头, 原被误判
        # 限流空耗 6 次重试约81秒后炸断管线); 混合文案无明确限流关键词("每分钟最多")
        # 时按永久错误判定, 不重试、记缓存为不可用
        self.tc.pro.query.side_effect = Exception("抱歉，积分不足将每分钟限制访问")
        with mock.patch.object(tc_mod.time, "sleep"):
            df = self.tc.call("daily", trade_date="20260904")
        self.assertTrue(df.empty)
        self.assertEqual(self.tc.last_status, "perm")
        self.assertEqual(self.tc.pro.query.call_count, 1)  # 不重试
        self.assertIs(self.tc._probe["daily"], False)

    def test_pure_rate_message_still_retries(self):
        # "每分钟最多"仍是限流关键词: 重试 6 次后 RuntimeError
        self.tc.pro.query.side_effect = Exception("每分钟最多访问次数限制")
        with mock.patch.object(tc_mod.time, "sleep"):
            with self.assertRaises(RuntimeError):
                self.tc.call("daily", trade_date="20260904")
        self.assertEqual(self.tc.pro.query.call_count, 6)

    def test_available_success_saves_only_own_entry(self):
        self.tc.pro.query.return_value = tc_mod.pd.DataFrame({"a": [1]})
        self.assertTrue(self.tc.available("daily", trade_date="20260904"))
        self.assertEqual(list(self._cache().keys()), ["daily"])


if __name__ == "__main__":
    unittest.main()
