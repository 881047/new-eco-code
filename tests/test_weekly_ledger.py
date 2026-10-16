"""周频袖套账本的守卫测试(不依赖实库、不联网 —— 全部用注入的 fill_fn 与临时文件)

守五条:
  1. 成交价 = 注入的成交价;费用按真实费率扣(含 5 元最低佣金);
  2. 一字板/停牌(fill_fn 返回 None)必须**顺延**而不是强行成交;
  3. **T+1**: 当日买入的持仓当日不可卖出;
  4. **整手**: 差额不足一手时不动(不产生碎股);
  5. 现金不足时不融资(顺延), 且账本可保存/重载(幂等)。
外加: 盯市时按 adj 比例缩放股数(除权不产生假亏损) —— 用 stub conn 覆盖。
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from app.weekly.ledger import Ledger, backup_snapshots, week_anchors  # noqa: E402


def _plan(rows):
    return {"rows": [{"ts_code": c, "shares": s} for c, s in rows]}


class TestLedger(unittest.TestCase):
    def setUp(self):
        self.path = BASE / "data" / "weekly" / "_test_ledger.json"
        if self.path.exists():
            self.path.unlink()
        self.led = Ledger(100_000.0, self.path)
        self.prices = {}                      # code -> {date: price}
        # 盯市隔离（2026-09-14 修，既有缺陷）：`execute_plan` 内部会调 `mark()`，而
        # `mark()` 默认走**生产库**读收盘价与逆回购利率 —— 本文件 docstring 写着"不依赖实库"，
        # 实际却依赖。当天 600000.SH 在 20260914 的真实收盘（**9.40**）一进库，"只损失买入
        # 佣金"的断言就差了 1000×(10.00−9.40)=600 元当场变红。⇒ 测试自己喂空价表 +
        # 空利率表：持仓按**成交价**盯市，利息走 1.5% 兜底（与 _StubConn 的用法一致）。
        import pandas as pd
        empty = pd.DataFrame({"ts_code": [], "close": [], "adj_factor": []})
        for patcher in (mock.patch("app.weekly.ledger.pd.read_sql", return_value=empty),
                        mock.patch("app.weekly.ledger.db.get_conn",
                                   side_effect=lambda: _StubConn(None))):
            patcher.start()
            self.addCleanup(patcher.stop)

    def tearDown(self):
        if self.path.exists():
            self.path.unlink()

    def _fill(self, code, date, side):
        return self.prices.get(code, {}).get(date)

    def test_buy_then_fee_and_shares(self):
        self.prices["600000.SH"] = {"20260914": 10.00}
        done = self.led.execute_plan(_plan([("600000.SH", 1000)]), "20260914",
                                     fill_fn=self._fill)
        self.assertEqual(len(done), 1)
        self.assertEqual(done[0]["side"], "buy")
        self.assertEqual(done[0]["shares"], 1000)
        # 费用: 佣金 max(10000×0.00025, 5)=5 + 过户费 0.1 = 5.1
        self.assertAlmostEqual(done[0]["fee"], 5.1, places=2)
        self.assertAlmostEqual(self.led.state["cash"], 100_000 - 10_000 - 5.1, places=2)

    def test_limit_up_is_deferred_not_filled(self):
        self.prices["600000.SH"] = {}          # 一字涨停 ⇒ fill_fn 返回 None
        done = self.led.execute_plan(_plan([("600000.SH", 1000)]), "20260914",
                                     fill_fn=self._fill)
        self.assertEqual(done, [])
        self.assertEqual(self.led.state["positions"], {})
        self.assertEqual(self.led.state["cash"], 100_000.0)

    def test_t_plus_1_blocks_same_day_sell(self):
        self.prices["600000.SH"] = {"20260914": 10.00}
        self.led.execute_plan(_plan([("600000.SH", 1000)]), "20260914", fill_fn=self._fill)
        done = self.led.execute_plan(_plan([]), "20260914", fill_fn=self._fill)   # 同日清仓
        self.assertEqual(done, [], "T+1 应阻止当日卖出")
        self.assertIn("600000.SH", self.led.state["positions"])

    def test_poison_guard_refuses_zero_capital_new_file(self):
        """2026-09-28 毒账本守卫: 新建账本 capital<=0 ⇒ save() 拒绝落盘(文件不创建)。
        正常初始化永远带真实本金; 0 资金新账本只会来自误操作(如无账本时 resume)。"""
        bad = Ledger(0.0, self.path)
        self.assertFalse(bad.save())
        self.assertFalse(self.path.exists())

    def test_retained_position_is_not_topped_up(self):
        """留存持仓**不动**: 目标股数变大也不再买入(与回测"留存不调股数"同口径)。
        2026-09-13 回放实测: 原"按差额补仓"使实盘换手/费用显著高于回测。"""
        self.prices["600000.SH"] = {"20260914": 10.00, "20260921": 10.00}
        self.led.execute_plan(_plan([("600000.SH", 1000)]), "20260914", fill_fn=self._fill)
        done = self.led.execute_plan(_plan([("600000.SH", 5000)]), "20260921", fill_fn=self._fill)
        self.assertEqual(done, [], "留存持仓不应被补仓")
        self.assertEqual(self.led.state["positions"]["600000.SH"]["shares"], 1000.0)

    def test_odd_lot_delta_is_skipped(self):
        """⚠️ 2026-09-19 审查: 这条**名不副实** —— 它实际守的是"留存持仓不动", 不是"差额不足一手"。

        第二次调用时 600000.SH 已在 `positions` 里, `execute_plan` 的买入循环一开头就
        `continue`(留存持仓不调股数), 所以 **`delta = want - cur` 那半段从来没执行过**
        (`cur` 恒为 0 使得 delta 恒等于 want; 原"按差额补仓"分支是不可达死代码, 已删除)。
        断言 `done == []` 因此永远成立 —— 与 50 股这个数无关, 改成 10500 股也一样绿。
        保留本用例(它在"留存不补仓"这条主规则被改坏时会红), 但别再把它当作整手算术的守卫。
        """
        self.prices["600000.SH"] = {"20260914": 10.00, "20260921": 10.00}
        self.led.execute_plan(_plan([("600000.SH", 1000)]), "20260914", fill_fn=self._fill)
        done = self.led.execute_plan(_plan([("600000.SH", 1050)]), "20260921",
                                     fill_fn=self._fill)     # 差额 50 股 < 一手
        self.assertEqual(done, [], "不足一手的差额不应成交")

    def test_no_margin_when_cash_short(self):
        self.prices["600000.SH"] = {"20260914": 10.00}
        done = self.led.execute_plan(_plan([("600000.SH", 100_000)]), "20260914",
                                     fill_fn=self._fill)      # 需要 100 万, 只有 10 万
        self.assertEqual(done, [])
        self.assertGreaterEqual(self.led.state["cash"], 0.0)

    def test_save_and_reload_is_idempotent(self):
        self.prices["600000.SH"] = {"20260914": 10.00}
        self.led.execute_plan(_plan([("600000.SH", 1000)]), "20260914", fill_fn=self._fill)
        again = Ledger.load(100_000.0, self.path)
        self.assertEqual(again.state["positions"]["600000.SH"]["shares"], 1000.0)
        done = again.execute_plan(_plan([("600000.SH", 1000)]), "20260914", fill_fn=self._fill)
        self.assertEqual(done, [], "同目标重复执行不应再次买入")

    def test_sell_frees_position_and_cash(self):
        self.prices["600000.SH"] = {"20260914": 10.00, "20260921": 11.00}
        self.led.execute_plan(_plan([("600000.SH", 1000)]), "20260914", fill_fn=self._fill)
        done = self.led.execute_plan(_plan([]), "20260921", fill_fn=self._fill)
        self.assertEqual(len(done), 1)
        self.assertEqual(done[0]["side"], "sell")
        # 卖出: 11,000 - 佣金 5 - 印花税 5.5 - 过户费 0.11
        self.assertAlmostEqual(done[0]["fee"], 10.61, places=2)
        self.assertNotIn("600000.SH", self.led.state["positions"])

    def test_kpi_snapshot_shape(self):
        self.prices["600000.SH"] = {"20260914": 10.00}
        self.led.execute_plan(_plan([("600000.SH", 1000)]), "20260914", fill_fn=self._fill)
        snap = self.led.snapshot(benchmark=False)
        self.assertEqual(snap["n_positions"], 1)
        self.assertIn("capital", snap)
        # 成交后才记净值点 ⇒ 卖出后 snapshot 必须能算出总收益(不是只剩现金)
        self.assertIn("nav", snap)
        self.assertAlmostEqual(snap["nav"], 100_000 - 5.1, places=2)   # 只损失买入佣金

    def test_same_day_remark_replaces_nav_point(self):
        """开盘档 + 晚间兜底档会在同一天各盯市一次 ⇒ 只能留一个净值点(否则周数翻倍)。"""
        self.prices["600000.SH"] = {"20260914": 10.00}
        self.led.execute_plan(_plan([("600000.SH", 1000)]), "20260914", fill_fn=self._fill)
        n1 = len(self.led.state["nav_history"])
        self.led.execute_plan(_plan([("600000.SH", 1000)]), "20260914", fill_fn=self._fill)
        self.assertEqual(len(self.led.state["nav_history"]), n1, "同日重复盯市不应新增净值点")

    def test_quote_path_uses_realtime_open(self):
        """use_quotes=True 且行情可用时, 必须走实时开盘价函数(_exec_price_open)。"""
        from unittest import mock
        fake = {"600000.SH": {"open": 10.5, "prev_close": 10.0}}
        with mock.patch("app.datasource.quote_client.get_quotes", return_value=fake), \
             mock.patch("app.weekly.ledger._exec_price_open", return_value=999.0) as m:
            done = self.led.execute_plan(_plan([("600000.SH", 100)]), "20260914",
                                         use_quotes=True)
            self.assertTrue(m.called, "应调用实时开盘价函数")
            self.assertEqual(done[0]["price"], 999.0)

    def test_adj_anchor_never_falls_back_to_one(self):
        """FUND-01(2026-09-16 资金线审查): 复权锚**绝不能落到 1.0**。

        病理场景: daily 已同步、adj_factor 同步失败的日子买入并盯市 —— 旧实现把
        `pos["adj"]` 锚成 1.0, 次日真因子(老股累计因子常在 100+)出现时
        shares ×139 ⇒ 净值爆炸。修复: 因子缺失时保持 None 不锚定, 真因子出现再锚。
        红检: 本测试在旧代码上第 2 步断言失败(shares 变 139000)。"""
        import pandas as pd
        from unittest import mock
        self.prices["600000.SH"] = {"20260914": 10.00}
        self.led.execute_plan(_plan([("600000.SH", 1000)]), "20260914", fill_fn=self._fill)

        def px(close, adj):
            return pd.DataFrame({"ts_code": ["600000.SH"], "close": [close],
                                 "adj_factor": [adj]})

        stub = _StubConn(None)
        # 第 1 次盯市: close 有、adj 缺失(病理日) ⇒ 不得锚定, 股数不动
        with mock.patch("app.weekly.ledger.pd.read_sql", return_value=px(10.00, None)):
            self.led.mark("20260914", conn=stub, record=False)
        pos = self.led.state["positions"]["600000.SH"]
        self.assertIsNone(pos.get("adj"), "因子缺失日不得锚定(旧代码锚成 1.0)")
        self.assertEqual(pos["shares"], 1000.0)
        # 第 2 次盯市: 真因子 139.0 出现 ⇒ 锚定 139, 股数不变(锚定日不缩放)
        with mock.patch("app.weekly.ledger.pd.read_sql", return_value=px(10.00, 139.0)):
            self.led.mark("20260914", conn=stub, record=False)
        self.assertEqual(pos["adj"], 139.0)
        self.assertEqual(pos["shares"], 1000.0, "锚定日不得缩放股数(旧代码 ×139)")
        # 第 3 次盯市: 因子 140(除权) ⇒ 正常除权再投资 1000×140/139
        with mock.patch("app.weekly.ledger.pd.read_sql", return_value=px(10.00, 140.0)):
            self.led.mark("20260914", conn=stub, record=False)
        self.assertAlmostEqual(pos["shares"], 1000.0 * 140.0 / 139.0, places=6)


class _StubConn:
    """只提供 `_accrue_interest` 需要的那一条查询(上一盯市日最近隔夜利率)。"""

    def __init__(self, rate):
        self.rate = rate

    def execute(self, sql, params=None):
        return self

    def fetchone(self):
        return (self.rate,)

    def fetchall(self):
        return [(self.rate,)]

    def close(self):
        pass


class TestIdleCashInterest(unittest.TestCase):
    """闲置现金计息（2026-09-13）：月频模拟盘一直在算（`paper_interest`），周频账本漏了。

    同一终端对同一件物理事实有两套记账法，本身就是缺陷；袖套平均只有 77% 投出去，
    23% 现金按逆回购隔夜均值 1.6% 算 ≈ **0.37pp/年**，且方向是低估自己。

    守五条：① 首个净值点只记起点、不计息（否则从"时间起点"开始算）；② 按**日历日**
    （与月频侧同口径）× 隔夜利率计息且金额可手算复现；③ **幂等** —— 同日重复盯市
    （开盘档+晚间档）不重复计息；④ 净值含利息、`invested` **不含**（否则投入率被污染）；
    ⑤ 利率缺失有兜底、旧账本自动补键。
    """

    def setUp(self):
        self.path = BASE / "data" / "weekly" / "_test_ledger_interest.json"
        if self.path.exists():
            self.path.unlink()
        self.led = Ledger(100_000.0, self.path)

    def tearDown(self):
        if self.path.exists():
            self.path.unlink()

    def _mark(self, date, rate=None):
        from unittest import mock
        import pandas as pd
        empty = pd.DataFrame({"ts_code": [], "close": [], "adj_factor": []})
        with mock.patch("app.weekly.ledger.pd.read_sql", return_value=empty):
            return self.led.mark(date, conn=_StubConn(rate))

    def test_first_mark_sets_origin_without_interest(self):
        self._mark("20260914")
        self.assertEqual(self.led.state["interest"], 0.0)
        self.assertEqual(self.led.state["interest_upto"], "20260914")

    def test_interest_matches_hand_calculation_over_calendar_days(self):
        self._mark("20260914")
        self._mark("20260917", rate=1.5)          # 3 个日历日
        self.assertAlmostEqual(self.led.state["interest"],
                               100_000.0 * 1.5 / 100.0 / 365.0 * 3, places=6)

    def test_same_day_remark_is_idempotent(self):
        self._mark("20260914")
        self._mark("20260916", rate=1.5)
        first = self.led.state["interest"]
        self._mark("20260916", rate=1.5)      # 同日晚间兜底档再盯一次
        self.assertEqual(self.led.state["interest"], first, "同日重复盯市不该重复计息")

    def test_nav_includes_interest_and_invested_excludes_it(self):
        self._mark("20260914")
        self._mark("20260915", rate=1.5)
        pt = self.led.state["nav_history"][-1]
        self.assertGreater(pt["interest"], 0)
        self.assertAlmostEqual(pt["nav"], 100_000 + pt["interest"], places=2)
        self.assertEqual(pt["invested"], 0.0, "invested 只算持仓市值, 不许把利息算进去")

    def test_missing_rate_falls_back_to_1_5(self):
        self._mark("20260914")
        self._mark("20260915", rate=None)
        self.assertAlmostEqual(self.led.state["interest"],
                               100_000.0 * 1.5 / 100.0 / 365.0, places=6)

    def test_old_ledger_without_interest_keys_still_works(self):
        self.led.state.pop("interest", None)
        self.led.state.pop("interest_upto", None)
        self._mark("20260914")
        self.assertIn("interest", self.led.state)


class TestLedgerAntiCorruption(unittest.TestCase):
    """账本是袖套唯一的成交记录 ⇒ 必须有两层抗损坏: 覆写前滚动 .bak + 按日快照。"""

    def setUp(self):
        self.dir = BASE / "data" / "weekly" / "_test_ledger_anti"
        if self.dir.exists():
            import shutil
            shutil.rmtree(self.dir)
        self.dir.mkdir(parents=True)
        self.path = self.dir / "ledger.json"

    def tearDown(self):
        if self.dir.exists():
            import shutil
            shutil.rmtree(self.dir)

    def test_save_keeps_previous_version_as_bak(self):
        led = Ledger(100_000.0, self.path)
        led.save()                                  # 第一次: 没有上一版, 不该有 .bak
        self.assertFalse(Path(str(self.path) + ".bak").exists())
        first = self.path.read_bytes()
        led.state["cash"] = 1.0
        led.save()                                  # 第二次: 应把上一版留成 .bak
        self.assertEqual(Path(str(self.path) + ".bak").read_bytes(), first)
        import json as _json
        self.assertEqual(_json.loads(self.path.read_text(encoding="utf-8"))["cash"], 1.0)

    def test_save_is_atomic_and_never_leaves_a_partial_file(self):
        """2026-09-14（07 号 #48）：落盘改**原子写**（同目录 tmp + os.replace）。

        原实现直接 `write_text` 覆写：写到一半崩/断电 ⇒ 半截 JSON，而 `.bak` 存的是
        **上一版**（不是当前版），恢复会丢最近一笔成交。这里钉两件事：
          ① `os.replace` 失败（如文件被占用）⇒ **原文件必须原封不动**；
          ② 失败路径不许留下 `.tmp`（否则 data/weekly 会长出无人清理的垃圾）。
        红检：把 save() 改回 `self.path.write_text(...)` ⇒ 本条变红（替换被绕过）。
        """
        led = Ledger(100_000.0, self.path)
        led.save()
        first = self.path.read_bytes()
        led.state["cash"] = 1.0
        with mock.patch("app.weekly.ledger.os.replace", side_effect=OSError("locked")):
            with self.assertRaises(OSError):
                led.save()
        self.assertEqual(self.path.read_bytes(), first, "替换失败不许破坏原文件")
        self.assertFalse(Path(str(self.path) + ".tmp").exists(), "失败后不许留临时文件")
        led.save()                                       # 恢复后能正常落盘
        import json as _json
        self.assertEqual(_json.loads(self.path.read_text(encoding="utf-8"))["cash"], 1.0)

    def test_stale_instance_cannot_overwrite_newer_ledger(self):
        """**丢失更新守卫**（2026-09-15，外部审查 P1-3 复核为真）。

        存在两条互不知情的写入路径：调度档（进程内串行 + `.task.lock`）与人工 CLI
        （`run_weekly.py exec|resume`，**不经过** `.task.lock`）。若 A 先 load、B 改完并保存、
        A 再 save ⇒ 原实现会整份覆盖掉 B 的成交（track record 静默丢失）。

        判据：晚到的陈旧写入者**必须被拒绝**（save() 返回 False），且盘上内容仍是 B 的。
        红检：把 `_save_locked` 里的指纹比对注释掉 ⇒ 本条变红（A 覆盖 B）。
        """
        import json as _json
        a = Ledger(100_000.0, self.path)
        a.save()                                     # 初始账本落盘
        stale = Ledger.load(100_000.0, self.path)     # A：加载后迟迟不写
        fresh = Ledger.load(100_000.0, self.path)     # B：同一时刻加载，先写
        fresh.state["cash"] = 5.0
        self.assertTrue(fresh.save(), "B 的正常落账不该被拒")
        stale.state["cash"] = 999.0
        self.assertFalse(stale.save(), "陈旧实例必须被拒绝（否则覆盖他人成交）")
        self.assertEqual(_json.loads(self.path.read_text(encoding="utf-8"))["cash"], 5.0)

    def test_save_releases_lock_file_even_on_failure(self):
        """锁文件是**临时物**：成功/失败都不许留在 `data/weekly/` 里（否则下次被当活锁）。"""
        led = Ledger(100_000.0, self.path)
        led.save()
        self.assertFalse(Path(str(self.path) + ".lock").exists(), "成功路径残留锁文件")
        led.state["cash"] = 1.0
        with mock.patch("app.weekly.ledger.os.replace", side_effect=OSError("locked")):
            with self.assertRaises(OSError):
                led.save()
        self.assertFalse(Path(str(self.path) + ".lock").exists(), "异常路径残留锁文件")

    def test_same_instance_can_save_repeatedly(self):
        """指纹更新必须发生在自己落账之后 —— 否则第二条 save 会被自己挡住（假冲突）。"""
        led = Ledger(100_000.0, self.path)
        for i in (1.0, 2.0, 3.0):
            led.state["cash"] = i
            self.assertTrue(led.save(), "同一实例的连续落账不该被自己的守卫拒绝")

    def test_snapshot_and_prune(self):
        led = Ledger(100_000.0, self.path)
        led.save()
        p = backup_snapshots(self.path, keep=2)
        self.assertTrue(p and Path(p).exists())
        # 造 3 个「更早」的假快照 ⇒ 保留策略应只留最近 keep 份
        out = self.dir / "backup"
        for d in ("20250101", "20250201", "20250301"):
            (out / f"ledger_{d}.json").write_text("{}", encoding="utf-8")
        backup_snapshots(self.path, keep=2)
        left = sorted(x.name for x in out.glob("ledger_????????.json"))
        self.assertEqual(len(left), 2)
        self.assertTrue(left[-1].startswith("ledger_2026"))   # 最新那份一定留着

    def test_snapshot_of_missing_ledger_is_none(self):
        self.assertIsNone(backup_snapshots(self.dir / "nope.json"))

    def test_trades_carry_the_plan_fingerprint(self):
        """审计链: 成交必须能反查它用的是哪份方案 + 那份方案的输入指纹。"""
        import json as _json
        led = Ledger(100_000.0, self.path)
        plan = {"anchor": "20260911", "rows": [{"ts_code": "600000.SH", "shares": 1000}],
                "meta": {"code": "abc1234", "scores_sha16": "deadbeef", "config_sha16": "cafe"}}
        led.execute_plan(plan, "20260914", fill_fn=lambda c, d, s: 10.0)
        lp = led.state.get("last_plan") or {}
        self.assertEqual(lp.get("anchor"), "20260911")
        self.assertEqual(lp.get("meta", {}).get("scores_sha16"), "deadbeef")
        self.assertEqual(lp.get("applied_on"), "20260914")
        # 落盘后仍在(重启不丢)
        self.assertIn("last_plan", _json.loads(self.path.read_text(encoding="utf-8")))


class TestWeekAnchorSemantics(unittest.TestCase):
    """「周」必须是**自然周**, 不能是日点(2026-09-21 修正的一处会产出误导数字的缺陷)。

    袖套**每天盯市**落一个净值点, 而执行档(调仓)只在周一推进一次。此前三处把日点数
    当周数用: `ledger.snapshot` 用 `len(nav.pct_change())`、`protocol.G1` 用 `len(nav)`、
    `attrib` 用归因期数 ⇒ 实测后果: 一周基准 −2% 会被年化成 **−53%**; 约 12 个日点
    (≈2.5 周)就能点亮 protocol 的"可进入小资金档"。

    红检方向: 把 `snapshot` 里的 `week_anchors(nav.index)` 改回 `len(ret)` ⇒
    `test_day_points_must_not_unlock_annualized` 当场红。
    """

    def setUp(self):
        self.path = BASE / "data" / "weekly" / "_test_week_anchor.json"
        if self.path.exists():
            self.path.unlink()
        self.addCleanup(lambda: self.path.unlink() if self.path.exists() else None)

    def _led(self, dates):
        led = Ledger(100_000.0, self.path)
        nav = 100_000.0
        led.state["nav_history"] = []
        for d in dates:
            nav *= 1.001
            led.state["nav_history"].append(
                {"date": d, "nav": round(nav, 2), "invested": round(nav, 2)})
        return led

    # 0914(周一)~0918(周五) 同一周; 0921 起新的一周
    _DATES_9 = ["20260914", "20260915", "20260916", "20260917", "20260918",
                "20260921", "20260922", "20260923", "20260924"]

    def test_week_anchors_counts_natural_weeks_not_days(self):
        self.assertEqual(week_anchors([]), 0)
        self.assertEqual(week_anchors(["20260914"]), 1)
        # 5 个日点全在同一周 ⇒ 1 周(不是 5 周)
        self.assertEqual(week_anchors(["20260914", "20260915", "20260916",
                                       "20260917", "20260918"]), 1)
        # 6 个日点跨两周 ⇒ 2 周(不是 6 周)
        self.assertEqual(week_anchors(["20260914", "20260915", "20260916", "20260917",
                                       "20260918", "20260921"]), 2)

    def test_day_points_must_not_unlock_annualized(self):
        """09-24 的真实情形: 9 个日点只覆盖 2 个自然周 ⇒ 仍不得报年化/回撤。"""
        snap = self._led(self._DATES_9).snapshot(benchmark=False)
        self.assertEqual(snap["n_day_points"], 9)
        self.assertEqual(snap["weeks"], 2, "9 个日点只覆盖 2 个自然周, 不能当成 8 周")
        self.assertEqual(snap["n_week_anchors"], 2)
        self.assertNotIn("ann_return", snap, "不足 8 个自然周 ⇒ 不得报年化")
        self.assertNotIn("mdd", snap)
        self.assertIn("自然周", snap["note"], "note 必须说清是周还是点")

    def test_eight_real_weeks_does_unlock(self):
        """反过来必须成立: 真够 8 个自然周时要照常报, 不能把守卫改死。"""
        # 连续 8 个周一(跨 8 个自然周)
        dates = ["20260914", "20260921", "20260928", "20261012",
                 "20261019", "20261026", "20261102", "20261109"]
        snap = self._led(dates).snapshot(benchmark=False)
        self.assertEqual(snap["weeks"], 8)
        self.assertIn("ann_return", snap, "够 8 个自然周 ⇒ 应当报年化")
        self.assertIn("mdd", snap)


if __name__ == "__main__":
    unittest.main()
