from __future__ import annotations

import io
import json
import shlex
import shutil
import tarfile
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from baton.fetch import REMOTE_COMPLETION_MARKER
from baton.resume import (
    ResumeError,
    _GitState,
    _remote_git_state,
    resume_remote_session,
)

SESSION_ID = "019f5ef4-780a-7973-a1d2-c460461ced1f"
DEVBOX_ID = "devbox-resume-test"
ROLLOUT_PATH = f"sessions/2026/08/23/rollout-2026-08-23T13-14-26-{SESSION_ID}.jsonl"
BASELINE_ROLLOUT = (
    json.dumps({"payload": {"session_id": SESSION_ID}}).encode("utf-8") + b"\n"
)
REMOTE_RECORD = (
    json.dumps({"type": "response_item", "payload": {"text": "remote"}}).encode("utf-8")
    + b"\n"
)


class ResumeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        (self.workspace / "app.py").write_text("print('local')\n", encoding="utf-8")
        self.codex_home = self.root / ".codex"
        self.local_rollout = self.codex_home / ROLLOUT_PATH
        self.local_rollout.parent.mkdir(parents=True)
        self.local_rollout.write_bytes(BASELINE_ROLLOUT)
        (self.codex_home / "auth.json").write_text("local-auth\n", encoding="utf-8")
        (self.codex_home / "session_index.jsonl").write_text(
            json.dumps({"id": SESSION_ID, "thread_name": "local"})
            + "\n"
            + json.dumps({"id": "another-session", "thread_name": "keep"})
            + "\n",
            encoding="utf-8",
        )
        self.snapshot = self.root / "snapshot.tar.gz"
        _write_snapshot(self.snapshot)
        self.receipt = self.workspace / ".baton/handoffs" / f"{DEVBOX_ID}.json"
        self.receipt.parent.mkdir(parents=True)
        self.receipt.write_text(
            json.dumps(
                {
                    "format_version": 2,
                    "devbox_id": DEVBOX_ID,
                    "session_id": SESSION_ID,
                    "archive": str(self.snapshot.resolve()),
                    "workspace": str(self.workspace.resolve()),
                }
            ),
            encoding="utf-8",
        )
        self.fetch_root = self.workspace / ".baton/fetches" / DEVBOX_ID
        fetched_workspace = self.fetch_root / "workspace"
        fetched_workspace.mkdir(parents=True)
        (fetched_workspace / "app.py").write_text("print('local')\n", encoding="utf-8")
        (self.fetch_root / "result.json").write_text(
            json.dumps(
                {
                    "devbox_id": DEVBOX_ID,
                    "session_id": SESSION_ID,
                    "archive": str(self.snapshot.resolve()),
                    "remote_workspace": str(fetched_workspace.resolve()),
                    "applied": True,
                    "apply_status": "applied",
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_completed_remote_session_replaces_rollout_and_saves_local_backup(
        self,
    ) -> None:
        remote_rollout = BASELINE_ROLLOUT + REMOTE_RECORD
        runloop = _FakeRunloop({ROLLOUT_PATH: remote_rollout})

        result = resume_remote_session(
            devbox_id=DEVBOX_ID,
            workspace=self.workspace,
            codex_home=self.codex_home,
            receipt_path=self.receipt,
            runloop_client=runloop,
        )

        self.assertEqual(self.local_rollout.read_bytes(), remote_rollout)
        self.assertEqual(result.session_id, SESSION_ID)
        self.assertEqual(result.rollout_path, self.local_rollout.resolve())
        backup_path = result.backup_path
        self.assertIsNotNone(backup_path)
        assert backup_path is not None
        self.assertEqual(backup_path.read_bytes(), BASELINE_ROLLOUT)

    def test_restore_uses_existing_devbox_without_credentials_or_lifecycle_changes(
        self,
    ) -> None:
        runloop = _FakeRunloop({ROLLOUT_PATH: BASELINE_ROLLOUT + REMOTE_RECORD})

        resume_remote_session(
            devbox_id=DEVBOX_ID,
            workspace=self.workspace,
            codex_home=self.codex_home,
            receipt_path=self.receipt,
            runloop_client=runloop,
        )

        self.assertEqual(runloop.devboxes.retrieve_calls, [DEVBOX_ID])
        self.assertEqual(runloop.devboxes.create_calls, [])
        self.assertEqual(runloop.devboxes.shutdown_calls, [])
        self.assertFalse(runloop.devbox.terminated)
        self.assertFalse(runloop.devbox.detached)
        self.assertNotIn(
            "auth.json",
            " ".join(part for command in runloop.devbox.commands for part in command),
        )

    def test_remote_auth_member_is_rejected_without_changing_local_auth(self) -> None:
        runloop = _FakeRunloop(
            {
                ROLLOUT_PATH: BASELINE_ROLLOUT + REMOTE_RECORD,
                "auth.json": b"remote-auth\n",
            }
        )

        with self.assertRaisesRegex(ResumeError, "auth|unexpected|credential|member"):
            resume_remote_session(
                devbox_id=DEVBOX_ID,
                workspace=self.workspace,
                codex_home=self.codex_home,
                receipt_path=self.receipt,
                runloop_client=runloop,
            )

        self.assertEqual((self.codex_home / "auth.json").read_text(), "local-auth\n")

    def test_traversal_member_is_rejected_without_changing_local_rollout(self) -> None:
        runloop = _FakeRunloop(
            {
                ROLLOUT_PATH: BASELINE_ROLLOUT + REMOTE_RECORD,
                "../outside.jsonl": b"escape\n",
            }
        )

        with self.assertRaisesRegex(ResumeError, "unsafe|member|path"):
            resume_remote_session(
                devbox_id=DEVBOX_ID,
                workspace=self.workspace,
                codex_home=self.codex_home,
                receipt_path=self.receipt,
                runloop_client=runloop,
            )

        self.assertEqual(self.local_rollout.read_bytes(), BASELINE_ROLLOUT)

    def test_symlinked_rollout_member_is_rejected_without_changing_local_rollout(
        self,
    ) -> None:
        runloop = _FakeRunloop({}, archive_bytes=_tar_symlink(ROLLOUT_PATH, "auth.json"))

        with self.assertRaisesRegex(ResumeError, "regular file|member|link"):
            resume_remote_session(
                devbox_id=DEVBOX_ID,
                workspace=self.workspace,
                codex_home=self.codex_home,
                receipt_path=self.receipt,
                runloop_client=runloop,
            )

        self.assertEqual(self.local_rollout.read_bytes(), BASELINE_ROLLOUT)

    def test_remote_symlink_is_rejected_before_session_download(self) -> None:
        def command_result(command: tuple[str, ...]) -> tuple[int, str, str] | None:
            if command[:3] == ("test", "!", "-L"):
                return 1, "", ""
            return None

        runloop = _FakeRunloop(
            {ROLLOUT_PATH: BASELINE_ROLLOUT + REMOTE_RECORD},
            command_result=command_result,
        )

        with self.assertRaisesRegex(ResumeError, "symlink|credentials"):
            resume_remote_session(
                devbox_id=DEVBOX_ID,
                workspace=self.workspace,
                codex_home=self.codex_home,
                receipt_path=self.receipt,
                runloop_client=runloop,
            )

        self.assertEqual(runloop.devbox.filesystem.copy_calls, [])
        self.assertEqual(self.local_rollout.read_bytes(), BASELINE_ROLLOUT)

    def test_remote_rollout_with_symlinked_ancestor_is_rejected_before_download(
        self,
    ) -> None:
        remote_rollout_path = f"/baton/.codex/{ROLLOUT_PATH}"

        def command_result(command: tuple[str, ...]) -> tuple[int, str, str] | None:
            if command == ("realpath", "-e", "--", remote_rollout_path):
                return 0, "/baton/.codex/auth.json\n", ""
            return None

        runloop = _FakeRunloop(
            {ROLLOUT_PATH: BASELINE_ROLLOUT + REMOTE_RECORD},
            command_result=command_result,
        )

        with self.assertRaisesRegex(ResumeError, "symlink|outside the Codex home"):
            resume_remote_session(
                devbox_id=DEVBOX_ID,
                workspace=self.workspace,
                codex_home=self.codex_home,
                receipt_path=self.receipt,
                runloop_client=runloop,
            )

        self.assertEqual(runloop.devbox.filesystem.copy_calls, [])
        self.assertEqual(self.local_rollout.read_bytes(), BASELINE_ROLLOUT)
        self.assertFalse(
            any(command[:3] == ("stat", "-c", "%s") for command in runloop.devbox.commands)
        )

    def test_remote_index_with_symlinked_ancestor_is_rejected_before_filtering(
        self,
    ) -> None:
        remote_index_path = "/baton/.codex/session_index.jsonl"

        def command_result(command: tuple[str, ...]) -> tuple[int, str, str] | None:
            if command == ("realpath", "-e", "--", remote_index_path):
                return 0, "/baton/.codex/auth.json\n", ""
            return None

        runloop = _FakeRunloop(
            {
                ROLLOUT_PATH: BASELINE_ROLLOUT + REMOTE_RECORD,
                "session_index.jsonl": b"{}\n",
            },
            command_result=command_result,
        )

        with self.assertRaisesRegex(ResumeError, "symlink|outside the Codex home"):
            resume_remote_session(
                devbox_id=DEVBOX_ID,
                workspace=self.workspace,
                codex_home=self.codex_home,
                receipt_path=self.receipt,
                runloop_client=runloop,
            )

        self.assertEqual(runloop.devbox.filesystem.copy_calls, [])
        self.assertEqual(self.local_rollout.read_bytes(), BASELINE_ROLLOUT)
        self.assertFalse(
            any(command[:2] == ("node", "-e") for command in runloop.devbox.commands)
        )

    def test_duplicate_rollout_member_is_rejected_without_changing_local_rollout(
        self,
    ) -> None:
        runloop = _FakeRunloop(
            {},
            archive_bytes=_tar_duplicate(
                ROLLOUT_PATH,
                BASELINE_ROLLOUT + REMOTE_RECORD,
            ),
        )

        with self.assertRaisesRegex(ResumeError, "duplicate member"):
            resume_remote_session(
                devbox_id=DEVBOX_ID,
                workspace=self.workspace,
                codex_home=self.codex_home,
                receipt_path=self.receipt,
                runloop_client=runloop,
            )

        self.assertEqual(self.local_rollout.read_bytes(), BASELINE_ROLLOUT)

    def test_hardlinked_rollout_member_is_rejected_without_changing_local_rollout(
        self,
    ) -> None:
        runloop = _FakeRunloop({}, archive_bytes=_tar_hardlink(ROLLOUT_PATH, "auth.json"))

        with self.assertRaisesRegex(ResumeError, "regular file"):
            resume_remote_session(
                devbox_id=DEVBOX_ID,
                workspace=self.workspace,
                codex_home=self.codex_home,
                receipt_path=self.receipt,
                runloop_client=runloop,
            )

        self.assertEqual(self.local_rollout.read_bytes(), BASELINE_ROLLOUT)

    def test_device_rollout_member_is_rejected_without_changing_local_rollout(
        self,
    ) -> None:
        runloop = _FakeRunloop({}, archive_bytes=_tar_device(ROLLOUT_PATH))

        with self.assertRaisesRegex(ResumeError, "regular file"):
            resume_remote_session(
                devbox_id=DEVBOX_ID,
                workspace=self.workspace,
                codex_home=self.codex_home,
                receipt_path=self.receipt,
                runloop_client=runloop,
            )

        self.assertEqual(self.local_rollout.read_bytes(), BASELINE_ROLLOUT)

    def test_archive_with_too_many_members_is_rejected_without_changing_local_rollout(
        self,
    ) -> None:
        runloop = _FakeRunloop(
            {
                ROLLOUT_PATH: BASELINE_ROLLOUT + REMOTE_RECORD,
                "session_index.jsonl": b"",
                "unexpected.jsonl": b"{}\n",
            }
        )

        with self.assertRaisesRegex(ResumeError, "too many members"):
            resume_remote_session(
                devbox_id=DEVBOX_ID,
                workspace=self.workspace,
                codex_home=self.codex_home,
                receipt_path=self.receipt,
                runloop_client=runloop,
            )

        self.assertEqual(self.local_rollout.read_bytes(), BASELINE_ROLLOUT)

    def test_truncated_remote_jsonl_is_rejected_without_changing_local_rollout(
        self,
    ) -> None:
        truncated_rollout = BASELINE_ROLLOUT + b'{"type":"response_item"}'
        runloop = _FakeRunloop({ROLLOUT_PATH: truncated_rollout})

        with self.assertRaisesRegex(ResumeError, "truncated final JSONL record"):
            resume_remote_session(
                devbox_id=DEVBOX_ID,
                workspace=self.workspace,
                codex_home=self.codex_home,
                receipt_path=self.receipt,
                runloop_client=runloop,
            )

        self.assertEqual(self.local_rollout.read_bytes(), BASELINE_ROLLOUT)

    def test_receipt_for_another_session_is_rejected_before_runloop_lookup(self) -> None:
        payload = json.loads(self.receipt.read_text(encoding="utf-8"))
        payload["session_id"] = "11111111-1111-4111-8111-111111111111"
        self.receipt.write_text(json.dumps(payload), encoding="utf-8")
        runloop = _FakeRunloop({ROLLOUT_PATH: BASELINE_ROLLOUT + REMOTE_RECORD})

        with self.assertRaisesRegex(ResumeError, "receipt|session|snapshot"):
            resume_remote_session(
                devbox_id=DEVBOX_ID,
                workspace=self.workspace,
                codex_home=self.codex_home,
                receipt_path=self.receipt,
                runloop_client=runloop,
            )

        self.assertEqual(runloop.devboxes.retrieve_calls, [])

    def test_incomplete_handoff_is_rejected_before_session_download(self) -> None:
        runloop = _FakeRunloop(
            {ROLLOUT_PATH: BASELINE_ROLLOUT + REMOTE_RECORD}, marker=None
        )

        with self.assertRaisesRegex(ResumeError, "still working|completion|complete"):
            resume_remote_session(
                devbox_id=DEVBOX_ID,
                workspace=self.workspace,
                codex_home=self.codex_home,
                receipt_path=self.receipt,
                runloop_client=runloop,
            )

        self.assertEqual(runloop.devbox.filesystem.copy_calls, [])

    def test_failed_remote_handoff_is_rejected_before_session_download(self) -> None:
        runloop = _FakeRunloop(
            {ROLLOUT_PATH: BASELINE_ROLLOUT + REMOTE_RECORD},
            marker='{"exit_code": 2}\n',
        )

        with self.assertRaisesRegex(ResumeError, "exit|failed|successful|status"):
            resume_remote_session(
                devbox_id=DEVBOX_ID,
                workspace=self.workspace,
                codex_home=self.codex_home,
                receipt_path=self.receipt,
                runloop_client=runloop,
            )

        self.assertEqual(runloop.devbox.filesystem.copy_calls, [])

    def test_remote_git_head_mismatch_is_rejected_before_session_download(self) -> None:
        _write_snapshot(
            self.snapshot,
            repository={
                "present": True,
                "head": "local-head",
                "branch": "main",
                "bundle_archive_path": "git/repository.bundle",
            },
        )
        runloop = _FakeRunloop(
            {ROLLOUT_PATH: BASELINE_ROLLOUT + REMOTE_RECORD},
            marker=json.dumps(
                {
                    "exit_code": 0,
                    "git_state": {
                        "head": "remote-head",
                        "branch": "main",
                        "status": "",
                        "index": "same-index",
                        "index_flags": "same-index-flags",
                        "refs": "same-refs",
                    },
                }
            ),
        )

        with (
            patch(
                "baton.resume._remote_git_state",
                return_value=_GitState(
                    head="remote-head",
                    branch="main",
                    status="",
                    index="same-index",
                    index_flags="same-index-flags",
                    refs="same-refs",
                ),
            ),
            patch(
                "baton.resume._local_git_state",
                return_value=_GitState(
                    head="local-head",
                    branch="main",
                    status="",
                    index="same-index",
                    index_flags="same-index-flags",
                    refs="same-refs",
                ),
            ),
            self.assertRaisesRegex(ResumeError, "Git state differs|different repository"),
        ):
            resume_remote_session(
                devbox_id=DEVBOX_ID,
                workspace=self.workspace,
                codex_home=self.codex_home,
                receipt_path=self.receipt,
                runloop_client=runloop,
            )

        self.assertEqual(runloop.devbox.filesystem.copy_calls, [])
        self.assertEqual(self.local_rollout.read_bytes(), BASELINE_ROLLOUT)

    def test_remote_git_index_mismatch_is_rejected_before_session_download(self) -> None:
        _write_snapshot(
            self.snapshot,
            repository={
                "present": True,
                "head": "same-head",
                "branch": "main",
                "bundle_archive_path": "git/repository.bundle",
            },
        )
        runloop = _FakeRunloop(
            {ROLLOUT_PATH: BASELINE_ROLLOUT + REMOTE_RECORD},
            marker=json.dumps(
                {
                    "exit_code": 0,
                    "git_state": {
                        "head": "same-head",
                        "branch": "main",
                        "status": "",
                        "index": "remote-index",
                        "index_flags": "same-index-flags",
                        "refs": "same-refs",
                    },
                }
            ),
        )

        with (
            patch(
                "baton.resume._remote_git_state",
                return_value=_GitState(
                    head="same-head",
                    branch="main",
                    status="",
                    index="remote-index",
                    index_flags="same-index-flags",
                    refs="same-refs",
                ),
            ),
            patch(
                "baton.resume._local_git_state",
                return_value=_GitState(
                    head="same-head",
                    branch="main",
                    status="",
                    index="local-index",
                    index_flags="same-index-flags",
                    refs="same-refs",
                ),
            ),
            self.assertRaisesRegex(ResumeError, "Git state differs|different repository"),
        ):
            resume_remote_session(
                devbox_id=DEVBOX_ID,
                workspace=self.workspace,
                codex_home=self.codex_home,
                receipt_path=self.receipt,
                runloop_client=runloop,
            )

        self.assertEqual(runloop.devbox.filesystem.copy_calls, [])
        self.assertEqual(self.local_rollout.read_bytes(), BASELINE_ROLLOUT)

    def test_remote_git_refs_changed_from_marker_are_rejected_before_download(
        self,
    ) -> None:
        _write_snapshot(
            self.snapshot,
            repository={
                "present": True,
                "head": "same-head",
                "branch": "main",
                "bundle_archive_path": "git/repository.bundle",
            },
        )
        marker = json.dumps(
            {
                "exit_code": 0,
                "git_state": {
                    "head": "same-head",
                    "branch": "main",
                    "status": "",
                    "index": "same-index",
                    "index_flags": "same-index-flags",
                    "refs": "baseline-refs",
                },
            }
        )
        runloop = _FakeRunloop(
            {ROLLOUT_PATH: BASELINE_ROLLOUT + REMOTE_RECORD},
            marker=marker,
        )
        remote_state = _GitState(
            head="same-head",
            branch="main",
            status="",
            index="same-index",
            index_flags="same-index-flags",
            refs="changed-refs",
        )
        local_state = _GitState(
            head="same-head",
            branch="main",
            status="",
            index="same-index",
            index_flags="same-index-flags",
            refs="baseline-refs",
        )

        with (
            patch("baton.resume._remote_git_state", return_value=remote_state),
            patch("baton.resume._local_git_state", return_value=local_state),
            self.assertRaisesRegex(ResumeError, "Git state changed after handoff"),
        ):
            resume_remote_session(
                devbox_id=DEVBOX_ID,
                workspace=self.workspace,
                codex_home=self.codex_home,
                receipt_path=self.receipt,
                runloop_client=runloop,
            )

        self.assertEqual(runloop.devbox.filesystem.copy_calls, [])
        self.assertEqual(self.local_rollout.read_bytes(), BASELINE_ROLLOUT)

    def test_remote_git_inspection_runs_as_the_runtime_workspace_owner(self) -> None:
        prefix = (
            "runuser",
            "--user",
            "baton-agent",
            "--",
            "git",
            "-C",
            "/baton/workspace",
        )
        outputs = {
            ("rev-parse", "--is-inside-work-tree"): "true\n",
            ("rev-parse", "--show-toplevel"): "/baton/workspace\n",
            ("rev-parse", "HEAD"): "same-head\n",
            ("branch", "--show-current"): "main\n",
            (
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=all",
                "--",
                ".",
                ":(exclude).baton",
            ): " M app.py\0",
            ("ls-files", "--stage", "-z", "--"): "same-index",
            ("ls-files", "-v", "-z", "--"): "same-index-flags",
            (
                "for-each-ref",
                "--format=%(refname)%00%(objectname)%00%(symref)%00",
            ): "same-refs",
        }

        def command_result(command: tuple[str, ...]) -> tuple[int, str, str] | None:
            self.assertEqual(command[: len(prefix)], prefix)
            arguments = command[len(prefix) :]
            return 0, outputs[arguments], ""

        runloop = _FakeRunloop({}, command_result=command_result)

        state = _remote_git_state(runloop.devbox)

        self.assertEqual(
            state,
            _GitState(
                head="same-head",
                branch="main",
                status=" M app.py\0",
                index="same-index",
                index_flags="same-index-flags",
                refs="same-refs",
            ),
        )
        self.assertEqual(len(runloop.devbox.commands), len(outputs))

    def test_matching_fetched_worktree_edit_can_restore_the_remote_rollout(self) -> None:
        _write_snapshot(
            self.snapshot,
            repository={
                "present": True,
                "head": "same-head",
                "branch": "main",
                "bundle_archive_path": "git/repository.bundle",
            },
        )
        marker = json.dumps(
            {
                "exit_code": 0,
                "git_state": {
                    "head": "same-head",
                    "branch": "main",
                    "status": "",
                    "index": "same-index",
                    "index_flags": "same-index-flags",
                    "refs": "same-refs",
                },
            }
        )
        runloop = _FakeRunloop(
            {ROLLOUT_PATH: BASELINE_ROLLOUT + REMOTE_RECORD},
            marker=marker,
        )
        post_fetch_state = _GitState(
            head="same-head",
            branch="main",
            status=" M app.py\0",
            index="same-index",
            index_flags="same-index-flags",
            refs="same-refs",
        )

        with (
            patch("baton.resume._remote_git_state", return_value=post_fetch_state),
            patch("baton.resume._local_git_state", return_value=post_fetch_state),
        ):
            result = resume_remote_session(
                devbox_id=DEVBOX_ID,
                workspace=self.workspace,
                codex_home=self.codex_home,
                receipt_path=self.receipt,
                runloop_client=runloop,
            )

        self.assertEqual(result.session_id, SESSION_ID)
        self.assertEqual(self.local_rollout.read_bytes(), BASELINE_ROLLOUT + REMOTE_RECORD)

    def test_git_snapshot_without_marker_baseline_is_rejected_before_download(self) -> None:
        _write_snapshot(
            self.snapshot,
            repository={
                "present": True,
                "head": "same-head",
                "branch": "main",
                "bundle_archive_path": "git/repository.bundle",
            },
        )
        runloop = _FakeRunloop(
            {ROLLOUT_PATH: BASELINE_ROLLOUT + REMOTE_RECORD},
            marker=json.dumps({"exit_code": 0}),
        )
        matching_state = _GitState(
            head="same-head",
            branch="main",
            status="",
            index="same-index",
            index_flags="same-index-flags",
            refs="same-refs",
        )

        with (
            patch("baton.resume._remote_git_state", return_value=matching_state),
            patch("baton.resume._local_git_state", return_value=matching_state),
            self.assertRaisesRegex(ResumeError, "Git baseline|git_state"),
        ):
            resume_remote_session(
                devbox_id=DEVBOX_ID,
                workspace=self.workspace,
                codex_home=self.codex_home,
                receipt_path=self.receipt,
                runloop_client=runloop,
            )

        self.assertEqual(runloop.devbox.filesystem.copy_calls, [])

    def test_git_snapshot_with_null_marker_baseline_is_rejected_before_download(self) -> None:
        _write_snapshot(
            self.snapshot,
            repository={
                "present": True,
                "head": "same-head",
                "branch": "main",
                "bundle_archive_path": "git/repository.bundle",
            },
        )
        runloop = _FakeRunloop(
            {ROLLOUT_PATH: BASELINE_ROLLOUT + REMOTE_RECORD},
            marker=json.dumps({"exit_code": 0, "git_state": None}),
        )
        matching_state = _GitState(
            head="same-head",
            branch="main",
            status="",
            index="same-index",
            index_flags="same-index-flags",
            refs="same-refs",
        )

        with (
            patch("baton.resume._remote_git_state", return_value=matching_state),
            patch("baton.resume._local_git_state", return_value=matching_state),
            self.assertRaisesRegex(ResumeError, "Git baseline|git_state"),
        ):
            resume_remote_session(
                devbox_id=DEVBOX_ID,
                workspace=self.workspace,
                codex_home=self.codex_home,
                receipt_path=self.receipt,
                runloop_client=runloop,
            )

        self.assertEqual(runloop.devbox.filesystem.copy_calls, [])

    def test_oversized_remote_archive_is_rejected_before_download(self) -> None:
        def command_result(command: tuple[str, ...]) -> tuple[int, str, str] | None:
            if command[:3] != ("stat", "-c", "%s"):
                return None
            remote_path = command[-1]
            if remote_path.startswith("/tmp/baton-session-") and remote_path.endswith(
                ".tar.gz"
            ):
                return (0, "1025\n", "")
            return (0, "1\n", "")

        runloop = _FakeRunloop(
            {ROLLOUT_PATH: BASELINE_ROLLOUT + REMOTE_RECORD},
            command_result=command_result,
        )

        with (
            patch("baton.resume.MAX_SESSION_ARCHIVE_BYTES", 1024),
            patch("baton.resume._remote_git_state", return_value=None),
            patch("baton.resume._local_git_state", return_value=None),
            self.assertRaisesRegex(ResumeError, "archive.*safety limit|archive.*too large"),
        ):
            resume_remote_session(
                devbox_id=DEVBOX_ID,
                workspace=self.workspace,
                codex_home=self.codex_home,
                receipt_path=self.receipt,
                runloop_client=runloop,
            )

        self.assertTrue(
            any(
                command[:3] == ("stat", "-c", "%s")
                and command[-1].startswith("/tmp/baton-session-")
                for command in runloop.devbox.commands
            )
        )
        self.assertEqual(runloop.devbox.filesystem.copy_calls, [])

    def test_missing_applied_fetch_artifact_is_rejected_before_runloop_lookup(
        self,
    ) -> None:
        shutil.rmtree(self.fetch_root)
        runloop = _FakeRunloop({ROLLOUT_PATH: BASELINE_ROLLOUT + REMOTE_RECORD})

        with self.assertRaisesRegex(ResumeError, "fetch|workspace|applied"):
            resume_remote_session(
                devbox_id=DEVBOX_ID,
                workspace=self.workspace,
                codex_home=self.codex_home,
                receipt_path=self.receipt,
                runloop_client=runloop,
            )

        self.assertEqual(runloop.devboxes.retrieve_calls, [])

    def test_non_applied_fetch_artifact_is_rejected_before_runloop_lookup(self) -> None:
        result_path = self.fetch_root / "result.json"
        result = json.loads(result_path.read_text(encoding="utf-8"))
        result.update({"applied": False, "apply_status": "review_only"})
        result_path.write_text(json.dumps(result), encoding="utf-8")
        runloop = _FakeRunloop({ROLLOUT_PATH: BASELINE_ROLLOUT + REMOTE_RECORD})

        with self.assertRaisesRegex(ResumeError, "fetch|workspace|applied"):
            resume_remote_session(
                devbox_id=DEVBOX_ID,
                workspace=self.workspace,
                codex_home=self.codex_home,
                receipt_path=self.receipt,
                runloop_client=runloop,
            )

        self.assertEqual(runloop.devboxes.retrieve_calls, [])

    def test_workspace_drift_after_fetch_is_rejected_before_runloop_lookup(self) -> None:
        (self.workspace / "app.py").write_text("print('drift')\n", encoding="utf-8")
        runloop = _FakeRunloop({ROLLOUT_PATH: BASELINE_ROLLOUT + REMOTE_RECORD})

        with self.assertRaisesRegex(ResumeError, "fetch|workspace|changed|drift|match"):
            resume_remote_session(
                devbox_id=DEVBOX_ID,
                workspace=self.workspace,
                codex_home=self.codex_home,
                receipt_path=self.receipt,
                runloop_client=runloop,
            )

        self.assertEqual(runloop.devboxes.retrieve_calls, [])

    def test_remote_rollout_that_does_not_extend_snapshot_is_rejected_without_mutation(
        self,
    ) -> None:
        divergent = (
            json.dumps(
                {"payload": {"session_id": SESSION_ID}, "changed": True}
            ).encode()
            + b"\n"
        )
        runloop = _FakeRunloop({ROLLOUT_PATH: divergent})

        with self.assertRaisesRegex(ResumeError, "extend|baseline|prefix|diverge"):
            resume_remote_session(
                devbox_id=DEVBOX_ID,
                workspace=self.workspace,
                codex_home=self.codex_home,
                receipt_path=self.receipt,
                runloop_client=runloop,
            )

        self.assertEqual(self.local_rollout.read_bytes(), BASELINE_ROLLOUT)

    def test_local_rollout_that_advanced_after_handoff_is_not_overwritten(self) -> None:
        local_record = (
            json.dumps({"type": "response_item", "payload": {"text": "local"}}).encode()
            + b"\n"
        )
        local_rollout = BASELINE_ROLLOUT + local_record
        self.local_rollout.write_bytes(local_rollout)
        runloop = _FakeRunloop({ROLLOUT_PATH: BASELINE_ROLLOUT + REMOTE_RECORD})

        with self.assertRaisesRegex(
            ResumeError, "local|changed|advance|baseline|conflict"
        ):
            resume_remote_session(
                devbox_id=DEVBOX_ID,
                workspace=self.workspace,
                codex_home=self.codex_home,
                receipt_path=self.receipt,
                runloop_client=runloop,
            )

        self.assertEqual(self.local_rollout.read_bytes(), local_rollout)

    def test_missing_remote_session_index_preserves_local_index(self) -> None:
        local_index = (self.codex_home / "session_index.jsonl").read_bytes()
        runloop = _FakeRunloop({ROLLOUT_PATH: BASELINE_ROLLOUT + REMOTE_RECORD})

        resume_remote_session(
            devbox_id=DEVBOX_ID,
            workspace=self.workspace,
            codex_home=self.codex_home,
            receipt_path=self.receipt,
            runloop_client=runloop,
        )

        self.assertEqual(
            (self.codex_home / "session_index.jsonl").read_bytes(), local_index
        )

    def test_idempotent_restore_accepts_rollout_that_already_matches_remote(
        self,
    ) -> None:
        remote_rollout = BASELINE_ROLLOUT + REMOTE_RECORD
        self.local_rollout.write_bytes(remote_rollout)
        runloop = _FakeRunloop({ROLLOUT_PATH: remote_rollout})

        result = resume_remote_session(
            devbox_id=DEVBOX_ID,
            workspace=self.workspace,
            codex_home=self.codex_home,
            receipt_path=self.receipt,
            runloop_client=runloop,
        )

        self.assertEqual(result.session_id, SESSION_ID)
        self.assertEqual(self.local_rollout.read_bytes(), remote_rollout)

    def test_session_index_merges_only_the_selected_remote_record(self) -> None:
        remote_index = (
            json.dumps({"id": "remote-unrelated", "thread_name": "drop"})
            + "\n"
            + json.dumps({"id": SESSION_ID, "thread_name": "remote"})
            + "\n"
        ).encode("utf-8")
        runloop = _FakeRunloop(
            {
                ROLLOUT_PATH: BASELINE_ROLLOUT + REMOTE_RECORD,
                "session_index.jsonl": remote_index,
            }
        )

        resume_remote_session(
            devbox_id=DEVBOX_ID,
            workspace=self.workspace,
            codex_home=self.codex_home,
            receipt_path=self.receipt,
            runloop_client=runloop,
        )

        records = [
            json.loads(line)
            for line in (self.codex_home / "session_index.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        self.assertIn({"id": "another-session", "thread_name": "keep"}, records)
        self.assertIn({"id": SESSION_ID, "thread_name": "remote"}, records)
        self.assertNotIn({"id": SESSION_ID, "thread_name": "local"}, records)
        self.assertNotIn({"id": "remote-unrelated", "thread_name": "drop"}, records)

    def test_remote_archive_contains_only_the_selected_session_index_record(self) -> None:
        unrelated_record = {
            "id": "remote-unrelated",
            "thread_name": "private-unrelated-session",
        }
        selected_record = {"id": SESSION_ID, "thread_name": "remote"}
        remote_index = (
            json.dumps(unrelated_record) + "\n" + json.dumps(selected_record) + "\n"
        ).encode("utf-8")
        runloop: _FakeRunloop

        def command_result(command: tuple[str, ...]) -> tuple[int, str, str] | None:
            if command[:2] == ("node", "-e"):
                self.assertEqual(
                    command[-3],
                    "/baton/.codex/session_index.jsonl",
                )
                self.assertRegex(
                    command[-2],
                    r"^/tmp/baton-session-index-[0-9a-f]+/session_index\.jsonl$",
                )
                self.assertEqual(command[-1], SESSION_ID)
                filtered_index = (json.dumps(selected_record) + "\n").encode("utf-8")
                runloop.devbox.filesystem.archive_bytes = _tar_bytes(
                    {
                        ROLLOUT_PATH: BASELINE_ROLLOUT + REMOTE_RECORD,
                        "session_index.jsonl": filtered_index,
                    }
                )
            return None

        runloop = _FakeRunloop(
            {
                ROLLOUT_PATH: BASELINE_ROLLOUT + REMOTE_RECORD,
                "session_index.jsonl": remote_index,
            },
            command_result=command_result,
        )

        resume_remote_session(
            devbox_id=DEVBOX_ID,
            workspace=self.workspace,
            codex_home=self.codex_home,
            receipt_path=self.receipt,
            runloop_client=runloop,
        )

        self.assertEqual(len(runloop.devbox.filesystem.copied_archives), 1)
        copied_archive = runloop.devbox.filesystem.copied_archives[0]
        with tarfile.open(fileobj=io.BytesIO(copied_archive), mode="r:gz") as archive:
            index_source = archive.extractfile("session_index.jsonl")
            self.assertIsNotNone(index_source)
            assert index_source is not None
            copied_index = index_source.read()
        self.assertEqual(copied_index, (json.dumps(selected_record) + "\n").encode())
        self.assertNotIn(b"private-unrelated-session", copied_index)
        self.assertTrue(
            any(
                command[:2] == ("rmdir", "--")
                and command[-1].startswith("/tmp/baton-session-index-")
                for command in runloop.devbox.commands
            )
        )

    def test_index_write_failure_rolls_back_replaced_rollout(self) -> None:
        remote_rollout = BASELINE_ROLLOUT + REMOTE_RECORD
        remote_index = (
            json.dumps({"id": SESSION_ID, "thread_name": "remote"}) + "\n"
        ).encode("utf-8")
        runloop = _FakeRunloop(
            {
                ROLLOUT_PATH: remote_rollout,
                "session_index.jsonl": remote_index,
            }
        )
        local_index_path = (self.codex_home / "session_index.jsonl").resolve()
        original_index = local_index_path.read_bytes()
        from baton.resume import _write_bytes_atomically

        def fail_index_write(path: Path, contents: bytes) -> None:
            if path == local_index_path:
                raise OSError("simulated index write failure")
            _write_bytes_atomically(path, contents)

        with (
            patch("baton.resume._write_bytes_atomically", side_effect=fail_index_write),
            self.assertRaisesRegex(ResumeError, "restore local session index"),
        ):
            resume_remote_session(
                devbox_id=DEVBOX_ID,
                workspace=self.workspace,
                codex_home=self.codex_home,
                receipt_path=self.receipt,
                runloop_client=runloop,
            )

        self.assertEqual(self.local_rollout.read_bytes(), BASELINE_ROLLOUT)
        self.assertEqual(local_index_path.read_bytes(), original_index)

    def test_launch_uses_local_codex_home_workspace_and_safe_argv(self) -> None:
        runloop = _FakeRunloop({ROLLOUT_PATH: BASELINE_ROLLOUT + REMOTE_RECORD})

        with patch("baton.resume.subprocess.run") as run:
            run.return_value.returncode = 0
            result = resume_remote_session(
                devbox_id=DEVBOX_ID,
                workspace=self.workspace,
                codex_home=self.codex_home,
                receipt_path=self.receipt,
                launch=True,
                runloop_client=runloop,
            )

        positional, keywords = run.call_args
        self.assertEqual(positional, (["codex", "resume", SESSION_ID],))
        self.assertEqual(keywords["cwd"], self.workspace.resolve())
        self.assertEqual(keywords["env"]["CODEX_HOME"], str(self.codex_home.resolve()))
        self.assertFalse(keywords["check"])
        self.assertTrue(result.launched)
        self.assertEqual(result.local_exit_code, 0)


class _Stream:
    def __init__(self, contents: str = "") -> None:
        self.contents = contents

    def read(self) -> str:
        return self.contents


class _Process:
    def __init__(
        self,
        *,
        stdout: str = "",
        stderr: str = "",
        returncode: int = 0,
    ) -> None:
        self.stdout = _Stream(stdout)
        self.stderr = _Stream(stderr)
        self.returncode = returncode

    def wait(self) -> int:
        return self.returncode


class _Filesystem:
    def __init__(self, marker: str | None, archive_bytes: bytes) -> None:
        self.marker = marker
        self.archive_bytes = archive_bytes
        self.read_calls: list[str] = []
        self.copy_calls: list[tuple[str, Path]] = []
        self.copied_archives: list[bytes] = []

    def read_text(self, remote_path: str) -> str:
        self.read_calls.append(remote_path)
        if remote_path == REMOTE_COMPLETION_MARKER and self.marker is not None:
            return self.marker
        raise FileNotFoundError(remote_path)

    def copy_to_local(self, remote_path: str, local_path: Path) -> None:
        destination = Path(local_path)
        self.copy_calls.append((remote_path, destination))
        self.copied_archives.append(self.archive_bytes)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(self.archive_bytes)


class _RemoteDevboxState:
    def __init__(
        self,
        members: dict[str, bytes],
        marker: str | None,
        archive_bytes: bytes | None = None,
        command_result: Callable[
            [tuple[str, ...]], tuple[int, str, str] | None
        ]
        | None = None,
    ) -> None:
        self.filesystem = _Filesystem(marker, archive_bytes or _tar_bytes(members))
        self.command_result = command_result
        self.commands: list[tuple[str, ...]] = []
        self.terminated = False
        self.detached = False

    def exec(self, *command: str, **kwargs: object) -> _Process:
        self.commands.append(command)
        if self.command_result is not None:
            result = self.command_result(command)
            if result is not None:
                returncode, stdout, stderr = result
                return _Process(
                    stdout=stdout,
                    stderr=stderr,
                    returncode=returncode,
                )
        if command[:3] == ("realpath", "-e", "--"):
            return _Process(stdout=f"{command[-1]}\n")
        if command[:4] == ("stat", "-c", "%s", "--"):
            return _Process(stdout=f"{len(self.filesystem.archive_bytes)}\n")
        return _Process()

    def terminate(self) -> None:
        self.terminated = True

    def detach(self) -> None:
        self.detached = True


class _Executions:
    def __init__(self, devbox: _RemoteDevboxState) -> None:
        self.devbox = devbox
        self.processes: dict[str, _Process] = {}
        self.rendered_commands: list[str] = []

    def execute_async(self, devbox_id: str, *, command: str) -> object:
        self._require_devbox(devbox_id)
        self.rendered_commands.append(command)
        argv = shlex.split(command)
        if argv[:3] != ["timeout", "--preserve-status", "120"]:
            raise AssertionError(f"missing bounded Runloop command timeout: {argv!r}")
        execution_id = f"execution-{len(self.processes) + 1}"
        self.processes[execution_id] = self.devbox.exec(*argv[3:])
        return SimpleNamespace(execution_id=execution_id)

    def await_completed(self, execution_id: str, *, devbox_id: str) -> object:
        self._require_devbox(devbox_id)
        process = self.processes[execution_id]
        return SimpleNamespace(
            exit_status=process.returncode,
            stdout=process.stdout.read(),
            stderr=process.stderr.read(),
        )

    def _require_devbox(self, devbox_id: str) -> None:
        if devbox_id != DEVBOX_ID:
            raise AssertionError(f"unexpected Devbox ID: {devbox_id}")


class _Devboxes:
    def __init__(self, devbox: _RemoteDevboxState) -> None:
        self.devbox = devbox
        self.executions = _Executions(devbox)
        self.retrieve_calls: list[str] = []
        self.create_calls: list[dict[str, object]] = []
        self.shutdown_calls: list[str] = []

    def retrieve(self, devbox_id: str) -> object:
        self.retrieve_calls.append(devbox_id)
        return SimpleNamespace(id=devbox_id)

    def read_file_contents(self, devbox_id: str, *, file_path: str) -> str:
        self._require_devbox(devbox_id)
        return self.devbox.filesystem.read_text(file_path)

    def download_file(self, devbox_id: str, *, path: str) -> bytes:
        self._require_devbox(devbox_id)
        filesystem = self.devbox.filesystem
        filesystem.copy_calls.append((path, Path("<runloop-download>")))
        filesystem.copied_archives.append(filesystem.archive_bytes)
        return filesystem.archive_bytes

    def shutdown(self, devbox_id: str) -> None:
        self.shutdown_calls.append(devbox_id)

    def _require_devbox(self, devbox_id: str) -> None:
        if devbox_id != DEVBOX_ID:
            raise AssertionError(f"unexpected Devbox ID: {devbox_id}")


class _FakeRunloop:
    def __init__(
        self,
        members: dict[str, bytes],
        marker: str | None = '{"exit_code": 0}\n',
        archive_bytes: bytes | None = None,
        command_result: Callable[
            [tuple[str, ...]], tuple[int, str, str] | None
        ]
        | None = None,
    ) -> None:
        self.devbox = _RemoteDevboxState(
            members,
            marker,
            archive_bytes=archive_bytes,
            command_result=command_result,
        )
        self.devboxes = _Devboxes(self.devbox)


def _write_snapshot(
    path: Path,
    *,
    repository: dict[str, object] | None = None,
) -> None:
    repository_metadata = repository or {"present": False}
    manifest = {
        "format_version": 1,
        "session": {
            "id": SESSION_ID,
            "rollout_archive_path": f"codex/{ROLLOUT_PATH}",
        },
        "repository": repository_metadata,
    }
    members = {
        "manifest.json": json.dumps(manifest).encode("utf-8"),
        f"codex/{ROLLOUT_PATH}": BASELINE_ROLLOUT,
        "workspace/app.py": b"print('local')\n",
    }
    if repository_metadata["present"]:
        members.update(
            {
                "git/repository.bundle": b"test bundle",
                "git/staged.patch": b"",
                "git/unstaged.patch": b"",
            }
        )
    path.write_bytes(_tar_bytes(members))


def _tar_bytes(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, contents in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(contents)
            archive.addfile(info, io.BytesIO(contents))
    return buffer.getvalue()


def _tar_symlink(name: str, target: str) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        info = tarfile.TarInfo(name)
        info.type = tarfile.SYMTYPE
        info.linkname = target
        archive.addfile(info)
    return buffer.getvalue()


def _tar_duplicate(name: str, contents: bytes) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for _ in range(2):
            info = tarfile.TarInfo(name)
            info.size = len(contents)
            archive.addfile(info, io.BytesIO(contents))
    return buffer.getvalue()


def _tar_hardlink(name: str, target: str) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        info = tarfile.TarInfo(name)
        info.type = tarfile.LNKTYPE
        info.linkname = target
        archive.addfile(info)
    return buffer.getvalue()


def _tar_device(name: str) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        info = tarfile.TarInfo(name)
        info.type = tarfile.CHRTYPE
        info.devmajor = 1
        info.devminor = 3
        archive.addfile(info)
    return buffer.getvalue()
