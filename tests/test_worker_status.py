"""Tests for the worker-status protocol (T-01).

Covers:
  * read/write/heartbeat helpers in ``worker_status``;
  * ``fleet.agent_activity`` and ``orch._worker_state`` preferring the status
    file when present and fresh, falling back to the legacy tmux/capture
    heuristic when it is missing or stale;
  * the dispatcher closing a running task on ``state="done"`` from the file
    even when the legacy ``WORKER-DONE:`` marker never appears.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import fleet
import orch
import worker_status


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
class _BackendStub:
    """Drives orch._capture / worker_exists with scripted values."""
    def __init__(self, alive=True, captures=("",)):
        self.alive = alive
        self.captures = list(captures)
        self._i = 0

    def available(self): return True
    def install_hint(self): return ""
    def session_exists(self): return True
    def list_workers(self): return ["a"]
    def worker_exists(self, n): return self.alive
    def spawn(self, *a, **k): return True, ""
    def kill(self, n): pass
    def kill_all(self): pass
    def set_scrollback(self, n): pass
    def attach_hint(self): return ""

    def capture(self, name, lines=200):
        v = self.captures[min(self._i, len(self.captures) - 1)]
        self._i += 1
        return v

    def send_text(self, name, text): pass


def _point_status_at(tmp_path, monkeypatch):
    """Route the worker-status helpers (and the freshness check) at tmp_path."""
    monkeypatch.setattr(worker_status, "STATUS_DIR", tmp_path / "status")


def _stale_iso() -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=300)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


# --------------------------------------------------------------------------- #
# worker_status core
# --------------------------------------------------------------------------- #
def test_write_status_creates_file_and_merges(tmp_path, monkeypatch):
    _point_status_at(tmp_path, monkeypatch)
    worker_status.write_status("a", state="starting", pid=42,
                               task_id="t-1", progress_note="boot")
    d = worker_status.read_status("a")
    assert d["state"] == "starting"
    assert d["pid"] == 42
    assert d["task_id"] == "t-1"
    assert d["progress_note"] == "boot"
    assert d["last_beat"]  # populated automatically

    # Subsequent write merges, preserving prior fields.
    worker_status.write_status("a", state="running")
    d2 = worker_status.read_status("a")
    assert d2["state"] == "running"
    assert d2["pid"] == 42
    assert d2["task_id"] == "t-1"


def test_write_status_rejects_unknown_state(tmp_path, monkeypatch):
    _point_status_at(tmp_path, monkeypatch)
    try:
        worker_status.write_status("a", state="bogus")
    except ValueError:
        return
    raise AssertionError("expected ValueError for unknown state")


def test_heartbeat_fresh_threshold(tmp_path, monkeypatch):
    _point_status_at(tmp_path, monkeypatch)
    worker_status.write_status("a", state="running")
    assert worker_status.heartbeat_fresh(worker_status.read_status("a"))

    # Spoof a stale last_beat by writing through the helper.
    worker_status.write_status("a", last_beat=_stale_iso())
    assert not worker_status.heartbeat_fresh(worker_status.read_status("a"))


def test_clear_status_removes_file(tmp_path, monkeypatch):
    _point_status_at(tmp_path, monkeypatch)
    worker_status.write_status("a", state="done")
    assert worker_status.status_path("a").exists()
    worker_status.clear_status("a")
    assert not worker_status.status_path("a").exists()
    # Idempotent: clearing a non-existent file is a no-op.
    worker_status.clear_status("a")


def test_atomic_write_no_tmp_file_left_behind(tmp_path, monkeypatch):
    _point_status_at(tmp_path, monkeypatch)
    worker_status.write_status("a", state="running")
    p = worker_status.status_path("a")
    assert p.exists()
    assert not p.with_suffix(".tmp").exists()


# --------------------------------------------------------------------------- #
# fleet.agent_activity preferred path
# --------------------------------------------------------------------------- #
def test_agent_activity_uses_status_file_when_fresh(tmp_path, monkeypatch):
    _point_status_at(tmp_path, monkeypatch)
    # Even with a "busy" capture in the backend, status=done should win.
    monkeypatch.setattr(orch, "_backend",
                        _BackendStub(alive=True,
                                     captures=("esc to interrupt",)))
    worker_status.write_status("a", state="done")
    assert fleet.agent_activity("a") == "idle"

    worker_status.write_status("a", state="running")
    assert fleet.agent_activity("a") == "busy"


def test_agent_activity_falls_back_when_no_status_file(tmp_path, monkeypatch):
    _point_status_at(tmp_path, monkeypatch)
    monkeypatch.setattr(orch, "_backend",
                        _BackendStub(alive=True,
                                     captures=("esc to interrupt",)))
    # No status file at all -> legacy heuristic from the capture.
    assert fleet.agent_activity("a") == "busy"


def test_agent_activity_falls_back_when_status_stale(tmp_path, monkeypatch):
    _point_status_at(tmp_path, monkeypatch)
    worker_status.write_status("a", state="running", last_beat=_stale_iso())
    monkeypatch.setattr(orch, "_backend",
                        _BackendStub(alive=True,
                                     captures=("? for shortcuts",)))
    # File present but stale -> fall back to the legacy heuristic (idle).
    assert fleet.agent_activity("a") == "idle"


# --------------------------------------------------------------------------- #
# orch._worker_state preferred path
# --------------------------------------------------------------------------- #
def test_orch_worker_state_uses_status_file(tmp_path, monkeypatch):
    _point_status_at(tmp_path, monkeypatch)
    # Capture would normally read as "idle"; status file says "done".
    monkeypatch.setattr(orch, "_backend",
                        _BackendStub(alive=True,
                                     captures=("? for shortcuts",)))
    worker_status.write_status("a", state="done")
    assert orch._worker_state("a") == "done"

    worker_status.write_status("a", state="running")
    assert orch._worker_state("a") == "busy"


def test_orch_worker_state_legacy_marker_when_no_file(tmp_path, monkeypatch):
    _point_status_at(tmp_path, monkeypatch)
    monkeypatch.setattr(orch, "_backend",
                        _BackendStub(alive=True,
                                     captures=("WORKER-DONE: ok",)))
    assert orch._worker_state("a") == "done"


# --------------------------------------------------------------------------- #
# dispatcher: status=done closes task even without legacy marker
# --------------------------------------------------------------------------- #
def test_dispatcher_closes_task_from_status_file(tmp_path, monkeypatch):
    _point_status_at(tmp_path, monkeypatch)
    # Backend reports "idle" only (no WORKER-DONE marker). Status file is the
    # only signal that the task finished.
    fb = _BackendStub(alive=True, captures=("? for shortcuts",))
    monkeypatch.setattr(orch, "_backend", fb)
    monkeypatch.setattr(fleet, "STATE_DIR", tmp_path)
    monkeypatch.setattr(fleet, "TASKS_FILE", tmp_path / "tasks.json")
    monkeypatch.setattr(fleet, "TASK_LOGS", tmp_path / "logs")
    monkeypatch.setattr(fleet, "agent_attention", lambda name: False)
    agents = [{"name": "a", "role": "", "project_dir": str(tmp_path)}]
    monkeypatch.setattr(fleet, "load_config", lambda *a, **k: agents)
    monkeypatch.setattr(fleet, "ensure_agent", lambda ag: "")

    fleet._save_tasks({"next_id": 2, "tasks": [{
        "id": "t-1", "agent": "a", "description": "do it",
        "status": "running", "created_at": fleet._now(),
        "started_at": fleet._now(), "finished_at": None,
        "saw_busy": True, "idle_seen": 0, "tries": 1,
        "needs_attention": False, "log": "",
    }]})
    # Agent_host would have flipped state=done on the busy->idle transition.
    worker_status.write_status("a", state="done", task_id="t-1")

    disp = fleet.Dispatcher(agents)
    disp.tick()

    t = fleet._load_tasks()["tasks"][0]
    assert t["status"] == "done"
    # And the task_id has been cleared so the next tick doesn't repeat.
    cleared = worker_status.read_status("a")
    assert cleared["task_id"] == ""
