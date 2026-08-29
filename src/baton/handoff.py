"""Move a verified Baton archive into a Modal Sandbox and resume Codex there."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import tarfile
from threading import Thread
from typing import Any, Callable, Mapping
from uuid import UUID

from .snapshot import ARCHIVE_FORMAT_VERSION


DEFAULT_MODAL_APP = "baton"
DEFAULT_MODAL_SECRET = "baton-openai"
REMOTE_ARCHIVE = "/tmp/baton-snapshot.tar.gz"
REMOTE_ROOT = "/baton"
REMOTE_STAGE = f"{REMOTE_ROOT}/stage"
REMOTE_CODEX_HOME = f"{REMOTE_ROOT}/.codex"
REMOTE_WORKSPACE = f"{REMOTE_ROOT}/workspace"
GIT_BUNDLE_ARCHIVE = "git/repository.bundle"
SETUP_COMMAND_TIMEOUT = 120
NATIVE_ARTIFACT_SUFFIXES = frozenset({".dll", ".dylib", ".node", ".pyd", ".so"})
NATIVE_BUILD_COMPONENTS = frozenset({".venv", "build", "dist", "node_modules", "venv"})


class HandoffError(RuntimeError):
    """Raised when a Baton archive cannot safely run in a Modal Sandbox."""


@dataclass(frozen=True)
class SnapshotArchive:
    """Validated information required to restore a Baton snapshot."""

    path: Path
    session_id: str
    manifest: Mapping[str, Any]

    @property
    def repository(self) -> Mapping[str, Any]:
        return self.manifest["repository"]


@dataclass(frozen=True)
class PreparedRuntime:
    """A named Modal image that contains the matching Codex CLI and Git."""

    app_name: str
    image_name: str
    codex_version: str

    def to_dict(self) -> dict[str, str]:
        return {
            "modal_app": self.app_name,
            "image_name": self.image_name,
            "codex_version": self.codex_version,
        }


@dataclass(frozen=True)
class HandoffResult:
    """Result of a remote handoff attempt."""

    archive: Path
    session_id: str
    sandbox_id: str
    detached: bool
    event_count: int
    exit_code: int | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "archive": str(self.archive),
            "session_id": self.session_id,
            "sandbox_id": self.sandbox_id,
            "detached": self.detached,
            "event_count": self.event_count,
            "exit_code": self.exit_code,
        }


def inspect_snapshot_archive(archive_path: Path) -> SnapshotArchive:
    """Check archive structure before uploading or extracting it remotely."""

    path = archive_path.expanduser().resolve()
    if not path.is_file():
        raise HandoffError(f"snapshot archive is not a file: {path}")

    try:
        with tarfile.open(path, "r:gz") as archive:
            members = archive.getmembers()
            _validate_tar_members(members)
            _validate_tar_link_targets(members)
            member_names = {
                _safe_tar_path(member.name).as_posix()
                for member in members
            }
            if "manifest.json" not in member_names:
                raise HandoffError("snapshot archive is missing manifest.json")

            manifest_file = archive.extractfile("manifest.json")
            if manifest_file is None:
                raise HandoffError("snapshot manifest could not be read")
            try:
                manifest = json.load(manifest_file)
            except json.JSONDecodeError as error:
                raise HandoffError("snapshot manifest is not valid JSON") from error
    except (OSError, tarfile.TarError) as error:
        raise HandoffError(f"snapshot archive is not a readable .tar.gz: {path}") from error

    session_id = _validate_manifest(manifest, member_names)
    _reject_native_restore_artifacts(member_names)
    return SnapshotArchive(path=path, session_id=session_id, manifest=manifest)


def build_resume_command(
    session_id: str,
    prompt: str,
    *,
    skip_git_repo_check: bool = False,
) -> list[str]:
    """Build the confirmed non-interactive Codex argv without invoking a shell."""

    normalized_session_id = _normalize_session_id(session_id)
    if not prompt or not prompt.strip():
        raise HandoffError("follow-up prompt must not be empty")
    command = [
        "codex",
        "exec",
        "--cd",
        REMOTE_WORKSPACE,
        "resume",
        normalized_session_id,
        "--json",
        "--dangerously-bypass-approvals-and-sandbox",
    ]
    if skip_git_repo_check:
        command.append("--skip-git-repo-check")
    command.append(prompt)
    return command


def infer_local_codex_version() -> str:
    """Return the local Codex semantic version to keep session formats aligned."""

    try:
        result = subprocess.run(
            ["codex", "--version"],
            capture_output=True,
            check=False,
            text=True,
        )
    except FileNotFoundError as error:
        raise HandoffError("Codex CLI is not on PATH; pass --codex-version explicitly") from error
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip()
        raise HandoffError(f"could not determine local Codex version: {message}")

    match = re.search(r"\b(\d+\.\d+\.\d+)\b", result.stdout)
    if match is None:
        raise HandoffError(f"could not parse Codex version from: {result.stdout.strip()}")
    return match.group(1)


def image_name_for_version(codex_version: str) -> str:
    """Name a reusable Modal image after the exact Codex version it contains."""

    normalized_version = _normalize_codex_version(codex_version)
    return f"baton-codex-{normalized_version.replace('.', '-')}"


def prepare_runtime(
    *,
    codex_version: str,
    app_name: str = DEFAULT_MODAL_APP,
    image_name: str | None = None,
    modal_module: Any | None = None,
) -> PreparedRuntime:
    """Build and publish the named Modal image used by later handoffs."""

    normalized_version = _normalize_codex_version(codex_version)
    resolved_image_name = image_name or image_name_for_version(normalized_version)
    _require_nonempty_label("Modal app name", app_name)
    _require_nonempty_label("Modal image name", resolved_image_name)
    modal = modal_module or _load_modal()
    try:
        app = modal.App.lookup(app_name, create_if_missing=True)
        image = _runtime_image(modal, normalized_version)
        image.build(app).publish(resolved_image_name)
    except Exception as error:
        raise HandoffError(f"Modal image build failed: {error}") from error
    return PreparedRuntime(
        app_name=app_name,
        image_name=resolved_image_name,
        codex_version=normalized_version,
    )


def handoff_archive(
    *,
    archive_path: Path,
    prompt: str,
    modal_secret: str = DEFAULT_MODAL_SECRET,
    app_name: str = DEFAULT_MODAL_APP,
    image_name: str,
    sandbox_timeout: int = 20 * 60,
    command_timeout: int = 20 * 60,
    detach: bool = False,
    on_event: Callable[[dict[str, Any]], None] | None = None,
    modal_module: Any | None = None,
) -> HandoffResult:
    """Restore an archive in Modal and run ``codex exec resume`` there.

    The remote command receives only the named Modal Secret. Local Codex OAuth
    files are neither consulted nor copied.
    """

    snapshot = inspect_snapshot_archive(archive_path)
    resume_command = build_resume_command(
        snapshot.session_id,
        prompt,
        skip_git_repo_check=not snapshot.repository["present"],
    )
    _validate_timeout("sandbox timeout", sandbox_timeout)
    _validate_timeout("command timeout", command_timeout)
    _require_nonempty_label("Modal app name", app_name)
    _require_nonempty_label("Modal image name", image_name)
    _require_nonempty_label("Modal Secret name", modal_secret)

    modal = modal_module or _load_modal()
    try:
        app = modal.App.lookup(app_name, create_if_missing=True)
        image = modal.Image.from_name(image_name)
        secret = modal.Secret.from_name(
            modal_secret,
            required_keys=["OPENAI_API_KEY"],
        )
        sandbox = modal.Sandbox.create(
            app=app,
            image=image,
            name=f"baton-{snapshot.session_id[:8]}",
            secrets=[secret],
            timeout=sandbox_timeout,
        )
    except Exception as error:
        raise HandoffError(f"Modal Sandbox creation failed: {error}") from error
    sandbox_id = _sandbox_id(sandbox)
    detached = False
    primary_error: BaseException | None = None

    try:
        sandbox.filesystem.copy_from_local(snapshot.path, REMOTE_ARCHIVE)
        _restore_snapshot(sandbox, snapshot)
        _configure_api_key_auth(sandbox)

        process = sandbox.exec(
            "env",
            f"CODEX_HOME={REMOTE_CODEX_HOME}",
            *resume_command,
            timeout=command_timeout,
        )
        if detach:
            sandbox.detach()
            detached = True
            return HandoffResult(
                archive=snapshot.path,
                session_id=snapshot.session_id,
                sandbox_id=sandbox_id,
                detached=True,
                event_count=0,
                exit_code=None,
            )

        event_count = _stream_codex_events(process, on_event)
        return HandoffResult(
            archive=snapshot.path,
            session_id=snapshot.session_id,
            sandbox_id=sandbox_id,
            detached=False,
            event_count=event_count,
            exit_code=_process_returncode(process),
        )
    except HandoffError as error:
        primary_error = error
        raise
    except Exception as error:
        handoff_error = HandoffError(f"Modal handoff failed: {error}")
        primary_error = handoff_error
        raise handoff_error from error
    except BaseException as error:
        primary_error = error
        raise
    finally:
        if not detached:
            cleanup_error = _cleanup_sandbox(sandbox)
            if cleanup_error is not None:
                if primary_error is None:
                    raise cleanup_error
                if isinstance(primary_error, HandoffError):
                    primary_error.args = (f"{primary_error}; additionally, {cleanup_error}",)
                elif hasattr(primary_error, "add_note"):
                    primary_error.add_note(str(cleanup_error))


def _runtime_image(modal: Any, codex_version: str) -> Any:
    """Define the Linux x86_64 Sandbox image entirely through ``modal.Image``."""

    return (
        modal.Image.from_registry("node:22-bookworm-slim")
        .apt_install("ca-certificates", "git", "gzip", "tar")
        .run_commands(
            f"npm install --global @openai/codex@{codex_version}",
            "codex --version",
            "git --version",
        )
    )


def _restore_snapshot(sandbox: Any, snapshot: SnapshotArchive) -> None:
    _run_checked(sandbox, "mkdir", "-p", REMOTE_STAGE)
    _run_checked(sandbox, "tar", "-xzf", REMOTE_ARCHIVE, "-C", REMOTE_STAGE)
    _run_checked(sandbox, "test", "-f", f"{REMOTE_STAGE}/manifest.json")
    _run_checked(sandbox, "test", "-d", f"{REMOTE_STAGE}/codex")
    _run_checked(sandbox, "test", "-d", f"{REMOTE_STAGE}/workspace")

    if snapshot.repository["present"]:
        _restore_git_workspace(sandbox, snapshot.repository)
    else:
        _run_checked(sandbox, "mv", f"{REMOTE_STAGE}/workspace", REMOTE_WORKSPACE)

    _assert_remote_workspace_portable(sandbox)
    _run_checked(sandbox, "mv", f"{REMOTE_STAGE}/codex", REMOTE_CODEX_HOME)
    _run_checked(sandbox, "test", "-d", REMOTE_CODEX_HOME)
    _run_checked(sandbox, "test", "-d", REMOTE_WORKSPACE)


def _configure_api_key_auth(sandbox: Any) -> None:
    """Create ephemeral Codex API-key auth from Modal's injected Secret."""

    _run_checked(
        sandbox,
        "sh",
        "-c",
        (
            'if [ -z "$OPENAI_API_KEY" ]; then '
            "echo 'OPENAI_API_KEY is empty' >&2; exit 2; "
            "fi; umask 077; "
            f"printf '%s\\n' \"$OPENAI_API_KEY\" | "
            f"env CODEX_HOME={REMOTE_CODEX_HOME} codex login --with-api-key"
        ),
    )


def _restore_git_workspace(sandbox: Any, repository: Mapping[str, Any]) -> None:
    head = _required_repository_value(repository, "head")
    branch = repository.get("branch")
    if branch is not None and not isinstance(branch, str):
        raise HandoffError("snapshot repository branch is invalid")
    bundle_archive_path = repository.get("bundle_archive_path")
    if bundle_archive_path != GIT_BUNDLE_ARCHIVE:
        raise HandoffError("snapshot Git metadata has an invalid repository bundle path")

    bundle_path = f"{REMOTE_STAGE}/{GIT_BUNDLE_ARCHIVE}"
    _run_checked(sandbox, "git", "clone", "--no-checkout", bundle_path, REMOTE_WORKSPACE)

    if branch:
        _run_checked(sandbox, "git", "-C", REMOTE_WORKSPACE, "checkout", "-B", branch, head)
    else:
        _run_checked(sandbox, "git", "-C", REMOTE_WORKSPACE, "checkout", "--detach", head)

    origin = repository.get("origin_url")
    if origin is None:
        _run_checked(sandbox, "git", "-C", REMOTE_WORKSPACE, "remote", "remove", "origin")
    elif isinstance(origin, str) and origin:
        _run_checked(sandbox, "git", "-C", REMOTE_WORKSPACE, "remote", "set-url", "origin", origin)
    else:
        raise HandoffError("snapshot repository origin URL is invalid")

    _run_checked(
        sandbox,
        "git",
        "-C",
        REMOTE_WORKSPACE,
        "apply",
        "--index",
        "--allow-empty",
        f"{REMOTE_STAGE}/git/staged.patch",
    )
    _run_checked(
        sandbox,
        "git",
        "-C",
        REMOTE_WORKSPACE,
        "apply",
        "--allow-empty",
        f"{REMOTE_STAGE}/git/unstaged.patch",
    )
    _run_checked(
        sandbox,
        "cp",
        "-a",
        f"{REMOTE_STAGE}/workspace/.",
        f"{REMOTE_WORKSPACE}/",
    )


def _assert_remote_workspace_portable(sandbox: Any) -> None:
    """Reject source trees that would require a cross-platform native rebuild."""

    native_directory = _run_checked(
        sandbox,
        "find",
        REMOTE_WORKSPACE,
        "-type",
        "d",
        "(",
        "-name",
        "node_modules",
        "-o",
        "-name",
        "venv",
        "-o",
        "-name",
        ".venv",
        "-o",
        "-name",
        "build",
        "-o",
        "-name",
        "dist",
        ")",
        "-print",
        "-quit",
    ).strip()
    if native_directory:
        raise HandoffError(
            "restored workspace contains dependency/build output that cannot cross macOS arm64 "
            f"to Linux x86_64: {native_directory}. Baton will not rebuild native dependencies."
        )

    native_file = _run_checked(
        sandbox,
        "find",
        REMOTE_WORKSPACE,
        "-type",
        "f",
        "(",
        "-name",
        "binding.gyp",
        "-o",
        "-name",
        "*.node",
        "-o",
        "-name",
        "*.so",
        "-o",
        "-name",
        "*.dylib",
        "-o",
        "-name",
        "*.pyd",
        "-o",
        "-name",
        "*.dll",
        ")",
        "-print",
        "-quit",
    ).strip()
    if native_file:
        raise HandoffError(
            "restored workspace contains a native build marker or binary that cannot cross "
            f"macOS arm64 to Linux x86_64: {native_file}. Baton will not rebuild native dependencies."
        )


def _run_checked(sandbox: Any, *command: str) -> str:
    process = sandbox.exec(*command, timeout=SETUP_COMMAND_TIMEOUT)
    stdout = _read_all(process.stdout)
    stderr = _read_all(process.stderr)
    process.wait()
    returncode = _process_returncode(process)
    if returncode != 0:
        rendered_command = " ".join(command)
        detail = stderr.strip() or stdout.strip() or "no command output"
        raise HandoffError(
            f"remote setup command failed (exit {returncode}): {rendered_command}: {detail}"
        )
    return stdout


def _stream_codex_events(
    process: Any,
    on_event: Callable[[dict[str, Any]], None] | None,
) -> int:
    stderr_chunks: list[str] = []
    stderr_thread = Thread(
        target=lambda: stderr_chunks.append(_read_all(process.stderr)),
        daemon=True,
    )
    stderr_thread.start()
    event_count = 0
    buffered = ""

    for chunk in process.stdout:
        buffered += _coerce_text(chunk)
        while "\n" in buffered:
            raw_line, buffered = buffered.split("\n", 1)
            event_count += _emit_jsonl_event(raw_line, on_event)

    if buffered:
        event_count += _emit_jsonl_event(buffered, on_event)

    process.wait()
    stderr_thread.join()
    returncode = _process_returncode(process)
    if returncode != 0:
        detail = "".join(stderr_chunks).strip() or "no stderr output"
        raise HandoffError(f"remote Codex resume failed (exit {returncode}): {detail}")
    return event_count


def _emit_jsonl_event(
    raw_line: str,
    on_event: Callable[[dict[str, Any]], None] | None,
) -> int:
    if not raw_line.strip():
        return 0
    try:
        payload = json.loads(raw_line)
    except json.JSONDecodeError:
        _emit_event(
            on_event,
            {"type": "codex_non_json", "raw": raw_line.rstrip("\r")},
        )
        return 0
    _emit_event(on_event, {"type": "codex_event", "event": payload})
    return 1


def _emit_event(
    on_event: Callable[[dict[str, Any]], None] | None,
    event: dict[str, Any],
) -> None:
    if on_event is not None:
        on_event(event)


def _validate_tar_members(members: list[tarfile.TarInfo]) -> None:
    names: set[str] = set()
    for member in members:
        normalized_name = _safe_tar_path(member.name)
        name = normalized_name.as_posix()
        if name in names:
            raise HandoffError(f"snapshot archive has duplicate member: {name}")
        names.add(name)
        if not (member.isdir() or member.isfile() or member.issym() or member.islnk()):
            raise HandoffError(f"snapshot archive contains unsupported member type: {name}")
        if member.issym():
            target = _resolve_tar_link(normalized_name.parent, member.linkname)
            _require_workspace_link_target(name, target)
        if member.islnk():
            target = _safe_tar_path(member.linkname)
            _require_workspace_link_target(name, target)


def _validate_tar_link_targets(members: list[tarfile.TarInfo]) -> None:
    member_names = {_safe_tar_path(member.name).as_posix() for member in members}
    for member in members:
        normalized_name = _safe_tar_path(member.name)
        if member.issym():
            target = _resolve_tar_link(normalized_name.parent, member.linkname)
        elif member.islnk():
            target = _safe_tar_path(member.linkname)
        else:
            continue
        target_name = target.as_posix()
        if target_name not in member_names and not any(
            name.startswith(f"{target_name}/") for name in member_names
        ):
            raise HandoffError(
                "snapshot archive link targets content that is absent from the archive: "
                f"{normalized_name} -> {target}"
            )


def _safe_tar_path(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if not value or "\x00" in value or path.is_absolute():
        raise HandoffError(f"snapshot archive has an unsafe member path: {value!r}")
    return _normalize_tar_path(path)


def _resolve_tar_link(parent: PurePosixPath, linkname: str) -> PurePosixPath:
    target = PurePosixPath(linkname)
    if not linkname or target.is_absolute():
        raise HandoffError(f"snapshot archive has an unsafe symlink target: {linkname!r}")
    return _normalize_tar_path(parent / target)


def _normalize_tar_path(path: PurePosixPath) -> PurePosixPath:
    parts: list[str] = []
    for part in path.parts:
        if part in ("", "."):
            continue
        if part == "..":
            if not parts:
                raise HandoffError(f"snapshot archive path escapes its root: {path}")
            parts.pop()
            continue
        parts.append(part)
    if not parts:
        raise HandoffError(f"snapshot archive has an unsafe member path: {path!s}")
    return PurePosixPath(*parts)


def _require_workspace_link_target(member_name: str, target: PurePosixPath) -> None:
    if not target.parts or target.parts[0] != "workspace":
        raise HandoffError(
            "snapshot archive link escapes the portable workspace: "
            f"{member_name} -> {target}"
        )


def _validate_manifest(manifest: Any, member_names: set[str]) -> str:
    if not isinstance(manifest, dict):
        raise HandoffError("snapshot manifest must be a JSON object")
    if manifest.get("format_version") != ARCHIVE_FORMAT_VERSION:
        raise HandoffError(
            "unsupported snapshot format version: "
            f"{manifest.get('format_version')!r}"
        )

    session = manifest.get("session")
    if not isinstance(session, dict):
        raise HandoffError("snapshot manifest is missing session metadata")
    session_id = _normalize_session_id(session.get("id"))
    rollout_path = session.get("rollout_archive_path")
    if not isinstance(rollout_path, str) or rollout_path not in member_names:
        raise HandoffError("snapshot archive is missing its selected rollout")

    repository = manifest.get("repository")
    if not isinstance(repository, dict) or not isinstance(repository.get("present"), bool):
        raise HandoffError("snapshot manifest has invalid repository metadata")
    if repository["present"]:
        if repository.get("bundle_archive_path") != GIT_BUNDLE_ARCHIVE:
            raise HandoffError("snapshot manifest has an invalid Git repository bundle path")
        for artifact in (GIT_BUNDLE_ARCHIVE, "git/staged.patch", "git/unstaged.patch"):
            if artifact not in member_names:
                raise HandoffError(f"snapshot archive is missing Git artifact: {artifact}")

    if not any(name == "workspace" or name.startswith("workspace/") for name in member_names):
        raise HandoffError("snapshot archive is missing workspace content")
    if not any(name == "codex" or name.startswith("codex/") for name in member_names):
        raise HandoffError("snapshot archive is missing Codex session content")
    if "codex/auth.json" in member_names:
        raise HandoffError(
            "snapshot archive must not contain Codex auth.json; "
            "Baton authenticates only with the Modal OPENAI_API_KEY Secret"
        )
    return session_id


def _reject_native_restore_artifacts(member_names: set[str]) -> None:
    for name in member_names:
        path = PurePosixPath(name)
        if not path.parts or path.parts[0] != "workspace":
            continue
        relative_parts = path.parts[1:]
        if any(component in NATIVE_BUILD_COMPONENTS for component in relative_parts):
            raise HandoffError(
                "snapshot contains dependency/build output that cannot cross macOS arm64 to "
                f"Linux x86_64: {name}. Baton will not rebuild native dependencies."
            )
        if relative_parts and relative_parts[-1] == "binding.gyp":
            raise HandoffError(
                "snapshot contains binding.gyp, which requires a native Node build. "
                "Baton will not rebuild native dependencies."
            )
        if path.suffix.lower() in NATIVE_ARTIFACT_SUFFIXES:
            raise HandoffError(
                "snapshot contains a native binary that cannot cross macOS arm64 to Linux x86_64: "
                f"{name}"
            )


def _normalize_session_id(session_id: Any) -> str:
    try:
        return str(UUID(str(session_id).strip()))
    except (AttributeError, ValueError) as error:
        raise HandoffError("session ID must be a UUID") from error


def _normalize_codex_version(value: str) -> str:
    if not re.fullmatch(r"\d+\.\d+\.\d+", value):
        raise HandoffError("Codex version must be a release version such as 0.147.0")
    return value


def _required_repository_value(repository: Mapping[str, Any], key: str) -> str:
    value = repository.get(key)
    if not isinstance(value, str) or not value:
        raise HandoffError(
            f"snapshot Git metadata has no {key}; cannot reconstruct the Git worktree remotely"
        )
    return value


def _validate_timeout(label: str, value: int) -> None:
    if not isinstance(value, int) or value <= 0 or value > 24 * 60 * 60:
        raise HandoffError(f"{label} must be between 1 and 86400 seconds")


def _require_nonempty_label(label: str, value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise HandoffError(f"{label} must not be empty")


def _read_all(stream: Any) -> str:
    return _coerce_text(stream.read())


def _coerce_text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _process_returncode(process: Any) -> int:
    returncode = process.returncode
    if not isinstance(returncode, int):
        raise HandoffError("remote process did not report an exit code")
    return returncode


def _sandbox_id(sandbox: Any) -> str:
    value = getattr(sandbox, "object_id", None) or getattr(sandbox, "id", None)
    return str(value) if value else "unknown"


def _cleanup_sandbox(sandbox: Any) -> HandoffError | None:
    failures: list[str] = []
    try:
        sandbox.terminate()
    except Exception as error:
        failures.append(f"terminate failed: {error}")
    try:
        sandbox.detach()
    except Exception as error:
        failures.append(f"detach failed: {error}")
    if failures:
        return HandoffError("Modal Sandbox cleanup failed: " + "; ".join(failures))
    return None


def _load_modal() -> Any:
    try:
        import modal
    except ImportError as error:
        raise HandoffError("Modal SDK is required; install Baton with its Modal dependency") from error
    return modal
