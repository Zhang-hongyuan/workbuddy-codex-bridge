"""Pure, defensive rendering of complete rollout JSONL records.

This module never reads a session file or invokes a tool. Display filters affect
only ``text``; structured turn events and diagnostics always reach the caller.
"""

from dataclasses import dataclass
from datetime import datetime
import json
import math
import shlex
import unicodedata
from typing import Optional


@dataclass(frozen=True)
class RenderedRecord:
    text: Optional[str] = None
    event_type: Optional[str] = None
    turn_id: Optional[str] = None
    terminal_status: Optional[str] = None
    diagnostic: Optional[str] = None
    # 未知类型的诊断携带稳定 key，供调用方按类型降采样（每类型只 WARN 一次）；
    # None 表示结构性真异常（坏 JSON/形状错误），每次都应上报，不降采样。
    diagnostic_key: Optional[str] = None


# 2026-10-03 追平桌面 App 0.159.2 实证：token_usage_record / world_state /
# inter_agent_communication_metadata 是合法落盘记录（payload 无 type 字段），
# 未注册时每 710 条真实记录触发 396 条 WARN，直播被噪声淹没。
_NOISE_ROOT_TYPES = {"session_meta", "turn_context", "compacted",
                     "token_usage_record", "world_state",
                     "inter_agent_communication_metadata"}
_NOISE_EVENT_TYPES = {
    "token_count", "user_message", "agent_message", "agent_reasoning",
    "agent_reasoning_raw_content", "context_compacted", "turn_diff",
    # App 0.159.2 高频事件：item 生命周期通知与线程设置应用，与 response_item
    # 通道冗余，静默不丢直播内容。
    "item_completed", "item_started", "thread_settings_applied",
}
_NOISE_RESPONSE_TYPES = {
    "reasoning", "function_call_output", "custom_tool_call_output",
    "tool_result", "web_search_call",
}
_TURN_STATUS = {
    "task_started": None,
    "task_complete": "completed",
    "turn_aborted": "aborted",
    "task_failed": "failed",
}
_TURN_TEXT = {
    "task_started": "[turn] task_started：turn 开始",
    "task_complete": "[turn] task_complete：收到完成事件",
    "turn_aborted": "[turn] turn_aborted：收到中止事件",
    "task_failed": "[turn] task_failed：收到失败事件",
}


def _summary(value, limit=160):
    """Bounded single-line text, including explicit control-character escapes."""
    escapes = {"\n": "\\n", "\r": "\\r", "\t": "\\t", "\b": "\\b",
               "\f": "\\f", "\\": "\\\\", '"': '\\"'}
    parts, size = [], 0
    for character in value:
        escaped = escapes.get(character)
        if escaped is None:
            if (unicodedata.category(character) in {"Cc", "Cf", "Cs"}
                    or character in {"\u2028", "\u2029"}):
                code = ord(character)
                escaped = (f"\\u{code:04x}" if code <= 0xffff
                           else f"\\U{code:08x}")
            else:
                escaped = character
        if size + len(escaped) > limit:
            return "".join(parts) + "…[省略]"
        parts.append(escaped)
        size += len(escaped)
    return "".join(parts)


def _stamp(value):
    # Missing/invalid timestamps must not depend on the caller's current clock.
    if not isinstance(value, str):
        return "--:--:--"
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            return "--:--:--"
        # 本地时区自适应（2026-10-03 定型）：aware dt 调无参 astimezone() 即转
        # 本机时区墙钟；UTC+8 机器输出与原硬编码一致，跨时区机器不再错位。
        # 注意陷阱：无参 astimezone 只对 aware dt 安全（naive 会被当本地时间），
        # 故上面 tzinfo is None 必须先挡掉。
        return dt.astimezone().strftime("%H:%M:%S")
    except (ValueError, OverflowError):
        return "--:--:--"


def _reject_constant(value):
    raise ValueError(f"non-JSON numeric constant: {value}")


def _finite_float(value):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("numeric value is outside finite float range")
    return result


def _decode(value):
    return json.loads(value, parse_constant=_reject_constant, parse_float=_finite_float)


def _serialized(value):
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        return "<无法安全序列化>"


def _diagnostic(message, value=None):
    if value is None:
        return message
    return f"{message}；摘要={_summary(_serialized(value), 100)}"


def _tool_label(payload):
    name, namespace = payload.get("name", "?"), payload.get("namespace", "")
    diagnostics = []
    if not isinstance(name, str) or not name:
        name = "?"
        diagnostics.append("工具 name 必须是非空字符串")
    if not isinstance(namespace, str):
        namespace = ""
        diagnostics.append("工具 namespace 必须是字符串")
    label = f"{namespace}.{name}" if namespace else name
    return _summary(label, 100), diagnostics


def _arguments(value):
    if isinstance(value, dict):
        return value, None
    if isinstance(value, str):
        try:
            decoded = _decode(value)
        except (ValueError, TypeError, RecursionError):
            return None, _diagnostic("工具 arguments 不是合法 JSON object", value)
        if isinstance(decoded, dict):
            return decoded, None
    return None, _diagnostic("工具 arguments 必须是 JSON object 或其 JSON 字符串", value)


def _render_arguments(stamp, label, arguments):
    diagnostics = []
    command_key = "command" if "command" in arguments else "cmd" if "cmd" in arguments else None
    command = None
    if command_key is not None:
        value = arguments[command_key]
        if isinstance(value, str):
            command = value
        elif isinstance(value, list) and all(isinstance(item, str) for item in value):
            command = shlex.join(value)
        else:
            diagnostics.append(_diagnostic(f"{command_key} 必须是字符串或纯字符串列表", value))

    duration = None
    if "duration_ms" in arguments:
        value = arguments["duration_ms"]
        valid = isinstance(value, (int, float)) and not isinstance(value, bool)
        try:
            valid = valid and math.isfinite(value) and value >= 0
        except OverflowError:
            valid = False
        if valid:
            duration = value / 1000
        else:
            diagnostics.append(_diagnostic("duration_ms 必须是有限非负 number", value))

    if command is not None:
        text = f"{stamp} [命令] {label}: {_summary(command, 160) or '(空命令)'}"
    elif duration is not None:
        text = f"{stamp} [工具] {label}(duration={duration:g}s)"
    else:
        text = f"{stamp} [工具] {label}({_summary(_serialized(arguments), 100)})"
    return text, diagnostics


def parse_rollout_line(line, only=None):
    """Parse one complete JSONL line without exceptions for unfamiliar shapes.

    ``only`` is ``None``, ``agentMessage``, ``command`` or ``file``. Turn events
    are returned independently of that display filter. ``terminal_status`` is
    merely an observed event; deciding whether it ends a watch belongs to its
    caller, which must bind the corresponding ``turn_id``.
    """
    if only not in (None, "agentMessage", "command", "file"):
        return RenderedRecord(diagnostic="未知显示 filter")
    if not isinstance(line, str):
        return RenderedRecord(diagnostic="JSONL 记录必须是字符串")
    try:
        record = _decode(line)
    except (ValueError, TypeError, RecursionError):
        return RenderedRecord(diagnostic=f"坏 JSON 完整记录；摘要={_summary(line, 100)}")
    if not isinstance(record, dict):
        return RenderedRecord(diagnostic=_diagnostic("JSONL 根必须是 object", record))

    root_type, payload = record.get("type"), record.get("payload")
    if not isinstance(root_type, str):
        return RenderedRecord(diagnostic=_diagnostic("记录 type 必须是字符串", root_type))
    if not isinstance(payload, dict):
        return RenderedRecord(diagnostic=_diagnostic("payload 必须是 object", payload))
    if root_type in _NOISE_ROOT_TYPES:
        return RenderedRecord()
    event_type = payload.get("type")
    if not isinstance(event_type, str):
        return RenderedRecord(diagnostic=_diagnostic("payload.type 必须是字符串", event_type))
    stamp = _stamp(record.get("timestamp"))

    if root_type == "event_msg":
        if event_type in _TURN_STATUS:
            turn_id = payload.get("turn_id", payload.get("turnId"))
            diagnostic = None
            if not isinstance(turn_id, str) or not turn_id.strip():
                turn_id = None
                diagnostic = f"{event_type} 缺少有效 turn_id，无法绑定轮次"
            text = f"{stamp} {_TURN_TEXT[event_type]}"
            if turn_id is not None:
                text += f" (turn={_summary(turn_id, 80)})"
            return RenderedRecord(text=text if only is None else None,
                                  event_type=event_type, turn_id=turn_id,
                                  terminal_status=_TURN_STATUS[event_type],
                                  diagnostic=diagnostic)
        if event_type in _NOISE_EVENT_TYPES:
            return RenderedRecord(event_type=event_type)
        return RenderedRecord(event_type=event_type,
                              diagnostic=_diagnostic("未知 event_msg payload 类型", payload),
                              diagnostic_key=f"unknown_event_msg:{event_type}")

    if root_type != "response_item":
        return RenderedRecord(event_type=event_type,
                              diagnostic=_diagnostic("未知记录类型", record),
                              diagnostic_key=f"unknown_root:{root_type}")
    if event_type in _NOISE_RESPONSE_TYPES:
        return RenderedRecord(event_type=event_type)

    text, diagnostics, display_kind = None, [], None
    if event_type == "agent_message":
        # App 0.159.2 多代理内部通信（author/recipient 为 /root/... 代理路径，内容与
        # message 通道零重叠，2026-10-03 实证）：已知类型，不诊断不显示——与 user
        # message 同等待遇；event_type 照常流通，全保真用 --raw 或直读 jsonl。
        return RenderedRecord(event_type=event_type)
    if event_type == "message":
        role = payload.get("role")
        if role in ("user", "system", "developer", "tool"):
            return RenderedRecord(event_type=event_type)
        if role != "assistant":
            return RenderedRecord(event_type=event_type,
                                  diagnostic=_diagnostic("message.role 未知或无效", role))
        display_kind = "agentMessage"
        content = payload.get("content")
        if not isinstance(content, list):
            diagnostics.append(_diagnostic("assistant content 必须是 list", content))
        else:
            parts = []
            for item in content:
                if not isinstance(item, dict):
                    diagnostics.append(_diagnostic("assistant content 条目必须是 object", item))
                elif "text" in item:
                    if isinstance(item["text"], str):
                        parts.append(item["text"])
                    else:
                        diagnostics.append(_diagnostic("assistant text 必须是字符串", item["text"]))
                else:
                    diagnostics.append(_diagnostic("未知 assistant content 条目", item))
            message = "".join(parts).strip()
            if message:
                text = f"{stamp} [Codex] {_summary(message, 160)}"
    elif event_type in ("function_call", "custom_tool_call"):
        display_kind = "command"
        label, label_diagnostics = _tool_label(payload)
        diagnostics.extend(label_diagnostics)
        if event_type == "function_call":
            arguments, diagnostic = _arguments(payload.get("arguments", {}))
            if diagnostic:
                diagnostics.append(diagnostic)
            if arguments is not None:
                text, argument_diagnostics = _render_arguments(stamp, label, arguments)
                diagnostics.extend(argument_diagnostics)
        else:
            value = payload.get("input")
            arguments = None
            if isinstance(value, dict):
                arguments = value
            elif isinstance(value, str):
                try:
                    parsed = _decode(value)
                    if isinstance(parsed, dict):
                        arguments = parsed
                except (ValueError, TypeError, RecursionError):
                    pass  # custom_tool_call is allowed to contain arbitrary free text.
            else:
                diagnostics.append(_diagnostic("custom_tool_call input 必须是字符串或 object", value))
            if arguments is not None and ("cmd" in arguments or "command" in arguments):
                text, argument_diagnostics = _render_arguments(stamp, label, arguments)
                diagnostics.extend(argument_diagnostics)
            elif isinstance(value, str):
                text = f"{stamp} [工具] {label}(自由文本摘要: {_summary(value, 160)})"
            elif isinstance(value, dict):
                text = f"{stamp} [工具] {label}(JSON 摘要: {_summary(_serialized(value), 100)})"
    elif event_type in ("file_change", "local_file_change"):
        display_kind = "file"
        text = f"{stamp} [文件] {event_type}"
    else:
        return RenderedRecord(event_type=event_type,
                              diagnostic=_diagnostic("未知 response_item payload 类型", payload),
                              diagnostic_key=f"unknown_response_item:{event_type}")

    if only is not None and only != display_kind:
        text = None
    return RenderedRecord(text=text, event_type=event_type,
                          diagnostic="；".join(diagnostics) or None)
