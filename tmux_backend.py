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

    def send_keys(self, name: str, data: bytes) -> None:
        # Best-effort raw injection over tmux: send-keys -l writes literally,
        # but tmux interprets a small set of bytes as key names. We translate
        # the common VT escape sequences (arrows, Enter, Esc, Ctrl-C, …) into
        # tmux key names so an interactive dashboard user gets a usable
        # experience even when the fleet is running under tmux instead of
        # ConPTY.
        target = self._target(name)
        i, n = 0, len(data)
        seqs = [
            (b"\x1b[A", "Up"), (b"\x1b[B", "Down"),
            (b"\x1b[C", "Right"), (b"\x1b[D", "Left"),
            (b"\x1b[H", "Home"), (b"\x1b[F", "End"),
            (b"\x1b[3~", "DC"), (b"\x1b[5~", "PPage"), (b"\x1b[6~", "NPage"),
        ]
        single = {b"\r": "Enter", b"\n": "Enter", b"\t": "Tab",
                  b"\x1b": "Escape", b"\x7f": "BSpace", b"\x08": "BSpace",
                  b"\x03": "C-c", b"\x04": "C-d"}
        buf = bytearray()

        def flush_buf():
            if not buf:
                return
            self._tmux("send-keys", "-t", target, "-l", "--",
                       buf.decode("utf-8", errors="replace"))
            buf.clear()

        while i < n:
            matched = False
            for raw, key in seqs:
                if data[i:i + len(raw)] == raw:
                    flush_buf()
                    self._tmux("send-keys", "-t", target, key)
                    i += len(raw)
                    matched = True
                    break
            if matched:
                continue
            b = data[i:i + 1]
            if b in single:
                flush_buf()
                self._tmux("send-keys", "-t", target, single[b])
                i += 1
                continue
            buf.append(data[i])
            i += 1
        flush_buf()

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
