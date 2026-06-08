"""Worker status / heartbeat protocol.

Each worker keeps a small JSON file at ``~/.claude-orch/status/<agent>.json``
describing its current state, the task it is working on (if any), and when it
last beat. fleet.py / orch.py read this file *first* when deciding whether a
running task is busy, idle, or finished, falling back to the legacy
``WORKER-DONE:`` marker + tmux-activity heuristic only when the file is
missing — so workers spawned before this protocol existed keep working.

Fields:
    state          one of ``starting | running | done | error``
    last_beat      ISO-8601 UTC timestamp of the last write
    progress_note  short free-form string (e.g. truncated task description)
    task_id        the task id the worker is currently on ("" if none)
    pid            agent-host process id

All writes go through :func:`write_status`, which merges the supplied fields
into the existing record and atomically replaces the file (``.tmp`` + rename).
``last_beat`` is refreshed to "now" unless the caller passes it explicitly.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import common

STATUS_DIR = common.STATE_DIR / "status"
VALID_STATES = ("starting", "running", "done", "error")
# How long a heartbeat is considered fresh. The agent-host heartbeats every
# ~15s, so 60s gives 3 missed beats of slack before we treat the file as stale.
HEARTBEAT_FRESH_SECS = 60


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def status_path(agent: str) -> Path:
    """Absolute path of the status file for ``agent`` (file may not exist)."""
    return STATUS_DIR / f"{agent}.json"


def read_status(agent: str) -> dict | None:
    """Return the parsed status dict, or ``None`` if missing/unreadable."""
    p = status_path(agent)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def write_status(agent: str, **fields) -> dict:
    """Merge ``fields`` into the agent's status file and write atomically.

    Returns the resulting dict. Creates the status directory on first call.
    Refreshes ``last_beat`` to now unless an explicit ``last_beat`` was passed.
    Unknown ``state`` values raise ``ValueError`` so callers can't drift the
    protocol silently.
    """
    if "state" in fields and fields["state"] not in VALID_STATES:
        raise ValueError(f"invalid state {fields['state']!r}; "
                         f"expected one of {VALID_STATES}")
    data = read_status(agent) or {
        "state": "starting", "last_beat": "", "progress_note": "",
        "task_id": "", "pid": 0,
    }
    data.update(fields)
    if "last_beat" not in fields:
        data["last_beat"] = _now_iso()
    STATUS_DIR.mkdir(parents=True, exist_ok=True)
    p = status_path(agent)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(p)
    return data


def clear_status(agent: str) -> None:
    """Delete the agent's status file if present."""
    p = status_path(agent)
    if p.exists():
        try:
            p.unlink()
        except OSError:
            pass


def heartbeat_fresh(status: dict | None, now: datetime | None = None) -> bool:
    """True iff ``status`` has a ``last_beat`` within HEARTBEAT_FRESH_SECS."""
    if not status:
        return False
    try:
        t = datetime.strptime(status.get("last_beat", ""), "%Y-%m-%dT%H:%M:%SZ")
        t = t.replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    now = now or datetime.now(timezone.utc)
    return (now - t).total_seconds() <= HEARTBEAT_FRESH_SECS
