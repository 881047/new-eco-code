"""个股详情聚合（app/stock_detail.py）的测试。

数据面用**隔离临时库**（tests/_isolation 已把 db.DB_PATH 指到带全 schema 的空库）：
setUpClass 插入已知 fixture，断言全部锚定合成数据——绝不读写生产库（隔离红线）。
跨账户矩阵部分用合成 payload 注入（不依赖外部账本节奏）。
"""
import unittest

from app import db
from app import stock_detail as sd

CODE = "300621.SZ"


def _payload_with_holding(code=CODE):
    """跨账户矩阵的合成 payload：fterm-w2 持有 + 成交史，其余账户无关。"""
    return {"accounts": [
        {"id": "fterm-w2", "name": "fterm 2万真实档", "model": "fterm", "ok": True,
         "positions": [{"code": code, "name": None, "shares": 400.0, "cost": 7.11,
                        "last": 7.10, "value": 2840.0}],
         "trades": [{"date": "2026-09-28", "code": code, "side": "buy", "shares": 400,
                     "price": 7.11, "amount": 2844.0, "reason": "目标调入"}],
         "extra": {}},
        {"id": "ecow2-k6", "name": "ECO-W2 B0-k6", "model": "ECO-W2", "ok": True,
         "positions": [], "trades": [], "extra": {}},
        {"id": "ecow2-k8", "name": "ECO-W2 B0-k8", "model": "ECO-W2", "ok": True,
         "positions": [], "trades": [], "extra": {}},
    ], "totals": {}}


class TestStockDetail(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with db.conn_ctx() as conn:
            conn.execute("INSERT OR REPLACE INTO stock_basic (ts_code, symbol, name, area, industry,"
                         " market, list_date) VALUES (?,?,?,?,?,?,?)",
                         (CODE, "300621", "维业股份", "浙江", "专业工程", "创业板", "20110308"))
            for d, c in (("20260929", 7.27), ("20260930", 7.10)):
                conn.execute("INSERT OR REPLACE INTO daily (ts_code, trade_date, open, high, low, close,"
                             " pre_close, change, pct_chg, vol, amount) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                             (CODE, d, c + 0.05, c + 0.1, c - 0.05, c, c + 0.18,
                              c - (c + 0.18), -2.34, 32314.0, 23133.99))
            # 排名语义：另一票分更高 ⇒ CODE 全市场第 2/2 名
            conn.execute("INSERT OR REPLACE INTO daily_score (ts_code, trade_date, score, pred_ret)"
                         " VALUES (?,?,?,?)", ("999998.SZ", "20260930", 95.0, 0.02))
            conn.execute("INSERT OR REPLACE INTO daily_score (ts_code, trade_date, score, pred_ret)"
                         " VALUES (?,?,?,?)", (CODE, "20260930", 92.84, 0.0094))
            conn.execute("INSERT OR REPLACE INTO daily_score (ts_code, trade_date, score, pred_ret)"
                         " VALUES (?,?,?,?)", (CODE, "20260929", 94.8, 0.0107))
            conn.execute("INSERT OR REPLACE INTO model_score (ts_code, trade_date, score, pred_ret)"
                         " VALUES (?,?,?,?)", (CODE, "20260930", 89.19, 0.0087))
            conn.execute("INSERT OR REPLACE INTO lhb (trade_date, ts_code, name, close, pct_chg, net_buy,"
                         " buy_value, sell_value, lhb_amount, market_amount, net_ratio,"
                         " amount_ratio, turnover, float_mv, reason) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         ("20241030", CODE, "维业股份", 5.9, 10.01, -39063754.96,
                          120000000.0, 159063754.96, 266844039.96, 900000000.0,
                          -14.6, 28.4, 28.4653, 2596996660.16, "连续三个交易日内涨幅偏离值累计达到30%"))
            conn.execute("INSERT OR REPLACE INTO signal (signal_date, ts_code, name, action, reason)"
                         " VALUES (?,?,?,?,?)",
                         ("20260930", CODE, "维业股份", "buy_candidate", "新进前20: 分位 99.8"))
            conn.execute("INSERT OR REPLACE INTO paper_position (ts_code, name, shares, avg_cost, entry_adj)"
                         " VALUES (?,?,?,?,?)", (CODE, "维业股份", 200, 9.359, 5.78))
            conn.execute("INSERT OR REPLACE INTO paper_trade (exec_date, ts_code, name, side, shares,"
                         " price, cost, amount, reason) VALUES (?,?,?,?,?,?,?,?,?)",
                         ("20260930", CODE, "维业股份", "buy", 200, 10.19, 5.02, 2038.0, "月度方案买入"))
            conn.commit()
        cls.d = sd.build_stock_detail(CODE, _payload_with_holding())

    def test_basic_and_known(self):
        self.assertTrue(self.d["known"])
        self.assertEqual(self.d["name"], "维业股份")
        self.assertEqual(self.d["industry"], "专业工程")

    def test_kline_shape(self):
        k = self.d["kline"]
        self.assertEqual(len(k), 2, "fixture 两根 K 线")
        self.assertEqual(k[-1]["date"], "20260930", "升序排列、末行最新")
        self.assertEqual(k[-1]["close"], 7.10)

    def test_daily_score_with_rank(self):
        ds = self.d["daily_scores"]
        self.assertEqual(len(ds), 2)
        self.assertEqual(ds[0]["trade_date"], "20260930")
        self.assertAlmostEqual(ds[0]["score"], 92.84, places=2)
        self.assertEqual(self.d["score_rank"], 2, "另一票 95.0 更高 ⇒ 本票第 2 名")
        self.assertEqual(self.d["score_pool"], 2)

    def test_verdict_mentions_holding_score_lhb_signal(self):
        v = self.d["verdict"]
        self.assertIn("被 1/7 账户持有（fterm 2万真实档）", v)
        self.assertIn("最新日推分 92.8", v)
        self.assertIn("全市场第 2/2 名", v)
        self.assertIn("最近上榜 20241030（净买 -3,906 万）", v)
        self.assertIn("最近信号 20260930 buy_candidate", v)

    def test_lhb_negative_net_buy_sets_warn(self):
        self.assertEqual(self.d["tone"], "warn", "最近上榜净卖出 ⇒ warn（资金面事实陈述）")

    def test_cross_account_matrix(self):
        h = self.d["holdings"]
        self.assertEqual(len(h), 1)
        self.assertEqual(h[0]["account_id"], "fterm-w2")
        self.assertEqual(h[0]["model"], "fterm")
        self.assertEqual(h[0]["shares"], 400.0)
        self.assertEqual([t["account_id"] for t in self.d["trades_by_account"]], ["fterm-w2"])

    def test_monthly_account_holding_and_trade(self):
        self.assertEqual(self.d["monthly"]["holding"]["shares"], 200)
        self.assertEqual(len(self.d["monthly"]["trades"]), 1)
        self.assertEqual(self.d["monthly"]["trades"][0]["reason"], "月度方案买入")

    def test_unknown_code_is_graceful(self):
        """不存在的票：known=False + 空行情，不抛（前端展示"基础库无此票"）。"""
        d = sd.build_stock_detail("999999.SZ", _payload_with_holding(CODE))
        self.assertFalse(d["known"])
        self.assertEqual(d["kline"], [])
        self.assertIn("均未持有", d["verdict"])

    def test_empty_code_raises(self):
        with self.assertRaises(ValueError):
            sd.build_stock_detail("  ")


if __name__ == "__main__":
    unittest.main()
