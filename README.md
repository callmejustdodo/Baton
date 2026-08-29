# Baton : you can close your macbook now

We need to keep our MacBook open in agent era. **Before Baton.**

> Hand an in-progress Codex session to a [Modal Sandbox](https://modal.com/docs/guide/sandboxes), then return for the completed work and conversation.

![A laptop hands off an in-progress agent session to the cloud](assets/baton-handoff.png)

| Before Baton | With Baton |
| --- | --- |
| Keep your MacBook open until Codex finishes. | Hand off the session, close the laptop, and pick up later. |

Baton is a checkpoint-and-handoff tool, not a live terminal connection or a PR bot. It snapshots the selected Codex rollout, workspace, and Git state; restores them under `/baton` in a Linux x86_64 Sandbox; and resumes Codex non-interactively.

## How it works

1. **Handoff** — select a local Codex session and upload a safe snapshot to Modal.
2. **Remote work** — Codex continues in a detached Sandbox while your laptop can sleep.
3. **Fetch** — download a review artifact and, when safe, apply the remote changes.
4. **Resume** — restore the completed remote conversation into your local Codex TUI.

## Quick start

### 1. Install and configure

You need:

- Python 3.11+
- The Codex CLI installed locally and available as `codex`
- A Modal account
- An OpenAI API key with Codex API access. A ChatGPT OAuth login is not transferred.

From a clone of this repository:

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install --upgrade pip
python3 -m pip install -e .

codex --version
modal setup
```

Create the Modal Secret Baton expects. This sends the API key to Modal; Baton does not copy your local `~/.codex/auth.json`.

```bash
modal secret create baton-openai OPENAI_API_KEY="$OPENAI_API_KEY"
```

`modal secret list` will show the Secret name (`baton-openai`), not the key name or its value.

Build the reusable Sandbox image once. It bakes the matching Codex CLI release and Git into a Debian-based image so a handoff does not spend its cold start installing them.

```bash
baton prepare
```

Run `baton prepare` again after changing the local Codex CLI version or updating Baton, so Modal publishes the current runtime image. Use `--app-name` or `--image-name` if you use names other than the defaults (`baton` and `baton-codex-<version>`); `--secret-name` is a `baton handoff` option.

After a runtime-image update, start a new detached handoff: an already-running Sandbox keeps its older control layout.

### 2. Hand off a session

Open a terminal in the project you want to hand off, then start a detached handoff:

```bash
baton handoff "Continue the task. When finished, write a short summary to HANDOFF_RESULT.md." --detach
```

Baton presents only Codex sessions whose recorded working directory matches the current project. Sessions are ordered newest first; use ↑/↓ to move, Enter to select, and `q` or Esc to cancel. The command prints a `sandbox_id`, the snapshot path, and a local handoff receipt. Then you can close the laptop while the Modal Sandbox keeps working.

> **Important:** Treat this as a checkpoint, not live process migration. Stop or wait for the local Codex turn before handoff, and do not let local and remote agents modify the same session or workspace concurrently. Baton detects changes during capture, but it cannot merge divergent work created afterward.

### 3. Fetch the remote work

When the remote work is complete, return to the same project and run:

```bash
baton fetch
```

Choose the detached handoff. Baton always saves a review artifact:

```text
.baton/fetches/<sandbox-id>/
├── baseline/       # workspace as it was at handoff time
├── workspace/      # workspace after remote Codex work
├── changes.patch   # binary-safe baseline → remote diff
└── result.json     # sandbox/session metadata and Codex exit code
```

By default, Baton then applies the fetched patch to your current workspace. It first verifies that the portable source files—and, for Git projects, the checkout and index—still match the handoff baseline; if they do not, it refuses to overwrite anything and leaves this artifact in place for review. Baton never creates a commit, pushes, or opens a PR.

Use `--no-apply` to download the artifact without changing your workspace:

```bash
baton fetch --no-apply
```

If automatic application refuses because you continued working locally, inspect `changes.patch` or compare `baseline/` with `workspace/`; you can resolve the divergence and apply the patch yourself with Git.

### 4. Restore the conversation

After a successful fetch that applied its patch (the default), restore the remote conversation and reopen it in your local Codex TUI:

```bash
baton resume
```

Choose the same detached handoff. Baton then:

- downloads only that session's rollout and matching session-index record;
- verifies that the remote transcript extends the immutable handoff snapshot;
- backs up the local record under `.baton/session-backups/`; and
- starts `codex resume <session-id>` with your local `CODEX_HOME`.

It never downloads the Sandbox's `auth.json`, global databases, plugins, logs, or other Codex runtime state. Run `baton resume --no-launch` when you only want to restore the conversation; then start it later with `codex resume <session-id>`.

Fetch first: the restored conversation can refer to remote edits, while `baton resume` restores the conversation history only. Baton refuses to restore a remote session whose Codex command failed, whose local transcript advanced after handoff, or whose remote Git checkout, index, or refs changed in a way fetch cannot reproduce locally.

Add `.baton/` to the target project's `.gitignore`: snapshots contain source and transcripts and should stay local.

## Command reference

| Command | Purpose |
| --- | --- |
| `baton prepare` | Build and publish the Modal Sandbox image with Codex and Git. |
| `baton handoff` | Snapshot a session and continue it in a Modal Sandbox. |
| `baton snapshot` | Create a local archive without starting Modal work. |
| `baton fetch` | Download a completed Sandbox workspace and safely apply its changes. |
| `baton resume` | Restore a completed remote Codex conversation locally. |

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

| Option | Meaning |
| --- | --- |
| `--workspace PATH` | Project to snapshot; defaults to the current directory. |
| `--codex-home PATH` | Codex state source; defaults to `$CODEX_HOME` or `~/.codex`. |
| `--timeout SECONDS` | Sandbox and Codex timeout; defaults to 1200 seconds. |
| `--secret-name NAME` | Modal Secret containing `OPENAI_API_KEY`. |

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

The explicit form needs the matching local receipt (or `--receipt PATH`). Fetch checks for the remote completion marker, so it will not download a workspace Codex may still be modifying. If the marker is absent, wait for Codex to finish and retry.

Once complete, Baton applies the result only when the current portable workspace and Git state still match the handoff baseline. Pass `--no-apply` for review-only mode. A nonzero remote Codex exit code appears in `result.json`: its files remain available for review, but Baton does not auto-apply a failed run.

### `baton resume`

Restore the completed remote conversation into your local Codex state and open it:

```bash
baton resume
```

Or pass the Sandbox ID explicitly:

```bash
baton resume <sandbox-id>
```

Like `fetch`, the explicit form needs the matching receipt (or `--receipt PATH`), and the picker is available only in an interactive terminal.

`baton resume` requires a successful, completed remote handoff and its successfully applied fetch artifact. It will not overwrite a locally advanced session or reopen one when remote Git commits, refs, or index changes were not reproduced by fetch. It restores only the selected rollout and session-index record; it does not copy remote API-key auth or global Codex databases.

Use `--no-launch` to restore without opening the local TUI, `--codex-home PATH` to restore into a non-default local Codex home, or `--fetch-root PATH` if you gave `baton fetch` a custom output directory.

## Safety and portability

### What Baton transfers

- The selected Codex rollout and its matching session-index record, when available.
- The portable workspace and Git provenance/deltas needed to recover local-only commits plus staged and unstaged changes.

### What Baton excludes

- Local Codex OAuth/auth files. The Sandbox receives `OPENAI_API_KEY` only through the named Modal Secret and uses `CODEX_HOME=/baton/.codex` inside the container.
- Remote global Codex databases, plugins, logs, and other runtime state.

Workspaces containing known credential paths—such as `.env`, `.ssh`, `.aws`, and private-key files—are rejected rather than uploaded.

Snapshots can still contain secrets in ordinary source files or transcripts, so treat them as sensitive.

### Platform limits

Most laptops are macOS arm64 while Modal is Linux x86_64. Baton excludes common dependency and build directories such as `node_modules`, virtual environments, `build`, and `dist`. It rejects remaining native artifacts or markers—such as `binding.gyp`, `.so`, `.dylib`, and `.node`—rather than rebuilding them remotely.

For an attached handoff, the remote session's prose response streams to stdout. For detached work, use `baton fetch` for workspace changes and `baton resume` for the completed conversation.

## Troubleshooting

| Message or symptom | What to do |
| --- | --- |
| No local Codex sessions in the picker | Run from the session's project directory, pass `--workspace`, or use `baton handoff <session-id> "prompt"`. |
| No detached handoffs in `baton fetch` | Start the handoff with `--detach` from this same workspace. |
| Missing `OPENAI_API_KEY` Secret | Run `modal secret create baton-openai OPENAI_API_KEY="$OPENAI_API_KEY"`, then retry with the same Modal profile. |
| Native dependency/build-output error | Remove generated/native outputs from the handoff scope; Baton will fail rather than rebuild across macOS arm64 and Linux x86_64. |
| Fetch says the remote handoff has not completed | Wait for Codex to finish, then run `baton fetch` again. |
| `baton resume` refuses the remote handoff | Fetch the workspace artifact for review. Baton restores sessions only after a successful remote Codex exit, a matching workspace/Git state, and no locally advanced transcript. |

## Development

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## License

Baton is released under the [MIT License](LICENSE).
