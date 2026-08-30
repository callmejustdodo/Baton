from __future__ import annotations

import io
import unittest

from baton.progress import TerminalSpinner


class _TerminalBuffer(io.StringIO):
    def isatty(self) -> bool:
        return True


class TerminalSpinnerTests(unittest.TestCase):
    def test_spinner_writes_and_clears_one_terminal_line(self) -> None:
        output = _TerminalBuffer()

        with TerminalSpinner("Working...", stream=output, interval=60):
            pass

        self.assertEqual(output.getvalue(), "\r| Working...\r            \r")

    def test_spinner_is_silent_when_stderr_is_not_a_terminal(self) -> None:
        output = io.StringIO()

        with TerminalSpinner("Working...", stream=output):
            pass

        self.assertEqual(output.getvalue(), "")
