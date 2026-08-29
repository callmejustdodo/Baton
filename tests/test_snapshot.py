from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest

from baton.snapshot import SnapshotError, snapshot


SESSION_ID = "019f5ef4-780a-7973-a1d2-c460461ced1f"
OTHER_SESSION_ID = "019f5ef4-780a-7973-a1d2-c42661d5f05d"
MISSING_SESSION_ID = "00000000-0000-0000-0000-000000000000"


class SnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        (self.workspace / "app.py").write_text("print('hello')\n", encoding="utf-8")
        (self.workspace / "nested").mkdir()
        (self.workspace / "nested" / "note.txt").write_text("state\n", encoding="utf-8")
        (self.workspace / "node_modules").mkdir()
        (self.workspace / "node_modules" / "dependency.js").write_text("ignored", encoding="utf-8")
        (self.workspace / "build").mkdir()
        (self.workspace / "build" / "macos-artifact").write_text("ignored", encoding="utf-8")
        (self.workspace / "src" / "baton.egg-info").mkdir(parents=True)
        (self.workspace / "src" / "baton.egg-info" / "PKG-INFO").write_text(
            "ignored",
            encoding="utf-8",
        )
        (self.workspace / ".omx").mkdir()
        (self.workspace / ".omx" / "runtime-state.json").write_text("ignored", encoding="utf-8")
        (self.workspace / ".omc").mkdir()
        (self.workspace / ".omc" / "runtime-state.json").write_text("ignored", encoding="utf-8")
        (self.workspace / ".env.example").write_text("OPENAI_API_KEY=", encoding="utf-8")

        self.codex_home = self.root / "codex"
        self.rollout = (
            self.codex_home
            / "sessions"
            / "2026"
            / "08"
            / "23"
            / f"rollout-2026-08-23T13-14-26-{SESSION_ID}.jsonl"
        )
        self.rollout.parent.mkdir(parents=True)
        self.rollout.write_text(
            '{"type":"session_meta","payload":{"session_id":"' + SESSION_ID + '"}}\n',
            encoding="utf-8",
        )
        (self.codex_home / "session_index.jsonl").write_text(
            json.dumps({"id": SESSION_ID, "thread_name": "selected"})
            + "\n"
            + json.dumps({"id": OTHER_SESSION_ID, "thread_name": "not selected"})
            + "\n",
            encoding="utf-8",
        )
        (self.codex_home / "auth.json").write_text("never archive credentials", encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_snapshot_contains_selected_session_and_workspace_without_codex_credentials(self) -> None:
        result = snapshot(
            session_id=SESSION_ID,
            workspace=self.workspace,
            codex_home=self.codex_home,
        )

        self.assertTrue(result.path.is_file())
        self.assertEqual(result.session_id, SESSION_ID)
        self.assertEqual(len(result.sha256), 64)

        with tarfile.open(result.path, "r:gz") as archive:
            names = set(archive.getnames())
            manifest = json.load(archive.extractfile("manifest.json"))
            index = archive.extractfile("codex/session_index.jsonl").read().decode("utf-8")

        self.assertIn("workspace/app.py", names)
        self.assertIn("workspace/nested/note.txt", names)
        self.assertNotIn("workspace/node_modules/dependency.js", names)
        self.assertNotIn("workspace/build/macos-artifact", names)
        self.assertNotIn("workspace/src/baton.egg-info/PKG-INFO", names)
        self.assertNotIn("workspace/.omx/runtime-state.json", names)
        self.assertNotIn("workspace/.omc/runtime-state.json", names)
        self.assertIn("workspace/.env.example", names)
        self.assertNotIn("codex/auth.json", names)
        self.assertIn(
            f"codex/sessions/2026/08/23/rollout-2026-08-23T13-14-26-{SESSION_ID}.jsonl",
            names,
        )
        self.assertNotIn(OTHER_SESSION_ID, index)
        self.assertEqual(manifest["session"]["id"], SESSION_ID)
        self.assertFalse(manifest["security"]["codex_oauth_included"])
        self.assertTrue(manifest["security"]["known_workspace_credential_paths_rejected"])
        self.assertNotIn("openai_api_key_included", manifest["security"])
        self.assertEqual(
            manifest["restore_contract"]["environment"]["CODEX_HOME"],
            "/baton/.codex",
        )
        self.assertFalse(manifest["repository"]["present"])

    def test_snapshot_does_not_archive_itself(self) -> None:
        result = snapshot(
            session_id=SESSION_ID,
            workspace=self.workspace,
            codex_home=self.codex_home,
        )

        with tarfile.open(result.path, "r:gz") as archive:
            names = archive.getnames()

        self.assertFalse(any(name.startswith("workspace/.baton/") for name in names))

    def test_snapshot_records_git_provenance_and_deltas(self) -> None:
        self._git("init", "-q")
        self._git("config", "user.email", "baton@example.test")
        self._git("config", "user.name", "Baton test")
        self._git("add", "app.py", "nested/note.txt", ".env.example")
        self._git("commit", "-qm", "initial state")
        (self.workspace / "app.py").write_text("print('changed')\n", encoding="utf-8")
        (self.workspace / "nested" / "note.txt").write_text("staged\n", encoding="utf-8")
        self._git("add", "nested/note.txt")

        result = snapshot(
            session_id=SESSION_ID,
            workspace=self.workspace,
            codex_home=self.codex_home,
        )

        with tarfile.open(result.path, "r:gz") as archive:
            names = set(archive.getnames())
            manifest = json.load(archive.extractfile("manifest.json"))
            bundle = archive.extractfile("git/repository.bundle").read()
            staged_patch = archive.extractfile("git/staged.patch").read()
            unstaged_patch = archive.extractfile("git/unstaged.patch").read()

        self.assertTrue(manifest["repository"]["present"])
        self.assertIsNotNone(manifest["repository"]["head"])
        self.assertEqual(
            manifest["repository"]["bundle_archive_path"],
            "git/repository.bundle",
        )
        self.assertIn("git/repository.bundle", names)
        bundle_path = self.root / "repository.bundle"
        bundle_path.write_bytes(bundle)
        verified_bundle = self._git("bundle", "verify", str(bundle_path))
        self.assertIn("The bundle contains", verified_bundle.stdout)
        self.assertIn("git/status-v1.z", names)
        self.assertIn("git/untracked.z", names)
        self.assertIn(b"nested/note.txt", staged_patch)
        self.assertIn(b"app.py", unstaged_patch)

    def test_git_bundle_round_trip_recovers_clean_staged_and_unstaged_worktrees(self) -> None:
        self._git("init", "-q")
        self._git("config", "user.email", "baton@example.test")
        self._git("config", "user.name", "Baton test")
        (self.workspace / ".gitignore").write_text(
            ".baton/\nnode_modules/\nbuild/\nsrc/*.egg-info/\n.omx/\n.omc/\n",
            encoding="utf-8",
        )
        self._git("add", "app.py", "nested/note.txt", ".env.example", ".gitignore")
        self._git("commit", "-qm", "local-only base")

        for state in ("clean", "staged", "unstaged"):
            with self.subTest(state=state):
                self._git("reset", "--hard", "-q", "HEAD")
                if state == "staged":
                    (self.workspace / "nested" / "note.txt").write_text(
                        "staged change\n",
                        encoding="utf-8",
                    )
                    self._git("add", "nested/note.txt")
                if state == "unstaged":
                    (self.workspace / "app.py").write_text(
                        "print('unstaged change')\n",
                        encoding="utf-8",
                    )

                archive_path = self.root / f"{state}.tar.gz"
                result = snapshot(
                    session_id=SESSION_ID,
                    workspace=self.workspace,
                    codex_home=self.codex_home,
                    output=archive_path,
                )
                restored = self._restore_git_archive(result.path, state)

                self.assertEqual(
                    self._git("rev-parse", "HEAD").stdout,
                    self._git_at(restored, "rev-parse", "HEAD").stdout,
                )
                self.assertEqual(
                    self._git("status", "--porcelain=v1").stdout,
                    self._git_at(restored, "status", "--porcelain=v1").stdout,
                )
                self.assertEqual(
                    (self.workspace / "app.py").read_text(encoding="utf-8"),
                    (restored / "app.py").read_text(encoding="utf-8"),
                )
                self.assertEqual(
                    (self.workspace / "nested" / "note.txt").read_text(encoding="utf-8"),
                    (restored / "nested" / "note.txt").read_text(encoding="utf-8"),
                )

    def test_snapshot_refuses_credential_like_workspace_files(self) -> None:
        (self.workspace / ".env").write_text("OPENAI_API_KEY=not-for-archive", encoding="utf-8")

        with self.assertRaisesRegex(SnapshotError, "credential-like workspace paths"):
            snapshot(
                session_id=SESSION_ID,
                workspace=self.workspace,
                codex_home=self.codex_home,
            )

    def test_snapshot_refuses_symlink_that_escapes_workspace(self) -> None:
        outside = self.root / "outside.txt"
        outside.write_text("not portable", encoding="utf-8")
        (self.workspace / "escape").symlink_to(outside)

        with self.assertRaisesRegex(SnapshotError, "symlink that escapes"):
            snapshot(
                session_id=SESSION_ID,
                workspace=self.workspace,
                codex_home=self.codex_home,
            )

    def test_snapshot_refuses_symlink_targeting_excluded_workspace_content(self) -> None:
        (self.workspace / "deps").symlink_to(
            self.workspace / "node_modules",
            target_is_directory=True,
        )

        with self.assertRaisesRegex(SnapshotError, "target is excluded"):
            snapshot(
                session_id=SESSION_ID,
                workspace=self.workspace,
                codex_home=self.codex_home,
            )

    def test_snapshot_does_not_claim_to_scan_transcript_or_source_contents(self) -> None:
        self.rollout.write_text(
            '{"type":"session_meta","payload":{"session_id":"'
            + SESSION_ID
            + '"}}\n'
            + '{"type":"response_item","payload":{"text":"'
            + 'OPENAI_API_KEY=transcript-content-is-not-scanned'
            + '"}}\n',
            encoding="utf-8",
        )

        result = snapshot(
            session_id=SESSION_ID,
            workspace=self.workspace,
            codex_home=self.codex_home,
        )

        with tarfile.open(result.path, "r:gz") as archive:
            manifest = json.load(archive.extractfile("manifest.json"))

        self.assertEqual(manifest["security"]["archive_classification"], "sensitive")
        self.assertNotIn("openai_api_key_included", manifest["security"])

    def test_invalid_session_id_is_rejected_before_matching_files(self) -> None:
        with self.assertRaisesRegex(SnapshotError, "session_id must be a UUID"):
            snapshot(
                session_id="*",
                workspace=self.workspace,
                codex_home=self.codex_home,
            )

    def test_rollout_payload_must_match_requested_session(self) -> None:
        self.rollout.write_text(
            '{"type":"session_meta","payload":{"session_id":"' + OTHER_SESSION_ID + '"}}\n',
            encoding="utf-8",
        )

        with self.assertRaisesRegex(SnapshotError, "rollout metadata belongs"):
            snapshot(
                session_id=SESSION_ID,
                workspace=self.workspace,
                codex_home=self.codex_home,
            )

    def test_missing_session_is_a_clear_error(self) -> None:
        with self.assertRaisesRegex(SnapshotError, "no rollout found"):
            snapshot(
                session_id=MISSING_SESSION_ID,
                workspace=self.workspace,
                codex_home=self.codex_home,
            )

    def _git(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return self._git_at(self.workspace, *arguments)

    def _git_at(
        self,
        workspace: Path,
        *arguments: str,
    ) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            ["git", "-C", str(workspace), *arguments],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def _restore_git_archive(self, archive_path: Path, state: str) -> Path:
        stage = self.root / f"stage-{state}"
        restored = self.root / f"restored-{state}"
        with tarfile.open(archive_path, "r:gz") as archive:
            archive.extractall(stage)
        manifest = json.loads((stage / "manifest.json").read_text(encoding="utf-8"))
        repository = manifest["repository"]
        self.assertTrue(repository["present"])
        self._git_at(
            stage,
            "clone",
            "--no-checkout",
            str(stage / repository["bundle_archive_path"]),
            str(restored),
        )
        branch = repository["branch"]
        if branch:
            self._git_at(restored, "checkout", "-B", branch, repository["head"])
        else:
            self._git_at(restored, "checkout", "--detach", repository["head"])
        self._git_at(
            restored,
            "apply",
            "--index",
            "--allow-empty",
            str(stage / "git/staged.patch"),
        )
        self._git_at(
            restored,
            "apply",
            "--allow-empty",
            str(stage / "git/unstaged.patch"),
        )
        shutil.copytree(stage / "workspace", restored, dirs_exist_ok=True, symlinks=True)
        return restored
