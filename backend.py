"""Pluggable terminal backend: tmux on Unix, ConPTY on Windows."""
from __future__ import annotations

import os
import sys
import time

# --- live screen streaming -------------------------------------------------
# Sentinel a stream_screen generator yields to mean "no new screen, just keep
# the connection warm". The SSE layer maps it to an ``: ping`` comment.
HEARTBEAT = object()
# Base (poll-based) fallback cadence. Overridable in tests.
STREAM_POLL_SECS = 0.2
STREAM_HEARTBEAT_SECS = 10.0


class Backend:
    """Interface every backend implements. See win_backend / tmux_backend."""

    def available(self) -> bool:
        raise NotImplementedError

    def install_hint(self) -> str:
        raise NotImplementedError

    def session_exists(self) -> bool:
        raise NotImplementedError

    def list_workers(self) -> list[str]:
        raise NotImplementedError

    def worker_exists(self, name: str) -> bool:
        raise NotImplementedError

    def spawn(self, name: str, project_dir: str, role_file: str = "",
              ready_timeout: float = 45.0) -> tuple[bool, str]:
        raise NotImplementedError

    def capture(self, name: str, lines: int = 200) -> str:
        raise NotImplementedError

    def send_text(self, name: str, text: str) -> None:
        raise NotImplementedError

    def send_keys(self, name: str, data: bytes) -> None:
        """Write ``data`` to the worker's terminal verbatim — no whitespace
        coalescing, no auto Enter. The dashboard's interactive terminal uses
        this to forward single keystrokes (Enter, arrows, Ctrl+C, …) as the
        matching VT bytes. Default no-op so backends that can't support raw
        injection (legacy stubs) don't break callers; real backends override.
        """

    def stream_screen(self, name: str):
        """Yield the agent's rendered screen text whenever it changes, and a
        ``HEARTBEAT`` sentinel when it has been idle. Generator ends when the
        worker stops existing. This default polls ``capture``; backends with a
        push channel (Windows ConPTY host) override it."""
        last = None
        last_emit = time.monotonic()
        while self.worker_exists(name):
            txt = self.capture(name)
            now = time.monotonic()
            if txt != last:
                last = txt
                last_emit = now
                yield txt
            elif now - last_emit >= STREAM_HEARTBEAT_SECS:
                last_emit = now
                yield HEARTBEAT
            time.sleep(STREAM_POLL_SECS)

    def kill(self, name: str) -> None:
        raise NotImplementedError

    def kill_all(self) -> None:
        raise NotImplementedError

    def set_scrollback(self, lines: int) -> None:
        """tmux-only optimization; no-op elsewhere."""

    def attach_hint(self) -> str:
        raise NotImplementedError


def get_backend() -> Backend:
    choice = os.environ.get("ORCH_BACKEND", "").strip().lower()
    if not choice:
        choice = "win" if sys.platform == "win32" else "tmux"
    if choice == "win":
        try:
            import win_backend
        except ImportError as e:
            raise RuntimeError(
                f"Windows backend unavailable ({e}). Run: python -m pip install -r requirements.txt"
            ) from e
        return win_backend.WinBackend()
    if choice == "tmux":
        try:
            import tmux_backend
        except ImportError as e:
            raise RuntimeError(
                f"tmux backend unavailable ({e}). Run: python -m pip install -r requirements.txt"
            ) from e
        return tmux_backend.TmuxBackend()
    raise ValueError(f"unknown ORCH_BACKEND {choice!r} (use 'win' or 'tmux')")
