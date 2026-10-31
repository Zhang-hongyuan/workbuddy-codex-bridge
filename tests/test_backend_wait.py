"""Backend event binding without a process, socket, real receipt or App."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import queue
import shutil
import sys
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import codex_bridge as bridge


THREAD = "fixture-thread"
TURN = "requested-turn"


def completed(turn=TURN, thread=THREAD, status="completed"):
    return {"method": "turn/completed", "params": {
        "threadId": thread, "turn": {"id": turn, "status": status},
    }}


def delta(text, turn=TURN, thread=THREAD):
    return {"method": "item/agentMessage/delta", "params": {
        "threadId": thread, "turnId": turn, "delta": text,
    }}


class BackendWaitTests(unittest.TestCase):
    def server(self, *, notes=(), queued=()):
        server = bridge.AppServer.__new__(bridge.AppServer)
        server._notes = list(notes)
        server.q = queue.Queue()
        for message in queued:
            server.q.put(json.dumps(message))
        return server

    def test_old_turn_completion_cannot_stop_requested_turn(self):
        server = self.server(notes=[completed("old-turn"), delta("old-reply", "old-turn")],
                             queued=[delta("new-reply"), completed()])
        navigations = []
        final, reply, _ = server.wait_turn(0.1, THREAD, turn_id=TURN,
                                          on_first_event=lambda: navigations.append("requested"))
        self.assertEqual(final["id"], TURN)
        self.assertEqual(reply, "new-reply")
        self.assertEqual(navigations, ["requested"])

    def test_other_thread_and_untagged_events_do_not_claim_success(self):
        untagged = completed()
        del untagged["params"]["threadId"]
        server = self.server(notes=[completed(thread="other"), untagged],
                             queued=[completed(status="failed")])
        final, _, _ = server.wait_turn(0.1, THREAD, turn_id=TURN)
        self.assertEqual(final["status"], "failed")
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as error:
                bridge.check_turn_ok(final, THREAD)
        self.assertEqual(error.exception.code, 2)

    def test_missing_or_malformed_turn_identity_does_not_complete_wait(self):
        malformed = [
            {"method": "turn/completed", "params": None},
            {"method": "turn/completed", "params": {"threadId": THREAD, "turn": None}},
            {"method": "turn/completed", "params": {"threadId": THREAD, "turn": {"status": "completed"}}},
        ]
        server = self.server(notes=malformed, queued=[completed()])
        final, _, _ = server.wait_turn(0.1, THREAD, turn_id=TURN)
        self.assertEqual(final["id"], TURN)

    def test_missing_turn_id_cannot_start_unbound_wait(self):
        with self.assertRaises(bridge.BridgeError):
            self.server().wait_turn(0.1, THREAD)

    def test_wrong_turn_only_times_out_instead_of_completing(self):
        server = self.server(notes=[completed("old-turn")])
        with self.assertRaisesRegex(bridge.BridgeError, "超时"):
            server.wait_turn(0.01, THREAD, turn_id=TURN)

    def test_sync_dispatch_passes_started_turn_id_to_wait(self):
        args = SimpleNamespace(
            ephemeral=False, name="fixture", transport="daemon", open_bg=False,
            no_open=True, prepare_only=False, async_fire=False, task="fixture",
            cwd=str(Path(__file__).resolve().parents[1]), model="gpt-6.1-sol",
            effort=None, timeout=1)
        server = mock.Mock()
        server.call.side_effect = [
            {"thread": {"id": THREAD}}, {"turn": {"id": TURN}},
        ]
        server.wait_turn.return_value = ({"id": TURN, "status": "completed"}, "reply", [])
        with mock.patch.object(bridge, "AppServer", return_value=server), \
                redirect_stdout(io.StringIO()):
            bridge.cmd_dispatch(args)
        self.assertEqual(server.wait_turn.call_args.kwargs["turn_id"], TURN)
        server.close.assert_called_once()

    def test_append_steer_passes_started_turn_id_to_wait(self):
        args = SimpleNamespace(
            thread_id=THREAD, transport="daemon", model="gpt-6.1-sol",
            turn_id=None, live=False, text="fixture", effort=None, timeout=1)
        server = mock.Mock()
        server.call.side_effect = [{}, {"turn": {"id": TURN}}]
        server.wait_turn.return_value = ({"id": TURN, "status": "completed"}, "reply", [])
        with mock.patch.object(bridge, "AppServer", return_value=server), \
                mock.patch.object(bridge, "require_background_writer"), \
                redirect_stdout(io.StringIO()):
            bridge.cmd_steer(args)
        self.assertEqual(server.wait_turn.call_args.kwargs["turn_id"], TURN)
        server.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
