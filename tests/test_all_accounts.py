"""全账户聚合（app/all_accounts.py）的测试。

两个关注点：
1. 对真实外部账本（D:\\Final、D:\\ECO-W2）**只读**跑通并断言统一 schema ——
   外部文件缺失时整类 skip（别的机器/CI 没挂这两盘不该红）；
2. 故障隔离与聚合形状（坏路径 → ok:false，不拖垮其他账户）——与真实文件无关。

2026-10-05 自 D:\\account-board\\tests\\test_sources.py 迁入并适配本仓。
"""
import json
import os
import unittest
from pathlib import Path

from app import all_accounts as aa

_EXTERNAL_READY = (
    os.path.exists(aa.FTERM_W2_DB)
    and os.path.exists(aa.ECOW2_MINI_LEDGER)
)


def _descs(fn_name):
    for name, d in aa.SOURCES:
        if name == fn_name:
            return d
    raise KeyError(fn_name)


@unittest.skipUnless(_EXTERNAL_READY, "外部账本(Final/ECO-W2)不在这台机器上")
class TestAdaptersReal(unittest.TestCase):
    """对真实账本文件只读跑一遍，断言统一 schema。全程不写任何源文件。"""

    def _check(self, accs, n_expect):
        self.assertEqual(len(accs), n_expect)
        for a in accs:
            self.assertTrue(a["ok"], f"{a['id']} 应读取成功: {a['error']}")
            for key in ("id", "name", "model", "equity", "cash", "curve", "asof",
                        "dd_pct", "days"):
                self.assertIn(key, a)
            self.assertTrue(a["curve"], f"{a['id']} 曲线不应为空")
            self.assertEqual(a["pct"], a["curve"][-1][1], "pct 应等于曲线末点")
            self.assertGreaterEqual(a["dd_pct"], 0.0, "回撤非负")
            self.assertEqual(a["days"], len(a["curve"]))
            self.assertTrue(a["asof"], f"{a['id']} 应有数据日")

    def test_neweco_monthly(self):
        (acc,) = aa.neweco_monthly(_descs("neweco_monthly"))
        self._check([acc], 1)
        self.assertTrue(acc["extra"]["bench_curve"], "月频盘应有基准曲线")
        self.assertEqual(acc["extra"]["bench_curve"][0][1], 0.0, "基准首点归一")

    def test_neweco_weekly(self):
        accs = aa.neweco_weekly(_descs("neweco_weekly"))
        self._check(accs, 1)
        self.assertEqual(accs[0]["id"], "neweco-weekly")

    def test_fterm_w2(self):
        (acc,) = aa.fterm_w2(_descs("fterm_w2"))
        self._check([acc], 1)
        ex = acc["extra"]
        self.assertIn("decisions", ex)
        self.assertIn("limits", ex)
        self.assertIn("halt_line", ex)
        self.assertIn("total_dd_halt", ex["limits"], "应从 config.yaml 抠到总回撤熔断参数")
        self.assertAlmostEqual(
            ex["halt_line"],
            round(acc["equity"] * (1 - ex["limits"]["total_dd_halt"]), 2))

    def test_fterm_blend_and_k100(self):
        """2026-10-15 新接入的两个 Final 轨（#124 影子 + B8 观测）。

        建账首日（2026-10-14）只有 1 行 equity_curve ⇒ 曲线 1 点、pct=0、dd=0
        都是合法状态；关键钉：不抛错、extra 契约字段齐、id 正确。
        """
        for key, acc_id in (("fterm_blend", "fterm-blend"), ("fterm_k100", "fterm-k100")):
            with self.subTest(key=key):
                (acc,) = getattr(aa, key)(_descs(key))
                self.assertEqual(acc["id"], acc_id)
                self._check([acc], 1)
                ex = acc["extra"]
                self.assertIn("decisions", ex)
                self.assertIn("limits", ex)

    def test_ecow2_stock(self):
        accs = aa.ecow2_stock(_descs("ecow2_stock"))
        self._check(accs, 2)
        for acc in accs:
            ex = acc["extra"]
            self.assertTrue(ex["periods"], "股票臂应有逐期记录")
            self.assertTrue(ex["runs"], "应有 runs 运行记录")
            for p in acc["positions"]:
                self.assertIn("stuck", p)

class TestBuildAll(unittest.TestCase):
    def test_shape_and_cache(self):
        payload = aa.build_all(force=True)
        self.assertEqual(len(payload["accounts"]), 8)
        ids = [a["id"] for a in payload["accounts"]]
        self.assertEqual(len(set(ids)), 8, "账户 id 应唯一")
        self.assertEqual(payload["totals"]["n_total"], 8)
        self.assertEqual(len(payload["totals"]["by_model"]), 3)
        # 服务端序列化必须干净(禁 NaN)
        json.dumps(payload, ensure_ascii=False, allow_nan=False)
        # TTL 内命中缓存返回同一对象
        self.assertIs(aa.build_all(), payload)

    def test_model_meta_portal(self):
        """门户卡片的模型元数据：因子清单/训练时间点/档位，三模型各有来源。"""
        meta = aa.build_model_meta()
        self.assertEqual(set(meta), {"NEW ECO", "Final", "W2"})
        ne = meta["NEW ECO"]
        self.assertEqual(len(ne["factors"]), 26, "NEW ECO = 全池 31 − 剔除 5")
        self.assertEqual(len(ne["importance"]), 26)
        self.assertTrue(ne["trained_at"], "应有 model_meta 训练时间")
        self.assertTrue(ne["schedule"])
        ft = meta["Final"]
        self.assertEqual(len(ft["factors"]), 25, "fterm dlv2 = 25 特征")
        self.assertEqual(ft["n_nets"], 14, "实际落盘 14 网")
        self.assertTrue(ft["trained_at"])
        w2 = meta["W2"]
        self.assertEqual(len(w2["factors"]), 5, "ECO-W2 M1 入模 5 因子")
        self.assertIn("规则臂", w2["factor_source"], "纸面轨=规则臂必须注记")
        json.dumps(meta, ensure_ascii=False, allow_nan=False)

    def test_real_files_aggregate_all_green(self):
        if not _EXTERNAL_READY:
            self.skipTest("外部账本不在这台机器上")
        payload = aa.build_all(force=True)
        self.assertEqual(payload["totals"]["n_ok"], 8,
                         "三个源项目账本都在时, 8 账户应全部读取成功")


class TestFaultIsolation(unittest.TestCase):
    def _patch(self, attr, bad):
        old = getattr(aa, attr)
        setattr(aa, attr, bad)
        self.addCleanup(setattr, aa, attr, old)

    def test_bad_sqlite_path_isolated(self):
        self._patch("NEWECO_DB", r"D:\__no_such_dir__\missing.db")
        payload = aa.build_all(force=True)
        acc = next(a for a in payload["accounts"] if a["id"] == "neweco-monthly")
        self.assertFalse(acc["ok"])
        self.assertTrue(acc["error"])
        others = [a for a in payload["accounts"] if a["id"] != "neweco-monthly"]
        self.assertTrue(all(a["ok"] for a in others), "其余 5 账户应不受影响")

    def test_bad_json_path_isolated(self):
        self._patch("ECOW2_MINI_LEDGER", r"D:\__no_such_dir__\missing.json")
        payload = aa.build_all(force=True)
        acc = next(a for a in payload["accounts"] if a["id"] == "ecow2-k6")
        self.assertFalse(acc["ok"])


if __name__ == "__main__":
    unittest.main()
