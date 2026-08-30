"""Create a portable archive for one Codex session and its workspace."""

from __future__ import annotations

import hashlib
import io
import json
import os
import platform
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

ARCHIVE_FORMAT_VERSION = 1
EXCLUDED_PATH_COMPONENTS = frozenset(
    {
        ".baton",
        ".git",
        ".mypy_cache",
        ".omc",
        ".omx",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "node_modules",
        "venv",
    }
)
KNOWN_SECRET_FILENAMES = frozenset(
    {
        ".npmrc",
        ".pypirc",
        "credentials",
        "credentials.json",
        "id_ed25519",
        "id_rsa",
        "service-account.json",
    }
)
KNOWN_SECRET_DIRECTORIES = frozenset({".aws", ".gcp", ".ssh"})


class SnapshotError(RuntimeError):
    """Raised when a portable snapshot cannot be made safely."""


@dataclass
class WorkspaceStats:
    files: int = 0
    directories: int = 0
    symlinks: int = 0
    excluded_paths: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class GitSnapshot:
    metadata: dict[str, Any]
    artifacts: dict[str, bytes]
    fingerprint: str


@dataclass(frozen=True)
class SnapshotResult:
    path: Path
    session_id: str
    sha256: str
    size_bytes: int
    workspace_files: int
    workspace_directories: int
    workspace_symlinks: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "format_version": ARCHIVE_FORMAT_VERSION,
            "path": str(self.path),
            "session_id": self.session_id,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "workspace": {
                "files": self.workspace_files,
                "directories": self.workspace_directories,
                "symlinks": self.workspace_symlinks,
            },
        }


def snapshot(
    *,
    session_id: str,
    workspace: Path,
    codex_home: Path,
    output: Path | None = None,
) -> SnapshotResult:
    """Package a Codex rollout and workspace without copying Codex credentials.

    The archive layout is the target layout: extract ``codex/`` into
    ``/baton/.codex`` and ``workspace/`` into ``/baton/workspace``. The actual
    OpenAI credential is deliberately absent and must arrive through a Runloop
    account secret at restore time.
    """

    normalized_session_id = _normalize_session_id(session_id)
    source_workspace = _existing_directory(workspace, "workspace")
    source_codex_home = _existing_directory(codex_home, "CODEX_HOME")
    _reject_known_sensitive_paths(source_workspace)
    rollout = _find_rollout(source_codex_home, normalized_session_id)
    destination = _snapshot_destination(source_workspace, normalized_session_id, output)

    if destination.exists():
        raise SnapshotError(f"refusing to overwrite existing snapshot: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)

    before_rollout = rollout.stat()
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".tar.gz",
            prefix=".baton-snapshot-",
            dir=destination.parent,
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)

        excluded_absolutes = {destination.resolve(), temporary_path.resolve()}
        before_workspace = _workspace_fingerprint(source_workspace, excluded_absolutes)
        git_snapshot = _capture_git_snapshot(source_workspace)

        with tarfile.open(temporary_path, mode="w:gz", format=tarfile.PAX_FORMAT) as archive:
            workspace_stats = _add_workspace(
                archive,
                source_workspace,
                excluded_absolutes=excluded_absolutes,
            )

            if before_workspace != _workspace_fingerprint(source_workspace, excluded_absolutes):
                raise SnapshotError(
                    "the workspace changed while it was being archived; retry the snapshot"
                )
            if git_snapshot.fingerprint != _capture_git_snapshot(source_workspace).fingerprint:
                raise SnapshotError(
                    "the Git state changed while it was being archived; retry the snapshot"
                )

            archive.add(
                rollout,
                arcname=f"codex/{rollout.relative_to(source_codex_home).as_posix()}",
                recursive=False,
            )
            after_rollout = rollout.stat()
            if (
                before_rollout.st_size != after_rollout.st_size
                or before_rollout.st_mtime_ns != after_rollout.st_mtime_ns
            ):
                raise SnapshotError(
                    "the rollout changed while it was being archived; retry the snapshot"
                )

            session_index = _selected_session_index(source_codex_home, normalized_session_id)
            if session_index is not None:
                _add_bytes(archive, "codex/session_index.jsonl", session_index)

            for archive_path, contents in git_snapshot.artifacts.items():
                _add_bytes(archive, archive_path, contents)

            manifest = _manifest(
                session_id=normalized_session_id,
                rollout=rollout,
                codex_home=source_codex_home,
                workspace=source_workspace,
                workspace_stats=workspace_stats,
                session_index=session_index,
                git_snapshot=git_snapshot,
            )
            _add_bytes(
                archive,
                "manifest.json",
                json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8") + b"\n",
            )

        os.replace(temporary_path, destination)
        temporary_path = None
    except FileNotFoundError as error:
        raise SnapshotError("the workspace changed while it was being archived; retry the snapshot") from error
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)

    return SnapshotResult(
        path=destination,
        session_id=normalized_session_id,
        sha256=_sha256(destination),
        size_bytes=destination.stat().st_size,
        workspace_files=workspace_stats.files,
        workspace_directories=workspace_stats.directories,
        workspace_symlinks=workspace_stats.symlinks,
    )


def _normalize_session_id(session_id: str) -> str:
    try:
        return str(UUID(session_id.strip()))
    except (AttributeError, ValueError) as error:
        raise SnapshotError("session_id must be a UUID") from error


def _existing_directory(path: Path, label: str) -> Path:
    resolved = path.expanduser().resolve()
    if not resolved.is_dir():
        raise SnapshotError(f"{label} is not a directory: {resolved}")
    return resolved


def _find_rollout(codex_home: Path, session_id: str) -> Path:
    sessions_directory = codex_home / "sessions"
    if not sessions_directory.is_dir():
        raise SnapshotError(f"no session directory found at {sessions_directory}")

    suffix = f"-{session_id}.jsonl"
    matches = sorted(
        candidate
        for candidate in sessions_directory.rglob("rollout-*.jsonl")
        if candidate.name.endswith(suffix)
    )
    if not matches:
        raise SnapshotError(f"no rollout found for session {session_id}")
    if len(matches) > 1:
        formatted_matches = ", ".join(str(match) for match in matches)
        raise SnapshotError(
            f"found multiple rollout files for session {session_id}: {formatted_matches}"
        )

    rollout = matches[0]
    _validate_rollout_session_id(rollout, session_id)
    return rollout


def _validate_rollout_session_id(rollout: Path, session_id: str) -> None:
    try:
        with rollout.open("r", encoding="utf-8") as rollout_file:
            first_record = json.loads(rollout_file.readline())
        actual_id = str(UUID(first_record["payload"]["session_id"]))
    except (KeyError, ValueError, json.JSONDecodeError) as error:
        raise SnapshotError(f"invalid session metadata in rollout: {rollout}") from error

    if actual_id != session_id:
        raise SnapshotError(
            f"rollout metadata belongs to {actual_id}, not requested session {session_id}"
        )


def _snapshot_destination(workspace: Path, session_id: str, output: Path | None) -> Path:
    if output is not None:
        destination = output.expanduser().resolve()
        if destination.suffixes[-2:] != [".tar", ".gz"]:
            raise SnapshotError("snapshot output must end in .tar.gz")
        return destination

    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return workspace / ".baton" / "snapshots" / f"{session_id}-{timestamp}.tar.gz"


def _reject_known_sensitive_paths(workspace: Path) -> None:
    sensitive_paths: list[str] = []
    for root, dirnames, filenames in os.walk(workspace, topdown=True, followlinks=False):
        root_path = Path(root)
        retained_dirnames: list[str] = []
        for dirname in dirnames:
            candidate = root_path / dirname
            relative_path = candidate.relative_to(workspace)
            if _looks_like_secret_path(relative_path):
                sensitive_paths.append(relative_path.as_posix())
                continue
            if _is_excluded(candidate, workspace, set()):
                continue
            retained_dirnames.append(dirname)
        dirnames[:] = retained_dirnames

        for filename in filenames:
            candidate = root_path / filename
            relative_path = candidate.relative_to(workspace)
            if _looks_like_secret_path(relative_path):
                sensitive_paths.append(relative_path.as_posix())

    if sensitive_paths:
        formatted_paths = ", ".join(sorted(sensitive_paths))
        raise SnapshotError(
            "refusing to archive credential-like workspace paths: "
            f"{formatted_paths}. Inject runtime credentials with a Runloop account secret instead."
        )


def _looks_like_secret_path(relative_path: Path) -> bool:
    for component in relative_path.parts:
        if component in KNOWN_SECRET_DIRECTORIES:
            return True
        if component in KNOWN_SECRET_FILENAMES:
            return True
        if component.endswith((".key", ".pem")):
            return True
        if component.startswith(".env") and component != ".env.example":
            return True
    return False


def _workspace_arcname(relative_path: Path) -> str:
    return f"workspace/{relative_path.as_posix()}"


def _add_workspace(
    archive: tarfile.TarFile,
    workspace: Path,
    *,
    excluded_absolutes: set[Path],
) -> WorkspaceStats:
    stats = WorkspaceStats()
    archive.add(workspace, arcname="workspace", recursive=False)

    for root, dirnames, filenames in os.walk(workspace, topdown=True, followlinks=False):
        root_path = Path(root)
        relative_root = root_path.relative_to(workspace)
        if relative_root != Path("."):
            _add_stable_entry(archive, root_path, _workspace_arcname(relative_root))
            stats.directories += 1

        retained_dirnames: list[str] = []
        for dirname in dirnames:
            candidate = root_path / dirname
            if _is_excluded(candidate, workspace, excluded_absolutes):
                stats.excluded_paths.append(candidate.relative_to(workspace).as_posix())
                continue
            if candidate.is_symlink():
                _validate_portable_symlink(
                    candidate,
                    workspace,
                    excluded_absolutes=excluded_absolutes,
                )
                _add_stable_entry(
                    archive,
                    candidate,
                    _workspace_arcname(candidate.relative_to(workspace)),
                )
                stats.symlinks += 1
            else:
                retained_dirnames.append(dirname)
        dirnames[:] = retained_dirnames

        for filename in filenames:
            candidate = root_path / filename
            if _is_excluded(candidate, workspace, excluded_absolutes):
                stats.excluded_paths.append(candidate.relative_to(workspace).as_posix())
                continue
            if candidate.is_symlink():
                _validate_portable_symlink(
                    candidate,
                    workspace,
                    excluded_absolutes=excluded_absolutes,
                )
            _add_stable_entry(
                archive,
                candidate,
                _workspace_arcname(candidate.relative_to(workspace)),
            )
            if candidate.is_symlink():
                stats.symlinks += 1
            else:
                stats.files += 1

    return stats


def _workspace_fingerprint(workspace: Path, excluded_absolutes: set[Path]) -> dict[str, tuple[Any, ...]]:
    fingerprint: dict[str, tuple[Any, ...]] = {}
    for root, dirnames, filenames in os.walk(workspace, topdown=True, followlinks=False):
        root_path = Path(root)
        relative_root = root_path.relative_to(workspace)
        if relative_root != Path("."):
            fingerprint[f"directory:{relative_root.as_posix()}"] = _entry_signature(root_path)

        retained_dirnames: list[str] = []
        for dirname in dirnames:
            candidate = root_path / dirname
            if _is_excluded(candidate, workspace, excluded_absolutes):
                continue
            relative_path = candidate.relative_to(workspace)
            if candidate.is_symlink():
                _validate_portable_symlink(
                    candidate,
                    workspace,
                    excluded_absolutes=excluded_absolutes,
                )
                fingerprint[f"symlink:{relative_path.as_posix()}"] = _entry_signature(candidate)
            else:
                retained_dirnames.append(dirname)
        dirnames[:] = retained_dirnames

        for filename in filenames:
            candidate = root_path / filename
            if _is_excluded(candidate, workspace, excluded_absolutes):
                continue
            relative_path = candidate.relative_to(workspace)
            if candidate.is_symlink():
                _validate_portable_symlink(
                    candidate,
                    workspace,
                    excluded_absolutes=excluded_absolutes,
                )
                kind = "symlink"
            else:
                kind = "file"
            fingerprint[f"{kind}:{relative_path.as_posix()}"] = _entry_signature(candidate)

    return fingerprint


def _entry_signature(path: Path) -> tuple[Any, ...]:
    stat = path.lstat()
    if path.is_symlink():
        return ("symlink", os.readlink(path), stat.st_mode, stat.st_mtime_ns)
    return ("entry", stat.st_ino, stat.st_size, stat.st_mode, stat.st_mtime_ns)


def _add_stable_entry(archive: tarfile.TarFile, source: Path, archive_path: str) -> None:
    before = _entry_signature(source)
    archive.add(source, arcname=archive_path, recursive=False)
    after = _entry_signature(source)
    if before != after:
        raise SnapshotError(f"workspace entry changed while it was being archived: {source}")


def _validate_portable_symlink(
    link: Path,
    workspace: Path,
    *,
    excluded_absolutes: set[Path],
) -> None:
    target = Path(os.readlink(link))
    resolved_target = (link.parent / target).resolve() if not target.is_absolute() else target.resolve()
    if not _is_relative_to(resolved_target, workspace):
        raise SnapshotError(
            "refusing to archive a symlink that escapes the workspace: "
            f"{link.relative_to(workspace)} -> {target}"
        )
    if not resolved_target.exists():
        raise SnapshotError(
            "refusing to archive an unresolved symlink: "
            f"{link.relative_to(workspace)} -> {target}"
        )
    if _is_excluded(resolved_target, workspace, excluded_absolutes):
        raise SnapshotError(
            "refusing to archive a symlink whose target is excluded from the snapshot: "
            f"{link.relative_to(workspace)} -> {target}"
        )


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _is_excluded(candidate: Path, workspace: Path, excluded_absolutes: set[Path]) -> bool:
    resolved_candidate = candidate.resolve()
    if resolved_candidate in excluded_absolutes:
        return True
    relative_path = candidate.relative_to(workspace)
    return any(_is_excluded_component(component) for component in relative_path.parts)


def _is_excluded_component(component: str) -> bool:
    if component in EXCLUDED_PATH_COMPONENTS:
        return True
    if component.endswith(".egg-info"):
        return True
    return component.startswith(".env") and component != ".env.example"


def _capture_git_snapshot(workspace: Path) -> GitSnapshot:
    try:
        inside_worktree = _run_git(workspace, "rev-parse", "--is-inside-work-tree")
    except FileNotFoundError as error:
        if (workspace / ".git").exists():
            raise SnapshotError("Git is required to snapshot this Git workspace") from error
        return _git_snapshot({"present": False}, {})

    if inside_worktree.returncode != 0 or inside_worktree.stdout.strip() != b"true":
        return _git_snapshot({"present": False}, {})

    repository_root_result = _require_git(workspace, "rev-parse", "--show-toplevel")
    repository_root = Path(repository_root_result.stdout.decode("utf-8").strip()).resolve()
    if repository_root != workspace:
        raise SnapshotError(
            "workspace must be the Git worktree root; use "
            f"--workspace {repository_root}"
        )

    staged_patch = _require_git(
        workspace,
        "diff",
        "--binary",
        "--no-ext-diff",
        "--cached",
        "--root",
    ).stdout
    unstaged_patch = _require_git(
        workspace,
        "diff",
        "--binary",
        "--no-ext-diff",
    ).stdout
    status = _require_git(workspace, "status", "--porcelain=v1", "-z").stdout
    untracked = _require_git(workspace, "ls-files", "--others", "--exclude-standard", "-z").stdout

    head = _git_optional_output(workspace, "rev-parse", "HEAD")
    branch = _git_optional_output(workspace, "branch", "--show-current")
    origin = _git_optional_output(workspace, "remote", "get-url", "origin")
    if head is None:
        raise SnapshotError(
            "Git worktree has no committed HEAD; Baton cannot yet reconstruct an unborn branch"
        )
    repository_bundle = _create_git_bundle(workspace)

    artifacts = {
        "git/repository.bundle": repository_bundle,
        "git/status-v1.z": status,
        "git/staged.patch": staged_patch,
        "git/unstaged.patch": unstaged_patch,
        "git/untracked.z": untracked,
    }
    metadata = {
        "present": True,
        "head": head,
        "branch": branch or None,
        "origin_url": _sanitize_git_remote(origin) if origin else None,
        "bundle_archive_path": "git/repository.bundle",
        "artifacts": sorted(artifacts),
    }
    return _git_snapshot(metadata, artifacts)


def _run_git(workspace: Path, *arguments: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", "-C", str(workspace), *arguments],
        capture_output=True,
        check=False,
    )


def _require_git(workspace: Path, *arguments: str) -> subprocess.CompletedProcess[bytes]:
    result = _run_git(workspace, *arguments)
    if result.returncode != 0:
        message = result.stderr.decode("utf-8", errors="replace").strip()
        raise SnapshotError(f"Git command failed ({' '.join(arguments)}): {message}")
    return result


def _git_optional_output(workspace: Path, *arguments: str) -> str | None:
    result = _run_git(workspace, *arguments)
    if result.returncode != 0:
        return None
    value = result.stdout.decode("utf-8", errors="replace").strip()
    return value or None


def _sanitize_git_remote(remote: str) -> str:
    parsed = urlsplit(remote)
    if parsed.scheme not in {"http", "https", "ssh"} or not parsed.hostname:
        return remote

    hostname = parsed.hostname
    if parsed.port is not None:
        hostname = f"{hostname}:{parsed.port}"
    if parsed.scheme == "ssh" and parsed.username:
        hostname = f"{parsed.username}@{hostname}"
    return urlunsplit((parsed.scheme, hostname, parsed.path, "", ""))


def _create_git_bundle(workspace: Path) -> bytes:
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=".baton-git-",
            suffix=".bundle",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
        result = _run_git(workspace, "bundle", "create", str(temporary_path), "--all", "HEAD")
        if result.returncode != 0:
            message = result.stderr.decode("utf-8", errors="replace").strip()
            raise SnapshotError(f"could not capture Git history bundle: {message}")
        return temporary_path.read_bytes()
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _git_snapshot(metadata: dict[str, Any], artifacts: dict[str, bytes]) -> GitSnapshot:
    digest = hashlib.sha256()
    digest.update(json.dumps(metadata, sort_keys=True).encode("utf-8"))
    for archive_path in sorted(artifacts):
        digest.update(archive_path.encode("utf-8"))
        digest.update(artifacts[archive_path])
    return GitSnapshot(metadata=metadata, artifacts=artifacts, fingerprint=digest.hexdigest())


def _selected_session_index(codex_home: Path, session_id: str) -> bytes | None:
    index_path = codex_home / "session_index.jsonl"
    if not index_path.is_file():
        return None

    matching_records: list[str] = []
    with index_path.open("r", encoding="utf-8") as index_file:
        for raw_line in index_file:
            try:
                record = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            if record.get("id") == session_id:
                matching_records.append(raw_line.rstrip("\n"))

    if not matching_records:
        return None
    return ("\n".join(matching_records) + "\n").encode("utf-8")


def _manifest(
    *,
    session_id: str,
    rollout: Path,
    codex_home: Path,
    workspace: Path,
    workspace_stats: WorkspaceStats,
    session_index: bytes | None,
    git_snapshot: GitSnapshot,
) -> dict[str, Any]:
    resume_command = [
        "codex",
        "exec",
        "--cd",
        "/baton/workspace",
        "resume",
        session_id,
        "--json",
        "--dangerously-bypass-approvals-and-sandbox",
    ]
    if not git_snapshot.metadata["present"]:
        resume_command.append("--skip-git-repo-check")
    resume_command.append("<follow-up prompt>")
    return {
        "format_version": ARCHIVE_FORMAT_VERSION,
        "created_at": datetime.now(UTC).isoformat(),
        "session": {
            "id": session_id,
            "title": _session_title(session_index),
            "rollout_archive_path": f"codex/{rollout.relative_to(codex_home).as_posix()}",
            "session_index_included": session_index is not None,
        },
        "workspace": {
            "archive_path": "workspace",
            "source_path": str(workspace),
            "files": workspace_stats.files,
            "directories": workspace_stats.directories,
            "symlinks": workspace_stats.symlinks,
            "excluded_paths": sorted(workspace_stats.excluded_paths),
        },
        "repository": git_snapshot.metadata,
        "restore_contract": {
            "environment": {"CODEX_HOME": "/baton/.codex"},
            "codex_home": "/baton/.codex",
            "workspace": "/baton/workspace",
            "command": resume_command,
        },
        "security": {
            "codex_oauth_included": False,
            "known_workspace_credential_paths_rejected": True,
            "archive_classification": "sensitive",
            "note": (
                "Known workspace credential paths are rejected, but session transcripts and "
                "ordinary source files can still contain secrets. Treat this archive as sensitive."
            ),
        },
        "platform": {
            "snapshot_host": {
                "system": platform.system(),
                "machine": platform.machine(),
            },
            "target": {"system": "Linux", "machine": "x86_64"},
            "native_dependency_policy": "fail rather than rebuild native dependencies",
        },
    }


def _session_title(session_index: bytes | None) -> str | None:
    """Return the latest non-empty thread name from a selected index record."""

    if session_index is None:
        return None
    title: str | None = None
    for raw_line in session_index.decode("utf-8", errors="replace").splitlines():
        try:
            record = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        candidate = record.get("thread_name")
        if isinstance(candidate, str) and candidate.strip():
            title = candidate
    return title


def _add_bytes(archive: tarfile.TarFile, archive_path: str, contents: bytes) -> None:
    entry = tarfile.TarInfo(name=archive_path)
    entry.size = len(contents)
    entry.mtime = int(datetime.now(UTC).timestamp())
    archive.addfile(entry, io.BytesIO(contents))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as archive:
        for chunk in iter(lambda: archive.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
