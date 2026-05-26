"""A small wrapper over pyte that turns a stream of terminal bytes into the
current rendered screen text. Isolated so it can be unit-tested without a real
pseudo-console or claude."""
from __future__ import annotations

import pyte


class ScreenBuffer:
    def __init__(self, cols: int = 120, rows: int = 50):
        self.cols, self.rows = cols, rows
        self._screen = pyte.Screen(cols, rows)
        self._stream = pyte.ByteStream(self._screen)

    def feed(self, data: bytes) -> None:
        if data:
            self._stream.feed(data)

    def lines(self) -> list[str]:
        """Visible screen lines, right-stripped, trailing blank lines dropped."""
        rows = [row.rstrip() for row in self._screen.display]
        while rows and not rows[-1]:
            rows.pop()
        return rows

    def text(self, lines: int | None = None) -> str:
        rows = self.lines()
        if lines is not None:
            rows = rows[-lines:]
        return "\n".join(rows)

    def contains_any(self, markers) -> bool:
        low = self.text().lower()
        return any(m in low for m in markers)
