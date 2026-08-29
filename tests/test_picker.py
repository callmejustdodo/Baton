from __future__ import annotations

import io
import json
import os
import tempfile
import termios
import unittest
from pathlib import Path
from unittest.mock import patch

from baton.fetch import HandoffReceipt
from baton.picker import (
    PickerCancelled,
    PickerError,
    SessionChoice,
    _raw_terminal,
    _read_picker_key,
    _truncate_terminal_line,
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

    def test_choose_session_selects_the_first_latest_choice_with_enter(self) -> None:
        first, second = self._choices()
        output = io.StringIO()

        chosen = choose_session(
            (first, second),
            input_stream=io.StringIO("\r"),
            output_stream=output,
        )

        self.assertEqual(chosen, first)
        rendered = output.getvalue()
        self.assertIn("Most recently active session is first", rendered)
        self.assertIn("> First", rendered)
        self.assertNotIn("1. First", rendered)

    def test_choose_session_uses_down_arrow_and_enter(self) -> None:
        first, second = self._choices()
        output = io.StringIO()

        chosen = choose_session(
            (first, second),
            input_stream=io.StringIO("\x1b[B\r"),
            output_stream=output,
        )

        self.assertEqual(chosen, second)
        self.assertIn("\x1b[2A", output.getvalue())
        self.assertIn("> Second", output.getvalue())

    def test_choose_session_wraps_up_arrow_from_first_choice_to_last(self) -> None:
        first, second = self._choices()

        chosen = choose_session(
            (first, second),
            input_stream=io.StringIO("\x1b[A\r"),
            output_stream=io.StringIO(),
        )

        self.assertEqual(chosen, second)

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

        choose_session((unsafe,), input_stream=io.StringIO("\r"), output_stream=output)

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
            choose_session((choice,), input_stream=io.StringIO("\x1b"), output_stream=io.StringIO())
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
            devbox_id="devbox-picker-test",
            session_id=SESSION_A,
            archive=self.root / "snapshot.tar.gz",
            workspace=self.root,
        )

        chosen = choose_handoff(
            (receipt,), input_stream=io.StringIO("\r"), output_stream=io.StringIO()
        )

        self.assertEqual(chosen, receipt)

    def test_raw_terminal_restores_settings_after_selection_ends(self) -> None:
        input_stream = _TTYInput()
        original_settings = ["original"]
        with (
            patch("termios.tcgetattr", return_value=original_settings) as get_attributes,
            patch("tty.setraw") as set_raw,
            patch("termios.tcsetattr") as set_attributes,
            _raw_terminal(input_stream),
        ):
            pass

        get_attributes.assert_called_once_with(input_stream.fileno())
        set_raw.assert_called_once_with(input_stream.fileno())
        set_attributes.assert_called_once_with(
            input_stream.fileno(),
            termios.TCSADRAIN,
            original_settings,
        )

    def test_raw_terminal_reads_down_arrow_directly_from_file_descriptor(self) -> None:
        input_stream = _TTYInput()
        with (
            patch("baton.picker.os.read", side_effect=(b"\x1b", b"[", b"B")) as read_byte,
            patch("baton.picker._terminal_input_ready", return_value=True),
        ):
            key = _read_picker_key(input_stream, raw_terminal=True)

        self.assertEqual(key, "down")
        self.assertEqual(
            [read_call.args for read_call in read_byte.call_args_list],
            [(input_stream.fileno(), 1)] * 3,
        )

    def test_terminal_line_truncation_keeps_each_choice_to_one_row(self) -> None:
        self.assertEqual(_truncate_terminal_line("abcdef", 5), "abcd…")
        self.assertEqual(_truncate_terminal_line("abcdef", 1), "…")
        self.assertEqual(_truncate_terminal_line("abcdef", None), "abcdef")

    def test_raw_terminal_restores_settings_when_selection_fails(self) -> None:
        input_stream = _TTYInput()
        original_settings = ["original"]
        with (
            patch("termios.tcgetattr", return_value=original_settings),
            patch("tty.setraw"),
            patch("termios.tcsetattr") as set_attributes,
            self.assertRaisesRegex(RuntimeError, "selection failed"),
            _raw_terminal(input_stream),
        ):
            raise RuntimeError("selection failed")

        set_attributes.assert_called_once_with(
            input_stream.fileno(),
            termios.TCSADRAIN,
            original_settings,
        )

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


class _TTYInput:
    @staticmethod
    def fileno() -> int:
        return 42
