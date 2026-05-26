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
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import orch  # tmux primitives + shared constants

HERE = Path(__file__).resolve().parent
CONFIG = HERE / "fleet.json"
DASHBOARD = HERE / "dashboard.html"

STATE_DIR = orch.STATE_DIR
TASKS_FILE = STATE_DIR / "fleet_tasks.json"
FLEET_PID = STATE_DIR / "fleet.pid"
TASK_LOGS = STATE_DIR / "logs" / "tasks"
ROLES_DIR = STATE_DIR / "roles"

POLL = 2.0          # dispatcher poll interval (seconds)
DELIVER_GRACE = 10  # if an agent never goes busy this long after dispatch, retry
MAX_TRIES = 3       # give up delivering a task after this many attempts
CONFIRM_IDLE = 2    # consecutive idle polls required before a task is called done
CONFIRM_GONE = 3    # consecutive 'window gone' polls before a running task fails

_LOCK = threading.Lock()
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
    data = json.loads(path.read_text())
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
        })
    if not agents:
        orch._die(f"no agents defined in {path}")
    return agents


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


def agent_activity(name: str) -> str:
    """'offline' | 'busy' | 'idle'."""
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
)


def agent_attention(name: str) -> bool:
    """True when an idle agent looks like it's blocked on a question for you."""
    if not orch._window_exists(name):
        return False
    low = orch._capture(name, 40).lower()
    if any(m in low for m in orch.BUSY_MARKERS):
        return False
    return any(m in low for m in ATTENTION_MARKERS)


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
    return (
        f"New task (id {task['id']}): {task['description']}  "
        "Work autonomously and do not ask questions — make reasonable decisions "
        "yourself. When finished, give a one-line summary of what you changed."
    )


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
            "started_at": None, "finished_at": None, "saw_busy": False, "log": "",
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


# --------------------------------------------------------------------------- #
# dispatcher
# --------------------------------------------------------------------------- #
class Dispatcher(threading.Thread):
    def __init__(self, agents: list[dict]):
        super().__init__(daemon=True)
        self.agents = {a["name"]: a for a in agents}
        self.stop = threading.Event()

    def run(self) -> None:
        while not self.stop.is_set():
            try:
                self.tick()
            except Exception as e:  # keep the loop alive
                print(f"[dispatch] error: {e}", file=sys.stderr, flush=True)
            self.stop.wait(POLL)

    def _reload_agents(self) -> None:
        """Re-read fleet.json so edits (new/removed agents) take effect live.

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
                if not orch._window_exists(name):
                    # Require sustained absence: a busy agent under load can miss a
                    # PING; one missed read must not fail an in-flight task.
                    t["gone_seen"] = t.get("gone_seen", 0) + 1
                    changed = True
                    if t["gone_seen"] >= CONFIRM_GONE:
                        t.update(status="failed", finished_at=_now(),
                                 log=(t.get("log") or "") + "\n[agent terminal closed]")
                    continue
                t["gone_seen"] = 0
                act = agent_activity(name)
                if act == "busy":
                    if not t.get("saw_busy") or t.get("idle_seen"):
                        t["saw_busy"] = True
                        t["idle_seen"] = 0
                        changed = True
                    continue
                # agent looks idle:
                if t.get("saw_busy"):
                    # Require CONFIRM_IDLE consecutive idle reads before declaring
                    # done — a single dropped capture reads as idle and would end
                    # the task early with a truncated log.
                    t["idle_seen"] = t.get("idle_seen", 0) + 1
                    changed = True
                    if t["idle_seen"] >= CONFIRM_IDLE:
                        cap = orch._capture(name, 400)
                        _write_task_log(t["id"], cap)
                        t.update(status="done", finished_at=_now(), log=_tail(cap))
                elif _age(t.get("started_at")) > DELIVER_GRACE:
                    # never started working -> the message likely never landed
                    if t.get("tries", 1) >= MAX_TRIES:
                        t.update(status="failed", finished_at=_now(),
                                 log="agent never started the task (no response)")
                    else:
                        orch._send_text(name, _kickoff(t))
                        t.update(started_at=_now(), tries=t.get("tries", 1) + 1)
                    changed = True

            # 3) dispatch pending tasks to free, ready agents (FIFO)
            busy = {t["agent"] for t in d["tasks"] if t["status"] == "running"}
            for t in (x for x in d["tasks"] if x["status"] == "pending"):
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
                orch._send_text(name, _kickoff(t))
                t.update(status="running", started_at=_now(), saw_busy=False,
                         idle_seen=0, gone_seen=0, tries=1)
                busy.add(name)
                changed = True

            if changed:
                _save_tasks(d)


# --------------------------------------------------------------------------- #
# web dashboard
# --------------------------------------------------------------------------- #
def build_state() -> dict:
    with _LOCK:
        d = _load_tasks()
    running = {t["agent"]: t for t in d["tasks"] if t["status"] == "running"}
    agents = []
    for a in AGENTS:
        cur = running.get(a["name"])
        act = agent_activity(a["name"])
        agents.append({
            "name": a["name"], "role": a["role"], "project_dir": a["project_dir"],
            "activity": act,
            "attention": act == "idle" and agent_attention(a["name"]),
            "current_task": cur["id"] if cur else None,
            "current_desc": cur["description"] if cur else None,
            "current_started": cur["started_at"] if cur else None,
        })
    tasks = list(reversed(d["tasks"]))[:200]
    return {"agents": agents, "tasks": tasks}


class Handler(BaseHTTPRequestHandler):
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

    def do_GET(self):
        u = urlparse(self.path)
        try:
            if u.path == "/":
                if not DASHBOARD.exists():
                    return self._json({"error": "dashboard.html missing"}, 500)
                body = DASHBOARD.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
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
            else:
                self._json({"error": "not found"}, 404)
        except Exception as e:
            self._json({"error": "internal error"}, 500)

    def do_POST(self):
        u = urlparse(self.path)
        cl = int(self.headers.get("Content-Length", 0) or 0)
        if cl > MAX_REQUEST_BYTES:
            return self._json({"error": "request too large"}, 413)
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
            elif u.path == "/api/agent/restart":
                name = (body.get("name") or "").strip()
                if orch._window_exists(name):
                    orch._backend.kill(name)
                self._json({"ok": True})
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

    print(f"Starting fleet of {len(AGENTS)} agent(s)...")
    for ag in AGENTS:
        print(f"  {ag['name']:<14} {ensure_agent(ag)}")

    disp = Dispatcher(AGENTS)
    disp.start()

    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    shown = a.host if a.host not in ("0.0.0.0", "") else "<this-host>"
    print(f"\nDashboard:  http://{shown}:{a.port}/   (http://localhost:{a.port}/)")
    if a.host in ("0.0.0.0", ""):
        print("WARNING: bound to all interfaces — anyone on your network can queue")
        print("         tasks that run autonomously. Use --host 127.0.0.1 to restrict.")
    print("Ctrl-C stops the dashboard; agent terminals keep running ('./fleet down' to stop them).")
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
    print(f"{'AGENT':<14} {'ACTIVITY':<9} {'CURRENT':<8} PROJECT")
    for ag in AGENTS:
        st = build_state_one(ag)
        print(f"{ag['name']:<14} {st['activity']:<9} {str(st['current_task'] or '-'):<8} {ag['project_dir']}")
    d = _load_tasks()
    print(f"\n{'TASK':<8} {'AGENT':<14} {'STATUS':<9} DESCRIPTION")
    for t in d["tasks"][-30:]:
        print(f"{t['id']:<8} {t['agent']:<14} {t['status']:<9} {t['description'][:50]}")


def build_state_one(ag: dict) -> dict:
    d = _load_tasks()
    cur = next((t for t in d["tasks"]
                if t["agent"] == ag["name"] and t["status"] == "running"), None)
    return {"activity": agent_activity(ag["name"]),
            "current_task": cur["id"] if cur else None}


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


def _stop_dispatcher() -> None:
    """Stop a running `fleet up` (dispatcher) recorded in the pidfile, so its
    keep-alive loop can't respawn the agents `down` is about to kill."""
    if not FLEET_PID.exists():
        return
    try:
        pid = int(FLEET_PID.read_text().strip())
    except (ValueError, OSError):
        pid = 0
    if pid > 0 and _pid_alive(pid):
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/PID", str(pid), "/F", "/T"],
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
    # Stop the dispatcher first so its keep-alive loop can't respawn the agents
    # we're about to kill.
    _stop_dispatcher()
    if orch._session_exists():
        orch._backend.kill_all()
        print("stopped all agent terminals")
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

    s = sub.add_parser("down", help="stop all agent terminals")
    s.set_defaults(func=cmd_down)
    return p


def main() -> None:
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
