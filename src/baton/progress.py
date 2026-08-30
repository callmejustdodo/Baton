"""Small terminal progress indicator for long-running Baton commands."""

from __future__ import annotations

import sys
from threading import Event, Thread
from types import TracebackType
from typing import TextIO


class TerminalSpinner:
    """Animate a single-line spinner without touching machine-readable stdout."""

    _FRAMES = ("|", "/", "-", "\\")

    def __init__(
        self,
        message: str,
        *,
        stream: TextIO | None = None,
        interval: float = 0.1,
    ) -> None:
        self._message = message
        self._stream = stream if stream is not None else sys.stderr
        self._interval = interval
        self._stop = Event()
        self._thread: Thread | None = None
        self._line_width = len(message) + 2

    def __enter__(self) -> TerminalSpinner:
        if not self._stream.isatty():
            return self

        self._write_frame(0)
        self._thread = Thread(target=self._animate, daemon=True)
        self._thread.start()
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._thread is None:
            return

        self._stop.set()
        self._thread.join()
        self._stream.write("\r" + (" " * self._line_width) + "\r")
        self._stream.flush()

    def _animate(self) -> None:
        frame = 1
        while not self._stop.wait(self._interval):
            self._write_frame(frame)
            frame = (frame + 1) % len(self._FRAMES)

    def _write_frame(self, frame: int) -> None:
        self._stream.write(f"\r{self._FRAMES[frame]} {self._message}")
        self._stream.flush()
