import contextlib
import io
import unittest

from stress_tool.launcher_messages import main, render_message


class LauncherMessagesTests(unittest.TestCase):
    def test_start_message_respects_no_browser_flag(self):
        text = render_message("start", "http://127.0.0.1:18976", "1")
        self.assertIn("不自动打开浏览器", text)
        self.assertIn("http://127.0.0.1:18976", text)

    def test_existing_service_message_is_idempotent(self):
        text = render_message("already_running", "http://127.0.0.1:18976")
        self.assertIn("已经在运行", text)
        self.assertIn("不会重复启动", text)

    def test_explicit_stop_is_reported_as_normal_shutdown(self):
        self.assertIn("正常关闭", render_message("stopped_by_user"))

    def test_unknown_message_returns_nonzero_without_traceback(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(["not-a-message"])
        self.assertEqual(code, 64)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("参数错误", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
