"""Retrieve a completed Runloop Devbox workspace and apply it when safe."""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import UUID, uuid4

from .handoff import (
    REMOTE_COMPLETION_MARKER,
    HandoffError,
    SnapshotArchive,
    inspect_snapshot_archive,
)
from .runloop import RunloopClientError, load_runloop_client, open_devbox
from .snapshot import (
    EXCLUDED_PATH_COMPONENTS,
    KNOWN_SECRET_DIRECTORIES,
    KNOWN_SECRET_FILENAMES,
)

RECEIPT_FORMAT_VERSION = 2
HANDOFF_RECEIPTS_DIRECTORY = Path(".baton") / "handoffs"
FETCHES_DIRECTORY = Path(".baton") / "fetches"
REMOTE_ROOT = "/baton"
INTERNAL_WORKSPACE_COMPONENTS = frozenset({".baton", ".git", ".omc", ".omx"})
NATIVE_ARTIFACT_SUFFIXES = frozenset({".dll", ".dylib", ".node", ".pyd", ".so"})
NATIVE_BUILD_COMPONENTS = frozenset({".venv", "build", "dist", "node_modules", "venv"})
MAX_ARCHIVE_MEMBERS = 20_000
MAX_ARCHIVE_UNCOMPRESSED_BYTES = 2 * 1024 * 1024 * 1024
DEVBOX_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")


class FetchError(RuntimeError):
    """Raised when a remote workspace cannot be fetched safely."""


@dataclass(frozen=True)
class HandoffReceipt:
    """Local binding between a detached Devbox and its immutable snapshot."""

    path: Path
    devbox_id: str
    session_id: str
    archive: Path
    workspace: Path
    session_title: str | None = None


@dataclass(frozen=True)
class FetchResult:
    """A completed Devbox workspace staged locally, with optional local application."""

    devbox_id: str
    archive: Path
    fetch_root: Path
    baseline_workspace: Path
    remote_workspace: Path
    patch_path: Path
    changed_files: tuple[str, ...]
    remote_exit_code: int
    applied: bool
    session_title: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "devbox_id": self.devbox_id,
            "archive": str(self.archive),
            "fetch_root": str(self.fetch_root),
            "baseline_workspace": str(self.baseline_workspace),
            "remote_workspace": str(self.remote_workspace),
            "patch_path": str(self.patch_path),
            "changed_files": list(self.changed_files),
            "remote_exit_code": self.remote_exit_code,
            "applied": self.applied,
            "session_title": self.session_title,
        }


def write_handoff_receipt(
    *,
    devbox_id: str,
    session_id: str,
    archive_path: Path,
    workspace: Path,
) -> Path:
    """Persist the immutable baseline needed by a later fetch."""

    normalized_devbox_id = _normalize_devbox_id(devbox_id)
    normalized_session_id = _normalize_session_id(session_id)
    source_workspace = _existing_directory(workspace, "workspace")
    try:
        snapshot = inspect_snapshot_archive(archive_path)
    except HandoffError as error:
        raise FetchError(f"cannot write a receipt for an invalid snapshot: {error}") from error
    if snapshot.session_id != normalized_session_id:
        raise FetchError("handoff receipt session does not match the snapshot archive")

    receipts_directory = _workspace_state_directory(
        source_workspace,
        HANDOFF_RECEIPTS_DIRECTORY,
    )
    receipt_path = receipts_directory / f"{normalized_devbox_id}.json"
    if receipt_path.exists() or receipt_path.is_symlink():
        raise FetchError(f"refusing to overwrite existing handoff receipt: {receipt_path}")
    _write_json_atomically(
        receipt_path,
        {
            "format_version": RECEIPT_FORMAT_VERSION,
            "devbox_id": normalized_devbox_id,
            "session_id": normalized_session_id,
            "archive": str(snapshot.path),
            "workspace": str(source_workspace),
        },
    )
    return receipt_path


def list_handoff_receipts(*, workspace: Path) -> tuple[HandoffReceipt, ...]:
    """Return valid detached-handoff receipts for a workspace, newest first."""

    source_workspace = _existing_directory(workspace, "workspace")
    receipts_directory = _workspace_state_path(
        source_workspace,
        HANDOFF_RECEIPTS_DIRECTORY,
    )
    if not receipts_directory.is_dir():
        return ()

    receipts: list[tuple[int, HandoffReceipt]] = []
    for receipt_path in receipts_directory.glob("*.json"):
        try:
            if receipt_path.is_symlink():
                continue
            devbox_id = _normalize_devbox_id(receipt_path.stem)
            receipt = _load_handoff_receipt(receipt_path, devbox_id)
            modified_at = receipt_path.stat().st_mtime_ns
        except (FetchError, OSError):
            continue
        if receipt.workspace == source_workspace:
            receipts.append((modified_at, receipt))
    return tuple(
        receipt
        for _, receipt in sorted(
            receipts,
            key=lambda entry: (entry[0], entry[1].devbox_id),
            reverse=True,
        )
    )


def fetch_workspace(
    *,
    devbox_id: str,
    workspace: Path,
    receipt_path: Path | None = None,
    output: Path | None = None,
    apply_changes: bool = True,
    runloop_client: Any | None = None,
    before_apply: Callable[[FetchResult], None] | None = None,
) -> FetchResult:
    """Fetch a completed Devbox workspace and apply its patch by default.

    The immutable handoff snapshot is compared with the local workspace before any
    mutation.  If the project changed after handoff, application is refused and the
    staged fetch artifact remains available for review.
    """

    normalized_devbox_id = _normalize_devbox_id(devbox_id)
    source_workspace = _existing_directory(workspace, "workspace")
    resolved_receipt_path = (
        receipt_path.expanduser().resolve()
        if receipt_path is not None
        else _workspace_state_path(source_workspace, HANDOFF_RECEIPTS_DIRECTORY)
        / f"{normalized_devbox_id}.json"
    )
    receipt = _load_handoff_receipt(resolved_receipt_path, normalized_devbox_id)
    if receipt.workspace != source_workspace:
        raise FetchError(
            "handoff receipt belongs to a different workspace; fetch from the original "
            "workspace so its snapshot remains the comparison baseline"
        )
    fetch_root = _resolve_fetch_output(source_workspace, normalized_devbox_id, output)

    try:
        client = runloop_client if runloop_client is not None else load_runloop_client()
        devbox = open_devbox(client, normalized_devbox_id)
    except RunloopClientError as error:
        raise FetchError(str(error)) from error
    except Exception as error:
        raise FetchError(f"Runloop Devbox lookup failed: {error}") from error

    remote_exit_code = _require_completion_marker(devbox)
    fetch_root.parent.mkdir(parents=True, exist_ok=True)
    staging_root = Path(
        tempfile.mkdtemp(prefix=f".{fetch_root.name}-", dir=fetch_root.parent)
    )
    remote_archive = f"/tmp/baton-fetch-{uuid4().hex}.tar.gz"
    primary_error: BaseException | None = None
    remote_archive_removed = False

    try:
        baseline_workspace = staging_root / "baseline"
        remote_workspace = staging_root / "workspace"
        downloaded_archive = staging_root / "remote-workspace.tar.gz"
        _extract_snapshot_workspace(receipt.archive, baseline_workspace)
        _run_checked(
            devbox,
            "tar",
            "--exclude=workspace/.git",
            "--exclude=workspace/.baton",
            "--exclude=workspace/.omc",
            "--exclude=workspace/.omx",
            "-C",
            REMOTE_ROOT,
            "-czf",
            remote_archive,
            "workspace",
        )
        devbox.filesystem.copy_to_local(remote_archive, downloaded_archive)
        cleanup_error = _remove_remote_archive(devbox, remote_archive)
        if cleanup_error is not None:
            raise cleanup_error
        remote_archive_removed = True
        _extract_workspace_tarball(downloaded_archive, remote_workspace)
        downloaded_archive.unlink(missing_ok=True)

        patch_path = staging_root / "changes.patch"
        patch_bytes = _build_patch(staging_root)
        patch_path.write_bytes(patch_bytes)
        changed_files = _collect_changed_files(patch_bytes)
        result = FetchResult(
            devbox_id=normalized_devbox_id,
            archive=receipt.archive,
            fetch_root=fetch_root,
            baseline_workspace=fetch_root / "baseline",
            remote_workspace=fetch_root / "workspace",
            patch_path=fetch_root / "changes.patch",
            changed_files=changed_files,
            remote_exit_code=remote_exit_code,
            applied=False,
            session_title=receipt.session_title,
        )
        should_apply = apply_changes and remote_exit_code == 0
        _write_fetch_result(
            staging_root / "result.json",
            result,
            receipt.session_id,
            applied=None if should_apply else False,
            apply_status=(
                "pending"
                if should_apply
                else "review_only" if not apply_changes else "remote_exit_nonzero"
            ),
        )
        os.replace(staging_root, fetch_root)

        if not should_apply:
            return result

        if before_apply is not None:
            try:
                before_apply(result)
            except FetchError as error:
                try:
                    _write_fetch_result(
                        fetch_root / "result.json",
                        result,
                        receipt.session_id,
                        applied=False,
                        apply_status="session_preflight_failed",
                    )
                except OSError:
                    pass
                raise FetchError(
                    "remote changes were fetched to "
                    f"{fetch_root}, but their Codex session history failed validation; "
                    f"the workspace was left unchanged: {error}"
                ) from error
            except Exception as error:
                try:
                    _write_fetch_result(
                        fetch_root / "result.json",
                        result,
                        receipt.session_id,
                        applied=False,
                        apply_status="session_preflight_failed",
                    )
                except OSError:
                    pass
                raise FetchError(
                    "remote changes were fetched to "
                    f"{fetch_root}, but Baton's session-history preflight failed; "
                    f"the workspace was left unchanged: {error}"
                ) from error

        try:
            _apply_workspace_patch(
                source_workspace,
                result.baseline_workspace,
                result.patch_path,
                snapshot_archive=receipt.archive,
                ignored_paths=(fetch_root,),
            )
        except FetchError as error:
            try:
                _write_fetch_result(
                    fetch_root / "result.json",
                    result,
                    receipt.session_id,
                    applied=False,
                    apply_status="apply_failed",
                )
            except OSError:
                pass
            raise FetchError(
                "remote changes were fetched to "
                f"{fetch_root} but were not applied: {error}"
            ) from error
        result = FetchResult(
            devbox_id=result.devbox_id,
            archive=result.archive,
            fetch_root=result.fetch_root,
            baseline_workspace=result.baseline_workspace,
            remote_workspace=result.remote_workspace,
            patch_path=result.patch_path,
            changed_files=result.changed_files,
            remote_exit_code=result.remote_exit_code,
            applied=True,
            session_title=result.session_title,
        )
        try:
            _write_fetch_result(
                fetch_root / "result.json",
                result,
                receipt.session_id,
                applied=True,
                apply_status="applied",
            )
        except OSError as error:
            raise FetchError(
                f"remote changes were applied to {source_workspace}, but Baton could not "
                f"update {fetch_root / 'result.json'}: {error}. Do not rerun fetch; inspect "
                "the workspace and saved artifact instead."
            ) from error
        return result
    except FetchError as error:
        primary_error = error
        raise
    except Exception as error:
        fetch_error = FetchError(f"Runloop fetch failed: {error}")
        primary_error = fetch_error
        raise fetch_error from error
    except BaseException as error:
        primary_error = error
        raise
    finally:
        cleanup_error = (
            None
            if remote_archive_removed
            else _remove_remote_archive(devbox, remote_archive)
        )
        if staging_root.exists():
            shutil.rmtree(staging_root)
        if cleanup_error is not None:
            if primary_error is None:
                raise cleanup_error
            if isinstance(primary_error, FetchError):
                primary_error.args = (f"{primary_error}; additionally, {cleanup_error}",)
            elif hasattr(primary_error, "add_note"):
                primary_error.add_note(str(cleanup_error))


def _load_handoff_receipt(path: Path, devbox_id: str) -> HandoffReceipt:
    if not path.is_file():
        raise FetchError(
            "no handoff receipt found for this Devbox. Run 'baton handoff --detach' "
            "from this workspace first, or pass --receipt."
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise FetchError(f"handoff receipt is not valid JSON: {path}") from error
    if not isinstance(payload, Mapping) or payload.get("format_version") != RECEIPT_FORMAT_VERSION:
        raise FetchError(f"handoff receipt has an unsupported format: {path}")
    if payload.get("devbox_id") != devbox_id:
        raise FetchError("handoff receipt does not belong to the requested Devbox")
    session_id = _normalize_session_id(payload.get("session_id"))
    archive_value = payload.get("archive")
    workspace_value = payload.get("workspace")
    if not isinstance(archive_value, str) or not isinstance(workspace_value, str):
        raise FetchError("handoff receipt is missing its archive or workspace path")
    archive_path = Path(archive_value).expanduser().resolve()
    receipt_workspace = Path(workspace_value).expanduser().resolve()
    try:
        snapshot = inspect_snapshot_archive(archive_path)
    except HandoffError as error:
        raise FetchError(f"handoff receipt references an invalid snapshot: {error}") from error
    if snapshot.session_id != session_id:
        raise FetchError("handoff receipt session does not match its snapshot archive")
    return HandoffReceipt(
        path=path,
        devbox_id=devbox_id,
        session_id=session_id,
        archive=snapshot.path,
        workspace=receipt_workspace,
        session_title=_snapshot_session_title(snapshot),
    )


def _snapshot_session_title(snapshot: SnapshotArchive) -> str | None:
    """Read a session title from new manifests or old archived index records."""

    session = snapshot.manifest.get("session")
    if isinstance(session, Mapping):
        title = session.get("title")
        if isinstance(title, str) and title.strip():
            return title

    try:
        with tarfile.open(snapshot.path, "r:gz") as archive:
            index_file = archive.extractfile("codex/session_index.jsonl")
            if index_file is None:
                return None
            title = None
            for raw_line in index_file:
                try:
                    record = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue
                if record.get("id") != snapshot.session_id:
                    continue
                candidate = record.get("thread_name")
                if isinstance(candidate, str) and candidate.strip():
                    title = candidate
            return title
    except (KeyError, OSError, tarfile.TarError, UnicodeDecodeError):
        return None


def _require_completion_marker(devbox: Any) -> int:
    try:
        raw_marker = _run_checked(
            devbox,
            "sudo",
            "-n",
            "cat",
            "--",
            REMOTE_COMPLETION_MARKER,
        )
    except Exception as error:
        raise FetchError(
            "the remote handoff has not completed yet, or its Devbox is unavailable; "
            "wait for Codex to finish before fetching"
        ) from error
    try:
        marker = json.loads(str(raw_marker))
    except json.JSONDecodeError as error:
        raise FetchError("remote handoff completion marker is not valid JSON") from error
    if not isinstance(marker, Mapping):
        raise FetchError("remote handoff completion marker is invalid")
    exit_code = marker.get("exit_code")
    if not isinstance(exit_code, int) or isinstance(exit_code, bool):
        raise FetchError("remote handoff completion marker has no integer exit code")
    return exit_code


def _extract_snapshot_workspace(archive_path: Path, destination: Path) -> None:
    try:
        with tarfile.open(archive_path, "r:gz") as archive:
            workspace_members = [
                member
                for member in archive.getmembers()
                if _is_workspace_path(_safe_tar_path(member.name))
            ]
            _extract_workspace_members(archive, destination, workspace_members)
    except (OSError, tarfile.TarError) as error:
        raise FetchError(f"could not read snapshot workspace: {archive_path}") from error


def _extract_workspace_tarball(archive_path: Path, destination: Path) -> None:
    try:
        with tarfile.open(archive_path, "r:gz") as archive:
            _extract_workspace_members(archive, destination)
    except (OSError, tarfile.TarError) as error:
        raise FetchError(f"downloaded workspace is not a readable .tar.gz: {archive_path}") from error


def _extract_workspace_members(
    archive: tarfile.TarFile,
    destination: Path,
    members: list[tarfile.TarInfo] | None = None,
) -> None:
    members = archive.getmembers() if members is None else members
    _validate_workspace_members(members)
    destination.mkdir(parents=True, exist_ok=False)
    normalized_members = [(_safe_tar_path(member.name), member) for member in members]

    for normalized_name, member in normalized_members:
        if member.isdir():
            _destination_path(destination, normalized_name).mkdir(parents=True, exist_ok=True)
    for normalized_name, member in normalized_members:
        if not member.isfile():
            continue
        target = _destination_path(destination, normalized_name)
        target.parent.mkdir(parents=True, exist_ok=True)
        _assert_no_symlink_parent(destination, target.parent)
        source = archive.extractfile(member)
        if source is None:
            raise FetchError(f"workspace archive member could not be read: {member.name}")
        with source, target.open("xb") as handle:
            shutil.copyfileobj(source, handle)
        target.chmod(member.mode & 0o777)
    for normalized_name, member in normalized_members:
        if not member.issym():
            continue
        target = _destination_path(destination, normalized_name)
        target.parent.mkdir(parents=True, exist_ok=True)
        _assert_no_symlink_parent(destination, target.parent)
        target.symlink_to(member.linkname)


def _validate_workspace_members(members: list[tarfile.TarInfo]) -> None:
    if not members:
        raise FetchError("workspace archive is empty")
    if len(members) > MAX_ARCHIVE_MEMBERS:
        raise FetchError(f"workspace archive has too many members (limit {MAX_ARCHIVE_MEMBERS})")
    names: set[str] = set()
    total_size = 0
    for member in members:
        normalized_name = _safe_tar_path(member.name)
        name = normalized_name.as_posix()
        if name in names:
            raise FetchError(f"workspace archive has duplicate member: {name}")
        names.add(name)
        if not _is_workspace_path(normalized_name):
            raise FetchError(f"workspace archive contains content outside workspace: {name}")
        if not (member.isdir() or member.isfile() or member.issym()):
            raise FetchError(f"workspace archive contains unsupported member type: {name}")
        if member.isfile():
            total_size += member.size
            if total_size > MAX_ARCHIVE_UNCOMPRESSED_BYTES:
                raise FetchError("workspace archive expands beyond Baton's 2 GiB safety limit")
        _reject_workspace_path(normalized_name)

    for member in members:
        normalized_name = _safe_tar_path(member.name)
        if not member.issym():
            continue
        target = _resolve_tar_link(normalized_name.parent, member.linkname)
        if not _is_workspace_path(target):
            raise FetchError(
                "workspace archive link escapes the restored workspace: "
                f"{normalized_name} -> {target}"
            )
        target_name = target.as_posix()
        if target_name not in names and not any(name.startswith(f"{target_name}/") for name in names):
            raise FetchError(
                "workspace archive link targets content that is absent from the archive: "
                f"{normalized_name} -> {target}"
            )


def _reject_workspace_path(path: PurePosixPath) -> None:
    relative_parts = path.parts[1:]
    if any(component in INTERNAL_WORKSPACE_COMPONENTS for component in relative_parts):
        raise FetchError(f"workspace archive contains Baton-internal metadata: {path}")
    if _looks_like_secret_path(relative_parts):
        raise FetchError(
            "workspace archive contains a credential-like path: "
            f"{path}. Runtime credentials must stay in Runloop Secrets."
        )
    if any(component in NATIVE_BUILD_COMPONENTS for component in relative_parts):
        raise FetchError(
            "workspace archive contains dependency/build output that cannot cross macOS arm64 "
            f"to Linux x86_64: {path}. Baton will not rebuild native dependencies."
        )
    if relative_parts and relative_parts[-1] == "binding.gyp":
        raise FetchError(
            "workspace archive contains binding.gyp, which requires a native Node build. "
            "Baton will not rebuild native dependencies."
        )
    if path.suffix.lower() in NATIVE_ARTIFACT_SUFFIXES:
        raise FetchError(
            "workspace archive contains a native binary that cannot cross macOS arm64 to Linux "
            f"x86_64: {path}"
        )


def _looks_like_secret_path(parts: tuple[str, ...]) -> bool:
    return any(
        component in KNOWN_SECRET_DIRECTORIES
        or component in KNOWN_SECRET_FILENAMES
        or component.endswith((".key", ".pem"))
        or (component.startswith(".env") and component != ".env.example")
        for component in parts
    )


def _destination_path(destination: Path, archive_path: PurePosixPath) -> Path:
    relative = archive_path.parts[1:]
    return destination.joinpath(*relative) if relative else destination


def _assert_no_symlink_parent(destination: Path, parent: Path) -> None:
    current = destination
    for component in parent.relative_to(destination).parts:
        current = current / component
        if current.is_symlink():
            raise FetchError(f"workspace archive writes through a symlinked directory: {current}")


def _build_patch(staging_root: Path) -> bytes:
    result = subprocess.run(
        [
            "git",
            "diff",
            "--no-index",
            "--binary",
            "--no-ext-diff",
            "--src-prefix=a/",
            "--dst-prefix=b/",
            "baseline",
            "workspace",
        ],
        cwd=staging_root,
        capture_output=True,
        check=False,
    )
    if result.returncode not in (0, 1):
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise FetchError(f"could not build workspace diff: {detail or 'no command output'}")
    return _normalize_patch_paths(result.stdout)


def _normalize_patch_paths(patch: bytes) -> bytes:
    normalized: list[bytes] = []
    for line in patch.splitlines(keepends=True):
        if line.startswith(
            (b"diff --git ", b"--- a/", b'--- "a/', b"+++ b/", b'+++ "b/', b"Binary files ")
        ):
            for prefix in (b"a/baseline/", b"a/workspace/", b"b/baseline/", b"b/workspace/"):
                line = line.replace(prefix, prefix[:2], 1)
        normalized.append(line)
    return b"".join(normalized)


def _collect_changed_files(patch: bytes) -> tuple[str, ...]:
    changed: list[str] = []
    for line in patch.decode("utf-8", errors="replace").splitlines():
        if not line.startswith("diff --git a/"):
            continue
        left, separator, right = line[len("diff --git a/") :].partition(" b/")
        if not separator:
            continue
        candidate = right or left
        if candidate and candidate not in changed:
            changed.append(candidate)
    return tuple(changed)


def _write_fetch_result(
    path: Path,
    result: FetchResult,
    session_id: str,
    *,
    applied: bool | None,
    apply_status: str,
) -> None:
    session_restore = _existing_session_restore(path)
    payload: dict[str, object] = {
        "session_id": session_id,
        **result.to_dict(),
        "applied": applied,
        "apply_status": apply_status,
    }
    if session_restore is not None:
        payload["session_restore"] = session_restore
    _write_json_atomically(
        path,
        payload,
    )


def record_fetch_session_restore(
    *,
    fetch_root: Path,
    session_restore: Mapping[str, object],
) -> None:
    """Persist the outcome of restoring a fetched Codex session history.

    Workspace retrieval and session restoration have separate failure modes.  The
    artifact keeps both outcomes so a later ``baton resume`` has a clear recovery
    path instead of treating an applied workspace as a fully restored handoff.
    """

    candidate_root = fetch_root.expanduser()
    if candidate_root.is_symlink():
        raise FetchError(f"fetch artifact directory is not safe: {candidate_root}")
    root = candidate_root.resolve()
    result_path = root / "result.json"
    if not root.is_dir():
        raise FetchError(f"fetch artifact directory is not safe: {root}")
    if result_path.is_symlink() or not result_path.is_file():
        raise FetchError(f"fetch artifact has no safe result.json: {result_path}")
    try:
        payload = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise FetchError("fetch artifact result.json is not valid JSON") from error
    if not isinstance(payload, Mapping):
        raise FetchError("fetch artifact result.json is invalid")
    _write_json_atomically(
        result_path,
        {**payload, "session_restore": dict(session_restore)},
    )


def _existing_session_restore(path: Path) -> dict[str, object] | None:
    """Keep an already-recorded preflight result when fetch updates apply status."""

    if path.is_symlink() or not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, Mapping):
        return None
    session_restore = payload.get("session_restore")
    if not isinstance(session_restore, Mapping):
        return None
    return dict(session_restore)


def _apply_workspace_patch(
    workspace: Path,
    baseline_workspace: Path,
    patch_path: Path,
    *,
    snapshot_archive: Path | None = None,
    ignored_paths: tuple[Path, ...] = (),
) -> None:
    """Apply a prepared patch only when the local handoff baseline still matches."""

    _assert_workspace_matches_baseline(
        workspace,
        baseline_workspace,
        snapshot_archive=snapshot_archive,
        ignored_paths=ignored_paths,
    )
    if patch_path.stat().st_size == 0:
        return

    command = ["git", "-C", str(workspace), "apply", "--binary"]
    if not (workspace / ".git").exists():
        command.append("--no-index")
    command.append(str(patch_path))
    try:
        result = subprocess.run(command, capture_output=True, check=False)
    except FileNotFoundError as error:
        raise FetchError("Git is required to apply fetched changes") from error
    if result.returncode == 0:
        return

    detail = result.stderr.decode("utf-8", errors="replace").strip()
    if not detail:
        detail = result.stdout.decode("utf-8", errors="replace").strip()
    raise FetchError(f"could not apply the remote patch: {detail or 'no command output'}")


def _assert_workspace_matches_baseline(
    workspace: Path,
    baseline_workspace: Path,
    *,
    snapshot_archive: Path | None,
    ignored_paths: tuple[Path, ...],
) -> None:
    """Reject auto-apply when portable workspace content changed after handoff."""

    if _workspace_fingerprint(workspace, ignored_paths=ignored_paths) != _workspace_fingerprint(
        baseline_workspace
    ):
        raise FetchError(
            "local workspace changed since the handoff baseline; refusing to overwrite it. "
            "Inspect the saved fetch artifact instead."
        )
    if snapshot_archive is not None:
        _assert_git_state_matches_snapshot(workspace, snapshot_archive)


def _workspace_fingerprint(
    workspace: Path,
    *,
    ignored_paths: tuple[Path, ...] = (),
) -> dict[str, tuple[object, ...]]:
    """Match the snapshot's portable-content boundary without following symlinks."""

    fingerprint: dict[str, tuple[object, ...]] = {}
    for root, dirnames, filenames in os.walk(workspace, topdown=True, followlinks=False):
        root_path = Path(root)
        retained_directories: list[str] = []
        for dirname in dirnames:
            candidate = root_path / dirname
            relative_path = candidate.relative_to(workspace)
            if _is_snapshot_excluded(relative_path) or _is_ignored_workspace_path(
                candidate, ignored_paths
            ):
                continue
            if candidate.is_symlink():
                fingerprint[f"symlink:{relative_path.as_posix()}"] = _workspace_entry_signature(
                    candidate
                )
            else:
                retained_directories.append(dirname)
        dirnames[:] = retained_directories

        for filename in filenames:
            candidate = root_path / filename
            relative_path = candidate.relative_to(workspace)
            if _is_snapshot_excluded(relative_path) or _is_ignored_workspace_path(
                candidate, ignored_paths
            ):
                continue
            fingerprint[f"entry:{relative_path.as_posix()}"] = _workspace_entry_signature(candidate)
    return fingerprint


def _is_snapshot_excluded(relative_path: Path) -> bool:
    return any(
        component in EXCLUDED_PATH_COMPONENTS
        or component.endswith(".egg-info")
        or (component.startswith(".env") and component != ".env.example")
        for component in relative_path.parts
    )


def _is_ignored_workspace_path(path: Path, ignored_paths: tuple[Path, ...]) -> bool:
    return any(_path_is_within(path, ignored_path) for ignored_path in ignored_paths)


def _path_is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _workspace_entry_signature(path: Path) -> tuple[object, ...]:
    metadata = path.lstat()
    mode = stat.S_IMODE(metadata.st_mode)
    if stat.S_ISLNK(metadata.st_mode):
        return ("symlink", os.readlink(path), mode)
    if stat.S_ISREG(metadata.st_mode):
        digest = sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        return ("file", digest.hexdigest(), mode)
    return ("other", mode)


def _assert_git_state_matches_snapshot(workspace: Path, snapshot_archive: Path) -> None:
    """Keep auto-apply on the same Git checkout and index captured at handoff."""

    try:
        repository = inspect_snapshot_archive(snapshot_archive).repository
    except HandoffError as error:
        raise FetchError(f"could not verify the handoff Git state: {error}") from error
    expected_repository = repository.get("present")
    if not isinstance(expected_repository, bool):
        raise FetchError("handoff snapshot has invalid Git metadata")

    git_path = workspace / ".git"
    if not expected_repository:
        if git_path.exists() or git_path.is_symlink():
            raise FetchError(
                "the local workspace is now a Git repository, but the handoff snapshot was not; "
                "refusing to apply onto a different Git state"
            )
        return
    if not git_path.exists() and not git_path.is_symlink():
        raise FetchError(
            "the handoff snapshot was a Git repository, but the local workspace is not; "
            "refusing to apply onto a different Git state"
        )

    repository_root = _require_local_git_output(workspace, "rev-parse", "--show-toplevel")
    if Path(repository_root.decode("utf-8").strip()).resolve() != workspace:
        raise FetchError("local workspace is no longer the Git worktree root from the handoff")

    expected_head = repository.get("head")
    if not isinstance(expected_head, str) or not expected_head:
        raise FetchError("handoff snapshot has no Git HEAD to verify")
    current_head = _require_local_git_output(workspace, "rev-parse", "HEAD").decode(
        "utf-8"
    ).strip()
    if current_head != expected_head:
        raise FetchError("local Git HEAD changed since handoff; refusing to apply remote changes")

    expected_branch = repository.get("branch")
    if expected_branch is not None and not isinstance(expected_branch, str):
        raise FetchError("handoff snapshot has invalid Git branch metadata")
    current_branch = _optional_local_git_output(workspace, "branch", "--show-current")
    if current_branch != expected_branch:
        raise FetchError("local Git branch changed since handoff; refusing to apply remote changes")

    expected_staged = _read_snapshot_git_artifact(snapshot_archive, "git/staged.patch")
    current_staged = _require_local_git_output(
        workspace,
        "diff",
        "--binary",
        "--no-ext-diff",
        "--cached",
        "--root",
    )
    if current_staged != expected_staged:
        raise FetchError("local Git index changed since handoff; refusing to apply remote changes")

    expected_unstaged = _read_snapshot_git_artifact(snapshot_archive, "git/unstaged.patch")
    current_unstaged = _require_local_git_output(
        workspace,
        "diff",
        "--binary",
        "--no-ext-diff",
    )
    if current_unstaged != expected_unstaged:
        raise FetchError("local Git worktree changed since handoff; refusing to apply remote changes")


def _read_snapshot_git_artifact(archive_path: Path, member_name: str) -> bytes:
    try:
        with tarfile.open(archive_path, "r:gz") as archive:
            source = archive.extractfile(member_name)
            if source is None:
                raise FetchError(f"handoff snapshot is missing {member_name}")
            with source:
                return source.read()
    except (OSError, tarfile.TarError) as error:
        raise FetchError(f"could not read handoff Git metadata: {error}") from error


def _require_local_git_output(workspace: Path, *arguments: str) -> bytes:
    try:
        result = subprocess.run(
            ["git", "-C", str(workspace), *arguments],
            capture_output=True,
            check=False,
        )
    except FileNotFoundError as error:
        raise FetchError("Git is required to verify and apply fetched changes") from error
    if result.returncode == 0:
        return result.stdout
    detail = result.stderr.decode("utf-8", errors="replace").strip()
    raise FetchError(
        f"could not verify local Git state ({' '.join(arguments)}): {detail or 'no command output'}"
    )


def _optional_local_git_output(workspace: Path, *arguments: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(workspace), *arguments],
            capture_output=True,
            check=False,
        )
    except FileNotFoundError as error:
        raise FetchError("Git is required to verify and apply fetched changes") from error
    if result.returncode != 0:
        return None
    value = result.stdout.decode("utf-8", errors="replace").strip()
    return value or None


def _remove_remote_archive(devbox: Any, path: str) -> FetchError | None:
    try:
        _run_checked(devbox, "rm", "-f", path)
    except FetchError as error:
        return FetchError(f"could not remove temporary remote fetch archive: {error}")
    return None


def _run_checked(devbox: Any, *command: str) -> str:
    process = devbox.exec(*command, timeout=120)
    stdout = _read_all(process.stdout)
    stderr = _read_all(process.stderr)
    process.wait()
    returncode = _process_returncode(process)
    if returncode != 0:
        rendered_command = " ".join(command)
        detail = stderr.strip() or stdout.strip() or "no command output"
        raise FetchError(
            f"remote fetch command failed (exit {returncode}): {rendered_command}: {detail}"
        )
    return stdout


def _resolve_fetch_output(workspace: Path, devbox_id: str, output: Path | None) -> Path:
    if output is None:
        fetches_directory = _workspace_state_directory(workspace, FETCHES_DIRECTORY)
        destination = fetches_directory / devbox_id
    else:
        destination = output.expanduser().resolve()
    if destination.exists() or destination.is_symlink():
        raise FetchError(f"refusing to overwrite existing fetch output: {destination}")
    return destination


def _workspace_state_directory(workspace: Path, relative_path: Path) -> Path:
    current = workspace
    for component in relative_path.parts:
        current = current / component
        if current.is_symlink():
            raise FetchError(f"Baton state directory must not be a symlink: {current}")
        if current.exists():
            if not current.is_dir():
                raise FetchError(f"Baton state path is not a directory: {current}")
        else:
            current.mkdir()
    return current


def _workspace_state_path(workspace: Path, relative_path: Path) -> Path:
    current = workspace
    for component in relative_path.parts:
        current = current / component
        if current.is_symlink():
            raise FetchError(f"Baton state directory must not be a symlink: {current}")
        if current.exists() and not current.is_dir():
            raise FetchError(f"Baton state path is not a directory: {current}")
    return current


def _write_json_atomically(path: Path, payload: Mapping[str, object]) -> None:
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix=f".{path.name}-",
            dir=path.parent,
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            json.dump(payload, temporary_file, indent=2, sort_keys=True)
            temporary_file.write("\n")
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        temporary_path.chmod(0o600)
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _safe_tar_path(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if not value or "\x00" in value or path.is_absolute():
        raise FetchError(f"workspace archive has an unsafe member path: {value!r}")
    return _normalize_tar_path(path)


def _resolve_tar_link(parent: PurePosixPath, linkname: str) -> PurePosixPath:
    target = PurePosixPath(linkname)
    if not linkname or target.is_absolute():
        raise FetchError(f"workspace archive has an unsafe symlink target: {linkname!r}")
    return _normalize_tar_path(parent / target)


def _normalize_tar_path(path: PurePosixPath) -> PurePosixPath:
    parts: list[str] = []
    for part in path.parts:
        if part in ("", "."):
            continue
        if part == "..":
            if not parts:
                raise FetchError(f"workspace archive path escapes its root: {path}")
            parts.pop()
            continue
        parts.append(part)
    if not parts:
        raise FetchError(f"workspace archive has an unsafe member path: {path!s}")
    return PurePosixPath(*parts)


def _is_workspace_path(path: PurePosixPath) -> bool:
    return bool(path.parts) and path.parts[0] == "workspace"


def _existing_directory(path: Path, label: str) -> Path:
    resolved = path.expanduser().resolve()
    if not resolved.is_dir():
        raise FetchError(f"{label} is not a directory: {resolved}")
    return resolved


def _normalize_devbox_id(devbox_id: str) -> str:
    normalized = str(devbox_id).strip()
    if not DEVBOX_ID_PATTERN.fullmatch(normalized):
        raise FetchError("Devbox ID must contain only letters, numbers, '-' or '_'")
    return normalized


def _normalize_session_id(session_id: object) -> str:
    try:
        return str(UUID(str(session_id).strip()))
    except (AttributeError, ValueError) as error:
        raise FetchError("handoff receipt session ID must be a UUID") from error


def _read_all(stream: Any) -> str:
    value = stream.read()
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _process_returncode(process: Any) -> int:
    returncode = process.returncode
    if not isinstance(returncode, int):
        raise FetchError("remote process did not report an exit code")
    return returncode
