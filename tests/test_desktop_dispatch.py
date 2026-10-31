"""Native receipt/state regressions using only temporary SQLite and rollout files."""
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import stat
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import desktop_dispatch as desktop


THREAD = "22222222-2222-4222-8222-222222222222"
OTHER = "33333333-3333-4333-8333-333333333333"
TURN = "first-turn"
MODEL = "gpt-6.1-sol"


def record(kind, payload):
    return json.dumps({"type": kind, "payload": payload}, ensure_ascii=False) + "\n"


class DesktopDispatchTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="desktop-dispatch-test-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.home = self.root / "codex"
        self.home.mkdir()
        self.cwd = self.root / "shelf"
        self.cwd.mkdir()
        self.state = self.root / "receipts"
        patcher = mock.patch.dict(os.environ, {
            "CODEX_BRIDGE_CODEX_HOME": str(self.home),
            "CODEX_BRIDGE_STATE_DIR": str(self.state),
        })
        patcher.start()
        self.addCleanup(patcher.stop)
        # Any accidental App launch or notification fails this fixture immediately.
        process = mock.patch.object(desktop.subprocess, "run",
                                    side_effect=AssertionError("no real process allowed"))
        self.run = process.start()
        self.addCleanup(process.stop)
        self.database = self.home / "state_5.sqlite"
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("""CREATE TABLE threads (
                id TEXT PRIMARY KEY, cwd TEXT, first_user_message TEXT,
                created_at INTEGER, created_at_ms INTEGER, archived INTEGER,
                rollout_path TEXT, project_id TEXT)""")
        self.receipt_path = None
        self.receipt = None

    def prepare(self, **kwargs):
        result = desktop.prepare_dispatch(str(self.cwd), "fixture prompt", MODEL,
                                          effort="high", prepare_only=True, **kwargs)
        self.receipt_path = Path(result["receipt_path"])
        self.receipt = json.loads(self.receipt_path.read_text())
        return result

    def rollout(self, thread=THREAD, *, cwd=None, model=MODEL, effort="high",
                terminal="task_complete", include_prompt=True, include_context=True,
                prompt=None, session_id=None):
        folder = self.home / "sessions/2026/10/03"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"rollout-2026-10-03T00-00-00-{thread}.jsonl"
        data = record("session_meta", {"id": session_id or thread})
        data += record("event_msg", {"type": "task_started", "turn_id": TURN})
        # Actual desktop order can place user text before turn_context.
        if include_prompt:
            data += record("response_item", {
                "type": "message", "role": "user",
                "content": [{"type": "input_text",
                             "text": prompt if prompt is not None else self.receipt["prompt"] + "\n"}],
            })
        if include_context:
            data += record("turn_context", {
                "turn_id": TURN, "cwd": str(cwd or self.cwd), "model": model, "effort": effort,
            })
        if terminal:
            data += record("event_msg", {"type": terminal, "turn_id": TURN})
        path.write_text(data)
        return path

    def insert(self, thread=THREAD, *, cwd=None, archived=0, path=None, prompt=None,
               created_at_ms=None):
        path = path or self.rollout(thread, cwd=cwd)
        created = self.receipt["created_at_ms"] + 1 if created_at_ms is None else created_at_ms
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (
                thread, str(cwd or self.cwd),
                self.receipt["prompt"] if prompt is None else prompt,
                created // 1000, created, archived, str(path), None,
            ))
        return path

    def confirm(self, **kwargs):
        return desktop.confirm_dispatch(self.receipt_path, **kwargs)

    def test_prepare_only_has_one_dispatch_id_and_private_receipt(self):
        result = self.prepare()
        self.assertEqual(result["status"], "prepared")
        self.assertTrue(result["needs_gui_submit"])
        self.assertEqual(self.receipt["prompt"].count("[WB_DISPATCH_ID="), 1)
        self.assertEqual(stat.S_IMODE(self.receipt_path.stat().st_mode), 0o600)
        self.run.assert_not_called()

    def test_no_submission_remains_awaiting(self):
        self.prepare()
        result = self.confirm()
        self.assertEqual(result["status"], "awaiting_desktop_submit")
        self.assertIsNone(result["thread_id"])

    def test_created_checks_first_turn_and_never_claims_gui_verified(self):
        self.prepare()
        self.insert()
        result = self.confirm()
        self.assertEqual(result["status"], "created")
        self.assertEqual(result["thread_id"], THREAD)
        self.assertEqual(result["first_turn_id"], TURN)
        self.assertEqual(result["first_turn_status"], "completed")
        self.assertEqual(result["actual_model"], MODEL)
        self.assertEqual(result["actual_effort"], "high")
        self.assertFalse(result["gui_verified"])
        self.assertFalse(result["needs_gui_submit"])
        self.assertTrue(desktop.is_desktop_thread(THREAD))

    def test_delayed_context_transitions_from_pending_to_created(self):
        self.prepare()
        path = self.rollout(terminal=None, include_context=False)
        self.insert(path=path)
        self.assertEqual(self.confirm()["status"], "pending")
        with path.open("a") as stream:
            stream.write(record("turn_context", {
                "turn_id": TURN, "cwd": str(self.cwd), "model": MODEL, "effort": "high",
            }))
        self.assertEqual(self.confirm()["status"], "created")

    def test_wrong_project_is_mismatch_and_protected_before_and_after_confirmation(self):
        self.prepare()
        wrong = self.root / "wrong-project"
        wrong.mkdir()
        self.insert(cwd=wrong)
        self.assertTrue(desktop.is_desktop_thread(THREAD))
        result = self.confirm()
        self.assertEqual(result["status"], "mismatch")
        self.assertEqual(result["thread_id"], THREAD)
        self.assertIn("cwd_mismatch", result["reasons"])
        self.assertFalse(result["needs_gui_submit"])
        self.assertTrue(desktop.is_desktop_thread(THREAD))
        saved = json.loads(self.receipt_path.read_text())
        self.assertNotIn("confirmed_thread_id", saved)
        self.assertIn(THREAD, saved["observed_thread_ids"])

    def test_multiple_candidates_are_ambiguous_even_when_one_cwd_is_wrong(self):
        self.prepare()
        self.insert()
        wrong = self.root / "other-project"
        wrong.mkdir()
        self.insert(OTHER, cwd=wrong)
        result = self.confirm()
        self.assertEqual(result["status"], "ambiguous")
        self.assertEqual(set(result["candidate_thread_ids"]), {THREAD, OTHER})
        self.assertIsNone(result["thread_id"])
        self.assertTrue(desktop.is_desktop_thread(THREAD))
        self.assertTrue(desktop.is_desktop_thread(OTHER))

    def test_first_model_mismatch_cannot_be_repaired_by_later_turn(self):
        self.prepare()
        path = self.rollout(model="gpt-6-astra")
        with path.open("a") as stream:
            stream.write(record("event_msg", {"type": "task_started", "turn_id": "later-turn"}))
            stream.write(record("turn_context", {
                "turn_id": "later-turn", "cwd": str(self.cwd), "model": MODEL, "effort": "high",
            }))
        self.insert(path=path)
        result = self.confirm()
        self.assertEqual(result["status"], "mismatch")
        self.assertIn("model_mismatch", result["reasons"])
        self.assertEqual(result["actual_model"], "gpt-6-astra")

    def test_requested_effort_mismatch_is_rejected(self):
        self.prepare()
        self.insert(path=self.rollout(effort="low"))
        result = self.confirm()
        self.assertEqual(result["status"], "mismatch")
        self.assertIn("reasoning_effort_mismatch", result["reasons"])

    def test_failed_first_turn_reports_failure_while_identity_is_confirmed(self):
        self.prepare()
        self.insert(path=self.rollout(terminal="task_failed"))
        result = self.confirm()
        self.assertEqual(result["status"], "created", "created confirms identity, not task success")
        self.assertEqual(result["first_turn_status"], "failed")

    def test_failed_turn_without_matching_prompt_is_mismatch_not_pending(self):
        self.prepare()
        self.insert(path=self.rollout(terminal="task_failed", include_prompt=False))
        result = self.confirm()
        self.assertEqual(result["status"], "mismatch")
        self.assertEqual(result["first_turn_status"], "failed")
        self.assertIn("first_turn_prompt_mismatch", result["reasons"])

    def test_aborted_first_turn_remains_distinct(self):
        self.prepare()
        self.insert(path=self.rollout(terminal="turn_aborted"))
        self.assertEqual(self.confirm()["first_turn_status"], "aborted")

    def test_archived_candidate_cannot_be_confirmed(self):
        self.prepare()
        self.insert(archived=1)
        result = self.confirm()
        self.assertEqual(result["status"], "mismatch")
        self.assertIn("thread_archived", result["reasons"])

    def test_explicit_thread_with_wrong_prompt_is_mismatch(self):
        self.prepare()
        self.insert(prompt="different dispatch")
        result = self.confirm(thread_id=THREAD)
        self.assertEqual(result["status"], "mismatch")
        self.assertIn("prompt_mismatch", result["reasons"])

    def test_session_identity_mismatch_fails_closed(self):
        self.prepare()
        self.insert(path=self.rollout(session_id=OTHER))
        with self.assertRaises(desktop.DesktopDispatchError):
            self.confirm()

    def test_confirmed_receipt_cannot_rebind_to_another_thread(self):
        self.prepare()
        self.insert()
        self.assertEqual(self.confirm()["status"], "created")
        with self.assertRaises(desktop.DesktopDispatchError):
            self.confirm(thread_id=OTHER)

    def test_invalid_receipt_blocks_external_writer_ownership_check(self):
        self.prepare()
        damaged = dict(self.receipt, schema="unsupported-schema")
        self.receipt_path.write_text(json.dumps(damaged))
        with self.assertRaises(desktop.DesktopDispatchError):
            desktop.is_desktop_thread(THREAD)

    def test_preexisting_matching_thread_is_excluded_by_snapshot(self):
        request = "a" * 32
        prompt = "fixture prompt\n\n[WB_DISPATCH_ID=" + request + "]"
        created = time.time_ns() // 1_000_000
        # A preexisting matching row must be excluded even in the same clock tick.
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (
                THREAD, str(self.cwd), prompt, created // 1000, created, 0,
                str(self.home / "sessions/missing.jsonl"), None,
            ))
        with mock.patch.object(desktop.uuid, "uuid4", return_value=SimpleNamespace(hex=request)):
            self.prepare()
        self.assertIn(THREAD, self.receipt["existing_thread_ids"])
        self.assertEqual(self.confirm()["status"], "awaiting_desktop_submit")


if __name__ == "__main__":
    unittest.main()
