# Windows (ConPTY) Orchestrator Backend — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `orch.py` and `fleet.py` run natively on Windows by driving a real interactive `claude` TUI per worker through a pseudo-console (ConPTY), behind a pluggable backend seam, while keeping the tmux backend (Linux/macOS) and `fleet.py`/`dashboard.html` working unchanged.

**Architecture:** Extract all terminal operations into a `Backend` interface selected by platform (`backend.py`). `tmux_backend.py` holds today's logic; `win_backend.py` is new. On Windows each worker is a long-lived `agent_host.py` process running in its own visible console window: it runs `claude` under a `pywinpty` ConPTY, renders the screen with `pyte` (so existing text-marker state detection keeps working), and exposes a localhost-socket control protocol (`PING`/`STATE`/`CAPTURE`/`SEND`/`STOP`). Shared constants move to `common.py` to avoid circular imports.

**Tech Stack:** Python 3.14, `pywinpty` (ConPTY), `pyte` (ANSI screen rendering), stdlib `socket`/`socketserver`/`subprocess`, `pytest` for tests.

**Conventions:** TDD throughout. Frequent commits. Every commit message ends with the trailer shown in Task 0 Step 5. Run tests with `python -m pytest`. The full reference design is `docs/superpowers/specs/2026-05-26-windows-orchestrator-backend-design.md`.

---

## File Structure

- Create: `requirements.txt` — `pywinpty`, `pyte`.
- Create: `common.py` — shared constants + tiny helpers (markers, `NAME_RE`, `STATE_DIR`, `SESSION`, `_now`, `_die`, launcher env helper).
- Create: `backend.py` — `Backend` base class + `get_backend()`.
- Create: `tmux_backend.py` — `TmuxBackend`, today's tmux logic moved out of `orch.py`.
- Create: `screen_buffer.py` — `ScreenBuffer`, a unit-testable `pyte` wrapper.
- Create: `agent_host.py` — per-worker host: ConPTY + `ScreenBuffer` + control server + readiness/trust loop.
- Create: `win_backend.py` — `WinBackend`, talks to hosts via status files + sockets.
- Create: `orch.cmd`, `fleet.cmd` — PowerShell/cmd entry points.
- Modify: `orch.py` — delegate terminal primitives to the active backend; keep public CLI + state markers.
- Modify: `fleet.py` — replace direct `orch._tmux(...)` calls with backend methods.
- Modify: `orch`, `fleet` (bash wrappers) — prefer `python` when `python3` is the Windows Store stub.
- Modify: `README.md` — install note.
- Create: `tests/` — `test_common.py`, `test_backend.py`, `test_screen_buffer.py`, `test_win_backend.py`, `test_orch_wiring.py`, `test_host_conpty.py` (Windows+deps only), and a manual integration note.

---

## Task 0: Install dependencies and confirm the pywinpty API

**Files:**
- Create: `requirements.txt`

- [ ] **Step 1: Create `requirements.txt`**

```
# Windows backend dependencies (orchestrator)
pywinpty>=2.0.10
pyte>=0.8.2
# dev
pytest>=8.0
```

- [ ] **Step 2: Install**

Run: `python -m pip install -r requirements.txt`
Expected: all install successfully.

**If `pywinpty` has no wheel for this Python and the build fails:** stop and switch to the ctypes-ConPTY fallback (see "Risks / pywinpty fallback" at the end of this plan). Record the decision in a commit message. Do not proceed to Task 5+ until a ConPTY mechanism installs.

- [ ] **Step 3: Probe the real pywinpty API and capture exact method names**

Run:
```
python -c "from winpty import PtyProcess; import inspect; print([m for m in dir(PtyProcess) if not m.startswith('__')])"
```
Expected: a list including `spawn`, `read`, `write`, `isalive`, and a terminate method (`terminate` or `close`) and a resize method (`setwinsize` or `set_size`).

**Record** the exact names you see. Everywhere this plan writes `proc.read()/write()/isalive()/terminate()/setwinsize()`, substitute the names your probe printed if they differ.

- [ ] **Step 4: Probe pyte byte handling**

Run:
```
python -c "import pyte; s=pyte.Screen(20,3); st=pyte.ByteStream(s); st.feed(b'hello\r\nworld'); print(s.display)"
```
Expected: a 3-element list of 20-char strings; line 0 starts with `hello`, line 1 starts with `world`.

- [ ] **Step 5: Commit**

```bash
git add requirements.txt
git commit -m "$(cat <<'EOF'
chore: add Windows backend dependencies (pywinpty, pyte)

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 1: `common.py` — shared constants and helpers

**Files:**
- Create: `common.py`
- Test: `tests/test_common.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_common.py
import re
import common

def test_name_re_accepts_valid_names():
    assert common.NAME_RE.match("web-1")
    assert common.NAME_RE.match("Agent_2.test")

def test_name_re_rejects_invalid_names():
    assert not common.NAME_RE.match("-leading")
    assert not common.NAME_RE.match("has space")
    assert not common.NAME_RE.match("bad/slash")

def test_now_is_utc_iso_z():
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", common._now())

def test_markers_present():
    assert common.DONE_MARKER == "worker-done"
    assert any("interrupt" in m for m in common.BUSY_MARKERS)
    assert any("shortcuts" in m or "shift+tab" in m for m in common.READY_MARKERS)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_common.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'common'`.

- [ ] **Step 3: Create `common.py`**

```python
"""Shared constants and tiny helpers for the orchestrator and its backends.

Extracted from orch.py so backends can import them without importing orch
(which would create an import cycle: orch -> backend -> tmux/win backend).
"""
from __future__ import annotations

import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

SESSION = "corch"
STATE_DIR = Path.home() / ".claude-orch"
TASKS_DIR = STATE_DIR / "tasks"
REGISTRY = STATE_DIR / "workers.json"

# Footer text Claude's TUI shows once the input box is ready for typing.
READY_MARKERS = ("shift+tab to cycle", "for shortcuts")
# Text shown while Claude is actively working on a turn.
BUSY_MARKERS = ("esc to interrupt", "interrupt)")
# Folder-trust dialog we auto-accept (default option = trust).
TRUST_MARKERS = (
    "do you trust", "trust the files", "trust this folder",
    "is this a project you", "quick safety check",
)
# One-time "Bypass Permissions mode" acceptance; option 2 is "Yes, I accept".
BYPASS_MARKER = "yes, i accept"
# Marker we instruct workers to print when finished.
DONE_MARKER = "worker-done"

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _die(msg: str, code: int = 1):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(code)


def clean_child_env() -> dict:
    """Environment for launching claude: claude refuses to start inside a venv,
    so strip VIRTUAL_ENV and its bin/Scripts dir from PATH."""
    env = dict(os.environ)
    venv = env.pop("VIRTUAL_ENV", None)
    if venv:
        parts = env.get("PATH", "").split(os.pathsep)
        drop = {str(Path(venv) / "bin"), str(Path(venv) / "Scripts")}
        env["PATH"] = os.pathsep.join(p for p in parts if p not in drop)
    return env
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_common.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add common.py tests/test_common.py
git commit -m "$(cat <<'EOF'
refactor: extract shared constants/helpers into common.py

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: `backend.py` — Backend interface and selection

**Files:**
- Create: `backend.py`
- Test: `tests/test_backend.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_backend.py
import importlib
import backend

def test_env_override_selects_named_backend(monkeypatch):
    monkeypatch.setenv("ORCH_BACKEND", "tmux")
    importlib.reload(backend)
    b = backend.get_backend()
    assert type(b).__name__ == "TmuxBackend"

def test_default_is_win_on_win32(monkeypatch):
    monkeypatch.delenv("ORCH_BACKEND", raising=False)
    monkeypatch.setattr(backend.sys, "platform", "win32")
    b = backend.get_backend()
    assert type(b).__name__ == "WinBackend"

def test_default_is_tmux_off_win32(monkeypatch):
    monkeypatch.delenv("ORCH_BACKEND", raising=False)
    monkeypatch.setattr(backend.sys, "platform", "linux")
    b = backend.get_backend()
    assert type(b).__name__ == "TmuxBackend"

def test_unknown_backend_raises(monkeypatch):
    monkeypatch.setenv("ORCH_BACKEND", "nope")
    with __import__("pytest").raises(ValueError):
        backend.get_backend()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_backend.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'backend'`.

- [ ] **Step 3: Create `backend.py`**

> Note: `get_backend()` imports the backend module lazily so importing `backend.py` never requires `pywinpty`/`pyte` (those import lazily inside `WinBackend` methods).

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_backend.py -v`
Expected: PASS (4 passed). (Imports `tmux_backend`/`win_backend` lazily; those are created in Tasks 3 and 6. If running this task in isolation before those exist, the two selection tests that instantiate will fail import — that is expected until Tasks 3 and 6 land. Re-run after Task 6.)

> To keep this task green on its own, the selection tests only check `type(...).__name__`, which still requires the module to import. If you are executing strictly task-by-task, mark Step 4 complete when the module imports and `test_unknown_backend_raises` passes; the other three turn green once Tasks 3 and 6 create the backend modules.

- [ ] **Step 5: Commit**

```bash
git add backend.py tests/test_backend.py
git commit -m "$(cat <<'EOF'
feat: add Backend interface and platform-based selection

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: `tmux_backend.py` — move today's tmux logic

**Files:**
- Create: `tmux_backend.py`
- Test: `tests/test_backend.py` (extend)

This is a near-verbatim move of the tmux helpers currently in `orch.py` (`_tmux`, `_session_exists`, `_windows`, `_window_exists`, `_capture`, `_send_text`, `_wait_ready`, `_spawn_window`, launcher constants) into methods of `TmuxBackend`, importing shared constants from `common`.

- [ ] **Step 1: Add a test for availability detection**

```python
# append to tests/test_backend.py
def test_tmux_available_uses_which(monkeypatch):
    import tmux_backend
    monkeypatch.setattr(tmux_backend.shutil, "which", lambda n: None)
    assert tmux_backend.TmuxBackend().available() is False
    monkeypatch.setattr(tmux_backend.shutil, "which", lambda n: "/usr/bin/tmux")
    assert tmux_backend.TmuxBackend().available() is True
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_backend.py::test_tmux_available_uses_which -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'tmux_backend'`.

- [ ] **Step 3: Create `tmux_backend.py`**

```python
"""tmux-backed terminal operations (Linux/macOS). Moved out of orch.py."""
from __future__ import annotations

import shlex
import shutil
import subprocess
import time
from pathlib import Path

from backend import Backend
import common
from common import (SESSION, STATE_DIR, READY_MARKERS, TRUST_MARKERS,
                    BYPASS_MARKER)

LAUNCHER = STATE_DIR / "agent-launch.sh"
_LAUNCHER_BODY = (
    "#!/usr/bin/env bash\n"
    '[ -n "$VIRTUAL_ENV" ] && export PATH="${PATH//$VIRTUAL_ENV\\/bin:/}"\n'
    "unset VIRTUAL_ENV\n"
    'role="$(cat "$1" 2>/dev/null)"\n'
    'if [ -n "$role" ]; then\n'
    '  exec claude --dangerously-skip-permissions --append-system-prompt "$role"\n'
    "else\n"
    "  exec claude --dangerously-skip-permissions\n"
    "fi\n"
)


class TmuxBackend(Backend):
    def available(self) -> bool:
        return shutil.which("tmux") is not None

    def install_hint(self) -> str:
        return "tmux is not installed. Run: sudo apt install -y tmux"

    def _tmux(self, *args, check=False):
        try:
            return subprocess.run(["tmux", *args], capture_output=True,
                                  text=True, check=check)
        except FileNotFoundError:
            return subprocess.CompletedProcess(args, 127, "", "tmux: not found")

    def _target(self, name: str) -> str:
        return f"{SESSION}:{name}"

    def session_exists(self) -> bool:
        return self._tmux("has-session", "-t", SESSION).returncode == 0

    def list_workers(self) -> list[str]:
        if not self.session_exists():
            return []
        r = self._tmux("list-windows", "-t", SESSION, "-F", "#{window_name}")
        if r.returncode != 0:
            return []
        return [ln for ln in r.stdout.splitlines() if ln]

    def worker_exists(self, name: str) -> bool:
        return name in self.list_workers()

    def capture(self, name: str, lines: int = 200) -> str:
        r = self._tmux("capture-pane", "-p", "-t", self._target(name),
                       "-S", f"-{lines}")
        return r.stdout if r.returncode == 0 else ""

    def send_text(self, name: str, text: str) -> None:
        one_line = " ".join(text.split())
        self._tmux("send-keys", "-t", self._target(name), "-l", "--", one_line)
        time.sleep(0.25)
        self._tmux("send-keys", "-t", self._target(name), "Enter")

    def _ensure_launcher(self) -> None:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        if not LAUNCHER.exists() or LAUNCHER.read_text() != _LAUNCHER_BODY:
            LAUNCHER.write_text(_LAUNCHER_BODY)
        LAUNCHER.chmod(0o755)

    def _wait_ready(self, name: str, timeout: float = 40.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            low = self.capture(name, 60).lower()
            if BYPASS_MARKER in low:
                self._tmux("send-keys", "-t", self._target(name), "2")
                time.sleep(0.4)
                self._tmux("send-keys", "-t", self._target(name), "Enter")
                time.sleep(1.5)
                continue
            if any(m in low for m in TRUST_MARKERS):
                self._tmux("send-keys", "-t", self._target(name), "Enter")
                time.sleep(1.0)
                continue
            if any(m in low for m in READY_MARKERS):
                return True
            time.sleep(0.5)
        return False

    def spawn(self, name, project_dir, role_file="", ready_timeout=45.0):
        self._ensure_launcher()
        cmd = f"bash {shlex.quote(str(LAUNCHER))} {shlex.quote(role_file)}"
        if not self.session_exists():
            r = self._tmux("new-session", "-d", "-s", SESSION, "-n", name,
                           "-c", project_dir, cmd)
        else:
            r = self._tmux("new-window", "-t", SESSION, "-n", name,
                           "-c", project_dir, cmd)
        if r.returncode != 0:
            return False, r.stderr.strip()
        return self._wait_ready(name, ready_timeout), ""

    def kill(self, name: str) -> None:
        self._tmux("kill-window", "-t", self._target(name))

    def kill_all(self) -> None:
        if self.session_exists():
            self._tmux("kill-session", "-t", SESSION)

    def set_scrollback(self, lines: int) -> None:
        self._tmux("set-option", "-g", "history-limit", str(lines))

    def attach_hint(self) -> str:
        return (f"tmux attach -t {SESSION}\n"
                "  Ctrl-b w -> switch windows | Ctrl-b d -> detach")
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/test_backend.py -v`
Expected: `test_tmux_available_uses_which`, `test_env_override_selects_named_backend`, `test_default_is_tmux_off_win32`, `test_unknown_backend_raises` PASS. (`test_default_is_win_on_win32` still needs Task 6.)

- [ ] **Step 5: Commit**

```bash
git add tmux_backend.py tests/test_backend.py
git commit -m "$(cat <<'EOF'
refactor: move tmux terminal logic into TmuxBackend

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: `screen_buffer.py` — pyte wrapper (unit-testable)

**Files:**
- Create: `screen_buffer.py`
- Test: `tests/test_screen_buffer.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_screen_buffer.py
from screen_buffer import ScreenBuffer

def test_renders_plain_text():
    sb = ScreenBuffer(cols=20, rows=4)
    sb.feed(b"hello\r\nworld\r\n")
    text = sb.text()
    assert "hello" in text
    assert "world" in text

def test_text_lines_limit():
    sb = ScreenBuffer(cols=20, rows=6)
    sb.feed(b"a\r\nb\r\nc\r\nd\r\n")
    # last 2 non-empty-ish lines requested
    assert sb.text(lines=2).count("\n") <= 1

def test_overwrite_via_carriage_return():
    sb = ScreenBuffer(cols=20, rows=3)
    sb.feed(b"AAAAA\rBB")          # CR returns cursor; BB overwrites AA
    assert sb.text().splitlines()[0].startswith("BBAAA")

def test_detects_marker_substring():
    sb = ScreenBuffer(cols=40, rows=3)
    sb.feed(b"\r\n? for shortcuts\r\n")
    assert sb.contains_any(("for shortcuts",))
    assert not sb.contains_any(("nonexistent",))
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_screen_buffer.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'screen_buffer'`.

- [ ] **Step 3: Create `screen_buffer.py`**

```python
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
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/test_screen_buffer.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add screen_buffer.py tests/test_screen_buffer.py
git commit -m "$(cat <<'EOF'
feat: add pyte-backed ScreenBuffer for rendering terminal output

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: `agent_host.py` — per-worker host (control protocol first, ConPTY second)

Split into two commits: the control server/protocol (testable with a fake child), then the real ConPTY wiring.

**Files:**
- Create: `agent_host.py`
- Test: `tests/test_host_conpty.py` (Windows + deps only)

### 5a — control protocol against an in-memory fake

- [ ] **Step 1: Write the failing test**

```python
# tests/test_host_conpty.py
import json
import socket
import threading
import time
import pytest

import agent_host


class FakeChild:
    """Stand-in for the ConPTY child: records writes, feeds canned output."""
    def __init__(self):
        self.written = []
        self.alive = True
    def write(self, text): self.written.append(text)
    def isalive(self): return self.alive


def _send(port, line):
    with socket.create_connection(("127.0.0.1", port), timeout=2) as s:
        s.sendall(line.encode() + b"\n")
        return s.recv(65536).decode()


def test_control_protocol_roundtrip():
    child = FakeChild()
    host = agent_host.AgentHost.for_test(child=child, cols=40, rows=6)
    host.screen.feed(b"booting\r\n? for shortcuts\r\n")
    host.mark_ready_if_seen()
    port = host.start_server()
    try:
        assert _send(port, "PING").strip() == "OK"
        st = json.loads(_send(port, "STATE"))
        assert st["ready"] is True
        assert "for shortcuts" in _send(port, "CAPTURE 50")
        assert _send(port, "SEND do the thing").strip() == "OK"
        assert child.written and child.written[-1].endswith("\r")
        assert "do the thing" in child.written[-1]
    finally:
        host.stop_server()
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_host_conpty.py::test_control_protocol_roundtrip -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'agent_host'`.

- [ ] **Step 3: Create `agent_host.py` (control layer + test seam)**

```python
"""Per-worker host process. Runs `claude` under a ConPTY, renders its screen
with pyte, and serves a localhost control protocol so the short-lived `orch`
CLI can capture output, send input, query state, and stop the worker.

Run as:  python agent_host.py <name> <project_dir> <status_path> [role_file]
The visible console window this runs in shows the live claude TUI.
"""
from __future__ import annotations

import json
import os
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
        host: "AgentHost" = self.server.host  # type: ignore[attr-defined]
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
    def __init__(self, name: str, child, cols: int, rows: int,
                 status_path: Path | None = None):
        self.name = name
        self.child = child            # object with .write(str) and .isalive()
        self.screen = ScreenBuffer(cols=cols, rows=rows)
        self.status_path = status_path
        self.ready = False
        self.port = 0
        self._server = None
        self._started_at = common._now()

    # --- test seam: build a host with a fake child, no ConPTY ---
    @classmethod
    def for_test(cls, child, cols=80, rows=24):
        return cls(name="test", child=child, cols=cols, rows=rows)

    def mark_ready_if_seen(self) -> None:
        if not self.ready and self.screen.contains_any(common.READY_MARKERS):
            self.ready = True

    def state(self) -> dict:
        return {"ready": self.ready, "alive": bool(self.child.isalive()),
                "started_at": self._started_at, "pid": os.getpid()}

    def inject(self, text: str) -> None:
        # collapse to one submitted line, like tmux send-keys -l + Enter
        one_line = " ".join(text.split())
        self.child.write(one_line + "\r")

    def start_server(self) -> int:
        self._server = _Server(("127.0.0.1", 0), _Handler)
        self._server.host = self  # type: ignore[attr-defined]
        self.port = self._server.server_address[1]
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        self._write_status()
        return self.port

    def stop_server(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    def _write_status(self) -> None:
        if not self.status_path:
            return
        self.status_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.status_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({
            "name": self.name, "pid": os.getpid(), "port": self.port,
            "ready": self.ready, "started_at": self._started_at,
        }))
        tmp.replace(self.status_path)

    def shutdown(self) -> None:
        self.stop_server()
        # ConPTY child termination handled by run() in the real path.
        os._exit(0)
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/test_host_conpty.py::test_control_protocol_roundtrip -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agent_host.py tests/test_host_conpty.py
git commit -m "$(cat <<'EOF'
feat: agent_host control protocol (PING/STATE/CAPTURE/SEND/STOP)

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

### 5b — ConPTY wiring + readiness/trust loop + `main()`

- [ ] **Step 1: Write the failing test (Windows + deps only)**

```python
# append to tests/test_host_conpty.py
import shutil

winpty_missing = False
try:
    import winpty  # noqa: F401
except Exception:
    winpty_missing = True

@pytest.mark.skipif(sys.platform != "win32" or winpty_missing,
                    reason="needs Windows + pywinpty")
def test_conpty_runs_child_and_captures():
    # Spawn a real child under ConPTY that prints a known marker, then assert
    # the host's screen captures it. Uses cmd echo, not claude.
    host = agent_host.spawn_conpty(
        name="t", cmd=['cmd', '/c', 'echo HELLO_CONPTY && timeout /t 2'],
        cwd=str(Path.cwd()), cols=80, rows=24)
    port = host.start_server()
    try:
        deadline = time.time() + 8
        seen = ""
        while time.time() < deadline:
            seen = _send(port, "CAPTURE 50")
            if "HELLO_CONPTY" in seen:
                break
            time.sleep(0.2)
        assert "HELLO_CONPTY" in seen
    finally:
        _send(port, "STOP")
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_host_conpty.py::test_conpty_runs_child_and_captures -v`
Expected: FAIL with `AttributeError: module 'agent_host' has no attribute 'spawn_conpty'`.

- [ ] **Step 3: Add ConPTY wiring to `agent_host.py`**

> Substitute the pywinpty method names recorded in Task 0 Step 3 if they differ from `read`/`write`/`isalive`/`terminate`/`setwinsize`.

```python
# add near the top of agent_host.py
import shutil as _shutil


class _PtyChild:
    """Adapts winpty.PtyProcess to the .write(str)/.isalive() interface and
    exposes .read()."""
    def __init__(self, proc):
        self.proc = proc
    def write(self, text: str) -> None:
        self.proc.write(text)
    def isalive(self) -> bool:
        try:
            return self.proc.isalive()
        except Exception:
            return False
    def read(self, size: int = 65536) -> str:
        try:
            return self.proc.read(size)  # returns str
        except EOFError:
            return ""
    def terminate(self) -> None:
        try:
            self.proc.terminate(force=True)
        except Exception:
            pass


def spawn_conpty(name, cmd, cwd, cols, rows, status_path=None, role_file=""):
    """Start `cmd` under a ConPTY and return an AgentHost wired to it."""
    from winpty import PtyProcess
    env = common.clean_child_env()
    proc = PtyProcess.spawn(cmd, cwd=cwd, dimensions=(rows, cols), env=env)
    host = AgentHost(name=name, child=_PtyChild(proc), cols=cols, rows=rows,
                     status_path=status_path)
    host._pty = proc
    return host


def _pump(host: "AgentHost") -> None:
    """Read pty output forever: echo to this console + feed the pyte screen +
    auto-accept trust/bypass dialogs + flip `ready` when the footer appears."""
    child = host.child
    accepted_bypass = False
    while child.isalive():
        data = child.read(65536)
        if data:
            sys.stdout.write(data)        # live TUI in this visible window
            sys.stdout.flush()
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


def main(argv=None) -> int:
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
    _pump(host)        # blocks until claude exits
    host.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

Also update `AgentHost.shutdown` to terminate the pty when present:

```python
    def shutdown(self) -> None:
        self.stop_server()
        pty = getattr(self, "_pty", None)
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
        os._exit(0)
```

- [ ] **Step 4: Run to verify it passes (on Windows with deps)**

Run: `python -m pytest tests/test_host_conpty.py -v`
Expected: both host tests PASS on Windows. On non-Windows/without deps, the ConPTY test is skipped and `test_control_protocol_roundtrip` passes.

- [ ] **Step 5: Commit**

```bash
git add agent_host.py tests/test_host_conpty.py
git commit -m "$(cat <<'EOF'
feat: wire agent_host to ConPTY (pywinpty) with auto-accept + pump loop

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: `win_backend.py` — talk to hosts via status files + sockets

**Files:**
- Create: `win_backend.py`
- Test: `tests/test_win_backend.py`

- [ ] **Step 1: Write the failing test (uses a stub host server, no claude)**

```python
# tests/test_win_backend.py
import json
import socket
import socketserver
import threading
from pathlib import Path

import win_backend


class _StubHandler(socketserver.StreamRequestHandler):
    def handle(self):
        line = self.rfile.readline().decode().strip()
        cmd, _, arg = line.partition(" ")
        store = self.server.store
        if cmd == "PING":
            self.wfile.write(b"OK")
        elif cmd == "STATE":
            self.wfile.write(json.dumps({"ready": True, "alive": True}).encode())
        elif cmd == "CAPTURE":
            self.wfile.write(b"esc to interrupt")
        elif cmd == "SEND":
            store.append(arg); self.wfile.write(b"OK")
        elif cmd == "STOP":
            self.wfile.write(b"OK")


def _stub_server():
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _StubHandler)
    srv.store = []
    srv.allow_reuse_address = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _write_status(tmp_state, name, port):
    d = tmp_state / "win" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "status.json").write_text(json.dumps(
        {"name": name, "pid": 999999, "port": port, "ready": True}))


def test_capture_send_exists(tmp_path, monkeypatch):
    monkeypatch.setattr(win_backend.common, "STATE_DIR", tmp_path)
    srv = _stub_server()
    port = srv.server_address[1]
    _write_status(tmp_path, "w1", port)
    b = win_backend.WinBackend()
    try:
        assert b.worker_exists("w1") is True
        assert "interrupt" in b.capture("w1", 50)
        b.send_text("w1", "hello there")
        assert srv.store == ["hello there"]
        assert b.list_workers() == ["w1"]
        assert b.session_exists() is True
    finally:
        srv.shutdown()

def test_missing_worker_is_gone(tmp_path, monkeypatch):
    monkeypatch.setattr(win_backend.common, "STATE_DIR", tmp_path)
    b = win_backend.WinBackend()
    assert b.worker_exists("ghost") is False
    assert b.list_workers() == []
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_win_backend.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'win_backend'`.

- [ ] **Step 3: Create `win_backend.py`**

```python
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

WIN_DIR = lambda: common.STATE_DIR / "win"          # noqa: E731
HOST_SCRIPT = Path(__file__).resolve().parent / "agent_host.py"


def _status_path(name: str) -> Path:
    return WIN_DIR() / name / "status.json"


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

    def _port(self, name: str) -> int | None:
        st = _read_status(name)
        return st.get("port") if st else None

    def list_workers(self) -> list[str]:
        d = WIN_DIR()
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
        # started but not confirmed ready
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
        for n in [c.name for c in WIN_DIR().iterdir()] if WIN_DIR().is_dir() else []:
            self.kill(n)

    def attach_hint(self) -> str:
        return ("Each worker runs in its own console window titled "
                "'corch:<name>'. Bring one to the front from the taskbar.")
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/test_win_backend.py tests/test_backend.py -v`
Expected: `test_win_backend.py` PASS (2). `test_backend.py::test_default_is_win_on_win32` now PASS too.

- [ ] **Step 5: Commit**

```bash
git add win_backend.py tests/test_win_backend.py
git commit -m "$(cat <<'EOF'
feat: WinBackend — discover hosts via status files, drive via sockets

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: Wire `orch.py` to the backend

**Files:**
- Modify: `orch.py`
- Test: `tests/test_orch_wiring.py`

Replace the tmux-specific module-level helpers in `orch.py` with delegations to `backend.get_backend()`, keep the marker-based `_worker_state`, and import constants from `common`. The public CLI commands stay the same.

- [ ] **Step 1: Write the failing test (drives orch through a fake backend)**

```python
# tests/test_orch_wiring.py
import types
import orch

class FakeBackend:
    def __init__(self): self.sent = []
    def available(self): return True
    def install_hint(self): return "install"
    def session_exists(self): return True
    def list_workers(self): return ["w1"]
    def worker_exists(self, n): return n == "w1"
    def spawn(self, *a, **k): return True, ""
    def capture(self, n, lines=200): return "esc to interrupt"
    def send_text(self, n, t): self.sent.append((n, t))
    def kill(self, n): pass
    def kill_all(self): pass
    def set_scrollback(self, n): pass
    def attach_hint(self): return "hint"

def test_worker_state_uses_capture(monkeypatch):
    fb = FakeBackend()
    monkeypatch.setattr(orch, "_backend", fb)
    assert orch._worker_state("w1") == "busy"   # 'esc to interrupt'
    fb.capture = lambda n, lines=200: "worker-done: built it"
    assert orch._worker_state("w1") == "done"
    fb.capture = lambda n, lines=200: "idle prompt"
    assert orch._worker_state("w1") == "idle"
    assert orch._worker_state("nope") == "gone"

def test_send_delegates(monkeypatch):
    fb = FakeBackend()
    monkeypatch.setattr(orch, "_backend", fb)
    orch._send_text("w1", "hello")
    assert fb.sent == [("w1", "hello")]
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_orch_wiring.py -v`
Expected: FAIL (orch still defines tmux helpers / no `_backend`).

- [ ] **Step 3: Edit `orch.py`**

Replace the constants block and tmux helpers (current lines ~32–206) with imports from `common` + a module-level backend and delegations. Concretely:

Remove the duplicated constants (`SESSION`, `STATE_DIR`, markers, `NAME_RE`, `_now`, `_die`, launcher) and the tmux helpers (`_have_tmux`, `_tmux`, `_target`, `_session_exists`, `_windows`, `_window_exists`, `_capture`, `_send_text`, `_wait_ready`, launcher constants, `_spawn_window`). Add:

```python
import common
from common import (SESSION, STATE_DIR, TASKS_DIR, REGISTRY, NAME_RE,
                    READY_MARKERS, BUSY_MARKERS, TRUST_MARKERS, BYPASS_MARKER,
                    DONE_MARKER)
from common import _now, _die
import backend

_backend = backend.get_backend()

def _have_backend() -> bool:
    return _backend.available()

def _window_exists(name: str) -> bool:
    return _backend.worker_exists(name)

def _windows() -> list[str]:
    return _backend.list_workers()

def _session_exists() -> bool:
    return _backend.session_exists()

def _capture(name: str, lines: int = 200) -> str:
    return _backend.capture(name, lines)

def _send_text(name: str, text: str) -> None:
    _backend.send_text(name, text)

def _worker_state(name: str) -> str:
    if not _window_exists(name):
        return "gone"
    cap = _capture(name, 120).lower()
    if any(m in cap for m in BUSY_MARKERS):
        return "busy"
    if DONE_MARKER in cap:
        return "done"
    return "idle"
```

In `cmd_spawn`, replace the tmux guard and window creation:

```python
    if not _have_backend():
        _die(_backend.install_hint())
    ...
    ready, err = _backend.spawn(name, str(proj))
    if err:
        _die(f"failed to start worker: {err}")
```

In `cmd_stop`, replace tmux kills:

```python
    if a.name == "--all" or a.all:
        _backend.kill_all()
        for n in reg:
            reg[n]["status"] = "stopped"
        _save(reg)
        print("stopped all workers")
        return
    ...
    _backend.kill(a.name)
```

In `cmd_attach`:

```python
def cmd_attach(a):
    print(_backend.attach_hint())
```

Keep `cmd_list`, `cmd_peek`, `cmd_send`, `cmd_wait`, `_load`, `_save` unchanged (they already call the helpers above).

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/test_orch_wiring.py tests/test_backend.py tests/test_common.py -v`
Expected: PASS. Also smoke the CLI: `python orch.py list` → prints "no workers" (Windows) without error.

- [ ] **Step 5: Commit**

```bash
git add orch.py tests/test_orch_wiring.py
git commit -m "$(cat <<'EOF'
refactor: route orch.py terminal ops through the pluggable backend

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 8: Wire `fleet.py` to the backend

**Files:**
- Modify: `fleet.py`
- Test: `tests/test_orch_wiring.py` (extend)

`fleet.py` reaches into `orch` for primitives and three raw `orch._tmux(...)` calls. Replace the raw calls with backend methods; the primitive lookups (`orch._window_exists`, `orch._capture`, `orch._send_text`, `orch._spawn_window`, `orch._session_exists`) keep working because Task 7 kept those names — except `_spawn_window` and `_target`, which no longer exist. Fix those references.

- [ ] **Step 1: Add a wiring test**

```python
# append to tests/test_orch_wiring.py
import fleet

def test_fleet_uses_backend_for_activity(monkeypatch):
    fb = FakeBackend()
    monkeypatch.setattr(orch, "_backend", fb)
    # agent_activity reads orch._capture -> backend.capture ('esc to interrupt')
    assert fleet.agent_activity("w1") == "busy"
    fb.capture = lambda n, lines=200: "? for shortcuts"
    assert fleet.agent_activity("w1") == "idle"
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_orch_wiring.py::test_fleet_uses_backend_for_activity -v`
Expected: FAIL if `fleet` still references removed names (e.g., `orch._spawn_window`) at import or call time.

- [ ] **Step 3: Edit `fleet.py`**

- `ensure_agent`: replace
  `orch._tmux("set-option", "-g", "history-limit", "5000")` → `orch._backend.set_scrollback(5000)`
  and `orch._spawn_window(name, str(proj), _role_file(agent))` → `orch._backend.spawn(name, str(proj), _role_file(agent))`.
- `do_POST` `/api/agent/restart`: replace
  `orch._tmux("kill-window", "-t", orch._target(name))` → `orch._backend.kill(name)`.
- `cmd_down`: replace the `orch._session_exists()` + `orch._tmux("kill-session", ...)` block with:

```python
def cmd_down(a):
    if orch._session_exists():
        orch._backend.kill_all()
        print("stopped all agent terminals")
    else:
        print("no agents running")
```

Leave `agent_activity`, `agent_attention`, `agent_ready`, `send_message` unchanged — they use `orch._capture`/`orch._send_text`/`orch._window_exists`, which still exist.

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/test_orch_wiring.py -v`
Expected: PASS. Smoke: `python fleet.py status` → prints the roster from `fleet.json` without a tmux error (agents show `offline`).

- [ ] **Step 5: Commit**

```bash
git add fleet.py tests/test_orch_wiring.py
git commit -m "$(cat <<'EOF'
refactor: route fleet.py terminal ops through the backend

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 9: Native entry points (PowerShell/cmd) + bash wrapper fix

**Files:**
- Create: `orch.cmd`, `fleet.cmd`
- Modify: `orch`, `fleet` (bash wrappers)

- [ ] **Step 1: Create `orch.cmd`**

```bat
@echo off
python "%~dp0orch.py" %*
```

- [ ] **Step 2: Create `fleet.cmd`**

```bat
@echo off
python "%~dp0fleet.py" %*
```

- [ ] **Step 3: Make the bash wrappers prefer real `python`**

Edit `orch` (and `fleet` identically), replacing the `exec python3 ...` line with a probe that avoids the Windows Store stub:

```bash
#!/usr/bin/env bash
# Thin wrapper so you can run `./orch ...` from anywhere in the project.
DIR="$(dirname "$(readlink -f "$0")")"
# On Windows, `python3` is often a non-functional Microsoft Store alias.
if command -v python3 >/dev/null 2>&1 && python3 -c '' >/dev/null 2>&1; then
  PY=python3
else
  PY=python
fi
exec "$PY" "$DIR/orch.py" "$@"
```

(For `fleet`, change `orch.py` → `fleet.py`.)

- [ ] **Step 4: Verify**

Run (PowerShell): `.\orch.cmd list`
Expected: prints "no workers" with no error.
Run (git-bash): `./orch list`
Expected: same.

- [ ] **Step 5: Commit**

```bash
git add orch.cmd fleet.cmd orch fleet
git commit -m "$(cat <<'EOF'
feat: add Windows (.cmd) entry points and fix python3-stub fallback

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 10: README + manual integration smoke

**Files:**
- Modify: `README.md`
- Create: `docs/superpowers/plans/manual-smoke.md` (the manual checklist)

- [ ] **Step 1: Add a "Windows" section to `README.md`**

Document: `pip install -r requirements.txt`; run via `orch.cmd`/`fleet.cmd` (PowerShell) or `./orch`/`./fleet` (git-bash); note that each worker opens its own console window titled `corch:<name>`; note `ORCH_BACKEND=tmux|win` override.

- [ ] **Step 2: Write the manual smoke checklist** (`docs/superpowers/plans/manual-smoke.md`)

```markdown
# Manual smoke test (real claude, Windows)
1. mkdir %TEMP%\orch-smoke
2. .\orch.cmd spawn --name smoke --dir %TEMP%\orch-smoke ^
     --task "Create hello.txt containing 'hi', then print a line starting WORKER-DONE:"
   -> a new console window opens, claude boots, trust/bypass auto-accepted.
3. .\orch.cmd list        -> smoke shows busy, then done
4. .\orch.cmd peek smoke  -> shows claude output incl. WORKER-DONE
5. dir %TEMP%\orch-smoke\hello.txt  -> exists
6. .\orch.cmd send smoke "now create bye.txt with 'bye'"  -> bye.txt appears
7. .\orch.cmd stop smoke  -> window closes; status file removed
8. .\orch.cmd list        -> no workers
```

- [ ] **Step 3: Run the full automated suite**

Run: `python -m pytest -v`
Expected: all non-skipped tests PASS; ConPTY test PASS on Windows w/ deps.

- [ ] **Step 4: Run the manual smoke checklist** and confirm each step.

- [ ] **Step 5: Commit**

```bash
git add README.md docs/superpowers/plans/manual-smoke.md
git commit -m "$(cat <<'EOF'
docs: Windows usage + manual integration smoke checklist

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Risks / pywinpty fallback (contingency, only if Task 0 install fails)

If `pip install pywinpty` cannot produce a working module on this Python:

- Implement `spawn_conpty` in `agent_host.py` using **ctypes ConPTY** instead of `winpty.PtyProcess`:
  - `CreatePseudoConsole(COORD, hPipeIn, hPipeOut, 0, &hPC)` from `kernel32`.
  - Two anonymous pipes (`CreatePipe`) for PTY in/out.
  - `STARTUPINFOEX` with `InitializeProcThreadAttributeList` +
    `UpdateProcThreadAttribute(PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE, hPC)`.
  - `CreateProcessW(..., EXTENDED_STARTUPINFO_PRESENT, ...)`.
  - Background thread doing `ReadFile` on the output pipe → feed `ScreenBuffer`;
    `WriteFile` on the input pipe for `inject`.
  - `_PtyChild` adapts these handles to `.write(str)`/`.read()`/`.isalive()`/`.terminate()`.
- `WinBackend.available()` then checks `sys.platform == "win32"` + `pyte` import
  only (drop the `winpty` import check).
- Keep `pyte` (pure Python; installs fine). Everything else in the plan is unchanged.

Decision rule: spend at most ~30 min trying to get a `pywinpty` wheel/build; if it
doesn't yield a working import, switch to the ctypes path and note it in the
Task 0 commit.

---

## Self-Review

- **Spec coverage:** backend seam (Tasks 2,3,7,8) ✓; per-worker ConPTY host in a visible window (Task 5) ✓; pyte rendering keeps marker detection (Tasks 4,5,7) ✓; socket control protocol (Task 5,6) ✓; status-file registry + liveness (Task 6) ✓; `python3`/PowerShell entry points (Task 9) ✓; deps + requirements (Task 0,10) ✓; error handling — deps missing/host crash/port-in-use/orphan kill (Tasks 6,7) ✓; testing — selection, registry, pyte, protocol, integration smoke (Tasks 2–10) ✓; pywinpty-3.14 fallback (contingency section) ✓.
- **Placeholder scan:** no "TBD/TODO/handle edge cases"; every code step shows code; commands have expected output.
- **Type consistency:** `Backend` method names (`available`, `install_hint`, `session_exists`, `list_workers`, `worker_exists`, `spawn`, `capture`, `send_text`, `kill`, `kill_all`, `set_scrollback`, `attach_hint`) are identical across `backend.py`, `tmux_backend.py`, `win_backend.py`, and the `orch.py` delegations and `FakeBackend` test double. Host control verbs (`PING/STATE/CAPTURE/SEND/STOP`) match between `agent_host._Handler`, `win_backend._ask` callers, and the stub server in tests. `AgentHost` members (`screen`, `inject`, `mark_ready_if_seen`, `state`, `start_server`, `stop_server`, `shutdown`, `for_test`) are used consistently.
- **Known caveat flagged for the implementer:** `backend.py` selection tests (Task 2) only fully pass once Tasks 3 and 6 create the backend modules; pywinpty method names must be reconciled against the Task 0 Step 3 probe.
