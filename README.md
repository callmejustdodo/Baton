# Baton : you can close your macbook now

We need to keep our MacBook open in agent era. **Before Baton.**

> Hand an in-progress Codex session to a [Runloop Devbox](https://docs.runloop.ai/docs/devboxes/overview), close the laptop, then fetch the finished work and conversation.

![A laptop hands off an in-progress agent session to the cloud](assets/baton-handoff.png)

| Before Baton | With Baton |
| --- | --- |
| Keep your MacBook open until Codex finishes. | Hand off the session, close the laptop, and pick up later. |

Baton is a checkpoint-and-handoff tool, not a live terminal connection or a PR bot. It snapshots one selected Codex rollout, workspace, and Git state; restores them in a Linux x86_64 Runloop Devbox; and runs Codex non-interactively with `codex exec resume`.

## How it works

1. **Handoff** — select a local Codex session and upload a verified snapshot to Runloop.
2. **Remote work** — Codex continues in a detached Devbox while the laptop can sleep. When it finishes, Runloop suspends the idle Devbox and preserves its disk state.
3. **Fetch** — download the completed workspace and safely apply its changes when the local baseline still matches.
4. **Resume** — restore the completed remote conversation into the local Codex TUI.

## Built on Runloop

Baton uses [Runloop Devboxes](https://docs.runloop.ai/docs/devboxes/overview) as
its only remote runtime. `baton prepare` builds a reusable Linux x86_64
[Blueprint](https://docs.runloop.ai/docs/devboxes/blueprints/overview) with Git
and the matching Codex CLI already installed, so a handoff starts working instead
of spending its cold start installing tools.

For each handoff, Baton creates a Devbox, uploads the selected snapshot, and
injects the Runloop account secret `BATON_OPENAI_API_KEY` as `OPENAI_API_KEY`.
It never copies local Codex OAuth files. Codex runs as an unprivileged user with
`CODEX_HOME=/baton/.codex`; Baton keeps completion metadata root-only.

When Codex finishes, the Devbox suspends after five minutes of idle time. Its disk
remains available for `baton fetch` and `baton resume`, which wake it again; shut
the Devbox down in the Runloop dashboard after you have collected the work.

## Your first handoff

After the one-time setup below, this is the whole loop. Run it from the project
whose Codex session you want to continue:

```bash
baton handoff "Continue the task and write a concise summary to HANDOFF_RESULT.md." --detach
```

Choose the session with ↑/↓ and Enter, then close your MacBook. When you return
to the same project, collect the work and conversation:

```bash
baton fetch   # safely auto-applies changes when the local baseline still matches
baton resume  # restores the completed Codex conversation locally
```

Use `baton fetch --no-apply` when you want to review the downloaded patch before
changing local files.

## Quick start

### 1. Configure Runloop

You need Python 3.11+, the local Codex CLI, and a Runloop account.

Create a Runloop API key in Runloop Settings and export it locally. This key authorizes Baton to create and inspect Devboxes; it is not the OpenAI key used by Codex.

```bash
export RUNLOOP_API_KEY="..."
```

In the [Runloop Secrets settings](https://platform.runloop.ai/settings), create an account secret named `BATON_OPENAI_API_KEY`. Its value must be an OpenAI API key with Codex API access. Runloop injects that account secret into the Devbox as `OPENAI_API_KEY`; Baton does not transfer local ChatGPT OAuth or `~/.codex/auth.json`.

Install Baton from this repository:

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install --upgrade pip
python3 -m pip install -e .

codex --version
baton prepare
```

`baton prepare` builds a reusable Runloop Blueprint named `baton-codex-<local-version>`. It bakes the matching Codex CLI and Git into the image so a handoff does not spend its cold start installing them. Run it again after changing the local Codex CLI version or updating Baton. Use `--blueprint-name` to choose a different Blueprint name.

### 2. Hand off a session

Open a terminal in the project you want to hand off and start a detached handoff:

```bash
baton handoff "Continue the task. When finished, write a short summary to HANDOFF_RESULT.md." --detach
```

Baton presents only Codex sessions whose recorded working directory matches the current project. Sessions are newest first: use ↑/↓ to move, Enter to select, and `q` or Esc to cancel. The command prints a `devbox_id`, the snapshot path, and a local handoff receipt. You can then close the laptop while the Runloop Devbox keeps working.

The follow-up prompt is required: it becomes the next non-interactive Codex turn. The Devbox suspends after five idle minutes by default; `baton fetch` and `baton resume` wake it automatically. For scripts or a known session, pass the session ID explicitly:

```bash
baton handoff <session-id> "Continue the task and report what changed." --detach
```

> Treat a handoff as a checkpoint, not live process migration. Stop or wait for the local Codex turn before handoff, and do not let local and remote agents modify the same session or workspace concurrently.

### 3. Fetch remote work

When the work is complete, return to the same project and run:

```bash
baton fetch
```

Choose the detached handoff. Baton always writes a review artifact:

```text
.baton/fetches/<devbox-id>/
├── baseline/       # workspace at handoff time
├── workspace/      # workspace after remote Codex work
├── changes.patch   # binary-safe baseline → remote diff
└── result.json     # Devbox/session metadata and Codex exit code
```

By default Baton applies the fetched patch to the current workspace. Before it does, it verifies the portable source files—and, for Git projects, checkout and index—still match the handoff baseline. If they do not, Baton leaves the artifact in place and does not overwrite anything. Baton never creates a commit, pushes, or opens a PR.

Use review-only mode when you do not want local files changed:

```bash
baton fetch --no-apply
```

### 4. Restore the conversation

After a successful fetch that applied its patch, restore the remote conversation locally:

```bash
baton resume
```

Baton downloads only the selected rollout and matching session-index record, verifies that it extends the immutable handoff snapshot, backs up the local record under `.baton/session-backups/`, and starts `codex resume <session-id>` using local `CODEX_HOME`.

It never downloads the Devbox's `auth.json`, global databases, plugins, logs, or other Codex runtime state. Use `baton resume --no-launch` to restore without opening the local TUI.

Add `.baton/` to the target project's `.gitignore`: snapshots contain source and transcripts and should stay local.

## Command reference

| Command | Purpose |
| --- | --- |
| `baton prepare` | Build a Runloop Blueprint with Codex and Git. |
| `baton handoff` | Snapshot a session and continue it in a Runloop Devbox. |
| `baton snapshot` | Create a local archive without starting remote work. |
| `baton fetch` | Download a completed Devbox workspace and safely apply its changes. |
| `baton resume` | Restore a completed remote Codex conversation locally. |

Useful handoff options:

| Option | Meaning |
| --- | --- |
| `--workspace PATH` | Project to snapshot; defaults to the current directory. |
| `--codex-home PATH` | Codex source state; defaults to `$CODEX_HOME` or `~/.codex`. |
| `--blueprint-name NAME` | Runloop Blueprint to create the Devbox from. |
| `--secret-name NAME` | Runloop account secret mapped to `OPENAI_API_KEY` (default: `BATON_OPENAI_API_KEY`). Names must use letters, numbers, and underscores. |
| `--timeout SECONDS` | Codex command timeout; defaults to 1200. |
| `--idle-suspend SECONDS` | Suspend the finished Devbox after this many idle seconds; defaults to 300. |
| `--detach` | Leave the remote Devbox working after the local command exits. |

`baton fetch` and `baton resume` open the same arrow-key receipt picker if no `devbox_id` is supplied. Pass `baton fetch <devbox-id>` or `baton resume <devbox-id>` when scripting; an explicit ID needs the matching local receipt unless `--receipt PATH` is provided.

## Safety and portability

Baton transfers the selected rollout, matching session-index record when available, and portable workspace/Git deltas. It excludes local Codex OAuth/auth files. Runloop injects `OPENAI_API_KEY` only from the named account secret, and Codex runs with `CODEX_HOME=/baton/.codex` inside the Devbox.

Suspended Devboxes retain their disk and can be resumed by Baton for `fetch` or `resume`; they still incur Runloop storage charges. After you have fetched and restored the work, shut down the Devbox from the Runloop dashboard.

Workspaces containing known credential paths—such as `.env`, `.ssh`, `.aws`, and private-key files—are rejected rather than uploaded. Snapshots can still contain secrets in ordinary source files or transcripts, so treat them as sensitive.

Most laptops are macOS arm64 while the remote Devbox is Linux x86_64. Baton excludes common dependency/build directories such as `node_modules`, virtual environments, `build`, and `dist`. It rejects remaining native artifacts or markers—such as `binding.gyp`, `.so`, `.dylib`, and `.node`—rather than rebuilding them remotely.

## Troubleshooting

| Message or symptom | What to do |
| --- | --- |
| `RUNLOOP_API_KEY is required` | Create a Runloop API key, export it in the terminal, and retry. |
| Devbox creation says the secret is missing | In Runloop Settings → Secrets, create `BATON_OPENAI_API_KEY` containing the OpenAI API key, or pass `--secret-name`. |
| No local Codex sessions in the picker | Run from the session project, pass `--workspace`, or use `baton handoff <session-id> "prompt"`. |
| No detached handoffs in `baton fetch` | Start the handoff with `--detach` from this same workspace. |
| Native dependency/build-output error | Remove generated/native outputs from the handoff scope; Baton will not rebuild across macOS arm64 and Linux x86_64. |
| Fetch says the handoff is not complete | Wait for Codex to finish, then retry `baton fetch`. |
| `baton resume` refuses the handoff | Fetch the artifact for review. Baton restores only successful, reproducible remote work and never overwrites a locally advanced transcript. |

## Development

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## License

Baton is released under the [MIT License](LICENSE).
