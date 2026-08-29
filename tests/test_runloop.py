from __future__ import annotations

import os
import shlex
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import TypedDict, cast
from unittest.mock import patch

from baton.runloop import (
    DEFAULT_RUNLOOP_SECRET,
    RUNLOOP_BASE_BLUEPRINT,
    RunloopClientError,
    build_blueprint,
    create_devbox,
    load_runloop_client,
    open_devbox,
    render_command,
    validate_account_secret_name,
)


class _DevboxLaunchParameters(TypedDict):
    architecture: str
    lifecycle: dict[str, dict[str, int | str]]


class _BlueprintLaunchParameters(TypedDict):
    architecture: str


class _DevboxCreateCall(TypedDict):
    blueprint_name: str
    name: str
    secrets: dict[str, str]
    launch_parameters: _DevboxLaunchParameters
    metadata: dict[str, str]


class _BlueprintBuildCall(TypedDict):
    name: str
    base_blueprint_name: str
    launch_parameters: _BlueprintLaunchParameters
    system_setup_commands: tuple[str, ...]
    metadata: dict[str, str]


class RunloopCommandTests(unittest.TestCase):
    def test_shell_renderer_preserves_adversarial_arguments_as_arguments(self) -> None:
        argv = [
            "codex",
            "exec",
            "resume",
            "session id; $(touch should-not-run)",
            "line one\nline two 'quoted' \"double\" * $HOME ; --flag",
        ]

        rendered = render_command(argv)

        self.assertEqual(shlex.split(rendered), argv)
        self.assertTrue(rendered.startswith("codex "))

    def test_renderer_rejects_empty_or_non_string_argv(self) -> None:
        with self.assertRaisesRegex(RunloopClientError, "string arguments"):
            render_command([])
        with self.assertRaisesRegex(RunloopClientError, "string arguments"):
            render_command(["echo", 1])  # type: ignore[list-item]

    def test_exec_submits_the_shlex_rendered_command_not_raw_shell_source(self) -> None:
        client = _FakeClient()
        devbox = create_devbox(
            client,
            blueprint_name="baton-codex-0-149-0",
            secret_name=DEFAULT_RUNLOOP_SECRET,
            name="baton-test",
            idle_suspend_seconds=300,
        )
        argv = ("printf", "%s", "hello; $(bad)\n'quoted'")

        process = devbox.exec(*argv, timeout=17)

        self.assertEqual(
            shlex.split(client.devboxes.executions.commands[0][1]),
            ["timeout", "--preserve-status", "17", *argv],
        )
        self.assertEqual("".join(process.stdout), "first\nsecond\n")
        self.assertEqual(process.wait(), 0)
        self.assertEqual(process.stderr.read(), "")


class RunloopDevboxTests(unittest.TestCase):
    def test_create_injects_only_the_named_account_secret(self) -> None:
        client = _FakeClient()

        devbox = create_devbox(
            client,
            blueprint_name="baton-codex-0-149-0",
            secret_name=DEFAULT_RUNLOOP_SECRET,
            name="baton-019f5ef4",
            idle_suspend_seconds=300,
        )

        self.assertEqual(devbox.id, "devbox-test")
        self.assertEqual(len(client.devboxes.create_calls), 1)
        kwargs = client.devboxes.create_calls[0]
        self.assertEqual(kwargs["blueprint_name"], "baton-codex-0-149-0")
        self.assertEqual(
            kwargs["secrets"], {"OPENAI_API_KEY": "BATON_OPENAI_API_KEY"}
        )
        self.assertEqual(kwargs["launch_parameters"]["architecture"], "x86_64")
        self.assertEqual(
            kwargs["launch_parameters"]["lifecycle"],
            {"after_idle": {"idle_time_seconds": 300, "on_idle": "suspend"}},
        )
        self.assertNotIn("keep_alive_time_seconds", kwargs["launch_parameters"])
        self.assertNotIn("RUNLOOP_API_KEY", repr(kwargs))

    def test_invalid_account_secret_name_fails_before_devbox_creation(self) -> None:
        client = _FakeClient()

        with self.assertRaisesRegex(RunloopClientError, "letters, numbers"):
            create_devbox(
                client,
                blueprint_name="baton-codex-0-149-0",
                secret_name="baton-openai",
                name="baton-019f5ef4",
                idle_suspend_seconds=300,
            )

        self.assertEqual(client.devboxes.create_calls, [])

    def test_create_rejects_a_devbox_that_failed_to_start(self) -> None:
        client = _FakeClient()
        client.devboxes.status = "failure"

        with self.assertRaisesRegex(RunloopClientError, "did not reach the running state: failure"):
            create_devbox(
                client,
                blueprint_name="baton-codex-0-149-0",
                secret_name=DEFAULT_RUNLOOP_SECRET,
                name="baton-019f5ef4",
                idle_suspend_seconds=300,
            )

    def test_account_secret_name_accepts_environment_variable_style(self) -> None:
        self.assertEqual(
            validate_account_secret_name("BATON_OPENAI_API_KEY"),
            "BATON_OPENAI_API_KEY",
        )

    def test_open_running_devbox_does_not_change_its_lifecycle(self) -> None:
        client = _FakeClient()

        devbox = open_devbox(client, "devbox-test")

        self.assertEqual(devbox.id, "devbox-test")
        self.assertEqual(client.devboxes.retrieve_calls, ["devbox-test"])
        self.assertEqual(client.devboxes.create_calls, [])
        self.assertEqual(client.devboxes.shutdown_calls, [])
        self.assertEqual(client.devboxes.resume_calls, [])
        self.assertEqual(client.devboxes.await_running_calls, [])

    def test_open_resumes_a_suspended_devbox(self) -> None:
        client = _FakeClient()
        client.devboxes.status = "suspended"

        devbox = open_devbox(client, "devbox-test")

        self.assertEqual(devbox.id, "devbox-test")
        self.assertEqual(client.devboxes.resume_calls, ["devbox-test"])
        self.assertEqual(client.devboxes.await_running_calls, ["devbox-test"])

    def test_blueprint_build_is_code_defined_and_has_no_dockerfile(self) -> None:
        client = _FakeClient()

        build_blueprint(
            client,
            blueprint_name="baton-codex-0-149-0",
            codex_version="0.149.0",
            runtime_user="baton-agent",
        )

        kwargs = client.blueprints.calls[0]
        self.assertEqual(kwargs["base_blueprint_name"], RUNLOOP_BASE_BLUEPRINT)
        self.assertEqual(kwargs["name"], "baton-codex-0-149-0")
        commands = kwargs["system_setup_commands"]
        self.assertEqual(kwargs["launch_parameters"], {"architecture": "x86_64"})
        self.assertTrue(any("CODEX_VERSION=0.149.0" in command for command in commands))
        self.assertTrue(any("--prefix /usr/local" in command for command in commands))
        self.assertTrue(any("install -m 755" in command for command in commands))
        self.assertFalse(any("launch_commands" in command for command in commands))
        self.assertTrue(any("git" in command for command in commands))
        self.assertFalse(any("Dockerfile" in command for command in commands))

    def test_filesystem_upload_and_download_use_binary_contents(self) -> None:
        client = _FakeClient()
        devbox = open_devbox(client, "devbox-test")
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "archive.tar.gz"
            source.write_bytes(b"archive-bytes")
            destination = root / "download.tar.gz"

            devbox.filesystem.copy_from_local(source, "/tmp/archive.tar.gz")
            devbox.filesystem.copy_to_local("/tmp/result.tar.gz", destination)

            self.assertEqual(client.devboxes.uploads, [("devbox-test", "/tmp/archive.tar.gz", b"archive-bytes")])
            self.assertEqual(destination.read_bytes(), b"downloaded")


class RunloopAuthTests(unittest.TestCase):
    def test_missing_api_key_has_a_clear_local_error(self) -> None:
        with (
            patch.dict(os.environ, {"RUNLOOP_API_KEY": ""}, clear=False),
            self.assertRaisesRegex(RunloopClientError, "RUNLOOP_API_KEY"),
        ):
            load_runloop_client()


class _FakeExecutions:
    def __init__(self) -> None:
        self.commands: list[tuple[str, str]] = []

    def execute_async(self, devbox_id: str, *, command: str) -> SimpleNamespace:
        self.commands.append((devbox_id, command))
        return SimpleNamespace(execution_id="execution-test")

    def stream_stdout_updates(self, execution_id: str, *, devbox_id: str):
        assert (execution_id, devbox_id) == ("execution-test", "devbox-test")
        return iter((SimpleNamespace(output="first\n"), SimpleNamespace(output="second\n")))

    def await_completed(self, execution_id: str, *, devbox_id: str) -> SimpleNamespace:
        assert (execution_id, devbox_id) == ("execution-test", "devbox-test")
        return SimpleNamespace(exit_status=0, stdout="first\nsecond\n", stderr="")


class _FakeDownload:
    def write_to_file(self, destination: Path) -> None:
        destination.write_bytes(b"downloaded")


class _FakeDevboxes:
    def __init__(self) -> None:
        self.executions = _FakeExecutions()
        self.create_calls: list[_DevboxCreateCall] = []
        self.retrieve_calls: list[str] = []
        self.shutdown_calls: list[str] = []
        self.resume_calls: list[str] = []
        self.await_running_calls: list[str] = []
        self.uploads: list[tuple[str, str, bytes]] = []
        self.status = "running"

    def create_and_await_running(self, **kwargs: object) -> SimpleNamespace:
        self.create_calls.append(cast(_DevboxCreateCall, kwargs))
        return SimpleNamespace(id="devbox-test", status=self.status)

    def retrieve(self, devbox_id: str) -> SimpleNamespace:
        self.retrieve_calls.append(devbox_id)
        return SimpleNamespace(id=devbox_id, status=self.status)

    def resume(self, devbox_id: str) -> None:
        self.resume_calls.append(devbox_id)

    def await_running(self, devbox_id: str) -> SimpleNamespace:
        self.await_running_calls.append(devbox_id)
        return SimpleNamespace(id=devbox_id, status="running")

    def shutdown(self, devbox_id: str) -> None:
        self.shutdown_calls.append(devbox_id)

    def upload_file(self, devbox_id: str, *, path: str, file: object) -> None:
        self.uploads.append((devbox_id, path, file.read()))  # type: ignore[union-attr]

    def download_file(self, devbox_id: str, *, path: str) -> _FakeDownload:
        assert (devbox_id, path) == ("devbox-test", "/tmp/result.tar.gz")
        return _FakeDownload()

    def write_file_contents(self, devbox_id: str, *, file_path: str, contents: str) -> None:
        return None

    def read_file_contents(self, devbox_id: str, *, file_path: str) -> str:
        return ""


class _FakeBlueprints:
    def __init__(self) -> None:
        self.calls: list[_BlueprintBuildCall] = []

    def create_and_await_build_complete(self, **kwargs: object) -> SimpleNamespace:
        self.calls.append(cast(_BlueprintBuildCall, kwargs))
        return SimpleNamespace(id="blueprint-test")


class _FakeClient:
    def __init__(self) -> None:
        self.devboxes = _FakeDevboxes()
        self.blueprints = _FakeBlueprints()
