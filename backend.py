"""Pluggable terminal backend: tmux on Unix, ConPTY on Windows."""
from __future__ import annotations

import os
import sys


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
        import win_backend
        return win_backend.WinBackend()
    if choice == "tmux":
        import tmux_backend
        return tmux_backend.TmuxBackend()
    raise ValueError(f"unknown ORCH_BACKEND {choice!r} (use 'win' or 'tmux')")
