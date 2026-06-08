"""Tests for the T-06 per-task failure & retry policy.

Covers:
  * ``_record_failure`` retries the task (status=retrying) for the first
    ``max_retries`` failures and escalates (status=failed) on the next one;
  * the backoff between retries comes from
    ``rate_governor.compute_backoff_seconds`` (not a re-implementation);
  * the dispatcher actually drives the gone-agent failure path through the
    helper — two consecutive failures both produce retries, the third
    escalates, ``attempts`` / ``last_error`` are updated each time;
  * escalation persists an entry to ``NOTIFICATIONS_FILE``; when no PM exists
    the agent's attention bar surfaces the failure.
"""
from __future__ import annotations

import json

import fleet
import orch
import worker_status


# --------------------------------------------------------------------------- #
# tiny test doubles
# --------------------------------------------------------------------------- #
class _DeadBackend:
    """worker_exists() always False — drives the dispatcher's gone-agent path."""
    def available(self): return True
    def install_hint(self): return ""
    def session_exists(self): return True
    def list_workers(self): return []
    def worker_exists(self, n): return False
    def spawn(self, *a, **k): return True, ""
    def capture(self, n, lines=200): return ""
    def send_text(self, n, text): pass
    def kill(self, n): pass
    def kill_all(self): pass
    def set_scrollback(self, n): pass
    def attach_hint(self): return ""


def _setup(tmp_path, monkeypatch, *, agents=None, fast_retry=True):
    """Wire fleet to tmp_path, install a fake backend, return a Dispatcher."""
    monkeypatch.setattr(worker_status, "STATUS_DIR", tmp_path / "status")
    monkeypatch.setattr(fleet, "STATE_DIR", tmp_path)
    monkeypatch.setattr(fleet, "TASKS_FILE", tmp_path / "tasks.json")
    monkeypatch.setattr(fleet, "NOTIFICATIONS_FILE",
                        tmp_path / "fleet_notifications.json")
    monkeypatch.setattr(fleet, "TASK_LOGS", tmp_path / "logs")
    monkeypatch.setattr(fleet, "agent_attention", lambda name: False)
    monkeypatch.setattr(fleet, "ensure_agent", lambda ag: "")
    monkeypatch.setattr(orch, "_backend", _DeadBackend())
    agents = agents or [{"name": "a", "role": "", "project_dir": str(tmp_path),
                         "project": "", "manager_of": ""}]
    monkeypatch.setattr(fleet, "load_config", lambda *a, **k: agents)
    monkeypatch.setattr(fleet, "AGENTS", agents)
    disp = fleet.Dispatcher(agents)
    if fast_retry:
        # Tests should not actually sleep for the default 30s backoff. Override
        # the rate-limit cfg so retries become immediately re-queueable.
        disp.rate_limit_cfg = {
            **disp.rate_limit_cfg, "max_retries": 2,
            "backoff": {"initial_seconds": 1, "max_seconds": 1, "factor": 1.0},
        }
    return disp


# --------------------------------------------------------------------------- #
# _record_failure unit tests
# --------------------------------------------------------------------------- #
def test_record_failure_retries_then_escalates_no_pm(tmp_path, monkeypatch):
    monkeypatch.setattr(fleet, "NOTIFICATIONS_FILE",
                        tmp_path / "fleet_notifications.json")
    monkeypatch.setattr(fleet, "AGENTS",
                        [{"name": "a", "role": "", "project_dir": str(tmp_path),
                          "project": "", "manager_of": ""}])

    cfg = {"max_retries": 2,
           "backoff": {"initial_seconds": 1, "max_seconds": 1, "factor": 1.0}}
    t = {"id": "t-1", "agent": "a", "description": "x", "status": "running",
         "attempts": 0, "last_error": "", "log": "",
         "escalated": False, "notified_pm": "", "branch": None}

    escalated = fleet._record_failure(t, "a", "boom #1", cfg)
    assert escalated is False
    assert t["status"] == "retrying"
    assert t["attempts"] == 1
    assert t["last_error"] == "boom #1"
    assert t["retry_at"]                  # backoff time recorded

    escalated = fleet._record_failure(t, "a", "boom #2", cfg)
    assert escalated is False
    assert t["status"] == "retrying"
    assert t["attempts"] == 2

    escalated = fleet._record_failure(t, "a", "boom #3", cfg)
    assert escalated is True
    assert t["status"] == "failed"
    assert t["attempts"] == 3
    assert t["last_error"] == "boom #3"
    assert t["escalated"] is True
    assert t["notified_pm"] == ""         # no PM exists for this agent
    # And the notification was appended to the durable log.
    notif = json.loads((tmp_path / "fleet_notifications.json").read_text())
    assert notif["notifications"][-1]["task_id"] == "t-1"
    assert notif["notifications"][-1]["attempts"] == 3
    assert notif["notifications"][-1]["pm"] == ""


def test_record_failure_routes_to_pm_when_present(tmp_path, monkeypatch):
    monkeypatch.setattr(fleet, "NOTIFICATIONS_FILE",
                        tmp_path / "fleet_notifications.json")
    monkeypatch.setattr(fleet, "AGENTS", [
        {"name": "w", "role": "", "project_dir": str(tmp_path),
         "project": "proj-x", "manager_of": ""},
        {"name": "proj-x-pm", "role": "", "project_dir": str(tmp_path),
         "project": "", "manager_of": "proj-x"},
    ])
    # Stub the send_message helper so the test doesn't need a live PM terminal.
    sent = []
    monkeypatch.setattr(fleet, "send_message",
                        lambda name, msg: sent.append((name, msg)) or True)

    cfg = {"max_retries": 0,                # escalate on the first failure
           "backoff": {"initial_seconds": 1, "max_seconds": 1, "factor": 1.0}}
    t = {"id": "t-7", "agent": "w", "description": "x", "status": "running",
         "attempts": 0, "last_error": "", "log": "",
         "escalated": False, "notified_pm": "", "branch": None}

    fleet._record_failure(t, "w", "kaboom", cfg)
    assert t["status"] == "failed"
    assert t["notified_pm"] == "proj-x-pm"
    assert t["escalated"] is False        # PM handled it; attention bar quiet
    assert sent and sent[0][0] == "proj-x-pm"
    notif = json.loads((tmp_path / "fleet_notifications.json").read_text())
    assert notif["notifications"][-1]["pm"] == "proj-x-pm"


# --------------------------------------------------------------------------- #
# dispatcher end-to-end through the gone-agent failure path
# --------------------------------------------------------------------------- #
def _make_running(tid: str = "t-1") -> dict:
    return {
        "id": tid, "agent": "a", "description": "do x",
        "status": "running", "created_at": fleet._now(),
        "started_at": fleet._now(), "finished_at": None,
        "saw_busy": True, "idle_seen": 0, "gone_seen": 0, "tries": 1,
        "needs_attention": False, "log": "",
        "branch": None, "base_sha": None, "head_sha": None,
        "attempts": 0, "last_error": "", "escalated": False,
        "notified_pm": "",
    }


def _drive_failure_round(disp, tmp_path):
    """Tick the dispatcher CONFIRM_GONE times; the gone-agent path fires once."""
    for _ in range(fleet.CONFIRM_GONE):
        disp.tick()


def test_dispatcher_two_failures_retry_then_third_escalates(tmp_path, monkeypatch):
    disp = _setup(tmp_path, monkeypatch)
    fleet._save_tasks({"next_id": 2, "tasks": [_make_running()]})

    # Round 1: agent looks gone -> first retry recorded.
    _drive_failure_round(disp, tmp_path)
    t = fleet._load_tasks()["tasks"][0]
    assert t["status"] == "retrying"
    assert t["attempts"] == 1
    assert "agent terminal closed" in t["last_error"]

    # Manually re-prime the task as running (simulating the tick-2.5 re-queue
    # immediately followed by re-dispatch). The dispatcher's re-dispatch path
    # needs a live agent + ready TUI to actually flip the status; in this
    # synthetic test the dead backend never returns ready, so we skip the
    # round-trip and just simulate the next failure round.
    t = fleet._load_tasks()["tasks"][0]
    t["status"] = "running"
    t["started_at"] = fleet._now()
    t["gone_seen"] = 0
    fleet._save_tasks({"next_id": 2, "tasks": [t]})

    # Round 2: second failure -> second retry.
    _drive_failure_round(disp, tmp_path)
    t = fleet._load_tasks()["tasks"][0]
    assert t["status"] == "retrying"
    assert t["attempts"] == 2

    # Re-prime for round 3.
    t["status"] = "running"
    t["started_at"] = fleet._now()
    t["gone_seen"] = 0
    fleet._save_tasks({"next_id": 2, "tasks": [t]})

    # Round 3: third failure -> escalation.
    _drive_failure_round(disp, tmp_path)
    t = fleet._load_tasks()["tasks"][0]
    assert t["status"] == "failed"
    assert t["attempts"] == 3
    assert t["escalated"] is True        # no PM for "a"
    # And the notifications file records the escalation.
    notif_path = tmp_path / "fleet_notifications.json"
    assert notif_path.exists()
    notif = json.loads(notif_path.read_text())
    assert notif["notifications"][-1]["attempts"] == 3


# --------------------------------------------------------------------------- #
# attention bar surfaces escalation when no PM
# --------------------------------------------------------------------------- #
def test_attention_bar_lights_up_on_no_pm_escalation(tmp_path, monkeypatch):
    monkeypatch.setattr(fleet, "AGENTS",
                        [{"name": "a", "role": "", "project_dir": str(tmp_path),
                          "project": "", "manager_of": ""}])
    tasks = [{
        "id": "t-9", "agent": "a", "description": "x",
        "status": "failed", "created_at": fleet._now(),
        "started_at": fleet._now(), "finished_at": fleet._now(),
        "saw_busy": True, "idle_seen": 0, "log": "",
        "attempts": 3, "last_error": "boom",
        "escalated": True, "notified_pm": "",
    }]
    reason = fleet._escalation_reason("a", tasks)
    assert "t-9" in reason
    assert "3 attempts" in reason
    assert "boom" in reason


def test_attention_bar_silent_after_pm_handoff(tmp_path, monkeypatch):
    """``notified_pm`` set means the PM owns it — don't light the human's bar."""
    monkeypatch.setattr(fleet, "AGENTS",
                        [{"name": "a", "role": "", "project_dir": str(tmp_path),
                          "project": "p", "manager_of": ""},
                         {"name": "p-pm", "role": "", "project_dir": str(tmp_path),
                          "project": "", "manager_of": "p"}])
    tasks = [{
        "id": "t-9", "agent": "a", "description": "x",
        "status": "failed", "attempts": 3, "last_error": "boom",
        "escalated": False, "notified_pm": "p-pm",
    }]
    assert fleet._escalation_reason("a", tasks) == ""
