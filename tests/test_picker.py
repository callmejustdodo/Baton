from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from baton.fetch import HandoffReceipt
from baton.picker import (
    PickerCancelled,
    PickerError,
    SessionChoice,
    choose_handoff,
    choose_session,
    list_local_sessions,
)

SESSION_A = "019f5ef4-780a-7973-a1d2-c460461ced1f"
SESSION_B = "019f5ef4-780a-7973-a1d2-c42661d5f05d"
SESSION_C = "019f5ef4-780a-7973-a1d2-c44ad680bc0e"


class PickerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.codex_home = self.root / "codex"

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_list_local_sessions_uses_valid_rollouts_and_orders_newest_first(self) -> None:
        first = self._write_rollout(SESSION_A, cwd="/workspace/first")
        second = self._write_rollout(SESSION_B, cwd="/workspace/second")
        self._write_rollout(SESSION_C, payload_session_id=SESSION_A)
        os.utime(first, ns=(2_000_000_000, 2_000_000_000))
        os.utime(second, ns=(1_000_000_000, 1_000_000_000))
        (self.codex_home / "session_index.jsonl").write_text(
            "not json\n"
            + json.dumps(
                {
                    "id": SESSION_A,
                    "thread_name": "First session",
                    "updated_at": "2026-08-23T00:00:00Z",
                }
            )
            + "\n"
            + json.dumps(
                {
                    "id": SESSION_B,
                    "thread_name": "Second session",
                    "updated_at": "2026-08-23T01:00:00Z",
                }
            )
            + "\n"
            + json.dumps({"id": "not-a-uuid", "thread_name": "ignore"})
            + "\n",
            encoding="utf-8",
        )

        sessions = list_local_sessions(codex_home=self.codex_home)

        self.assertEqual([session.session_id for session in sessions], [SESSION_A, SESSION_B])
        self.assertEqual(sessions[0].title, "First session")
        self.assertEqual(sessions[0].cwd, "/workspace/first")
        self.assertEqual(sessions[1].updated_at, "2026-08-23T01:00:00Z")

    def test_list_local_sessions_filters_to_the_requested_workspace(self) -> None:
        workspace = self.root / "workspace"
        workspace.mkdir()
        self._write_rollout(SESSION_A, cwd=str(workspace))
        self._write_rollout(SESSION_B, cwd=str(self.root / "other-workspace"))

        sessions = list_local_sessions(
            codex_home=self.codex_home,
            workspace=workspace,
        )

        self.assertEqual([session.session_id for session in sessions], [SESSION_A])

    def test_choose_session_reprompts_and_returns_displayed_choice(self) -> None:
        first, second = self._choices()
        output = io.StringIO()

        chosen = choose_session(
            (first, second),
            input_stream=io.StringIO("not a number\n2\n"),
            output_stream=output,
        )

        self.assertEqual(chosen, second)
        rendered = output.getvalue()
        self.assertIn("1. First", rendered)
        self.assertIn("2. Second", rendered)
        self.assertIn("Enter a displayed number", rendered)

    def test_picker_escapes_terminal_control_sequences_in_session_metadata(self) -> None:
        unsafe = SessionChoice(
            session_id=SESSION_A,
            rollout_path=Path("/unsafe.jsonl"),
            title="\x1b]8;;https://example.test\x1b\\spoof\nrow",
            cwd="/workspace\tunsafe",
            updated_at="2026-08-23T00:00:00Z",
            modified_at_ns=1,
        )
        output = io.StringIO()

        choose_session((unsafe,), input_stream=io.StringIO("1\n"), output_stream=output)

        rendered = output.getvalue()
        self.assertNotIn("\x1b", rendered)
        self.assertIn("\\x1b", rendered)
        self.assertIn("\\x0a", rendered)
        self.assertIn("\\x09", rendered)

    def test_choose_session_cancellation_and_eof_are_clean(self) -> None:
        choice, _ = self._choices()

        with self.assertRaisesRegex(PickerCancelled, "cancelled"):
            choose_session(
                (choice,), input_stream=io.StringIO("q\n"), output_stream=io.StringIO()
            )
        with self.assertRaisesRegex(PickerCancelled, "cancelled"):
            choose_session((choice,), input_stream=io.StringIO(), output_stream=io.StringIO())

    def test_choose_session_requires_a_terminal_when_using_stdin(self) -> None:
        choice, _ = self._choices()

        with (
            patch("baton.picker.sys.stdin", _NonInteractiveInput()),
            self.assertRaisesRegex(PickerError, "interactive terminal"),
        ):
            choose_session((choice,), output_stream=io.StringIO())

    def test_choose_handoff_returns_the_selected_receipt(self) -> None:
        receipt = HandoffReceipt(
            path=self.root / "handoff.json",
            sandbox_id="sb-picker-test",
            session_id=SESSION_A,
            archive=self.root / "snapshot.tar.gz",
            workspace=self.root,
        )

        chosen = choose_handoff(
            (receipt,), input_stream=io.StringIO("1\n"), output_stream=io.StringIO()
        )

        self.assertEqual(chosen, receipt)

    def test_empty_picker_has_an_actionable_error(self) -> None:
        with self.assertRaisesRegex(PickerError, "no local Codex sessions"):
            choose_session((), input_stream=io.StringIO(), output_stream=io.StringIO())

    def _write_rollout(
        self,
        session_id: str,
        *,
        payload_session_id: str | None = None,
        cwd: str = "/workspace",
    ) -> Path:
        rollout = (
            self.codex_home
            / "sessions"
            / "2026"
            / "08"
            / "23"
            / f"rollout-2026-08-23T13-14-26-{session_id}.jsonl"
        )
        rollout.parent.mkdir(parents=True, exist_ok=True)
        rollout.write_text(
            json.dumps(
                {
                    "type": "session_meta",
                    "payload": {"session_id": payload_session_id or session_id, "cwd": cwd},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        return rollout

    @staticmethod
    def _choices() -> tuple[SessionChoice, SessionChoice]:
        return (
            SessionChoice(
                session_id=SESSION_A,
                rollout_path=Path("/first.jsonl"),
                title="First",
                cwd=None,
                updated_at=None,
                modified_at_ns=2,
            ),
            SessionChoice(
                session_id=SESSION_B,
                rollout_path=Path("/second.jsonl"),
                title="Second",
                cwd=None,
                updated_at=None,
                modified_at_ns=1,
            ),
        )


class _NonInteractiveInput(io.StringIO):
    def isatty(self) -> bool:
        return False
