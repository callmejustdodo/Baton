from __future__ import annotations

import io
import json
import shlex
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from typing import Any

from baton.handoff import (
    REMOTE_ARCHIVE,
    REMOTE_CODEX_HOME,
    REMOTE_COMPLETION_MARKER,
    REMOTE_COMPLETION_TEMP,
    REMOTE_CONTROL_DIR,
    REMOTE_GIT_BASELINE,
    REMOTE_ROOT,
    REMOTE_RUNTIME_USER,
    REMOTE_WORKSPACE,
    HandoffError,
    _capture_remote_git_state,
    blueprint_name_for_version,
    build_resume_command,
    handoff_archive,
    inspect_snapshot_archive,
    prepare_runtime,
)

SESSION_ID = "019f5ef4-780a-7973-a1d2-c460461ced1f"
ROLLOUT_PATH = (
    "codex/sessions/2026/08/23/"
    f"rollout-2026-08-23T13-14-26-{SESSION_ID}.jsonl"
)


class HandoffTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.archive = self.root / "snapshot.tar.gz"
        self._write_archive(self.archive)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_resume_command_uses_confirmed_noninteractive_argv(self) -> None:
        prompt = "say 'hello' $(whoami)\nand keep working"

        command = build_resume_command(SESSION_ID, prompt)

        self.assertEqual(
            command,
            [
                "codex",
                "exec",
                "--cd",
                REMOTE_WORKSPACE,
                "resume",
                SESSION_ID,
                "--json",
                "--dangerously-bypass-approvals-and-sandbox",
                prompt,
            ],
        )

    def test_inspect_archive_rejects_path_traversal_before_runloop_is_used(self) -> None:
        self._write_archive(self.archive, extra_members={"../outside.txt": b"nope"})

        with self.assertRaisesRegex(HandoffError, "escapes its root"):
            inspect_snapshot_archive(self.archive)

    def test_inspect_archive_rejects_native_build_marker(self) -> None:
        self._write_archive(self.archive, extra_members={"workspace/binding.gyp": b"{}"})

        with self.assertRaisesRegex(HandoffError, "native Node build"):
            inspect_snapshot_archive(self.archive)

    def test_inspect_archive_rejects_a_link_to_omitted_content(self) -> None:
        self._write_archive(self.archive, symlinks={"workspace/deps": "missing"})

        with self.assertRaisesRegex(HandoffError, "targets content that is absent"):
            inspect_snapshot_archive(self.archive)

    def test_inspect_archive_rejects_copied_codex_auth(self) -> None:
        for archive_path in (
            "codex/auth.json",
            "./codex/auth.json",
            "codex/./auth.json",
            "codex//auth.json",
        ):
            with self.subTest(archive_path=archive_path):
                self._write_archive(self.archive, extra_members={archive_path: b"oauth"})

                with self.assertRaisesRegex(HandoffError, "must not contain Codex auth.json"):
                    inspect_snapshot_archive(self.archive)

    def test_handoff_uploads_restores_and_streams_json_events(self) -> None:
        runloop = _FakeRunloopClient()
        events: list[dict[str, object]] = []

        result = handoff_archive(
            archive_path=self.archive,
            prompt="continue the task",
            runloop_secret="baton-openai",
            blueprint_name="baton-codex-0-147-0",
            on_event=events.append,
            runloop_client=runloop,
        )

        devbox = runloop.devbox
        self.assertEqual(result.devbox_id, "dbx-test")
        self.assertEqual(result.event_count, 2)
        self.assertEqual(
            runloop.create_calls,
            [
                {
                    "blueprint_name": "baton-codex-0-147-0",
                    "name": f"baton-{SESSION_ID[:8]}",
                    "secrets": {"OPENAI_API_KEY": "baton-openai"},
                    "launch_parameters": {
                        "architecture": "x86_64",
                        "keep_alive_time_seconds": 1200,
                    },
                    "metadata": {"baton": "handoff"},
                }
            ],
        )
        self.assertEqual(devbox.filesystem.copies, [(self.archive.resolve(), REMOTE_ARCHIVE)])
        self.assertTrue(devbox.terminated)
        self.assertEqual(events[0]["type"], "codex_event")
        self.assertEqual(events[1]["type"], "codex_event")

        commands = [command for command, _ in devbox.commands]
        auth_index = next(
            index
            for index, command in enumerate(commands)
            if command[:5]
            == ("sudo", "-n", "--preserve-env=OPENAI_API_KEY", "sh", "-c")
        )
        resume_index = next(
            index
            for index, command in enumerate(commands)
            if command[:6]
            == (
                "sudo",
                "-n",
                "-u",
                REMOTE_RUNTIME_USER,
                "--",
                "env",
            )
        )
        self.assertLess(auth_index, resume_index)
        auth_command = commands[auth_index]
        self.assertIn("$OPENAI_API_KEY", auth_command[5])
        self.assertIn("CODEX_HOME=/baton/.codex", auth_command[5])
        self.assertIn("codex login --with-api-key", auth_command[5])

        resume = commands[resume_index]
        self.assertEqual(
            resume,
            (
                "sudo",
                "-n",
                "-u",
                REMOTE_RUNTIME_USER,
                "--",
                "env",
                "HOME=/home/baton-agent",
                f"CODEX_HOME={REMOTE_CODEX_HOME}",
                "codex",
                "exec",
                "--cd",
                REMOTE_WORKSPACE,
                "resume",
                SESSION_ID,
                "--json",
                "--dangerously-bypass-approvals-and-sandbox",
                "--skip-git-repo-check",
                "continue the task",
            ),
        )

    def test_handoff_failure_cleans_up_the_devbox(self) -> None:
        runloop = _FakeRunloopClient(resume_returncode=2, resume_stderr="bad API key")

        with self.assertRaisesRegex(HandoffError, "remote Codex resume failed"):
            handoff_archive(
                archive_path=self.archive,
                prompt="continue the task",
                blueprint_name="baton-codex-0-147-0",
                runloop_client=runloop,
            )

        self.assertTrue(runloop.devbox.terminated)

    def test_handoff_buffers_split_and_combined_jsonl_chunks(self) -> None:
        runloop = _FakeRunloopClient(
            resume_stdout=[
                '{"type":"thread.',
                'started"}\n{"type":"turn.completed"}\n',
            ]
        )
        events: list[dict[str, object]] = []

        result = handoff_archive(
            archive_path=self.archive,
            prompt="continue the task",
            blueprint_name="baton-codex-0-147-0",
            on_event=events.append,
            runloop_client=runloop,
        )

        self.assertEqual(result.event_count, 2)
        self.assertEqual([event["type"] for event in events], ["codex_event", "codex_event"])
        self.assertEqual(events[0]["event"], {"type": "thread.started"})
        self.assertEqual(events[1]["event"], {"type": "turn.completed"})

    def test_git_handoff_restores_from_its_captured_bundle_without_an_origin(self) -> None:
        self._write_archive(
            self.archive,
            repository={
                "present": True,
                "head": "a" * 40,
                "branch": "main",
                "origin_url": None,
                "bundle_archive_path": "git/repository.bundle",
            },
        )
        runloop = _FakeRunloopClient()

        handoff_archive(
            archive_path=self.archive,
            prompt="continue the task",
            blueprint_name="baton-codex-0-147-0",
            runloop_client=runloop,
        )

        commands = [command for command, _ in runloop.devbox.commands]
        self.assertIn(
            (
                "sudo",
                "-n",
                "--",
                "git",
                "clone",
                "--no-checkout",
                "/baton/stage/git/repository.bundle",
                REMOTE_WORKSPACE,
            ),
            commands,
        )
        self.assertIn(
            (
                "sudo",
                "-n",
                "--",
                "git",
                "-C",
                REMOTE_WORKSPACE,
                "remote",
                "remove",
                "origin",
            ),
            commands,
        )
        self.assertIn(
            (
                "sudo",
                "-n",
                "--",
                "git",
                "-C",
                REMOTE_WORKSPACE,
                "apply",
                "--index",
                "--allow-empty",
                "/baton/stage/git/staged.patch",
            ),
            commands,
        )

    def test_handoff_rejects_native_output_after_git_restore_before_resuming(self) -> None:
        self._write_archive(
            self.archive,
            repository={
                "present": True,
                "head": "a" * 40,
                "branch": "main",
                "origin_url": None,
                "bundle_archive_path": "git/repository.bundle",
            },
        )
        runloop = _FakeRunloopClient(find_stdout=f"{REMOTE_WORKSPACE}/build\n")

        with self.assertRaisesRegex(HandoffError, "dependency/build output"):
            handoff_archive(
                archive_path=self.archive,
                prompt="continue the task",
                blueprint_name="baton-codex-0-147-0",
                runloop_client=runloop,
            )

        self.assertFalse(
            any("codex" in command for command, _ in runloop.devbox.commands)
        )

    def test_cleanup_failure_is_reported_after_an_otherwise_successful_handoff(self) -> None:
        runloop = _FakeRunloopClient(terminate_error=RuntimeError("network unavailable"))

        with self.assertRaisesRegex(HandoffError, "Devbox cleanup failed"):
            handoff_archive(
                archive_path=self.archive,
                prompt="continue the task",
                blueprint_name="baton-codex-0-147-0",
                runloop_client=runloop,
            )

    def test_cleanup_failure_is_visible_alongside_a_resume_failure(self) -> None:
        runloop = _FakeRunloopClient(
            resume_returncode=2,
            resume_stderr="bad API key",
            terminate_error=RuntimeError("network unavailable"),
        )

        with self.assertRaisesRegex(
            HandoffError,
            "remote Codex resume failed.*Devbox cleanup failed.*network unavailable",
        ):
            handoff_archive(
                archive_path=self.archive,
                prompt="continue the task",
                blueprint_name="baton-codex-0-147-0",
                runloop_client=runloop,
            )

    def test_detached_handoff_does_not_terminate_the_running_devbox(self) -> None:
        runloop = _FakeRunloopClient()

        result = handoff_archive(
            archive_path=self.archive,
            prompt="continue the task",
            blueprint_name="baton-codex-0-147-0",
            detach=True,
            runloop_client=runloop,
        )

        self.assertTrue(result.detached)
        self.assertIsNone(result.exit_code)
        self.assertFalse(runloop.devbox.terminated)
        completion_command = next(
            command
            for command, _ in runloop.devbox.commands
            if command[:4] == ("sudo", "-n", "sh", "-c")
            and "marker_temp=$2" in command[4]
        )
        self.assertEqual(
            completion_command[5:18],
            (
                "baton-resume",
                REMOTE_GIT_BASELINE,
                REMOTE_COMPLETION_TEMP,
                REMOTE_COMPLETION_MARKER,
                REMOTE_RUNTIME_USER,
                "sudo",
                "-n",
                "-u",
                REMOTE_RUNTIME_USER,
                "--",
                "env",
                "HOME=/home/baton-agent",
                f"CODEX_HOME={REMOTE_CODEX_HOME}",
            ),
        )
        self.assertIn('"$@"', completion_command[4])
        self.assertNotIn(REMOTE_GIT_BASELINE, completion_command[4])
        self.assertNotIn(REMOTE_COMPLETION_MARKER, completion_command[4])
        self.assertNotIn(REMOTE_COMPLETION_TEMP, completion_command[4])
        self.assertLess(
            completion_command[4].index("/usr/bin/pkill"),
            completion_command[4].index("printf"),
        )

    def test_handoff_provisions_an_unprivileged_runtime_boundary(self) -> None:
        runloop = _FakeRunloopClient()

        handoff_archive(
            archive_path=self.archive,
            prompt="continue the task",
            blueprint_name="baton-codex-0-147-0",
            runloop_client=runloop,
        )

        commands = [command for command, _ in runloop.devbox.commands]
        root = ("sudo", "-n", "--")
        self.assertIn((*root, "mkdir", "-p", REMOTE_CONTROL_DIR), commands)
        self.assertIn(
            (*root, "chown", "root:root", REMOTE_ROOT, REMOTE_CONTROL_DIR),
            commands,
        )
        self.assertIn((*root, "chmod", "755", REMOTE_ROOT), commands)
        self.assertIn((*root, "chmod", "700", REMOTE_CONTROL_DIR), commands)
        self.assertIn(
            (
                *root,
                "chown",
                "-R",
                f"{REMOTE_RUNTIME_USER}:{REMOTE_RUNTIME_USER}",
                REMOTE_CODEX_HOME,
                REMOTE_WORKSPACE,
            ),
            commands,
        )
        self.assertIn(
            (
                "sudo",
                "-n",
                "-u",
                REMOTE_RUNTIME_USER,
                "--",
                "test",
                "!",
                "-w",
                REMOTE_ROOT,
            ),
            commands,
        )
        self.assertIn(
            (
                "sudo",
                "-n",
                "-u",
                REMOTE_RUNTIME_USER,
                "--",
                "test",
                "!",
                "-r",
                REMOTE_CONTROL_DIR,
            ),
            commands,
        )

    def test_detached_git_handoff_records_baseline_without_shell_interpolation(self) -> None:
        self._write_archive(
            self.archive,
            repository={
                "present": True,
                "head": "a" * 40,
                "branch": "main",
                "origin_url": None,
                "bundle_archive_path": "git/repository.bundle",
            },
        )
        expected_git_state = {
            "head": "a" * 40,
            "branch": "main",
            "status": "M  app.py\0",
            "index": f"100644 {'b' * 40} 0\tapp.py\0",
            "index_flags": "H app.py\0",
            "refs": f"refs/heads/main\0{'a' * 40}\0\0\n",
        }
        runloop = _FakeRunloopClient(
            git_stdout_by_command={
                (
                    "sudo",
                    "-n",
                    "-u",
                    REMOTE_RUNTIME_USER,
                    "--",
                    "git",
                    "-C",
                    REMOTE_WORKSPACE,
                    "rev-parse",
                    "--show-toplevel",
                ): f"{REMOTE_WORKSPACE}\n",
                (
                    "sudo",
                    "-n",
                    "-u",
                    REMOTE_RUNTIME_USER,
                    "--",
                    "git",
                    "-C",
                    REMOTE_WORKSPACE,
                    "rev-parse",
                    "HEAD",
                ): f"{expected_git_state['head']}\n",
                (
                    "sudo",
                    "-n",
                    "-u",
                    REMOTE_RUNTIME_USER,
                    "--",
                    "git",
                    "-C",
                    REMOTE_WORKSPACE,
                    "branch",
                    "--show-current",
                ): f"{expected_git_state['branch']}\n",
                (
                    "sudo",
                    "-n",
                    "-u",
                    REMOTE_RUNTIME_USER,
                    "--",
                    "git",
                    "-C",
                    REMOTE_WORKSPACE,
                    "status",
                    "--porcelain=v1",
                    "-z",
                    "--untracked-files=all",
                    "--",
                    ".",
                    ":(exclude).baton",
                ): expected_git_state["status"],
                (
                    "sudo",
                    "-n",
                    "-u",
                    REMOTE_RUNTIME_USER,
                    "--",
                    "git",
                    "-C",
                    REMOTE_WORKSPACE,
                    "ls-files",
                    "--stage",
                    "-z",
                    "--",
                ): expected_git_state["index"],
                (
                    "sudo",
                    "-n",
                    "-u",
                    REMOTE_RUNTIME_USER,
                    "--",
                    "git",
                    "-C",
                    REMOTE_WORKSPACE,
                    "ls-files",
                    "-v",
                    "-z",
                    "--",
                ): expected_git_state["index_flags"],
                (
                    "sudo",
                    "-n",
                    "-u",
                    REMOTE_RUNTIME_USER,
                    "--",
                    "git",
                    "-C",
                    REMOTE_WORKSPACE,
                    "for-each-ref",
                    "--format=%(refname)%00%(objectname)%00%(symref)%00",
                ): expected_git_state["refs"],
            }
        )

        handoff_archive(
            archive_path=self.archive,
            prompt="continue the task",
            blueprint_name="baton-codex-0-147-0",
            detach=True,
            runloop_client=runloop,
        )

        serialized_state = json.dumps(expected_git_state, separators=(",", ":"))
        completion_command = next(
            command
            for command, _ in runloop.devbox.commands
            if command[:4] == ("sudo", "-n", "sh", "-c")
            and "marker_temp=$2" in command[4]
        )
        commands = [command for command, _ in runloop.devbox.commands]
        completion_index = commands.index(completion_command)
        ownership_boundary_index = commands.index(
            (
                "sudo",
                "-n",
                "--",
                "chown",
                "-R",
                f"{REMOTE_RUNTIME_USER}:{REMOTE_RUNTIME_USER}",
                REMOTE_CODEX_HOME,
                REMOTE_WORKSPACE,
            )
        )
        self.assertLess(
            commands.index(
                (
                    "sudo",
                    "-n",
                    "--",
                    "git",
                    "clone",
                    "--no-checkout",
                    "/baton/stage/git/repository.bundle",
                    REMOTE_WORKSPACE,
                )
            ),
            ownership_boundary_index,
        )
        for git_command in runloop.devbox.git_stdout_by_command:
            self.assertGreater(commands.index(git_command), ownership_boundary_index)
            self.assertLess(commands.index(git_command), completion_index)
            self.assertEqual(
                git_command[:8],
                (
                    "sudo",
                    "-n",
                    "-u",
                    REMOTE_RUNTIME_USER,
                    "--",
                    "git",
                    "-C",
                    REMOTE_WORKSPACE,
                ),
            )
        self.assertLess(
            commands.index(
                ("sudo", "-n", "--", "chmod", "600", REMOTE_GIT_BASELINE)
            ),
            completion_index,
        )
        baseline_write = next(
            command
            for command in commands
            if command[:5] == ("sudo", "-n", "sh", "-c", 'umask 077; printf "%s" "$1" > "$2"')
        )
        self.assertEqual(baseline_write[-2:], (serialized_state, REMOTE_GIT_BASELINE))
        self.assertEqual(completion_command[6], REMOTE_GIT_BASELINE)
        self.assertEqual(completion_command[7], REMOTE_COMPLETION_TEMP)
        self.assertEqual(completion_command[8], REMOTE_COMPLETION_MARKER)
        self.assertEqual(completion_command[9], REMOTE_RUNTIME_USER)
        self.assertNotIn(REMOTE_GIT_BASELINE, completion_command[4])
        self.assertNotIn(REMOTE_COMPLETION_TEMP, completion_command[4])
        self.assertNotIn(REMOTE_COMPLETION_MARKER, completion_command[4])
        self.assertNotIn(serialized_state, completion_command[4])
        self.assertIn('baseline_path=$1', completion_command[4])

    def test_detached_non_git_handoff_records_null_git_baseline(self) -> None:
        runloop = _FakeRunloopClient()

        handoff_archive(
            archive_path=self.archive,
            prompt="continue the task",
            blueprint_name="baton-codex-0-147-0",
            detach=True,
            runloop_client=runloop,
        )

        self.assertTrue(
            any(
                command[-2:] == ("null", REMOTE_GIT_BASELINE)
                for command, _ in runloop.devbox.commands
            )
        )
        self.assertFalse(
            any(
                command[:8]
                == (
                    "sudo",
                    "-n",
                    "-u",
                    REMOTE_RUNTIME_USER,
                    "--",
                    "git",
                    "-C",
                    REMOTE_WORKSPACE,
                    "rev-parse",
                )
                for command, _ in runloop.devbox.commands
            )
        )

    def test_git_baseline_index_distinguishes_assume_unchanged_and_skip_worktree(
        self,
    ) -> None:
        repository = self.root / "git-state"
        repository.mkdir()
        _run_git(repository, "init", "-q")
        _run_git(repository, "config", "user.email", "baton@example.com")
        _run_git(repository, "config", "user.name", "Baton Test")
        (repository / "assumed.txt").write_text("assumed\n", encoding="utf-8")
        (repository / "skipped.txt").write_text("skipped\n", encoding="utf-8")
        _run_git(repository, "add", "assumed.txt", "skipped.txt")
        _run_git(repository, "commit", "-qm", "baseline")
        devbox = _LocalGitDevbox(repository)
        ordinary_index_flags = _capture_remote_git_state(devbox)["index_flags"]

        _run_git(repository, "update-index", "--assume-unchanged", "assumed.txt")
        _run_git(repository, "update-index", "--skip-worktree", "skipped.txt")

        flagged_index_flags = _capture_remote_git_state(devbox)["index_flags"]
        self.assertNotEqual(flagged_index_flags, ordinary_index_flags)

    def test_git_baseline_captures_remote_tracking_and_notes_refs(self) -> None:
        repository = self.root / "git-refs"
        repository.mkdir()
        _run_git(repository, "init", "-q")
        _run_git(repository, "config", "user.email", "baton@example.com")
        _run_git(repository, "config", "user.name", "Baton Test")
        (repository / "app.py").write_text("print('hello')\n", encoding="utf-8")
        _run_git(repository, "add", "app.py")
        _run_git(repository, "commit", "-qm", "baseline")
        _run_git(repository, "update-ref", "refs/remotes/origin/main", "HEAD")
        _run_git(repository, "update-ref", "refs/notes/review", "HEAD")

        refs = _capture_remote_git_state(_LocalGitDevbox(repository))["refs"]

        self.assertIn("refs/remotes/origin/main\0", refs)
        self.assertIn("refs/notes/review\0", refs)

    def test_prepare_builds_a_pinned_runloop_blueprint(self) -> None:
        runloop = _FakeRunloopClient()

        result = prepare_runtime(
            codex_version="0.147.0",
            blueprint_name="baton-codex-0-147-0",
            runloop_client=runloop,
        )

        self.assertEqual(result.blueprint_name, "baton-codex-0-147-0")
        self.assertEqual(result.codex_version, "0.147.0")
        self.assertEqual(
            runloop.blueprint_calls[0]["name"],
            "baton-codex-0-147-0",
        )
        commands = runloop.blueprint_calls[0]["launch_parameters"]["launch_commands"]
        self.assertTrue(any("git" in command for command in commands))
        self.assertIn("sudo useradd --create-home --shell /bin/bash baton-agent || true", commands)
        self.assertIn("sudo npm install --global @openai/codex@0.147.0", commands)

    def test_blueprint_name_tracks_the_codex_version(self) -> None:
        self.assertEqual(blueprint_name_for_version("0.147.0"), "baton-codex-0-147-0")

    def _write_archive(
        self,
        path: Path,
        *,
        extra_members: dict[str, bytes] | None = None,
        symlinks: dict[str, str] | None = None,
        repository: dict[str, object] | None = None,
    ) -> None:
        manifest = {
            "format_version": 1,
            "session": {"id": SESSION_ID, "rollout_archive_path": ROLLOUT_PATH},
            "repository": repository or {"present": False},
        }
        with tarfile.open(path, "w:gz") as archive:
            self._add_member(archive, "manifest.json", json.dumps(manifest).encode())
            self._add_member(
                archive,
                ROLLOUT_PATH,
                ('{"payload":{"session_id":"' + SESSION_ID + '"}}\n').encode(),
            )
            self._add_member(archive, "workspace/app.py", b"print('hello')\n")
            if repository and repository["present"]:
                self._add_member(archive, "git/repository.bundle", b"placeholder bundle")
                self._add_member(archive, "git/staged.patch", b"")
                self._add_member(archive, "git/unstaged.patch", b"")
            for name, contents in (extra_members or {}).items():
                self._add_member(archive, name, contents)
            for name, target in (symlinks or {}).items():
                member = tarfile.TarInfo(name)
                member.type = tarfile.SYMTYPE
                member.linkname = target
                archive.addfile(member)

    @staticmethod
    def _add_member(archive: tarfile.TarFile, name: str, contents: bytes) -> None:
        member = tarfile.TarInfo(name)
        member.size = len(contents)
        archive.addfile(member, io.BytesIO(contents))


class _FakeStream:
    def __init__(self, values: list[str] | None = None) -> None:
        self.values = values or []

    def __iter__(self):
        return iter(self.values)

    def read(self) -> str:
        return "".join(self.values)


class _FakeProcess:
    def __init__(
        self,
        *,
        stdout: list[str] | None = None,
        stderr: list[str] | None = None,
        returncode: int = 0,
    ) -> None:
        self.stdout = _FakeStream(stdout)
        self.stderr = _FakeStream(stderr)
        self.returncode = returncode

    def wait(self) -> int:
        return self.returncode


class _LocalGitDevbox:
    def __init__(self, repository: Path) -> None:
        self.repository = repository

    def exec(self, *command: str, **kwargs: object) -> _FakeProcess:
        translated = list(command)
        if translated[:5] == ["sudo", "-n", "-u", REMOTE_RUNTIME_USER, "--"]:
            translated = translated[5:]
        if translated[:3] == ["git", "-C", REMOTE_WORKSPACE]:
            translated[2] = str(self.repository)
        completed = subprocess.run(
            translated,
            capture_output=True,
            check=False,
            text=True,
        )
        stdout = completed.stdout
        if command == (
            "sudo",
            "-n",
            "-u",
            REMOTE_RUNTIME_USER,
            "--",
            "git",
            "-C",
            REMOTE_WORKSPACE,
            "rev-parse",
            "--show-toplevel",
        ):
            stdout = f"{REMOTE_WORKSPACE}\n"
        return _FakeProcess(
            stdout=[stdout],
            stderr=[completed.stderr],
            returncode=completed.returncode,
        )


def _run_git(repository: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", "-C", str(repository), *arguments],
        capture_output=True,
        check=True,
        text=True,
    )


class _FakeFilesystem:
    def __init__(self) -> None:
        self.copies: list[tuple[Path, str]] = []

    def copy_from_local(self, local_path: Path, remote_path: str) -> None:
        self.copies.append((Path(local_path).resolve(), remote_path))

class _FakeDevboxState:
    def __init__(
        self,
        *,
        resume_returncode: int,
        resume_stderr: str,
        resume_stdout: list[str] | None = None,
        find_stdout: str = "",
        git_stdout_by_command: dict[tuple[str, ...], str] | None = None,
        terminate_error: Exception | None = None,
    ) -> None:
        self.id = "dbx-test"
        self.filesystem = _FakeFilesystem()
        self.commands: list[tuple[tuple[str, ...], dict[str, object]]] = []
        self.resume_returncode = resume_returncode
        self.resume_stderr = resume_stderr
        self.resume_stdout = resume_stdout or [
            '{"type":"thread.started"}\n',
            '{"type":"item.completed"}\n',
        ]
        self.find_stdout = find_stdout
        self.git_stdout_by_command = git_stdout_by_command or {}
        self.terminate_error = terminate_error
        self.terminated = False


class _Object:
    def __init__(self, **values: object) -> None:
        self.__dict__.update(values)


class _FakeExecutions:
    def __init__(self, parent: _FakeRunloopClient) -> None:
        self.parent = parent
        self.results: dict[str, _FakeProcess] = {}

    def execute_async(self, devbox_id: str, *, command: str) -> _Object:
        parsed = tuple(shlex.split(command))
        command_metadata: dict[str, object] = {}
        if parsed[:2] == ("timeout", "--preserve-status"):
            command_metadata["timeout"] = int(parsed[2])
            parsed = parsed[3:]
        execution_id = f"exec-{len(self.results) + 1}"
        state = self.parent.devbox
        state.commands.append((parsed, command_metadata))
        if "codex" in parsed:
            process = _FakeProcess(
                stdout=state.resume_stdout,
                stderr=[state.resume_stderr],
                returncode=state.resume_returncode,
            )
        elif "find" in parsed:
            process = _FakeProcess(stdout=[state.find_stdout])
        elif parsed in state.git_stdout_by_command:
            process = _FakeProcess(stdout=[state.git_stdout_by_command[parsed]])
        else:
            process = _FakeProcess()
        self.results[execution_id] = process
        return _Object(execution_id=execution_id)

    def await_completed(self, execution_id: str, *, devbox_id: str) -> _Object:
        process = self.results[execution_id]
        return _Object(
            stdout=process.stdout.read(),
            stderr=process.stderr.read(),
            exit_status=process.returncode,
        )

    def stream_stdout_updates(self, execution_id: str, *, devbox_id: str):
        for chunk in self.results[execution_id].stdout.values:
            yield _Object(output=chunk)


class _FakeDevboxes:
    def __init__(self, parent: _FakeRunloopClient) -> None:
        self.parent = parent
        self.executions = _FakeExecutions(parent)

    def create_and_await_running(self, **kwargs: object) -> _Object:
        self.parent.create_calls.append(kwargs)
        return _Object(id=self.parent.devbox.id)

    def upload_file(self, devbox_id: str, *, path: str, file: Any) -> None:
        self.parent.devbox.filesystem.copies.append((Path(file.name).resolve(), path))

    def write_file_contents(
        self, devbox_id: str, *, file_path: str, contents: str
    ) -> None:
        raise AssertionError("handoff must write root control files through sudo")

    def shutdown(self, devbox_id: str) -> None:
        self.parent.devbox.terminated = True
        if self.parent.devbox.terminate_error is not None:
            raise self.parent.devbox.terminate_error


class _FakeBlueprints:
    def __init__(self, parent: _FakeRunloopClient) -> None:
        self.parent = parent

    def create_and_await_build_complete(self, **kwargs: object) -> _Object:
        self.parent.blueprint_calls.append(kwargs)
        return _Object(id="bpt-test")


class _FakeRunloopClient:
    def __init__(
        self,
        *,
        resume_returncode: int = 0,
        resume_stderr: str = "",
        resume_stdout: list[str] | None = None,
        find_stdout: str = "",
        git_stdout_by_command: dict[tuple[str, ...], str] | None = None,
        terminate_error: Exception | None = None,
    ) -> None:
        self.create_calls: list[dict[str, Any]] = []
        self.blueprint_calls: list[dict[str, Any]] = []
        self.devbox = _FakeDevboxState(
            resume_returncode=resume_returncode,
            resume_stderr=resume_stderr,
            resume_stdout=resume_stdout,
            find_stdout=find_stdout,
            git_stdout_by_command=git_stdout_by_command,
            terminate_error=terminate_error,
        )
        self.devboxes = _FakeDevboxes(self)
        self.blueprints = _FakeBlueprints(self)
