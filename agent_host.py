"""Per-worker host process. Runs `claude` under a ConPTY, renders its screen
with pyte, and serves a localhost control protocol so the short-lived `orch`
CLI can capture output, send input, query state, and stop the worker.

Run as:  python agent_host.py <name> <project_dir> <status_path> [role_file]
The visible console window this runs in shows the live claude TUI.
"""
from __future__ import annotations

import base64
import binascii
import json
import os
import queue
import shutil as _shutil
import socket
import socketserver
import sys
import threading
import time
from pathlib import Path

from screen_buffer import ScreenBuffer
import common
import worker_status

# How often the heartbeat thread refreshes last_beat in the agent's status
# file. The fleet treats anything older than worker_status.HEARTBEAT_FRESH_SECS
# (default 60s) as stale, so 15s gives 3 missed beats of slack.
HEARTBEAT_INTERVAL_SECS = 15

# How long a STREAM connection waits for a screen change before emitting a
# zero-length heartbeat frame (keeps the connection + tunnel alive, and surfaces
# a dead client as a write error so the subscriber gets cleaned up).
STREAM_HEARTBEAT_SECS = 15.0


def _offer(q: queue.Queue, item: str) -> None:
    """Put ``item`` on ``q``; if full, drop the oldest so a slow consumer can
    never block the producer (the pump thread must never stall)."""
    try:
        q.put_nowait(item)
    except queue.Full:
        try:
            q.get_nowait()
        except queue.Empty:
            pass
        try:
            q.put_nowait(item)
        except queue.Full:
            pass


def _write_frame(wfile, payload: bytes) -> None:
    """Write one length-prefixed frame: ``<len>\n`` then the bytes. A zero-length
    payload (``0\n``) is a heartbeat. Writes the length line and payload in two
    calls; the caller MUST ``wfile.flush()`` afterwards on a streaming connection
    or the frame can sit in the buffer and never reach the client."""
    wfile.write(str(len(payload)).encode() + b"\n")
    if payload:
        wfile.write(payload)


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
        elif cmd == "KEYS":
            # Raw-keystroke path: arg is a base64 payload of the bytes to write
            # to the embedded TUI verbatim (no whitespace coalescing, no auto
            # Enter, no inter-write delay). Used by the dashboard's interactive
            # terminal so a user pressing Enter / arrows / Ctrl+C in the
            # browser maps to the matching VT sequence on the agent's PTY.
            try:
                raw = base64.b64decode(arg, validate=True)
            except (binascii.Error, ValueError):
                self.wfile.write(b"ERR bad base64")
                return
            try:
                text = raw.decode("utf-8", errors="replace")
            except UnicodeDecodeError:
                self.wfile.write(b"ERR bad utf-8")
                return
            host.write(text)
            self.wfile.write(b"OK")
        elif cmd == "STOP":
            self.wfile.write(b"OK")
            threading.Thread(target=host.shutdown, daemon=True).start()
        elif cmd == "STREAM":
            # Long-lived push of the rendered screen. Subscribe, send frames as
            # the screen changes, emit a heartbeat on idle, and clean up when the
            # client disconnects (write raises).
            q = host.subscribe()
            try:
                while True:
                    try:
                        txt = q.get(timeout=STREAM_HEARTBEAT_SECS)
                    except queue.Empty:
                        _write_frame(self.wfile, b"")        # heartbeat
                        self.wfile.flush()
                        continue
                    _write_frame(self.wfile, txt.encode("utf-8", "replace"))
                    self.wfile.flush()
            # Covers both _write_frame and flush() in either path: a disconnected
            # client makes the next write/flush raise, which ends the stream.
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                host.unsubscribe(q)
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
        # Reflects the BUSY-marker state of the embedded claude TUI so the
        # heartbeat thread doesn't have to re-scan the screen. Updated by the
        # pump and consumed by _heartbeat_tick. None until first observation.
        self._worker_busy: bool | None = None
        self._heartbeat_stop = threading.Event()
        # Live-screen subscribers (the dashboard's STREAM connections). Each is a
        # bounded queue fed the rendered screen text whenever it changes.
        self._subs: list[queue.Queue] = []
        self._subs_lock = threading.Lock()
        self._last_pub_text: str | None = None

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
        # Ready footer means the TUI is idle and waiting for input — NOT that
        # Claude is mid-turn. Write "done" (the dispatcher's "idle / ready to
        # receive" state); observe_screen_for_status will promote to "running"
        # the moment a busy marker appears on screen.
        self._write_worker_status(state="done")

    def _write_worker_status(self, **fields) -> None:
        """Best-effort merge-write to ``~/.claude-orch/status/<name>.json``.

        Writes never raise: a corrupt status dir or transient disk error must
        not bring down the pump or the host's control socket.
        """
        try:
            worker_status.write_status(self.name, **fields)
        except (OSError, ValueError):
            _log_crash(self.name, "worker-status-write")

    def observe_screen_for_status(self) -> None:
        """Drive the worker-status state machine from the current screen.

        Called from the pump after each chunk of TUI output. The transitions:

          idle -> busy  : state="running"          (claude started a turn)
          busy -> idle  : state="done"             (turn finished; signals task
                                                    completion to the fleet)

        ``starting`` is the initial state until the READY footer is seen; from
        there the host owns the state field until shutdown.
        """
        try:
            low = self.screen.text().lower()
        except Exception:
            return
        busy = any(m in low for m in common.BUSY_MARKERS)
        if self._worker_busy is None:
            self._worker_busy = busy
            return
        if busy and not self._worker_busy:
            self._worker_busy = True
            self._write_worker_status(state="running")
        elif not busy and self._worker_busy:
            self._worker_busy = False
            self._write_worker_status(state="done")

    def subscribe(self) -> queue.Queue:
        """Register a live-screen subscriber and seed it with the current screen
        so a freshly-connected client paints immediately. Returns the queue."""
        q: queue.Queue = queue.Queue(maxsize=8)
        with self._subs_lock:
            self._subs.append(q)
        # Seed outside the lock: a concurrent publish_screen may race this,
        # producing a benign duplicate initial frame, but the subscriber never
        # misses a screen. Keeping it out of the lock also avoids holding it
        # across screen.text() rendering.
        _offer(q, self.screen.text())
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._subs_lock:
            try:
                self._subs.remove(q)
            except ValueError:
                pass

    def publish_screen(self) -> None:
        """Fan the current rendered screen out to subscribers, but only when it
        changed since the last publish. Called from the pump after each chunk."""
        txt = self.screen.text()
        with self._subs_lock:
            if txt == self._last_pub_text:
                return
            self._last_pub_text = txt
            subs = list(self._subs)
        for q in subs:
            _offer(q, txt)

    def _heartbeat_tick(self) -> None:
        """One heartbeat iteration: refresh ``last_beat`` (state preserved)."""
        self._write_worker_status()

    def start_heartbeat(self, interval: float = HEARTBEAT_INTERVAL_SECS) -> None:
        """Spawn a daemon thread that heartbeats every ``interval`` seconds."""
        def _run():
            while not self._heartbeat_stop.wait(interval):
                self._heartbeat_tick()
        threading.Thread(target=_run, daemon=True).start()

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
        self._heartbeat_stop.set()
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
        # Leave one final record of the lifecycle in the worker-status file so
        # an outside reader (./fleet status, dashboard) sees a definitive end
        # state rather than a stale "running".
        self._write_worker_status(state="done")


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
                host.observe_screen_for_status()
                host.publish_screen()
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
    # --headless drops the per-agent visible console: no window title, no
    # msvcrt keystroke forwarder (there's no console to read from), and the
    # _pump's sys.stdout.write echo no-ops naturally because stdout is
    # DEVNULL. The dashboard's interactive terminal replaces what the local
    # console used to provide.
    headless = "--headless" in argv
    argv = [a for a in argv if a != "--headless"]
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
    # Seed the worker-status file before the pump starts so external readers
    # (./fleet status, dashboard) see "starting" immediately instead of a
    # window of "no file -> fall back to legacy heuristic".
    host._write_worker_status(state="starting", pid=os.getpid(),
                              task_id="", progress_note="")
    host.start_heartbeat()
    if not headless:
        try:
            os.system(f"title corch:{name}")  # window title
        except Exception:
            pass
        # Forward this console's keystrokes to claude so the window is
        # interactive (the orch control socket still drives it too, for
        # peek/send/dispatch). Skipped in headless mode: there's no console
        # for msvcrt.getwch() to read from, and the dashboard /api/agent/keys
        # path now covers raw-keystroke injection.
        threading.Thread(target=_forward_console_input, args=(host,),
                         daemon=True).start()
    pump_crashed = False
    try:
        host.start_pump(background=False)  # blocks until claude exits / STOP
    except Exception:
        pump_crashed = True
        _log_crash(name, "pump-fatal")
        try:
            worker_status.write_status(name, state="error")
        except (OSError, ValueError):
            pass
    finally:
        host.shutdown()
        # shutdown() writes state="done"; promote that to "error" if the pump
        # itself crashed, so the fleet can distinguish a clean exit from a
        # process death.
        if pump_crashed:
            try:
                worker_status.write_status(name, state="error")
            except (OSError, ValueError):
                pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
