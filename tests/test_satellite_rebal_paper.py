"""tests/test_satellite_rebal_paper.py — 指数调样群体追踪器的纯函数与状态机单测。

覆盖（预注册 plans/index-rebal-satellite-v2-prereg-20260928.md §2.4 规则的实现层）：
生效日推算 / 入退场日边界 / 下一批日历 / 台账自检 / 开仓幂等与 STAR 过滤 / 结算 CAR。
不联网、不写真实 ledger（tmp 目录重定向、today 显式注入）。
"""
import importlib.util
import json
import sys
import unittest
from datetime import date
from pathlib import Path

import pandas as pd

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

_spec = importlib.util.spec_from_file_location(
    "sat_rebal", BASE / "scripts" / "satellite_rebal_paper.py")
sr = importlib.util.module_from_spec(_spec)
sys.modules["sat_rebal"] = sr
_spec.loader.exec_module(sr)

CAL = ["20261209", "20261210", "20261211", "20261214", "20261215", "20261216",
       "20261217", "20261218", "20261222"]
DATES = ["20261214", "20261215", "20261216", "20261217", "20261218"]
TODAY = "20261218"


def _mk_frames(code_map: dict) -> tuple[pd.DataFrame, pd.DataFrame]:
    """code_map: {ts_code: [收盘价按 DATES 序]}；指数腿 000905.SH 每日 +0.1%。
    与 load_close_pivot 同向：index=trade_date，columns=ts_code。"""
    rows = []
    for c, vals in code_map.items():
        for d, v in zip(DATES, vals):
            rows.append((c, d, v))
    stocks = pd.DataFrame(rows, columns=["ts_code", "trade_date", "aclose"])
    stock_pv = stocks.pivot_table(index="trade_date", columns="ts_code", values="aclose")
    irows = [("000905.SH", d, 1000.0 * (1.001 ** j)) for j, d in enumerate(DATES)]
    idx = pd.DataFrame(irows, columns=["ts_code", "trade_date", "aclose"])
    idx_pv = idx.pivot_table(index="trade_date", columns="ts_code", values="aclose")
    return stock_pv, idx_pv


class TestCalendarRules(unittest.TestCase):
    def test_second_friday_next_td_202612(self):
        # 2026-12 的第二个周五 = 12-11，下一交易日 = 12-14（周一）
        self.assertEqual(sr.second_friday_next_td(2026, 12, CAL), "20261214")

    def test_first_td_after_strict(self):
        self.assertEqual(sr.first_td_after(CAL, date(2026, 12, 11)), "20261214")

    def test_last_td_before_strict(self):
        self.assertEqual(sr.last_td_before(CAL, date(2026, 12, 14)), "20261211")

    def test_next_batches_includes_december(self):
        got = sr.next_batches(date(2026, 9, 28), CAL)
        yms = {f"{x['y']}{x['m']:02d}" for x in got}
        self.assertIn("202612", yms)
        self.assertNotIn("202610", yms)


class TestLedgerStateMachine(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(__file__).parent / "_tmp_sat_rebal"
        self.tmp.mkdir(exist_ok=True)
        self._orig_ledger = sr.LEDGER
        sr.LEDGER = self.tmp / "ledger.json"
        self.stock_pv, self.idx_pv = _mk_frames(
            {"000001.SZ": [10.0, 10.2, 10.4, 10.6, 10.8],
             "000002.SZ": [20.0, 19.8, 20.2, 20.4, 20.6]})

    def tearDown(self):
        sr.LEDGER = self._orig_ledger
        if (self.tmp / "ledger.json").exists():
            (self.tmp / "ledger.json").unlink()
        self.tmp.rmdir()

    def _meta(self):
        return {"index": "000905.SH", "batch": "202612", "ann_date": "20261211",
                "ann_is_real": True, "eff_date": "20261218",
                "candidates": [{"ts_code": "000001.SZ"}, {"ts_code": "000002.SZ"},
                               {"ts_code": "688001.SH"}]}

    def test_open_batch_filters_star_and_idempotent(self):
        lg = {"batches": {}}
        self.assertTrue(sr.open_batch(lg, "202612", self._meta(), CAL,
                                      self.stock_pv, self.idx_pv, today=TODAY))
        b = lg["batches"]["202612"]
        self.assertEqual(b["n_candidates"], 2)
        self.assertEqual(b["skipped_star"], 1)
        self.assertNotIn("688001.SH", b["legs"])
        self.assertEqual(b["entry_td"], "20261214")
        self.assertEqual(b["exit_td"], "20261217")
        self.assertFalse(sr.open_batch(lg, "202612", self._meta(), CAL,
                                       self.stock_pv, self.idx_pv, today=TODAY))

    def test_open_batch_waits_for_entry_day(self):
        meta = self._meta()
        meta["ann_date"] = "20261220"  # 入场日 12-22 晚于注入的"今天"
        lg = {"batches": {}}
        self.assertFalse(sr.open_batch(lg, "202612", meta, CAL,
                                       self.stock_pv, self.idx_pv, today=TODAY))
        self.assertNotIn("202612", lg["batches"])

    def test_settle_batch_cohort_car(self):
        lg = {"batches": {}}
        sr.open_batch(lg, "202612", self._meta(), CAL, self.stock_pv, self.idx_pv,
                      today=TODAY)
        # 入场 12-14（j=0），退出 12-17（j=3）：个股 000001 +6.0%、000002 +2.0%；
        # 指数 (1.001³−1)=+0.3003%
        sr.settle_batch(lg, "202612", CAL, self.stock_pv, self.idx_pv, today=TODAY)
        b = lg["batches"]["202612"]
        self.assertEqual(b["status"], "settled")
        self.assertEqual(b["cohort_n"], 2)
        self.assertAlmostEqual(b["legs"]["000001.SZ"]["car"], 0.06 - 0.003003, places=4)
        self.assertAlmostEqual(b["cohort_car"],
                               (b["legs"]["000001.SZ"]["car"] + b["legs"]["000002.SZ"]["car"]) / 2,
                               places=6)

    def test_verify_flags_broken_ledger(self):
        lg = {"batches": {"202612": {
            "index": "000905.SH", "ann_date": "20261211", "ann_is_real": True,
            "eff_date": "20261218", "entry_td": "20261214", "exit_td": "20261217",
            "n_candidates": 2, "skipped_star": 1, "status": "settled",
            "cohort_n": 1, "cohort_car": 0.03, "settled_at": "20261218",
            "legs": {"000001.SZ": {"entry": 10.0, "status": "open"},
                     "000002.SZ": {"entry": 20.0, "exit": 20.4, "car": 0.0,
                                   "status": "open"}},
            "marks": {"20261215": {"n": 2, "cohort_car": 0.01},
                      "20261214": {"n": 2, "cohort_car": 0.00}}}}}
        errs = sr.verify(lg)
        self.assertTrue(any("已结算但" in e for e in errs))
        self.assertTrue(any("未按日期排序" in e for e in errs))

    def test_verify_passes_clean_ledger(self):
        lg = {"batches": {}}
        sr.open_batch(lg, "202612", self._meta(), CAL, self.stock_pv, self.idx_pv,
                      today=TODAY)
        self.assertEqual(sr.verify(lg), [])

    def test_ledger_roundtrip(self):
        lg = {"batches": {}}
        sr.open_batch(lg, "202612", self._meta(), CAL, self.stock_pv, self.idx_pv,
                      today=TODAY)
        sr.save_ledger(lg)
        lg2 = sr.load_ledger()
        self.assertIn("202612", lg2["batches"])
        self.assertEqual(lg2["batches"]["202612"]["n_candidates"], 2)


class TestPickFFRatio(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(__file__).parent / "_tmp_sat_pick"
        self.tmp.mkdir(exist_ok=True)
        self._orig_dir = sr.SAT_DIR
        sr.SAT_DIR = self.tmp
        pd.DataFrame({
            "ts_code": ["000001.SZ", "000002.SZ", "000003.SZ", "688001.SH"],
            "trade_date": ["20261130"] * 4,
            "free_share": [500.0, 900.0, 100.0, 300.0],
            "float_share": [1000.0, 1000.0, 1000.0, 400.0],
        }).to_parquet(self.tmp / "freefloat_monthly.parquet")
        (self.tmp / "batch_202612_candidates.json").write_text(json.dumps({
            "index": "000905.SH", "batch": "202612", "ann_date": "20261130",
            "candidates": [{"ts_code": "000001.SZ"}, {"ts_code": "000002.SZ"},
                           {"ts_code": "000003.SZ"}, {"ts_code": "688001.SH"}]}),
            encoding="utf-8")

    def tearDown(self):
        sr.SAT_DIR = self._orig_dir
        for f in self.tmp.iterdir():
            f.unlink()
        self.tmp.rmdir()

    def test_picks_top2_by_ff_ratio_excluding_star(self):
        picks = sr.pick_ff_ratio("202612", asof="20261215")
        self.assertEqual([p["ts_code"] for p in picks], ["000002.SZ", "000001.SZ"])
        self.assertEqual(picks[0]["ff_ratio"], 0.9)

    def test_missing_candidates_returns_empty(self):
        self.assertEqual(sr.pick_ff_ratio("209912"), [])


if __name__ == "__main__":
    unittest.main()
