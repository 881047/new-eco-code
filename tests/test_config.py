"""config 模块单测 (2026-09-06 第十八轮 R18-3): deep merge / mtime 缓存失效 /
inject_env 防授权码回写 / benchmark getter —— 此前该模块零直接测试"""
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config as cfg_mod

_ORIG_FILE = cfg_mod.CONFIG_FILE


class TestLoadConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".yaml", delete=False)
        self.tmp.close()
        self.patch = mock.patch.object(cfg_mod, "CONFIG_FILE", Path(self.tmp.name))
        self.patch.start()
        cfg_mod._cfg_cache["key"] = None
        cfg_mod._cfg_cache["merged"] = None
        # 清环境: 防本机 .env 干扰测试
        self.env_patch = mock.patch.dict(os.environ, {"EMAIL_AUTH_CODE": "TESTCODE"},
                                         clear=False)
        self.env_patch.start()

    def tearDown(self):
        self.env_patch.stop()
        self.patch.stop()
        os.unlink(self.tmp.name)
        cfg_mod._cfg_cache["key"] = None
        cfg_mod._cfg_cache["merged"] = None

    def _write(self, text: str):
        Path(self.tmp.name).write_text(text, encoding="utf-8")

    def test_deep_merge_missing_keys_filled_from_defaults(self):
        self._write("capital: 12345\n")
        cfg = cfg_mod.load_config(inject_env=False)
        self.assertEqual(cfg["capital"], 12345)
        # 2026-09-10 默认改恒定 6 只(选档纪律例外, 理由见 config.py 注释)
        self.assertEqual(cfg["state_stocks"], {"attack": 6, "neutral": 6, "defense": 6})
        self.assertIn("regime_equity_weight", cfg)
        self.assertIn("dynamic", cfg)

    def test_nested_merge_keeps_unlisted_subkeys(self):
        # 用户只覆盖 dynamic.enabled, 其余子键保留默认(深度合并而非整块替换)
        self._write("dynamic:\n  enabled: true\n")
        cfg = cfg_mod.load_config(inject_env=False)
        self.assertTrue(cfg["dynamic"]["enabled"])
        self.assertEqual(cfg["dynamic"]["stop_loss"], -0.15)

    def test_cache_invalidates_on_file_change(self):
        # 缓存键=(mtime_ns, 文件大小): 大小变化的写入必然失效(同 tick 等长改写属
        # 极端边缘, 生产 save_config 走 os.replace 新文件 mtime 必变)
        self._write("capital: 111\n")
        self.assertEqual(cfg_mod.load_config(inject_env=False)["capital"], 111)
        self._write("capital: 22222\n")
        self.assertEqual(cfg_mod.load_config(inject_env=False)["capital"], 22222)

    def test_caller_mutation_does_not_pollute_cache(self):
        self._write("dynamic:\n  enabled: false\n")
        a = cfg_mod.load_config(inject_env=False)
        a["dynamic"]["enabled"] = True  # 调用方原地修改(深拷贝, 不污染缓存)
        b = cfg_mod.load_config(inject_env=False)
        self.assertFalse(b["dynamic"]["enabled"])

    def test_inject_env_only_fills_empty_auth_code(self):
        # yaml 授权码为空: inject_env=True 注入 .env; False 不注入(设置页保存用)
        self._write("email:\n  auth_code: ''\n")
        cfg_on = cfg_mod.load_config(inject_env=True)
        self.assertEqual(cfg_on["email"]["auth_code"], "TESTCODE")
        cfg_off = cfg_mod.load_config(inject_env=False)
        self.assertEqual(cfg_off["email"]["auth_code"], "")

    def test_inject_env_does_not_override_yaml_code(self):
        self._write("email:\n  auth_code: 'YAMLCODE'\n")
        cfg = cfg_mod.load_config(inject_env=True)
        self.assertEqual(cfg["email"]["auth_code"], "YAMLCODE")  # yaml 优先

    def test_benchmark_code_default(self):
        self._write("capital: 1\n")
        self.assertEqual(cfg_mod.benchmark_code(), "000906.SH")
        self._write("capital: 1\nbenchmark: '000905.SH'\n")
        self.assertEqual(cfg_mod.benchmark_code(), "000905.SH")


class TestDataStart(unittest.TestCase):
    """训练起点 / 评估起点是两个独立旋钮(2026-09-11 用户拍板拆分)

    2026-09-10 曾用**同一个键** data_start 同时驱动训练(train_run(train_start=))
    与评估(run_backtest(start=)), 并默认 '20190722'。实测两者效应方向相反 ——
    截断训练 **-5.22pp/年**、截断评估窗 +9.59pp/年(**纯口径**: 删掉 2018 崩盘那段),
    原组合被「只改评估窗」完全支配。用户 2026-09-11 拍板恢复**全历史训练**、
    保留评估窗 2019-07-22。故此处锁死的是**结构**:

      · data_start 允许 None(= 全历史)或合法 YYYYMMDD; 若为非空值必须 <= 20190722
      · eval_start **必须存在且格式合法**(它是回测窗口的唯一来源, 误删 = 静默退回
        2017 起全窗口, 成绩与 pin 记录对不上却不报错)
      · _apply_train_start(None) 必须是恒等变换(全历史训练的落地前提)
      · _apply_train_start 传值时必须真的把该日之前的月份切掉(行为本身未变)
    """

    def test_default_config_data_start_is_none_or_valid(self):
        import re
        from app.config import DEFAULT_CONFIG
        self.assertIn("data_start", DEFAULT_CONFIG, "DEFAULT_CONFIG 缺 data_start 键")
        ds = DEFAULT_CONFIG["data_start"]
        if ds is None:
            return                      # 全历史训练(2026-09-11 起的生产口径)
        self.assertRegex(str(ds), r"^\d{8}$", "data_start 必须是 None 或 YYYYMMDD 字符串")
        self.assertLessEqual(str(ds), "20190722",
                             "训练起点不得晚于科创板开市日, 否则与实际口径不符")

    def test_default_config_has_valid_eval_start(self):
        import re
        from app.config import DEFAULT_CONFIG
        es = DEFAULT_CONFIG.get("eval_start")
        self.assertIsNotNone(es, "DEFAULT_CONFIG 缺 eval_start —— 回测窗口会静默退回全历史")
        self.assertRegex(str(es), r"^\d{8}$", "eval_start 必须是 YYYYMMDD 字符串")
        self.assertLessEqual(str(es), "20190722",
                             "评估窗起点不得晚于科创板开市日, 否则与实际口径不符")

    def test_train_start_none_is_identity(self):
        import pandas as pd
        from app.models.trainer import _apply_train_start
        md = pd.date_range("2014-01-31", "2026-08-31", freq="ME")
        for v in (None, "", 0):
            self.assertTrue(_apply_train_start(md, v).equals(md),
                            "train_start={!r} 不是恒等变换 —— 全历史训练会静默被截断".format(v))

    def test_train_start_cuts_everything_before_star_listing(self):
        import pandas as pd
        from app.models.trainer import _apply_train_start
        md = pd.date_range("2014-01-31", "2026-08-31", freq="ME")
        out = _apply_train_start(md, "20190722")
        self.assertEqual(out[0], pd.Timestamp("2019-07-31"), "首个保留月不是 2019-07")
        self.assertTrue((out >= pd.Timestamp("2019-07-22")).all(),
                        "仍有 2019-07-22 之前的月份漏过")
        self.assertLess(len(out), len(md), "起点没有切掉任何月份")
        # 幂等: 对已截断序列再截一次不变(日链增量重训会重复施加)
        self.assertTrue(out.equals(_apply_train_start(out, "20190722")))

    def test_yaml_values_are_valid(self):
        """默认模板合法不代表线上 config.yaml 合法 —— 线上值单独校验"""
        import re
        cfg = cfg_mod.load_config()
        ds = cfg.get("data_start")
        if ds is not None:
            self.assertRegex(str(ds), r"^\d{8}$", "线上 data_start 非法的非空值")
        es = cfg.get("eval_start")
        self.assertIsNotNone(es, "线上 config 未取到 eval_start —— 回测窗口会退回全历史")
        self.assertRegex(str(es), r"^\d{8}$")


class TestLiveConfigPassesValidator(unittest.TestCase):
    """**活配置**必须通过 `validate_config` —— 不是只让它打日志（2026-09-13 事故驱动）。

    起因（07 号 #20）：`fees.transfer_fee` 一度被写成 `1e-05`，本项目 YAML 装载器把它解析成
    **字符串**；运行期只在日志里报一条 ERROR（`load_config` 刻意不抛，免得一个手改错字让整条链起不来），
    于是"配置不合法"可以长期静默存在。更糟的一次是：修好之后被并行会话还原 ⇒ 提交信息声称修了、
    diff 里却没有（`git log -- config.yaml` 可查）。这条测试让**任何一次还原/手改**当场红灯。
    """

    def test_validate_config_has_no_problems(self):
        cfg = cfg_mod.load_config()
        problems = cfg_mod.validate_config(cfg)
        self.assertEqual(problems, [], f"活配置未通过校验: {problems}")

    def test_fees_are_numbers_not_strings(self):
        """费率的每个键都必须是数字 —— 字符串会绕过 `fees.<k> >= 0.05` 那条越界检查。"""
        fees = (cfg_mod.load_config().get("fees") or {})
        bad = {k: (v, type(v).__name__) for k, v in fees.items()
               if isinstance(v, bool) or not isinstance(v, (int, float))}
        self.assertEqual(bad, {}, f"config.yaml 的费率不是数字: {bad}")

    def test_exponent_without_dot_parses_as_float(self):
        """`1e-05` 必须解析成 float（2026-09-13，07 号 #21）。

        背景：PyYAML 走 YAML 1.1，其浮点正则**要求小数点** ⇒ `1e-05` 会静默变 str；
        而 `save_config` 的 ruamel round-trip 写回恰好把 float `0.00001` 写成 `1e-05`，
        于是「读 → 设置页保存 → 再读」就把数字变成字符串（校验报错 + 类型漂移）。
        这里同时钉住三条写法都用 `_ConfigLoader` 读成 float。
        """
        for text in ("fees:\n  transfer_fee: 1e-05\n",
                     "fees:\n  transfer_fee: 1e-5\n",
                     "fees:\n  transfer_fee: 1.0e-05\n"):
            with self.subTest(text=text):
                self.assertIsInstance(cfg_mod.yaml.load(text, Loader=cfg_mod._ConfigLoader)["fees"]["transfer_fee"],
                                      float)


class TestCommittedConfigHasNoSecrets(unittest.TestCase):
    """守卫「config.yaml 可以被安全提交」(2026-09-12 体检报告 C4/A1)。

    背景: config.yaml 原本被 .gitignore 排除(2026-09-04 提交 ec5e1db「安全加固」),
    但它同时是**基线唯一事实源** —— 20+ 行注释钉着 20260911_fullhist 的完整口径。
    2026-09-12 决定重新纳入跟踪, 前提是**它里面不含密钥**: 真正的密钥住在 .env
    (TUSHARE_TOKEN / HITHINK_FINANCE_API_KEY / EMAIL_AUTH_CODE), 均已 gitignore。

    没有这条守卫, 某天把 QQ 授权码填回 config.yaml 并保存, 就会随下一次提交
    永久留在 git 历史里 —— 而 .env 的忽略规则对此毫无作用。
    """

    _SECRET_KEY = re.compile(
        r"token|secret|passw|auth|api_?key|credential|access_?key", re.I)

    def test_no_secret_value_in_tracked_config(self):
        import yaml
        p = _ORIG_FILE                      # 真实路径, 不受其他用例的 CONFIG_FILE 补丁影响
        self.assertTrue(p.exists(), "线上 config.yaml 不存在: {}".format(p))
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}

        hits = []

        def walk(node, path=""):
            if isinstance(node, dict):
                for k, v in node.items():
                    walk(v, "{}.{}".format(path, k) if path else str(k))
            elif isinstance(node, list):
                for i, v in enumerate(node):
                    walk(v, "{}[{}]".format(path, i))
            elif isinstance(node, str) and node.strip():
                if self._SECRET_KEY.search(path):
                    hits.append((path, "{} 字符".format(len(node))))

        walk(raw)
        self.assertEqual(hits, [],
                         "config.yaml 出现疑似明文密钥(应放 .env, 该文件已 gitignore; "
                         "本文件自 2026-09-12 起纳入 git 跟踪): {}".format(hits))

    def test_email_auth_code_stays_empty_in_file(self):
        """授权码只有一个合法去处: .env。config.yaml 里必须留空 ——
        config.inject_env 会在读取时自动从环境回填, 不需要写进文件。"""
        import yaml
        raw = yaml.safe_load(_ORIG_FILE.read_text(encoding="utf-8")) or {}
        self.assertEqual((raw.get("email") or {}).get("auth_code", ""), "",
                         "config.yaml 里写了 email.auth_code —— 该值会被提交进 git, 请改放 .env")

    def test_env_example_lists_every_secret_key_the_app_reads(self):
        """代码在读的密钥键，`.env.example` 必须逐个列出来（2026-09-18 审查 Q-17）。

        缺键的坏法很安静：`app/config.py:53` 读 `HITHINK_FINANCE_API_KEY`，
        模板里没有 ⇒ 新环境照模板配好后 `lhb_client` 只 log.warning 一句就跳过龙虎榜
        同步 —— 表现是"龙虎榜永远不更新"，而没人知道自己少了哪个键。
        上面那个类只保证"密钥别进 git"，没保证"模板别漏键"，这是它的另一半。
        """
        p = Path(__file__).resolve().parent.parent / ".env.example"
        self.assertTrue(p.exists(), ".env.example 不见了")
        keys = {ln.split("=", 1)[0].strip() for ln in p.read_text(encoding="utf-8").splitlines()
                if ln.strip() and not ln.strip().startswith("#") and "=" in ln}
        for needed in ("TUSHARE_TOKEN", "HITHINK_FINANCE_API_KEY",
                       "EMAIL_AUTH_CODE", "LLM_API_KEY"):
            self.assertIn(needed, keys,
                          f"app 在读 {needed}，但 .env.example 没列出来")


if __name__ == "__main__":
    unittest.main()
