"""Safely retrieve a completed Modal Sandbox workspace for local review."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import UUID, uuid4

from .handoff import HandoffError, inspect_snapshot_archive
from .snapshot import KNOWN_SECRET_DIRECTORIES, KNOWN_SECRET_FILENAMES

RECEIPT_FORMAT_VERSION = 1
HANDOFF_RECEIPTS_DIRECTORY = Path(".baton") / "handoffs"
FETCHES_DIRECTORY = Path(".baton") / "fetches"
REMOTE_ROOT = "/baton"
REMOTE_COMPLETION_MARKER = f"{REMOTE_ROOT}/handoff-complete.json"
INTERNAL_WORKSPACE_COMPONENTS = frozenset({".baton", ".git", ".omc", ".omx"})
NATIVE_ARTIFACT_SUFFIXES = frozenset({".dll", ".dylib", ".node", ".pyd", ".so"})
NATIVE_BUILD_COMPONENTS = frozenset({".venv", "build", "dist", "node_modules", "venv"})
MAX_ARCHIVE_MEMBERS = 20_000
MAX_ARCHIVE_UNCOMPRESSED_BYTES = 2 * 1024 * 1024 * 1024
SANDBOX_ID_PATTERN = re.compile(r"sb-[A-Za-z0-9_-]{1,128}\Z")


class FetchError(RuntimeError):
    """Raised when a remote workspace cannot be fetched safely."""


@dataclass(frozen=True)
class HandoffReceipt:
    """Local binding between a detached Sandbox and its immutable snapshot."""

    path: Path
    sandbox_id: str
    session_id: str
    archive: Path
    workspace: Path


@dataclass(frozen=True)
class FetchResult:
    """A completed Sandbox workspace staged locally for review."""

    sandbox_id: str
    archive: Path
    fetch_root: Path
    baseline_workspace: Path
    remote_workspace: Path
    patch_path: Path
    changed_files: tuple[str, ...]
    remote_exit_code: int

    def to_dict(self) -> dict[str, object]:
        return {
            "sandbox_id": self.sandbox_id,
            "archive": str(self.archive),
            "fetch_root": str(self.fetch_root),
            "baseline_workspace": str(self.baseline_workspace),
            "remote_workspace": str(self.remote_workspace),
            "patch_path": str(self.patch_path),
            "changed_files": list(self.changed_files),
            "remote_exit_code": self.remote_exit_code,
            "applied": False,
        }


def write_handoff_receipt(
    *,
    sandbox_id: str,
    session_id: str,
    archive_path: Path,
    workspace: Path,
) -> Path:
    """Persist the immutable baseline needed by a later fetch."""

    normalized_sandbox_id = _normalize_sandbox_id(sandbox_id)
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
    receipt_path = receipts_directory / f"{normalized_sandbox_id}.json"
    if receipt_path.exists() or receipt_path.is_symlink():
        raise FetchError(f"refusing to overwrite existing handoff receipt: {receipt_path}")
    _write_json_atomically(
        receipt_path,
        {
            "format_version": RECEIPT_FORMAT_VERSION,
            "sandbox_id": normalized_sandbox_id,
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
            sandbox_id = _normalize_sandbox_id(receipt_path.stem)
            receipt = _load_handoff_receipt(receipt_path, sandbox_id)
            modified_at = receipt_path.stat().st_mtime_ns
        except (FetchError, OSError):
            continue
        if receipt.workspace == source_workspace:
            receipts.append((modified_at, receipt))
    return tuple(
        receipt
        for _, receipt in sorted(
            receipts,
            key=lambda entry: (entry[0], entry[1].sandbox_id),
            reverse=True,
        )
    )


def fetch_workspace(
    *,
    sandbox_id: str,
    workspace: Path,
    receipt_path: Path | None = None,
    output: Path | None = None,
    modal_module: Any | None = None,
) -> FetchResult:
    """Download a completed Sandbox workspace without changing the worktree."""

    normalized_sandbox_id = _normalize_sandbox_id(sandbox_id)
    source_workspace = _existing_directory(workspace, "workspace")
    resolved_receipt_path = (
        receipt_path.expanduser().resolve()
        if receipt_path is not None
        else _workspace_state_path(source_workspace, HANDOFF_RECEIPTS_DIRECTORY)
        / f"{normalized_sandbox_id}.json"
    )
    receipt = _load_handoff_receipt(resolved_receipt_path, normalized_sandbox_id)
    if receipt.workspace != source_workspace:
        raise FetchError(
            "handoff receipt belongs to a different workspace; fetch from the original "
            "workspace so its snapshot remains the comparison baseline"
        )
    fetch_root = _resolve_fetch_output(source_workspace, normalized_sandbox_id, output)

    modal = modal_module or _load_modal()
    try:
        sandbox = modal.Sandbox.from_id(normalized_sandbox_id)
    except Exception as error:
        raise FetchError(f"Modal Sandbox lookup failed: {error}") from error

    remote_exit_code = _require_completion_marker(sandbox)
    fetch_root.parent.mkdir(parents=True, exist_ok=True)
    staging_root = Path(
        tempfile.mkdtemp(prefix=f".{fetch_root.name}-", dir=fetch_root.parent)
    )
    remote_archive = f"/tmp/baton-fetch-{uuid4().hex}.tar.gz"
    primary_error: BaseException | None = None

    try:
        baseline_workspace = staging_root / "baseline"
        remote_workspace = staging_root / "workspace"
        downloaded_archive = staging_root / "remote-workspace.tar.gz"
        _extract_snapshot_workspace(receipt.archive, baseline_workspace)
        _run_checked(
            sandbox,
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
        sandbox.filesystem.copy_to_local(remote_archive, downloaded_archive)
        _extract_workspace_tarball(downloaded_archive, remote_workspace)
        downloaded_archive.unlink(missing_ok=True)

        patch_path = staging_root / "changes.patch"
        patch_bytes = _build_patch(staging_root)
        patch_path.write_bytes(patch_bytes)
        changed_files = _collect_changed_files(patch_bytes)
        _write_json_atomically(
            staging_root / "result.json",
            {
                "sandbox_id": normalized_sandbox_id,
                "session_id": receipt.session_id,
                "archive": str(receipt.archive),
                "remote_exit_code": remote_exit_code,
                "changed_files": list(changed_files),
            },
        )
        os.replace(staging_root, fetch_root)
        return FetchResult(
            sandbox_id=normalized_sandbox_id,
            archive=receipt.archive,
            fetch_root=fetch_root,
            baseline_workspace=fetch_root / "baseline",
            remote_workspace=fetch_root / "workspace",
            patch_path=fetch_root / "changes.patch",
            changed_files=changed_files,
            remote_exit_code=remote_exit_code,
        )
    except FetchError as error:
        primary_error = error
        raise
    except Exception as error:
        fetch_error = FetchError(f"Modal fetch failed: {error}")
        primary_error = fetch_error
        raise fetch_error from error
    except BaseException as error:
        primary_error = error
        raise
    finally:
        cleanup_error = _remove_remote_archive(sandbox, remote_archive)
        if staging_root.exists():
            shutil.rmtree(staging_root)
        if cleanup_error is not None:
            if primary_error is None:
                raise cleanup_error
            if isinstance(primary_error, FetchError):
                primary_error.args = (f"{primary_error}; additionally, {cleanup_error}",)
            elif hasattr(primary_error, "add_note"):
                primary_error.add_note(str(cleanup_error))


def _load_handoff_receipt(path: Path, sandbox_id: str) -> HandoffReceipt:
    if not path.is_file():
        raise FetchError(
            "no handoff receipt found for this Sandbox. Run 'baton handoff --detach' "
            "from this workspace first, or pass --receipt."
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise FetchError(f"handoff receipt is not valid JSON: {path}") from error
    if not isinstance(payload, Mapping) or payload.get("format_version") != RECEIPT_FORMAT_VERSION:
        raise FetchError(f"handoff receipt has an unsupported format: {path}")
    if payload.get("sandbox_id") != sandbox_id:
        raise FetchError("handoff receipt does not belong to the requested Sandbox")
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
        sandbox_id=sandbox_id,
        session_id=session_id,
        archive=snapshot.path,
        workspace=receipt_workspace,
    )


def _require_completion_marker(sandbox: Any) -> int:
    try:
        raw_marker = sandbox.filesystem.read_text(REMOTE_COMPLETION_MARKER)
    except Exception as error:
        raise FetchError(
            "the remote handoff has not completed yet, or its Sandbox is unavailable; "
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
            f"{path}. Runtime credentials must stay in Modal Secrets."
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


def _remove_remote_archive(sandbox: Any, path: str) -> FetchError | None:
    try:
        _run_checked(sandbox, "rm", "-f", path)
    except FetchError as error:
        return FetchError(f"could not remove temporary remote fetch archive: {error}")
    return None


def _run_checked(sandbox: Any, *command: str) -> str:
    process = sandbox.exec(*command, timeout=120)
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


def _resolve_fetch_output(workspace: Path, sandbox_id: str, output: Path | None) -> Path:
    if output is None:
        fetches_directory = _workspace_state_directory(workspace, FETCHES_DIRECTORY)
        destination = fetches_directory / sandbox_id
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


def _normalize_sandbox_id(sandbox_id: str) -> str:
    normalized = str(sandbox_id).strip()
    if not SANDBOX_ID_PATTERN.fullmatch(normalized):
        raise FetchError("sandbox ID must look like a Modal Sandbox ID (sb-...)")
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


def _load_modal() -> Any:
    try:
        import modal
    except ImportError as error:
        raise FetchError("Modal SDK is required; install Baton with its Modal dependency") from error
    return modal
