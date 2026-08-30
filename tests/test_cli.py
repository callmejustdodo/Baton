from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from baton.cli import build_parser, main
from baton.fetch import FetchError, HandoffReceipt
from baton.picker import PickerCancelled, SessionChoice
from baton.resume import ResumeError

SESSION_ID = "019f5ef4-780a-7973-a1d2-c460461ced1f"
DEVBOX_ID = "devbox-picker-test"


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
            devbox_id=DEVBOX_ID,
            session_id=SESSION_ID,
            to_dict=lambda: {"devbox_id": DEVBOX_ID},
        )
        self.fetch_result = SimpleNamespace(
            applied=False,
            remote_exit_code=0,
            fetch_root=self.workspace / ".baton/fetches" / DEVBOX_ID,
            to_dict=lambda: {"devbox_id": DEVBOX_ID},
        )
        self.resume_result = SimpleNamespace(
            local_exit_code=None,
            to_dict=lambda: {"devbox_id": DEVBOX_ID, "session_id": SESSION_ID},
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_handoff_defaults_to_valid_runloop_secret_name(self) -> None:
        args = build_parser().parse_args(["handoff", "continue working"])

        self.assertEqual(args.secret_name, "BATON_OPENAI_API_KEY")

    def test_prepare_runs_inside_loading_indicator(self) -> None:
        events: list[str] = []

        class _Indicator:
            def __init__(self, message: str) -> None:
                self.message = message

            def __enter__(self) -> None:
                events.append(f"start:{self.message}")

            def __exit__(self, *args: object) -> None:
                events.append("stop")

        def prepare_runtime(**kwargs: object) -> SimpleNamespace:
            events.append("prepare")
            return SimpleNamespace(to_dict=lambda: kwargs)

        with (
            patch("baton.cli.TerminalSpinner", _Indicator),
            patch("baton.cli.prepare_runtime", side_effect=prepare_runtime),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            exit_code = main(["prepare", "--codex-version", "0.151.0"])

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            events,
            ["start:Preparing Runloop runtime...", "prepare", "stop"],
        )

    def test_prepare_keeps_redirected_output_machine_readable(self) -> None:
        result = SimpleNamespace(to_dict=lambda: {"blueprint": "baton-test"})
        stdout = io.StringIO()
        stderr = io.StringIO()

        with (
            patch("baton.cli.prepare_runtime", return_value=result),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            exit_code = main(["prepare", "--codex-version", "0.151.0"])

        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(stdout.getvalue()), {"blueprint": "baton-test"})
        self.assertEqual(stderr.getvalue(), "")

    def test_handoff_rejects_invalid_runloop_secret_name(self) -> None:
        with (
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit) as raised,
        ):
            build_parser().parse_args(
                ["handoff", "continue working", "--secret-name", "baton-openai"]
            )

        self.assertEqual(raised.exception.code, 2)

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
        self.assertEqual(
            handoff_mock.call_args.kwargs["blueprint_name"], "baton-codex-0-147-0"
        )
        self.assertEqual(handoff_mock.call_args.kwargs["command_timeout"], 1200)
        self.assertEqual(handoff_mock.call_args.kwargs["idle_suspend_seconds"], 300)
        sessions_mock.assert_not_called()
        picker_mock.assert_not_called()

    def test_handoff_runs_snapshot_and_remote_work_inside_loading_indicator(self) -> None:
        events: list[str] = []

        class _Indicator:
            def __init__(self, message: str) -> None:
                self.message = message

            def __enter__(self) -> None:
                events.append(f"start:{self.message}")

            def __exit__(self, *args: object) -> None:
                events.append("stop")

        with (
            patch("baton.cli.TerminalSpinner", _Indicator),
            patch(
                "baton.cli.snapshot",
                side_effect=lambda **kwargs: (
                    events.append("snapshot") or self.snapshot_result
                ),
            ),
            patch(
                "baton.cli.handoff_archive",
                side_effect=lambda **kwargs: (
                    events.append("handoff") or self.handoff_result
                ),
            ),
            patch("baton.cli.infer_local_codex_version", return_value="0.147.0"),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            exit_code = main(["handoff", SESSION_ID, "continue working"])

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            events,
            ["start:Handing off Codex session...", "snapshot", "handoff", "stop"],
        )

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
            exit_code = main(["fetch", DEVBOX_ID, "--workspace", str(self.workspace)])

        self.assertEqual(exit_code, 0)
        self.assertEqual(fetch_mock.call_args.kwargs["devbox_id"], DEVBOX_ID)
        self.assertIsNone(fetch_mock.call_args.kwargs["receipt_path"])
        self.assertTrue(fetch_mock.call_args.kwargs["apply_changes"])
        receipts_mock.assert_not_called()
        picker_mock.assert_not_called()

    def test_fetch_runs_remote_work_inside_loading_indicator(self) -> None:
        events: list[str] = []

        class _Indicator:
            def __init__(self, message: str) -> None:
                self.message = message

            def __enter__(self) -> None:
                events.append(f"start:{self.message}")

            def __exit__(self, *args: object) -> None:
                events.append("stop")

        with (
            patch("baton.cli.TerminalSpinner", _Indicator),
            patch(
                "baton.cli.fetch_workspace",
                side_effect=lambda **kwargs: (
                    events.append("fetch") or self.fetch_result
                ),
            ),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            exit_code = main(["fetch", DEVBOX_ID])

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            events,
            ["start:Fetching remote changes...", "fetch", "stop"],
        )

    def test_successful_fetch_restores_session_without_launching_codex(self) -> None:
        fetch_result = SimpleNamespace(
            applied=True,
            remote_exit_code=0,
            fetch_root=self.workspace / ".baton/fetches" / DEVBOX_ID,
            to_dict=lambda: {"devbox_id": DEVBOX_ID, "applied": True},
        )
        stdout = io.StringIO()
        with (
            patch("baton.cli.fetch_workspace", return_value=fetch_result),
            patch(
                "baton.cli.resume_remote_session",
                return_value=self.resume_result,
            ) as resume_mock,
            contextlib.redirect_stdout(stdout),
        ):
            exit_code = main(
                [
                    "fetch",
                    DEVBOX_ID,
                    "--workspace",
                    str(self.workspace),
                    "--codex-home",
                    str(self.codex_home),
                ]
            )

        self.assertEqual(exit_code, 0)
        resume_mock.assert_called_once_with(
            devbox_id=DEVBOX_ID,
            workspace=self.workspace,
            codex_home=self.codex_home,
            receipt_path=None,
            fetch_root=fetch_result.fetch_root,
            launch=False,
        )
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["session_restore"]["status"], "restored")
        self.assertEqual(payload["session_restore"]["session_id"], SESSION_ID)

    def test_fetch_no_apply_reports_session_restore_skipped(self) -> None:
        stdout = io.StringIO()
        with (
            patch("baton.cli.fetch_workspace", return_value=self.fetch_result),
            patch("baton.cli.resume_remote_session") as resume_mock,
            patch("baton.cli.record_fetch_session_restore"),
            contextlib.redirect_stdout(stdout),
        ):
            exit_code = main(
                ["fetch", DEVBOX_ID, "--workspace", str(self.workspace), "--no-apply"]
            )

        self.assertEqual(exit_code, 0)
        resume_mock.assert_not_called()
        payload = json.loads(stdout.getvalue())
        self.assertEqual(
            payload["session_restore"],
            {"status": "skipped", "reason": "review_only"},
        )

    def test_failed_remote_fetch_reports_session_restore_skipped(self) -> None:
        fetch_result = SimpleNamespace(
            applied=False,
            remote_exit_code=2,
            fetch_root=self.workspace / ".baton/fetches" / DEVBOX_ID,
            to_dict=lambda: {
                "devbox_id": DEVBOX_ID,
                "applied": False,
                "remote_exit_code": 2,
            },
        )
        stdout = io.StringIO()
        with (
            patch("baton.cli.fetch_workspace", return_value=fetch_result),
            patch("baton.cli.resume_remote_session") as resume_mock,
            patch("baton.cli.record_fetch_session_restore"),
            contextlib.redirect_stdout(stdout),
        ):
            exit_code = main(["fetch", DEVBOX_ID, "--workspace", str(self.workspace)])

        self.assertEqual(exit_code, 0)
        resume_mock.assert_not_called()
        payload = json.loads(stdout.getvalue())
        self.assertEqual(
            payload["session_restore"],
            {"status": "skipped", "reason": "remote_exit_nonzero"},
        )

    def test_fetch_records_partial_success_when_session_history_is_unsafe(self) -> None:
        fetch_result = SimpleNamespace(
            applied=True,
            remote_exit_code=0,
            fetch_root=self.workspace / ".baton/fetches" / DEVBOX_ID,
            to_dict=lambda: {"devbox_id": DEVBOX_ID, "applied": True},
        )
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            patch("baton.cli.fetch_workspace", return_value=fetch_result),
            patch(
                "baton.cli.resume_remote_session",
                side_effect=ResumeError("remote rollout is unsafe"),
            ),
            patch("baton.cli.record_fetch_session_restore") as record_mock,
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            exit_code = main(["fetch", DEVBOX_ID, "--workspace", str(self.workspace)])

        self.assertEqual(exit_code, 1)
        record_mock.assert_called_once()
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["session_restore"]["status"], "failed")
        self.assertEqual(payload["session_restore"]["phase"], "restore")
        self.assertEqual(payload["session_restore"]["error"], "remote rollout is unsafe")
        self.assertIn("workspace changes were applied", stderr.getvalue())
        self.assertIn("session history was not restored", stderr.getvalue())

    def test_partial_fetch_does_not_claim_a_failed_record_was_persisted(self) -> None:
        fetch_result = SimpleNamespace(
            applied=True,
            remote_exit_code=0,
            fetch_root=self.workspace / ".baton/fetches" / DEVBOX_ID,
            to_dict=lambda: {"devbox_id": DEVBOX_ID, "applied": True},
        )
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            patch("baton.cli.fetch_workspace", return_value=fetch_result),
            patch(
                "baton.cli.resume_remote_session",
                side_effect=ResumeError("remote rollout is unsafe"),
            ),
            patch(
                "baton.cli.record_fetch_session_restore",
                side_effect=FetchError("disk full"),
            ),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            exit_code = main(["fetch", DEVBOX_ID, "--workspace", str(self.workspace)])

        self.assertEqual(exit_code, 1)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["session_restore"]["record_error"], "disk full")
        self.assertIn("could not update the fetch artifact", stderr.getvalue())
        self.assertNotIn("saved fetch artifact records", stderr.getvalue())

    def test_fetch_no_apply_keeps_review_only_mode(self) -> None:
        with (
            patch("baton.cli.fetch_workspace", return_value=self.fetch_result) as fetch_mock,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            exit_code = main(
                ["fetch", DEVBOX_ID, "--workspace", str(self.workspace), "--no-apply"]
            )

        self.assertEqual(exit_code, 0)
        self.assertFalse(fetch_mock.call_args.kwargs["apply_changes"])

    def test_fetch_without_id_selects_a_receipt_and_passes_its_exact_path(self) -> None:
        receipt = HandoffReceipt(
            path=self.workspace / ".baton/handoffs" / f"{DEVBOX_ID}.json",
            devbox_id=DEVBOX_ID,
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
        self.assertEqual(fetch_mock.call_args.kwargs["devbox_id"], DEVBOX_ID)
        self.assertEqual(fetch_mock.call_args.kwargs["receipt_path"], receipt.path)

    def test_fetch_picker_cancellation_does_not_contact_runloop(self) -> None:
        receipt = HandoffReceipt(
            path=self.workspace / ".baton/handoffs" / f"{DEVBOX_ID}.json",
            devbox_id=DEVBOX_ID,
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

    def test_explicit_resume_id_restores_and_opens_local_codex(self) -> None:
        with (
            patch(
                "baton.cli.resume_remote_session",
                return_value=self.resume_result,
            ) as resume_mock,
            patch("baton.cli.list_handoff_receipts") as receipts_mock,
            patch("baton.cli.choose_handoff") as picker_mock,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            exit_code = main(
                [
                    "resume",
                    DEVBOX_ID,
                    "--workspace",
                    str(self.workspace),
                    "--codex-home",
                    str(self.codex_home),
                ]
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(resume_mock.call_args.kwargs["devbox_id"], DEVBOX_ID)
        self.assertEqual(resume_mock.call_args.kwargs["workspace"], self.workspace)
        self.assertEqual(resume_mock.call_args.kwargs["codex_home"], self.codex_home)
        self.assertIsNone(resume_mock.call_args.kwargs["receipt_path"])
        self.assertTrue(resume_mock.call_args.kwargs["launch"])
        receipts_mock.assert_not_called()
        picker_mock.assert_not_called()

    def test_resume_picker_passes_receipt_and_no_launch(self) -> None:
        receipt = HandoffReceipt(
            path=self.workspace / ".baton/handoffs" / f"{DEVBOX_ID}.json",
            devbox_id=DEVBOX_ID,
            session_id=SESSION_ID,
            archive=self.root / "snapshot.tar.gz",
            workspace=self.workspace,
        )
        with (
            patch("baton.cli.list_handoff_receipts", return_value=(receipt,)) as receipts_mock,
            patch("baton.cli.choose_handoff", return_value=receipt) as picker_mock,
            patch(
                "baton.cli.resume_remote_session",
                return_value=self.resume_result,
            ) as resume_mock,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            exit_code = main(
                ["resume", "--workspace", str(self.workspace), "--no-launch"]
            )

        self.assertEqual(exit_code, 0)
        receipts_mock.assert_called_once_with(workspace=self.workspace)
        picker_mock.assert_called_once_with((receipt,))
        self.assertEqual(resume_mock.call_args.kwargs["devbox_id"], DEVBOX_ID)
        self.assertEqual(resume_mock.call_args.kwargs["receipt_path"], receipt.path)
        self.assertFalse(resume_mock.call_args.kwargs["launch"])

    def test_session_id_without_a_prompt_is_not_mistaken_for_picker_mode(self) -> None:
        with (
            patch("baton.cli.choose_session") as picker_mock,
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit) as error,
        ):
            main(["handoff", SESSION_ID])

        self.assertEqual(error.exception.code, 2)
        picker_mock.assert_not_called()

    def test_help_describes_only_the_runloop_flow(self) -> None:
        with self.assertRaises(SystemExit) as error, contextlib.redirect_stdout(io.StringIO()) as output:
            main(["handoff", "--help"])

        self.assertEqual(error.exception.code, 0)
        help_text = output.getvalue()
        self.assertIn("Runloop", help_text)
        self.assertIn("Devbox", help_text)
        self.assertIn("--blueprint-name", help_text)
        self.assertNotIn("Modal", help_text)
        self.assertNotIn("--app-name", help_text)
        self.assertNotIn("--image-name", help_text)

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
