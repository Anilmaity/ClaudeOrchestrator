#!/usr/bin/env python3
"""
fleet.py — a config-driven fleet of persistent Claude agents + web dashboard.

`fleet.json` defines the agents (name, role, project_dir). Each agent is one
long-lived `claude` terminal in the shared tmux session, launched with its role
injected via --append-system-prompt. Tasks are queued (from the dashboard or
`./fleet add`) and an auto-dispatch loop sends each pending task to its agent
when that agent is idle, then watches for completion.

  ./fleet up            start agents + dispatcher + dashboard (blocks)
  ./fleet status        print agents and tasks
  ./fleet add NAME "…"  queue a task for an agent
  ./fleet send NAME "…" send a message straight to a live agent (steer / answer)
  ./fleet cancel ID     cancel a pending task
  ./fleet down          stop all agent terminals

Reuses the tmux primitives in orch.py. Stdlib only.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import orch  # tmux primitives + shared constants
import rate_governor  # central rate-limit governor (cap, stagger, 429 retry)
import worker_status  # file-based worker state + heartbeat protocol
import git_checkpoint  # per-task git branches (base_sha/head_sha diff range)
import connectors  # external escalation channels (telegram, whatsapp)
import auth  # HTTP Basic Auth gate for the dashboard

HERE = Path(__file__).resolve().parent
CONFIG = HERE / "fleet.json"
DASHBOARD = HERE / "dashboard.html"
VENDOR_ALPINE = HERE / "vendor" / "alpine.min.js"   # served at /vendor/alpine.min.js

STATE_DIR = orch.STATE_DIR
TASKS_FILE = STATE_DIR / "fleet_tasks.json"
FLEET_PID = STATE_DIR / "fleet.pid"
TASK_LOGS = STATE_DIR / "logs" / "tasks"
ROLES_DIR = STATE_DIR / "roles"
# T-06 escalation log. Appended to whenever the dispatcher escalates a task
# that exhausted ``max_retries`` — both the PM-handoff case and the no-PM
# attention-bar case write here so an external reader (the PM agent, an
# operator, or the new test) has a durable record of every escalation.
NOTIFICATIONS_FILE = STATE_DIR / "fleet_notifications.json"

POLL = 2.0          # dispatcher poll interval (seconds)
DELIVER_GRACE = 10  # if an agent never goes busy this long after dispatch, retry
MAX_TRIES = 3       # give up delivering a task after this many attempts
CONFIRM_IDLE = 2    # consecutive idle polls required before a task is called done
CONFIRM_GONE = 3    # consecutive 'window gone' polls before a running task fails
# Per-task runtime watchdog: a wedged agent that keeps showing the busy marker
# would otherwise keep its task 'running' forever and head-of-line-block its
# queue. Fail a task once it has run longer than this — generous default so
# normal long work is never killed; override with env FLEET_TASK_TIMEOUT (secs).
try:
    TASK_TIMEOUT_SECS = int(os.environ.get("FLEET_TASK_TIMEOUT", "1800"))
except ValueError:
    TASK_TIMEOUT_SECS = 1800

_LOCK = threading.Lock()
# Serialize every read-modify-write of fleet.json. Dispatcher + ThreadingHTTPServer
# handlers share this process and can otherwise interleave: two helpers each load
# the file, mutate their own copy, then atomic-replace — the second write wipes
# the first. Reentrant so a helper that already holds the lock can call
# _write_config (which also acquires it) without deadlocking.
_CONFIG_LOCK = threading.RLock()
AGENTS: list[dict] = []     # active fleet, populated by `up`

DOCS_ROOT = HERE / "fleet_docs"          # uploaded documents live here
SHARED = "_shared"                        # reserved subfolder for shared docs
MAX_DOC_BYTES = 25 * 1024 * 1024          # 25 MB per-file upload cap
MAX_REQUEST_BYTES = MAX_DOC_BYTES * 2     # cap POST body memory (base64+JSON overhead)


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #
def load_config(path: Path = CONFIG) -> list[dict]:
    if not path.exists():
        orch._die(f"config not found: {path}\nCreate it (see fleet.json template).")
    data = json.loads(path.read_text(encoding="utf-8"))
    agents, seen = [], set()
    for a in data.get("agents", []):
        name = a.get("name", "")
        if not orch.NAME_RE.match(name):
            orch._die(f"invalid agent name {name!r} in {path}")
        if name in seen:
            orch._die(f"duplicate agent name {name!r} in {path}")
        seen.add(name)
        agents.append({
            "name": name,
            "role": a.get("role", ""),
            "project_dir": str(Path(a["project_dir"]).expanduser().resolve()),
            "project": a.get("project") or "",
            "manager_of": a.get("manager_of") or "",
        })
    if not agents:
        orch._die(f"no agents defined in {path}")
    return agents


def _normalize_dashboard_url(url: str) -> str:
    """Return a safe project dashboard URL or "".

    Only http(s) links are persisted; anything else (empty, junk, or an unsafe
    scheme like javascript:/ftp:) collapses to "" so the conditional CTA can't
    produce a broken or unsafe link. Single source of truth for the *_project
    helpers — handlers stay thin.
    """
    u = (url or "").strip()
    return u if u.startswith(("http://", "https://")) else ""


def load_projects(path: Path = CONFIG) -> list[dict]:
    """Top-level "projects" list, normalized to {name, path, description, dashboard_url}.

    Missing key ⇒ []. Validates each project name with orch.NAME_RE and rejects
    duplicates exactly the way load_config() does for agents.
    """
    if not path.exists():
        orch._die(f"config not found: {path}\nCreate it (see fleet.json template).")
    data = json.loads(path.read_text(encoding="utf-8"))
    projects, seen = [], set()
    for p in data.get("projects", []):
        name = p.get("name", "")
        if not orch.NAME_RE.match(name):
            orch._die(f"invalid project name {name!r} in {path}")
        if name in seen:
            orch._die(f"duplicate project name {name!r} in {path}")
        seen.add(name)
        projects.append({
            "name": name,
            "path": p.get("path") or "",
            "description": p.get("description") or "",
            "dashboard_url": p.get("dashboard_url") or "",
        })
    return projects


def _write_config(data: dict) -> None:
    """Atomically persist fleet.json (UTF-8) and refresh module-level AGENTS.

    The write mirrors the original _set_agent_path(): a sibling .tmp file is
    written then replace()d into place, and AGENTS is reloaded so the dashboard
    reflects the change on the next poll. Reference the module-global CONFIG so a
    monkeypatched config (tests) is honoured. Acquires _CONFIG_LOCK so a direct
    caller is still safe; helpers wrap their read-modify-write in the same lock
    so concurrent writers can't lose updates (reentrant lock).
    """
    global AGENTS
    with _CONFIG_LOCK:
        tmp = CONFIG.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(CONFIG)
        AGENTS = load_config(CONFIG)


def _set_agent_path(name: str, path: str) -> None:
    """Set one agent's project_dir in fleet.json (atomic) and refresh AGENTS."""
    with _CONFIG_LOCK:
        data = json.loads(CONFIG.read_text(encoding="utf-8"))
        found = False
        for a in data.get("agents", []):
            if a.get("name") == name:
                a["project_dir"] = path
                found = True
                break
        if not found:
            raise ValueError("unknown agent")
        _write_config(data)


_PM_ROLE_TEMPLATE = (
    "You are the PROJECT MANAGER and PRINCIPAL INVESTIGATOR for the project '<P>' "
    "in this Claude agent fleet. You coordinate the worker agents assigned to "
    "'<P>' — the agents in fleet.json whose \"project\" field equals \"<P>\" — "
    "AND you are responsible for actually delivering the project's outcomes, not "
    "just running tickets. You are NOT one of the workers; you manage them. "
    "WORKFLOW: Decompose a goal into worker-sized tasks and assign each by "
    "queuing it: ./fleet add <agent> \"task\". Monitor progress with ./fleet "
    "status and ./orch peek <agent>; unstick or answer a worker with ./orch "
    "send <agent> \"message\". When you finish coordinating a goal, report a "
    "concise summary. "
    "OWNERSHIP DISCIPLINE (this overrides the procedural workflow): "
    "(1) Do not accept 'blocked by X', 'sample too small', 'not enough info', "
    "'no edge yet', or any other incomplete-input verdict as a terminal answer. "
    "If a worker returns null because of a missing input, your next move is to "
    "queue the follow-up that produces that input — extend the data window, "
    "backfill the feed, gather the missing context, broaden the search — and "
    "re-run. A worker's 'I can't' is your 'what do I queue to make this "
    "answerable'. "
    "(2) Per goal, wave 1 ships >= 3 candidates or alternatives in parallel, "
    "not just 1-2. Reserve the narrow top-pick framing for wave 2, once wave 1 "
    "returns evidence. A single null cannot collapse a goal that started with "
    "3+ tries. "
    "(3) Keep a running scoreboard at briefs/<P>-pm-scoreboard.md tracking "
    "every attempt: what was tried, on what input/sample, what the verdict "
    "was, and the status (live / forward-walk / dropped / "
    "needs-more-input). Update it after every worker turn. "
    "(4) When workers return artifacts, you read the actual numbers/outputs "
    "and decide — do not just relay the worker's verdict. If a worker says "
    "'no edge / no fit / failed' but the numbers look promising on a slice, "
    "push for the slice; if a worker says 'shipped' but the artifact is "
    "partial, queue the gap. "
    "WORKING DIRECTORY DISCIPLINE: The project '<P>' lives at '<P_DIR>'. ALL "
    "project artifacts — code, briefs, scoreboards, decisions, plans, test "
    "outputs, dispatched-task files — go INSIDE '<P_DIR>'. Do not create or "
    "write project files outside '<P_DIR>'; the orchestrator dir you run in "
    "exists only to host the ./fleet and ./orch CLIs. When you queue work to "
    "your workers, frame every path relative to '<P_DIR>' (their working dir), "
    "never an absolute path outside it. Same rule binds your workers: they "
    "operate strictly inside their assigned project_dir. Override only when "
    "the human explicitly tells you to reach outside '<P_DIR>'. "
    "AUTONOMY MANDATE (this overrides every skill, plan-mode, or 'pause for "
    "user approval' workflow you might invoke): You DRIVE the project from "
    "goal to delivery. Do NOT call AskUserQuestion. Do NOT call EnterPlanMode "
    "or ExitPlanMode. Do NOT pause for spec review, plan sign-off, scope "
    "confirmation, or 'should I continue?' check-ins. If you invoke a "
    "skill (brainstorming, writing-plans, etc.) and it tells you to pause "
    "for the human, SKIP that pause and proceed with your best judgment. "
    "Never end a turn with a question to the user — end with the next "
    "action you took (a queued task, a written artifact, a decision). When "
    "one phase finishes, immediately queue the next phase or take the next "
    "action yourself; the only time you stop is when the whole goal is "
    "delivered or when an external dependency genuinely blocks you (in which "
    "case write the blocker to your scoreboard and queue around it). "
    "GUARDRAILS: Only ever manage agents in your own project '<P>'. NEVER run "
    "git, NEVER run ./fleet up, ./fleet down, or ./orch stop --all, and NEVER "
    "stop, restart, or manage agents outside '<P>' or manage yourself."
)


def _pm_name(project: str) -> str:
    """The agent name of a project's project-manager: '<project>-pm'."""
    return f"{project}-pm"


def _pm_role(project: str) -> str:
    """The manager role string for `project` — substitutes '<P>' and '<P_DIR>'.

    '<P_DIR>' resolves to the project's path from fleet.json's top-level
    ``projects`` list. When the project is missing (e.g. a hand-edited PM with
    no matching project entry) we fall back to a clearly-marked placeholder so
    the PM can see that the path was never configured rather than silently
    receiving an empty string.
    """
    path = ""
    try:
        for p in load_projects():
            if p["name"] == project:
                path = p.get("path") or ""
                break
    except SystemExit:
        # load_projects calls orch._die when CONFIG is missing — during tests
        # the config can be temporarily absent. Fall back to the placeholder.
        path = ""
    rendered = _PM_ROLE_TEMPLATE.replace("<P>", project)
    return rendered.replace(
        "<P_DIR>", path or f"<no path configured for project {project!r}>")


def _pm_agent(project: str) -> dict:
    """A fresh PM agent dict for `project` (project_dir is the orchestrator dir)."""
    return {
        "name": _pm_name(project),
        "role": _pm_role(project),
        "project_dir": str(HERE),
        "manager_of": project,
        "project": "",
    }


def ensure_project_managers() -> list[str]:
    """Backfill a project-manager agent for every project lacking one.

    Idempotent: a second call adds nothing and returns []. Persists fleet.json
    via _write_config only when at least one PM was added. A project is skipped if
    it already has a manager (some agent with manager_of == project) or if its
    _pm_name already exists as an agent (don't crash on a name clash). Returns the
    PM names created.
    """
    with _CONFIG_LOCK:
        data = json.loads(CONFIG.read_text(encoding="utf-8"))
        agents = data.setdefault("agents", [])
        managed = {a.get("manager_of") for a in agents if a.get("manager_of")}
        names = {a.get("name") for a in agents}
        created: list[str] = []
        for p in data.get("projects", []):
            name = p.get("name")
            if not name or name in managed or _pm_name(name) in names:
                continue
            agents.append(_pm_agent(name))
            names.add(_pm_name(name))
            created.append(_pm_name(name))
        if created:
            _write_config(data)
        return created


def create_project(name: str, path: str = "", description: str = "", dashboard_url: str = "") -> None:
    """Add a project to fleet.json (atomic) and refresh AGENTS.

    Raises ValueError("invalid name") / ("project exists") so the handler can
    map them to 400s. `dashboard_url` is stored only when it is an http(s) URL
    (see _normalize_dashboard_url), else "".
    """
    if not orch.NAME_RE.match(name or ""):
        raise ValueError("invalid name")
    with _CONFIG_LOCK:
        data = json.loads(CONFIG.read_text(encoding="utf-8"))
        if any(p.get("name") == name for p in data.get("projects", [])):
            raise ValueError("project exists")
        data.setdefault("projects", []).append({
            "name": name,
            "path": str(Path(path).expanduser()) if path else "",
            "description": description or "",
            "dashboard_url": _normalize_dashboard_url(dashboard_url),
        })
        if not any(a.get("name") == _pm_name(name) for a in data.get("agents", [])):
            data.setdefault("agents", []).append(_pm_agent(name))
        _write_config(data)


def delete_project(name: str) -> bool:
    """Remove a project and ungroup (not delete) its member agents.

    Returns True if removed, False if no such project (the handler reports the
    latter as {"ok": false}, not an error).
    """
    with _CONFIG_LOCK:
        data = json.loads(CONFIG.read_text(encoding="utf-8"))
        projects = data.get("projects", [])
        if not any(p.get("name") == name for p in projects):
            return False
        data["projects"] = [p for p in projects if p.get("name") != name]
        data["agents"] = [a for a in data.get("agents", []) if a.get("manager_of") != name]
        for a in data["agents"]:
            if a.get("project") == name:
                a["project"] = ""
        _write_config(data)
        return True


def create_agent(name: str, role: str, project_dir: str, project: str = "") -> None:
    """Append a new agent to fleet.json (atomic) and refresh AGENTS.

    project_dir is resolved exactly like load_config does. Raises ValueError with
    the contract messages ("invalid name", "agent exists", "not a directory:
    <path>", "unknown project") so the handler can map them to 400s.
    """
    if not orch.NAME_RE.match(name or ""):
        raise ValueError("invalid name")
    p = Path(project_dir or "").expanduser()
    if not project_dir or not p.is_dir():
        raise ValueError("not a directory: " + (project_dir or ""))
    with _CONFIG_LOCK:
        data = json.loads(CONFIG.read_text(encoding="utf-8"))
        if any(a.get("name") == name for a in data.get("agents", [])):
            raise ValueError("agent exists")
        if project and project not in {pr["name"] for pr in load_projects(CONFIG)}:
            raise ValueError("unknown project")
        data.setdefault("agents", []).append({
            "name": name,
            "role": role or "",
            "project_dir": str(p.resolve()),
            "project": project or "",
        })
        _write_config(data)


def set_agent_project(name: str, project: str) -> None:
    """Move an agent into a project (project="" ungroups) and refresh AGENTS.

    Raises ValueError("unknown agent") / ("unknown project") for 400 mapping.
    Grouping does not touch project_dir, so no restart is needed.
    """
    with _CONFIG_LOCK:
        data = json.loads(CONFIG.read_text(encoding="utf-8"))
        agent = next((a for a in data.get("agents", []) if a.get("name") == name), None)
        if agent is None:
            raise ValueError("unknown agent")
        if project and project not in {pr["name"] for pr in load_projects(CONFIG)}:
            raise ValueError("unknown project")
        agent["project"] = project or ""
        _write_config(data)


def rename_project(name: str, new_name: str) -> None:
    """Rename a project and re-point its member agents. Atomic; refreshes AGENTS.

    Raises ValueError("invalid name") / ("unknown project") / ("project exists")
    for 400 mapping. A no-op rename (new_name == name) is allowed as long as
    new_name is valid and the project exists.
    """
    if not orch.NAME_RE.match(new_name or ""):
        raise ValueError("invalid name")
    with _CONFIG_LOCK:
        data = json.loads(CONFIG.read_text(encoding="utf-8"))
        projects = data.get("projects", [])
        target = next((p for p in projects if p.get("name") == name), None)
        if target is None:
            raise ValueError("unknown project")
        if new_name != name and any(p.get("name") == new_name for p in projects):
            raise ValueError("project exists")
        target["name"] = new_name
        for a in data.get("agents", []):
            if a.get("project") == name:
                a["project"] = new_name
            if a.get("manager_of") == name:
                a["name"] = _pm_name(new_name)
                a["manager_of"] = new_name
                a["role"] = _pm_role(new_name)
        _write_config(data)


def update_project(name: str, path: str = "", description: str = "", dashboard_url: str = "") -> None:
    """Edit a project's path/description/dashboard_url in fleet.json. Atomic; refreshes AGENTS.

    Raises ValueError("unknown project") if `name` is not a project. `path` is
    stored as str(Path(path).expanduser()) when non-empty else "" (matching
    create_project); an empty `description` clears it. `dashboard_url` is stored
    only when it is an http(s) URL (see _normalize_dashboard_url), else "" — so
    passing an empty/junk value clears it. The project name is not changed here
    (use rename_project for that).
    """
    with _CONFIG_LOCK:
        data = json.loads(CONFIG.read_text(encoding="utf-8"))
        target = next((p for p in data.get("projects", []) if p.get("name") == name), None)
        if target is None:
            raise ValueError("unknown project")
        target["path"] = str(Path(path).expanduser()) if path else ""
        target["description"] = description or ""
        target["dashboard_url"] = _normalize_dashboard_url(dashboard_url)
        _write_config(data)


# --------------------------------------------------------------------------- #
# connectors (off-platform escalation channels)
# --------------------------------------------------------------------------- #
def load_connectors(path: Path = CONFIG) -> dict:
    """Return the ``connectors`` block from fleet.json, backfilled with defaults.

    Missing block ⇒ defaults. Never raises; a malformed connectors block reads
    as defaults so a corrupt save can't break the dashboard.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return connectors.merge_with_defaults(None)
    return connectors.merge_with_defaults(data.get("connectors"))


def load_auth(path: Path | None = None) -> dict | None:
    """Return the auth block from fleet.json (validated), or None when off.

    Reads through the same atomic-replace path the rest of fleet.json uses;
    held under _CONFIG_LOCK so a concurrent writer can't tear the read. A
    missing or malformed block fails closed (no access): ``None`` means
    "auth is disabled" (loopback dev mode) so the dashboard stays usable on
    a fresh checkout.

    ``path`` defaults to the **module-level** CONFIG so tests can
    monkeypatch ``fleet.CONFIG`` to a temp file and have it take effect.
    A Python default-arg ``CONFIG`` would have been captured at definition
    time and missed the monkeypatch.
    """
    target = path if path is not None else CONFIG
    with _CONFIG_LOCK:
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
    return auth.load_auth_config(data)


def update_connector(name: str, cfg: dict) -> dict:
    """Persist one connector's settings under _CONFIG_LOCK.

    ``cfg`` may be partial (only the fields the user touched); existing fields
    are preserved by merging with the current stored config first, then
    validated. Returns the persisted (unmasked) connector config.
    Raises ``ValueError`` from validate_connector on bad input.
    """
    with _CONFIG_LOCK:
        data = json.loads(CONFIG.read_text(encoding="utf-8"))
        block = connectors.merge_with_defaults(data.get("connectors"))
        merged = dict(block.get(name) or {})
        merged.update(cfg or {})
        block[name] = connectors.validate_connector(name, merged)
        data["connectors"] = block
        _write_config(data)
        return block[name]


# --------------------------------------------------------------------------- #
# documents
# --------------------------------------------------------------------------- #
def _safe_filename(filename: str) -> str:
    """Reduce to a bare filename; reject empty / relative / traversal names."""
    base = (filename or "").replace("\\", "/").split("/")[-1].strip()
    if base in ("", ".", "..") or "\x00" in base:
        raise ValueError("invalid filename")
    return base


def _docs_dir(scope: str, name: str | None = None) -> Path:
    """Resolve (and create) the folder for a scope. Validates scope/agent."""
    if scope == "shared":
        d = DOCS_ROOT / SHARED
    elif scope == "agent":
        if name not in {a["name"] for a in AGENTS}:
            raise ValueError("unknown agent")
        d = DOCS_ROOT / name
    else:
        raise ValueError("invalid scope")
    d.mkdir(parents=True, exist_ok=True)
    return d


def _doc_meta(p: Path) -> dict:
    st = p.stat()
    return {
        "name": p.name,
        "size": st.st_size,
        "modified": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(),
        "path": str(p),
    }


def list_docs(scope: str, name: str | None = None) -> list[dict]:
    d = _docs_dir(scope, name)
    return [_doc_meta(p) for p in sorted(d.iterdir()) if p.is_file()]


def save_doc(scope: str, name: str | None, filename: str, data: bytes) -> dict:
    if len(data) > MAX_DOC_BYTES:
        raise ValueError("file too large")
    fn = _safe_filename(filename)
    p = _docs_dir(scope, name) / fn
    p.write_bytes(data)
    return _doc_meta(p)


def delete_doc(scope: str, name: str | None, filename: str) -> bool:
    fn = _safe_filename(filename)
    p = _docs_dir(scope, name) / fn
    if p.is_file():
        p.unlink()
        return True
    return False


# --------------------------------------------------------------------------- #
# agent terminals
# --------------------------------------------------------------------------- #
def _role_file(agent: dict) -> str:
    """Write the agent's role to a file (read by the launcher's --append-system-prompt)."""
    ROLES_DIR.mkdir(parents=True, exist_ok=True)
    f = ROLES_DIR / f"{agent['name']}.txt"
    f.write_text(" ".join(agent["role"].split()))
    return str(f)


def ensure_agent(agent: dict) -> str:
    """Make sure the agent's terminal is up. Returns a short status string."""
    name = agent["name"]
    if orch._window_exists(name):
        return "already running"
    proj = Path(agent["project_dir"])
    if not proj.is_dir():
        return f"SKIPPED (project_dir missing: {proj})"

    orch._backend.set_scrollback(5000)
    ready, err = orch._backend.spawn(name, str(proj), _role_file(agent))
    if err:
        return f"FAILED ({err})"
    return "ready" if ready else "started (not confirmed ready)"


# Result state vocabulary for reattach_on_startup():
#   "reattached" — a live worker session was matched to this agent (no respawn).
#   "spawned"   — no live session existed, a fresh terminal was started.
#   "offline"   — neither a live session nor a successful spawn (e.g. project_dir
#                 missing, backend spawn failed). The agent is NOT removed from
#                 fleet.json; the dispatcher's keep-alive loop keeps retrying.
RECOVERY_STATES = ("reattached", "spawned", "offline")


def _reconcile_inflight_task(agent_name: str, task: dict) -> str | None:
    """Roll an in-flight task forward from the worker_status file if the worker
    finished (or errored) while the dispatcher was down.

    Returns the new task status if a transition happened, else None. Holds
    _LOCK so it doesn't race the dispatcher's own task-store writes.
    """
    st = worker_status.read_status(agent_name)
    if not st:
        return None
    if st.get("task_id") and st.get("task_id") != task.get("id"):
        return None       # status file is for a different task; leave it alone
    s = st.get("state")
    if s not in ("done", "error"):
        return None
    new_status = "done" if s == "done" else "failed"
    note = f"\n[reconciled on fleet up from status file: {s}]"
    with _LOCK:
        d = _load_tasks()
        for t in d["tasks"]:
            if t["id"] == task["id"] and t["status"] == "running":
                t.update(status=new_status, finished_at=_now(),
                         log=(t.get("log") or "") + note)
        _save_tasks(d)
    return new_status


def reattach_on_startup(agents: list[dict]) -> list[dict]:
    """Reconcile fleet.json against live worker sessions at ``./fleet up``.

    Enumerate live tmux / ConPTY sessions in the ``corch`` namespace
    (``orch._windows()`` -> ``backend.list_workers``). For each agent in
    ``agents``:

    - If a live session matches the agent's name, mark it ``reattached`` and
      reconcile any in-flight task against its worker_status file (so a task
      that finished while the dispatcher was down is rolled forward to
      ``done`` / ``failed`` instead of being stuck as ``running``).
    - Otherwise spawn a fresh terminal in the agent's ``project_dir``. A
      successful spawn is ``spawned``; a project_dir-missing / spawn-failed
      agent is ``offline``. **Agents are NEVER removed from fleet.json** —
      the dispatcher's keep-alive loop keeps retrying them on each tick.

    Returns a list of report dicts in the same order as ``agents``:
        {"name": str, "state": "reattached"|"spawned"|"offline",
         "note": short str, "task_id": optional task id rolled forward}
    """
    try:
        live = set(orch._windows())
    except Exception:  # backend may transiently fail to list — degrade safely
        live = set()
    in_flight = {t["agent"]: t for t in _load_tasks()["tasks"]
                 if t["status"] == "running"}

    report: list[dict] = []
    for ag in agents:
        name = ag["name"]
        note = ensure_agent(ag)   # reattaches via _window_exists, else spawns
        entry: dict = {"name": name, "note": note}

        if note == "already running":
            # ensure_agent's own _window_exists check caught a live worker —
            # treat as reattached even if the broader live-listing missed it
            # (some backends list lazily). Use 'live' only as a sanity probe.
            entry["state"] = "reattached"
            entry["live_listed"] = name in live
            cur = in_flight.get(name)
            if cur is not None:
                rolled = _reconcile_inflight_task(name, cur)
                if rolled is not None:
                    entry["task_id"] = cur["id"]
                    entry["task_rolled_to"] = rolled
        elif note.startswith(("SKIPPED", "FAILED")):
            entry["state"] = "offline"
        else:   # "ready" / "started (not confirmed ready)"
            entry["state"] = "spawned"
        report.append(entry)
    return report


def agent_activity(name: str) -> str:
    """'offline' | 'busy' | 'idle'.

    Prefers the worker_status file (written by agent_host every ~15s with the
    BUSY/READY-marker derived state) when it is present *and* fresh. Falls
    back to the legacy tmux/ConPTY capture + BUSY_MARKERS heuristic when the
    file is missing or stale, so workers spawned before this protocol was
    rolled out keep being detected correctly.
    """
    st = worker_status.read_status(name)
    if st is not None and worker_status.heartbeat_fresh(st):
        state = st.get("state")
        if state == "running":
            return "busy"
        if state in ("done", "starting"):
            # 'starting' = host booting, no claude turn yet; 'done' = idle
            # between/after turns. Both map to "ready to receive a task".
            return "idle"
        # state == "error" -> fall through to legacy detection.
    if not orch._window_exists(name):
        return "offline"
    low = orch._capture(name, 80).lower()
    return "busy" if any(m in low for m in orch.BUSY_MARKERS) else "idle"


# Heuristic markers that the agent has paused to ask the human something and is
# waiting on a numbered choice or a yes/no — display-only, never used by the
# dispatcher (which keys off busy/idle alone).
ATTENTION_MARKERS = (
    "do you want", "do you trust", "❯ 1.", "1. yes", "2. no",
    "would you like", "(y/n)", "press enter to", "waiting for your",
    # Explicit signal an agent can print to opt into the attention bar without
    # waiting for the heuristic markers above to coincidentally match. Mirrors
    # the WORKER-DONE: protocol — agents are told they can use this in roles
    # to surface a question or proposal to the dashboard. Lower-case because
    # ``agent_attention`` lowercases the captured text before comparing.
    "needs-input:",
)


def agent_attention(name: str) -> bool:
    """True when an idle agent looks like it's blocked on a question for you."""
    if not orch._window_exists(name):
        return False
    low = orch._capture(name, 40).lower()
    if any(m in low for m in orch.BUSY_MARKERS):
        return False
    return any(m in low for m in ATTENTION_MARKERS)


# Per-agent record of when each agent's captured terminal text last changed.
# The stuck-worker check uses this as a fallback signal for legacy workers
# without the T-01 heartbeat protocol. Updated by the dispatcher whenever it
# captures, so tests can prime it directly. Process-local: no cross-restart
# persistence needed because restart re-bootstraps from the heartbeats.
_TERMINAL_ACTIVITY: dict[str, tuple[int, datetime]] = {}


def _record_terminal_activity(name: str, capture: str) -> None:
    """Stamp ``name``'s last terminal-change time when ``capture`` differs from before.

    Cheap content hash so we only refresh the timestamp when the captured text
    actually changed — otherwise an unchanged screen would keep moving the
    timestamp forward and mask a real freeze.
    """
    h = hash(capture)
    prev = _TERMINAL_ACTIVITY.get(name)
    if prev is None or prev[0] != h:
        _TERMINAL_ACTIVITY[name] = (h, datetime.now(timezone.utc))


def _minutes_label(seconds: float) -> str:
    """Round ``seconds`` to whole minutes (floor 1) and format as ``Xm``."""
    return f"{max(1, int(round(seconds / 60.0)))}m"


def agent_stuck_reason(name: str, stuck_after_secs: float,
                       now: datetime | None = None,
                       *, in_flight: bool = False) -> str | None:
    """Return the reason ``name`` looks stuck, or ``None`` if it doesn't.

    Primary signal: the T-01 heartbeat file. If
    :func:`worker_status.read_status` returns a status whose ``state`` is
    ``running`` / ``starting`` and whose ``last_beat`` is older than
    ``stuck_after_secs``, this returns ``"no heartbeat for Xm"`` (minutes
    rounded).

    Fallback for legacy workers without the heartbeat protocol: when the
    dispatcher reports an in-flight task (``in_flight=True``) and the captured
    terminal hasn't changed within the window, this returns ``"no terminal
    output for Xm"``. Idle agents with no work and no status file are
    intentionally never flagged so a freshly-spawned, never-tasked agent
    doesn't spuriously light up the attention bar.
    """
    now = now or datetime.now(timezone.utc)

    status = worker_status.read_status(name)
    if status:
        state = status.get("state") or ""
        if state in ("running", "starting"):
            ts = status.get("last_beat") or ""
            try:
                t = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            except ValueError:
                t = None
            if t is not None:
                age = (now - t).total_seconds()
                if age > stuck_after_secs:
                    return f"no heartbeat for {_minutes_label(age)}"
        # Status file present, but state isn't running/starting (e.g. done /
        # error). The heartbeat protocol gave us a definitive answer; don't
        # fall through to the legacy terminal-silence fallback.
        return None

    if in_flight:
        seen = _TERMINAL_ACTIVITY.get(name)
        if seen is not None:
            age = (now - seen[1]).total_seconds()
            if age > stuck_after_secs:
                return f"no terminal output for {_minutes_label(age)}"
    return None



def send_message(name: str, text: str) -> bool:
    """Type a message straight into a live agent's prompt. Returns success."""
    if not orch._window_exists(name):
        return False
    orch._send_text(name, text)
    return True


def agent_ready(name: str) -> bool:
    """True only when the TUI input is actually ready (not a bare shell, not busy)."""
    if not orch._window_exists(name):
        return False
    low = orch._capture(name, 80).lower()
    if any(m in low for m in orch.BUSY_MARKERS):
        return False
    return any(m in low for m in orch.READY_MARKERS)


def _kickoff(task: dict) -> str:
    branch = task.get("branch")
    if branch:
        # Workers are normally forbidden from running git (see CLAUDE.md). T-03
        # carves out a single exception: their own task branch. The instruction
        # is *prepended* so it lands ahead of the existing autonomy block and
        # is impossible to miss when claude scans the kickoff.
        git_block = (
            f"Git: your work goes on branch '{branch}'. You are allowed to run "
            f"git ONLY on this branch (e.g. 'git add', 'git commit'). Commit at "
            f"meaningful milestones and one final commit when you finish. Do "
            f"NOT switch branches, do NOT push, do NOT touch any other branch. "
        )
    else:
        git_block = ""
    return (
        "DISREGARD any text already in your input box from earlier turns — "
        "it is stale and was never submitted. "
        f"{git_block}"
        f"New task (id {task['id']}): {task['description']}  "
        "WORKING DIRECTORY DISCIPLINE: You operate strictly inside your "
        "agent's project working directory (the cwd you are already in). "
        "Read, write, and run commands only inside that dir. Do not edit "
        "files outside it, do not cd elsewhere, and do not reference "
        "absolute paths outside it. Override only when this task text "
        "explicitly tells you to. "
        "AUTONOMY MANDATE (overrides any skill, plan-mode, or workflow that would "
        "pause for human input): Do NOT call AskUserQuestion. Do NOT call "
        "ExitPlanMode or EnterPlanMode. Do NOT pause to ask the user for "
        "review, approval, plan sign-off, scope confirmation, or next-step "
        "direction. Do NOT end your turn with a question to the user. If a "
        "skill you invoke says 'pause for approval' or 'check with the user', "
        "SKIP that step and proceed with your best judgment. When you finish "
        "this task, give a one-line summary of what you changed and STOP — do "
        "not ask 'what next'."
    )


def _record_head_sha(task: dict, agent_obj: dict | None) -> None:
    """Best-effort: snapshot the agent's HEAD into ``task['head_sha']``.

    Called when a task transitions to a terminal state (done/failed/canceled)
    so the task record carries a base_sha..head_sha diff range. Silently
    no-ops when the task has no branch, the agent is unknown, or git can't
    be talked to — head_sha stays None and the dashboard skips the diff link.
    """
    if not task.get("branch"):
        return
    if agent_obj is None:
        return
    project_dir = agent_obj.get("project_dir")
    if not project_dir:
        return
    sha = git_checkpoint.head_sha(project_dir)
    if sha:
        task["head_sha"] = sha


def _load_notifications() -> dict:
    if NOTIFICATIONS_FILE.exists():
        try:
            return json.loads(NOTIFICATIONS_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {"notifications": []}


def _save_notifications(d: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = NOTIFICATIONS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, indent=2), encoding="utf-8")
    tmp.replace(NOTIFICATIONS_FILE)


def _append_notification(entry: dict) -> None:
    d = _load_notifications()
    d["notifications"].append(entry)
    _save_notifications(d)


def _agent_project(agent_name: str) -> str:
    """The project key an agent belongs to (empty string when ungrouped)."""
    for a in AGENTS:
        if a.get("name") == agent_name:
            return a.get("project") or ""
    return ""


def _project_pm(project: str) -> str | None:
    """Return the PM agent name for ``project``, or None if no PM exists."""
    if not project:
        return None
    for a in AGENTS:
        if a.get("manager_of") == project:
            return a["name"]
    return None


def _escalate_task(task: dict, agent_name: str) -> None:
    """Final-failure handler: route to the project PM or to the attention bar.

    Always writes a durable entry to ``NOTIFICATIONS_FILE`` so an external
    reader can see every escalation. When a PM exists for the agent's project
    a best-effort ``./fleet send`` to the PM is also attempted; ``notified_pm``
    is set to the PM name. When no PM exists ``escalated`` flips to True so
    build_state() can light up the attention bar for the agent.
    """
    project = _agent_project(agent_name)
    pm = _project_pm(project)
    entry = {
        "task_id": task.get("id", ""),
        "agent": agent_name,
        "project": project,
        "pm": pm or "",
        "attempts": int(task.get("attempts", 0)),
        "reason": task.get("last_error", ""),
        "created_at": _now(),
    }
    try:
        _append_notification(entry)
    except OSError:
        pass
    if pm:
        # Best-effort handoff: send_message returns False when the PM terminal
        # isn't running yet. ``notified_pm`` records the intent regardless so
        # the dispatcher / dashboard don't double-escalate on the next tick.
        try:
            send_message(
                pm,
                f"Task {task.get('id', '')} (agent {agent_name}) failed "
                f"after {task.get('attempts', 0)} attempts: "
                f"{task.get('last_error', '')}",
            )
        except Exception:
            pass
        task["notified_pm"] = pm
    else:
        task["escalated"] = True

    # Off-platform fan-out: ping every enabled connector. Errors are caught
    # and recorded on the task so a flaky connector can never mask the
    # original failure escalation.
    _fanout_connectors(task, agent_name)


def _fanout_connectors(task: dict, agent_name: str) -> dict:
    """Send a short escalation note via every enabled connector.

    Returns a {connector_name: result_dict} map of attempts. Each result has
    the connectors.py shape ({ok, error, status, response}); a connector that
    isn't enabled is simply skipped. Send errors are logged into the task's
    ``connector_results`` field for the dashboard to surface — they never
    re-raise.
    """
    msg = (f"[orchestrator] task {task.get('id', '')} failed after "
           f"{task.get('attempts', 0)} retries: "
           f"{task.get('last_error', '')}")
    try:
        block = load_connectors(CONFIG)
    except Exception:
        return {}
    results: dict = {}
    for name in connectors.SUPPORTED:
        cfg = (block.get(name) or {})
        if not cfg.get("enabled"):
            continue
        try:
            results[name] = connectors.send_via_connector(name, msg, block)
        except Exception as e:  # never mask the task failure
            results[name] = {"ok": False, "error": f"send raised: {e!r}"}
    if results:
        task["connector_results"] = {
            n: {"ok": bool(r.get("ok")), "error": r.get("error")}
            for n, r in results.items()
        }
    return results


def _record_failure(task: dict, agent_name: str, reason: str,
                    rate_limit_cfg: dict, agent_obj: dict | None = None
                    ) -> bool:
    """Centralized retry / escalate dispatch for a failed task.

    Increments ``attempts``, records ``last_error``, then either:

    * **retries** by flipping the task to ``status="retrying"`` with a
      ``retry_at`` derived from rate_governor.compute_backoff_seconds — the
      existing tick-2.5 re-queue step picks it up and dispatches normally;
    * or **escalates** when ``attempts > max_retries``: marks the task
      ``failed``, snapshots head_sha for the diff range, and routes the
      failure to the project PM (or the attention bar — see _escalate_task).

    Returns True iff the task escalated (final failure).
    """
    task["attempts"] = int(task.get("attempts", 0)) + 1
    task["last_error"] = reason
    max_retries = int(rate_limit_cfg.get("max_retries", 2))
    if task["attempts"] <= max_retries:
        delay = rate_governor.compute_backoff_seconds(
            task["attempts"], rate_limit_cfg)
        retry_at = (datetime.now(timezone.utc)
                    + timedelta(seconds=delay)
                    ).strftime("%Y-%m-%dT%H:%M:%SZ")
        task.update(
            status="retrying", retry_at=retry_at,
            log=(task.get("log") or "")
                + f"\n[retry #{task['attempts']} in {int(delay)}s: {reason}]",
        )
        return False
    # Exhausted: terminal failure.
    task.update(
        status="failed", finished_at=_now(),
        log=(task.get("log") or "")
            + f"\nfailed after {task['attempts']} attempts: {reason}",
    )
    _record_head_sha(task, agent_obj)
    _escalate_task(task, agent_name)
    return True


def _status_says_task_done(name: str, task_id: str) -> bool:
    """True iff the worker-status file declares this task complete.

    The agent_host flips state to ``done`` on a busy->idle transition; when
    the recorded ``task_id`` matches the task we are advancing, that is a
    stronger and earlier signal than CONFIRM_IDLE consecutive tmux captures.
    Returns False if the file is missing, stale, or names a different task —
    in which case the caller falls back to the legacy idle-debounce path.
    """
    st = worker_status.read_status(name)
    if st is None or not worker_status.heartbeat_fresh(st):
        return False
    if st.get("state") != "done":
        return False
    # ``task_id`` may have been cleared by a prior tick; treat the cleared
    # case as a no-op rather than a definitive "done".
    return bool(st.get("task_id")) and st.get("task_id") == task_id


# --------------------------------------------------------------------------- #
# task store
# --------------------------------------------------------------------------- #
def _now() -> str:
    return orch._now()


def _age(iso: str | None) -> float:
    if not iso:
        return 1e9
    try:
        t = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - t).total_seconds()
    except ValueError:
        return 1e9


def _load_tasks() -> dict:
    if TASKS_FILE.exists():
        try:
            return json.loads(TASKS_FILE.read_text())
        except json.JSONDecodeError:
            pass
    return {"next_id": 1, "tasks": []}


def _save_tasks(d: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = TASKS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, indent=2))
    tmp.replace(TASKS_FILE)


def _tail(text: str, n: int = 160) -> str:
    return "\n".join(text.splitlines()[-n:])


def _write_task_log(tid: str, text: str) -> None:
    TASK_LOGS.mkdir(parents=True, exist_ok=True)
    # utf-8: captured TUI output has box-drawing glyphs that the Windows default
    # (cp1252) can't encode, which crashed the dispatcher's done-marking path.
    (TASK_LOGS / f"{tid}.log").write_text(text, encoding="utf-8")


def add_task(agent: str, description: str) -> str:
    with _LOCK:
        d = _load_tasks()
        tid = f"t-{d['next_id']:04d}"
        d["next_id"] += 1
        d["tasks"].append({
            "id": tid, "agent": agent, "description": description.strip(),
            "status": "pending", "created_at": _now(),
            "started_at": None, "finished_at": None, "saw_busy": False,
            "needs_attention": False, "log": "",
            # Git checkpoint fields — filled in by the dispatcher on dispatch
            # (branch, base_sha) and on terminal-state transitions (head_sha).
            # null when the agent's project_dir is not a git repo.
            "branch": None, "base_sha": None, "head_sha": None,
            # T-06 retry bookkeeping. ``attempts`` counts failed runs (not
            # the initial dispatch); ``last_error`` is the short reason of
            # the most recent failure. ``escalated`` flips to True after
            # max_retries is exhausted and no PM picked the task up.
            "attempts": 0, "last_error": "", "escalated": False,
            "notified_pm": "",
        })
        _save_tasks(d)
        return tid


def cancel_task(tid: str) -> bool:
    with _LOCK:
        d = _load_tasks()
        ok = False
        for t in d["tasks"]:
            if t["id"] == tid and t["status"] == "pending":
                t.update(status="canceled", finished_at=_now())
                ok = True
        _save_tasks(d)
        return ok


def add_group_tasks(project: str, description: str) -> list[str]:
    """Queue `description` as a task for every member agent of `project`.

    Raises ValueError("unknown project") if the project doesn't exist, or
    ("no agents in project") if it has no members. Members are read fresh from
    the config (load_config) and each gets its own task via add_task(); returns
    the new task ids in member (FIFO) order.
    """
    if project not in {p["name"] for p in load_projects(CONFIG)}:
        raise ValueError("unknown project")
    members = [a["name"] for a in load_config(CONFIG) if a.get("project") == project]
    if not members:
        raise ValueError("no agents in project")
    return [add_task(member, description) for member in members]


# --------------------------------------------------------------------------- #
# dispatcher
# --------------------------------------------------------------------------- #
class Dispatcher(threading.Thread):
    def __init__(self, agents: list[dict]):
        super().__init__(daemon=True)
        self.agents = {a["name"]: a for a in agents}
        self.stop = threading.Event()
        # Rate-limit governor state. ``last_spawn`` is a monotonic clock value
        # (or None when no task has been dispatched yet) used to enforce the
        # spawn_stagger; the cfg block is reloaded each tick from fleet.json
        # so live edits to ``rate_limit`` take effect without a restart.
        self.rate_limit_cfg = rate_governor.load_rate_limit(CONFIG)
        self.last_spawn: float | None = None

    def run(self) -> None:
        while not self.stop.is_set():
            try:
                self.tick()
            except Exception as e:  # keep the loop alive
                print(f"[dispatch] error: {e}", file=sys.stderr, flush=True)
            self.stop.wait(POLL)

    def _reload_agents(self) -> None:
        """Re-read fleet.json so edits (new/removed agents, rate_limit) take effect live.

        Updates both this dispatcher's view and the module-level AGENTS that the
        dashboard renders from. A malformed file mid-edit (load_config may even
        call sys.exit) must never kill the dispatcher, so swallow everything and
        keep the last good roster.
        """
        global AGENTS
        try:
            agents = load_config()
        except BaseException as e:  # incl. SystemExit from validation
            print(f"[dispatch] fleet.json reload skipped: {e}", file=sys.stderr)
            return
        self.agents = {a["name"]: a for a in agents}
        AGENTS = agents
        # Refresh rate-limit cfg from the same reload; the loader returns
        # defaults on any read/parse error so this can't kill the dispatcher.
        self.rate_limit_cfg = rate_governor.load_rate_limit(CONFIG)

    def tick(self) -> None:
        # 0) hot-reload fleet.json so agents added/removed while running show up
        self._reload_agents()

        # 1) keep terminals alive (outside the lock; may block on readiness).
        # Never respawn an agent that has a running task: a busy agent doing heavy
        # work can miss PINGs and look "gone", and respawning would displace its
        # working session and lose its progress. Only revive idle/dead agents.
        busy_with_task = {t["agent"] for t in _load_tasks()["tasks"]
                          if t["status"] == "running"}
        for a in self.agents.values():
            if a["name"] in busy_with_task:
                continue
            if not orch._window_exists(a["name"]):
                ensure_agent(a)

        with _LOCK:
            d = _load_tasks()
            changed = False

            # 2) advance running tasks
            for t in (x for x in d["tasks"] if x["status"] == "running"):
                name = t["agent"]

                # Per-task runtime watchdog: fail a task that has run past
                # TASK_TIMEOUT_SECS (a wedged agent stuck showing the busy marker,
                # or one parked on a question) so it stops head-of-line-blocking
                # its agent's queue; freeing the agent lets the next pending task
                # dispatch on this same tick. Guard a missing started_at so a task
                # mid-dispatch is never failed before it has a start time.
                started = t.get("started_at")
                if started is not None and _age(started) > TASK_TIMEOUT_SECS:
                    why = " (was waiting on your attention)" if t.get("needs_attention") else ""
                    _record_failure(
                        t, name,
                        f"exceeded max runtime ({TASK_TIMEOUT_SECS}s){why}",
                        self.rate_limit_cfg, self.agents.get(name),
                    )
                    changed = True
                    continue

                if not orch._window_exists(name):
                    # Require sustained absence: a busy agent under load can miss a
                    # PING; one missed read must not fail an in-flight task.
                    t["gone_seen"] = t.get("gone_seen", 0) + 1
                    changed = True
                    if t["gone_seen"] >= CONFIRM_GONE:
                        _record_failure(
                            t, name, "agent terminal closed",
                            self.rate_limit_cfg, self.agents.get(name),
                        )
                    continue
                t["gone_seen"] = 0
                # Worker-status file is authoritative when present and fresh:
                # the agent_host flips state to ``done`` on the busy->idle
                # transition, which lets us close the task without waiting for
                # CONFIRM_IDLE consecutive tmux captures (and without relying
                # on the worker printing the legacy WORKER-DONE: marker).
                if _status_says_task_done(name, t["id"]):
                    cap = orch._capture(name, 400)
                    _write_task_log(t["id"], cap)
                    t.update(status="done", finished_at=_now(), log=_tail(cap))
                    _record_head_sha(t, self.agents.get(name))
                    worker_status.write_status(
                        name, task_id="", progress_note="")
                    changed = True
                    continue
                # One capture per tick, reused for 429 detection and activity.
                cap_low = orch._capture(name, 80).lower()
                # Feed the stuck-worker fallback tracker so a legacy worker
                # without the T-01 heartbeat protocol still gets flagged when
                # its terminal goes silent for stuck_after_seconds.
                _record_terminal_activity(name, cap_low)
                # 429 / rate-limit takes priority over normal idle/busy logic:
                # mark the task ``retrying`` (NOT ``failed``), record a
                # backoff-derived ``retry_at``, and bail. We gate on saw_busy so
                # stale 429 text left in the buffer after a re-queue can't loop.
                if t.get("saw_busy") and rate_governor.detect_rate_limit(cap_low):
                    attempt = int(t.get("rate_retries", 0)) + 1
                    delay = rate_governor.compute_backoff_seconds(
                        attempt, self.rate_limit_cfg
                    )
                    retry_at = (datetime.now(timezone.utc)
                                + timedelta(seconds=delay)
                                ).strftime("%Y-%m-%dT%H:%M:%SZ")
                    t.update(
                        status="retrying",
                        rate_retries=attempt,
                        retry_at=retry_at,
                        log=(t.get("log") or "")
                            + f"\n[rate-limited; retry #{attempt} in {int(delay)}s at {retry_at}]",
                    )
                    changed = True
                    continue
                act = "busy" if any(m in cap_low for m in orch.BUSY_MARKERS) else "idle"
                if act == "busy":
                    # Working again: clear any prior idle / attention bookkeeping.
                    if not t.get("saw_busy") or t.get("idle_seen") or t.get("needs_attention"):
                        t["saw_busy"] = True
                        t["idle_seen"] = 0
                        t["needs_attention"] = False
                        changed = True
                    continue
                # agent looks idle:
                if t.get("saw_busy"):
                    # An agent paused on a question (trust/permission prompt or
                    # asking the human) also reads idle. Don't let that drift to
                    # "done" with a truncated log: flag it as needing attention
                    # and hold the task open (idle_seen reset) until it resumes.
                    if agent_attention(name):
                        if not t.get("needs_attention") or t.get("idle_seen"):
                            t["needs_attention"] = True
                            t["idle_seen"] = 0
                            changed = True
                        continue
                    if t.get("needs_attention"):   # resumed -> clear the flag
                        t["needs_attention"] = False
                        changed = True
                    # Require CONFIRM_IDLE consecutive idle reads before declaring
                    # done — a single dropped capture reads as idle and would end
                    # the task early with a truncated log.
                    t["idle_seen"] = t.get("idle_seen", 0) + 1
                    changed = True
                    if t["idle_seen"] >= CONFIRM_IDLE:
                        cap = orch._capture(name, 400)
                        _write_task_log(t["id"], cap)
                        t.update(status="done", finished_at=_now(), log=_tail(cap))
                        _record_head_sha(t, self.agents.get(name))
                        # Clear task_id/progress_note so the next ./fleet
                        # status read of the file doesn't still name a task
                        # we've just marked done.
                        worker_status.write_status(
                            name, task_id="", progress_note="")
                elif _age(t.get("started_at")) > DELIVER_GRACE:
                    # never started working -> the message likely never landed
                    if t.get("tries", 1) >= MAX_TRIES:
                        _record_failure(
                            t, name,
                            "agent never started the task (no response)",
                            self.rate_limit_cfg, self.agents.get(name),
                        )
                    else:
                        orch._send_text(name, _kickoff(t))
                        t.update(started_at=_now(), tries=t.get("tries", 1) + 1)
                    changed = True

            # 2.5) re-queue ``retrying`` tasks whose backoff window has elapsed.
            # A 429-marked task sleeps in ``retrying`` until now >= retry_at,
            # then transitions back to ``pending`` and is picked up by the
            # normal dispatch step below (still subject to max_concurrent and
            # stagger). rate_retries is preserved so successive 429s grow the
            # backoff per rate_governor.compute_backoff_seconds.
            for t in (x for x in d["tasks"] if x["status"] == "retrying"):
                retry_at = t.get("retry_at")
                if retry_at and _age(retry_at) >= 0:
                    t.update(status="pending", started_at=None, saw_busy=False,
                             idle_seen=0, gone_seen=0, retry_at=None,
                             needs_attention=False)
                    changed = True

            # 3) dispatch pending tasks to free, ready agents (FIFO), bounded by
            # the rate-limit governor: at most ``max_concurrent`` running tasks
            # fleet-wide, and at most one spawn per ``spawn_stagger_seconds``
            # window. self.last_spawn is None until the first ever dispatch so
            # a fresh dispatcher isn't blocked from its first spawn.
            busy = {t["agent"] for t in d["tasks"] if t["status"] == "running"}
            running_count = len(busy)
            cap = self.rate_limit_cfg["max_concurrent"]
            stagger = self.rate_limit_cfg["spawn_stagger_seconds"]
            now_mono = time.monotonic()
            for t in (x for x in d["tasks"] if x["status"] == "pending"):
                if running_count >= cap:
                    break
                name = t["agent"]
                if name in busy:
                    continue
                if name not in self.agents:
                    t.update(status="failed", finished_at=_now(),
                             log="no such agent in fleet.json")
                    changed = True
                    continue
                if not agent_ready(name):   # wait for a real, idle TUI prompt
                    continue
                if self.last_spawn is not None and (now_mono - self.last_spawn) < stagger:
                    # honor the stagger; the next eligible pending task waits a
                    # tick instead of bursting alongside this one.
                    break
                # Create the per-task branch *before* the kickoff is sent so
                # the worker lands on the right branch and so _kickoff() can
                # tell it the branch name. When project_dir isn't a git repo
                # both fields stay None and the kickoff omits the git block.
                # On a T-06 retry the branch already exists (create_task_branch
                # falls back to a plain checkout); we keep the *original*
                # base_sha so the diff range still spans the full task, not
                # just the retry's commits.
                agent_obj = self.agents.get(name) or {}
                branch, base_sha = git_checkpoint.create_task_branch(
                    agent_obj.get("project_dir", ""), t["id"])
                t["branch"] = branch
                if t.get("base_sha") is None:
                    t["base_sha"] = base_sha
                orch._send_text(name, _kickoff(t))
                t.update(status="running", started_at=_now(), saw_busy=False,
                         idle_seen=0, gone_seen=0, tries=1, needs_attention=False)
                # Stamp the task on the worker-status file so the next tick
                # (and ./fleet status) knows what this agent is working on,
                # independent of the legacy WORKER-DONE: marker path. Force
                # state back to "starting" so a stale "done" left over from the
                # agent_host's previous busy->idle transition can't race the
                # next-tick _status_says_task_done check and immediately close
                # the brand-new task before the embedded claude even paints
                # its first busy frame.
                worker_status.write_status(
                    name, state="starting", task_id=t["id"],
                    progress_note=t["description"][:80])
                busy.add(name)
                running_count += 1
                self.last_spawn = now_mono
                changed = True

            if changed:
                _save_tasks(d)


# --------------------------------------------------------------------------- #
# web dashboard
# --------------------------------------------------------------------------- #
def _escalation_reason(agent_name: str, tasks: list[dict]) -> str:
    """Return the attention-bar reason for an un-acked T-06 escalation, else ''.

    A task escalates (``escalated=True``) only when it failed terminally and
    no PM was available to pick it up. We surface the *newest* such task on
    the agent so an operator sees something actionable rather than a count.
    """
    candidates = [t for t in tasks
                  if t.get("agent") == agent_name and t.get("escalated")
                  and not t.get("notified_pm")]
    if not candidates:
        return ""
    # tasks are appended in dispatch order; the last one is the most recent.
    t = candidates[-1]
    reason = (t.get("last_error") or "").strip()
    return (f"task {t['id']} failed after {t.get('attempts', 0)} attempts"
            + (f": {reason[:60]}" if reason else ""))


def build_state() -> dict:
    with _LOCK:
        d = _load_tasks()
    running = {t["agent"]: t for t in d["tasks"] if t["status"] == "running"}
    # Re-read stuck_after_seconds from the rate_limit block each call so a live
    # edit takes effect without bouncing the dashboard. Loader returns
    # defaults if the file is missing / malformed.
    stuck_after = rate_governor.load_rate_limit(CONFIG)["stuck_after_seconds"]
    agents = []
    for a in AGENTS:
        cur = running.get(a["name"])
        act = agent_activity(a["name"])
        # Three independent reasons an agent needs the human's attention:
        #   * the T-05 stuck-worker check (stale heartbeat or terminal silence)
        #   * the prompt-question heuristic (display-only, idle + ATTENTION markers)
        #   * a T-06 terminal failure with no PM to pick it up
        # Stuck takes precedence (immediate action), then a no-PM escalation
        # (work blocked until acknowledged), then the prompt question (often
        # self-clears).
        stuck_reason = agent_stuck_reason(
            a["name"], stuck_after, in_flight=cur is not None,
        )
        prompt_attn = act == "idle" and agent_attention(a["name"])
        escalation = _escalation_reason(a["name"], d["tasks"])
        if stuck_reason:
            attention, attention_reason = True, stuck_reason
        elif escalation:
            attention, attention_reason = True, escalation
        elif prompt_attn:
            attention, attention_reason = True, "blocked on a question"
        else:
            attention, attention_reason = False, ""
        agents.append({
            "name": a["name"], "role": a["role"], "project_dir": a["project_dir"],
            "project": a.get("project", ""),
            "manager_of": a.get("manager_of", ""),
            "activity": act,
            "attention": attention,
            "attention_reason": attention_reason,
            "current_task": cur["id"] if cur else None,
            "current_desc": cur["description"] if cur else None,
            "current_started": cur["started_at"] if cur else None,
        })
    tasks = list(reversed(d["tasks"]))[:200]
    return {"agents": agents, "tasks": tasks, "projects": load_projects(CONFIG)}


def _sse_data(obj) -> bytes:
    """One SSE ``data:`` event carrying a JSON object. JSON-encoding keeps the
    (multi-line) screen text on a single SSE data line."""
    return b"data: " + json.dumps(obj).encode("utf-8") + b"\n\n"


def _sse_ping() -> bytes:
    """An SSE comment line — keeps the connection (and the tunnel) warm."""
    return b": ping\n\n"


def stream_agent_screen(write, backend, name) -> None:
    """Pump ``backend.stream_screen(name)`` to ``write`` as SSE. A HEARTBEAT
    sentinel becomes an ``: ping`` comment; a screen string becomes a data
    event. Returns quietly when the client disconnects (write raises)."""
    from backend import HEARTBEAT
    try:
        for item in backend.stream_screen(name):
            if item is HEARTBEAT:
                write(_sse_ping())
            else:
                write(_sse_data({"screen": item}))
    except (BrokenPipeError, ConnectionResetError, OSError):
        return


class Handler(BaseHTTPRequestHandler):
    # Paths that bypass the auth gate. ``/logout`` must answer with its own 401
    # (to invalidate the browser's cached Basic credentials) so it cannot be
    # blocked by the gate or it would never get the chance to send that 401.
    AUTH_EXEMPT = ("/healthz", "/logout")

    def log_message(self, *a):  # silence per-request logging
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict:
        n = int(self.headers.get("Content-Length", 0) or 0)
        if not n:
            return {}
        n = min(n, MAX_REQUEST_BYTES)   # bound memory; oversize bodies truncate -> rejected
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            return {}

    def _send_401(self, realm: str, body: bytes = b"Unauthorized\n") -> None:
        """Send a 401 with ``WWW-Authenticate: Basic realm="..."``.

        Body is intentionally generic ("Unauthorized") — we never leak which
        side (username vs password) the credential check failed on. The realm
        can be tweaked (e.g. for /logout's cache-busting) by the caller.
        """
        self.send_response(401)
        self.send_header("WWW-Authenticate", f'Basic realm="{realm}"')
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _auth_gate(self) -> bool:
        """Return True iff the request may proceed.

        - Exempt paths bypass the gate (healthz, logout).
        - When ``fleet.json`` has no ``auth`` block or ``enabled: false`` we
          fall through to legacy loopback-dev behavior — no challenge.
        - Otherwise validate ``Authorization: Basic ...`` in constant time and
          either allow the handler to continue (True) or write the 401
          ourselves (False — caller MUST return without touching the socket).
        """
        path = urlparse(self.path).path
        if path in self.AUTH_EXEMPT:
            return True
        try:
            cfg = load_auth()
        except Exception:
            cfg = None
        if cfg is None:
            return True   # auth disabled
        creds = auth.parse_basic_header(self.headers.get("Authorization"))
        if creds is None:
            self._send_401(cfg["realm"])
            return False
        user, password = creds
        if not auth.check_credentials(user, password, cfg):
            self._send_401(cfg["realm"])
            return False
        # Expose the authenticated username to handlers that want it.
        self.auth_user = user
        return True

    def do_GET(self):
        u = urlparse(self.path)
        # /logout must answer 401 with a DIFFERENT realm to invalidate the
        # browser's cached Basic credentials — it can't be auth-gated or it
        # never gets the chance to send that 401.
        if u.path == "/logout":
            # ASCII-only realm: HTTP headers are latin-1, and the unique
            # string also busts the browser's cached creds for the real realm.
            return self._send_401("Signed out - close this tab to sign in again",
                                  b"Signed out.\n")
        if u.path == "/healthz":
            return self._json({"ok": True})
        if not self._auth_gate():
            return
        try:
            if u.path == "/api/whoami":
                cfg = load_auth()
                user = getattr(self, "auth_user", None) if cfg else None
                return self._json({"username": user,
                                   "enabled": cfg is not None})
            if u.path == "/":
                if not DASHBOARD.exists():
                    return self._json({"error": "dashboard.html missing"}, 500)
                body = DASHBOARD.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif u.path == "/vendor/alpine.min.js":
                if not VENDOR_ALPINE.exists():
                    return self._json({"error": "alpine.min.js missing"}, 404)
                body = VENDOR_ALPINE.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "application/javascript; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "max-age=86400")
                self.end_headers()
                self.wfile.write(body)
            elif u.path == "/api/state":
                self._json(build_state())
            elif u.path == "/api/logs":
                q = parse_qs(u.query)
                name = (q.get("agent") or [""])[0]
                try:
                    lines = int((q.get("lines") or ["200"])[0])
                except ValueError:
                    lines = 200
                if not orch._window_exists(name):
                    return self._json({"text": "(agent offline)"})
                self._json({"text": orch._capture(name, lines)})
            elif u.path == "/api/agent/stream":
                q = parse_qs(u.query)
                name = (q.get("agent") or [""])[0]
                if name not in {a["name"] for a in AGENTS}:
                    return self._json({"error": "unknown agent"}, 400)
                # Switch to a streaming response; we own the socket from here.
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
                self.send_header("X-Accel-Buffering", "no")
                self.end_headers()
                if not orch._window_exists(name):
                    try:
                        self.wfile.write(_sse_data({"status": "offline"}))
                        self.wfile.flush()
                    except OSError:
                        pass
                    return
                def _w(b):
                    self.wfile.write(b)
                    self.wfile.flush()
                stream_agent_screen(_w, orch._backend, name)
                return
            elif u.path == "/api/task/log":
                q = parse_qs(u.query)
                tid = (q.get("id") or [""])[0]
                f = TASK_LOGS / f"{tid}.log"
                text = f.read_text(encoding="utf-8") if f.exists() else "(no captured log yet)"
                self._json({"text": text})
            elif u.path == "/api/docs":
                q = parse_qs(u.query)
                scope = (q.get("scope") or [""])[0]
                name = (q.get("name") or [None])[0]
                try:
                    self._json({"files": list_docs(scope, name)})
                except ValueError as e:
                    self._json({"error": str(e)}, 400)
            elif u.path == "/api/connectors":
                # mask secrets so they never leave the server, then add a
                # ``configured`` flag so the UI can render "(not set)" vs the
                # masked value without re-deriving it.
                block = load_connectors(CONFIG)
                masked = connectors.mask_connectors_block(block)
                for name, cfg in masked.items():
                    raw = block.get(name) or {}
                    for field in connectors.SECRET_FIELDS.get(name, ()):
                        cfg[f"{field}_configured"] = bool(raw.get(field))
                self._json({"connectors": masked,
                            "supported": list(connectors.SUPPORTED)})
            else:
                self._json({"error": "not found"}, 404)
        except Exception as e:
            self._json({"error": "internal error"}, 500)

    def do_PUT(self):
        u = urlparse(self.path)
        cl = int(self.headers.get("Content-Length", 0) or 0)
        if cl > MAX_REQUEST_BYTES:
            return self._json({"error": "request too large"}, 413)
        if not self._auth_gate():
            return
        try:
            body = self._read_body()
            # /api/connectors/<name>
            if u.path.startswith("/api/connectors/"):
                name = u.path[len("/api/connectors/"):].strip("/")
                if not name or "/" in name:
                    return self._json({"error": "missing connector name"}, 400)
                if name not in connectors.SUPPORTED:
                    return self._json({"error": f"unknown connector {name!r}"}, 400)
                try:
                    saved = update_connector(name, body)
                except ValueError as e:
                    return self._json({"error": str(e)}, 400)
                except OSError:
                    return self._json({"error": "could not update fleet.json"}, 500)
                # echo back the masked config so the UI sees the persisted state
                # without ever receiving the cleartext secret.
                masked = connectors.mask_connectors_block({name: saved})[name]
                for field in connectors.SECRET_FIELDS.get(name, ()):
                    masked[f"{field}_configured"] = bool(saved.get(field))
                self._json({"ok": True, "name": name, "connector": masked})
            else:
                self._json({"error": "not found"}, 404)
        except Exception:
            self._json({"error": "internal error"}, 500)

    def do_POST(self):
        u = urlparse(self.path)
        cl = int(self.headers.get("Content-Length", 0) or 0)
        if cl > MAX_REQUEST_BYTES:
            return self._json({"error": "request too large"}, 413)
        if not self._auth_gate():
            return
        try:
            body = self._read_body()
            if u.path == "/api/tasks":
                agent = (body.get("agent") or "").strip()
                desc = (body.get("description") or "").strip()
                names = {a["name"] for a in AGENTS}
                if agent not in names:
                    return self._json({"error": "unknown agent"}, 400)
                if not desc:
                    return self._json({"error": "empty task"}, 400)
                self._json({"id": add_task(agent, desc)})
            elif u.path == "/api/task/cancel":
                ok = cancel_task((body.get("id") or "").strip())
                self._json({"ok": ok})
            elif u.path == "/api/agent/send":
                name = (body.get("name") or "").strip()
                msg = (body.get("message") or "").strip()
                if name not in {a["name"] for a in AGENTS}:
                    return self._json({"error": "unknown agent"}, 400)
                if not msg:
                    return self._json({"error": "empty message"}, 400)
                self._json({"ok": send_message(name, msg)})
            elif u.path == "/api/agent/keys":
                # Raw-keystroke endpoint for the dashboard's interactive
                # terminal. Body: {name, keys_b64} — keys_b64 is base64 of the
                # UTF-8/VT bytes to write verbatim to the worker's PTY. A 1 MB
                # cap blocks runaway pastes from queueing huge writes.
                name = (body.get("name") or "").strip()
                keys_b64 = (body.get("keys_b64") or "").strip()
                if name not in {a["name"] for a in AGENTS}:
                    return self._json({"error": "unknown agent"}, 400)
                if not keys_b64:
                    return self._json({"error": "empty keys"}, 400)
                if len(keys_b64) > 1_400_000:
                    return self._json({"error": "keys payload too large"}, 413)
                try:
                    data = base64.b64decode(keys_b64, validate=True)
                except (ValueError, base64.binascii.Error):
                    return self._json({"error": "bad base64"}, 400)
                if not orch._window_exists(name):
                    return self._json({"error": "agent offline"}, 409)
                orch._send_keys(name, data)
                self._json({"ok": True})
            elif u.path == "/api/agent/restart":
                name = (body.get("name") or "").strip()
                if orch._window_exists(name):
                    orch._backend.kill(name)
                self._json({"ok": True})
            elif u.path == "/api/agent/path":
                name = (body.get("name") or "").strip()
                new_path = (body.get("path") or "").strip()
                if name not in {a["name"] for a in AGENTS}:
                    return self._json({"error": "unknown agent"}, 400)
                if not new_path:
                    return self._json({"error": "empty path"}, 400)
                p = Path(new_path).expanduser()
                if not p.is_dir():
                    return self._json({"error": "not a directory: " + new_path}, 400)
                try:
                    _set_agent_path(name, str(p))
                except ValueError as e:
                    return self._json({"error": str(e)}, 400)
                except OSError:
                    return self._json({"error": "could not update fleet.json"}, 500)
                # relaunch the agent so it runs in the new dir (dispatcher respawns it)
                if orch._window_exists(name):
                    orch._backend.kill(name)
                self._json({"ok": True})
            elif u.path == "/api/projects":
                name = (body.get("name") or "").strip()
                path = (body.get("path") or "").strip()
                description = (body.get("description") or "").strip()
                dashboard_url = (body.get("dashboard_url") or "").strip()
                try:
                    create_project(name, path, description, dashboard_url)
                except ValueError as e:
                    return self._json({"error": str(e)}, 400)
                except OSError:
                    return self._json({"error": "could not update fleet.json"}, 500)
                self._json({"ok": True, "name": name})
            elif u.path == "/api/projects/delete":
                name = (body.get("name") or "").strip()
                try:
                    ok = delete_project(name)
                except OSError:
                    return self._json({"error": "could not update fleet.json"}, 500)
                self._json({"ok": ok})
            elif u.path == "/api/agents":
                name = (body.get("name") or "").strip()
                role = (body.get("role") or "").strip()
                project_dir = (body.get("project_dir") or "").strip()
                project = (body.get("project") or "").strip()
                try:
                    create_agent(name, role, project_dir, project)
                except ValueError as e:
                    return self._json({"error": str(e)}, 400)
                except OSError:
                    return self._json({"error": "could not update fleet.json"}, 500)
                self._json({"ok": True, "name": name})
            elif u.path == "/api/agent/group":
                name = (body.get("name") or "").strip()
                project = (body.get("project") or "").strip()
                try:
                    set_agent_project(name, project)
                except ValueError as e:
                    return self._json({"error": str(e)}, 400)
                except OSError:
                    return self._json({"error": "could not update fleet.json"}, 500)
                self._json({"ok": True})
            elif u.path == "/api/tasks/group":
                project = (body.get("project") or "").strip()
                desc = (body.get("description") or "").strip()
                if not desc:
                    return self._json({"error": "empty task"}, 400)
                try:
                    ids = add_group_tasks(project, desc)
                except ValueError as e:
                    return self._json({"error": str(e)}, 400)
                self._json({"ok": True, "ids": ids, "count": len(ids)})
            elif u.path == "/api/projects/rename":
                name = (body.get("name") or "").strip()
                new_name = (body.get("new_name") or "").strip()
                try:
                    rename_project(name, new_name)
                except ValueError as e:
                    return self._json({"error": str(e)}, 400)
                except OSError:
                    return self._json({"error": "could not update fleet.json"}, 500)
                self._json({"ok": True, "name": new_name})
            elif u.path == "/api/projects/update":
                name = (body.get("name") or "").strip()
                path = (body.get("path") or "").strip()
                description = (body.get("description") or "").strip()
                dashboard_url = (body.get("dashboard_url") or "").strip()
                try:
                    update_project(name, path, description, dashboard_url)
                except ValueError as e:
                    return self._json({"error": str(e)}, 400)
                except OSError:
                    return self._json({"error": "could not update fleet.json"}, 500)
                self._json({"ok": True, "name": name})
            elif u.path == "/api/docs/upload":
                scope = (body.get("scope") or "").strip()
                name = body.get("name") or None
                filename = (body.get("filename") or "").strip()
                try:
                    data = base64.b64decode(body.get("content_base64") or "")
                except Exception:
                    return self._json({"error": "bad base64 content"}, 400)
                try:
                    self._json({"file": save_doc(scope, name, filename, data)})
                except ValueError as e:
                    self._json({"error": str(e)}, 400)
                except OSError:
                    self._json({"error": "storage error"}, 500)
            elif u.path == "/api/docs/delete":
                scope = (body.get("scope") or "").strip()
                name = body.get("name") or None
                filename = (body.get("filename") or "").strip()
                try:
                    self._json({"ok": delete_doc(scope, name, filename)})
                except ValueError as e:
                    self._json({"error": str(e)}, 400)
                except OSError:
                    self._json({"error": "storage error"}, 500)
            elif u.path.startswith("/api/connectors/") and u.path.endswith("/test"):
                name = u.path[len("/api/connectors/"):-len("/test")].strip("/")
                if not name or "/" in name:
                    return self._json({"error": "missing connector name"}, 400)
                if name not in connectors.SUPPORTED:
                    return self._json({"error": f"unknown connector {name!r}"}, 400)
                block = load_connectors(CONFIG)
                msg = (f"Orchestrator connector test from "
                       f"{socket.gethostname()} at {_now()}")
                result = connectors.send_via_connector(name, msg, block)
                # Surface failures as 200s with ok=false so the UI can render
                # the connector-level error inline without treating it as a
                # transport-layer 500.
                self._json({"ok": bool(result.get("ok")),
                            "error": result.get("error"),
                            "status": result.get("status")})
            else:
                self._json({"error": "not found"}, 404)
        except Exception as e:
            self._json({"error": "internal error"}, 500)


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #
def _pid_alive(pid: int) -> bool:
    """Best-effort check whether a process id is currently running."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            r = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                               capture_output=True, text=True)
            return str(pid) in r.stdout
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def cmd_up(a: argparse.Namespace) -> None:
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
        except (AttributeError, ValueError):
            pass
    # Refuse to start a second dispatcher: two would fight over the same agents
    # and spawn duplicate host processes.
    if FLEET_PID.exists():
        try:
            old = int(FLEET_PID.read_text().strip())
        except (ValueError, OSError):
            old = 0
        if _pid_alive(old):
            orch._die(f"a fleet is already running (pid {old}). Stop it "
                      f"(Ctrl-C) or run './fleet down', then retry.")
    FLEET_PID.parent.mkdir(parents=True, exist_ok=True)
    FLEET_PID.write_text(str(os.getpid()))
    if not orch._backend.available():
        orch._die(orch._backend.install_hint())
    global AGENTS
    AGENTS = load_config()
    # Backfill a project-manager agent for every project (refreshes AGENTS if it
    # added any), so the spawn loop below brings their terminals up too.
    ensure_project_managers()

    print(f"Starting fleet of {len(AGENTS)} agent(s)...")
    for r in reattach_on_startup(AGENTS):
        extra = ""
        if r.get("task_id"):
            extra = f"  [task {r['task_id']} -> {r['task_rolled_to']}]"
        print(f"  {r['name']:<14} {r['state']:<10} {r['note']}{extra}")

    disp = Dispatcher(AGENTS)
    disp.start()

    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    shown = a.host if a.host not in ("0.0.0.0", "") else "<this-host>"
    print(f"\nDashboard:  http://{shown}:{a.port}/   (http://localhost:{a.port}/)")
    if a.host in ("0.0.0.0", ""):
        print("WARNING: bound to all interfaces — anyone on your network can queue")
        print("         tasks that run autonomously. Use --host 127.0.0.1 to restrict.")
    print("Ctrl-C stops the dashboard; agent terminals keep running so `./fleet up` can reattach.")
    print("Use `./fleet down` to stop the dispatcher (agents persist); `./fleet down --all` for a hard reset.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down dashboard...")
    finally:
        disp.stop.set()
        srv.shutdown()
        try:
            FLEET_PID.unlink()
        except OSError:
            pass


def cmd_status(a: argparse.Namespace) -> None:
    global AGENTS
    AGENTS = load_config()
    print(f"{'AGENT':<14} {'ACTIVITY':<9} {'CURRENT':<8} {'ATTENTION':<28} PROJECT")
    for ag in AGENTS:
        st = build_state_one(ag)
        attn = ("! " + st["attention_reason"]) if st.get("attention") else "-"
        print(f"{ag['name']:<14} {st['activity']:<9} "
              f"{str(st['current_task'] or '-'):<8} {attn:<28} {ag['project_dir']}")
    d = _load_tasks()
    print(f"\n{'TASK':<8} {'AGENT':<14} {'STATUS':<9} {'TRIES':<5} DESCRIPTION")
    for t in d["tasks"][-30:]:
        # ``attempts`` counts failures (T-06); a healthy task shows 0. When a
        # task is in flight after retries, the last_error from the previous
        # attempt is appended after the description so the operator can see
        # *why* it retried without opening the task log.
        tries = str(t.get("attempts", 0))
        suffix = ""
        if t.get("attempts", 0) and t.get("last_error"):
            suffix = f"  [last_err: {t['last_error'][:40]}]"
        print(f"{t['id']:<8} {t['agent']:<14} {t['status']:<9} "
              f"{tries:<5} {t['description'][:50]}{suffix}")


def build_state_one(ag: dict) -> dict:
    d = _load_tasks()
    cur = next((t for t in d["tasks"]
                if t["agent"] == ag["name"] and t["status"] == "running"), None)
    stuck_after = rate_governor.load_rate_limit(CONFIG)["stuck_after_seconds"]
    stuck_reason = agent_stuck_reason(
        ag["name"], stuck_after, in_flight=cur is not None,
    )
    act = agent_activity(ag["name"])
    prompt_attn = act == "idle" and agent_attention(ag["name"])
    escalation = _escalation_reason(ag["name"], d["tasks"])
    if stuck_reason:
        attention, attention_reason = True, stuck_reason
    elif escalation:
        attention, attention_reason = True, escalation
    elif prompt_attn:
        attention, attention_reason = True, "blocked on a question"
    else:
        attention, attention_reason = False, ""
    return {"activity": act,
            "current_task": cur["id"] if cur else None,
            "attention": attention,
            "attention_reason": attention_reason}


def cmd_add(a: argparse.Namespace) -> None:
    agents = {x["name"] for x in load_config()}
    if a.agent not in agents:
        orch._die(f"unknown agent {a.agent!r}. Known: {', '.join(sorted(agents))}")
    print(f"queued {add_task(a.agent, a.task)} for {a.agent}")


def cmd_send(a: argparse.Namespace) -> None:
    agents = {x["name"] for x in load_config()}
    if a.agent not in agents:
        orch._die(f"unknown agent {a.agent!r}. Known: {', '.join(sorted(agents))}")
    if send_message(a.agent, a.message):
        print(f"sent to {a.agent}: {a.message}")
    else:
        orch._die(f"agent {a.agent!r} is offline (start it with './fleet up')")


def cmd_cancel(a: argparse.Namespace) -> None:
    print("canceled" if cancel_task(a.id) else "not found or not pending")


def cmd_sync_pms(a: argparse.Namespace) -> None:
    created = ensure_project_managers()
    if created:
        print(f"created project-manager agent(s): {', '.join(created)}")
    else:
        print("nothing to do — every project already has a project manager")


def _stop_dispatcher() -> None:
    """Stop a running `fleet up` (dispatcher) recorded in the pidfile.

    We kill ONLY the dispatcher process — NOT its child process tree. On
    Windows the agent_host processes are children of the dispatcher; if we
    used ``taskkill /T`` they'd all die with it, defeating the soft-down
    contract. ``kill_all`` is the explicit path for tearing down agents and
    is invoked separately under ``./fleet down --all``.
    """
    if not FLEET_PID.exists():
        return
    try:
        pid = int(FLEET_PID.read_text().strip())
    except (ValueError, OSError):
        pid = 0
    if pid > 0 and _pid_alive(pid):
        if sys.platform == "win32":
            # No /T — leave the agent_host children alive so `./fleet up`
            # can reattach to them.
            subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                           capture_output=True)
        else:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
        print(f"stopped dispatcher (pid {pid})")
    try:
        FLEET_PID.unlink()
    except OSError:
        pass


def cmd_down(a: argparse.Namespace) -> None:
    # Default = "soft down": stop the dispatcher (and the dashboard, since they
    # share a process) and LEAVE every agent terminal running. A follow-up
    # ``./fleet up`` reattaches to the live workers via reattach_on_startup(),
    # so any Claude conversation context an agent built up while working on a
    # task is preserved. ``--all`` is the hard reset: kill every agent terminal
    # too, losing the in-memory Claude state.
    _stop_dispatcher()
    if not getattr(a, "all", False):
        if orch._session_exists():
            try:
                live = list(orch._windows())
            except Exception:
                live = []
            n = len(live)
            agents = ", ".join(live) if live else "?"
            print(f"left {n} agent terminal(s) running: {agents}")
            print("`./fleet up` will reattach; use `./fleet down --all` to fully stop them.")
        else:
            print("no agent terminals to leave running")
        return
    if orch._session_exists():
        orch._backend.kill_all()
        print("stopped all agent terminals (Claude context lost)")
    else:
        print("no agents running")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="fleet", description="Claude agent fleet + dashboard")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("up", help="start agents, dispatcher, and dashboard")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=8787)
    s.set_defaults(func=cmd_up)

    s = sub.add_parser("status", help="print agents and tasks")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("add", help="queue a task for an agent")
    s.add_argument("agent")
    s.add_argument("task")
    s.set_defaults(func=cmd_add)

    s = sub.add_parser("send", help="send a message straight to a live agent")
    s.add_argument("agent")
    s.add_argument("message")
    s.set_defaults(func=cmd_send)

    s = sub.add_parser("cancel", help="cancel a pending task")
    s.add_argument("id")
    s.set_defaults(func=cmd_cancel)

    s = sub.add_parser("down",
        help="stop the dispatcher/dashboard (agent terminals stay alive; --all kills them too)")
    s.add_argument("--all", action="store_true",
        help="also kill every agent terminal — loses any Claude conversation context")
    s.set_defaults(func=cmd_down)

    s = sub.add_parser("sync-pms", help="backfill a project-manager agent for every project")
    s.set_defaults(func=cmd_sync_pms)
    return p


def main() -> None:
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
