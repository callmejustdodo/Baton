"""Runloop Devbox integration used by Baton.

This module intentionally wraps only the Runloop SDK.  It is not a pluggable
backend layer: Baton now has one remote runtime, Runloop Devboxes.  The small
wrapper keeps the command/file semantics used by the handoff safety checks
explicit and testable.
"""

from __future__ import annotations

import os
import re
import shlex
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any

DEFAULT_RUNLOOP_BLUEPRINT = "baton-codex"
DEFAULT_RUNLOOP_SECRET = "BATON_OPENAI_API_KEY"
RUNLOOP_BASE_BLUEPRINT = "runloop/universal-ubuntu-24.04-x86_64"
_ACCOUNT_SECRET_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class RunloopClientError(RuntimeError):
    """Raised when Baton cannot use the configured Runloop account."""


def validate_account_secret_name(name: str) -> str:
    """Require the environment-variable-style names accepted by Runloop."""

    if not _ACCOUNT_SECRET_NAME.fullmatch(name):
        raise RunloopClientError(
            "Runloop account secret name must start with a letter or underscore and "
            "contain only letters, numbers, and underscores"
        )
    return name


def render_command(argv: Iterable[str]) -> str:
    """Render a fixed argv as one shell command without interpolation.

    Runloop's execution API accepts a shell command string.  Every Baton
    command therefore passes through ``shlex.join`` so a prompt, path, or
    session ID remains a single argument rather than executable shell source.
    """

    command = list(argv)
    if not command or any(not isinstance(part, str) for part in command):
        raise RunloopClientError("remote command must contain string arguments")
    return shlex.join(command)


def load_runloop_client() -> Any:
    """Instantiate the official synchronous Runloop client from its API key."""

    token = os.environ.get("RUNLOOP_API_KEY")
    if not token:
        raise RunloopClientError(
            "RUNLOOP_API_KEY is required; create a Runloop API key and export it before "
            "running Baton"
        )
    try:
        from runloop_api_client import Runloop
    except ImportError as error:
        raise RunloopClientError(
            "Runloop SDK is required; install Baton with its Runloop dependency"
        ) from error
    return Runloop(bearer_token=token)


def create_devbox(
    client: Any,
    *,
    blueprint_name: str,
    secret_name: str,
    name: str,
    idle_suspend_seconds: int,
) -> RunloopDevbox:
    """Create a ready x86_64 Devbox that suspends after its work is idle."""

    secret_name = validate_account_secret_name(secret_name)
    if not isinstance(idle_suspend_seconds, int) or idle_suspend_seconds <= 0:
        raise RunloopClientError("Runloop idle suspend time must be a positive number of seconds")

    try:
        view = client.devboxes.create_and_await_running(
            blueprint_name=blueprint_name,
            name=name,
            secrets={"OPENAI_API_KEY": secret_name},
            launch_parameters={
                "architecture": "x86_64",
                "lifecycle": {
                    "after_idle": {
                        "idle_time_seconds": idle_suspend_seconds,
                        "on_idle": "suspend",
                    }
                },
            },
            metadata={"baton": "handoff"},
        )
    except Exception as error:
        raise RunloopClientError(f"Runloop Devbox creation failed: {error}") from error
    devbox_id = _object_id(view)
    if devbox_id is None:
        raise RunloopClientError("Runloop created a Devbox without an ID")
    status = getattr(view, "status", None)
    if status is not None and status != "running":
        reason = getattr(view, "failure_reason", None) or getattr(view, "shutdown_reason", None)
        detail = f" ({reason})" if isinstance(reason, str) and reason else ""
        raise RunloopClientError(
            f"Runloop Devbox did not reach the running state: {status or 'unknown'}{detail}"
        )
    return RunloopDevbox(client, devbox_id)


def open_devbox(client: Any, devbox_id: str) -> RunloopDevbox:
    """Open a Devbox, resuming its suspended disk state when needed."""

    try:
        view = client.devboxes.retrieve(devbox_id)
    except Exception as error:
        raise RunloopClientError(f"Runloop Devbox lookup failed: {error}") from error
    actual_id = _object_id(view)
    if actual_id is None:
        raise RunloopClientError("Runloop returned a Devbox without an ID")
    if actual_id != devbox_id:
        raise RunloopClientError("Runloop returned a different Devbox than requested")
    status = getattr(view, "status", None)
    if status in {"shutdown", "failure"}:
        reason = getattr(view, "shutdown_reason", None) or getattr(view, "failure_reason", None)
        detail = f" ({reason})" if isinstance(reason, str) and reason else ""
        raise RunloopClientError(f"Runloop Devbox is no longer available: {status}{detail}")
    try:
        if status == "suspending":
            client.devboxes.await_suspended(actual_id)
            status = "suspended"
        if status == "suspended":
            client.devboxes.resume(actual_id)
            status = "resuming"
        if status in {"resuming", "scheduled", "queued", "provisioning", "initializing"}:
            client.devboxes.await_running(actual_id)
    except Exception as error:
        raise RunloopClientError(f"Runloop Devbox could not resume: {error}") from error
    return RunloopDevbox(client, actual_id)


def build_blueprint(
    client: Any,
    *,
    blueprint_name: str,
    codex_version: str,
    runtime_user: str,
) -> Any:
    """Build a reusable Codex runtime without a checked-in Dockerfile."""

    if not re.fullmatch(r"[a-z_][a-z0-9_-]*[$]?", runtime_user):
        raise RunloopClientError("Runloop runtime user name is invalid")
    if not re.fullmatch(r"\d+\.\d+\.\d+", codex_version):
        raise RunloopClientError("Codex version must use major.minor.patch format")
    quoted_user = shlex.quote(runtime_user)
    sudoers_rule = shlex.quote(f"{runtime_user} ALL=(ALL:ALL) !ALL")
    codex_install_command = (
        'node_bin="$(dirname "$(command -v node)")"; '
        'npm_bin="$(command -v npm)"; '
        'test -n "$node_bin" && test -n "$npm_bin"; '
        f"sudo -n env NODE_BIN=\"$node_bin\" NPM_BIN=\"$npm_bin\" "
        f"CODEX_VERSION={shlex.quote(codex_version)} sh -c "
        + shlex.quote(
            'PATH="$NODE_BIN:$PATH"; '
            '"$NPM_BIN" install --global --prefix /usr/local "@openai/codex@$CODEX_VERSION"; '
            '/usr/bin/install -m 755 "$NODE_BIN/node" /usr/local/bin/node; '
            '/usr/local/bin/codex --version'
        )
    )
    commands = (
        "sudo apt-get update",
        "sudo apt-get install -y --no-install-recommends coreutils git gzip passwd procps sudo tar util-linux",
        (
            f"id -u {quoted_user} >/dev/null 2>&1 || "
            f"sudo useradd --create-home --shell /bin/bash {quoted_user}"
        ),
        f"sudo gpasswd --delete {quoted_user} sudo || true",
        (
            f"printf '%s\\n' {sudoers_rule} | "
            "sudo tee /etc/sudoers.d/99-baton-agent-deny >/dev/null"
        ),
        "sudo chmod 440 /etc/sudoers.d/99-baton-agent-deny",
        "sudo visudo -cf /etc/sudoers.d/99-baton-agent-deny",
        codex_install_command,
        "/usr/local/bin/codex --version",
        "git --version",
    )
    try:
        return client.blueprints.create_and_await_build_complete(
            name=blueprint_name,
            base_blueprint_name=RUNLOOP_BASE_BLUEPRINT,
            launch_parameters={"architecture": "x86_64"},
            system_setup_commands=commands,
            metadata={"baton": "runtime", "codex_version": codex_version},
        )
    except Exception as error:
        raise RunloopClientError(f"Runloop Blueprint build failed: {error}") from error


@dataclass
class _CompletedExecution:
    stdout: str
    stderr: str
    returncode: int


class _RunloopStream:
    """A file-like stdout/stderr view over one Runloop execution."""

    def __init__(self, process: RunloopProcess, stream_name: str) -> None:
        self._process = process
        self._stream_name = stream_name

    def read(self) -> str:
        completed = self._process._completed_execution()
        return completed.stdout if self._stream_name == "stdout" else completed.stderr

    def __iter__(self) -> Iterator[str]:
        if self._stream_name != "stdout":
            value = self.read()
            if value:
                yield value
            return
        yield from self._process._stream_stdout()


class RunloopProcess:
    """Expose Runloop execution output in the limited shape Baton consumes."""

    def __init__(self, client: Any, devbox_id: str, execution: Any) -> None:
        self._client = client
        self._devbox_id = devbox_id
        execution_id = _object_id(execution, "execution_id")
        if execution_id is None:
            raise RunloopClientError("Runloop started an execution without an ID")
        self._execution_id = execution_id
        self._completed: _CompletedExecution | None = None
        self._completion_lock = Lock()
        self.stdout = _RunloopStream(self, "stdout")
        self.stderr = _RunloopStream(self, "stderr")

    @property
    def returncode(self) -> int | None:
        return self._completed.returncode if self._completed is not None else None

    def wait(self) -> int:
        return self._completed_execution().returncode

    def _completed_execution(self) -> _CompletedExecution:
        with self._completion_lock:
            if self._completed is None:
                try:
                    result = self._client.devboxes.executions.await_completed(
                        self._execution_id,
                        devbox_id=self._devbox_id,
                    )
                except Exception as error:
                    raise RunloopClientError(
                        f"Runloop execution {self._execution_id} could not be completed: {error}"
                    ) from error
                status = getattr(result, "exit_status", None)
                if not isinstance(status, int):
                    raise RunloopClientError(
                        f"Runloop execution {self._execution_id} did not report an exit code"
                    )
                self._completed = _CompletedExecution(
                    stdout=_as_text(getattr(result, "stdout", "")),
                    stderr=_as_text(getattr(result, "stderr", "")),
                    returncode=status,
                )
            return self._completed

    def _stream_stdout(self) -> Iterator[str]:
        try:
            updates = self._client.devboxes.executions.stream_stdout_updates(
                self._execution_id,
                devbox_id=self._devbox_id,
            )
            for update in updates:
                output = _as_text(getattr(update, "output", ""))
                if output:
                    yield output
        except Exception as error:
            raise RunloopClientError(
                f"Runloop execution {self._execution_id} output stream failed: {error}"
            ) from error


class RunloopFilesystem:
    """Runloop file operations used by the existing Baton archive safeguards."""

    def __init__(self, client: Any, devbox_id: str) -> None:
        self._client = client
        self._devbox_id = devbox_id

    def copy_from_local(self, source: Path, destination: str) -> None:
        with source.open("rb") as archive:
            self._client.devboxes.upload_file(self._devbox_id, path=destination, file=archive)

    def copy_to_local(self, source: str, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        response = self._client.devboxes.download_file(self._devbox_id, path=source)
        if hasattr(response, "write_to_file"):
            response.write_to_file(destination)
            return
        destination.write_bytes(bytes(response))

    def write_text(self, text: str, destination: str) -> None:
        self._client.devboxes.write_file_contents(
            self._devbox_id,
            file_path=destination,
            contents=text,
        )

    def read_text(self, source: str) -> str:
        return _as_text(
            self._client.devboxes.read_file_contents(self._devbox_id, file_path=source)
        )


class RunloopDevbox:
    """Concrete Runloop Devbox remote used by Baton, with safe argv rendering."""

    def __init__(self, client: Any, devbox_id: str) -> None:
        self._client = client
        self.id = devbox_id
        self.filesystem = RunloopFilesystem(client, devbox_id)

    def exec(self, *argv: str, timeout: int | None = None) -> RunloopProcess:
        command = list(argv)
        if timeout is not None:
            command = ["timeout", "--preserve-status", str(timeout), *command]
        try:
            execution = self._client.devboxes.executions.execute_async(
                self.id,
                command=render_command(command),
            )
        except Exception as error:
            raise RunloopClientError(f"Runloop command could not start: {error}") from error
        return RunloopProcess(self._client, self.id, execution)

    def terminate(self) -> None:
        self._client.devboxes.shutdown(self.id)

    def detach(self) -> None:
        """A detached Runloop execution keeps running without a client action."""


def _object_id(value: Any, attribute: str = "id") -> str | None:
    candidate = getattr(value, attribute, None)
    return candidate if isinstance(candidate, str) and candidate else None


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)
