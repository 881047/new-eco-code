"""`POST /settings` 的**秘密泄漏**路径（2026-09-13，独立审查 §4.1 第一批）

为什么这条必须测：`config.yaml` **受 git 跟踪**，而设置页是最容易把授权码意外落盘的一次
（界面上填一次就写文件）。项目为此做了三层防护（P0-06 + N4）：

  ① 读配置时 `inject_env=False` —— 不把 `.env` 里的码注入内存再写回；
  ② 只要文件里**还有**明文码，**任何一次保存**都把它搬进 `.env`（不只在你动了 email 段时）；
  ③ 新提交的码只写 `.env`，`config.yaml` 里那份恒置空；界面回显的 `******` 视为"没改"。

本测试直接调端点函数（不走 TestClient），把 `load_config / set_env_secret / save_config`
全部替换成桩，然后**检查交到保存层的那份 cfg**（这是唯一能证明"没写明文"的地方）。
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from app.web import api                                       # noqa: E402


def _cfg_with_legacy(secret: str = None) -> dict:
    cfg = {"capital": 20000, "risk_profile": "aggressive", "max_stocks": 6,
           "industry_max_weight": 0.25, "single_side_cost": 0.003,
           "email": {"enabled": True, "smtp_server": "smtp.qq.com", "smtp_port": 465,
                     "sender": "a@qq.com", "recipients": ["b@qq.com"], "auth_code": ""}}
    if secret is not None:
        cfg["email"]["auth_code"] = secret
    return cfg


class TestSettingsNeverWritesSecret(unittest.TestCase):
    def _call(self, body, cfg):
        saved = {}
        with mock.patch.object(api.cfg_mod, "load_config", return_value=cfg) as p_load, \
                mock.patch.object(api.cfg_mod, "set_env_secret") as p_env, \
                mock.patch.object(api.cfg_mod, "save_config",
                                  side_effect=lambda c: saved.update(c)):
            out = api.post_settings(body)
        return out, p_load, p_env, saved

    def test_save_without_touching_email_still_migrates_legacy_code(self):
        """最常见的路径：进设置页只改了资金、没碰邮箱 —— 明文码必须照样被搬走。"""
        cfg = _cfg_with_legacy("LEGACYCODE")
        out, p_load, p_env, saved = self._call({"capital": 30000}, cfg)
        self.assertTrue(out["ok"])
        p_env.assert_called_once_with("EMAIL_AUTH_CODE", "LEGACYCODE")
        self.assertEqual(saved["email"]["auth_code"], "",
                         "残余明文码没有从将要保存的 cfg 里清掉 ⇒ 会被写进受跟踪的 config.yaml")
        # 读配置必须关掉 .env 注入
        self.assertEqual(p_load.call_args.kwargs.get("inject_env"), False)

    def test_new_code_goes_to_env_not_to_file(self):
        cfg = _cfg_with_legacy()
        body = {"email": {"auth_code": "BRANDNEWCODE", "smtp_server": "smtp.qq.com",
                          "smtp_port": 465, "sender": "a@qq.com", "recipients": ["b@qq.com"]}}
        out, _, p_env, saved = self._call(body, cfg)
        self.assertTrue(out["ok"])
        p_env.assert_called_once_with("EMAIL_AUTH_CODE", "BRANDNEWCODE")
        self.assertEqual(saved["email"]["auth_code"], "",
                         "新授权码被写进了 config.yaml ⇒ 等于提交进 git")

    def test_masked_placeholder_is_not_treated_as_new_code(self):
        """界面回显的 `******` 表示"没改"，不许当成新码往 .env 里灌。"""
        cfg = _cfg_with_legacy()
        body = {"email": {"auth_code": "******", "smtp_server": "smtp.qq.com",
                          "smtp_port": 465, "sender": "a@qq.com", "recipients": ["b@qq.com"]}}
        out, _, p_env, saved = self._call(body, cfg)
        self.assertTrue(out["ok"])
        p_env.assert_not_called()
        self.assertEqual(saved["email"]["auth_code"], "")


if __name__ == "__main__":
    unittest.main()
