"""Read-only JSONL watch with verified identity and complete-record checkpoints."""
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import time

from rollout_render import RenderedRecord, parse_rollout_line


class WatchError(RuntimeError):
    pass


_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_FILENAME = re.compile(r"rollout-.*-(" + _UUID + r")\.jsonl\Z")
_ANCHOR_SCHEMA = "codex-watch-anchor-v1"


def _prefix(value):
    value = value.lower()
    shape = "00000000-0000-0000-0000-000000000000"
    if not 8 <= len(value) <= len(shape) or any(
        (char != "-" if shape[index] == "-" else char not in "0123456789abcdef")
        for index, char in enumerate(value)
    ):
        raise WatchError("thread ID 必须是完整 UUID 或至少 8 位的规范 UUID 前缀，不能包含 glob")
    return value


def _safe_path(path, home):
    home = Path(home).resolve()
    roots = (home / "sessions", home / "archived_sessions")
    resolved = path.resolve()
    if path.is_symlink() or not any(resolved.is_relative_to(root) for root in roots):
        raise WatchError("rollout 路径必须留在指定 Codex 根目录的会话目录内，且不能是符号链接")
    return resolved


def _open(path, home):
    path = _safe_path(Path(path), home)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(path, flags)
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise WatchError("rollout 必须是普通文件")
    return os.fdopen(descriptor, "rb")


def _session_id(stream):
    stream.seek(0)
    line = stream.readline(1024 * 1024)
    if not line.endswith(b"\n"):
        return None  # A partial metadata line is not a complete identity record.
    try:
        item = json.loads(line)
    except (ValueError, UnicodeError) as error:
        raise WatchError("rollout 的完整 session_meta 记录无效") from error
    payload = item.get("payload") if isinstance(item, dict) else None
    tid = payload.get("id") if isinstance(payload, dict) else None
    if (not isinstance(item, dict) or item.get("type") != "session_meta"
            or not isinstance(tid, str) or not re.fullmatch(_UUID, tid)):
        raise WatchError("rollout 首条完整记录必须是含完整 UUID 的 session_meta")
    return tid


def find_rollout(home, tid):
    """Only accept unique, metadata-verified UUID matches in the configured root."""
    prefix = _prefix(tid)
    home = Path(home).resolve()
    # Enumerate again once if an entry moved between listing and opening.
    for attempt in range(2):
        paths = list((home / "sessions").glob(f"*/*/*/rollout-*{prefix}*.jsonl"))
        paths += list((home / "archived_sessions").rglob(f"rollout-*{prefix}*.jsonl"))
        hits, moved = {}, False
        for path in sorted(paths):
            match = _FILENAME.fullmatch(path.name)
            if not match or not match[1].startswith(prefix):
                continue
            try:
                with _open(path, home) as stream:
                    actual = _session_id(stream)
                    identity = os.fstat(stream.fileno())
            except FileNotFoundError:
                moved = True
                continue
            except OSError as error:
                raise WatchError(f"无法只读打开 rollout：{error}") from error
            if actual is None:
                continue
            if actual != match[1]:
                raise WatchError("rollout 文件名与 session_meta.id 不一致，拒绝绑定")
            hits[(identity.st_dev, identity.st_ino)] = (path, actual)
        if moved and attempt == 0:
            continue
        if len(hits) > 1:
            candidates = ", ".join(sorted(value[1] for value in hits.values()))
            raise WatchError(f"rollout 匹配有歧义，不能猜最新：{candidates}")
        return next(iter(hits.values()))[0] if hits else None
    return None


def _current_stat(home, tid, last_path):
    """Stat the watched source; full rediscovery only when the known path vanishes.

    Returns (stat, current_path). The fast path stats the known path directly:
    a replaced file there still yields its new inode for the caller's identity
    check, and a rename within the watched roots falls back to verified
    rediscovery and hands the new path back so later polls stay cheap.
    """
    if last_path is not None:
        try:
            return last_path.stat(), last_path
        except FileNotFoundError:
            pass  # Renamed or removed: fall through to verified rediscovery.
        except OSError:
            return None, last_path
    for attempt in range(2):
        path = find_rollout(home, tid)
        if path is None:
            return None, last_path
        try:
            return path.stat(), path
        except FileNotFoundError:
            if attempt:
                return None, last_path
    return None, last_path


def _hash_prefix(stream, offset):
    digest = hashlib.sha256()
    position = 0
    while position < offset:
        data = os.pread(stream.fileno(), min(65536, offset - position), position)
        if not data:
            raise WatchError("续播锚点超过文件长度")
        digest.update(data)
        position += len(data)
    return digest


def _source_changed(home, tid, path, stream, identity, digest, offset, pending):
    """Recheck every emitted checkpoint, including unknown and interrupted stops."""
    try:
        final_stat, _ = _current_stat(home, tid, path)
        return (final_stat is None or (final_stat.st_dev, final_stat.st_ino) !=
                (identity.st_dev, identity.st_ino)
                or _hash_prefix(stream, offset).digest() != digest.digest()
                or bool(pending and os.pread(stream.fileno(), len(pending), offset) != pending))
    except (WatchError, OSError):
        return True


def _load_anchor(value, stream, identity, tid, since):
    try:
        anchor = json.loads(value)
    except (ValueError, TypeError) as error:
        raise WatchError("ANCHOR 必须是上一轮输出的 JSON 对象") from error
    if not isinstance(anchor, dict) or anchor.get("schema") != _ANCHOR_SCHEMA:
        raise WatchError("无效的 ANCHOR schema")
    for key in ("device", "inode", "offset"):
        if type(anchor.get(key)) is not int or anchor[key] < 0:
            raise WatchError(f"无效的 ANCHOR {key}")
    if (anchor.get("thread_id") != tid or anchor["device"] != identity.st_dev
            or anchor["inode"] != identity.st_ino):
        raise WatchError("ANCHOR 与线程或文件身份不一致，拒绝套用旧 OFFSET")
    offset = anchor["offset"]
    if since and since != offset:
        raise WatchError("--since 与 ANCHOR 的 offset 不一致")
    if offset > identity.st_size or (offset and os.pread(stream.fileno(), 1, offset - 1) != b"\n"):
        raise WatchError("ANCHOR offset 必须位于现有完整 JSONL 的换行边界")
    checksum = anchor.get("sha256")
    if not isinstance(checksum, str) or not re.fullmatch(r"[0-9a-f]{64}", checksum):
        raise WatchError("无效的 ANCHOR sha256")
    digest = _hash_prefix(stream, offset)
    if digest.hexdigest() != checksum:
        raise WatchError("ANCHOR 前缀内容已改变，拒绝套用旧 OFFSET")
    turn = anchor.get("turn_id")
    if turn is not None and (not isinstance(turn, str) or not turn.strip()):
        raise WatchError("无效的 ANCHOR turn_id")
    return offset, digest, turn


def _parse_bytes(line, only=None):
    try:
        text = line.decode("utf-8")
    except UnicodeDecodeError:
        return RenderedRecord(diagnostic="完整 JSONL 记录含无效 UTF-8，未替换为伪文本")
    return parse_rollout_line(text, only)


def _last_started(stream, size):
    """Inspect the startup snapshot before choosing a default follow target."""
    stream.seek(0)
    target = None
    while stream.tell() < size:
        line = stream.readline(size - stream.tell())
        if not line.endswith(b"\n"):
            break
        record = _parse_bytes(line)
        if record.event_type == "task_started" and record.turn_id:
            target = record.turn_id
    return target


def watch_rollout(args, home):
    """Return a CLI exit code; never create an app-server or write a session."""
    start = time.monotonic()
    deadline = start + args.timeout
    file_deadline = min(deadline, start + args.wait_file)
    stream = identity = digest = None
    tid = target = None
    offset, pending, records, diagnostics = 0, b"", 0, 0
    seen_diagnostic_keys = set()  # 未知类型按 key 降采样：每类型只 WARN 一次，计数照累计
    status, reason, code = "unknown", "error", 1
    anchor_usable = True
    try:
        _prefix(args.thread_id)
        if args.since > 0 and not args.anchor:
            raise WatchError("--since > 0 必须同时提供上一轮 ANCHOR，或直接使用 --anchor")
        if args.turn_id is not None and not args.turn_id.strip():
            raise WatchError("--turn-id 不能为空")
        path = None
        while True:
            path = find_rollout(home, args.thread_id)
            if path is not None:
                break
            now = time.monotonic()
            if now >= file_deadline:
                reason = "timeout" if now >= deadline else "file_timeout"
                code = 3
                return code
            time.sleep(min(0.5, file_deadline - now))
        for attempt in range(2):
            try:
                stream = _open(path, home)
                break
            except FileNotFoundError as error:
                if attempt:
                    raise WatchError("初次打开 rollout 时来源持续移动，请重新核验") from error
                path = find_rollout(home, args.thread_id)
                if path is None:
                    raise WatchError("初次打开 rollout 时来源已不可核验") from error
        identity = os.fstat(stream.fileno())
        tid = _session_id(stream)
        if tid is None or not tid.startswith(_prefix(args.thread_id)):
            raise WatchError("rollout 身份仍未落盘或已改变")
        digest = hashlib.sha256()
        anchor_turn = None
        if args.anchor:
            offset, digest, anchor_turn = _load_anchor(
                args.anchor, stream, identity, tid, args.since)
        if args.turn_id and anchor_turn and args.turn_id != anchor_turn:
            raise WatchError("--turn-id 与 ANCHOR 的目标轮次不一致")
        target = args.turn_id or (anchor_turn if args.follow else None)
        if args.follow and target is None:
            target = _last_started(stream, identity.st_size)
        stream.seek(offset)
        print(f"WATCH={path}", flush=True)
        print(f"THREAD_ID={tid}", flush=True)
        if target:
            print(f"WATCH_TURN_ID={target}", flush=True)
        last_activity = time.monotonic()
        last_mtime = identity.st_mtime_ns
        while True:
            now = time.monotonic()
            if now >= deadline:
                reason, code = "timeout", 3
                break
            try:
                current_stat, path = _current_stat(home, tid, path)
                metadata = os.fstat(stream.fileno())
                changed = current_stat is None
                if current_stat is not None:
                    changed = (current_stat.st_dev, current_stat.st_ino) != (
                        identity.st_dev, identity.st_ino)
                changed = changed or metadata.st_size < stream.tell()
                if not changed and metadata.st_mtime_ns != last_mtime:
                    if pending:
                        # 半条缓存记录比对（O(pending)，便宜）：append 路径也必检——
                        # 被改写的旧字节若放行会拼接到新尾巴污染输出（2026-10-03 回归实测）
                        changed = os.pread(stream.fileno(), len(pending), offset) != pending
                    # 降频（2026-10-03）：全量前缀重 hash（O(offset)，昂贵）只在 mtime 变化
                    # 且读位置已到 EOF（无新数据可读）时执行——append 正常路径跳过，长任务
                    # 总开销由 O(n²) 降为 O(n)；同长覆写/原地改写仍必被检出。终态/回放
                    # 每种停止路径在输出锚点前重算兜底（含 unknown / SIGINT）。
                    if not changed and metadata.st_size <= stream.tell():
                        changed = _hash_prefix(stream, offset).digest() != digest.digest()
                last_mtime = metadata.st_mtime_ns
            except (WatchError, OSError) as error:
                changed = True
                print(f"WARN: rollout 来源核验失败：{error}", file=sys.stderr)
            if changed:
                reason, code, anchor_usable = "source_changed", 3, False
                break
            chunk = stream.read(65536)
            if chunk:
                last_activity = time.monotonic()
                pending += chunk
                boundary = pending.rfind(b"\n")
                if boundary >= 0:
                    complete, pending = pending[:boundary + 1], pending[boundary + 1:]
                    raw_lines = complete.split(b"\n")[:-1]
                    for index, raw in enumerate(raw_lines):
                        line = raw + b"\n"
                        record = _parse_bytes(line, args.filter)
                        digest.update(line)
                        offset += len(line)
                        records += 1
                        if record.diagnostic:
                            diagnostics += 1
                            key = record.diagnostic_key
                            if key is None or key not in seen_diagnostic_keys:
                                if key is not None:
                                    seen_diagnostic_keys.add(key)
                                print(f"WARN: record@{offset}: {record.diagnostic}",
                                      file=sys.stderr, flush=True)
                        if record.text:
                            print(record.text, flush=True)
                        if args.follow and target is None and (
                                record.event_type == "task_started" and record.turn_id):
                            target = record.turn_id
                            print(f"WATCH_TURN_ID={target}", flush=True)
                        if args.follow and target and record.turn_id == target and record.terminal_status:
                            status = record.terminal_status
                            reason = {"completed": "turn_completed", "aborted": "turn_aborted",
                                      "failed": "turn_failed"}[status]
                            code = 0 if status == "completed" else 2
                            # Keep all read-ahead bytes rooted at the committed offset.
                            pending = b"".join(item + b"\n" for item in raw_lines[index + 1:]) + pending
                            break
                    if status in ("completed", "aborted", "failed"):
                        break
                continue
            if not args.follow:
                if pending:
                    reason, code = "partial_record", 3
                else:
                    status, reason, code = "replay", "eof", 0
                break
            now = time.monotonic()
            if now - last_activity >= args.idle_timeout:
                reason, code = "idle_timeout", 3
                break
            time.sleep(min(args.interval, max(0, deadline - now),
                           max(0, args.idle_timeout - (now - last_activity))))
        # Unknown stops also expose a resumable checkpoint; certify it before output.
        if stream is not None and anchor_usable and _source_changed(
                home, tid, path, stream, identity, digest, offset, pending):
            status, reason, code, anchor_usable = "unknown", "source_changed", 3, False
        return code
    except KeyboardInterrupt:
        status, reason, code = "unknown", "user_interrupt", 130
        if (stream is not None and identity is not None and digest is not None
                and anchor_usable and _source_changed(
                    home, tid, path, stream, identity, digest, offset, pending)):
            # Keep the user-interrupt exit code; an invalid source still has no anchor.
            reason, anchor_usable = "source_changed", False
        return code
    except OSError as error:
        raise WatchError(f"rollout 读取失败：{error}") from error
    finally:
        anchor = None
        if stream is not None and identity is not None and digest is not None and anchor_usable:
            anchor = {"schema": _ANCHOR_SCHEMA, "thread_id": tid,
                      "device": identity.st_dev, "inode": identity.st_ino,
                      "offset": offset, "sha256": digest.hexdigest(), "turn_id": target}
        if stream is not None:
            stream.close()
        print(f"WATCH_STATUS={status}", flush=True)
        print(f"STOP_REASON={reason}", flush=True)
        print(f"OFFSET={offset}", flush=True)
        print("ANCHOR=" + json.dumps(anchor, ensure_ascii=False, separators=(",", ":")), flush=True)
        print(f"RECORDS={records} DIAGNOSTICS={diagnostics} PENDING_BYTES={len(pending)}", flush=True)
