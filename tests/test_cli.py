from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from baton.cli import main
from baton.fetch import HandoffReceipt
from baton.picker import PickerCancelled, SessionChoice

SESSION_ID = "019f5ef4-780a-7973-a1d2-c460461ced1f"
SANDBOX_ID = "sb-picker-test"


class CliPickerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.codex_home = self.root / "codex"
        self.codex_home.mkdir()
        self.snapshot_result = SimpleNamespace(path=self.root / "snapshot.tar.gz")
        self.handoff_result = SimpleNamespace(
            detached=False,
            sandbox_id=SANDBOX_ID,
            session_id=SESSION_ID,
            to_dict=lambda: {"sandbox_id": SANDBOX_ID},
        )
        self.fetch_result = SimpleNamespace(to_dict=lambda: {"sandbox_id": SANDBOX_ID})

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_explicit_handoff_id_bypasses_session_picker(self) -> None:
        with (
            patch("baton.cli.snapshot", return_value=self.snapshot_result) as snapshot_mock,
            patch("baton.cli.handoff_archive", return_value=self.handoff_result) as handoff_mock,
            patch("baton.cli.infer_local_codex_version", return_value="0.147.0"),
            patch("baton.cli.list_local_sessions") as sessions_mock,
            patch("baton.cli.choose_session") as picker_mock,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            exit_code = main(
                [
                    "handoff",
                    SESSION_ID,
                    "continue exactly from here",
                    "--workspace",
                    str(self.workspace),
                    "--codex-home",
                    str(self.codex_home),
                ]
            )

        self.assertEqual(exit_code, 0)
        snapshot_mock.assert_called_once()
        self.assertEqual(snapshot_mock.call_args.kwargs["session_id"], SESSION_ID)
        self.assertEqual(
            handoff_mock.call_args.kwargs["prompt"], "continue exactly from here"
        )
        sessions_mock.assert_not_called()
        picker_mock.assert_not_called()

    def test_handoff_prompt_only_selects_a_session(self) -> None:
        selected = self._session_choice()
        with (
            patch("baton.cli.list_local_sessions", return_value=(selected,)) as sessions_mock,
            patch("baton.cli.choose_session", return_value=selected) as picker_mock,
            patch("baton.cli.snapshot", return_value=self.snapshot_result) as snapshot_mock,
            patch("baton.cli.handoff_archive", return_value=self.handoff_result) as handoff_mock,
            patch("baton.cli.infer_local_codex_version", return_value="0.147.0"),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            exit_code = main(
                [
                    "handoff",
                    "continue exactly from here",
                    "--workspace",
                    str(self.workspace),
                    "--codex-home",
                    str(self.codex_home),
                ]
            )

        self.assertEqual(exit_code, 0)
        sessions_mock.assert_called_once_with(
            codex_home=self.codex_home,
            workspace=self.workspace,
        )
        picker_mock.assert_called_once_with((selected,))
        self.assertEqual(snapshot_mock.call_args.kwargs["session_id"], SESSION_ID)
        self.assertEqual(
            handoff_mock.call_args.kwargs["prompt"], "continue exactly from here"
        )

    def test_handoff_picker_cancellation_does_not_snapshot(self) -> None:
        selected = self._session_choice()
        with (
            patch("baton.cli.list_local_sessions", return_value=(selected,)),
            patch("baton.cli.choose_session", side_effect=PickerCancelled("selection cancelled")),
            patch("baton.cli.snapshot") as snapshot_mock,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            exit_code = main(["handoff", "continue working"])

        self.assertEqual(exit_code, 130)
        snapshot_mock.assert_not_called()

    def test_explicit_fetch_id_bypasses_handoff_picker(self) -> None:
        with (
            patch("baton.cli.fetch_workspace", return_value=self.fetch_result) as fetch_mock,
            patch("baton.cli.list_handoff_receipts") as receipts_mock,
            patch("baton.cli.choose_handoff") as picker_mock,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            exit_code = main(["fetch", SANDBOX_ID, "--workspace", str(self.workspace)])

        self.assertEqual(exit_code, 0)
        self.assertEqual(fetch_mock.call_args.kwargs["sandbox_id"], SANDBOX_ID)
        self.assertIsNone(fetch_mock.call_args.kwargs["receipt_path"])
        receipts_mock.assert_not_called()
        picker_mock.assert_not_called()

    def test_fetch_without_id_selects_a_receipt_and_passes_its_exact_path(self) -> None:
        receipt = HandoffReceipt(
            path=self.workspace / ".baton/handoffs" / f"{SANDBOX_ID}.json",
            sandbox_id=SANDBOX_ID,
            session_id=SESSION_ID,
            archive=self.root / "snapshot.tar.gz",
            workspace=self.workspace,
        )
        with (
            patch("baton.cli.list_handoff_receipts", return_value=(receipt,)) as receipts_mock,
            patch("baton.cli.choose_handoff", return_value=receipt) as picker_mock,
            patch("baton.cli.fetch_workspace", return_value=self.fetch_result) as fetch_mock,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            exit_code = main(["fetch", "--workspace", str(self.workspace)])

        self.assertEqual(exit_code, 0)
        receipts_mock.assert_called_once_with(workspace=self.workspace)
        picker_mock.assert_called_once_with((receipt,))
        self.assertEqual(fetch_mock.call_args.kwargs["sandbox_id"], SANDBOX_ID)
        self.assertEqual(fetch_mock.call_args.kwargs["receipt_path"], receipt.path)

    def test_fetch_picker_cancellation_does_not_contact_modal(self) -> None:
        receipt = HandoffReceipt(
            path=self.workspace / ".baton/handoffs" / f"{SANDBOX_ID}.json",
            sandbox_id=SANDBOX_ID,
            session_id=SESSION_ID,
            archive=self.root / "snapshot.tar.gz",
            workspace=self.workspace,
        )
        with (
            patch("baton.cli.list_handoff_receipts", return_value=(receipt,)),
            patch("baton.cli.choose_handoff", side_effect=PickerCancelled("selection cancelled")),
            patch("baton.cli.fetch_workspace") as fetch_mock,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            exit_code = main(["fetch", "--workspace", str(self.workspace)])

        self.assertEqual(exit_code, 130)
        fetch_mock.assert_not_called()

    def test_session_id_without_a_prompt_is_not_mistaken_for_picker_mode(self) -> None:
        with (
            patch("baton.cli.choose_session") as picker_mock,
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit) as error,
        ):
            main(["handoff", SESSION_ID])

        self.assertEqual(error.exception.code, 2)
        picker_mock.assert_not_called()

    @staticmethod
    def _session_choice() -> SessionChoice:
        return SessionChoice(
            session_id=SESSION_ID,
            rollout_path=Path("/rollout.jsonl"),
            title="Selected session",
            cwd=None,
            updated_at=None,
            modified_at_ns=1,
        )
