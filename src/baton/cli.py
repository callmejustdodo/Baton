"""Command-line entry point for Baton."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Sequence

from .handoff import (
    DEFAULT_MODAL_APP,
    DEFAULT_MODAL_SECRET,
    HandoffError,
    handoff_archive,
    image_name_for_version,
    infer_local_codex_version,
    prepare_runtime,
)
from .snapshot import SnapshotError, snapshot


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
        help="build and publish the Modal Sandbox image with Codex and Git",
    )
    prepare_parser.add_argument(
        "--codex-version",
        help="Codex release to bake (default: local codex --version)",
    )
    prepare_parser.add_argument(
        "--app-name",
        default=DEFAULT_MODAL_APP,
        help=f"Modal app to use (default: {DEFAULT_MODAL_APP})",
    )
    prepare_parser.add_argument(
        "--image-name",
        help="published Modal image name (default: baton-codex-<local version>)",
    )

    handoff_parser = subcommands.add_parser(
        "handoff",
        help="snapshot a session and resume it in a Modal Sandbox",
    )
    handoff_parser.add_argument("session_id", help="Codex session UUID to resume")
    handoff_parser.add_argument("follow_up_prompt", help="prompt to resume with")
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
        "--app-name",
        default=DEFAULT_MODAL_APP,
        help=f"Modal app name for the Sandbox (default: {DEFAULT_MODAL_APP})",
    )
    handoff_parser.add_argument(
        "--secret-name",
        default=DEFAULT_MODAL_SECRET,
        help=(
            "Modal Secret containing OPENAI_API_KEY "
            f"(default: {DEFAULT_MODAL_SECRET})"
        ),
    )
    handoff_parser.add_argument(
        "--image-name",
        help="published Modal image name (default: baton-codex-<local version>)",
    )
    handoff_parser.add_argument(
        "--timeout",
        type=int,
        default=20 * 60,
        help="Sandbox and Codex command timeout in seconds (default: 1200)",
    )
    handoff_parser.add_argument(
        "--detach",
        action="store_true",
        help="leave the remote Sandbox working after the local command exits",
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
            codex_version = args.codex_version or infer_local_codex_version()
            result = prepare_runtime(
                codex_version=codex_version,
                app_name=args.app_name,
                image_name=args.image_name,
            )
        except HandoffError as error:
            parser.error(str(error))
        print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
        return 0

    if args.command == "handoff":
        try:
            snapshot_result = snapshot(
                session_id=args.session_id,
                workspace=args.workspace,
                codex_home=args.codex_home,
                output=args.output,
            )
            image_name = args.image_name or image_name_for_version(infer_local_codex_version())
            result = handoff_archive(
                archive_path=snapshot_result.path,
                prompt=args.follow_up_prompt,
                app_name=args.app_name,
                modal_secret=args.secret_name,
                image_name=image_name,
                sandbox_timeout=args.timeout,
                command_timeout=args.timeout,
                detach=args.detach,
                on_event=_print_handoff_event,
            )
        except (SnapshotError, HandoffError) as error:
            parser.error(str(error))
        print(json.dumps({"type": "handoff_complete", **result.to_dict()}, sort_keys=True))
        return 0

    parser.error(f"unsupported command: {args.command}")
    return 2


def _print_handoff_event(event: dict[str, object]) -> None:
    print(json.dumps(event, sort_keys=True), flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
