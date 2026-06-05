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

import backend as _backend
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


def _read_frame(rfile):
    """Read one length-prefixed frame. Returns the payload bytes, ``b""`` for a
    heartbeat, or ``None`` at EOF / on a malformed length line."""
    line = rfile.readline()
    if not line:
        return None
    try:
        n = int(line.strip())
    except ValueError:
        return None
    if n == 0:
        return b""
    buf = bytearray()
    while len(buf) < n:
        chunk = rfile.read(n - len(buf))
        if not chunk:
            break
        buf += chunk
    if len(buf) < n:
        return None        # EOF mid-frame: the stream is broken, end it
    return bytes(buf)


def _iter_frames(rfile):
    """Yield screen strings / HEARTBEAT sentinels from a STREAM connection's
    length-prefixed frames until EOF."""
    while True:
        payload = _read_frame(rfile)
        if payload is None:
            return
        if payload == b"":
            # Look up the sentinel lazily (not a bound import) so identity holds
            # even if `backend` is reloaded mid-process (e.g. in tests).
            yield _backend.HEARTBEAT
        else:
            yield payload.decode("utf-8", "replace")


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
        # Tolerate a transient socket hiccup: one dropped PING under load must
        # not read a live agent as dead, or the dispatcher's keep-alive loop
        # respawns it and duplicate host processes accumulate.
        for _ in range(3):
            if _ask(port, "PING", timeout=1.0) is not None:
                return True
            time.sleep(0.1)
        return False

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
        # Headless by default: the dashboard's interactive Attention tab now
        # owns terminal viewing and keystroke entry, so the per-agent console
        # window is just clutter on a multi-monitor desktop. CREATE_NO_WINDOW
        # detaches from any visible console; DEVNULL handles keep the child's
        # Python stdio from inheriting the parent's pipes (the embedded TUI
        # gets its own ConPTY anyway).
        args.append("--headless")
        try:
            flags = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
        except AttributeError:
            flags = 0
        subprocess.Popen(args, creationflags=flags, cwd=project_dir,
                         stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
        deadline = time.time() + ready_timeout
        while time.time() < deadline:
            st = _read_status(name)
            if st and st.get("ready"):
                return True, ""
            time.sleep(0.4)
        # timed out. Distinguish "host never launched" (hard error) from
        # "launched but not confirmed ready" (warn + proceed, like tmux backend).
        if _read_status(name) is None:
            return False, "worker host failed to start (no status file written)"
        return False, ""

    def capture(self, name: str, lines: int = 200) -> str:
        port = self._port(name)
        if port is None:
            return ""
        # Retry like worker_exists: a single dropped socket read must not return
        # an empty (==idle-looking) capture, which would mark a running task done.
        for _ in range(3):
            out = _ask(port, f"CAPTURE {lines}", timeout=1.5)
            if out:
                return out
            time.sleep(0.1)
        return ""

    def send_text(self, name: str, text: str) -> None:
        port = self._port(name)
        if port is not None:
            _ask(port, f"SEND {' '.join(text.split())}")

    def send_keys(self, name: str, data: bytes) -> None:
        port = self._port(name)
        if port is None:
            return
        # Base64 frames the bytes safely over the line-based control protocol —
        # the agent_host KEYS handler reverses it before writing to the PTY.
        import base64
        _ask(port, "KEYS " + base64.b64encode(data).decode("ascii"))

    def stream_screen(self, name: str):
        """Open the host's STREAM channel and yield screen frames as they arrive.
        Falls through to nothing (generator ends) if the agent is offline."""
        port = self._port(name)
        if port is None:
            return
        try:
            sock = socket.create_connection(("127.0.0.1", port), timeout=3.0)
        except OSError:
            return
        rfile = None
        try:
            sock.sendall(b"STREAM\n")
            # Generous read timeout: the host heartbeats every ~15s, so 40s of
            # total silence means the socket is dead — let recv raise and end.
            sock.settimeout(40.0)
            rfile = sock.makefile("rb")
            yield from _iter_frames(rfile)
        except OSError:
            return
        finally:
            # Close rfile first: makefile() holds a SocketIO reference, so
            # sock.close() alone won't release the fd until rfile is GC'd.
            if rfile is not None:
                try:
                    rfile.close()
                except OSError:
                    pass
            try:
                sock.close()
            except OSError:
                pass

    def kill(self, name: str) -> None:
        st = _read_status(name)
        if not st:
            # No status file: nothing to reap, nothing to clean up.
            return
        port = st.get("port")
        pid = st.get("pid")             # the python host process
        child_pid = st.get("child_pid")  # claude, running inside the ConPTY

        def _taskkill_trees():
            # Kill the ConPTY child tree FIRST (claude is not an OS child of the
            # host, so killing the host alone leaves it alive answering PING),
            # then the host tree.
            for target in (child_pid, pid):
                if target:
                    subprocess.run(
                        ["taskkill", "/PID", str(target), "/F", "/T"],
                        capture_output=True)

        def _still_responding() -> bool:
            return port is not None and _ask(port, "PING", timeout=1.0) is not None

        # Ask the host to stop gracefully, then force-kill both trees.
        if port:
            _ask(port, "STOP", timeout=2.0)
        time.sleep(0.3)
        _taskkill_trees()

        # Verify death: only delete the status file once it no longer answers,
        # otherwise a survivor becomes invisible to `list` but keeps responding.
        if _still_responding():
            time.sleep(0.5)
            _taskkill_trees()
            time.sleep(0.3)

        if _still_responding():
            print(f"warning: worker '{name}' still responding after kill; "
                  f"leaving status file in place", file=sys.stderr)
            return

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
