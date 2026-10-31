"""Config and desktop reminder tests: all file/process boundaries are isolated."""
import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import codex_bridge as bridge


class BridgeIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="bridge-integration-test-")
        self.addCleanup(self.temp.cleanup)
        self.config = Path(self.temp.name) / "config.json"
        patcher = mock.patch.object(bridge, "CONFIG_PATH", self.config)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_nonobject_and_invalid_configs_warn_and_fall_back(self):
        for value in ("[]", "null", "42", '"value"', "{bad"):
            with self.subTest(config=value):
                self.config.write_text(value)
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    self.assertEqual(bridge.effective_model(), bridge.FALLBACK_MODEL)
                self.assertIn("WARN:", stderr.getvalue())

    def test_non_utf8_config_warns_without_crashing(self):
        self.config.write_bytes(b"\xff")
        with contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(bridge.load_config(), {})
        self.assertIn("WARN:", stderr.getvalue())

    def test_model_priority_keeps_cli_config_and_fallback(self):
        self.config.write_text(json.dumps({"default_model": " gpt-6-astra "}))
        self.assertEqual(bridge.effective_model("gpt-6-sol"), "gpt-6-sol")
        self.assertEqual(bridge.effective_model(), "gpt-6-astra")
        self.config.write_text('{"default_model": 42}')
        self.assertEqual(bridge.effective_model(), bridge.FALLBACK_MODEL)
        self.config.unlink()
        self.assertEqual(bridge.effective_model(), bridge.FALLBACK_MODEL)

    def test_watch_help_does_not_load_model_configuration(self):
        with mock.patch.object(bridge, "load_config", side_effect=AssertionError("must not read config")), \
                mock.patch.object(sys, "argv", ["codex_bridge", "watch", "--help"]), \
                contextlib.redirect_stdout(io.StringIO()) as stdout:
            with self.assertRaises(SystemExit) as exit_context:
                bridge.main()
        self.assertEqual(exit_context.exception.code, 0)
        self.assertIn("--anchor", stdout.getvalue())
        self.assertIn("--idle-timeout", stdout.getvalue())

    def args(self, prepare_only=False):
        return SimpleNamespace(
            ephemeral=False, name='topic"; do shell script "ignored"',
            transport="desktop", open_bg=False, no_open=False,
            prepare_only=prepare_only, async_fire=False,
            task="test prompt", cwd=self.temp.name, model="gpt-6.1-sol",
            effort="high", timeout=1)

    def dispatch(self, process_result=None, process_error=None, prepare_only=False):
        output, error = io.StringIO(), io.StringIO()
        receipt = {"status": "prepared" if prepare_only else "open_request_sent",
                   "request_id": "fixture", "needs_gui_submit": True}
        with mock.patch.object(bridge, "prepare_dispatch", return_value=receipt) as prepare, \
                mock.patch.object(bridge.subprocess, "run", return_value=process_result,
                                  side_effect=process_error) as run, \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
            bridge.cmd_dispatch(self.args(prepare_only))
        return json.loads(output.getvalue()), error.getvalue(), run, prepare

    def test_reminder_success_reports_request_only_and_keeps_title_in_argv(self):
        result, error, run, _ = self.dispatch(SimpleNamespace(returncode=0))
        self.assertEqual(result["notification_status"], "requested")
        argv = run.call_args.args[0]
        self.assertEqual(argv[:2], ["osascript", "-e"])
        self.assertNotIn(self.args().name, argv[2], "user title must not be script source")
        self.assertIn(self.args().name, argv[3])
        self.assertIn("已请求打开", argv[4])
        self.assertNotIn("任务已预填", argv[4])
        self.assertIn("用户在 App 核对并亲手提交一次", error)
        self.assertEqual(result["status"], "open_request_sent")

    def test_nonzero_reminder_is_reported_without_aborting_preparation(self):
        result, error, _, _ = self.dispatch(SimpleNamespace(returncode=9))
        self.assertEqual(result["notification_status"], "failed")
        self.assertIn("WARN:", error)
        self.assertIn("rc=9", error)
        self.assertEqual(result["status"], "open_request_sent")

    def test_exception_reminder_is_reported_without_aborting_preparation(self):
        for problem in (OSError("fixture"), subprocess.TimeoutExpired("fixture", 10)):
            with self.subTest(problem=type(problem).__name__):
                result, error, _, _ = self.dispatch(process_error=problem)
                self.assertEqual(result["notification_status"], "failed")
                self.assertIn("WARN:", error)
                self.assertEqual(result["status"], "open_request_sent")

    def test_prepare_only_never_requests_a_reminder(self):
        result, _, run, prepare = self.dispatch(prepare_only=True)
        self.assertEqual(result["notification_status"], "skipped")
        run.assert_not_called()
        self.assertTrue(prepare.call_args.kwargs["prepare_only"])

    def test_dispatch_does_not_duplicate_title_already_in_task(self):
        """任务文本首行已含同款【WB委派】标题时尊重原文，不重复拼接（2026-10-03 实测发现撞衫）。"""
        args = self.args()
        title = f"【WB委派·{time.strftime('%y%m%d')}·{args.name}】"
        args.task = f"{title}\n正文内容"
        with mock.patch.object(bridge, "prepare_dispatch", return_value={
                "status": "open_request_sent", "request_id": "fixture",
                "needs_gui_submit": True}) as prepare, \
                mock.patch.object(bridge.subprocess, "run",
                                  return_value=SimpleNamespace(returncode=0)), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            bridge.cmd_dispatch(args)
        task_text = prepare.call_args.args[1]
        self.assertEqual(task_text, args.task)
        self.assertEqual(task_text.count(title), 1)

    def test_dispatch_prepends_title_once_when_task_lacks_it(self):
        """任务文本无标题时拼接一次且仅一次（命名铁律不回归）。"""
        _, _, _, prepare = self.dispatch(SimpleNamespace(returncode=0))
        title = f"【WB委派·{time.strftime('%y%m%d')}·{self.args().name}】"
        task_text = prepare.call_args.args[1]
        self.assertTrue(task_text.startswith(title + "\n"))
        self.assertEqual(task_text.count(title), 1)

    def test_stdio_constructor_oserror_becomes_humanized_bridge_error(self):
        """Popen 构造期 OSError（cwd 被删/权限缺失）必须翻译成人话，不裸 traceback。"""
        with self.assertRaises(bridge.BridgeError) as context:
            bridge.AppServer(self.temp.name + "/vanished-cwd", transport="stdio")
        message = str(context.exception)
        self.assertNotIn("Traceback", message)
        # 本机有 CODEX_BIN 时走 Popen 失败文案；无二进制时走"找不到"文案，均为 BridgeError
        self.assertTrue("无法启动" in message or "找不到 codex" in message)

    def test_vanished_cwd_getcwd_failure_is_humanized(self):
        """cwd 失效时 os.getcwd() 层抛 BridgeError 人话（QA Round 1 复现路径），保留原始 errno。"""
        for problem in (FileNotFoundError(2, "No such file or directory"),
                        PermissionError(13, "Permission denied")):
            with self.subTest(problem=type(problem).__name__), \
                    mock.patch("os.getcwd", side_effect=problem):
                with self.assertRaises(bridge.BridgeError) as context:
                    bridge._safe_getcwd()
            message = str(context.exception)
            self.assertIn("当前工作目录已失效", message)
            self.assertIn("cd 到有效目录", message)
            self.assertIn(str(problem.errno), message, "原始 errno 必须保留在人话里")

    def test_main_status_with_vanished_cwd_exits_1_without_traceback(self):
        """端到端：status 在失效 cwd 下 exit 1 + 人话 stderr + 零 traceback。"""
        with mock.patch("os.getcwd",
                        side_effect=FileNotFoundError(2, "No such file or directory")), \
                mock.patch.object(sys, "argv", ["codex_bridge", "status", "--transport", "stdio"]), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()) as stderr:
            with self.assertRaises(SystemExit) as exit_context:
                bridge.main()
        self.assertEqual(exit_context.exception.code, 1)
        self.assertIn("当前工作目录已失效", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())

    def test_main_dispatch_relative_cwd_with_vanished_process_cwd_exits_1(self):
        """dispatch 相对 --cwd + 进程 cwd 失效：abspath 层人话化（QA Known Issue 缓修），
        exit 1 + 保留原始 errno + 指引 + 零 traceback。"""
        with mock.patch("os.getcwd",
                        side_effect=FileNotFoundError(2, "No such file or directory")), \
                mock.patch.object(sys, "argv", ["codex_bridge", "dispatch",
                                                "--task", "x", "--cwd", "relative/path",
                                                "--name", "topic"]), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()) as stderr:
            with self.assertRaises(SystemExit) as exit_context:
                bridge.main()
        self.assertEqual(exit_context.exception.code, 1)
        self.assertIn("无法解析 --cwd 的绝对路径", stderr.getvalue())
        self.assertIn("Errno 2", stderr.getvalue(), "原始 errno 必须保留在人话里")
        self.assertIn("传绝对路径", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())

    def test_cli_parse_errors_exit_2_without_starting_dispatch_or_backend(self):
        cases = [
            ["dispatch", "--name", "fixture"],
            ["desktop-confirm"],
            ["dispatch", "--task", "x", "--name", "fixture", "--timeout", "0"],
            ["dispatch", "--task", "x", "--name", "fixture",
             "--transport", "daemon", "--async", "--timeout", "nan"],
            ["steer", "fixture", "--text", "x", "--timeout", "0"],
            ["read"],
        ]
        for argv in cases:
            with self.subTest(argv=argv), \
                    mock.patch.object(sys, "argv", ["codex_bridge", *argv]), \
                    mock.patch.object(bridge, "AppServer") as server, \
                    mock.patch.object(bridge, "prepare_dispatch") as prepare, \
                    contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    bridge.main()
                self.assertEqual(error.exception.code, 2)
                server.assert_not_called()
                prepare.assert_not_called()

    def test_business_dispatch_parameter_error_exits_1(self):
        with mock.patch.object(sys, "argv", ["codex_bridge", "dispatch", "--task", "x"]), \
                mock.patch.object(bridge, "prepare_dispatch") as prepare, \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as error:
                bridge.main()
        self.assertEqual(error.exception.code, 1)
        prepare.assert_not_called()

    def test_desktop_confirm_exit_codes_follow_state(self):
        for status, expected in (("created", 0), ("mismatch", 2), ("ambiguous", 2),
                                 ("awaiting_desktop_submit", 3), ("pending", 3)):
            with self.subTest(status=status), \
                    mock.patch.object(bridge, "confirm_dispatch", return_value={"status": status}), \
                    mock.patch.object(sys, "argv", ["codex_bridge", "desktop-confirm", "fixture"]), \
                    contextlib.redirect_stdout(io.StringIO()):
                if expected == 0:
                    bridge.main()
                else:
                    with self.assertRaises(SystemExit) as error:
                        bridge.main()
                    self.assertEqual(error.exception.code, expected)


if __name__ == "__main__":
    unittest.main()
