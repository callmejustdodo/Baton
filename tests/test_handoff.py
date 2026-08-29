from __future__ import annotations

import io
import json
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path

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
    _runtime_image,
    build_resume_command,
    handoff_archive,
    inspect_snapshot_archive,
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

    def test_inspect_archive_rejects_path_traversal_before_modal_is_used(self) -> None:
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
        modal = _FakeModal()
        events: list[dict[str, object]] = []

        result = handoff_archive(
            archive_path=self.archive,
            prompt="continue the task",
            modal_secret="baton-openai",
            app_name="baton",
            image_name="baton-codex-0-147-0",
            on_event=events.append,
            modal_module=modal,
        )

        sandbox = modal.sandbox
        self.assertEqual(result.sandbox_id, "sb-test")
        self.assertEqual(result.event_count, 2)
        self.assertEqual(modal.app_lookup_calls, [("baton", True)])
        self.assertEqual(
            modal.secret_calls,
            [("baton-openai", ("OPENAI_API_KEY",))],
        )
        self.assertEqual(sandbox.filesystem.copies, [(self.archive.resolve(), REMOTE_ARCHIVE)])
        self.assertTrue(sandbox.terminated)
        self.assertTrue(sandbox.detached)
        self.assertEqual(events[0]["type"], "codex_event")
        self.assertEqual(events[1]["type"], "codex_event")

        commands = [command for command, _ in sandbox.commands]
        auth_index = next(index for index, command in enumerate(commands) if command[0] == "sh")
        resume_index = next(
            index
            for index, command in enumerate(commands)
            if command[:6]
            == (
                "runuser",
                "--user",
                REMOTE_RUNTIME_USER,
                "--preserve-environment",
                "--",
                "env",
            )
        )
        self.assertLess(auth_index, resume_index)
        auth_command = commands[auth_index]
        self.assertIn("$OPENAI_API_KEY", auth_command[2])
        self.assertIn("CODEX_HOME=/baton/.codex", auth_command[2])
        self.assertIn("codex login --with-api-key", auth_command[2])

        resume = commands[resume_index]
        self.assertEqual(
            resume,
            (
                "runuser",
                "--user",
                REMOTE_RUNTIME_USER,
                "--preserve-environment",
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

    def test_handoff_failure_cleans_up_the_sandbox(self) -> None:
        modal = _FakeModal(resume_returncode=2, resume_stderr="bad API key")

        with self.assertRaisesRegex(HandoffError, "remote Codex resume failed"):
            handoff_archive(
                archive_path=self.archive,
                prompt="continue the task",
                image_name="baton-codex-0-147-0",
                modal_module=modal,
            )

        self.assertTrue(modal.sandbox.terminated)
        self.assertTrue(modal.sandbox.detached)

    def test_handoff_buffers_split_and_combined_jsonl_chunks(self) -> None:
        modal = _FakeModal(
            resume_stdout=[
                '{"type":"thread.',
                'started"}\n{"type":"turn.completed"}\n',
            ]
        )
        events: list[dict[str, object]] = []

        result = handoff_archive(
            archive_path=self.archive,
            prompt="continue the task",
            image_name="baton-codex-0-147-0",
            on_event=events.append,
            modal_module=modal,
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
        modal = _FakeModal()

        handoff_archive(
            archive_path=self.archive,
            prompt="continue the task",
            image_name="baton-codex-0-147-0",
            modal_module=modal,
        )

        commands = [command for command, _ in modal.sandbox.commands]
        self.assertIn(
            (
                "git",
                "clone",
                "--no-checkout",
                "/baton/stage/git/repository.bundle",
                REMOTE_WORKSPACE,
            ),
            commands,
        )
        self.assertIn(
            ("git", "-C", REMOTE_WORKSPACE, "remote", "remove", "origin"),
            commands,
        )
        self.assertIn(
            (
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
        modal = _FakeModal(find_stdout=f"{REMOTE_WORKSPACE}/build\n")

        with self.assertRaisesRegex(HandoffError, "dependency/build output"):
            handoff_archive(
                archive_path=self.archive,
                prompt="continue the task",
                image_name="baton-codex-0-147-0",
                modal_module=modal,
            )

        self.assertFalse(
            any("codex" in command for command, _ in modal.sandbox.commands)
        )

    def test_cleanup_failure_is_reported_after_an_otherwise_successful_handoff(self) -> None:
        modal = _FakeModal(terminate_error=RuntimeError("network unavailable"))

        with self.assertRaisesRegex(HandoffError, "Sandbox cleanup failed"):
            handoff_archive(
                archive_path=self.archive,
                prompt="continue the task",
                image_name="baton-codex-0-147-0",
                modal_module=modal,
            )

    def test_cleanup_failure_is_visible_alongside_a_resume_failure(self) -> None:
        modal = _FakeModal(
            resume_returncode=2,
            resume_stderr="bad API key",
            terminate_error=RuntimeError("network unavailable"),
        )

        with self.assertRaisesRegex(
            HandoffError,
            "remote Codex resume failed.*Sandbox cleanup failed.*network unavailable",
        ):
            handoff_archive(
                archive_path=self.archive,
                prompt="continue the task",
                image_name="baton-codex-0-147-0",
                modal_module=modal,
            )

    def test_detached_handoff_does_not_terminate_the_running_sandbox(self) -> None:
        modal = _FakeModal()

        result = handoff_archive(
            archive_path=self.archive,
            prompt="continue the task",
            image_name="baton-codex-0-147-0",
            detach=True,
            modal_module=modal,
        )

        self.assertTrue(result.detached)
        self.assertIsNone(result.exit_code)
        self.assertFalse(modal.sandbox.terminated)
        self.assertTrue(modal.sandbox.detached)
        completion_command = next(
            command
            for command, _ in modal.sandbox.commands
            if command[0] == "sh" and "marker_temp=$2" in command[2]
        )
        self.assertEqual(
            completion_command[3:16],
            (
                "baton-resume",
                REMOTE_GIT_BASELINE,
                REMOTE_COMPLETION_TEMP,
                REMOTE_COMPLETION_MARKER,
                REMOTE_RUNTIME_USER,
                "runuser",
                "--user",
                REMOTE_RUNTIME_USER,
                "--preserve-environment",
                "--",
                "env",
                "HOME=/home/baton-agent",
                f"CODEX_HOME={REMOTE_CODEX_HOME}",
            ),
        )
        self.assertIn('"$@"', completion_command[2])
        self.assertNotIn(REMOTE_GIT_BASELINE, completion_command[2])
        self.assertNotIn(REMOTE_COMPLETION_MARKER, completion_command[2])
        self.assertNotIn(REMOTE_COMPLETION_TEMP, completion_command[2])
        self.assertLess(
            completion_command[2].index("/usr/bin/pkill"),
            completion_command[2].index("printf"),
        )

    def test_handoff_provisions_an_unprivileged_runtime_boundary(self) -> None:
        modal = _FakeModal()

        handoff_archive(
            archive_path=self.archive,
            prompt="continue the task",
            image_name="baton-codex-0-147-0",
            modal_module=modal,
        )

        commands = [command for command, _ in modal.sandbox.commands]
        self.assertIn(("mkdir", "-p", REMOTE_CONTROL_DIR), commands)
        self.assertIn(
            ("chown", "root:root", REMOTE_ROOT, REMOTE_CONTROL_DIR),
            commands,
        )
        self.assertIn(("chmod", "755", REMOTE_ROOT), commands)
        self.assertIn(("chmod", "700", REMOTE_CONTROL_DIR), commands)
        self.assertIn(
            (
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
                "runuser",
                "--user",
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
                "runuser",
                "--user",
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
        modal = _FakeModal(
            git_stdout_by_command={
                (
                    "runuser",
                    "--user",
                    REMOTE_RUNTIME_USER,
                    "--",
                    "git",
                    "-C",
                    REMOTE_WORKSPACE,
                    "rev-parse",
                    "--show-toplevel",
                ): f"{REMOTE_WORKSPACE}\n",
                (
                    "runuser",
                    "--user",
                    REMOTE_RUNTIME_USER,
                    "--",
                    "git",
                    "-C",
                    REMOTE_WORKSPACE,
                    "rev-parse",
                    "HEAD",
                ): f"{expected_git_state['head']}\n",
                (
                    "runuser",
                    "--user",
                    REMOTE_RUNTIME_USER,
                    "--",
                    "git",
                    "-C",
                    REMOTE_WORKSPACE,
                    "branch",
                    "--show-current",
                ): f"{expected_git_state['branch']}\n",
                (
                    "runuser",
                    "--user",
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
                    "runuser",
                    "--user",
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
                    "runuser",
                    "--user",
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
                    "runuser",
                    "--user",
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
            image_name="baton-codex-0-147-0",
            detach=True,
            modal_module=modal,
        )

        serialized_state = json.dumps(expected_git_state, separators=(",", ":"))
        self.assertEqual(
            modal.sandbox.filesystem.text_writes,
            [(serialized_state, REMOTE_GIT_BASELINE)],
        )
        completion_command = next(
            command
            for command, _ in modal.sandbox.commands
            if command[0] == "sh" and "marker_temp=$2" in command[2]
        )
        commands = [command for command, _ in modal.sandbox.commands]
        completion_index = commands.index(completion_command)
        ownership_boundary_index = commands.index(
            (
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
                    "git",
                    "clone",
                    "--no-checkout",
                    "/baton/stage/git/repository.bundle",
                    REMOTE_WORKSPACE,
                )
            ),
            ownership_boundary_index,
        )
        for git_command in modal.sandbox.git_stdout_by_command:
            self.assertGreater(commands.index(git_command), ownership_boundary_index)
            self.assertLess(commands.index(git_command), completion_index)
            self.assertEqual(
                git_command[:7],
                (
                    "runuser",
                    "--user",
                    REMOTE_RUNTIME_USER,
                    "--",
                    "git",
                    "-C",
                    REMOTE_WORKSPACE,
                ),
            )
        self.assertLess(
            commands.index(("chmod", "600", REMOTE_GIT_BASELINE)),
            completion_index,
        )
        self.assertEqual(completion_command[4], REMOTE_GIT_BASELINE)
        self.assertEqual(completion_command[5], REMOTE_COMPLETION_TEMP)
        self.assertEqual(completion_command[6], REMOTE_COMPLETION_MARKER)
        self.assertEqual(completion_command[7], REMOTE_RUNTIME_USER)
        self.assertNotIn(REMOTE_GIT_BASELINE, completion_command[2])
        self.assertNotIn(REMOTE_COMPLETION_TEMP, completion_command[2])
        self.assertNotIn(REMOTE_COMPLETION_MARKER, completion_command[2])
        self.assertNotIn(serialized_state, completion_command[2])
        self.assertIn('baseline_path=$1', completion_command[2])

    def test_detached_non_git_handoff_records_null_git_baseline(self) -> None:
        modal = _FakeModal()

        handoff_archive(
            archive_path=self.archive,
            prompt="continue the task",
            image_name="baton-codex-0-147-0",
            detach=True,
            modal_module=modal,
        )

        self.assertEqual(
            modal.sandbox.filesystem.text_writes,
            [("null", REMOTE_GIT_BASELINE)],
        )
        self.assertFalse(
            any(
                command[:8]
                == (
                    "runuser",
                    "--user",
                    REMOTE_RUNTIME_USER,
                    "--",
                    "git",
                    "-C",
                    REMOTE_WORKSPACE,
                    "rev-parse",
                )
                for command, _ in modal.sandbox.commands
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
        sandbox = _LocalGitSandbox(repository)
        ordinary_index_flags = _capture_remote_git_state(sandbox)["index_flags"]

        _run_git(repository, "update-index", "--assume-unchanged", "assumed.txt")
        _run_git(repository, "update-index", "--skip-worktree", "skipped.txt")

        flagged_index_flags = _capture_remote_git_state(sandbox)["index_flags"]
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

        refs = _capture_remote_git_state(_LocalGitSandbox(repository))["refs"]

        self.assertIn("refs/remotes/origin/main\0", refs)
        self.assertIn("refs/notes/review\0", refs)

    def test_runtime_image_bakes_pinned_codex_and_git_without_a_dockerfile(self) -> None:
        modal = _FakeModal()

        _runtime_image(modal, "0.147.0")

        self.assertEqual(modal.image.registry_tags, ["node:22-bookworm-slim"])
        self.assertIn("git", modal.image.apt_packages)
        self.assertIn("passwd", modal.image.apt_packages)
        self.assertIn("procps", modal.image.apt_packages)
        self.assertIn("util-linux", modal.image.apt_packages)
        self.assertEqual(
            modal.image.commands,
            [
                "useradd --create-home --shell /bin/bash baton-agent",
                "npm install --global @openai/codex@0.147.0",
                "codex --version",
                "git --version",
            ],
        )

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


class _LocalGitSandbox:
    def __init__(self, repository: Path) -> None:
        self.repository = repository

    def exec(self, *command: str, **kwargs: object) -> _FakeProcess:
        translated = list(command)
        if translated[:4] == ["runuser", "--user", REMOTE_RUNTIME_USER, "--"]:
            translated = translated[4:]
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
            "runuser",
            "--user",
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
        self.text_writes: list[tuple[str, str]] = []

    def copy_from_local(self, local_path: Path, remote_path: str) -> None:
        self.copies.append((Path(local_path).resolve(), remote_path))

    def write_text(self, contents: str, remote_path: str) -> None:
        self.text_writes.append((contents, remote_path))


class _FakeSandbox:
    def __init__(
        self,
        *,
        resume_returncode: int,
        resume_stderr: str,
        resume_stdout: list[str] | None = None,
        find_stdout: str = "",
        git_stdout_by_command: dict[tuple[str, ...], str] | None = None,
        terminate_error: Exception | None = None,
        detach_error: Exception | None = None,
    ) -> None:
        self.object_id = "sb-test"
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
        self.detach_error = detach_error
        self.terminated = False
        self.detached = False

    def exec(self, *command: str, **kwargs: object) -> _FakeProcess:
        self.commands.append((command, kwargs))
        if "codex" in command:
            return _FakeProcess(
                stdout=self.resume_stdout,
                stderr=[self.resume_stderr],
                returncode=self.resume_returncode,
            )
        if command[0] == "find":
            return _FakeProcess(stdout=[self.find_stdout])
        if command in self.git_stdout_by_command:
            return _FakeProcess(stdout=[self.git_stdout_by_command[command]])
        return _FakeProcess()

    def terminate(self) -> None:
        self.terminated = True
        if self.terminate_error is not None:
            raise self.terminate_error

    def detach(self) -> None:
        self.detached = True
        if self.detach_error is not None:
            raise self.detach_error


class _FakeImage:
    def __init__(self) -> None:
        self.registry_tags: list[str] = []
        self.apt_packages: tuple[str, ...] = ()
        self.commands: list[str] = []
        self.named_images: list[str] = []

    def from_registry(self, tag: str) -> _FakeImage:
        self.registry_tags.append(tag)
        return self

    def from_name(self, name: str) -> _FakeImage:
        self.named_images.append(name)
        return self

    def apt_install(self, *packages: str) -> _FakeImage:
        self.apt_packages = packages
        return self

    def run_commands(self, *commands: str) -> _FakeImage:
        self.commands = list(commands)
        return self


class _FakeApp:
    def __init__(self, parent: _FakeModal) -> None:
        self.parent = parent

    def lookup(self, name: str, *, create_if_missing: bool):
        self.parent.app_lookup_calls.append((name, create_if_missing))
        return {"name": name}


class _FakeSecret:
    def __init__(self, parent: _FakeModal) -> None:
        self.parent = parent

    def from_name(self, name: str, *, required_keys: list[str]):
        self.parent.secret_calls.append((name, tuple(required_keys)))
        return {"name": name}


class _FakeSandboxFactory:
    def __init__(self, parent: _FakeModal) -> None:
        self.parent = parent

    def create(self, **kwargs: object) -> _FakeSandbox:
        self.parent.sandbox_create_kwargs = kwargs
        return self.parent.sandbox


class _FakeModal:
    def __init__(
        self,
        *,
        resume_returncode: int = 0,
        resume_stderr: str = "",
        resume_stdout: list[str] | None = None,
        find_stdout: str = "",
        git_stdout_by_command: dict[tuple[str, ...], str] | None = None,
        terminate_error: Exception | None = None,
        detach_error: Exception | None = None,
    ) -> None:
        self.app_lookup_calls: list[tuple[str, bool]] = []
        self.secret_calls: list[tuple[str, tuple[str, ...]]] = []
        self.sandbox_create_kwargs: dict[str, object] = {}
        self.image = _FakeImage()
        self.App = _FakeApp(self)
        self.Image = self.image
        self.Secret = _FakeSecret(self)
        self.sandbox = _FakeSandbox(
            resume_returncode=resume_returncode,
            resume_stderr=resume_stderr,
            resume_stdout=resume_stdout,
            find_stdout=find_stdout,
            git_stdout_by_command=git_stdout_by_command,
            terminate_error=terminate_error,
            detach_error=detach_error,
        )
        self.Sandbox = _FakeSandboxFactory(self)
