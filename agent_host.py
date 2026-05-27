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
        # All writes to the ConPTY child funnel through write(); the lock keeps
        # concurrent writers (auto-accept pump, control-socket SEND, console
        # keystroke forwarder) from interleaving multi-byte VT sequences.
        self._write_lock = threading.Lock()
        # _write_status runs from both the server thread and the pump thread;
        # this guards the shared .tmp write/replace.
        self._status_lock = threading.Lock()

    @classmethod
    def for_test(cls, child, cols=80, rows=24):
        return cls(name="test", child=child, cols=cols, rows=rows)

    def write(self, data):
        """Single serialized path to the ConPTY child. Every writer (pump
        auto-accept, control-socket SEND, console forwarder) goes through here
        so their writes can't interleave and corrupt VT escape sequences."""
        with self._write_lock:
            self.child.write(data)

    def mark_ready_if_seen(self):
        # Only mark ready once the input box is actually accepting typing: a
        # READY footer is showing AND no trust/bypass dialog is still up (those
        # would otherwise swallow the kickoff we send right after spawn). The
        # dialog option text ("yes, i accept", "do you trust", ...) disappears
        # from the screen once auto-accept settles, so absence == settled.
        if self.ready:
            return
        if not self.screen.contains_any(common.READY_MARKERS):
            return
        if self._dialog_pending():
            return
        self.ready = True

    def _dialog_pending(self) -> bool:
        """True while a trust/bypass dialog is still awaiting input on screen."""
        markers = (common.BYPASS_MARKER,) + tuple(common.TRUST_MARKERS)
        return self.screen.contains_any(markers)

    def state(self):
        return {"ready": self.ready, "alive": bool(self.child.isalive()),
                "started_at": self._started_at, "pid": os.getpid()}

    def inject(self, text):
        # Type the body, then submit with a SEPARATE Enter after a short gap.
        # A long line followed by a bundled "\r" in one write gets swallowed as
        # part of a paste and never submits; a distinct, slightly-delayed "\r"
        # is seen as a real Enter keystroke. Mirrors tmux send-keys -l + Enter.
        one_line = " ".join(text.split())
        self.write(one_line)
        time.sleep(0.3)
        self.write("\r")

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
        # The ConPTY child (claude) is NOT an OS child of this host process, so
        # its pid must be recorded explicitly for the backend to kill its tree.
        child_pid = getattr(self._pty, "pid", None)
        with self._status_lock:
            self.status_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.status_path.with_suffix(".tmp")
            tmp.write_text(json.dumps({
                "name": self.name, "pid": os.getpid(), "port": self.port,
                "child_pid": child_pid,
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
            # Release the winpty reader thread / socket so the host can exit
            # cleanly even if terminate() raced or already happened.
            if hasattr(pty, "close"):
                try:
                    pty.close()
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


def _log_crash(name: str, where: str) -> None:
    """Append the current exception traceback to the host's crash log, so a host
    that dies is never silent again. Best-effort: never raises."""
    import traceback
    try:
        d = common.STATE_DIR / "win" / name
        d.mkdir(parents=True, exist_ok=True)
        with (d / "crash.log").open("a", encoding="utf-8") as f:
            f.write(f"\n--- {where} {common._now()} ---\n")
            f.write(traceback.format_exc())
    except Exception:
        pass


def _pump(host):
    """Read pty output forever: echo to this console + feed the pyte screen +
    auto-accept trust/bypass dialogs + flip `ready` when the footer appears.

    Every iteration is guarded: a transient screen/IO error must never escape
    and end the loop, because that returns from main() and lets the ConPTY close
    (which kills claude). On error we log and keep pumping."""
    child = host.child
    accepted_bypass = False
    accepted_trust = False
    while child.isalive():
        try:
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
                    host.write("2")
                    time.sleep(0.3)
                    host.write("\r")
                    accepted_bypass = True
                    time.sleep(1.0)
                    continue
                if not accepted_trust and any(m in low for m in common.TRUST_MARKERS):
                    host.write("\r")
                    time.sleep(0.8)
                    accepted_trust = True
                    continue
                host.mark_ready_if_seen()
                host._write_status()
            else:
                time.sleep(0.05)
        except Exception:
            _log_crash(host.name, "pump-iteration")
            time.sleep(0.1)
    host._write_status()


# msvcrt.getwch() returns these prefixes for special keys, followed by a second
# char identifying the key. Map the common ones to the VT/ANSI sequences a
# terminal app (claude's TUI) expects.
_SPECIAL_KEYS = {
    "H": "\x1b[A",   # Up
    "P": "\x1b[B",   # Down
    "M": "\x1b[C",   # Right
    "K": "\x1b[D",   # Left
    "G": "\x1b[H",   # Home
    "O": "\x1b[F",   # End
    "I": "\x1b[5~",  # Page Up
    "Q": "\x1b[6~",  # Page Down
    "R": "\x1b[2~",  # Insert
    "S": "\x1b[3~",  # Delete
}


def _key_to_bytes(ch: str, ch2: str = "") -> str:
    """Translate one msvcrt.getwch() result into what to feed the ConPTY child.

    Special keys arrive as a prefix ('\\x00' or '\\xe0') plus a second char in
    `ch2`; everything else is a literal character. Returns "" for keys we drop.
    """
    if ch in ("\x00", "\xe0"):
        return _SPECIAL_KEYS.get(ch2, "")
    if ch in ("\r", "\n"):
        return "\r"
    if ch == "\x08":          # Backspace -> DEL, what line editors expect
        return "\x7f"
    return ch


def _forward_console_input(host) -> None:
    """Read keystrokes from this window's console and feed them to the child so
    the user can type directly into the agent. Runs in a daemon thread; no-op
    where msvcrt is unavailable (non-Windows / no console)."""
    try:
        import msvcrt
    except ImportError:
        return
    child = host.child
    while child.isalive():
        try:
            ch = msvcrt.getwch()
            ch2 = msvcrt.getwch() if ch in ("\x00", "\xe0") else ""
        except Exception:
            break
        data = _key_to_bytes(ch, ch2)
        if not data:
            continue
        try:
            host.write(data)
        except Exception:
            break


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
    # Forward this console's keystrokes to claude so the window is interactive
    # (the orch control socket still drives it too, for peek/send/dispatch).
    threading.Thread(target=_forward_console_input, args=(host,),
                     daemon=True).start()
    try:
        host.start_pump(background=False)  # blocks until claude exits / STOP
    except Exception:
        _log_crash(name, "pump-fatal")
    finally:
        host.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
