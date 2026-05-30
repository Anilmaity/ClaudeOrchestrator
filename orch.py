#!/usr/bin/env python3
"""
orch.py — Claude Orchestrator control CLI.

Drives multiple autonomous `claude` workers, each running in its own visible
terminal window (tmux on Linux/Mac, ConPTY on Windows) inside a single session.
A "manager" Claude session calls this CLI to spawn, monitor, message, and stop
workers.

Design choices (see CLAUDE.md):
  * Each worker is a real, attachable terminal.
  * Workers are fully autonomous -> `claude --dangerously-skip-permissions`.
  * Each worker works in its own project directory (no shared worktree).
  * Task text lives in a file the worker is told to read, so we never have to
    shell-quote long/multiline prompts.

Stdlib only (except the pluggable backend). Python 3.8+.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import common
from common import (SESSION, STATE_DIR, TASKS_DIR, REGISTRY, NAME_RE,
                    READY_MARKERS, BUSY_MARKERS, TRUST_MARKERS, BYPASS_MARKER,
                    DONE_MARKER)
from common import _now, _die
import backend
import worker_status

_backend = backend.get_backend()


# --------------------------------------------------------------------------- #
# backend delegation helpers
# --------------------------------------------------------------------------- #

def _have_backend() -> bool:
    return _backend.available()


def _window_exists(name: str) -> bool:
    return _backend.worker_exists(name)


def _windows() -> list:
    return _backend.list_workers()


def _session_exists() -> bool:
    return _backend.session_exists()


def _capture(name: str, lines: int = 200) -> str:
    return _backend.capture(name, lines)


def _send_text(name: str, text: str) -> None:
    _backend.send_text(name, text)


def _send_keys(name: str, data: bytes) -> None:
    """Raw-keystroke path used by the dashboard's interactive terminal — bytes
    flow verbatim to the worker's PTY (no whitespace coalescing, no auto
    Enter). Falls back to the backend's default no-op when the active backend
    hasn't implemented it.
    """
    _backend.send_keys(name, data)


def _worker_state(name: str) -> str:
    """'busy' | 'done' | 'idle' | 'gone'.

    Reads the worker-status file *first* (the agent_host updates it from the
    BUSY-marker state of the embedded claude TUI and on shutdown), and falls
    back to the legacy capture + ``WORKER-DONE:`` marker heuristic when the
    file is missing or its heartbeat is stale — so workers spawned before
    this protocol existed keep being detected correctly.
    """
    st = worker_status.read_status(name)
    if st is not None and worker_status.heartbeat_fresh(st):
        s = st.get("state")
        if s == "running":
            return "busy"
        if s == "done":
            return "done"
        if s == "starting":
            return "idle"
        # "error" -> fall through to legacy detection below.
    if not _window_exists(name):
        return "gone"
    cap = _capture(name, 120).lower()
    if any(m in cap for m in BUSY_MARKERS):
        return "busy"
    if any(ln.lstrip().startswith(DONE_MARKER) for ln in cap.splitlines()):
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
    tmp = REGISTRY.with_suffix(".tmp")
    tmp.write_text(json.dumps(reg, indent=2))
    tmp.replace(REGISTRY)


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #
def cmd_spawn(a: argparse.Namespace) -> None:
    if not _have_backend():
        _die(_backend.install_hint())
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

    # create a worker terminal running claude directly
    ready, err = _backend.spawn(name, str(proj))
    if err:
        _die(f"failed to start worker: {err}")
    if not ready:
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
    print(f"  watch: {_backend.attach_hint()}")


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
    if not a.all and not a.name:
        _die("provide a worker name or --all")
    reg = _load()
    if a.name == "--all" or a.all:
        if _session_exists():
            _backend.kill_all()
        for n in reg:
            reg[n]["status"] = "stopped"
        _save(reg)
        print("stopped all workers (killed session)")
        return
    if not _window_exists(a.name):
        _die(f"no live worker named {a.name!r}")
    _backend.kill(a.name)
    if a.name in reg:
        reg[a.name]["status"] = "stopped"
        _save(reg)
    print(f"stopped worker '{a.name}'")


def cmd_attach(a: argparse.Namespace) -> None:
    print(_backend.attach_hint())


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


def _force_utf8_output() -> None:
    """Make stdout/stderr UTF-8 so printing captured TUI output never crashes.

    Windows consoles (and redirected pipes) default to cp1252, which can't encode
    the box-drawing glyphs Claude's TUI emits; that raised UnicodeEncodeError in
    `peek`. Reconfigure best-effort; ignore streams that don't support it.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def main() -> None:
    _force_utf8_output()
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
