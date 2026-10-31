"""Renderer fixtures only: no real sessions, socket connections or tool calls."""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import shlex
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from rollout_render import RenderedRecord, parse_rollout_line


def line(payload, root_type="response_item", **record_fields):
    return json.dumps({"type": root_type, "payload": payload, **record_fields})


def function(arguments, **fields):
    return line({"type": "function_call", "name": "exec_command",
                 "arguments": arguments, **fields})


def custom(value, **fields):
    return line({"type": "custom_tool_call", "name": "exec",
                 "input": value, **fields})


class RolloutRendererTests(unittest.TestCase):
    def test_all_non_object_roots_are_diagnostic(self):
        for value in (None, [], "text", 123, True):
            with self.subTest(value=value):
                rendered = parse_rollout_line(json.dumps(value))
                self.assertIsInstance(rendered, RenderedRecord)
                self.assertIn("根必须是 object", rendered.diagnostic)
                self.assertIsNone(rendered.text)

    def test_bad_json_including_nonfinite_numbers_is_diagnostic(self):
        for value in ('{"type":', '{"x": NaN}', '{"x": 1e999}', 'not-json'):
            with self.subTest(value=value):
                self.assertIn("坏 JSON", parse_rollout_line(value).diagnostic)

    def test_non_string_line_is_diagnostic(self):
        for value in (None, b"{}", [], 12):
            self.assertIsNotNone(parse_rollout_line(value).diagnostic)

    def test_payload_shape_and_types_never_crash(self):
        for payload in (None, [], "text", 12):
            self.assertIn("payload 必须", parse_rollout_line(line(payload)).diagnostic)
        for payload_type in (None, [], {}, True):
            self.assertIn("payload.type", parse_rollout_line(line({"type": payload_type})).diagnostic)
        self.assertIn("记录 type", parse_rollout_line(line({}, root_type=[])).diagnostic)

    def test_unknown_record_and_payload_types_are_visible_diagnostics(self):
        for root_type, payload_type in (("future_root", "message"),
                                        ("event_msg", "future_event"),
                                        ("response_item", "future_response")):
            rendered = parse_rollout_line(line({"type": payload_type}, root_type))
            self.assertIn("未知", rendered.diagnostic)
            self.assertEqual(rendered.event_type, payload_type)

    def test_known_noise_can_be_skipped_without_diagnostic(self):
        values = [line({}, "session_meta"), line({}, "turn_context"),
                  line({"type": "token_count"}, "event_msg"),
                  line({"type": "user_message"}, "event_msg"),
                  line({"type": "reasoning"}),
                  line({"type": "message", "role": "user", "content": []})]
        for value in values:
            rendered = parse_rollout_line(value)
            self.assertIsNone(rendered.text)
            self.assertIsNone(rendered.diagnostic)

    def test_app_0159_record_types_are_known_and_quiet(self):
        """2026-10-03 本机 0.159.2 真实 rollout 实证类型：合法落盘，不诊断不显示。"""
        values = [line({"thread_id": "t", "usage": {}}, "token_usage_record"),
                  line({"full": True, "state": {}}, "world_state"),
                  line({"thread_id": "t"}, "inter_agent_communication_metadata"),
                  line({"type": "item_completed"}, "event_msg"),
                  line({"type": "item_started"}, "event_msg"),
                  line({"type": "thread_settings_applied"}, "event_msg"),
                  # 多代理内部通信：与 message 通道零重叠（实证），event_type 仍流通
                  line({"type": "agent_message", "id": "amsg_1", "author": "/root/docs",
                        "recipient": "/root",
                        "content": [{"type": "output_text", "text": "internal"}]})]
        for value in values:
            with self.subTest(value=value):
                rendered = parse_rollout_line(value)
                self.assertIsNone(rendered.text)
                self.assertIsNone(rendered.diagnostic)
        agent = parse_rollout_line(values[-1])
        self.assertEqual(agent.event_type, "agent_message")

    def test_unknown_types_carry_stable_dedup_key(self):
        """未知类型诊断带稳定 key 供调用方降采样；结构性真异常无 key（不降采样）。"""
        rendered = parse_rollout_line(line({"type": "future_event"}, "event_msg"))
        self.assertIn("未知", rendered.diagnostic)
        self.assertEqual(rendered.diagnostic_key, "unknown_event_msg:future_event")
        rendered = parse_rollout_line(line({"type": "message"}, "future_root"))
        self.assertEqual(rendered.diagnostic_key, "unknown_root:future_root")
        rendered = parse_rollout_line(line({"type": "future_response"}))
        self.assertEqual(rendered.diagnostic_key, "unknown_response_item:future_response")
        # 结构性诊断（payload.type 非字符串/坏 JSON）每次都应上报：key 为 None
        self.assertIsNone(parse_rollout_line(line({"type": None})).diagnostic_key)
        self.assertIsNone(parse_rollout_line("{bad json").diagnostic_key)

    def test_structured_turn_events_survive_every_display_filter(self):
        statuses = {"task_started": None, "task_complete": "completed",
                    "turn_aborted": "aborted", "task_failed": "failed"}
        for event_type, status in statuses.items():
            for only in (None, "agentMessage", "command", "file"):
                rendered = parse_rollout_line(line({"type": event_type, "turn_id": "T-2"}, "event_msg"), only)
                self.assertEqual(rendered.event_type, event_type)
                self.assertEqual(rendered.turn_id, "T-2")
                self.assertEqual(rendered.terminal_status, status)
                self.assertIsNone(rendered.diagnostic)
                if only is None:
                    self.assertIsNotNone(rendered.text)
                    self.assertNotIn("直播收尾", rendered.text)

    def test_turn_id_alias_and_invalid_binding(self):
        self.assertEqual(parse_rollout_line(line({"type": "task_started", "turnId": "T-1"}, "event_msg")).turn_id, "T-1")
        for value in (None, "", " ", [], 7):
            rendered = parse_rollout_line(line({"type": "task_complete", "turn_id": value}, "event_msg"))
            self.assertEqual(rendered.terminal_status, "completed")
            self.assertIsNone(rendered.turn_id)
            self.assertIn("无法绑定", rendered.diagnostic)

    def test_timestamp_conversion_is_deterministic(self):
        """断言格式与动态期望值，不死时区（_stamp 本地时区自适应）。"""
        payload = {"type": "task_started", "turn_id": "T"}
        instant = "2026-10-02T18:00:00Z"
        rendered = parse_rollout_line(line(payload, "event_msg", timestamp=instant))
        expected = datetime(2026, 10, 2, 18, 0, 0,
                            tzinfo=timezone.utc).astimezone().strftime("%H:%M:%S")
        self.assertTrue(rendered.text.startswith(expected))
        for timestamp in (None, "bad-time", "2026-10-02T18:00:00", {}):
            self.assertTrue(parse_rollout_line(line(payload, "event_msg", timestamp=timestamp)).text.startswith("--:--:--"))

    def test_timestamp_stamp_applies_local_timezone_offset(self):
        """输出带本地时区偏移：同一时刻的不同时区写法归一到同一本地墙钟；
        非 UTC 机器上渲染结果必不同于 UTC 裸墙钟（偏移确实生效）。"""
        payload = {"type": "task_started", "turn_id": "T"}
        utc_text = parse_rollout_line(
            line(payload, "event_msg", timestamp="2026-10-02T18:00:00Z")).text
        shifted_text = parse_rollout_line(
            line(payload, "event_msg", timestamp="2026-10-02T20:00:00+02:00")).text
        self.assertEqual(utc_text, shifted_text)
        local_offset = datetime.now().astimezone().utcoffset()
        if local_offset is not None and local_offset != timedelta(0):
            self.assertFalse(utc_text.startswith("18:00:00"),
                             "非 UTC 机器渲染 UTC 裸墙钟 = 时区偏移未生效")

    def test_assistant_message_escapes_linebreaks_and_controls(self):
        message = 'line1\nline2\r\t"quote"\x1b[31m\x7f\u2028\u202e\ud800'
        rendered = parse_rollout_line(line({"type": "message", "role": "assistant", "content": [{"text": message}]}))
        self.assertIn('line1\\nline2\\r\\t\\"quote\\"', rendered.text)
        for value in ("\n", "\r", "\t", "\x1b", "\x7f", "\u2028", "\u202e", "\ud800"):
            self.assertNotIn(value, rendered.text)
        rendered.text.encode("utf-8")

    def test_long_message_has_explicit_truncation_marker(self):
        rendered = parse_rollout_line(line({"type": "message", "role": "assistant", "content": [{"text": "a" * 200}]}))
        self.assertIn("…[省略]", rendered.text)

    def test_null_text_and_malformed_content_are_diagnostic(self):
        for content in (None, {}, [None], [{"text": None}], [{"text": []}], [{"image_url": "x"}]):
            rendered = parse_rollout_line(line({"type": "message", "role": "assistant", "content": content}))
            self.assertIsNotNone(rendered.diagnostic)
        rendered = parse_rollout_line(line({"type": "message", "role": "assistant", "content": [{"text": "valid"}, {"text": None}]}))
        self.assertIn("valid", rendered.text)
        self.assertIsNotNone(rendered.diagnostic)

    def test_unknown_role_and_empty_assistant_message(self):
        self.assertIsNotNone(parse_rollout_line(line({"type": "message", "role": []})).diagnostic)
        rendered = parse_rollout_line(line({"type": "message", "role": "assistant", "content": []}))
        self.assertIsNone(rendered.text)
        self.assertIsNone(rendered.diagnostic)

    def test_function_arguments_can_be_object_or_json_object_string(self):
        for arguments in ({"cmd": "echo hello"}, json.dumps({"command": "echo hello"})):
            rendered = parse_rollout_line(function(arguments, namespace="functions"))
            self.assertIn("[命令] functions.exec_command: echo hello", rendered.text)
            self.assertIsNone(rendered.diagnostic)

    def test_invalid_arguments_are_diagnostic_instead_of_replaced_with_empty(self):
        for arguments in (None, [], 5, "null", "[]", "broken json", '"text"'):
            rendered = parse_rollout_line(function(arguments))
            self.assertIn("arguments", rendered.diagnostic)
            self.assertIsNone(rendered.text)

    def test_list_command_uses_shell_quoting(self):
        command = ["printf", "%s", "with spaces", 'a"b', "a'b"]
        rendered = parse_rollout_line(function({"command": command}))
        # A quoted summary may escape double quotes, but must retain shell token quoting.
        expected = shlex.join(command).replace('"', '\\"')
        self.assertIn(expected, rendered.text)
        self.assertIsNone(rendered.diagnostic)

    def test_mixed_command_list_is_diagnostic(self):
        for command in (["echo", None], ["echo", 7], {"cmd": "echo"}, 12, None):
            rendered = parse_rollout_line(function({"command": command}))
            self.assertIn("纯字符串列表", rendered.diagnostic)
            self.assertNotIn("[命令]", rendered.text)

    def test_duration_validates_type_sign_and_finiteness_even_with_command(self):
        for duration in ("1000", True, None, -1, {}, [], 10 ** 400):
            rendered = parse_rollout_line(function({"cmd": "echo hello", "duration_ms": duration}))
            self.assertIn("duration_ms", rendered.diagnostic)
            self.assertIn("[命令]", rendered.text)
        for duration in (0, 1500, 0.5):
            rendered = parse_rollout_line(function({"duration_ms": duration}))
            self.assertIn(f"duration={duration / 1000:g}s", rendered.text)
            self.assertIsNone(rendered.diagnostic)
        for duration in ("NaN", "Infinity", "-Infinity", "1e999"):
            value = '{"type":"response_item","payload":{"type":"function_call","arguments":{"duration_ms":' + duration + '}}}'
            self.assertIn("坏 JSON", parse_rollout_line(value).diagnostic)

    def test_invalid_label_fields_cannot_crash_or_inject_lines(self):
        rendered = parse_rollout_line(function({}, name=[], namespace={}))
        self.assertIsNotNone(rendered.diagnostic)
        rendered = parse_rollout_line(function({}, namespace="functions\nforged"))
        self.assertIn("functions\\nforged", rendered.text)
        self.assertNotIn("\n", rendered.text)

    def test_custom_text_multiline_quotes_and_many_cmds_are_not_guessed(self):
        values = [
            'await tools.exec_command({ cmd: "echo first" });\nawait tools.exec_command({cmd:"echo second"});',
            "await tools.exec_command({cmd='echo quoted'});",
            'const c = {cmd:"printf \\"a\\""};',
            '{"cmd":"echo first"}\n{"cmd":"echo second"}',
        ]
        for value in values:
            rendered = parse_rollout_line(custom(value, namespace="functions"))
            self.assertIn("[工具] functions.exec", rendered.text)
            self.assertIn("自由文本摘要", rendered.text)
            self.assertNotIn("[命令]", rendered.text)
            self.assertNotIn("\n", rendered.text)
            self.assertIsNone(rendered.diagnostic)

    def test_custom_input_only_whole_json_command_object_gets_command_label(self):
        for value in ({"cmd": "echo a\necho b"}, '{"command":["echo","with spaces"]}'):
            rendered = parse_rollout_line(custom(value))
            self.assertIn("[命令]", rendered.text)
            self.assertNotIn("\n", rendered.text)
            self.assertIsNone(rendered.diagnostic)
        for value in ([], None, 12):
            self.assertIsNotNone(parse_rollout_line(custom(value)).diagnostic)
        for value in ("[]", "null", '{"duration_ms":1000}', "prefix {\"cmd\":\"echo x\"}"):
            self.assertNotIn("[命令]", parse_rollout_line(custom(value)).text)

    def test_file_events_and_filters(self):
        for event_type in ("file_change", "local_file_change"):
            value = line({"type": event_type})
            self.assertIn("[文件]", parse_rollout_line(value, "file").text)
            self.assertIsNone(parse_rollout_line(value, "command").text)
        value = function({"cmd": ["echo", None]})
        self.assertIsNone(parse_rollout_line(value, "file").text)
        self.assertIsNotNone(parse_rollout_line(value, "file").diagnostic)
        self.assertIn("filter", parse_rollout_line(value, "future-filter").diagnostic)

    def test_deep_or_malformed_input_reports_instead_of_recursing_out(self):
        value = '{"type":"response_item","payload":{"type":"function_call","arguments":' + '[' * 1500 + '0' + ']' * 1500 + '}}'
        self.assertIsNotNone(parse_rollout_line(value).diagnostic)


if __name__ == "__main__":
    unittest.main()
