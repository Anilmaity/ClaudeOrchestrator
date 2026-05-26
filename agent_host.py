"""Per-worker host process. Runs `claude` under a ConPTY, renders its screen
with pyte, and serves a localhost control protocol so the short-lived `orch`
CLI can capture output, send input, query state, and stop the worker.

Run as:  python agent_host.py <name> <project_dir> <status_path> [role_file]
The visible console window this runs in shows the live claude TUI.
"""
from __future__ import annotations

import json
import os
import shutil as _shutil
import socket
import socketserver
import sys
import threading
import time
from pathlib import Path

from screen_buffer import ScreenBuffer
import common


class _Handler(socketserver.StreamRequestHandler):
    def handle(self):
        host = self.server.host  # type: ignore[attr-defined]
        line = self.rfile.readline().decode(errors="replace").rstrip("\r\n")
        if not line:
            return
        cmd, _, arg = line.partition(" ")
        cmd = cmd.upper()
        if cmd == "PING":
            self.wfile.write(b"OK")
        elif cmd == "STATE":
            self.wfile.write(json.dumps(host.state()).encode())
        elif cmd == "CAPTURE":
            try:
                n = int(arg)
            except ValueError:
                n = 200
            self.wfile.write(host.screen.text(lines=n).encode())
        elif cmd == "SEND":
            host.inject(arg)
            self.wfile.write(b"OK")
        elif cmd == "STOP":
            self.wfile.write(b"OK")
            threading.Thread(target=host.shutdown, daemon=True).start()
        else:
            self.wfile.write(b"ERR unknown")


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class AgentHost:
    def __init__(self, name, child, cols, rows, status_path=None):
        self.name = name
        self.child = child            # object with .write(str) and .isalive()
        self.screen = ScreenBuffer(cols=cols, rows=rows)
        self.status_path = status_path
        self.ready = False
        self.port = 0
        self._server = None
        self._pty = None
        self._started_at = common._now()

    @classmethod
    def for_test(cls, child, cols=80, rows=24):
        return cls(name="test", child=child, cols=cols, rows=rows)

    def mark_ready_if_seen(self):
        if not self.ready and self.screen.contains_any(common.READY_MARKERS):
            self.ready = True

    def state(self):
        return {"ready": self.ready, "alive": bool(self.child.isalive()),
                "started_at": self._started_at, "pid": os.getpid()}

    def inject(self, text):
        # collapse to one submitted line, like tmux send-keys -l + Enter
        one_line = " ".join(text.split())
        self.child.write(one_line + "\r")

    def start_server(self):
        self._server = _Server(("127.0.0.1", 0), _Handler)
        self._server.host = self  # type: ignore[attr-defined]
        self.port = self._server.server_address[1]
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        self._write_status()
        return self.port

    def stop_server(self):
        srv, self._server = self._server, None
        if srv:
            srv.shutdown()
            srv.server_close()

    def _write_status(self):
        if not self.status_path:
            return
        self.status_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.status_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({
            "name": self.name, "pid": os.getpid(), "port": self.port,
            "ready": self.ready, "started_at": self._started_at,
        }))
        tmp.replace(self.status_path)

    def start_pump(self, background=False):
        """Run the pty->screen pump. background=True for tests; foreground in
        main() so the visible console shows the live TUI."""
        if background:
            t = threading.Thread(target=_pump, args=(self,), daemon=True)
            t.start()
            return t
        _pump(self)
        return None

    def shutdown(self):
        # NOTE: never call os._exit here — tests run AgentHost in-process.
        self.stop_server()
        pty = self._pty
        if pty is not None:
            try:
                pty.terminate(force=True)
            except Exception:
                pass
        if self.status_path and self.status_path.exists():
            try:
                self.status_path.unlink()
            except OSError:
                pass


class _PtyChild:
    """Adapts winpty.PtyProcess to the .write(str)/.isalive()/.read() interface."""
    def __init__(self, proc):
        self.proc = proc
    def write(self, text):
        self.proc.write(text)
    def isalive(self):
        try:
            return self.proc.isalive()
        except Exception:
            return False
    def read(self, size=65536):
        try:
            return self.proc.read(size)
        except EOFError:
            return ""


def spawn_conpty(name, cmd, cwd, cols, rows, status_path=None, role_file=""):
    """Start `cmd` under a ConPTY and return an AgentHost wired to it."""
    from winpty import PtyProcess
    env = common.clean_child_env()
    proc = PtyProcess.spawn(cmd, cwd=cwd, dimensions=(rows, cols), env=env)
    host = AgentHost(name=name, child=_PtyChild(proc), cols=cols, rows=rows,
                     status_path=status_path)
    host._pty = proc
    return host


def _pump(host):
    """Read pty output forever: echo to this console + feed the pyte screen +
    auto-accept trust/bypass dialogs + flip `ready` when the footer appears."""
    child = host.child
    accepted_bypass = False
    while child.isalive():
        data = child.read(65536)
        if data:
            try:
                sys.stdout.write(data)
                sys.stdout.flush()
            except Exception:
                pass
            host.screen.feed(data.encode("utf-8", "replace"))
            low = host.screen.text().lower()
            if not accepted_bypass and common.BYPASS_MARKER in low:
                child.write("2")
                time.sleep(0.3)
                child.write("\r")
                accepted_bypass = True
                time.sleep(1.0)
                continue
            if any(m in low for m in common.TRUST_MARKERS):
                child.write("\r")
                time.sleep(0.8)
                continue
            host.mark_ready_if_seen()
            host._write_status()
        else:
            time.sleep(0.05)
    host._write_status()


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    name, project_dir, status_path = argv[0], argv[1], Path(argv[2])
    role_file = argv[3] if len(argv) > 3 else ""
    cols, rows = _shutil.get_terminal_size(fallback=(120, 50))
    role = ""
    if role_file and Path(role_file).exists():
        role = " ".join(Path(role_file).read_text().split())
    cmd = ["claude", "--dangerously-skip-permissions"]
    if role:
        cmd += ["--append-system-prompt", role]
    host = spawn_conpty(name, cmd, project_dir, cols, rows, status_path)
    host.start_server()
    try:
        os.system(f"title corch:{name}")  # window title
    except Exception:
        pass
    host.start_pump(background=False)     # blocks until claude exits / STOP
    host.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
