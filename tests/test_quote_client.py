"""实时行情客户端(腾讯批量接口)单测: python -m unittest tests.test_quote_client -v"""
import datetime
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.datasource import quote_client as qc
from app.web.api import _merge_live


def _mk_line(code="600000.SH", price="9.42", prev="9.27", pct="1.62",
             open_="9.31", ts="20260904125746", extra_fields=None):
    """构造 v_xxx="1~名称~代码~现价~昨收~今开~...~时间~涨跌~涨跌%~..." 样式的行情行"""
    num, pfx = code.split(".")
    fields = [""] * 50
    fields[0], fields[1], fields[2] = "1", "TEST", num
    fields[3], fields[4], fields[5] = price, prev, open_
    fields[30], fields[31], fields[32] = ts, "0.15", pct
    if extra_fields:
        fields = list(fields) + list(extra_fields)
    return f'v_{pfx.lower()}{num}="' + "~".join(fields) + '";'


def _resp(lines):
    resp = mock.Mock()
    resp.content = "\n".join(lines).encode("gbk")
    resp.raise_for_status = mock.Mock()
    return resp


class TestToTencentSymbol(unittest.TestCase):
    def test_markets(self):
        self.assertEqual(qc.to_tencent_symbol("600000.SH"), "sh600000")
        self.assertEqual(qc.to_tencent_symbol("000001.SZ"), "sz000001")
        self.assertEqual(qc.to_tencent_symbol("830799.BJ"), "bj830799")

    def test_lowercase_suffix(self):
        self.assertEqual(qc.to_tencent_symbol("600000.sh"), "sh600000")

    def test_invalid(self):
        self.assertIsNone(qc.to_tencent_symbol("600000"))
        self.assertIsNone(qc.to_tencent_symbol("123456.XX"))
        self.assertIsNone(qc.to_tencent_symbol(""))
        self.assertIsNone(qc.to_tencent_symbol(None))


class TestParseLine(unittest.TestCase):
    def test_normal_sh(self):
        q = qc._parse_line(_mk_line())
        self.assertEqual(q["ts_code"], "600000.SH")
        self.assertEqual(q["price"], 9.42)
        self.assertEqual(q["prev_close"], 9.27)
        self.assertEqual(q["open"], 9.31)  # 今开(开盘档撮合用)
        self.assertEqual(q["pct_chg"], 1.62)
        self.assertEqual(q["time"], "20260904125746")
        self.assertEqual(q["date"], "20260904")

    def test_missing_open_field(self):
        # 集合竞价前今开为空: 行仍可解析, open=None 由调用方顺延
        q = qc._parse_line(_mk_line(open_=""))
        self.assertEqual(q["price"], 9.42)
        self.assertIsNone(q["open"])
        self.assertIsNone(qc._parse_line(_mk_line(open_="0"))["open"])
        self.assertIsNone(qc._parse_line(_mk_line(open_="abc"))["open"])

    def test_bj(self):
        q = qc._parse_line(_mk_line(code="830799.BJ"))
        self.assertEqual(q["ts_code"], "830799.BJ")

    def test_gbk_garbage_in_name(self):
        # 名称字段含非法 GBK 字节: bytes.decode(errors="ignore") 后数字字段仍可解析
        raw = _mk_line().encode("gbk").replace(b"TEST", b"\xff\xfe\xfe")
        line = raw.decode("gbk", errors="ignore")
        q = qc._parse_line(line)
        self.assertIsNotNone(q)
        self.assertEqual(q["price"], 9.42)

    def test_bad_lines(self):
        for line in ["", "v_sh600000", "v_sh600000=1", 'x_sh600000="1~..."',
                     _mk_line(price="0"), _mk_line(price="-1"),
                     _mk_line(price="nan"), _mk_line(price="abc"),
                     _mk_line(price="inf"), _mk_line(ts=""), _mk_line(ts="2026")]:
            self.assertIsNone(qc._parse_line(line), line)

    def test_too_few_fields(self):
        line = 'v_sh600000="1~TEST~600000~9.42";'  # 4 字段 < 33
        self.assertIsNone(qc._parse_line(line))

    def test_missing_time_field(self):
        # 时间字段为空串 → None
        self.assertIsNone(qc._parse_line(_mk_line(ts="")))
        # 时间非 14 位数字 → None
        self.assertIsNone(qc._parse_line(_mk_line(ts="2026090412574a")))

    def test_no_trailing_semicolon(self):
        line = _mk_line()[:-1]  # 去掉结尾 ;
        self.assertEqual(qc._parse_line(line)["price"], 9.42)


class TestSessionGate(unittest.TestCase):
    def _at(self, wd, hm):
        return datetime.datetime(2026, 9, 4, int(hm[:2]), int(hm[2:]), tzinfo=qc._TZ) + \
            datetime.timedelta(days=(wd - 4))  # 2026-09-04 是周五

    def test_boundaries(self):
        # 周五: 9:24 前/9:25 起/11:35 止/11:36 午休/12:54 午休/12:55 起/15:05 止/15:06 后
        for hm, expect in [("0924", False), ("0925", True), ("1135", True), ("1136", False),
                           ("1254", False), ("1255", True), ("1505", True), ("1506", False)]:
            self.assertIs(qc._in_session(self._at(4, hm)), expect, hm)

    def test_weekend(self):
        for wd in [5, 6]:  # 周六/周日
            self.assertFalse(qc._in_session(self._at(wd, "1000")))

    def test_default_now(self):
        self.assertIsInstance(qc._in_session(), bool)


class TestFetchQuotes(unittest.TestCase):
    def setUp(self):
        today = datetime.datetime.now(qc._TZ).strftime("%Y%m%d")
        yest = (datetime.datetime.now(qc._TZ) - datetime.timedelta(days=1)).strftime("%Y%m%d")
        self.today, self.yest = today, yest

    def _ts(self, d, hms="145730"):
        return d + hms

    def test_partial_valid(self):
        # 999999.SH 是无效代码: 真实接口不返回对应行, mock 响应里也没有
        lines = [_mk_line(code="600000.SH", ts=self._ts(self.today)),
                 _mk_line(code="000001.SZ", ts=self._ts(self.today))]
        with mock.patch.object(qc.requests, "get", return_value=_resp(lines)) as get:
            out = qc.fetch_quotes(["600000.SH", "000001.SZ", "999999.SH"])
        self.assertEqual(sorted(out), ["000001.SZ", "600000.SH"])
        # 请求的符号是排序去重后的
        self.assertEqual(get.call_args[0][0], qc.QUOTE_URL + "sh600000,sh999999,sz000001")

    def test_yesterday_timestamp_dropped(self):
        lines = [_mk_line(code="600000.SH", ts=self._ts(self.yest))]
        with mock.patch.object(qc.requests, "get", return_value=_resp(lines)):
            self.assertIsNone(qc.fetch_quotes(["600000.SH"]))

    def test_empty_codes_no_http(self):
        with mock.patch.object(qc.requests, "get") as get:
            self.assertIsNone(qc.fetch_quotes([]))
            self.assertIsNone(qc.fetch_quotes(["600000"]))  # 缺后缀
            get.assert_not_called()


class TestTTLCache(unittest.TestCase):
    def setUp(self):
        qc._cache.clear()
        qc._live_state[0] = None
        self.today = datetime.datetime.now(qc._TZ).strftime("%Y%m%d")
        self.line = _mk_line(code="600000.SH", ts=self.today + "145730")
        self.clock = [1000.0]

    def _patch(self, raise_exc=None):
        get = mock.Mock(side_effect=raise_exc) if raise_exc else \
            mock.Mock(return_value=_resp([self.line]))
        return mock.patch.object(qc.requests, "get", get), \
            mock.patch.object(qc, "_now", side_effect=lambda: self.clock[0]), \
            mock.patch.object(qc, "_in_session", return_value=True)

    def test_hit_and_expire(self):
        with self._patch()[0] as get, self._patch()[1], self._patch()[2]:
            q1 = qc.get_quotes(["600000.SH"])
            q2 = qc.get_quotes(["600000.SH"])
            self.assertEqual(get.call_count, 1)  # 第二次命中缓存
            self.assertEqual(q2["600000.SH"]["price"], q1["600000.SH"]["price"])
            self.clock[0] += qc.TTL_OK + 1  # 超过成功 TTL → 重新拉取
            qc.get_quotes(["600000.SH"])
            self.assertEqual(get.call_count, 2)

    def test_failure_cooldown(self):
        exc = mock.Mock(side_effect=Exception("boom"))
        with self._patch(exc)[0] as get, self._patch()[1], self._patch()[2]:
            self.assertIsNone(qc.get_quotes(["600000.SH"]))
            self.assertIsNone(qc.get_quotes(["600000.SH"]))  # 30s 冷却内不再外呼
            self.assertEqual(get.call_count, 1)
            self.clock[0] += qc.TTL_FAIL + 1
            qc.get_quotes(["600000.SH"])
            self.assertEqual(get.call_count, 2)

    def test_out_of_session_no_http(self):
        with self._patch()[0] as get, self._patch()[1], \
                mock.patch.object(qc, "_in_session", return_value=False):
            self.assertIsNone(qc.get_quotes(["600000.SH"]))
            get.assert_not_called()

    def test_logged_once_on_state_change(self):
        # 失败→恢复只各记一次日志, 不随轮询刷屏
        with self._patch(Exception("boom"))[0], self._patch()[1], self._patch()[2]:
            qc.get_quotes(["600000.SH"])
            qc.get_quotes(["600000.SH"])
        qc._live_state[0] = None
        with self.assertLogs(qc.log.name, level="INFO") as cm, \
                self._patch()[0], self._patch()[1], self._patch()[2]:
            qc._cache.clear()  # 清缓存强制走外呼
            qc.get_quotes(["600000.SH"])
            qc.get_quotes(["600000.SH"])
        infos = [r for r in cm.output if "实时行情恢复" in r]
        self.assertEqual(len(infos), 1)


class TestMergeLive(unittest.TestCase):
    def _pos(self, code="600000.SH", shares=100, avg=9.0, close=9.3, pnl=30.0, pct=3.33):
        return {"ts_code": code, "name": "TEST", "shares": shares, "avg_cost": avg,
                "close": close, "pnl": pnl, "pnl_pct": pct}

    def test_all_live(self):
        positions = [self._pos(shares=100, avg=9.0, close=9.3),
                     self._pos(code="000001.SZ", shares=200, avg=11.0, close=11.5)]
        quotes = {"600000.SH": {"price": 9.42, "pct_chg": 1.62, "time": "20260904125746"},
                  "000001.SZ": {"price": 11.1, "pct_chg": -0.9, "time": "20260904125740"}}
        mv, qa = _merge_live(positions, quotes)
        self.assertEqual(mv, round(100 * 9.42 + 200 * 11.1, 2))
        self.assertEqual(qa, "12:57:46")  # 取最大时间戳
        p0, p1 = positions
        self.assertEqual(p0["pnl"], round(100 * (9.42 - 9.0), 2))
        self.assertEqual(p0["pnl_pct"], round((9.42 / 9.0 - 1) * 100, 2))
        self.assertEqual(p0["live_price"], 9.42)
        self.assertEqual(p0["live_pct_chg"], 1.62)
        self.assertEqual(p1["live_pct_chg"], -0.9)

    def test_mixed_fallback_to_close(self):
        # 无实时行情的持仓也改用原价收盘口径(2026-09-04 统一), 不再混入后复权口径
        positions = [self._pos(shares=100, avg=9.0, close=9.3, pnl=30.0, pct=3.33),
                     self._pos(code="000001.SZ", shares=200, avg=11.0, close=11.5, pnl=999.0, pct=4.55)]
        quotes = {"600000.SH": {"price": 9.42, "pct_chg": 1.62, "time": "20260904125746"}}
        mv, qa = _merge_live(positions, quotes)
        self.assertEqual(mv, round(100 * 9.42 + 200 * 11.5, 2))  # 无行情持仓按原价收盘计市值
        self.assertEqual(positions[1]["pnl"], round(200 * (11.5 - 11.0), 2))  # 重算为原价口径
        self.assertNotIn("live_price", positions[1])

    def test_avg_cost_none_keeps_pnl(self):
        positions = [self._pos(avg=None, close=9.3, pnl=None, pct=None)]
        quotes = {"600000.SH": {"price": 9.42, "pct_chg": 1.62, "time": "20260904125746"}}
        _merge_live(positions, quotes)
        self.assertIsNone(positions[0]["pnl"])
        self.assertIsNone(positions[0]["pnl_pct"])

    def test_empty_quotes(self):
        positions = [self._pos()]
        mv, qa = _merge_live(positions, {})
        self.assertEqual(mv, 930.0)  # 原价收盘口径
        self.assertIsNone(qa)

    def test_no_time_keeps_quoted_at_none(self):
        positions = [self._pos()]
        quotes = {"600000.SH": {"price": 9.42, "pct_chg": None, "time": None}}
        mv, qa = _merge_live(positions, quotes)
        self.assertIsNone(qa)
        self.assertIsNone(positions[0]["live_pct_chg"])


class TestSuppressLiveQuotes(unittest.TestCase):
    """2026-09-19 审查 A3：「这次不要外呼」必须是**请求级**开关，不是进程级替身。

    原 `app/web/llm.py` 用 `unittest.mock.patch("…quote_client.get_quotes", return_value={})`
    来跳过实时行情 —— 那是**改模块属性**：
      · uvicorn 线程池并发服务，同一时刻前端 `/api/paper` 轮询会被一起拔掉实时价；
      · `mock.patch` 的 enter/exit 不抗并发，两次嵌套会让后进入者把**已打桩的**实现当成
        "原值"还原 ⇒ 桩**永久留在进程里**，直到重启都取不到实时价（静默）。
    换成 ContextVar 后：出上下文即恢复，且对新起的线程不生效（≈ 对其他请求不生效）。
    """

    def setUp(self):
        self.calls = []
        self.saved_cache = dict(qc._cache)
        qc._cache.clear()
        patchers = [
            mock.patch.object(qc, "_in_session", return_value=True),
            mock.patch.object(qc, "fetch_quotes", side_effect=self._fetch),
        ]
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)

    def _fetch(self, codes):
        self.calls.append(list(codes))
        return {"600000.SH": {"price": 9.42, "pct_chg": 1.62, "time": "20260904125746"}}

    def test_suppressed_skips_the_network(self):
        with qc.suppress_live_quotes():
            self.assertEqual(qc.get_quotes(["600000.SH"]), {})
        self.assertEqual(self.calls, [], "抑制期内不许外呼")

    def test_restored_once_context_exits(self):
        with qc.suppress_live_quotes():
            pass
        qc.get_quotes(["600000.SH"])
        self.assertEqual(len(self.calls), 1, "出上下文必须恢复外呼（否则又是常驻桩）")

    def test_concurrent_thread_is_not_suppressed(self):
        """别的线程（= 并发的另一个请求）照常拿实时价 —— 这条是 patch 方案做不到的。"""
        import threading
        with qc.suppress_live_quotes():
            out = {}
            t = threading.Thread(target=lambda: out.update(
                quotes=qc.get_quotes(["600000.SH"])))
            t.start()
            t.join(timeout=10)
        self.assertEqual(len(out["quotes"]), 1, "并发请求被一起打成了无实时价")
        self.assertEqual(len(self.calls), 1)

    def test_nested_suppression_unwinds_completely(self):
        """嵌套：外层仍在上下文内 ⇒ 继续抑制（对的）；**全部退出后必须彻底恢复**（A3 漏的那半）。"""
        with qc.suppress_live_quotes():
            with qc.suppress_live_quotes():
                self.assertEqual(qc.get_quotes(["600000.SH"]), {})
            self.assertEqual(qc.get_quotes(["600000.SH"]), {},
                             "外层还没退出就恢复 ⇒ 把「要省的这次外呼」又打开了")
        self.assertEqual(len(qc.get_quotes(["600000.SH"]) or {}), 1,
                         "两层都退出后仍抑制 ⇒ 正是 mock.patch 嵌套留下的常驻桩")

    def tearDown(self):
        qc._cache.clear()
        qc._cache.update(self.saved_cache)


if __name__ == "__main__":
    unittest.main()
