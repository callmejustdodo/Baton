"""Small terminal pickers for local Codex sessions and Baton handoffs."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO, TypeVar
from uuid import UUID

from .fetch import HandoffReceipt


class PickerError(RuntimeError):
    """Raised when an interactive Baton selection cannot be made."""


class PickerCancelled(PickerError):
    """Raised when the user declines an interactive selection."""


@dataclass(frozen=True)
class SessionChoice:
    """A locally stored Codex session that can safely be snapshotted."""

    session_id: str
    rollout_path: Path
    title: str | None
    cwd: str | None
    updated_at: str | None
    modified_at_ns: int


_Choice = TypeVar("_Choice")


def list_local_sessions(
    *,
    codex_home: Path,
    workspace: Path | None = None,
) -> tuple[SessionChoice, ...]:
    """Return valid locally stored Codex sessions, newest first.

    The session index is optional metadata only. A selectable session must have a
    rollout whose filename and first record agree on its UUID, because that is the
    file Baton will actually archive.
    """

    source_codex_home = codex_home.expanduser().resolve()
    source_workspace = workspace.expanduser().resolve() if workspace is not None else None
    sessions_directory = source_codex_home / "sessions"
    if not sessions_directory.is_dir():
        return ()

    index = _read_session_index(source_codex_home / "session_index.jsonl")
    choices: list[SessionChoice] = []
    for rollout in sessions_directory.rglob("rollout-*.jsonl"):
        try:
            session_id, cwd = _rollout_session_metadata(rollout)
            modified_at_ns = rollout.stat().st_mtime_ns
        except (OSError, PickerError):
            continue
        if source_workspace is not None and not _matches_workspace(cwd, source_workspace):
            continue
        metadata = index.get(session_id, {})
        choices.append(
            SessionChoice(
                session_id=session_id,
                rollout_path=rollout,
                title=_optional_text(metadata.get("thread_name")),
                cwd=cwd,
                updated_at=_optional_text(metadata.get("updated_at")),
                modified_at_ns=modified_at_ns,
            )
        )
    return tuple(
        sorted(
            choices,
            key=lambda choice: (choice.modified_at_ns, choice.session_id),
            reverse=True,
        )
    )


def choose_session(
    sessions: Sequence[SessionChoice],
    *,
    input_stream: TextIO | None = None,
    output_stream: TextIO | None = None,
) -> SessionChoice:
    """Present local Codex sessions and return the selected one."""

    return _choose(
        sessions,
        title="Select a Codex session to hand off:",
        render=_render_session,
        ordering_hint="Most recently active session is first.",
        empty_message=(
            "no local Codex sessions are available; use "
            "'baton handoff <session-id> <prompt>' with an explicit session ID"
        ),
        input_stream=input_stream,
        output_stream=output_stream,
    )


def choose_handoff(
    receipts: Sequence[HandoffReceipt],
    *,
    input_stream: TextIO | None = None,
    output_stream: TextIO | None = None,
) -> HandoffReceipt:
    """Present completed detached-handoff receipts and return the selected one."""

    return _choose(
        receipts,
        title="Select a detached Baton handoff:",
        render=_render_handoff,
        ordering_hint="Most recently created handoff is first.",
        empty_message=(
            "no detached Baton handoffs are available in this workspace; run "
            "'baton handoff <session-id> <prompt> --detach' first"
        ),
        input_stream=input_stream,
        output_stream=output_stream,
    )


def _choose(
    choices: Sequence[_Choice],
    *,
    title: str,
    render: Callable[[_Choice], str],
    ordering_hint: str,
    empty_message: str,
    input_stream: TextIO | None,
    output_stream: TextIO | None,
) -> _Choice:
    if not choices:
        raise PickerError(empty_message)

    interactive_input = input_stream is None
    source = input_stream or sys.stdin
    destination = output_stream or sys.stderr
    if interactive_input and not source.isatty():
        raise PickerError(
            "selection needs an interactive terminal; pass an explicit ID instead"
        )

    print(title, file=destination)
    print(f"{ordering_hint} ↑/↓ move · Enter select · q or Esc cancel", file=destination)
    choice_width = _choice_width(destination) if interactive_input else None
    rendered_choices = tuple(
        _truncate_terminal_line(render(choice), choice_width) for choice in choices
    )
    selection = 0
    _draw_choices(destination, rendered_choices, selection)

    terminal_mode = _raw_terminal(source) if interactive_input else nullcontext()
    try:
        with terminal_mode:
            while True:
                key = _read_picker_key(source, raw_terminal=interactive_input)
                if key == "enter":
                    _finish_picker(destination)
                    return choices[selection]
                if key in {"cancel", "eof"}:
                    _finish_picker(destination)
                    raise PickerCancelled("selection cancelled")
                next_selection = _move_selection(selection, key, len(choices))
                if next_selection != selection:
                    selection = next_selection
                    _draw_choices(
                        destination,
                        rendered_choices,
                        selection,
                        redraw=True,
                    )
    except KeyboardInterrupt as error:
        _finish_picker(destination)
        raise PickerCancelled("selection cancelled") from error


def _draw_choices(
    destination: TextIO,
    choices: Sequence[str],
    selection: int,
    *,
    redraw: bool = False,
) -> None:
    if redraw:
        destination.write(f"\x1b[{len(choices)}A")
    for index, choice in enumerate(choices):
        if redraw:
            destination.write("\r\x1b[2K")
        marker = ">" if index == selection else " "
        destination.write(f"{marker} {choice}\n")
    destination.flush()


def _finish_picker(destination: TextIO) -> None:
    destination.write("\n")
    destination.flush()


def _choice_width(destination: TextIO) -> int:
    """Reserve two columns for the selection marker on an interactive terminal."""

    try:
        columns = os.get_terminal_size(destination.fileno()).columns
    except (AttributeError, OSError):
        columns = 80
    return max(1, columns - 2)


def _truncate_terminal_line(value: str, width: int | None) -> str:
    if width is None or len(value) <= width:
        return value
    if width == 1:
        return "…"
    return f"{value[: width - 1]}…"


def _move_selection(selection: int, key: str, count: int) -> int:
    if key == "up":
        return (selection - 1) % count
    if key == "down":
        return (selection + 1) % count
    return selection


def _read_picker_key(source: TextIO, *, raw_terminal: bool) -> str:
    key = _read_key_byte(source, raw_terminal=raw_terminal)
    if key == "":
        return "eof"
    if key in {"\r", "\n"}:
        return "enter"
    if key in {"q", "Q", "\x03", "\x04"}:
        return "cancel"
    if key != "\x1b":
        return "ignore"
    return _read_escape_sequence(source, raw_terminal=raw_terminal)


def _read_escape_sequence(source: TextIO, *, raw_terminal: bool) -> str:
    if raw_terminal and not _terminal_input_ready(source):
        return "cancel"
    prefix = _read_key_byte(source, raw_terminal=raw_terminal)
    if prefix not in {"[", "O"}:
        return "cancel" if prefix == "" else "ignore"
    if raw_terminal and not _terminal_input_ready(source):
        return "ignore"
    suffix = _read_key_byte(source, raw_terminal=raw_terminal)
    if suffix == "A":
        return "up"
    if suffix == "B":
        return "down"
    return "ignore"


def _read_key_byte(source: TextIO, *, raw_terminal: bool) -> str:
    """Read exactly one input byte, bypassing TextIO buffering in raw mode."""

    if not raw_terminal:
        return source.read(1)
    try:
        value = os.read(source.fileno(), 1)
    except OSError as error:
        raise PickerError("could not read keyboard input") from error
    return value.decode("latin-1")


def _terminal_input_ready(source: TextIO) -> bool:
    try:
        import select

        readable, _, _ = select.select([source.fileno()], [], [], 0.05)
    except (AttributeError, OSError):
        return False
    return bool(readable)


@contextmanager
def _raw_terminal(source: TextIO) -> Iterator[None]:
    """Temporarily read one key at a time and always restore terminal settings."""

    try:
        import termios
        import tty

        file_descriptor = source.fileno()
        original_settings = termios.tcgetattr(file_descriptor)
    except (AttributeError, ImportError, OSError) as error:
        raise PickerError("arrow-key selection needs a POSIX terminal") from error
    try:
        tty.setraw(file_descriptor)
        yield
    finally:
        termios.tcsetattr(file_descriptor, termios.TCSADRAIN, original_settings)


def _read_session_index(path: Path) -> dict[str, dict[str, object]]:
    if not path.is_file():
        return {}

    records: dict[str, dict[str, object]] = {}
    try:
        with path.open("r", encoding="utf-8") as index_file:
            for line in index_file:
                try:
                    record = json.loads(line)
                    if not isinstance(record, dict):
                        continue
                    session_id = _normalize_session_id(record.get("id"))
                except (PickerError, json.JSONDecodeError):
                    continue
                records[session_id] = record
    except OSError:
        return {}
    return records


def _rollout_session_metadata(rollout: Path) -> tuple[str, str | None]:
    filename_session_id = _session_id_from_rollout_name(rollout.name)
    try:
        with rollout.open("r", encoding="utf-8") as rollout_file:
            first_record = json.loads(rollout_file.readline())
        payload = first_record["payload"]
        if not isinstance(payload, dict):
            raise PickerError("rollout metadata payload is invalid")
        record_session_id = _normalize_session_id(payload.get("session_id"))
    except (KeyError, OSError, json.JSONDecodeError, PickerError) as error:
        raise PickerError(f"invalid rollout metadata: {rollout}") from error
    if filename_session_id != record_session_id:
        raise PickerError(f"rollout filename and metadata disagree: {rollout}")
    return record_session_id, _optional_text(payload.get("cwd"))


def _session_id_from_rollout_name(name: str) -> str:
    if not name.startswith("rollout-") or not name.endswith(".jsonl"):
        raise PickerError(f"not a rollout JSONL filename: {name}")
    return _normalize_session_id(name.removesuffix(".jsonl")[-36:])


def _normalize_session_id(value: object) -> str:
    try:
        return str(UUID(str(value).strip()))
    except (AttributeError, ValueError) as error:
        raise PickerError("session ID is not a UUID") from error


def _optional_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _matches_workspace(cwd: str | None, workspace: Path) -> bool:
    if cwd is None:
        return False
    try:
        return Path(cwd).expanduser().resolve() == workspace
    except OSError:
        return False


def _render_session(session: SessionChoice) -> str:
    label = _display_text(session.title or "Untitled Codex session")
    cwd = _display_text(session.cwd or "unknown workspace")
    updated = f" — {_display_text(session.updated_at)}" if session.updated_at else ""
    return f"{label} — {cwd} [{session.session_id}]{updated}"


def _render_handoff(receipt: HandoffReceipt) -> str:
    try:
        modified = datetime.fromtimestamp(receipt.path.stat().st_mtime, tz=UTC).isoformat()
    except OSError:
        modified = "unknown time"
    return (
        f"{receipt.sandbox_id} — session {receipt.session_id} "
        f"(receipt updated {modified})"
    )


def _display_text(value: str, *, limit: int = 120) -> str:
    """Render local metadata without allowing it to control the terminal."""

    rendered = "".join(
        character if character.isprintable() else f"\\x{ord(character):02x}"
        for character in value
    )
    return rendered if len(rendered) <= limit else f"{rendered[: limit - 1]}…"
