"""Windows backend: each worker is an agent_host.py process in its own visible
console window. We discover workers via STATE_DIR/win/<name>/status.json and
talk to each host over its localhost control socket."""
from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
from pathlib import Path

from backend import Backend
import common

HOST_SCRIPT = Path(__file__).resolve().parent / "agent_host.py"


def _win_dir() -> Path:
    return common.STATE_DIR / "win"


def _status_path(name: str) -> Path:
    return _win_dir() / name / "status.json"


def _read_status(name: str) -> dict | None:
    p = _status_path(name)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _ask(port: int, line: str, timeout: float = 3.0) -> str | None:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout) as s:
            s.sendall(line.encode() + b"\n")
            chunks = []
            while True:
                b = s.recv(65536)
                if not b:
                    break
                chunks.append(b)
            return b"".join(chunks).decode(errors="replace")
    except OSError:
        return None


class WinBackend(Backend):
    def available(self) -> bool:
        try:
            import winpty  # noqa: F401
            import pyte     # noqa: F401
            return True
        except Exception:
            return False

    def install_hint(self) -> str:
        return ("Windows backend needs pywinpty + pyte. Run: "
                "python -m pip install -r requirements.txt")

    def _port(self, name: str):
        st = _read_status(name)
        return st.get("port") if st else None

    def list_workers(self) -> list[str]:
        d = _win_dir()
        if not d.is_dir():
            return []
        names = [c.name for c in d.iterdir() if (c / "status.json").exists()]
        return [n for n in names if self.worker_exists(n)]

    def session_exists(self) -> bool:
        return bool(self.list_workers())

    def worker_exists(self, name: str) -> bool:
        port = self._port(name)
        if port is None:
            return False
        return _ask(port, "PING") is not None

    def spawn(self, name, project_dir, role_file="", ready_timeout=45.0):
        if not self.available():
            return False, self.install_hint()
        status = _status_path(name)
        status.parent.mkdir(parents=True, exist_ok=True)
        if status.exists():
            status.unlink()
        args = [sys.executable, str(HOST_SCRIPT), name, project_dir, str(status)]
        if role_file:
            args.append(role_file)
        try:
            flags = subprocess.CREATE_NEW_CONSOLE  # type: ignore[attr-defined]
        except AttributeError:
            flags = 0
        subprocess.Popen(args, creationflags=flags, cwd=project_dir)
        deadline = time.time() + ready_timeout
        while time.time() < deadline:
            st = _read_status(name)
            if st and st.get("ready"):
                return True, ""
            time.sleep(0.4)
        return (_read_status(name) is not None), ""

    def capture(self, name: str, lines: int = 200) -> str:
        port = self._port(name)
        if port is None:
            return ""
        return _ask(port, f"CAPTURE {lines}") or ""

    def send_text(self, name: str, text: str) -> None:
        port = self._port(name)
        if port is not None:
            _ask(port, f"SEND {' '.join(text.split())}")

    def kill(self, name: str) -> None:
        st = _read_status(name)
        if st:
            port, pid = st.get("port"), st.get("pid")
            if port:
                _ask(port, "STOP", timeout=2.0)
            time.sleep(0.3)
            if pid:
                subprocess.run(["taskkill", "/PID", str(pid), "/F", "/T"],
                               capture_output=True)
        p = _status_path(name)
        if p.exists():
            try:
                p.unlink()
            except OSError:
                pass

    def kill_all(self) -> None:
        d = _win_dir()
        names = [c.name for c in d.iterdir()] if d.is_dir() else []
        for n in names:
            self.kill(n)

    def attach_hint(self) -> str:
        return ("Each worker runs in its own console window titled "
                "'corch:<name>'. Bring one to the front from the taskbar.")
