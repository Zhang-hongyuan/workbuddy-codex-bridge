"""Prepare native desktop drafts and reconcile their receipt with read-only metadata.

The deep link only prefills a draft. Neither an accepted open request nor a
matching SQLite row proves that the GUI is visible or its stream is updating.
Model settings must be chosen in the desktop UI and are checked after submit.
"""

import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess
import tempfile
import time
from urllib.parse import quote, urlencode
import uuid


class DesktopDispatchError(RuntimeError):
    """A desktop request cannot be safely prepared or reconciled."""


_RECEIPT_NAME = re.compile(r"dispatch-[0-9a-f]{32}\.json\Z")
_SCHEMA = "codex-bridge-desktop-dispatch-v1"


def _state_dir(create=False):
    configured = os.environ.get("CODEX_BRIDGE_STATE_DIR")
    if configured is not None and not configured.strip():
        raise DesktopDispatchError("CODEX_BRIDGE_STATE_DIR must not be empty")
    directory = Path(configured).expanduser() if configured else Path.home() / ".workbuddy/codex-bridge/dispatch"
    if create:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.is_symlink():
        raise DesktopDispatchError("Receipt directory must not be a symlink")
    return directory.resolve()


def _receipt_path(value):
    path = Path(value).expanduser().absolute()
    if not _RECEIPT_NAME.fullmatch(path.name) or path.parent.resolve() != _state_dir():
        raise DesktopDispatchError("Receipt must be a dispatch JSON file inside CODEX_BRIDGE_STATE_DIR")
    if path.is_symlink():
        raise DesktopDispatchError("Receipt must not be a symlink")
    return path


def _read_receipt(path):
    path = _receipt_path(path)
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise DesktopDispatchError("Receipt must be a regular file")
        receipt = json.load(stream)
    if not isinstance(receipt, dict) or receipt.get("schema") != _SCHEMA:
        raise DesktopDispatchError("Invalid desktop dispatch receipt schema")
    required = ("request_id", "created_at_ms", "cwd", "prompt", "requested_model", "existing_thread_ids")
    if any(key not in receipt for key in required):
        raise DesktopDispatchError("Incomplete desktop dispatch receipt")
    if path.name != "dispatch-" + receipt["request_id"] + ".json":
        raise DesktopDispatchError("Receipt request ID does not match its filename")
    if (not isinstance(receipt["created_at_ms"], int)
            or not isinstance(receipt["existing_thread_ids"], list)
            or not all(isinstance(item, str) for item in receipt["existing_thread_ids"])
            or any(not isinstance(receipt[key], str) for key in ("cwd", "prompt", "requested_model"))):
        raise DesktopDispatchError("Invalid desktop dispatch receipt fields")
    for key in ("confirmed_thread_id", "observed_thread_id", "requested_effort"):
        if receipt.get(key) is not None and not isinstance(receipt[key], str):
            raise DesktopDispatchError("Invalid desktop dispatch receipt field: " + key)
    observed = receipt.get("observed_thread_ids", [])
    if not isinstance(observed, list) or not all(isinstance(value, str) for value in observed):
        raise DesktopDispatchError("Invalid observed thread IDs in receipt")
    if not Path(receipt["cwd"]).is_absolute():
        raise DesktopDispatchError("Receipt cwd must be absolute")
    return receipt


def _write_receipt(path, receipt, new=False):
    path = _receipt_path(path)
    payload = json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if new:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(payload)
        return
    # Replace only this validated receipt; temporary files stay in the same directory.
    descriptor, temporary = tempfile.mkstemp(prefix=".dispatch-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(payload)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _canonical_cwd(value):
    return str(Path(value).expanduser().resolve())


def _codex_dir():
    configured = os.environ.get("CODEX_BRIDGE_CODEX_HOME")
    if configured is not None and not configured.strip():
        raise DesktopDispatchError("CODEX_BRIDGE_CODEX_HOME must not be empty")
    return (Path(configured).expanduser() if configured else Path.home() / ".codex").resolve()


def _read_threads(prompt=None, thread_id=None):
    database = _codex_dir() / "state_5.sqlite"
    connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=5)
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.row_factory = sqlite3.Row
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(threads)")}
        required = {"id", "cwd", "first_user_message", "created_at", "archived", "rollout_path"}
        if not required <= columns:
            raise DesktopDispatchError("Codex threads metadata schema is missing: " + ", ".join(sorted(required - columns)))
        selected = sorted(required | ({"created_at_ms", "reasoning_effort", "project_id", "source", "thread_source", "originator"} & columns))
        predicate, parameter = ("id = ?", thread_id) if thread_id is not None else ("first_user_message = ?", prompt)
        rows = connection.execute("SELECT " + ", ".join(selected) + " FROM threads WHERE " + predicate, (parameter,))
        return [dict(row) for row in rows]
    finally:
        connection.close()


def _read_first_turn(row, prompt):
    """Read only the first turn; thread-level model metadata can change later."""
    path = Path(row["rollout_path"]).expanduser().resolve()
    roots = [_codex_dir() / "sessions", _codex_dir() / "archived_sessions"]
    if path.suffix != ".jsonl" or not any(path.is_relative_to(root) for root in roots):
        raise DesktopDispatchError("Rollout path must remain inside Codex session directories")
    evidence = {"first_turn_id": None, "first_turn_status": "not_observed", "context": None,
                "prompt_matched": False, "session_id": None}
    if not path.exists():
        return evidence
    if not path.is_file():
        raise DesktopDispatchError("Rollout must be a regular file")
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            try:
                item = json.loads(line)
            except json.JSONDecodeError as error:
                if not line.endswith("\n"):
                    break  # The active writer may not have flushed its final line.
                raise DesktopDispatchError("Invalid complete JSONL record in rollout") from error
            payload = item.get("payload", {})
            kind = item.get("type")
            if kind == "session_meta":
                evidence["session_id"] = payload.get("id")
                if payload.get("id") != row["id"]:
                    raise DesktopDispatchError("Rollout session ID does not match candidate thread")
            if kind == "event_msg" and payload.get("type") == "task_started":
                if evidence["first_turn_id"] is not None:
                    break
                evidence["first_turn_id"] = payload.get("turn_id")
                evidence["first_turn_status"] = "started"
                continue
            if evidence["first_turn_id"] is None:
                continue
            if kind == "turn_context" and payload.get("turn_id") == evidence["first_turn_id"] and evidence["context"] is None:
                evidence["context"] = payload
            if kind == "response_item" and payload.get("type") == "message" and payload.get("role") == "user" and evidence["prompt_matched"] is False:
                # 2026-10-03 战场实证修复：真实 rollout 中首轮 user message 可能先于 turn_context 落盘，
                # 原逻辑要求 context 先到导致 prompt_matched 永假（completed 后误报 first_turn_prompt_mismatch）。
                # 首轮边界已由 task_started（重复即 break）保证，context 仅服务模型/cwd 核验，无需前置。
                content = payload.get("content", [])
                texts = [part.get("text", "") for part in content if part.get("type") == "input_text"]
                if "".join(texts).rstrip("\n") == prompt.rstrip("\n"):
                    # 尾部换行归一：App 提交时会在 prompt 末尾追加 \n（2026-10-03 战场实证，146 vs 145 字节）
                    evidence["prompt_matched"] = True
            if kind == "event_msg" and payload.get("turn_id") == evidence["first_turn_id"]:
                terminal = {"task_complete": "completed", "turn_aborted": "aborted",
                            "task_failed": "failed"}.get(payload.get("type"))
                if terminal:
                    evidence["first_turn_status"] = terminal
    return evidence


def _is_new(receipt, row):
    if row["id"] in receipt["existing_thread_ids"]:
        return False
    if row.get("created_at_ms") is not None:
        return row["created_at_ms"] >= receipt["created_at_ms"]
    # Older databases have second resolution; the baseline IDs disambiguate old rows.
    return row["created_at"] >= receipt["created_at_ms"] // 1000


def _result(path, receipt, status, **extra):
    return {"status": status, "request_id": receipt["request_id"], "receipt_path": str(path),
            "cwd": receipt["cwd"], "requested_model": receipt["requested_model"],
            "requested_effort": receipt.get("requested_effort"), **extra}


def prepare_dispatch(cwd, task_text, model, effort=None, prepare_only=False):
    """Save a receipt, then request a desktop draft (never starts an app-server)."""
    try:
        if not isinstance(task_text, str) or not task_text.strip():
            raise DesktopDispatchError("Desktop dispatch requires a nonempty task prompt")
        if not isinstance(model, str) or not model or model.strip() != model:
            raise DesktopDispatchError("Desktop dispatch requires an exact model name")
        if effort is not None and (not isinstance(effort, str) or not effort or effort.strip() != effort):
            raise DesktopDispatchError("Desktop dispatch requires an exact reasoning effort")
        canonical = _canonical_cwd(cwd)
        if not Path(canonical).is_dir():
            raise DesktopDispatchError("Desktop dispatch cwd must be an existing directory")
        request_id = uuid.uuid4().hex
        full_prompt = task_text + "\n\n[WB_DISPATCH_ID=" + request_id + "]"
        existing = [row["id"] for row in _read_threads(prompt=full_prompt) if _canonical_cwd(row["cwd"]) == canonical]
        created_at_ms = time.time_ns() // 1_000_000
        url = "codex://new?" + urlencode({"mode": "codex", "path": canonical, "prompt": full_prompt}, quote_via=quote, safe="")
        receipt = {"schema": _SCHEMA, "request_id": request_id, "created_at_ms": created_at_ms,
                   "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(created_at_ms / 1000)),
                   "cwd": canonical, "original_task": task_text, "prompt": full_prompt, "requested_model": model,
                   "requested_effort": effort, "existing_thread_ids": sorted(existing),
                   "status": "prepared", "url": url}
        path = _state_dir(create=True) / ("dispatch-" + request_id + ".json")
        _write_receipt(path, receipt, new=True)
        if not prepare_only:
            try:
                opened = subprocess.run(["open", url], capture_output=True, text=True, timeout=15, check=False)
            except (OSError, subprocess.SubprocessError) as error:
                receipt["status"] = "open_request_failed"
                _write_receipt(path, receipt)
                raise DesktopDispatchError("Desktop open request failed; receipt: " + str(path)) from error
            if opened.returncode != 0:
                receipt["status"] = "open_request_failed"
                receipt["open_returncode"] = opened.returncode
                _write_receipt(path, receipt)
                raise DesktopDispatchError("Desktop open request returned " + str(opened.returncode) + "; receipt: " + str(path))
            receipt["status"] = "open_request_sent"
            _write_receipt(path, receipt)
        return _result(path, receipt, receipt["status"], url=url, needs_gui_submit=True)
    except DesktopDispatchError:
        raise
    except Exception as error:
        raise DesktopDispatchError("Could not prepare desktop dispatch: " + str(error)) from error


def confirm_dispatch(receipt_path, thread_id=None):
    """Reconcile metadata without sending, resuming, steering, or interrupting a turn."""
    try:
        if thread_id is not None and (not isinstance(thread_id, str) or not thread_id.strip()):
            raise DesktopDispatchError("thread_id must be a nonempty string")
        path = _receipt_path(receipt_path)
        receipt = _read_receipt(path)
        confirmed = receipt.get("confirmed_thread_id")
        if confirmed and thread_id is not None and thread_id != confirmed:
            raise DesktopDispatchError("Receipt is already confirmed for a different thread")
        requested = confirmed or thread_id
        rows = _read_threads(thread_id=requested) if requested else _read_threads(prompt=receipt["prompt"])
        if not requested:
            # The unique full prompt identifies this dispatch even in the wrong
            # project. Keep such candidates so they report mismatch, not unsubmitted.
            rows = [row for row in rows if _is_new(receipt, row)]
        if not rows:
            return _result(path, receipt, "awaiting_desktop_submit", thread_id=None, actual_model=None, project_id=None, needs_gui_submit=True)
        if len(rows) > 1:
            receipt["observed_thread_ids"] = sorted(set(receipt.get("observed_thread_ids", [])) | {row["id"] for row in rows})
            receipt["status"] = "ambiguous"
            _write_receipt(path, receipt)
            return _result(path, receipt, "ambiguous", thread_id=None,
                           candidate_thread_ids=sorted(row["id"] for row in rows),
                           gui_verified=False, needs_gui_submit=False)
        row = rows[0]
        reasons = []
        if _canonical_cwd(row["cwd"]) != receipt["cwd"]:
            reasons.append("cwd_mismatch")
        if row["first_user_message"] != receipt["prompt"]:
            reasons.append("prompt_mismatch")
        if not _is_new(receipt, row):
            reasons.append("thread_predates_dispatch")
        if row["first_user_message"] == receipt["prompt"] and _is_new(receipt, row):
            receipt["observed_thread_id"] = row["id"]
            receipt["observed_thread_ids"] = sorted(set(receipt.get("observed_thread_ids", [])) | {row["id"]})
            _write_receipt(path, receipt)
        if row["archived"]:
            reasons.append("thread_archived")
        evidence = _read_first_turn(row, receipt["prompt"])
        context = evidence["context"] or {}
        actual_model = context.get("model")
        actual_effort = context.get("effort")
        if context.get("cwd") is not None and _canonical_cwd(context["cwd"]) != receipt["cwd"]:
            reasons.append("first_turn_cwd_mismatch")
        if actual_model is not None and actual_model != receipt["requested_model"]:
            reasons.append("model_mismatch")
        if receipt.get("requested_effort") is not None and actual_effort is not None and actual_effort != receipt["requested_effort"]:
            reasons.append("reasoning_effort_mismatch")
        pending = []
        if evidence["session_id"] is None or evidence["first_turn_id"] is None:
            pending.append("waiting_for_first_turn")
        if actual_model is None or context.get("cwd") is None:
            pending.append("waiting_for_first_turn_context")
        if receipt.get("requested_effort") is not None and actual_effort is None:
            pending.append("waiting_for_first_turn_effort")
        if not evidence["prompt_matched"]:
            if evidence["first_turn_status"] in ("completed", "aborted", "failed"):
                reasons.append("first_turn_prompt_mismatch")
            else:
                pending.append("waiting_for_first_turn_prompt")
        status = "mismatch" if reasons else "pending" if pending else "created"
        receipt["status"] = status
        if status == "created":
            receipt["confirmed_thread_id"] = row["id"]
        receipt["last_confirmation"] = {"status": status, "thread_id": row["id"], "reasons": reasons + pending}
        _write_receipt(path, receipt)
        return _result(path, receipt, status, thread_id=row["id"], actual_model=actual_model,
                       actual_effort=actual_effort, project_id=row.get("project_id"),
                       first_turn_id=evidence["first_turn_id"], first_turn_status=evidence["first_turn_status"],
                       reasons=reasons + pending, gui_verified=False, needs_gui_submit=False)
    except DesktopDispatchError:
        raise
    except Exception as error:
        raise DesktopDispatchError("Could not confirm desktop dispatch: " + str(error)) from error


def is_desktop_thread(thread_id):
    """Protect observed as well as confirmed native threads from external writers.

    Invalid receipts or metadata read failures raise rather than returning False.
    Callers must stop external writer operations on error. Unconfirmed receipts
    also guard newly submitted matching threads before desktop-confirm is run.
    """
    try:
        directory = _state_dir()
        if not directory.exists():
            return False
        for path in directory.glob("dispatch-*.json"):
            receipt = _read_receipt(path)
            if thread_id in {receipt.get("confirmed_thread_id"), receipt.get("observed_thread_id"), *receipt.get("observed_thread_ids", [])}:
                return True
            # Also cover GUI submission before the caller has run desktop-confirm.
            if not receipt.get("confirmed_thread_id"):
                for row in _read_threads(thread_id=thread_id):
                    if row["first_user_message"] == receipt["prompt"] and _is_new(receipt, row):
                        return True
        return False
    except DesktopDispatchError:
        raise
    except Exception as error:
        raise DesktopDispatchError("Cannot safely check desktop thread ownership: " + str(error)) from error
