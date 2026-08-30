"""Command-line entry point for Baton."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from uuid import UUID

from .fetch import (
    FetchError,
    FetchResult,
    fetch_workspace,
    list_handoff_receipts,
    record_fetch_session_restore,
    write_handoff_receipt,
)
from .handoff import (
    DEFAULT_RUNLOOP_BLUEPRINT,
    DEFAULT_RUNLOOP_SECRET,
    HandoffError,
    blueprint_name_for_version,
    handoff_archive,
    infer_local_codex_version,
    prepare_runtime,
)
from .picker import (
    PickerCancelled,
    PickerError,
    choose_handoff,
    choose_session,
    list_local_sessions,
)
from .progress import TerminalSpinner
from .resume import (
    ResumeError,
    preflight_remote_session_restore,
    resume_remote_session,
)
from .runloop import RunloopClientError, validate_account_secret_name
from .snapshot import SnapshotError, snapshot


def _runloop_secret_name(value: str) -> str:
    try:
        return validate_account_secret_name(value)
    except RunloopClientError as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="baton",
        description="Hand an in-progress Codex session to another machine.",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    snapshot_parser = subcommands.add_parser(
        "snapshot",
        help="package one Codex session and the current workspace",
    )
    snapshot_parser.add_argument("session_id", help="Codex session UUID to package")
    snapshot_parser.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="workspace to package (default: current directory)",
    )
    snapshot_parser.add_argument(
        "--codex-home",
        type=Path,
        default=Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")),
        help="source CODEX_HOME (default: $CODEX_HOME or ~/.codex)",
    )
    snapshot_parser.add_argument(
        "--output",
        type=Path,
        help="destination .tar.gz (default: <workspace>/.baton/snapshots/...)",
    )

    prepare_parser = subcommands.add_parser(
        "prepare",
        help="build a Runloop Blueprint with Codex and Git",
    )
    prepare_parser.add_argument(
        "--codex-version",
        help="Codex release to bake (default: local codex --version)",
    )
    prepare_parser.add_argument(
        "--blueprint-name",
        help="Runloop Blueprint name (default: baton-codex-<local version>)",
    )

    handoff_parser = subcommands.add_parser(
        "handoff",
        help="snapshot a session and resume it in a Runloop Devbox",
    )
    handoff_parser.add_argument(
        "session_or_prompt",
        help=(
            "Codex session UUID when followed by a prompt; otherwise the prompt and "
            "Baton opens a session picker"
        ),
    )
    handoff_parser.add_argument(
        "follow_up_prompt",
        nargs="?",
        help="prompt to resume with when a session UUID is supplied",
    )
    handoff_parser.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="workspace to package (default: current directory)",
    )
    handoff_parser.add_argument(
        "--codex-home",
        type=Path,
        default=Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")),
        help="source CODEX_HOME (default: $CODEX_HOME or ~/.codex)",
    )
    handoff_parser.add_argument(
        "--output",
        type=Path,
        help="destination .tar.gz (default: <workspace>/.baton/snapshots/...)",
    )
    handoff_parser.add_argument(
        "--secret-name",
        default=DEFAULT_RUNLOOP_SECRET,
        type=_runloop_secret_name,
        help=(
            "Runloop account secret injected as OPENAI_API_KEY "
            f"(default: {DEFAULT_RUNLOOP_SECRET})"
        ),
    )
    handoff_parser.add_argument(
        "--blueprint-name",
        help=(
            "Runloop Blueprint name (default: baton-codex-<local Codex version>; "
            f"or {DEFAULT_RUNLOOP_BLUEPRINT} when calling the Python API directly)"
        ),
    )
    handoff_parser.add_argument(
        "--timeout",
        type=int,
        default=20 * 60,
        help="Codex command timeout in seconds (default: 1200)",
    )
    handoff_parser.add_argument(
        "--idle-suspend",
        type=int,
        default=5 * 60,
        help="suspend the Devbox after this many idle seconds (default: 300)",
    )
    handoff_parser.add_argument(
        "--detach",
        action="store_true",
        help="leave the remote Devbox working after the local command exits",
    )

    fetch_parser = subcommands.add_parser(
        "fetch",
        help="download and safely apply a completed Runloop Devbox workspace",
    )
    fetch_parser.add_argument(
        "devbox_id",
        nargs="?",
        help="Runloop Devbox ID to fetch (omit to open a handoff picker)",
    )
    fetch_parser.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="workspace containing the detached-handoff receipt (default: current directory)",
    )
    fetch_parser.add_argument(
        "--receipt",
        type=Path,
        help="handoff receipt path (default: <workspace>/.baton/handoffs/<devbox-id>.json)",
    )
    fetch_parser.add_argument(
        "--output",
        type=Path,
        help="fetch artifact directory (default: <workspace>/.baton/fetches/<devbox-id>)",
    )
    fetch_parser.add_argument(
        "--codex-home",
        type=Path,
        default=Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")),
        help=(
            "local CODEX_HOME to restore the fetched session into "
            "(default: $CODEX_HOME or ~/.codex)"
        ),
    )
    fetch_parser.add_argument(
        "--no-apply",
        dest="apply_changes",
        action="store_false",
        default=True,
        help="download the fetched result for review without changing the workspace",
    )

    resume_parser = subcommands.add_parser(
        "resume",
        help="restore a completed remote Codex session and open it locally",
    )
    resume_parser.add_argument(
        "devbox_id",
        nargs="?",
        help="Runloop Devbox ID to restore (omit to open a handoff picker)",
    )
    resume_parser.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="workspace containing the detached-handoff receipt (default: current directory)",
    )
    resume_parser.add_argument(
        "--receipt",
        type=Path,
        help="handoff receipt path (default: <workspace>/.baton/handoffs/<devbox-id>.json)",
    )
    resume_parser.add_argument(
        "--codex-home",
        type=Path,
        default=Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")),
        help="local CODEX_HOME to restore into (default: $CODEX_HOME or ~/.codex)",
    )
    resume_parser.add_argument(
        "--fetch-root",
        type=Path,
        help=(
            "completed fetch artifact directory (default: "
            "<workspace>/.baton/fetches/<devbox-id>)"
        ),
    )
    resume_parser.add_argument(
        "--no-launch",
        action="store_true",
        help="restore the session without opening the local Codex TUI",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "snapshot":
        try:
            result = snapshot(
                session_id=args.session_id,
                workspace=args.workspace,
                codex_home=args.codex_home,
                output=args.output,
            )
        except SnapshotError as error:
            parser.error(str(error))
        print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
        return 0

    if args.command == "prepare":
        try:
            with TerminalSpinner("Preparing Runloop runtime..."):
                codex_version = args.codex_version or infer_local_codex_version()
                result = prepare_runtime(
                    codex_version=codex_version,
                    blueprint_name=args.blueprint_name,
                )
        except HandoffError as error:
            parser.error(str(error))
        print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
        return 0

    if args.command == "handoff":
        try:
            session_id, follow_up_prompt = _resolve_handoff_selection(args)
            with TerminalSpinner("Handing off Codex session..."):
                snapshot_result = snapshot(
                    session_id=session_id,
                    workspace=args.workspace,
                    codex_home=args.codex_home,
                    output=args.output,
                )
                blueprint_name = args.blueprint_name or blueprint_name_for_version(
                    infer_local_codex_version()
                )
                result = handoff_archive(
                    archive_path=snapshot_result.path,
                    prompt=follow_up_prompt,
                    runloop_secret=args.secret_name,
                    blueprint_name=blueprint_name,
                    idle_suspend_seconds=args.idle_suspend,
                    command_timeout=args.timeout,
                    detach=args.detach,
                    on_event=_print_handoff_event,
                )
                receipt_path = None
                if result.detached:
                    receipt_path = write_handoff_receipt(
                        devbox_id=result.devbox_id,
                        session_id=result.session_id,
                        archive_path=snapshot_result.path,
                        workspace=args.workspace,
                    )
        except PickerCancelled:
            print("Baton: selection cancelled", file=sys.stderr)
            return 130
        except (SnapshotError, HandoffError, FetchError, PickerError) as error:
            parser.error(str(error))
        payload = {"type": "handoff_complete", **result.to_dict()}
        if receipt_path is not None:
            payload["receipt"] = str(receipt_path)
        print(json.dumps(payload, sort_keys=True))
        return 0

    if args.command == "fetch":
        try:
            devbox_id, receipt_path = _resolve_fetch_selection(args)

            def preflight_session_history(fetch_result: FetchResult) -> None:
                try:
                    preflight_remote_session_restore(
                        devbox_id=devbox_id,
                        workspace=args.workspace,
                        codex_home=args.codex_home,
                        fetch_root=fetch_result.fetch_root,
                        receipt_path=receipt_path,
                    )
                except ResumeError as error:
                    session_restore = {
                        "status": "failed",
                        "phase": "preflight",
                        "error": str(error),
                    }
                    try:
                        record_fetch_session_restore(
                            fetch_root=fetch_result.fetch_root,
                            session_restore=session_restore,
                        )
                    except FetchError as record_error:
                        raise FetchError(
                            f"{error}; additionally, Baton could not record the session "
                            f"preflight failure: {record_error}"
                        ) from error
                    raise FetchError(str(error)) from error

            with TerminalSpinner("Fetching remote changes..."):
                result = fetch_workspace(
                    devbox_id=devbox_id,
                    workspace=args.workspace,
                    receipt_path=receipt_path,
                    output=args.output,
                    apply_changes=args.apply_changes,
                    before_apply=preflight_session_history if args.apply_changes else None,
                )
                restore_failed = False
                if result.applied:
                    try:
                        restored_session = resume_remote_session(
                            devbox_id=devbox_id,
                            workspace=args.workspace,
                            codex_home=args.codex_home,
                            receipt_path=receipt_path,
                            fetch_root=result.fetch_root,
                            launch=False,
                        )
                    except ResumeError as error:
                        session_restore = {
                            "status": "failed",
                            "phase": "restore",
                            "error": str(error),
                        }
                        restore_failed = True
                    else:
                        session_restore = {
                            "status": "restored",
                            **restored_session.to_dict(),
                        }
                else:
                    session_restore = {
                        "status": "skipped",
                        "reason": (
                            "review_only"
                            if not args.apply_changes
                            else "remote_exit_nonzero"
                        ),
                    }
                try:
                    record_fetch_session_restore(
                        fetch_root=result.fetch_root,
                        session_restore=session_restore,
                    )
                except FetchError as error:
                    session_restore = {
                        **session_restore,
                        "record_error": str(error),
                    }
        except PickerCancelled:
            print("Baton: selection cancelled", file=sys.stderr)
            return 130
        except (FetchError, PickerError) as error:
            parser.error(str(error))
        print(
            json.dumps(
                {
                    "type": "fetch_complete",
                    **result.to_dict(),
                    "session_restore": session_restore,
                },
                sort_keys=True,
            )
        )
        if restore_failed:
            if "record_error" in session_restore:
                print(
                    "Baton: remote workspace changes were applied, but its Codex session "
                    "history was not restored and Baton could not update the fetch artifact. "
                    "The structured output includes both errors; fix the issue and run "
                    "'baton resume' to retry.",
                    file=sys.stderr,
                )
            else:
                print(
                    "Baton: remote workspace changes were applied, but its Codex session "
                    "history was not restored. The saved fetch artifact records the error; "
                    "fix it and run 'baton resume' to retry.",
                    file=sys.stderr,
                )
            return 1
        return 0

    if args.command == "resume":
        try:
            devbox_id, receipt_path = _resolve_fetch_selection(args)
            result = resume_remote_session(
                devbox_id=devbox_id,
                workspace=args.workspace,
                codex_home=args.codex_home,
                receipt_path=receipt_path,
                fetch_root=args.fetch_root,
                launch=not args.no_launch,
            )
        except PickerCancelled:
            print("Baton: selection cancelled", file=sys.stderr)
            return 130
        except (FetchError, ResumeError, PickerError) as error:
            parser.error(str(error))
        print(json.dumps({"type": "session_restored", **result.to_dict()}, sort_keys=True))
        return result.local_exit_code or 0

    parser.error(f"unsupported command: {args.command}")
    return 2


def _print_handoff_event(event: dict[str, object]) -> None:
    print(json.dumps(event, sort_keys=True), flush=True)


def _resolve_handoff_selection(args: argparse.Namespace) -> tuple[str, str]:
    """Interpret one positional handoff argument as picker mode."""

    if args.follow_up_prompt is not None:
        return args.session_or_prompt, args.follow_up_prompt
    if _looks_like_uuid(args.session_or_prompt):
        raise PickerError(
            "a session ID needs a follow-up prompt; use "
            "'baton handoff <session-id> <prompt>'"
        )
    choice = choose_session(
        list_local_sessions(codex_home=args.codex_home, workspace=args.workspace)
    )
    return choice.session_id, args.session_or_prompt


def _resolve_fetch_selection(args: argparse.Namespace) -> tuple[str, Path | None]:
    """Use a receipt picker only when the Devbox ID was omitted."""

    if args.devbox_id is not None:
        return args.devbox_id, args.receipt
    if args.receipt is not None:
        raise FetchError("--receipt requires an explicit Devbox ID")
    receipt = choose_handoff(list_handoff_receipts(workspace=args.workspace))
    return receipt.devbox_id, receipt.path


def _looks_like_uuid(value: str) -> bool:
    try:
        UUID(value)
    except ValueError:
        return False
    return True


if __name__ == "__main__":
    raise SystemExit(main())
