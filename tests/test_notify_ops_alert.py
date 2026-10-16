"""运维告警出口（2026-09-13）

它在链路里的位置很特殊：**服务已经挂了**的时候才会被调用 ⇒ 它自己绝对不能抛异常、
也不能因为邮件没配好就崩。守四条：① 未配置/未启用 ⇒ 返回 False 且不外呼；
② 配好了 ⇒ 走生产发送实现（`_send_with_retry`）并落 notify_log；③ 发送抛异常 ⇒ 吞掉返回 False；
④ 命令行入口（看门狗调的那个）缺参数时必须非零退出，不许"静默成功"。
"""
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from app import notify  # noqa: E402


class TestSendOpsAlert(unittest.TestCase):
    def test_disabled_config_returns_false_without_sending(self):
        with mock.patch.object(notify.cfg_mod, "load_config", return_value={"email": {}}), \
                mock.patch.object(notify, "_send_with_retry") as p_send:
            self.assertFalse(notify.send_ops_alert("标题", "正文"))
            p_send.assert_not_called()

    def test_enabled_uses_production_sender(self):
        cfg = {"email": {"enabled": True, "sender": "a@b.c", "auth_code": "x",
                         "recipients": ["d@e.f"]}}
        with mock.patch.object(notify.cfg_mod, "load_config", return_value=cfg), \
                mock.patch.object(notify, "_send_with_retry", return_value=True) as p_send:
            self.assertTrue(notify.send_ops_alert("服务自愈: 已自动拉起", "第一行\n第二行"))
            self.assertTrue(p_send.called)
            msg, tag = p_send.call_args.args[1], p_send.call_args.args[2]
            self.assertIn("运维", tag)
            self.assertIn("服务自愈", str(msg["Subject"]))
            self.assertIn("第一行", msg.get_payload(decode=True).decode("utf-8"))

    def test_sender_exception_is_swallowed(self):
        cfg = {"email": {"enabled": True, "sender": "a@b.c", "auth_code": "x",
                         "recipients": ["d@e.f"]}}
        with mock.patch.object(notify.cfg_mod, "load_config", return_value=cfg), \
                mock.patch.object(notify, "_send_with_retry", side_effect=RuntimeError("smtp 挂了")):
            self.assertFalse(notify.send_ops_alert("标题", "正文"))

    def test_cli_requires_arguments(self):
        """看门狗调的就是这个脚本：参数缺失必须非零退出，不能"静默成功"。"""
        p = subprocess.run([sys.executable, "-X", "utf8",
                            str(BASE / "scripts" / "alert_service.py")],
                           capture_output=True, timeout=60)
        self.assertNotEqual(p.returncode, 0)


if __name__ == "__main__":
    unittest.main()
