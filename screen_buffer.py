"""A small wrapper over pyte that turns a stream of terminal bytes into the
current rendered screen text. Isolated so it can be unit-tested without a real
pseudo-console or claude."""
from __future__ import annotations

import threading

import pyte


class ScreenBuffer:
    def __init__(self, cols: int = 120, rows: int = 50):
        self.cols, self.rows = cols, rows
        self._screen = pyte.Screen(cols, rows)
        self._stream = pyte.ByteStream(self._screen)
        # pyte's Screen/ByteStream are NOT thread-safe. In the host, the pump
        # thread feeds bytes while control-socket CAPTURE handlers read the
        # display on other threads. Concurrent access raises inside pyte (e.g.
        # "generator already executing" / "dictionary changed size during
        # iteration"); when that surfaces on the pump thread it kills the host
        # process, which closes the ConPTY and takes claude down with it. One
        # lock serializing every screen access prevents the race entirely.
        self._lock = threading.Lock()

    def feed(self, data: bytes) -> None:
        if data:
            with self._lock:
                self._stream.feed(data)

    def _lines_nolock(self) -> list[str]:
        """Visible screen lines, right-stripped, trailing blank lines dropped.
        Caller must hold self._lock."""
        rows = [row.rstrip() for row in self._screen.display]
        while rows and not rows[-1]:
            rows.pop()
        return rows

    def lines(self) -> list[str]:
        with self._lock:
            return self._lines_nolock()

    def text(self, lines: int | None = None) -> str:
        with self._lock:
            rows = self._lines_nolock()
        if lines is not None:
            rows = rows[-lines:]
        return "\n".join(rows)

    def contains_any(self, markers) -> bool:
        low = self.text().lower()
        return any(m in low for m in markers)
