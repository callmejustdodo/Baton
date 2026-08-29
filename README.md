# Baton

Move an in-progress Codex session from your laptop to a [Modal Sandbox](https://modal.com/docs/guide/sandboxes), then retrieve the remote workspace for review after the agent finishes.

Baton snapshots the selected Codex rollout, your workspace, and Git state; restores them under `/baton` in a Linux x86_64 Sandbox; and resumes Codex non-interactively. It is intentionally a handoff tool, not a live terminal connection or a PR bot.

## Prerequisites

- Python 3.11+
- The Codex CLI installed locally and available as `codex`
- A Modal account
- An OpenAI API key with Codex API access. A ChatGPT OAuth login is not transferred.

## Install and configure

From a clone of this repository:

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install --upgrade pip
python3 -m pip install -e .

codex --version
modal setup
```

Create the Modal Secret Baton expects. This sends the key to Modal; Baton does not copy your local `~/.codex/auth.json`.

```bash
modal secret create baton-openai OPENAI_API_KEY="$OPENAI_API_KEY"
```

`modal secret list` will show the Secret name (`baton-openai`), not the key name or its value.

Build the reusable Sandbox image once. It bakes the matching Codex CLI release and Git into a Debian-based image so a handoff does not spend its cold start installing them.

```bash
baton prepare
```

Run `baton prepare` again after changing the local Codex CLI version. Use `--app-name` or `--image-name` if you use names other than the defaults (`baton` and `baton-codex-<version>`); `--secret-name` is a `baton handoff` option.

## Quick start

Open a terminal in the project you want to hand off, then start a detached handoff:

```bash
baton handoff "Continue the task. When finished, write a short summary to HANDOFF_RESULT.md." --detach
```

Baton presents a keyboard picker containing only Codex sessions whose recorded working directory matches the current project. Sessions are ordered by most recent rollout activity, with the newest first; use ↑/↓ to move and Enter to select. Then you can close the laptop: the Modal Sandbox keeps working. The command prints a `sandbox_id`, the snapshot path, and a local handoff receipt.

Treat this as a checkpoint, not live process migration. Stop or wait for the local Codex turn before handoff, and do not let local and remote agents modify the same session/workspace concurrently. Baton detects changes during capture, but it cannot merge divergent work created afterward.

When the remote work is complete, return to the same project and run:

```bash
baton fetch
```

Choose the detached handoff. Baton downloads a review copy without touching your working tree:

```text
.baton/fetches/<sandbox-id>/
├── baseline/       # workspace as it was at handoff time
├── workspace/      # workspace after remote Codex work
├── changes.patch   # binary-safe baseline → remote diff
└── result.json     # sandbox/session metadata and Codex exit code
```

Inspect `changes.patch` or compare `baseline/` with `workspace/`, then copy or apply the changes yourself. Baton never changes your local worktree during fetch, and it never creates a commit, pushes, or opens a PR.

To apply a reviewed patch manually, first ensure your local workspace has not drifted from the handoff baseline:

```bash
FETCH_ROOT=".baton/fetches/<sandbox-id>"
git apply --check "$FETCH_ROOT/changes.patch"
git apply "$FETCH_ROOT/changes.patch"
```

Add `.baton/` to the target project's `.gitignore`: snapshots contain source and transcripts and should stay local.

## Commands

### `baton handoff`

Interactive form (recommended at a terminal):

```bash
baton handoff "Your follow-up prompt" --detach
```

Explicit form (best for scripts or a known session):

```bash
baton handoff <session-id> "Your follow-up prompt" --detach
```

Use ↑/↓ and Enter in a picker; press `q` or Esc to cancel. Non-interactive input deliberately requires the explicit form, so automation cannot accidentally choose a session.

Omit `--detach` to keep the local command attached. Baton streams Codex JSONL events to stdout and cleans up the Sandbox when the command finishes. Use `--detach` when you want the work to continue after the laptop sleeps; `baton fetch` needs the receipt created by a detached handoff.

Useful options:

```text
--workspace PATH    Project to snapshot (default: current directory)
--codex-home PATH   Source Codex state (default: $CODEX_HOME or ~/.codex)
--timeout SECONDS   Sandbox and Codex timeout (default: 1200)
--secret-name NAME  Modal Secret containing OPENAI_API_KEY
```

### `baton snapshot`

Create a local archive without starting Modal work:

```bash
baton snapshot <session-id>
```

The default archive path is `.baton/snapshots/`. It is useful for inspecting the capture boundary before a handoff.

### `baton fetch`

Interactive form:

```bash
baton fetch
```

Explicit form:

```bash
baton fetch <sandbox-id>
```

The explicit form needs the matching local receipt (or `--receipt PATH`). Fetch checks for the remote completion marker and refuses to download a workspace that Codex may still be modifying; wait for Codex to finish and retry if the marker is absent. A nonzero remote Codex exit code is reported in `result.json`, but any files it changed remain available for review.

## What Baton moves—and what it does not

The snapshot contains the selected Codex rollout, its matching session-index record when available, the portable workspace, and Git provenance/deltas needed to recover local-only commits plus staged and unstaged changes.

- Local Codex OAuth/auth files are excluded. The Sandbox receives `OPENAI_API_KEY` only through the named Modal Secret and uses `CODEX_HOME=/baton/.codex` inside the container.
- Known credential paths such as `.env`, `.ssh`, `.aws`, and private key files are rejected. Still treat every snapshot as sensitive: ordinary source files and the transcript can contain secrets.
- The laptop is commonly macOS arm64 while Modal is Linux x86_64. Baton excludes normal dependency/build directories such as `node_modules`, virtual environments, `build`, and `dist`; it rejects native artifacts or markers that remain (for example `binding.gyp`, `.so`, `.dylib`, and `.node`) rather than rebuilding them remotely.
- The remote session's prose response is streamed to stdout only for an attached handoff. A detached fetch returns the workspace, not a copied Codex transcript. Ask the agent to write a report into the workspace if you need its summary later.

## Troubleshooting

| Message or symptom | What to do |
| --- | --- |
| No local Codex sessions in the picker | Run from the session's project directory, pass `--workspace`, or use `baton handoff <session-id> "prompt"`. |
| No detached handoffs in `baton fetch` | Start the handoff with `--detach` from this same workspace. |
| Missing `OPENAI_API_KEY` Secret | Run `modal secret create baton-openai OPENAI_API_KEY="$OPENAI_API_KEY"`, then retry with the same Modal profile. |
| Native dependency/build-output error | Remove generated/native outputs from the handoff scope; Baton will fail rather than rebuild across macOS arm64 and Linux x86_64. |
| Fetch says the remote handoff has not completed | Wait for Codex to finish, then run `baton fetch` again. |

## Development

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## License

Baton is released under the [MIT License](LICENSE).
