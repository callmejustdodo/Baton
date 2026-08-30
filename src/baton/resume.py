"""Restore one completed remote Codex session to the local Codex home."""

from __future__ import annotations

import io
import json
import os
import shutil
import stat
import subprocess
import tarfile
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import UUID, uuid4

from .fetch import (
    FETCHES_DIRECTORY,
    HANDOFF_RECEIPTS_DIRECTORY,
    REMOTE_COMPLETION_MARKER,
    HandoffReceipt,
    _load_handoff_receipt,
    _normalize_devbox_id,
    _workspace_fingerprint,
    _workspace_state_directory,
    _workspace_state_path,
)
from .handoff import (
    REMOTE_RUNTIME_USER,
    REMOTE_WORKSPACE,
    HandoffError,
    SnapshotArchive,
    inspect_snapshot_archive,
)
from .runloop import RunloopClientError, load_runloop_client, open_devbox

REMOTE_CODEX_HOME = "/baton/.codex"
SESSION_BACKUPS_DIRECTORY = Path(".baton") / "session-backups"
PREPARED_SESSION_DIRECTORY = "session-history"
PREPARED_SESSION_ARCHIVE = "remote-session.tar.gz"
PREPARED_SESSION_MANIFEST = "manifest.json"
PREPARED_SESSION_FORMAT_VERSION = 1
MAX_SESSION_ARCHIVE_BYTES = 512 * 1024 * 1024
_FILTER_SESSION_INDEX_SCRIPT = """\
const fs = require("fs");
const readline = require("readline");
const [sourcePath, destinationPath, sessionId] = process.argv.slice(1);

(async () => {
  const destination = fs.openSync(destinationPath, "wx", 0o600);
  let lineNumber = 0;
  try {
    const lines = readline.createInterface({
      input: fs.createReadStream(sourcePath),
      crlfDelay: Infinity,
    });
    for await (const line of lines) {
      lineNumber += 1;
      if (!line.trim()) continue;
      let record;
      try {
        record = JSON.parse(line);
      } catch (error) {
        throw new Error(`invalid session index JSONL at line ${lineNumber}: ${error}`);
      }
      if (record === null || typeof record !== "object" || Array.isArray(record)) {
        throw new Error(`invalid session index record at line ${lineNumber}`);
      }
      if (record.id === sessionId) fs.writeSync(destination, `${line}\\n`);
    }
  } finally {
    fs.closeSync(destination);
  }
})().catch((error) => {
  console.error(error.message);
  process.exitCode = 1;
});
"""


class ResumeError(RuntimeError):
    """Raised when a remote Codex session cannot safely be restored locally."""


@dataclass(frozen=True)
class _GitState:
    head: str
    branch: str
    status: str
    index: str
    index_flags: str
    refs: str


@dataclass(frozen=True)
class ResumeResult:
    """The local result of recovering one completed remote Codex session."""

    devbox_id: str
    session_id: str
    archive: Path
    fetch_root: Path
    rollout_path: Path
    backup_path: Path | None
    index_backup_path: Path | None
    remote_exit_code: int
    launched: bool
    local_exit_code: int | None

    def to_dict(self) -> dict[str, object]:
        return {
            "devbox_id": self.devbox_id,
            "session_id": self.session_id,
            "archive": str(self.archive),
            "fetch_root": str(self.fetch_root),
            "rollout_path": str(self.rollout_path),
            "backup_path": str(self.backup_path) if self.backup_path is not None else None,
            "index_backup_path": (
                str(self.index_backup_path) if self.index_backup_path is not None else None
            ),
            "remote_exit_code": self.remote_exit_code,
            "launched": self.launched,
            "local_exit_code": self.local_exit_code,
        }


@dataclass(frozen=True)
class _LocalRestorePlan:
    """The already-validated local files that a restore may replace."""

    rollout_path: Path
    index_path: Path
    current_rollout: bytes | None
    current_index: bytes | None
    merged_index: bytes | None
    backups_directory: Path


@dataclass(frozen=True)
class _PreparedSessionPayload:
    """Validated remote history retained locally before the workspace is changed."""

    remote_rollout: bytes
    remote_index: bytes | None
    remote_exit_code: int
    remote_git_state: _GitState | None
    baseline_git_state: _GitState | None


def resume_remote_session(
    *,
    devbox_id: str,
    workspace: Path,
    codex_home: Path,
    receipt_path: Path | None = None,
    fetch_root: Path | None = None,
    launch: bool = False,
    runloop_client: Any | None = None,
) -> ResumeResult:
    """Recover the selected rollout from a completed detached handoff.

    Only the manifest-selected transcript and its matching session-index metadata
    are read from the Devbox.  Runtime state such as ``auth.json``, SQLite
    databases, plugins, logs, and locks intentionally stays remote.
    """

    result = _restore_remote_session(
        devbox_id=devbox_id,
        workspace=workspace,
        codex_home=codex_home,
        receipt_path=receipt_path,
        fetch_root=fetch_root,
        launch=launch,
        runloop_client=runloop_client,
        preflight_only=False,
    )
    assert result is not None
    return result


def preflight_remote_session_restore(
    *,
    devbox_id: str,
    workspace: Path,
    codex_home: Path,
    fetch_root: Path,
    receipt_path: Path | None = None,
    runloop_client: Any | None = None,
) -> None:
    """Verify remote history and local Codex targets without changing either one.

    ``baton fetch`` uses this before applying its workspace patch. It catches a
    missing or unsafe CODEX_HOME, malformed remote history, and divergent local
    transcript while the workspace is still untouched, then retains the verified
    rollout in the fetch artifact for the later local restore.
    """

    _restore_remote_session(
        devbox_id=devbox_id,
        workspace=workspace,
        codex_home=codex_home,
        receipt_path=receipt_path,
        fetch_root=fetch_root,
        launch=False,
        runloop_client=runloop_client,
        preflight_only=True,
    )


def _restore_remote_session(
    *,
    devbox_id: str,
    workspace: Path,
    codex_home: Path,
    receipt_path: Path | None,
    fetch_root: Path | None,
    launch: bool,
    runloop_client: Any | None,
    preflight_only: bool,
) -> ResumeResult | None:
    normalized_devbox_id = _normalize_devbox_id_or_raise(devbox_id)
    source_workspace = _existing_directory(workspace, "workspace")
    local_codex_home = _existing_directory(codex_home, "CODEX_HOME")
    resolved_receipt_path = _receipt_path(
        source_workspace,
        normalized_devbox_id,
        receipt_path,
    )
    receipt = _load_receipt(resolved_receipt_path, normalized_devbox_id)
    if receipt.workspace != source_workspace:
        raise ResumeError(
            "handoff receipt belongs to a different workspace; resume from the original "
            "workspace so Baton can verify the handoff baseline"
        )
    applied_fetch_root = (
        None
        if preflight_only
        else _require_applied_workspace(
            workspace=source_workspace,
            receipt=receipt,
            fetch_root=fetch_root,
        )
    )

    snapshot = _inspect_snapshot(receipt)
    rollout_relative = _rollout_relative_path(snapshot, receipt.session_id)
    baseline_rollout = _snapshot_member_bytes(
        snapshot.path,
        PurePosixPath("codex") / rollout_relative,
    )
    _validate_rollout_bytes(baseline_rollout, receipt.session_id, "snapshot rollout")

    if not preflight_only:
        assert applied_fetch_root is not None
        prepared_payload = _load_prepared_session_payload(
            fetch_root=applied_fetch_root,
            receipt=receipt,
            rollout_relative=rollout_relative,
        )
        if prepared_payload is not None:
            return _restore_prepared_session_payload(
                devbox_id=normalized_devbox_id,
                workspace=source_workspace,
                codex_home=local_codex_home,
                receipt=receipt,
                snapshot=snapshot,
                rollout_relative=rollout_relative,
                baseline_rollout=baseline_rollout,
                fetch_root=applied_fetch_root,
                payload=prepared_payload,
                launch=launch,
            )

    client = runloop_client
    try:
        if client is None:
            client = load_runloop_client()
        devbox = open_devbox(client, normalized_devbox_id)
    except RunloopClientError as error:
        raise ResumeError(str(error)) from error

    completion_marker = _completion_marker(devbox)
    remote_exit_code = _completion_exit_code(completion_marker)
    baseline_git_state = _marker_git_state(completion_marker)
    if remote_exit_code != 0:
        raise ResumeError(
            "the remote Codex handoff exited with "
            f"status {remote_exit_code}; Baton will not restore a failed session. "
            "Use 'baton fetch --no-apply' to inspect its workspace result."
        )
    remote_archive = f"/tmp/baton-session-{uuid4().hex}.tar.gz"
    remote_index_directory = f"/tmp/baton-session-index-{uuid4().hex}"
    remote_filtered_index = f"{remote_index_directory}/session_index.jsonl"
    primary_error: BaseException | None = None
    artifacts_removed = False
    index_directory_created = False

    try:
        remote_git_state: _GitState | None = None
        if preflight_only:
            remote_git_state = _assert_remote_git_state_matches_baseline(
                devbox,
                snapshot.repository,
                baseline_git_state,
            )
        else:
            _assert_git_state_matches_remote(
                devbox,
                source_workspace,
                snapshot.repository,
                baseline_git_state,
            )
        remote_rollout_path = _remote_path(rollout_relative)
        _assert_remote_unlinked_regular_file(
            devbox,
            remote_rollout_path,
            canonical_root=REMOTE_CODEX_HOME,
        )
        archive_members = [rollout_relative.as_posix()]
        total_payload_size = _assert_remote_file_size(
            devbox,
            remote_rollout_path,
            label="selected remote rollout",
        )
        if _remote_regular_file_exists(devbox, f"{REMOTE_CODEX_HOME}/session_index.jsonl"):
            remote_index_path = f"{REMOTE_CODEX_HOME}/session_index.jsonl"
            _assert_remote_unlinked_regular_file(
                devbox,
                remote_index_path,
                canonical_root=REMOTE_CODEX_HOME,
            )
            _assert_remote_file_size(
                devbox,
                remote_index_path,
                label="remote session index",
            )
            _run_checked(
                devbox,
                "mkdir",
                "-m",
                "700",
                "--",
                remote_index_directory,
            )
            index_directory_created = True
            _run_checked(
                devbox,
                "node",
                "-e",
                _FILTER_SESSION_INDEX_SCRIPT,
                remote_index_path,
                remote_filtered_index,
                receipt.session_id,
            )
            _assert_remote_unlinked_regular_file(devbox, remote_filtered_index)
            total_payload_size += _assert_remote_file_size(
                devbox,
                remote_filtered_index,
                label="filtered remote session index",
            )
            archive_members.insert(0, "session_index.jsonl")
        if total_payload_size > MAX_SESSION_ARCHIVE_BYTES:
            raise ResumeError(
                "remote session payload exceeds Baton's 512 MiB safety limit before download"
            )
        if index_directory_created:
            _run_checked(
                devbox,
                "tar",
                "--no-recursion",
                "-czf",
                remote_archive,
                "-C",
                REMOTE_CODEX_HOME,
                rollout_relative.as_posix(),
                "-C",
                remote_index_directory,
                "session_index.jsonl",
            )
        else:
            _run_checked(
                devbox,
                "tar",
                "--no-recursion",
                "-C",
                REMOTE_CODEX_HOME,
                "-czf",
                remote_archive,
                "--",
                *archive_members,
            )
        _assert_remote_file_size(
            devbox,
            remote_archive,
            label="temporary remote session archive",
        )

        with tempfile.TemporaryDirectory(prefix="baton-resume-") as temporary_directory:
            staging = Path(temporary_directory)
            downloaded_archive = staging / "remote-session.tar.gz"
            devbox.filesystem.copy_to_local(remote_archive, downloaded_archive)
            _assert_local_archive_size(downloaded_archive)
            cleanup_error = _remove_remote_artifacts(
                devbox,
                remote_archive,
                remote_index_directory if index_directory_created else None,
            )
            if cleanup_error is not None:
                raise cleanup_error
            artifacts_removed = True

            remote_rollout, remote_index = _read_remote_session_archive(
                downloaded_archive,
                rollout_relative,
            )

        _validate_rollout_bytes(remote_rollout, receipt.session_id, "remote rollout")
        if not remote_rollout.startswith(baseline_rollout):
            raise ResumeError(
                "remote rollout does not extend the immutable handoff baseline; "
                "refusing to overwrite the local session"
            )

        if preflight_only:
            _prepare_local_restore(
                local_codex_home=local_codex_home,
                rollout_relative=rollout_relative,
                session_id=receipt.session_id,
                remote_rollout=remote_rollout,
                remote_index=remote_index,
                workspace=source_workspace,
            )
            assert fetch_root is not None
            _stage_prepared_session_payload(
                fetch_root=fetch_root,
                receipt=receipt,
                rollout_relative=rollout_relative,
                remote_rollout=remote_rollout,
                remote_index=remote_index,
                remote_exit_code=remote_exit_code,
                remote_git_state=remote_git_state,
                baseline_git_state=baseline_git_state,
            )
            return None

        assert applied_fetch_root is not None

        # Downloading and validating a long transcript can take time. Recheck the
        # fetched checkout immediately before writing local session state so we do
        # not attach the remote conversation to a workspace that changed mid-restore.
        _require_applied_workspace(
            workspace=source_workspace,
            receipt=receipt,
            fetch_root=applied_fetch_root,
        )
        _assert_git_state_matches_remote(
            devbox,
            source_workspace,
            snapshot.repository,
            baseline_git_state,
        )

        local_restore = _prepare_local_restore(
            local_codex_home=local_codex_home,
            rollout_relative=rollout_relative,
            session_id=receipt.session_id,
            remote_rollout=remote_rollout,
            remote_index=remote_index,
            workspace=source_workspace,
        )
        backup_path, index_backup_path = _restore_locally(
            rollout_path=local_restore.rollout_path,
            index_path=local_restore.index_path,
            current_rollout=local_restore.current_rollout,
            current_index=local_restore.current_index,
            remote_rollout=remote_rollout,
            merged_index=local_restore.merged_index,
            backups_directory=local_restore.backups_directory,
            session_id=receipt.session_id,
        )

        local_exit_code: int | None = None
        if launch:
            local_exit_code = _launch_local_codex(
                receipt.session_id,
                source_workspace,
                local_codex_home,
            )
        return ResumeResult(
            devbox_id=normalized_devbox_id,
            session_id=receipt.session_id,
            archive=receipt.archive,
            fetch_root=applied_fetch_root,
            rollout_path=local_restore.rollout_path,
            backup_path=backup_path,
            index_backup_path=index_backup_path,
            remote_exit_code=remote_exit_code,
            launched=launch,
            local_exit_code=local_exit_code,
        )
    except ResumeError as error:
        primary_error = error
        raise
    except Exception as error:
        resume_error = ResumeError(f"could not restore remote Codex session: {error}")
        primary_error = resume_error
        raise resume_error from error
    except BaseException as error:
        primary_error = error
        raise
    finally:
        cleanup_error = (
            None
            if artifacts_removed
            else _remove_remote_artifacts(
                devbox,
                remote_archive,
                remote_index_directory if index_directory_created else None,
            )
        )
        if cleanup_error is not None:
            if primary_error is None:
                raise cleanup_error
            if isinstance(primary_error, ResumeError):
                primary_error.args = (f"{primary_error}; additionally, {cleanup_error}",)
            elif hasattr(primary_error, "add_note"):
                primary_error.add_note(str(cleanup_error))


def _restore_prepared_session_payload(
    *,
    devbox_id: str,
    workspace: Path,
    codex_home: Path,
    receipt: HandoffReceipt,
    snapshot: SnapshotArchive,
    rollout_relative: PurePosixPath,
    baseline_rollout: bytes,
    fetch_root: Path,
    payload: _PreparedSessionPayload,
    launch: bool,
) -> ResumeResult:
    """Commit the already-downloaded history after the workspace patch is applied."""

    if not payload.remote_rollout.startswith(baseline_rollout):
        raise ResumeError(
            "prepared remote rollout does not extend the immutable handoff baseline; "
            "refusing to overwrite the local session"
        )
    _require_applied_workspace(
        workspace=workspace,
        receipt=receipt,
        fetch_root=fetch_root,
    )
    _assert_prepared_git_state_matches_local(
        workspace,
        snapshot.repository,
        payload.remote_git_state,
        payload.baseline_git_state,
    )
    local_restore = _prepare_local_restore(
        local_codex_home=codex_home,
        rollout_relative=rollout_relative,
        session_id=receipt.session_id,
        remote_rollout=payload.remote_rollout,
        remote_index=payload.remote_index,
        workspace=workspace,
    )
    backup_path, index_backup_path = _restore_locally(
        rollout_path=local_restore.rollout_path,
        index_path=local_restore.index_path,
        current_rollout=local_restore.current_rollout,
        current_index=local_restore.current_index,
        remote_rollout=payload.remote_rollout,
        merged_index=local_restore.merged_index,
        backups_directory=local_restore.backups_directory,
        session_id=receipt.session_id,
    )
    local_exit_code: int | None = None
    if launch:
        local_exit_code = _launch_local_codex(receipt.session_id, workspace, codex_home)
    return ResumeResult(
        devbox_id=devbox_id,
        session_id=receipt.session_id,
        archive=receipt.archive,
        fetch_root=fetch_root,
        rollout_path=local_restore.rollout_path,
        backup_path=backup_path,
        index_backup_path=index_backup_path,
        remote_exit_code=payload.remote_exit_code,
        launched=launch,
        local_exit_code=local_exit_code,
    )


def _normalize_devbox_id_or_raise(devbox_id: str) -> str:
    try:
        return _normalize_devbox_id(devbox_id)
    except Exception as error:
        raise ResumeError(str(error)) from error


def _receipt_path(workspace: Path, devbox_id: str, receipt_path: Path | None) -> Path:
    if receipt_path is not None:
        candidate = receipt_path.expanduser()
        if candidate.is_symlink():
            raise ResumeError(f"handoff receipt must not be a symlink: {candidate}")
        return candidate.resolve()
    try:
        receipts_directory = _workspace_state_path(workspace, HANDOFF_RECEIPTS_DIRECTORY)
    except Exception as error:
        raise ResumeError(str(error)) from error
    return receipts_directory / f"{devbox_id}.json"


def _load_receipt(path: Path, devbox_id: str) -> HandoffReceipt:
    try:
        return _load_handoff_receipt(path, devbox_id)
    except Exception as error:
        raise ResumeError(str(error)) from error


def _require_applied_workspace(
    *,
    workspace: Path,
    receipt: HandoffReceipt,
    fetch_root: Path | None,
) -> Path:
    """Require the exact workspace snapshot that the remote session worked against."""

    if fetch_root is None:
        try:
            fetches_directory = _workspace_state_path(workspace, FETCHES_DIRECTORY)
        except Exception as error:
            raise ResumeError(str(error)) from error
        root = fetches_directory / receipt.devbox_id
    else:
        candidate_root = fetch_root.expanduser()
        if candidate_root.is_symlink():
            raise ResumeError(f"fetch artifact directory is not safe: {candidate_root}")
        root = candidate_root.resolve()
    if root.is_symlink() or not root.is_dir():
        raise ResumeError(
            "no completed applied fetch was found for this handoff; run 'baton fetch' "
            "before restoring its Codex session"
        )
    result_path = root / "result.json"
    remote_workspace = root / "workspace"
    if result_path.is_symlink() or not result_path.is_file():
        raise ResumeError(
            "the fetch artifact has no safe result.json; run 'baton fetch' before "
            "restoring its Codex session"
        )
    if remote_workspace.is_symlink() or not remote_workspace.is_dir():
        raise ResumeError("the fetch artifact has no safe restored workspace")
    try:
        result = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ResumeError("the fetch artifact result.json is not valid JSON") from error
    if not isinstance(result, Mapping):
        raise ResumeError("the fetch artifact result.json is invalid")
    if (
        result.get("devbox_id") != receipt.devbox_id
        or result.get("session_id") != receipt.session_id
        or result.get("applied") is not True
        or result.get("apply_status") != "applied"
    ):
        raise ResumeError(
            "the fetch artifact does not prove this handoff was applied; run 'baton fetch' "
            "before restoring its Codex session"
        )
    archive_value = result.get("archive")
    if not isinstance(archive_value, str) or Path(archive_value).expanduser().resolve() != receipt.archive:
        raise ResumeError("the fetch artifact belongs to a different handoff snapshot")
    remote_workspace_value = result.get("remote_workspace")
    if (
        not isinstance(remote_workspace_value, str)
        or Path(remote_workspace_value).expanduser().resolve() != remote_workspace.resolve()
    ):
        raise ResumeError("the fetch artifact remote workspace path is invalid")
    try:
        local_fingerprint = _workspace_fingerprint(workspace)
        fetched_fingerprint = _workspace_fingerprint(remote_workspace)
    except OSError as error:
        raise ResumeError(f"could not verify fetched workspace state: {error}") from error
    if local_fingerprint != fetched_fingerprint:
        raise ResumeError(
            "the local workspace no longer matches the applied remote result; refusing to "
            "open a session that describes different files"
        )
    return root


def _inspect_snapshot(receipt: HandoffReceipt) -> SnapshotArchive:
    try:
        snapshot = inspect_snapshot_archive(receipt.archive)
    except HandoffError as error:
        raise ResumeError(f"could not inspect handoff snapshot: {error}") from error
    if snapshot.session_id != receipt.session_id:
        raise ResumeError("handoff receipt session does not match its snapshot archive")
    return snapshot


def _rollout_relative_path(snapshot: SnapshotArchive, session_id: str) -> PurePosixPath:
    session = snapshot.manifest.get("session")
    if not isinstance(session, Mapping):
        raise ResumeError("handoff snapshot is missing session metadata")
    archive_path = session.get("rollout_archive_path")
    if not isinstance(archive_path, str):
        raise ResumeError("handoff snapshot has no selected rollout path")
    normalized = _safe_member_path(archive_path)
    if len(normalized.parts) < 3 or normalized.parts[:2] != ("codex", "sessions"):
        raise ResumeError("handoff snapshot selected rollout must be inside codex/sessions")
    if not normalized.name.endswith(f"-{session_id}.jsonl"):
        raise ResumeError("handoff snapshot rollout filename does not match its session ID")
    relative = PurePosixPath(*normalized.parts[1:])
    if relative.name.startswith("."):
        raise ResumeError("handoff snapshot selected rollout has an unsafe filename")
    return relative


def _snapshot_member_bytes(archive_path: Path, member_path: PurePosixPath) -> bytes:
    try:
        with tarfile.open(archive_path, "r:gz") as archive:
            matches = [
                member
                for member in archive.getmembers()
                if _safe_member_path(member.name) == member_path
            ]
            if len(matches) != 1 or not matches[0].isfile() or matches[0].islnk():
                raise ResumeError("handoff snapshot selected rollout is not one regular file")
            source = archive.extractfile(matches[0])
            if source is None:
                raise ResumeError("handoff snapshot selected rollout could not be read")
            with source:
                contents = source.read()
    except (OSError, tarfile.TarError) as error:
        raise ResumeError(f"could not read handoff snapshot rollout: {error}") from error
    if len(contents) > MAX_SESSION_ARCHIVE_BYTES:
        raise ResumeError("handoff snapshot rollout exceeds Baton's 512 MiB safety limit")
    return contents


def _completion_marker(devbox: Any) -> Mapping[str, Any]:
    try:
        raw_marker = _run_checked(
            devbox,
            "sudo",
            "-n",
            "cat",
            "--",
            REMOTE_COMPLETION_MARKER,
        )
    except ResumeError as error:
        raise ResumeError(
            "the remote handoff has not completed yet, or its Devbox is unavailable; "
            "wait for Codex to finish before restoring its session"
        ) from error
    try:
        marker = json.loads(str(raw_marker))
    except json.JSONDecodeError as error:
        raise ResumeError("remote handoff completion marker is not valid JSON") from error
    if not isinstance(marker, Mapping):
        raise ResumeError("remote handoff completion marker is invalid")
    return marker


def _completion_exit_code(marker: Mapping[str, Any]) -> int:
    exit_code = marker.get("exit_code")
    if not isinstance(exit_code, int) or isinstance(exit_code, bool):
        raise ResumeError("remote handoff completion marker has no integer exit code")
    return exit_code


def _marker_git_state(marker: Mapping[str, Any]) -> _GitState | None:
    """Decode the immutable detached-handoff baseline when the Devbox has one."""

    value = marker.get("git_state")
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ResumeError("remote handoff completion marker has invalid Git baseline")
    required = ("head", "branch", "status", "index", "index_flags", "refs")
    if any(not isinstance(value.get(key), str) for key in required):
        raise ResumeError("remote handoff completion marker has invalid Git baseline")
    return _GitState(
        head=value["head"],
        branch=value["branch"],
        status=value["status"],
        index=value["index"],
        index_flags=value["index_flags"],
        refs=value["refs"],
    )


def _remote_path(relative: PurePosixPath) -> str:
    return f"{REMOTE_CODEX_HOME}/{relative.as_posix()}"


def _remote_regular_file_exists(devbox: Any, remote_path: str) -> bool:
    returncode, _, _ = _run_remote_command(devbox, "test", "-f", remote_path)
    return returncode == 0


def _assert_remote_unlinked_regular_file(
    devbox: Any,
    remote_path: str,
    *,
    canonical_root: str | None = None,
) -> None:
    if canonical_root is not None:
        _assert_remote_canonical_path(devbox, remote_path, canonical_root)
    _run_checked(devbox, "test", "-f", remote_path)
    try:
        _run_checked(devbox, "test", "!", "-L", remote_path)
    except ResumeError as error:
        raise ResumeError(
            "remote session state has a symlink; refusing to archive a file that could "
            f"point at credentials: {remote_path}"
        ) from error
    linked_path = _run_checked(
        devbox,
        "find",
        remote_path,
        "-type",
        "f",
        "-links",
        "+1",
        "-print",
        "-quit",
    ).strip()
    if linked_path:
        raise ResumeError(
            "remote session state has a hard-linked file; refusing to archive a file that "
            f"could alias credentials: {remote_path}"
        )


def _assert_remote_canonical_path(
    devbox: Any,
    remote_path: str,
    canonical_root: str,
) -> None:
    """Reject target or ancestor symlinks before reading remote session state."""

    path = PurePosixPath(remote_path)
    root = PurePosixPath(canonical_root)
    try:
        relative = path.relative_to(root)
    except ValueError as error:
        raise ResumeError(
            f"remote session state is outside the expected Codex home: {remote_path}"
        ) from error
    if not relative.parts:
        raise ResumeError("remote session state must name a file inside the Codex home")

    try:
        resolved_root = _run_checked(
            devbox,
            "realpath",
            "-e",
            "--",
            canonical_root,
        ).strip()
        resolved_path = _run_checked(
            devbox,
            "realpath",
            "-e",
            "--",
            remote_path,
        ).strip()
    except ResumeError as error:
        raise ResumeError(
            "remote session state does not have a canonical path inside the Codex home: "
            f"{remote_path}"
        ) from error
    if resolved_root != canonical_root or resolved_path != remote_path:
        raise ResumeError(
            "remote session state resolves through a symlink or outside the Codex home; "
            f"refusing to archive it: {remote_path}"
        )


def _assert_remote_file_size(devbox: Any, remote_path: str, *, label: str) -> int:
    """Read one remote file size before transferring it to the laptop."""

    output = _run_checked(devbox, "stat", "-c", "%s", "--", remote_path).strip()
    if not output.isdecimal():
        raise ResumeError(f"could not determine size of {label}")
    size = int(output)
    if size > MAX_SESSION_ARCHIVE_BYTES:
        raise ResumeError(f"{label} exceeds Baton's 512 MiB safety limit before download")
    return size


def _assert_local_archive_size(archive_path: Path) -> None:
    try:
        if archive_path.is_symlink() or not archive_path.is_file():
            raise ResumeError("downloaded remote session archive is not a safe regular file")
        size = archive_path.stat().st_size
    except OSError as error:
        raise ResumeError("could not inspect downloaded remote session archive") from error
    if size > MAX_SESSION_ARCHIVE_BYTES:
        raise ResumeError(
            "downloaded remote session archive exceeds Baton's 512 MiB safety limit"
        )


def _assert_git_state_matches_remote(
    devbox: Any,
    workspace: Path,
    repository: Mapping[str, Any],
    baseline_state: _GitState | None,
) -> None:
    """Keep a restored conversation aligned with the Git state it observed remotely."""

    expected_repository = repository.get("present")
    if not isinstance(expected_repository, bool):
        raise ResumeError("handoff snapshot has invalid Git metadata")
    remote_state = _assert_remote_git_state_matches_baseline(
        devbox,
        repository,
        baseline_state,
    )
    local_state = _local_git_state(workspace)
    if not expected_repository:
        if local_state is not None:
            raise ResumeError(
                "Git repository state changed during a non-Git handoff; refusing to reopen "
                "a session against a different checkout"
            )
        return
    if remote_state is None or local_state is None:
        raise ResumeError(
            "the remote or local Git checkout is missing; Baton cannot reopen a session "
            "against a different repository state"
        )
    if not _checkout_state_matches(remote_state, local_state):
        raise ResumeError(
            "remote Git state differs from the fetched local checkout (HEAD, branch, index, or "
            "worktree state); "
            "Baton fetches worktree files but does not recreate remote Git mutations. Restore "
            "the matching Git state before reopening this session."
        )


def _assert_remote_git_state_matches_baseline(
    devbox: Any,
    repository: Mapping[str, Any],
    baseline_state: _GitState | None,
) -> _GitState | None:
    """Verify remote Git metadata without requiring the local worktree to be applied yet."""

    expected_repository = repository.get("present")
    if not isinstance(expected_repository, bool):
        raise ResumeError("handoff snapshot has invalid Git metadata")
    remote_state = _remote_git_state(devbox)
    if not expected_repository:
        if baseline_state is not None or remote_state is not None:
            raise ResumeError(
                "Git repository state changed during a non-Git handoff; refusing to reopen "
                "a session against a different checkout"
            )
        return None
    if remote_state is None:
        raise ResumeError(
            "the remote Git checkout is missing; Baton cannot reopen a session against a "
            "different repository state"
        )
    if baseline_state is None:
        raise ResumeError(
            "remote handoff completion marker has no Git baseline; Baton cannot prove that "
            "the session still belongs to the captured repository state"
        )
    if not _git_metadata_matches_baseline(remote_state, baseline_state):
        raise ResumeError(
            "remote Git state changed after handoff (commit, ref, index, or checkout); "
            "Baton fetches worktree files but does not recreate remote Git mutations. Restore "
            "the matching Git state before reopening this session."
        )
    return remote_state


def _assert_prepared_git_state_matches_local(
    workspace: Path,
    repository: Mapping[str, Any],
    remote_state: _GitState | None,
    baseline_state: _GitState | None,
) -> None:
    """Bind locally staged session history to the checkout it was preflighted against."""

    expected_repository = repository.get("present")
    if not isinstance(expected_repository, bool):
        raise ResumeError("handoff snapshot has invalid Git metadata")
    local_state = _local_git_state(workspace)
    if not expected_repository:
        if baseline_state is not None or remote_state is not None or local_state is not None:
            raise ResumeError(
                "Git repository state changed during a non-Git handoff; refusing to reopen "
                "a session against a different checkout"
            )
        return
    if remote_state is None or local_state is None:
        raise ResumeError(
            "the prepared remote or local Git checkout is missing; Baton cannot reopen a "
            "session against a different repository state"
        )
    if baseline_state is None:
        raise ResumeError(
            "prepared session history has no Git baseline; Baton cannot prove that the "
            "session still belongs to the captured repository state"
        )
    if not _git_metadata_matches_baseline(remote_state, baseline_state):
        raise ResumeError(
            "prepared remote Git state changed after handoff (commit, ref, index, or "
            "checkout); refusing to reopen the session"
        )
    if not _checkout_state_matches(remote_state, local_state):
        raise ResumeError(
            "prepared remote Git state differs from the fetched local checkout (HEAD, "
            "branch, index, or worktree state); refusing to reopen the session"
        )


def _git_metadata_matches_baseline(current: _GitState, baseline: _GitState) -> bool:
    """Bind metadata fetch cannot reproduce while allowing remote worktree edits."""

    return (
        current.head == baseline.head
        and current.branch == baseline.branch
        and current.index == baseline.index
        and current.index_flags == baseline.index_flags
        and current.refs == baseline.refs
    )


def _checkout_state_matches(left: _GitState, right: _GitState) -> bool:
    """Compare Git state that fetch can preserve; refs need the remote baseline instead."""

    return (
        left.head == right.head
        and left.branch == right.branch
        and left.status == right.status
        and left.index == right.index
        and left.index_flags == right.index_flags
    )


def _remote_git_state(devbox: Any) -> _GitState | None:
    def command(*arguments: str) -> tuple[str, ...]:
        return (
            "sudo",
            "-n",
            "-u",
            REMOTE_RUNTIME_USER,
            "--",
            "git",
            "-C",
            REMOTE_WORKSPACE,
            *arguments,
        )

    returncode, stdout, _ = _run_remote_command(
        devbox,
        *command("rev-parse", "--is-inside-work-tree"),
    )
    if returncode != 0:
        return None
    if stdout.strip() != "true":
        return None
    repository_root = _run_checked(
        devbox,
        *command("rev-parse", "--show-toplevel"),
    ).strip()
    if repository_root != REMOTE_WORKSPACE:
        raise ResumeError(
            "remote Git checkout is not rooted at /baton/workspace; refusing to restore a "
            "session against a nested or substituted repository"
        )
    return _GitState(
        head=_run_checked(devbox, *command("rev-parse", "HEAD")).strip(),
        branch=_run_checked(
            devbox,
            *command("branch", "--show-current"),
        ).strip(),
        status=_run_checked(
            devbox,
            *command(
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=all",
                "--",
                ".",
                ":(exclude).baton",
            ),
        ),
        index=_run_checked(
            devbox,
            *command("ls-files", "--stage", "-z", "--"),
        ),
        index_flags=_run_checked(
            devbox,
            *command("ls-files", "-v", "-z", "--"),
        ),
        refs=_run_checked(
            devbox,
            *command(
                "for-each-ref",
                "--format=%(refname)%00%(objectname)%00%(symref)%00",
            ),
        ),
    )


def _local_git_state(workspace: Path) -> _GitState | None:
    is_repository = _run_local_git(
        workspace,
        "rev-parse",
        "--is-inside-work-tree",
        allow_failure=True,
    )
    if is_repository is None or is_repository.strip() != "true":
        return None
    repository_root = _run_local_git(workspace, "rev-parse", "--show-toplevel")
    assert repository_root is not None
    if Path(repository_root.strip()).resolve() != workspace:
        raise ResumeError(
            "local Git checkout is not rooted at the requested workspace; refusing to restore "
            "a session against a nested or substituted repository"
        )
    head = _run_local_git(workspace, "rev-parse", "HEAD")
    branch = _run_local_git(workspace, "branch", "--show-current")
    status = _run_local_git(
        workspace,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        "--",
        ".",
        ":(exclude).baton",
    )
    index = _run_local_git(workspace, "ls-files", "--stage", "-z", "--")
    index_flags = _run_local_git(workspace, "ls-files", "-v", "-z", "--")
    refs = _run_local_git(
        workspace,
        "for-each-ref",
        "--format=%(refname)%00%(objectname)%00%(symref)%00",
    )
    assert (
        head is not None
        and branch is not None
        and status is not None
        and index is not None
        and index_flags is not None
        and refs is not None
    )
    return _GitState(
        head=head.strip(),
        branch=branch.strip(),
        status=status,
        index=index,
        index_flags=index_flags,
        refs=refs,
    )


def _run_local_git(
    workspace: Path,
    *arguments: str,
    allow_failure: bool = False,
) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(workspace), *arguments],
            capture_output=True,
            check=False,
        )
    except FileNotFoundError as error:
        raise ResumeError("Git is required to verify the restored session state") from error
    if completed.returncode == 0:
        return completed.stdout.decode("utf-8", errors="replace")
    if allow_failure:
        return None
    detail = completed.stderr.decode("utf-8", errors="replace").strip()
    raise ResumeError(
        f"could not verify local Git state ({' '.join(arguments)}): {detail or 'no command output'}"
    )


def _run_checked(devbox: Any, *command: str) -> str:
    returncode, stdout, stderr = _run_remote_command(devbox, *command)
    if returncode != 0:
        detail = stderr.strip() or stdout.strip() or "no command output"
        raise ResumeError(
            f"remote session command failed (exit {returncode}): {' '.join(command)}: {detail}"
        )
    return stdout


def _run_remote_command(devbox: Any, *command: str) -> tuple[int, str, str]:
    process = devbox.exec(*command, timeout=120)
    stdout = _read_all(process.stdout)
    stderr = _read_all(process.stderr)
    process.wait()
    return _process_returncode(process), stdout, stderr


def _read_remote_session_archive(
    archive_path: Path,
    rollout_relative: PurePosixPath,
) -> tuple[bytes, bytes | None]:
    allowed_files = {rollout_relative.as_posix(), "session_index.jsonl"}
    names: set[str] = set()
    total_size = 0
    contents: dict[str, bytes] = {}
    try:
        with tarfile.open(archive_path, "r:gz") as archive:
            members = archive.getmembers()
            if not members:
                raise ResumeError("remote session archive is empty")
            if len(members) > 2:
                raise ResumeError("remote session archive has too many members")
            for member in members:
                name = _safe_member_path(member.name).as_posix()
                if name in names:
                    raise ResumeError(f"remote session archive has duplicate member: {name}")
                names.add(name)
                if name not in allowed_files:
                    raise ResumeError(
                        f"remote session archive has an unexpected member: {name}; "
                        "only the selected rollout and session index are allowed"
                    )
                if (
                    not member.isfile()
                    or member.issym()
                    or member.islnk()
                    or member.size < 0
                ):
                    raise ResumeError(f"remote session archive member must be a regular file: {name}")
                total_size += member.size
                if total_size > MAX_SESSION_ARCHIVE_BYTES:
                    raise ResumeError("remote session archive expands beyond Baton's 512 MiB safety limit")
                source = archive.extractfile(member)
                if source is None:
                    raise ResumeError(f"remote session archive member could not be read: {name}")
                with source:
                    contents[name] = source.read()
    except (OSError, tarfile.TarError) as error:
        raise ResumeError(f"remote session archive is not a readable .tar.gz: {error}") from error

    rollout_name = rollout_relative.as_posix()
    if names != set(contents) or rollout_name not in contents:
        raise ResumeError("remote session archive is missing the selected rollout")
    return contents[rollout_name], contents.get("session_index.jsonl")


def _stage_prepared_session_payload(
    *,
    fetch_root: Path,
    receipt: HandoffReceipt,
    rollout_relative: PurePosixPath,
    remote_rollout: bytes,
    remote_index: bytes | None,
    remote_exit_code: int,
    remote_git_state: _GitState | None,
    baseline_git_state: _GitState | None,
) -> None:
    """Retain the preflighted session payload next to the fetched workspace."""

    root = _safe_fetch_root(fetch_root)
    directory = root / PREPARED_SESSION_DIRECTORY
    if directory.exists() or directory.is_symlink():
        raise ResumeError(f"prepared session directory already exists: {directory}")
    try:
        directory.mkdir(mode=0o700)
    except OSError as error:
        raise ResumeError(f"could not create prepared session directory: {directory}") from error

    archive_path = directory / PREPARED_SESSION_ARCHIVE
    manifest_path = directory / PREPARED_SESSION_MANIFEST
    try:
        _write_prepared_session_archive(
            archive_path,
            rollout_relative,
            remote_rollout,
            remote_index,
        )
        _assert_local_archive_size(archive_path)
        persisted_rollout, persisted_index = _read_remote_session_archive(
            archive_path,
            rollout_relative,
        )
        if persisted_rollout != remote_rollout or persisted_index != remote_index:
            raise ResumeError("prepared session archive did not preserve the verified history")
        manifest = {
            "format_version": PREPARED_SESSION_FORMAT_VERSION,
            "devbox_id": receipt.devbox_id,
            "session_id": receipt.session_id,
            "archive": str(receipt.archive),
            "rollout_path": rollout_relative.as_posix(),
            "remote_exit_code": remote_exit_code,
            "remote_git_state": _git_state_to_dict(remote_git_state),
            "baseline_git_state": _git_state_to_dict(baseline_git_state),
        }
        _write_bytes_atomically(
            manifest_path,
            (json.dumps(manifest, sort_keys=True) + "\n").encode("utf-8"),
        )
    except Exception as error:
        shutil.rmtree(directory, ignore_errors=True)
        if isinstance(error, ResumeError):
            raise
        raise ResumeError(f"could not stage the verified remote session history: {error}") from error


def _write_prepared_session_archive(
    path: Path,
    rollout_relative: PurePosixPath,
    remote_rollout: bytes,
    remote_index: bytes | None,
) -> None:
    temporary_path = path.parent / f".{path.name}-{uuid4().hex}"
    try:
        with tarfile.open(temporary_path, "w:gz") as archive:
            _add_prepared_session_member(
                archive,
                rollout_relative.as_posix(),
                remote_rollout,
            )
            if remote_index is not None:
                _add_prepared_session_member(archive, "session_index.jsonl", remote_index)
        temporary_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        os.replace(temporary_path, path)
    except (OSError, tarfile.TarError) as error:
        raise ResumeError(f"could not write prepared session archive: {error}") from error
    finally:
        temporary_path.unlink(missing_ok=True)


def _add_prepared_session_member(
    archive: tarfile.TarFile,
    name: str,
    contents: bytes,
) -> None:
    member = tarfile.TarInfo(name)
    member.mode = stat.S_IRUSR | stat.S_IWUSR
    member.size = len(contents)
    archive.addfile(member, io.BytesIO(contents))


def _load_prepared_session_payload(
    *,
    fetch_root: Path,
    receipt: HandoffReceipt,
    rollout_relative: PurePosixPath,
) -> _PreparedSessionPayload | None:
    """Read a preflighted payload, if this fetch was created by a newer Baton."""

    root = _safe_fetch_root(fetch_root)
    directory = root / PREPARED_SESSION_DIRECTORY
    if not directory.exists() and not directory.is_symlink():
        return None
    if directory.is_symlink() or not directory.is_dir():
        raise ResumeError(f"prepared session directory is not safe: {directory}")
    archive_path = directory / PREPARED_SESSION_ARCHIVE
    manifest_path = directory / PREPARED_SESSION_MANIFEST
    if archive_path.is_symlink() or not archive_path.is_file():
        raise ResumeError("prepared session archive is missing or unsafe")
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ResumeError("prepared session manifest is missing or unsafe")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ResumeError("prepared session manifest is not valid JSON") from error
    if not isinstance(manifest, Mapping):
        raise ResumeError("prepared session manifest is invalid")
    if manifest.get("format_version") != PREPARED_SESSION_FORMAT_VERSION:
        raise ResumeError("prepared session manifest has an unsupported format")
    if manifest.get("devbox_id") != receipt.devbox_id or manifest.get("session_id") != receipt.session_id:
        raise ResumeError("prepared session history belongs to a different handoff")
    archive_value = manifest.get("archive")
    if not isinstance(archive_value, str) or Path(archive_value).expanduser().resolve() != receipt.archive:
        raise ResumeError("prepared session history belongs to a different snapshot")
    if manifest.get("rollout_path") != rollout_relative.as_posix():
        raise ResumeError("prepared session rollout path does not match the handoff")
    if manifest.get("remote_exit_code") != 0:
        raise ResumeError("prepared session history came from a failed remote handoff")
    remote_git_state = _git_state_from_manifest(
        manifest.get("remote_git_state"),
        "prepared remote Git state",
    )
    baseline_git_state = _git_state_from_manifest(
        manifest.get("baseline_git_state"),
        "prepared Git baseline",
    )
    _assert_local_archive_size(archive_path)
    remote_rollout, remote_index = _read_remote_session_archive(archive_path, rollout_relative)
    _validate_rollout_bytes(remote_rollout, receipt.session_id, "prepared remote rollout")
    return _PreparedSessionPayload(
        remote_rollout=remote_rollout,
        remote_index=remote_index,
        remote_exit_code=0,
        remote_git_state=remote_git_state,
        baseline_git_state=baseline_git_state,
    )


def _safe_fetch_root(fetch_root: Path) -> Path:
    candidate = fetch_root.expanduser()
    if candidate.is_symlink():
        raise ResumeError(f"fetch artifact directory is not safe: {candidate}")
    root = candidate.resolve()
    if not root.is_dir():
        raise ResumeError(f"fetch artifact directory is not safe: {root}")
    return root


def _git_state_to_dict(state: _GitState | None) -> dict[str, str] | None:
    if state is None:
        return None
    return {
        "head": state.head,
        "branch": state.branch,
        "status": state.status,
        "index": state.index,
        "index_flags": state.index_flags,
        "refs": state.refs,
    }


def _git_state_from_manifest(value: object, label: str) -> _GitState | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ResumeError(f"{label} is invalid")
    required = ("head", "branch", "status", "index", "index_flags", "refs")
    if any(not isinstance(value.get(key), str) for key in required):
        raise ResumeError(f"{label} is invalid")
    return _GitState(
        head=value["head"],
        branch=value["branch"],
        status=value["status"],
        index=value["index"],
        index_flags=value["index_flags"],
        refs=value["refs"],
    )


def _safe_member_path(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if not value or "\x00" in value or path.is_absolute():
        raise ResumeError(f"remote session archive has an unsafe member path: {value!r}")
    parts: list[str] = []
    for part in path.parts:
        if part in ("", "."):
            continue
        if part == "..":
            raise ResumeError(f"remote session archive has an unsafe member path: {value!r}")
        parts.append(part)
    if not parts:
        raise ResumeError(f"remote session archive has an unsafe member path: {value!r}")
    normalized = PurePosixPath(*parts)
    if value != normalized.as_posix():
        raise ResumeError(f"remote session archive has a non-canonical member path: {value!r}")
    return normalized


def _validate_rollout_bytes(contents: bytes, session_id: str, label: str) -> None:
    if not contents:
        raise ResumeError(f"{label} is empty")
    if len(contents) > MAX_SESSION_ARCHIVE_BYTES:
        raise ResumeError(f"{label} exceeds Baton's 512 MiB safety limit")
    if not contents.endswith(b"\n"):
        raise ResumeError(f"{label} has a truncated final JSONL record")
    try:
        text = contents.decode("utf-8")
        lines = text.splitlines()
        first_record = json.loads(lines[0])
        actual_id = str(UUID(str(first_record["payload"]["session_id"])))
        for line in lines:
            if line.strip():
                record = json.loads(line)
                if isinstance(record, Mapping) and record.get("type") == "session_meta":
                    metadata_id = str(UUID(str(record["payload"]["session_id"])))
                    if metadata_id != session_id:
                        raise ResumeError(
                            f"{label} has session metadata for {metadata_id}, not the requested session"
                        )
    except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise ResumeError(f"{label} is not valid Codex JSONL") from error
    if actual_id != session_id:
        raise ResumeError(f"{label} belongs to {actual_id}, not the requested session")


def _existing_directory(path: Path, label: str) -> Path:
    resolved = path.expanduser().resolve()
    if not resolved.is_dir():
        raise ResumeError(f"{label} is not a directory: {resolved}")
    return resolved


def _assert_safe_codex_target(codex_home: Path, target: Path) -> None:
    try:
        relative = target.relative_to(codex_home)
    except ValueError as error:
        raise ResumeError("local Codex target escapes CODEX_HOME") from error
    current = codex_home
    for component in relative.parts:
        current = current / component
        if current.is_symlink():
            raise ResumeError(f"refusing to write session state through symlink: {current}")
        if current.exists() and component != relative.parts[-1] and not current.is_dir():
            raise ResumeError(f"local Codex session parent is not a directory: {current}")


def _assert_no_duplicate_local_rollout(
    codex_home: Path,
    target: Path,
    session_id: str,
) -> None:
    sessions_directory = codex_home / "sessions"
    if not sessions_directory.exists():
        return
    if sessions_directory.is_symlink() or not sessions_directory.is_dir():
        raise ResumeError(f"local Codex sessions directory is unsafe: {sessions_directory}")
    suffix = f"-{session_id}.jsonl"
    conflicts = [
        candidate
        for candidate in sessions_directory.rglob("rollout-*.jsonl")
        if candidate.name.endswith(suffix) and candidate != target
    ]
    if conflicts:
        raise ResumeError(
            "found another local rollout for this session; refusing an ambiguous restore: "
            + ", ".join(str(path) for path in conflicts)
        )


def _read_optional_regular_file(path: Path, label: str) -> bytes | None:
    if not path.exists() and not path.is_symlink():
        return None
    if path.is_symlink() or not path.is_file():
        raise ResumeError(f"{label} is not a safe regular file: {path}")
    try:
        return path.read_bytes()
    except OSError as error:
        raise ResumeError(f"could not read {label}: {path}") from error


def _merge_session_index(
    current_index: bytes | None,
    remote_index: bytes | None,
    session_id: str,
) -> bytes | None:
    if remote_index is None:
        return None
    if len(remote_index) > MAX_SESSION_ARCHIVE_BYTES:
        raise ResumeError("remote session index exceeds Baton's 512 MiB safety limit")
    try:
        remote_lines = remote_index.decode("utf-8").splitlines()
    except UnicodeDecodeError as error:
        raise ResumeError("remote session index is not UTF-8 JSONL") from error

    selected_lines: list[str] = []
    for line in remote_lines:
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as error:
            raise ResumeError("remote session index is not valid JSONL") from error
        if not isinstance(record, Mapping):
            raise ResumeError("remote session index record is not an object")
        if record.get("id") == session_id:
            selected_lines.append(line)
    if not selected_lines:
        return None

    retained_lines: list[str] = []
    if current_index is not None:
        try:
            current_lines = current_index.decode("utf-8").splitlines()
        except UnicodeDecodeError as error:
            raise ResumeError("local session index is not UTF-8 JSONL") from error
        for line in current_lines:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                retained_lines.append(line)
                continue
            if not isinstance(record, Mapping) or record.get("id") != session_id:
                retained_lines.append(line)
    return ("\n".join([*retained_lines, *selected_lines]) + "\n").encode("utf-8")


def _prepare_local_restore(
    *,
    local_codex_home: Path,
    rollout_relative: PurePosixPath,
    session_id: str,
    remote_rollout: bytes,
    remote_index: bytes | None,
    workspace: Path,
) -> _LocalRestorePlan:
    """Read and validate the local files that a restore would replace."""

    rollout_path = local_codex_home.joinpath(*rollout_relative.parts)
    _assert_safe_codex_target(local_codex_home, rollout_path)
    _assert_no_duplicate_local_rollout(local_codex_home, rollout_path, session_id)
    current_rollout = _read_optional_regular_file(rollout_path, "local rollout")
    if current_rollout is not None:
        _validate_rollout_bytes(current_rollout, session_id, "local rollout")
    if current_rollout is not None and not remote_rollout.startswith(current_rollout):
        raise ResumeError(
            "the local rollout changed since handoff; refusing to overwrite a divergent "
            "Codex session"
        )

    index_path = local_codex_home / "session_index.jsonl"
    _assert_safe_codex_target(local_codex_home, index_path)
    current_index = _read_optional_regular_file(index_path, "local session index")
    merged_index = _merge_session_index(current_index, remote_index, session_id)
    return _LocalRestorePlan(
        rollout_path=rollout_path,
        index_path=index_path,
        current_rollout=current_rollout,
        current_index=current_index,
        merged_index=merged_index,
        backups_directory=_workspace_state_directory_or_raise(
            workspace,
            SESSION_BACKUPS_DIRECTORY,
        ),
    )


def _workspace_state_directory_or_raise(workspace: Path, relative_path: Path) -> Path:
    try:
        return _workspace_state_directory(workspace, relative_path)
    except Exception as error:
        raise ResumeError(str(error)) from error


def _restore_locally(
    *,
    rollout_path: Path,
    index_path: Path,
    current_rollout: bytes | None,
    current_index: bytes | None,
    remote_rollout: bytes,
    merged_index: bytes | None,
    backups_directory: Path,
    session_id: str,
) -> tuple[Path | None, Path | None]:
    """Back up and atomically replace local state, rolling back on index failure."""

    if _read_optional_regular_file(rollout_path, "local rollout") != current_rollout:
        raise ResumeError(
            "the local rollout changed while Baton was restoring it; retry after the local "
            "Codex session is stopped"
        )
    if _read_optional_regular_file(index_path, "local session index") != current_index:
        raise ResumeError(
            "the local session index changed while Baton was restoring it; retry after the "
            "local Codex session is stopped"
        )
    backup_root = backups_directory / f"{session_id}-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{uuid4().hex[:8]}"
    if backup_root.exists() or backup_root.is_symlink():
        raise ResumeError(f"refusing to overwrite session backup: {backup_root}")
    backup_root.mkdir(mode=0o700)

    backup_path: Path | None = None
    index_backup_path: Path | None = None
    try:
        if current_rollout is not None:
            backup_path = backup_root / "rollout.jsonl"
            _write_bytes_atomically(backup_path, current_rollout)
        index_changes = merged_index is not None and merged_index != current_index
        if index_changes and current_index is not None:
            index_backup_path = backup_root / "session_index.jsonl"
            _write_bytes_atomically(index_backup_path, current_index)

        rollout_changes = current_rollout != remote_rollout
        if rollout_changes:
            _write_bytes_atomically(rollout_path, remote_rollout)
        try:
            if index_changes and merged_index is not None:
                _write_bytes_atomically(index_path, merged_index)
        except Exception as error:
            rollback_error = _rollback_local_file(rollout_path, current_rollout) if rollout_changes else None
            if rollback_error is not None:
                raise ResumeError(
                    f"could not restore local session index: {error}; additionally, "
                    f"could not roll back rollout: {rollback_error}"
                ) from error
            raise ResumeError(f"could not restore local session index: {error}") from error
    except ResumeError:
        raise
    except OSError as error:
        raise ResumeError(f"could not restore local Codex session: {error}") from error

    if backup_path is None and index_backup_path is None:
        shutil.rmtree(backup_root)
    return backup_path, index_backup_path


def _rollback_local_file(path: Path, previous_contents: bytes | None) -> OSError | None:
    try:
        if previous_contents is None:
            path.unlink(missing_ok=True)
        else:
            _write_bytes_atomically(path, previous_contents)
    except OSError as error:
        return error
    return None

def _write_bytes_atomically(path: Path, contents: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{path.name}-",
            dir=path.parent,
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            temporary_file.write(contents)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        temporary_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _launch_local_codex(session_id: str, workspace: Path, codex_home: Path) -> int:
    environment = os.environ.copy()
    environment["CODEX_HOME"] = str(codex_home)
    try:
        completed = subprocess.run(
            ["codex", "resume", session_id],
            cwd=workspace,
            env=environment,
            check=False,
        )
    except FileNotFoundError as error:
        raise ResumeError(
            "the remote session was restored, but the local Codex CLI is not on PATH"
        ) from error
    return completed.returncode


def _read_all(stream: Any) -> str:
    value = stream.read()
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _process_returncode(process: Any) -> int:
    returncode = process.returncode
    if not isinstance(returncode, int):
        raise ResumeError("remote process did not report an exit code")
    return returncode


def _remove_remote_artifacts(
    devbox: Any,
    remote_archive: str,
    remote_index_directory: str | None,
) -> ResumeError | None:
    commands: list[tuple[str, ...]] = [("rm", "-f", "--", remote_archive)]
    if remote_index_directory is not None:
        commands.extend(
            [
                (
                    "rm",
                    "-f",
                    "--",
                    f"{remote_index_directory}/session_index.jsonl",
                ),
                ("rmdir", "--", remote_index_directory),
            ]
        )
    errors: list[str] = []
    for command in commands:
        try:
            _run_checked(devbox, *command)
        except ResumeError as error:
            errors.append(str(error))
    if errors:
        return ResumeError(
            "could not remove temporary remote session artifacts: " + "; ".join(errors)
        )
    return None
