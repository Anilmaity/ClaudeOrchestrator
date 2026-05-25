#!/usr/bin/env python3
"""
orch.py — Claude Orchestrator control CLI.

Drives multiple autonomous `claude` workers, each running in its own visible
tmux window inside a single tmux session (default: "corch"). A "manager"
Claude session calls this CLI to spawn, monitor, message, and stop workers.

Design choices (see CLAUDE.md):
  * Each worker is a real, attachable terminal -> tmux window.
  * Workers are fully autonomous -> `claude --dangerously-skip-permissions`.
  * Each worker works in its own project directory (no shared worktree).
  * Task text lives in a file the worker is told to read, so we never have to
    shell-quote long/multiline prompts.

Stdlib only. Python 3.8+.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
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
# Folder-trust dialog we auto-accept (default option) so spawning is hands-off.
TRUST_MARKERS = ("do you trust", "trust the files")
# One-time "Bypass Permissions mode" acceptance; option 2 is "Yes, I accept".
BYPASS_MARKER = "yes, i accept"
# Sanitize the worker shell before launch: claude refuses to run inside a
# Python venv, and an inherited venv on PATH would shadow the worker's own
# interpreter. Strip the venv bin from PATH and clear VIRTUAL_ENV.
LAUNCH_CMD = (
    r'[ -n "$VIRTUAL_ENV" ] && export PATH="${PATH//$VIRTUAL_ENV\/bin:/}"; '
    r"unset VIRTUAL_ENV; claude --dangerously-skip-permissions"
)
# Marker we instruct workers to print when finished.
DONE_MARKER = "worker-done"

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _die(msg: str, code: int = 1) -> "NoReturn":  # type: ignore[name-defined]
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(code)


def _have_tmux() -> bool:
    return shutil.which("tmux") is not None


def _tmux(*args: str, check: bool = False) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["tmux", *args], capture_output=True, text=True, check=check
        )
    except FileNotFoundError:
        # tmux not installed yet: behave like a failed call so callers degrade.
        return subprocess.CompletedProcess(args, 127, "", "tmux: not found")


def _target(name: str) -> str:
    return f"{SESSION}:{name}"


def _session_exists() -> bool:
    return _tmux("has-session", "-t", SESSION).returncode == 0


def _windows() -> list[str]:
    """Live window names in the session."""
    if not _session_exists():
        return []
    r = _tmux("list-windows", "-t", SESSION, "-F", "#{window_name}")
    if r.returncode != 0:
        return []
    return [ln for ln in r.stdout.splitlines() if ln]


def _window_exists(name: str) -> bool:
    return name in _windows()


def _capture(name: str, lines: int = 200) -> str:
    """Rendered text of a worker's pane, including recent scrollback."""
    r = _tmux("capture-pane", "-p", "-t", _target(name), "-S", f"-{lines}")
    return r.stdout if r.returncode == 0 else ""


def _send_text(name: str, text: str) -> None:
    """Type a single submitted message into a worker's Claude prompt."""
    one_line = " ".join(text.split())  # collapse newlines so Enter submits once
    _tmux("send-keys", "-t", _target(name), "-l", "--", one_line)
    time.sleep(0.25)
    _tmux("send-keys", "-t", _target(name), "Enter")


def _worker_state(name: str) -> str:
    """Heuristic: 'busy' | 'done' | 'idle' | 'gone' from pane contents."""
    if not _window_exists(name):
        return "gone"
    cap = _capture(name, 120).lower()
    if any(m in cap for m in BUSY_MARKERS):
        return "busy"
    if DONE_MARKER in cap:
        return "done"
    return "idle"


# --------------------------------------------------------------------------- #
# registry
# --------------------------------------------------------------------------- #
def _load() -> dict:
    if REGISTRY.exists():
        try:
            return json.loads(REGISTRY.read_text())
        except json.JSONDecodeError:
            return {}
    return {}

def _save(reg: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    REGISTRY.write_text(json.dumps(reg, indent=2))


# --------------------------------------------------------------------------- #
# readiness
# --------------------------------------------------------------------------- #
def _wait_ready(name: str, timeout: float = 40.0) -> bool:
    """Wait for the worker's TUI to be ready, auto-accepting trust dialogs."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        low = _capture(name, 60).lower()
        if BYPASS_MARKER in low:  # "Bypass Permissions mode" warning -> accept
            _tmux("send-keys", "-t", _target(name), "2")
            time.sleep(0.4)
            _tmux("send-keys", "-t", _target(name), "Enter")
            time.sleep(1.5)
            continue
        if any(m in low for m in TRUST_MARKERS):
            _tmux("send-keys", "-t", _target(name), "Enter")  # accept default
            time.sleep(1.0)
            continue
        if any(m in low for m in READY_MARKERS):
            return True
        time.sleep(0.5)
    return False


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #
def cmd_spawn(a: argparse.Namespace) -> None:
    if not _have_tmux():
        _die("tmux is not installed. Run: sudo apt install -y tmux")
    name = a.name
    if not NAME_RE.match(name):
        _die(f"invalid worker name {name!r} (use letters, digits, . _ -)")

    proj = Path(a.dir).expanduser().resolve()
    if not proj.is_dir():
        _die(f"project dir does not exist: {proj}")

    if a.task_file:
        task = Path(a.task_file).expanduser().read_text()
    elif a.task:
        task = a.task
    else:
        _die("provide --task \"...\" or --task-file PATH")

    if _window_exists(name):
        _die(f"worker {name!r} already exists. Use 'send', 'stop', or pick a new name.")

    TASKS_DIR.mkdir(parents=True, exist_ok=True)
    task_path = TASKS_DIR / f"{name}.md"
    task_path.write_text(task)

    # create the window (and session if needed), starting in the project dir
    if not _session_exists():
        r = _tmux("new-session", "-d", "-s", SESSION, "-n", name, "-c", str(proj))
    else:
        r = _tmux("new-window", "-t", SESSION, "-n", name, "-c", str(proj))
    if r.returncode != 0:
        _die(f"failed to create tmux window: {r.stderr.strip()}")

    # launch the autonomous claude TUI inside the window (venv-sanitized)
    _tmux("send-keys", "-t", _target(name), "-l", "--", LAUNCH_CMD)
    _tmux("send-keys", "-t", _target(name), "Enter")

    if not _wait_ready(name):
        print(f"warning: {name} TUI not confirmed ready; sending task anyway",
              file=sys.stderr)

    kickoff = (
        f"You are an autonomous worker. Read the file {task_path} for your full "
        f"task and complete it end to end without asking questions; make "
        f"reasonable decisions on your own. When finished, print one line "
        f"starting with 'WORKER-DONE:' followed by a short summary of what you "
        f"changed."
    )
    _send_text(name, kickoff)

    reg = _load()
    reg[name] = {
        "project_dir": str(proj),
        "task_file": str(task_path),
        "created_at": _now(),
        "status": "running",
    }
    _save(reg)
    print(f"spawned worker '{name}' in {proj}")
    print(f"  task: {task_path}")
    print(f"  watch: tmux attach -t {SESSION}  (then Ctrl-b w to pick a window)")


def cmd_list(a: argparse.Namespace) -> None:
    reg = _load()
    live = set(_windows())
    names = sorted(set(reg) | live)
    if not names:
        print("no workers. Spawn one with: ./orch spawn --name NAME --dir DIR --task \"...\"")
        return
    print(f"{'WORKER':<16} {'STATE':<8} {'PROJECT DIR':<40} CREATED")
    for n in names:
        info = reg.get(n, {})
        state = _worker_state(n) if n in live else "gone"
        print(f"{n:<16} {state:<8} {info.get('project_dir','?'):<40} "
              f"{info.get('created_at','?')}")


def cmd_peek(a: argparse.Namespace) -> None:
    if not _window_exists(a.name):
        _die(f"no live worker named {a.name!r} (see './orch list')")
    print(f"--- {a.name} [{_worker_state(a.name)}] (last {a.lines} lines) ---")
    print(_capture(a.name, a.lines).rstrip())


def cmd_send(a: argparse.Namespace) -> None:
    if not _window_exists(a.name):
        _die(f"no live worker named {a.name!r} (see './orch list')")
    _send_text(a.name, a.message)
    print(f"sent to {a.name}: {a.message}")


def cmd_wait(a: argparse.Namespace) -> None:
    if not _window_exists(a.name):
        _die(f"no live worker named {a.name!r}")
    deadline = time.time() + a.timeout
    idle_streak = 0
    while time.time() < deadline:
        st = _worker_state(a.name)
        if st == "done":
            print(f"{a.name}: done")
            return
        if st == "gone":
            print(f"{a.name}: window closed")
            return
        idle_streak = idle_streak + 1 if st == "idle" else 0
        if idle_streak >= 3:  # ~idle for 3 consecutive polls
            print(f"{a.name}: idle (no DONE marker yet — peek to inspect)")
            return
        time.sleep(a.interval)
    print(f"{a.name}: still busy after {a.timeout}s (timeout)")


def cmd_stop(a: argparse.Namespace) -> None:
    reg = _load()
    if a.name == "--all" or a.all:
        if _session_exists():
            _tmux("kill-session", "-t", SESSION)
        for n in reg:
            reg[n]["status"] = "stopped"
        _save(reg)
        print("stopped all workers (killed session)")
        return
    if not _window_exists(a.name):
        _die(f"no live worker named {a.name!r}")
    _tmux("kill-window", "-t", _target(a.name))
    if a.name in reg:
        reg[a.name]["status"] = "stopped"
        _save(reg)
    print(f"stopped worker '{a.name}'")


def cmd_attach(a: argparse.Namespace) -> None:
    print(f"tmux attach -t {SESSION}")
    print("  Ctrl-b w  -> list/switch windows   |   Ctrl-b d -> detach (leave running)")


# --------------------------------------------------------------------------- #
# argparse
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="orch", description="Claude worker orchestrator")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("spawn", help="start a new autonomous worker")
    s.add_argument("--name", required=True)
    s.add_argument("--dir", required=True, help="project directory the worker runs in")
    s.add_argument("--task", help="task text")
    s.add_argument("--task-file", help="path to a file containing the task")
    s.set_defaults(func=cmd_spawn)

    s = sub.add_parser("list", help="list workers and their state")
    s.set_defaults(func=cmd_list)

    s = sub.add_parser("peek", help="show a worker's recent terminal output")
    s.add_argument("name")
    s.add_argument("--lines", type=int, default=60)
    s.set_defaults(func=cmd_peek)

    s = sub.add_parser("send", help="send a follow-up message to a worker")
    s.add_argument("name")
    s.add_argument("message")
    s.set_defaults(func=cmd_send)

    s = sub.add_parser("wait", help="block until a worker is done/idle")
    s.add_argument("name")
    s.add_argument("--timeout", type=float, default=900)
    s.add_argument("--interval", type=float, default=5)
    s.set_defaults(func=cmd_wait)

    s = sub.add_parser("stop", help="stop a worker (or --all)")
    s.add_argument("name", nargs="?", default="")
    s.add_argument("--all", action="store_true", help="stop every worker")
    s.set_defaults(func=cmd_stop)

    s = sub.add_parser("attach", help="print how to watch workers live")
    s.set_defaults(func=cmd_attach)

    return p


def main() -> None:
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
