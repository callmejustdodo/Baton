from __future__ import annotations

import io
import json
import shlex
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import baton.fetch as fetch_module
from baton.fetch import (
    FetchError,
    FetchResult,
    _apply_workspace_patch,
    fetch_workspace,
    list_handoff_receipts,
)

SESSION_ID = "019f5ef4-780a-7973-a1d2-c460461ced1f"
DEVBOX_ID = "devbox-fetch-test"
COMPLETION_PATH = "/baton-control/handoff-complete.json"


class FetchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        (self.workspace / "app.py").write_text("print('local')\n", encoding="utf-8")
        (self.workspace / "keep.txt").write_text("unchanged\n", encoding="utf-8")
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

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_invalid_devbox_id_is_rejected_before_runloop_lookup(self) -> None:
        runloop = _FakeRunloop({"workspace/app.py": b"ignored\n"})

        with self.assertRaisesRegex(FetchError, "[Dd]evbox"):
            fetch_workspace(
                devbox_id="../sb-escape",
                workspace=self.workspace,
                receipt_path=self.receipt,
                runloop_client=runloop,
            )

        self.assertEqual(runloop.devboxes.retrieve_calls, [])

    def test_list_handoff_receipts_skips_symlinked_receipts(self) -> None:
        linked_receipt = self.receipt.parent / "sb-linked-receipt.json"
        linked_receipt.symlink_to(self.receipt)

        receipts = list_handoff_receipts(workspace=self.workspace)

        self.assertEqual([receipt.devbox_id for receipt in receipts], [DEVBOX_ID])
        self.assertEqual(receipts[0].path, self.receipt.resolve())

    def test_modal_receipt_format_is_rejected_before_runloop_lookup(self) -> None:
        payload = json.loads(self.receipt.read_text(encoding="utf-8"))
        payload["format_version"] = 1
        payload["sandbox_id"] = payload.pop("devbox_id")
        self.receipt.write_text(json.dumps(payload), encoding="utf-8")
        runloop = _FakeRunloop({"workspace/app.py": b"ignored\n"})

        with self.assertRaisesRegex(FetchError, "unsupported format"):
            fetch_workspace(
                devbox_id=DEVBOX_ID,
                workspace=self.workspace,
                receipt_path=self.receipt,
                runloop_client=runloop,
            )

        self.assertEqual(runloop.devboxes.retrieve_calls, [])

    def test_missing_completion_marker_is_rejected_before_download(self) -> None:
        runloop = _FakeRunloop({"workspace/app.py": b"ignored\n"}, marker=None)

        with self.assertRaisesRegex(FetchError, "still working|completion|complete"):
            fetch_workspace(
                devbox_id=DEVBOX_ID,
                workspace=self.workspace,
                receipt_path=self.receipt,
                runloop_client=runloop,
            )

        self.assertEqual(runloop.devboxes.download_calls, [])

    def test_receipt_from_another_workspace_is_rejected_before_runloop_lookup(self) -> None:
        payload = json.loads(self.receipt.read_text(encoding="utf-8"))
        payload["workspace"] = str((self.root / "other-workspace").resolve())
        self.receipt.write_text(json.dumps(payload), encoding="utf-8")
        runloop = _FakeRunloop({"workspace/app.py": b"ignored\n"})

        with self.assertRaisesRegex(FetchError, "different workspace"):
            fetch_workspace(
                devbox_id=DEVBOX_ID,
                workspace=self.workspace,
                receipt_path=self.receipt,
                runloop_client=runloop,
            )

        self.assertEqual(runloop.devboxes.retrieve_calls, [])

    def test_failed_remote_resume_is_fetchable_after_completion(self) -> None:
        runloop = _FakeRunloop(
            {"workspace/app.py": b"print('remote')\n"},
            marker='{"exit_code": 2}\n',
        )

        result = fetch_workspace(
            devbox_id=DEVBOX_ID,
            workspace=self.workspace,
            receipt_path=self.receipt,
            runloop_client=runloop,
        )

        self.assertEqual(result.remote_exit_code, 2)
        self.assertFalse(result.applied)
        self.assertEqual((self.workspace / "app.py").read_text(), "print('local')\n")
        self.assertEqual(len(runloop.devboxes.download_calls), 1)

    def test_successful_fetch_applies_added_modified_and_deleted_files(self) -> None:
        runloop = _FakeRunloop(
            {
                "workspace/app.py": b"print('remote')\n",
                "workspace/new.txt": b"fresh\n",
            }
        )

        result = fetch_workspace(
            devbox_id=DEVBOX_ID,
            workspace=self.workspace,
            receipt_path=self.receipt,
            runloop_client=runloop,
        )

        output = (self.workspace / ".baton/fetches" / DEVBOX_ID).resolve()
        self.assertEqual(result.remote_workspace, output / "workspace")
        self.assertEqual(result.patch_path, output / "changes.patch")
        self.assertEqual((result.remote_workspace / "new.txt").read_text(), "fresh\n")
        self.assertIn("print('remote')", result.patch_path.read_text(encoding="utf-8"))
        self.assertIn(
            "diff --git a/new.txt b/new.txt",
            result.patch_path.read_text(encoding="utf-8"),
        )
        self.assertTrue(result.applied)
        self.assertTrue(result.to_dict()["applied"])
        self.assertEqual(result.to_dict()["devbox_id"], DEVBOX_ID)
        self.assertNotIn("sandbox_id", result.to_dict())
        persisted_result = json.loads((output / "result.json").read_text(encoding="utf-8"))
        self.assertTrue(persisted_result["applied"])
        self.assertEqual((self.workspace / "app.py").read_text(), "print('remote')\n")
        self.assertEqual((self.workspace / "new.txt").read_text(), "fresh\n")
        self.assertFalse((self.workspace / "keep.txt").exists())
        self.assertEqual(runloop.devboxes.retrieve_calls, [DEVBOX_ID])
        self.assertEqual(runloop.devboxes.read_calls, [COMPLETION_PATH])
        self.assertEqual(runloop.devboxes.create_calls, [])
        self.assertEqual(runloop.devboxes.shutdown_calls, [])

    def test_fetch_with_apply_changes_false_leaves_workspace_unchanged(self) -> None:
        runloop = _FakeRunloop(
            {
                "workspace/app.py": b"print('remote')\n",
                "workspace/new.txt": b"fresh\n",
            }
        )

        result = fetch_workspace(
            devbox_id=DEVBOX_ID,
            workspace=self.workspace,
            receipt_path=self.receipt,
            runloop_client=runloop,
            apply_changes=False,
        )

        self.assertFalse(result.applied)
        self.assertFalse(result.to_dict()["applied"])
        self.assertEqual((self.workspace / "app.py").read_text(), "print('local')\n")
        self.assertEqual((self.workspace / "keep.txt").read_text(), "unchanged\n")
        self.assertFalse((self.workspace / "new.txt").exists())

    def test_custom_output_inside_workspace_does_not_count_as_local_drift(self) -> None:
        runloop = _FakeRunloop({"workspace/app.py": b"print('remote')\n"})
        output = self.workspace / "review-output"

        result = fetch_workspace(
            devbox_id=DEVBOX_ID,
            workspace=self.workspace,
            receipt_path=self.receipt,
            output=output,
            runloop_client=runloop,
        )

        self.assertTrue(result.applied)
        self.assertEqual(result.fetch_root, output.resolve())
        self.assertEqual((self.workspace / "app.py").read_text(), "print('remote')\n")

    def test_empty_remote_diff_is_a_successful_no_op(self) -> None:
        runloop = _FakeRunloop(
            {
                "workspace/app.py": b"print('local')\n",
                "workspace/keep.txt": b"unchanged\n",
            }
        )
        before = _workspace_files(self.workspace)

        result = fetch_workspace(
            devbox_id=DEVBOX_ID,
            workspace=self.workspace,
            receipt_path=self.receipt,
            runloop_client=runloop,
        )

        self.assertTrue(result.applied)
        self.assertEqual(result.patch_path.read_bytes(), b"")
        self.assertEqual(_workspace_files(self.workspace), before)

    def test_fetch_refuses_local_drift_before_changing_workspace(self) -> None:
        runloop = _FakeRunloop({"workspace/app.py": b"print('remote')\n"})
        (self.workspace / "app.py").write_text("print('newer local edit')\n")
        before = _workspace_files(self.workspace)

        with self.assertRaisesRegex(FetchError, "local|drift|changed|baseline|conflict"):
            fetch_workspace(
                devbox_id=DEVBOX_ID,
                workspace=self.workspace,
                receipt_path=self.receipt,
                runloop_client=runloop,
            )

        self.assertEqual(_workspace_files(self.workspace), before)
        result_payload = json.loads(
            (self.workspace / ".baton/fetches" / DEVBOX_ID / "result.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertFalse(result_payload["applied"])
        self.assertEqual(result_payload["apply_status"], "apply_failed")

    def test_apply_failure_leaves_workspace_unchanged(self) -> None:
        runloop = _FakeRunloop(
            {
                "workspace/app.py": b"print('remote')\n",
                "workspace/new.txt": b"fresh\n",
            }
        )
        before = _workspace_files(self.workspace)

        with (
            patch(
                "baton.fetch._apply_workspace_patch",
                side_effect=FetchError("injected apply failure"),
            ),
            self.assertRaisesRegex(FetchError, "apply|injected apply failure"),
        ):
            fetch_workspace(
                devbox_id=DEVBOX_ID,
                workspace=self.workspace,
                receipt_path=self.receipt,
                runloop_client=runloop,
            )

        self.assertEqual(_workspace_files(self.workspace), before)
        result_payload = json.loads(
            (self.workspace / ".baton/fetches" / DEVBOX_ID / "result.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertFalse(result_payload["applied"])
        self.assertEqual(result_payload["apply_status"], "apply_failed")

    def test_post_apply_metadata_failure_reports_that_changes_were_applied(self) -> None:
        runloop = _FakeRunloop({"workspace/app.py": b"print('remote')\n"})
        original_write = fetch_module._write_fetch_result
        write_calls = 0

        def fail_final_write(
            path: Path,
            result: FetchResult,
            session_id: str,
            *,
            applied: bool | None,
            apply_status: str,
        ) -> None:
            nonlocal write_calls
            write_calls += 1
            if write_calls == 2:
                raise OSError("disk full")
            original_write(
                path,
                result,
                session_id,
                applied=applied,
                apply_status=apply_status,
            )

        with (
            patch("baton.fetch._write_fetch_result", side_effect=fail_final_write),
            self.assertRaisesRegex(FetchError, "were applied|Do not rerun"),
        ):
            fetch_workspace(
                devbox_id=DEVBOX_ID,
                workspace=self.workspace,
                receipt_path=self.receipt,
                runloop_client=runloop,
            )

        result_path = self.workspace / ".baton/fetches" / DEVBOX_ID / "result.json"
        result_payload = json.loads(result_path.read_text(encoding="utf-8"))
        self.assertIsNone(result_payload["applied"])
        self.assertEqual(result_payload["apply_status"], "pending")
        self.assertEqual((self.workspace / "app.py").read_text(), "print('remote')\n")

    def test_git_index_drift_is_rejected_before_auto_apply(self) -> None:
        _git(self.workspace, "init")
        _git(self.workspace, "config", "user.email", "baton@example.test")
        _git(self.workspace, "config", "user.name", "Baton Test")
        _git(self.workspace, "add", "app.py", "keep.txt")
        _git(self.workspace, "commit", "-m", "baseline")
        _write_git_snapshot(self.snapshot, self.workspace)
        (self.workspace / "app.py").write_text("print('staged')\n", encoding="utf-8")
        _git(self.workspace, "add", "app.py")
        (self.workspace / "app.py").write_text("print('local')\n", encoding="utf-8")
        runloop = _FakeRunloop({"workspace/app.py": b"print('remote')\n"})

        with self.assertRaisesRegex(FetchError, "Git index changed"):
            fetch_workspace(
                devbox_id=DEVBOX_ID,
                workspace=self.workspace,
                receipt_path=self.receipt,
                runloop_client=runloop,
            )

        self.assertEqual((self.workspace / "app.py").read_text(), "print('local')\n")

    def test_matching_git_workspace_auto_applies_without_changing_its_index(self) -> None:
        _git(self.workspace, "init")
        _git(self.workspace, "config", "user.email", "baton@example.test")
        _git(self.workspace, "config", "user.name", "Baton Test")
        _git(self.workspace, "add", "app.py", "keep.txt")
        _git(self.workspace, "commit", "-m", "baseline")
        _write_git_snapshot(self.snapshot, self.workspace)
        runloop = _FakeRunloop(
            {
                "workspace/app.py": b"print('remote')\n",
                "workspace/keep.txt": b"unchanged\n",
                "workspace/new.txt": b"fresh\n",
            }
        )

        result = fetch_workspace(
            devbox_id=DEVBOX_ID,
            workspace=self.workspace,
            receipt_path=self.receipt,
            runloop_client=runloop,
        )

        self.assertTrue(result.applied)
        self.assertEqual((self.workspace / "app.py").read_text(), "print('remote')\n")
        self.assertEqual((self.workspace / "new.txt").read_text(), "fresh\n")
        self.assertEqual(_git(self.workspace, "diff", "--cached").stdout, b"")

    def test_matching_dirty_git_workspace_auto_applies_without_rewriting_its_index(self) -> None:
        _git(self.workspace, "init")
        _git(self.workspace, "config", "user.email", "baton@example.test")
        _git(self.workspace, "config", "user.name", "Baton Test")
        _git(self.workspace, "add", "app.py", "keep.txt")
        _git(self.workspace, "commit", "-m", "baseline")
        (self.workspace / "app.py").write_text("print('staged')\n", encoding="utf-8")
        _git(self.workspace, "add", "app.py")
        (self.workspace / "keep.txt").write_text("unstaged\n", encoding="utf-8")
        _write_git_snapshot(self.snapshot, self.workspace)
        staged_before = _git(self.workspace, "diff", "--cached", "--binary", "--root").stdout
        runloop = _FakeRunloop(
            {
                "workspace/app.py": b"print('remote staged')\n",
                "workspace/keep.txt": b"remote unstaged\n",
            }
        )

        result = fetch_workspace(
            devbox_id=DEVBOX_ID,
            workspace=self.workspace,
            receipt_path=self.receipt,
            runloop_client=runloop,
        )

        self.assertTrue(result.applied)
        self.assertEqual((self.workspace / "app.py").read_text(), "print('remote staged')\n")
        self.assertEqual((self.workspace / "keep.txt").read_text(), "remote unstaged\n")
        self.assertEqual(_git(self.workspace, "diff", "--cached", "--binary", "--root").stdout, staged_before)

    def test_changed_directory_symlink_is_rejected_before_it_can_escape_workspace(self) -> None:
        baseline = self.root / "baseline"
        baseline.mkdir()
        (baseline / "nested").mkdir()
        (baseline / "nested/file.txt").write_text("baseline\n", encoding="utf-8")
        workspace = self.root / "symlink-workspace"
        workspace.mkdir()
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "file.txt").write_text("outside\n", encoding="utf-8")
        (workspace / "nested").symlink_to(outside, target_is_directory=True)
        patch_path = self.root / "changes.patch"
        patch_path.write_text(
            "diff --git a/nested/file.txt b/nested/file.txt\n"
            "--- a/nested/file.txt\n"
            "+++ b/nested/file.txt\n"
            "@@ -1 +1 @@\n"
            "-baseline\n"
            "+remote\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(FetchError, "local workspace changed"):
            _apply_workspace_patch(workspace, baseline, patch_path)

        self.assertEqual((outside / "file.txt").read_text(), "outside\n")

    def test_unsafe_tar_path_is_rejected_without_finalizing_output(self) -> None:
        runloop = _FakeRunloop({"../outside.txt": b"escape"})
        output = self.root / "unsafe-fetch"

        with self.assertRaisesRegex(FetchError, "escape|outside|root|unsafe"):
            fetch_workspace(
                devbox_id=DEVBOX_ID,
                workspace=self.workspace,
                receipt_path=self.receipt,
                output=output,
                runloop_client=runloop,
            )

        self.assertFalse(output.exists())

    def test_patch_paths_are_normalized_for_a_deleted_file(self) -> None:
        runloop = _FakeRunloop({"workspace/app.py": b"print('local')\n"})
        output = self.root / "deleted-file-fetch"

        result = fetch_workspace(
            devbox_id=DEVBOX_ID,
            workspace=self.workspace,
            receipt_path=self.receipt,
            output=output,
            runloop_client=runloop,
        )

        patch = result.patch_path.read_text(encoding="utf-8")
        self.assertIn("diff --git a/keep.txt b/keep.txt", patch)
        self.assertNotIn("baseline/keep.txt", patch)
        self.assertNotIn("workspace/keep.txt", patch)
        self.assertFalse((self.root / "outside.txt").exists())

    def test_git_directory_is_rejected_without_finalizing_output(self) -> None:
        runloop = _FakeRunloop(
            {
                "workspace/app.py": b"print('remote')\n",
                "workspace/.git/config": b"[core]\n",
            }
        )
        output = self.root / "git-fetch"

        with self.assertRaisesRegex(FetchError, "\\.git|internal"):
            fetch_workspace(
                devbox_id=DEVBOX_ID,
                workspace=self.workspace,
                receipt_path=self.receipt,
                output=output,
                runloop_client=runloop,
            )

        self.assertFalse(output.exists())


def _workspace_files(workspace: Path) -> dict[str, bytes]:
    return {
        path.relative_to(workspace).as_posix(): path.read_bytes()
        for path in workspace.rglob("*")
        if path.is_file() and ".baton" not in path.relative_to(workspace).parts
    }


class _DevboxView:
    def __init__(self, devbox_id: str) -> None:
        self.id = devbox_id


class _ExecutionView:
    def __init__(self, execution_id: str) -> None:
        self.execution_id = execution_id


class _ExecutionResult:
    exit_status = 0
    stdout = ""
    stderr = ""


class _Executions:
    def __init__(self, parent: _Devboxes) -> None:
        self.parent = parent
        self.commands: list[list[str]] = []

    def execute_async(self, devbox_id: str, *, command: str) -> _ExecutionView:
        self.parent._require_id(devbox_id)
        argv = shlex.split(command)
        if argv[:3] == ["timeout", "--preserve-status", "120"]:
            argv = argv[3:]
        self.commands.append(argv)
        if argv[0] == "tar":
            remote_archive = argv[argv.index("-czf") + 1]
            self.parent.remote_files[remote_archive] = _tar_bytes(self.parent.members)
        elif argv[:2] == ["rm", "-f"]:
            self.parent.remote_files.pop(argv[2], None)
        return _ExecutionView(f"execution-{len(self.commands)}")

    def await_completed(self, execution_id: str, *, devbox_id: str) -> _ExecutionResult:
        self.parent._require_id(devbox_id)
        if not execution_id.startswith("execution-"):
            raise ValueError(execution_id)
        return _ExecutionResult()


class _Devboxes:
    def __init__(self, members: dict[str, bytes], marker: str | None) -> None:
        self.members = members
        self.marker = marker
        self.remote_files: dict[str, bytes] = {}
        self.retrieve_calls: list[str] = []
        self.read_calls: list[str] = []
        self.download_calls: list[tuple[str, str]] = []
        self.create_calls: list[dict[str, object]] = []
        self.shutdown_calls: list[str] = []
        self.executions = _Executions(self)

    def retrieve(self, devbox_id: str) -> _DevboxView:
        self.retrieve_calls.append(devbox_id)
        return _DevboxView(devbox_id)

    def read_file_contents(self, devbox_id: str, *, file_path: str) -> str:
        self._require_id(devbox_id)
        self.read_calls.append(file_path)
        if file_path == COMPLETION_PATH and self.marker is not None:
            return self.marker
        raise FileNotFoundError(file_path)

    def download_file(self, devbox_id: str, *, path: str) -> bytes:
        self._require_id(devbox_id)
        self.download_calls.append((devbox_id, path))
        return self.remote_files[path]

    def create(self, **kwargs: object) -> _DevboxView:
        self.create_calls.append(kwargs)
        return _DevboxView(DEVBOX_ID)

    def shutdown(self, devbox_id: str) -> None:
        self.shutdown_calls.append(devbox_id)

    @staticmethod
    def _require_id(devbox_id: str) -> None:
        if devbox_id != DEVBOX_ID:
            raise ValueError(devbox_id)


class _FakeRunloop:
    def __init__(
        self,
        members: dict[str, bytes],
        marker: str | None = '{"exit_code": 0}\n',
    ) -> None:
        self.devboxes = _Devboxes(members, marker)


def _write_snapshot(path: Path) -> None:
    rollout = f"codex/sessions/2026/08/23/rollout-{SESSION_ID}.jsonl"
    manifest = {
        "format_version": 1,
        "session": {"id": SESSION_ID, "rollout_archive_path": rollout},
        "repository": {"present": False},
    }
    members = {
        "manifest.json": json.dumps(manifest).encode(),
        rollout: json.dumps({"payload": {"session_id": SESSION_ID}}).encode() + b"\n",
        "workspace/app.py": b"print('local')\n",
        "workspace/keep.txt": b"unchanged\n",
    }
    path.write_bytes(_tar_bytes(members))


def _write_git_snapshot(path: Path, workspace: Path) -> None:
    head = _git(workspace, "rev-parse", "HEAD").stdout.decode("utf-8").strip()
    branch = _git(workspace, "branch", "--show-current").stdout.decode("utf-8").strip()
    staged = _git(
        workspace,
        "diff",
        "--binary",
        "--no-ext-diff",
        "--cached",
        "--root",
    ).stdout
    unstaged = _git(workspace, "diff", "--binary", "--no-ext-diff").stdout
    rollout = f"codex/sessions/2026/08/23/rollout-{SESSION_ID}.jsonl"
    manifest = {
        "format_version": 1,
        "session": {"id": SESSION_ID, "rollout_archive_path": rollout},
        "repository": {
            "present": True,
            "head": head,
            "branch": branch or None,
            "bundle_archive_path": "git/repository.bundle",
            "artifacts": [
                "git/repository.bundle",
                "git/staged.patch",
                "git/unstaged.patch",
            ],
        },
    }
    members = {
        "manifest.json": json.dumps(manifest).encode(),
        rollout: json.dumps({"payload": {"session_id": SESSION_ID}}).encode() + b"\n",
        "workspace/app.py": (workspace / "app.py").read_bytes(),
        "workspace/keep.txt": (workspace / "keep.txt").read_bytes(),
        "git/repository.bundle": b"test bundle",
        "git/staged.patch": staged,
        "git/unstaged.patch": unstaged,
    }
    path.write_bytes(_tar_bytes(members))


def _git(workspace: Path, *arguments: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", "-C", str(workspace), *arguments],
        capture_output=True,
        check=True,
    )


def _tar_bytes(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz", format=tarfile.PAX_FORMAT) as archive:
        for name, contents in members.items():
            member = tarfile.TarInfo(name)
            member.size = len(contents)
            archive.addfile(member, io.BytesIO(contents))
    return buffer.getvalue()
