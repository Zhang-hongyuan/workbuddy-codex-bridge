"""Black-box watch regressions; all rollout input lives in disposable fixtures.

Run from the candidate worktree with:
    PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -p test_watch.py -v
"""
import hashlib
import importlib
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "codex_bridge.py"
THREAD = "11111111-1111-4111-8111-111111111111"
OTHER_THREAD = "11111111-2222-4222-8222-222222222222"
TURN_1 = "turn-one"
TURN_2 = "turn-two"


def record(kind, payload):
    return (json.dumps({"timestamp": "2026-10-03T00:00:00Z", "type": kind,
                        "payload": payload}, ensure_ascii=False) + "\n").encode("utf-8")


def meta(thread=THREAD):
    return record("session_meta", {"id": thread})


def event(kind, turn=TURN_1):
    return record("event_msg", {"type": kind, "turn_id": turn})


def message(text):
    return record("response_item", {"type": "message", "role": "assistant",
                                    "content": [{"type": "output_text", "text": text}]})


class LiveWatch:
    """Read both pipes concurrently and synchronize writers with visible output."""

    def __init__(self, argv, *, env, cwd):
        self.proc = subprocess.Popen(argv, cwd=cwd, env=env, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True, encoding="utf-8")
        self.stdout = []
        self.stderr = []
        self.condition = threading.Condition()
        self.readers = []
        for pipe, target in ((self.proc.stdout, self.stdout),
                             (self.proc.stderr, self.stderr)):
            def reader(pipe=pipe, target=target):
                for line in pipe:
                    with self.condition:
                        target.append(line)
                        self.condition.notify_all()
            worker = threading.Thread(target=reader, daemon=True)
            worker.start()
            self.readers.append(worker)

    def wait_for(self, marker, timeout=3):
        end = time.monotonic() + timeout
        with self.condition:
            while marker not in "".join(self.stdout):
                if self.proc.poll() is not None:
                    break
                remaining = end - time.monotonic()
                if remaining <= 0:
                    break
                self.condition.wait(min(remaining, 0.05))
        output = "".join(self.stdout)
        if marker not in output:
            raise AssertionError(f"watch never printed {marker!r}; rc={self.proc.poll()} "
                                 f"stdout={output!r} stderr={''.join(self.stderr)!r}")

    def finish(self, timeout=3):
        code = self.proc.wait(timeout=timeout)
        for worker in self.readers:
            worker.join(timeout=1)
        return subprocess.CompletedProcess(self.proc.args, code,
                                           "".join(self.stdout), "".join(self.stderr))

    def close(self):
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait(timeout=3)
        for worker in self.readers:
            worker.join(timeout=1)
        self.proc.stdout.close()
        self.proc.stderr.close()


class WatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="codex-watch-test-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.env = dict(os.environ, CODEX_BRIDGE_CODEX_HOME=str(self.home),
                        PYTHONDONTWRITEBYTECODE="1")

    def rollout(self, data, thread=THREAD, *, archived=False):
        folder = self.home / ("archived_sessions" if archived else "sessions/2026/10/03")
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"rollout-2026-10-03T00-00-00-{thread}.jsonl"
        path.write_bytes(data)
        return path

    def argv(self, *args, thread=THREAD):
        return [sys.executable, "-u", str(SCRIPT), "watch", thread,
                "--timeout", "2", "--idle-timeout", "1", "--interval", "0.01",
                "--wait-file", "0.2", *map(str, args)]

    def run_watch(self, *args, thread=THREAD):
        return subprocess.run(self.argv(*args, thread=thread), env=self.env,
                              cwd=SCRIPT.parents[1], capture_output=True,
                              text=True, encoding="utf-8", timeout=4)

    def start_watch(self, *args, thread=THREAD):
        live = LiveWatch(self.argv(*args, thread=thread), env=self.env, cwd=SCRIPT.parents[1])
        self.addCleanup(live.close)
        return live

    def assert_result(self, result, code, status=None, reason=None):
        self.assertEqual(result.returncode, code, (result.stdout, result.stderr))
        if status:
            self.assertIn(f"WATCH_STATUS={status}", result.stdout)
        if reason:
            self.assertIn(f"STOP_REASON={reason}", result.stdout)
        self.assertNotIn("Traceback", result.stderr)

    def anchor(self, result):
        lines = result.stdout.splitlines()
        anchor_line = next(line for line in reversed(lines) if line.startswith("ANCHOR="))
        offset_line = next(line for line in reversed(lines) if line.startswith("OFFSET="))
        return int(offset_line.removeprefix("OFFSET=")), json.loads(anchor_line.removeprefix("ANCHOR="))

    def append(self, path, data):
        with path.open("ab") as stream:
            stream.write(data)
            stream.flush()

    def test_replay_reads_all_history_after_old_completion(self):
        data = (meta() + event("task_started") + message("past-one")
                + event("task_complete") + event("task_started", TURN_2)
                + message("present-two"))
        path = self.rollout(data)
        result = self.run_watch()
        self.assert_result(result, 0, "replay", "eof")
        self.assertIn("past-one", result.stdout)
        self.assertIn("present-two", result.stdout)
        self.assertEqual(self.anchor(result)[0], len(data))
        self.assertEqual(path.read_bytes(), data, "watch must remain read-only")

    def test_follow_latest_turn_ignores_late_old_completion(self):
        path = self.rollout(meta() + event("task_started") + event("task_complete")
                            + event("task_started", TURN_2) + message("new-turn-running"))
        live = self.start_watch("--follow")
        live.wait_for("new-turn-running")
        self.append(path, event("task_complete", TURN_1) + message("after-old-completion"))
        live.wait_for("after-old-completion")
        self.assertIsNone(live.proc.poll(), "old turn must not stop the active turn")
        self.append(path, event("task_complete", TURN_2))
        self.assert_result(live.finish(), 0, "completed", "turn_completed")

    def test_explicit_turn_binding_and_filter_do_not_change_terminal_result(self):
        self.rollout(meta() + event("task_started") + message("hidden-message")
                     + event("task_started", TURN_2) + event("task_complete", TURN_2)
                     + event("task_complete", TURN_1))
        result = self.run_watch("--follow", "--turn-id", TURN_1, "--filter", "command")
        self.assert_result(result, 0, "completed", "turn_completed")
        self.assertNotIn("hidden-message", result.stdout)

    def test_completion_checkpoint_stops_before_read_ahead_records_and_partial_tail(self):
        prefix = (meta() + event("task_started", TURN_1) + message("bound-first-turn")
                  + event("task_complete", TURN_1))
        tail = (event("task_started", TURN_2) + message("later-turn-must-not-render")
                + message("unread-partial-tail")[:-17])
        data = prefix + tail
        self.assertLess(len(data), 65536, "fixture must fit in the single initial read")
        path = self.rollout(data)
        result = self.run_watch("--follow", "--turn-id", TURN_1)
        self.assert_result(result, 0, "completed", "turn_completed")
        self.assertIn("bound-first-turn", result.stdout)
        self.assertNotIn("later-turn-must-not-render", result.stdout)
        self.assertNotIn("unread-partial-tail", result.stdout)
        offset, anchor = self.anchor(result)
        self.assertEqual(offset, len(prefix))
        self.assertEqual(anchor["offset"], len(prefix))
        self.assertEqual(anchor["sha256"], hashlib.sha256(prefix).hexdigest())
        self.assertEqual(anchor["turn_id"], TURN_1)
        self.assertIn(f"PENDING_BYTES={len(tail)}", result.stdout)
        self.assertEqual(path.read_bytes(), data)

    def test_aborted_and_failed_are_distinct_unsuccessful_terminal_states(self):
        for terminal, status, reason in (("turn_aborted", "aborted", "turn_aborted"),
                                         ("task_failed", "failed", "turn_failed")):
            with self.subTest(terminal=terminal):
                self.rollout(meta() + event("task_started") + event(terminal))
                self.assert_result(self.run_watch("--follow"), 2, status, reason)

    def test_unbound_terminal_event_does_not_claim_completion(self):
        self.rollout(meta() + record("event_msg", {"type": "task_complete"}))
        result = self.run_watch("--follow", "--idle-timeout", "0.08")
        self.assert_result(result, 3, "unknown", "idle_timeout")

    def test_invalid_utf8_complete_record_cannot_trigger_terminal_event(self):
        bad = (b'{"type":"event_msg","payload":{"type":"task_complete",'
               b'"turn_id":"turn-one","extra":"\xff"}}\n')
        self.rollout(meta() + event("task_started") + bad
                     + message("after-bad-utf8") + event("task_complete"))
        result = self.run_watch("--follow")
        self.assert_result(result, 0, "completed", "turn_completed")
        self.assertIn("after-bad-utf8", result.stdout)
        self.assertIn("WARN:", result.stderr)

    def test_partial_json_and_utf8_are_reassembled_once(self):
        for utf8_split in (False, True):
            with self.subTest(utf8_split=utf8_split):
                line = message("unique-中文-assembled")
                cut = line.index("中".encode("utf-8")) + (1 if utf8_split else -3)
                path = self.rollout(meta() + event("task_started") + line[:cut])
                live = self.start_watch("--follow")
                # The initial read includes the partial line before rendering this event.
                live.wait_for("task_started：")
                self.assertNotIn("unique-", "".join(live.stdout))
                self.append(path, line[cut:] + event("task_complete"))
                result = live.finish()
                self.assert_result(result, 0, "completed", "turn_completed")
                self.assertEqual(result.stdout.count("unique-中文-assembled"), 1)
                self.assertEqual(self.anchor(result)[0], path.stat().st_size)

    def test_cached_partial_record_rewrite_cannot_join_old_bytes_to_new_tail(self):
        line = message("OLD_PARTIAL-following-tail")
        cut = line.index(b"OLD_PARTIAL") + len(b"OLD_PARTIAL")
        complete = meta() + event("task_started")
        data = complete + line[:cut]
        path = self.rollout(data)
        live = self.start_watch("--follow")
        # The initial read contains the partial bytes before this event is rendered.
        live.wait_for("task_started：")
        before = path.stat()
        with path.open("r+b") as stream:
            stream.seek(data.index(b"OLD_PARTIAL"))
            stream.write(b"NEW_PARTIAL")
            stream.flush()
        self.append(path, line[cut:] + event("task_complete"))
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000))
        self.assertEqual(before.st_ino, path.stat().st_ino)
        self.assertNotEqual(before.st_mtime_ns, path.stat().st_mtime_ns)
        result = live.finish()
        self.assert_result(result, 3, "unknown", "source_changed")
        self.assertNotIn("OLD_PARTIAL", result.stdout)
        self.assertNotIn("NEW_PARTIAL", result.stdout)
        offset, anchor = self.anchor(result)
        self.assertEqual(offset, len(complete))
        self.assertIsNone(anchor)

    def test_incomplete_final_line_is_not_consumed_as_replay_offset(self):
        complete = meta() + event("task_started") + message("fully-consumed")
        self.rollout(complete + message("still-partial")[:-2])
        result = self.run_watch()
        self.assert_result(result, 3, "unknown", "partial_record")
        self.assertEqual(self.anchor(result)[0], len(complete))
        self.assertNotIn("still-partial", result.stdout)

    def test_id_prefix_collision_is_rejected(self):
        self.rollout(meta())
        self.rollout(meta(OTHER_THREAD), OTHER_THREAD)
        self.assert_result(self.run_watch(thread="11111111"), 1)

    def test_same_thread_in_two_distinct_files_is_rejected(self):
        self.rollout(meta() + message("active-copy"))
        self.rollout(meta() + message("archive-copy"), archived=True)
        result = self.run_watch()
        self.assert_result(result, 1)
        self.assertNotIn("active-copy", result.stdout)
        self.assertNotIn("archive-copy", result.stdout)

    def test_unambiguous_prefix_resolves_metadata_verified_thread(self):
        self.rollout(meta() + message("prefix-proof"))
        result = self.run_watch(thread="11111111-1111")
        self.assert_result(result, 0, "replay", "eof")
        self.assertIn("prefix-proof", result.stdout)

    def test_glob_and_invalid_ids_are_rejected(self):
        self.rollout(meta() + message("must-not-broadcast"))
        for value in ("*", "?", "[1]", "../sessions", "not-a-uuid", ""):
            with self.subTest(value=value):
                result = self.run_watch(thread=value)
                self.assert_result(result, 1)
                self.assertNotIn("must-not-broadcast", result.stdout)

    def test_filename_identity_must_match_session_header(self):
        self.rollout(meta(OTHER_THREAD) + message("forged-header"))
        result = self.run_watch()
        self.assert_result(result, 1)
        self.assertNotIn("forged-header", result.stdout)

    def test_missing_or_noncanonical_header_id_is_rejected(self):
        for data in (message("no-header"), meta("not-a-uuid")):
            with self.subTest(data=data):
                self.rollout(data)
                self.assert_result(self.run_watch(), 1)

    def test_archived_sessions_are_discovered(self):
        self.rollout(meta() + message("archived-proof"), archived=True)
        result = self.run_watch()
        self.assert_result(result, 0, "replay", "eof")
        self.assertIn("archived-proof", result.stdout)

    def test_archive_rename_same_inode_continues(self):
        path = self.rollout(meta() + event("task_started") + message("before-archive"))
        live = self.start_watch("--follow")
        live.wait_for("before-archive")
        archived = self.home / "archived_sessions" / path.name
        archived.parent.mkdir(parents=True)
        path.rename(archived)
        self.append(archived, message("after-archive") + event("task_complete"))
        result = live.finish()
        self.assert_result(result, 0, "completed", "turn_completed")
        self.assertIn("after-archive", result.stdout)

    def test_discovery_retries_archive_move_between_enumeration_and_open(self):
        path = self.rollout(meta())
        archived = self.home / "archived_sessions" / path.name
        archived.parent.mkdir(parents=True)
        before = path.stat()
        with mock.patch.object(sys, "path", [str(SCRIPT.parent), *sys.path]), \
                mock.patch.object(sys, "dont_write_bytecode", True):
            module = importlib.import_module("rollout_watch")
        original_open = module._open
        moved = False

        def move_before_first_open(candidate, home):
            nonlocal moved
            if not moved:
                self.assertEqual(Path(candidate).resolve(), path.resolve())
                path.rename(archived)
                moved = True
            return original_open(candidate, home)

        with mock.patch.object(module, "_open", side_effect=move_before_first_open) as opened:
            found = module.find_rollout(self.home, THREAD)
        self.assertEqual(found, archived.resolve())
        self.assertEqual(opened.call_count, 2, "discovery must perform one finite retry")
        after = found.stat()
        self.assertEqual((before.st_dev, before.st_ino), (after.st_dev, after.st_ino))

    def test_watch_retries_archive_move_after_discovery_before_initial_open(self):
        path = self.rollout(meta() + event("task_started") + message("reopened-archive")
                            + event("task_complete"))
        archived = self.home / "archived_sessions" / path.name
        archived.parent.mkdir(parents=True)
        before = path.stat()
        with mock.patch.object(sys, "path", [str(SCRIPT.parent), *sys.path]), \
                mock.patch.object(sys, "dont_write_bytecode", True):
            module = importlib.import_module("rollout_watch")
        original_find, original_open = module.find_rollout, module._open
        found_paths = []
        discovery_finished, moved = False, False

        def observe_discovery(home, tid):
            nonlocal discovery_finished
            found = original_find(home, tid)
            found_paths.append(found)
            discovery_finished = True
            return found

        def move_before_watch_open(candidate, home):
            nonlocal moved
            if discovery_finished and not moved:
                self.assertEqual(Path(candidate).resolve(), path.resolve())
                path.rename(archived)
                moved = True
            return original_open(candidate, home)

        args = SimpleNamespace(thread_id=THREAD, timeout=2, wait_file=0.2,
                               since=0, anchor=None, turn_id=TURN_1, follow=True,
                               filter=None, idle_timeout=1, interval=0.01)
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(module, "find_rollout", side_effect=observe_discovery), \
                mock.patch.object(module, "_open", side_effect=move_before_watch_open), \
                redirect_stdout(stdout), redirect_stderr(stderr):
            code = module.watch_rollout(args, self.home)
        result = subprocess.CompletedProcess(["watch_rollout"], code,
                                             stdout.getvalue(), stderr.getvalue())
        self.assert_result(result, 0, "completed", "turn_completed")
        self.assertTrue(moved, "the race must occur after discovery returned")
        self.assertEqual(found_paths[:2], [path.resolve(), archived.resolve()])
        self.assertIn("reopened-archive", result.stdout)
        _, anchor = self.anchor(result)
        self.assertEqual((anchor["device"], anchor["inode"]), (before.st_dev, before.st_ino))

    def test_replacement_and_truncation_stop_as_unknown(self):
        for change in ("replacement", "truncation"):
            with self.subTest(change=change):
                path = self.rollout(meta() + event("task_started") + message("source-bound"))
                live = self.start_watch("--follow")
                live.wait_for("source-bound")
                if change == "replacement":
                    replacement = self.home / "replacement.jsonl"
                    replacement.write_bytes(meta() + event("task_started")
                                            + message("replacement-must-not-broadcast")
                                            + event("task_complete"))
                    os.replace(replacement, path)
                else:
                    path.write_bytes(meta())
                result = live.finish()
                self.assert_result(result, 3, "unknown", "source_changed")
                self.assertNotIn("replacement-must-not-broadcast", result.stdout)
                self.assertIsNone(self.anchor(result)[1], "changed source invalidates its anchor")

    def test_live_same_length_prefix_rewrite_invalidates_source(self):
        data = meta() + event("task_started") + message("live-prefix")
        path = self.rollout(data)
        live = self.start_watch("--follow")
        live.wait_for("live-prefix")
        before = path.stat()
        with path.open("r+b") as stream:
            stream.seek(data.index(b"live-prefix"))
            stream.write(b"LIVE-prefix")
            stream.flush()
        after = path.stat()
        self.assertEqual((before.st_ino, before.st_size), (after.st_ino, after.st_size))
        result = live.finish()
        self.assert_result(result, 3, "unknown", "source_changed")
        self.assertIsNone(self.anchor(result)[1])

    def test_rewrite_plus_append_invalidates_unknown_and_sigint_checkpoints(self):
        """A growing file must still recheck old bytes before any resume anchor."""
        with mock.patch.object(sys, "path", [str(SCRIPT.parent), *sys.path]), \
                mock.patch.object(sys, "dont_write_bytecode", True):
            module = importlib.import_module("rollout_watch")
        for stop in ("idle", "timeout", "sigint"):
            with self.subTest(stop=stop):
                data = meta() + event("task_started") + message("old-prefix")
                path = self.rollout(data)
                original_stat = module._current_stat
                calls = 0

                def rewrite_then_stat(home, tid, current_path):
                    nonlocal calls
                    calls += 1
                    if calls == 2:
                        # Mutate inside a poll, avoiding a timing race between two writes.
                        before = path.stat()
                        with path.open("r+b") as stream:
                            stream.seek(data.index(b"old-prefix"))
                            stream.write(b"NEW-prefix")
                            stream.seek(0, os.SEEK_END)
                            stream.write(message("appended-after-rewrite"))
                        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000))
                    if stop == "sigint" and calls == 3:
                        raise KeyboardInterrupt
                    return original_stat(home, tid, current_path)

                args = SimpleNamespace(
                    thread_id=THREAD, timeout=0.08 if stop == "timeout" else 2,
                    wait_file=0.2, since=0, anchor=None, turn_id=TURN_1, follow=True,
                    filter=None, idle_timeout=0.04 if stop == "idle" else 1, interval=0.001)
                stdout, stderr = io.StringIO(), io.StringIO()
                with mock.patch.object(module, "_current_stat", side_effect=rewrite_then_stat), \
                        redirect_stdout(stdout), redirect_stderr(stderr):
                    code = module.watch_rollout(args, self.home)
                result = subprocess.CompletedProcess(
                    ["watch_rollout"], code, stdout.getvalue(), stderr.getvalue())
                self.assert_result(result, 130 if stop == "sigint" else 3,
                                   "unknown", "source_changed")
                self.assertIn("appended-after-rewrite", result.stdout)
                self.assertIsNone(self.anchor(result)[1])

    def test_anchor_resume_alone_and_with_since_does_not_repeat(self):
        path = self.rollout(meta() + event("task_started") + message("already-seen"))
        first = self.run_watch()
        self.assert_result(first, 0, "replay", "eof")
        offset, anchor = self.anchor(first)
        self.append(path, message("newly-appended") + event("task_complete"))
        for options in (("--anchor", json.dumps(anchor)),
                        ("--since", offset, "--anchor", json.dumps(anchor))):
            with self.subTest(options=options):
                result = self.run_watch(*options)
                self.assert_result(result, 0, "replay", "eof")
                self.assertNotIn("already-seen", result.stdout)
                self.assertEqual(result.stdout.count("newly-appended"), 1)

    def test_nonzero_since_without_anchor_and_mismatched_offset_are_rejected(self):
        self.rollout(meta() + message("seen"))
        offset, anchor = self.anchor(self.run_watch())
        self.assert_result(self.run_watch("--since", offset), 1)
        self.assert_result(self.run_watch("--since", offset - 1,
                                         "--anchor", json.dumps(anchor)), 1)

    def test_anchor_rejects_prefix_mutation_and_different_inode(self):
        for mutation in ("prefix", "inode"):
            with self.subTest(mutation=mutation):
                data = meta() + message("old-prefix")
                path = self.rollout(data)
                _, anchor = self.anchor(self.run_watch())
                if mutation == "prefix":
                    path.write_bytes(data.replace(b"old-prefix", b"NEW-prefix"))
                else:
                    replacement = self.home / "replacement.jsonl"
                    replacement.write_bytes(data)
                    os.replace(replacement, path)
                self.assert_result(self.run_watch("--anchor", json.dumps(anchor)), 1)

    def test_anchor_cannot_cross_thread_identity(self):
        self.rollout(meta())
        _, anchor = self.anchor(self.run_watch())
        self.rollout(meta(OTHER_THREAD), OTHER_THREAD)
        self.assert_result(self.run_watch("--anchor", json.dumps(anchor),
                                         thread=OTHER_THREAD), 1)

    def test_anchor_rejects_mid_record_and_mid_utf8_offsets_even_with_valid_hash(self):
        data = meta() + message("anchor-中文-boundary")
        self.rollout(data)
        _, anchor = self.anchor(self.run_watch())
        cuts = (data.index(b"anchor-") + 3,
                data.index("中".encode("utf-8")) + 1)
        for cut in cuts:
            with self.subTest(cut=cut):
                forged = dict(anchor, offset=cut, sha256=hashlib.sha256(data[:cut]).hexdigest())
                result = self.run_watch("--anchor", json.dumps(forged))
                self.assert_result(result, 1)

    def test_anchor_resume_after_archive_keeps_identity(self):
        path = self.rollout(meta() + message("before-move"))
        _, anchor = self.anchor(self.run_watch())
        archived = self.home / "archived_sessions" / path.name
        archived.parent.mkdir(parents=True)
        path.rename(archived)
        self.append(archived, message("after-move"))
        result = self.run_watch("--anchor", json.dumps(anchor))
        self.assert_result(result, 0, "replay", "eof")
        self.assertNotIn("before-move", result.stdout)
        self.assertIn("after-move", result.stdout)

    def test_follow_anchor_keeps_bound_turn_and_rejects_conflicting_turn(self):
        path = self.rollout(meta() + event("task_started") + message("original-turn"))
        first = self.run_watch("--follow", "--idle-timeout", "0.06")
        self.assert_result(first, 3, "unknown", "idle_timeout")
        _, anchor = self.anchor(first)
        self.assertEqual(anchor["turn_id"], TURN_1)
        self.assert_result(self.run_watch("--follow", "--turn-id", TURN_2,
                                         "--anchor", json.dumps(anchor)), 1)
        self.append(path, event("task_started", TURN_2) + message("later-turn")
                    + event("task_complete", TURN_1))
        result = self.run_watch("--follow", "--anchor", json.dumps(anchor))
        self.assert_result(result, 0, "completed", "turn_completed")
        self.assertIn(f"WATCH_TURN_ID={TURN_1}", result.stdout)
        self.assertNotIn("original-turn", result.stdout)

    def test_anchor_rejects_malformed_json(self):
        self.rollout(meta())
        for value in ("not json", "[]", "null", "{}"):
            with self.subTest(value=value):
                self.assert_result(self.run_watch("--anchor", value), 1)

    def test_idle_timeout_and_file_wait_obey_total_deadline(self):
        self.rollout(meta() + event("task_started"))
        result = self.run_watch("--follow", "--idle-timeout", "0.08")
        self.assert_result(result, 3, "unknown", "idle_timeout")
        started = time.monotonic()
        result = self.run_watch("--follow", "--timeout", "0.08", "--wait-file", "2",
                                thread=OTHER_THREAD)
        self.assert_result(result, 3, "unknown", "timeout")
        self.assertLess(time.monotonic() - started, 1.5, "file wait escaped total deadline")

    def test_active_turn_global_timeout_is_distinct_from_idle_timeout(self):
        self.rollout(meta() + event("task_started"))
        result = self.run_watch("--follow", "--timeout", "0.08", "--idle-timeout", "2")
        self.assert_result(result, 3, "unknown", "timeout")
        self.anchor(result)

    def test_duration_arguments_require_positive_finite_numbers(self):
        for option in ("--timeout", "--idle-timeout", "--interval", "--wait-file"):
            for value in ("nan", "inf", "1e400", "0", "-1"):
                with self.subTest(option=option, value=value):
                    self.assert_result(self.run_watch(option, value), 2)
        for value in ("-1", "1.5", "nan"):
            with self.subTest(since=value):
                self.assert_result(self.run_watch("--since", value), 2)

    @unittest.skipUnless(os.name == "posix", "SIGINT return-code contract is POSIX-specific")
    def test_user_sigint_preserves_resume_anchor(self):
        self.rollout(meta() + event("task_started") + message("signal-ready"))
        live = self.start_watch("--follow")
        live.wait_for("signal-ready")
        live.proc.send_signal(signal.SIGINT)
        result = live.finish()
        self.assert_result(result, 130, "unknown", "user_interrupt")
        self.assertGreater(self.anchor(result)[0], 0)

    def test_app_0159_record_types_replay_quietly(self):
        """0.159.2 真实落盘类型回放零 WARN（2026-10-03 实锤：未注册时 710 条记录 396 条诊断）。"""
        data = (meta()
                + record("token_usage_record", {"thread_id": THREAD, "turn_id": TURN_1,
                                                "usage": {"total": 1}})
                + record("world_state", {"full": True, "state": {}})
                + record("inter_agent_communication_metadata", {"thread_id": THREAD})
                + event("task_started")
                + record("event_msg", {"type": "item_started", "turn_id": TURN_1,
                                       "item": {"type": "agentMessage"}})
                + record("event_msg", {"type": "thread_settings_applied", "turn_id": TURN_1})
                + record("event_msg", {"type": "item_completed", "turn_id": TURN_1,
                                       "item": {"type": "agentMessage"}})
                + record("response_item", {"type": "agent_message", "id": "amsg_1",
                                           "author": "/root/docs", "recipient": "/root",
                                           "content": [{"type": "output_text",
                                                        "text": "internal-chatter"}]})
                + message("visible-reply")
                + event("task_complete"))
        path = self.rollout(data)
        result = self.run_watch()
        self.assert_result(result, 0, "replay", "eof")
        self.assertIn("visible-reply", result.stdout)
        self.assertNotIn("internal-chatter", result.stdout,
                         "多代理内部通信不进直播摘要")
        self.assertNotIn("WARN:", result.stderr)
        self.assertIn("DIAGNOSTICS=0", result.stdout)
        self.assertEqual(path.read_bytes(), data, "watch must remain read-only")

    def test_unknown_type_warns_once_per_type_but_counts_all(self):
        """降采样：同一未知类型只 WARN 一次；DIAGNOSTICS 仍累计总出现次数。"""
        weird = record("event_msg", {"type": "brand_new_future_event", "turn_id": TURN_1})
        other = record("response_item", {"type": "brand_new_future_item"})
        data = meta() + event("task_started") + weird + weird + weird + other + other
        self.rollout(data)
        result = self.run_watch()
        self.assert_result(result, 0, "replay", "eof")
        self.assertEqual(result.stderr.count("未知 event_msg payload 类型"), 1,
                         result.stderr)
        self.assertEqual(result.stderr.count("未知 response_item payload 类型"), 1,
                         result.stderr)
        self.assertIn("DIAGNOSTICS=5", result.stdout)

    def test_structural_anomalies_warn_every_time(self):
        """无降采样 key 的结构性诊断（真异常）每次出现都 WARN，不被静默。"""
        bad = record("event_msg", {"no_type_field": True})
        data = meta() + event("task_started") + bad + bad
        self.rollout(data)
        result = self.run_watch()
        self.assert_result(result, 0, "replay", "eof")
        self.assertEqual(result.stderr.count("payload.type 必须是字符串"), 2, result.stderr)
        self.assertIn("DIAGNOSTICS=2", result.stdout)


if __name__ == "__main__":
    unittest.main()
