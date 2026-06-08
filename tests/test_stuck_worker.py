"""T-05 stuck-worker detection.

A worker that has stopped beating its heartbeat must surface in the dashboard
attention bar with a human-readable reason. Tests cover:
  * the pure ``agent_stuck_reason`` helper (heartbeat staleness, freshness,
    terminal-silence fallback, idle-agent suppression);
  * the ``build_state`` integration that the dashboard reads — a stale
    heartbeat flips ``attention=True`` with ``attention_reason`` matching
    "no heartbeat for Xm";
  * ``stuck_after_seconds`` is loaded from the ``rate_limit`` block in
    ``fleet.json`` and merged into the governor cfg.
"""
from datetime import datetime, timedelta, timezone

import fleet
import rate_governor
import worker_status


# --------------------------------------------------------------------------- #
# config plumbing
# --------------------------------------------------------------------------- #
def test_stuck_after_seconds_default_is_600():
    cfg = rate_governor._merge(None)
    assert cfg["stuck_after_seconds"] == 600


def test_stuck_after_seconds_loaded_from_fleet_json(tmp_path):
    cfg_path = tmp_path / "fleet.json"
    cfg_path.write_text(
        '{"rate_limit": {"stuck_after_seconds": 120}}', encoding="utf-8")
    assert rate_governor.load_rate_limit(cfg_path)["stuck_after_seconds"] == 120


# --------------------------------------------------------------------------- #
# agent_stuck_reason (pure logic)
# --------------------------------------------------------------------------- #
def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _setup_status_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(worker_status, "STATUS_DIR", tmp_path / "status")
    (tmp_path / "status").mkdir(parents=True, exist_ok=True)


def test_stuck_reason_flags_stale_heartbeat(tmp_path, monkeypatch):
    _setup_status_dir(tmp_path, monkeypatch)
    now = datetime(2030, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    stale = _iso(now - timedelta(seconds=720))   # 12m ago
    worker_status.write_status("alice", state="running", last_beat=stale,
                               task_id="t-1", pid=123)
    reason = fleet.agent_stuck_reason("alice", 600, now=now)
    assert reason == "no heartbeat for 12m"


def test_stuck_reason_clears_when_heartbeat_fresh(tmp_path, monkeypatch):
    _setup_status_dir(tmp_path, monkeypatch)
    now = datetime(2030, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    fresh = _iso(now - timedelta(seconds=10))
    worker_status.write_status("alice", state="running", last_beat=fresh,
                               task_id="t-1", pid=123)
    assert fleet.agent_stuck_reason("alice", 600, now=now) is None


def test_stuck_reason_ignores_done_agent(tmp_path, monkeypatch):
    # A worker that has finished its task isn't expected to keep beating, so a
    # stale last_beat on a state=done record must NOT raise an alarm.
    _setup_status_dir(tmp_path, monkeypatch)
    now = datetime(2030, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    stale = _iso(now - timedelta(seconds=3600))
    worker_status.write_status("alice", state="done", last_beat=stale,
                               task_id="t-1", pid=123)
    assert fleet.agent_stuck_reason("alice", 600, now=now) is None


def test_stuck_reason_no_status_and_no_in_flight_not_flagged(tmp_path, monkeypatch):
    # Fresh, never-tasked agent: no status file, no in-flight task → silent.
    _setup_status_dir(tmp_path, monkeypatch)
    monkeypatch.setattr(fleet, "_TERMINAL_ACTIVITY", {})
    assert fleet.agent_stuck_reason("alice", 600, in_flight=False) is None


def test_stuck_reason_terminal_silence_fallback(tmp_path, monkeypatch):
    # Legacy worker without the heartbeat protocol but with an in-flight task
    # whose terminal hasn't changed inside the window → flagged.
    _setup_status_dir(tmp_path, monkeypatch)
    now = datetime(2030, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(
        fleet, "_TERMINAL_ACTIVITY",
        {"alice": (12345, now - timedelta(seconds=720))},
    )
    reason = fleet.agent_stuck_reason("alice", 600, now=now, in_flight=True)
    assert reason == "no terminal output for 12m"


def test_record_terminal_activity_only_stamps_on_change(tmp_path, monkeypatch):
    monkeypatch.setattr(fleet, "_TERMINAL_ACTIVITY", {})
    fleet._record_terminal_activity("alice", "screen-a")
    first = fleet._TERMINAL_ACTIVITY["alice"][1]
    fleet._record_terminal_activity("alice", "screen-a")
    assert fleet._TERMINAL_ACTIVITY["alice"][1] == first   # unchanged stamp
    fleet._record_terminal_activity("alice", "screen-b")
    assert fleet._TERMINAL_ACTIVITY["alice"][1] != first   # advanced


# --------------------------------------------------------------------------- #
# build_state integration — the dashboard contract the brief requires
# --------------------------------------------------------------------------- #
def _setup_build_state(tmp_path, monkeypatch, agents, tasks=None):
    """Stub the parts of build_state that hit live processes / disk."""
    cfg_path = tmp_path / "fleet.json"
    cfg_path.write_text(
        '{"rate_limit": {"stuck_after_seconds": 600}, "projects": []}',
        encoding="utf-8")
    monkeypatch.setattr(fleet, "CONFIG", cfg_path)
    monkeypatch.setattr(fleet, "AGENTS", agents)
    monkeypatch.setattr(fleet, "agent_activity", lambda name: "busy")
    monkeypatch.setattr(fleet, "agent_attention", lambda name: False)
    monkeypatch.setattr(fleet, "_load_tasks",
                        lambda: {"next_id": 1,
                                 "tasks": tasks if tasks is not None else []})
    monkeypatch.setattr(fleet, "load_projects", lambda *a, **k: [])
    _setup_status_dir(tmp_path, monkeypatch)


def test_build_state_flags_stuck_worker_with_reason(tmp_path, monkeypatch):
    """A stale heartbeat must surface in build_state as attention + reason."""
    agents = [{"name": "alice", "role": "", "project_dir": str(tmp_path),
               "project": "", "manager_of": ""}]
    running_task = {
        "id": "t-1", "agent": "alice", "description": "x", "status": "running",
        "created_at": fleet._now(), "started_at": fleet._now(),
        "finished_at": None,
    }
    _setup_build_state(tmp_path, monkeypatch, agents, tasks=[running_task])
    # Stale heartbeat (15m old; window is 600s = 10m)
    stale = _iso(datetime.now(timezone.utc) - timedelta(seconds=900))
    worker_status.write_status("alice", state="running", last_beat=stale,
                               task_id="t-1", pid=123)

    state = fleet.build_state()
    alice = next(a for a in state["agents"] if a["name"] == "alice")
    assert alice["attention"] is True
    assert "no heartbeat for" in alice["attention_reason"]
    assert alice["attention_reason"].endswith("m")   # rounded-minute label


def test_build_state_fresh_heartbeat_is_not_flagged(tmp_path, monkeypatch):
    agents = [{"name": "alice", "role": "", "project_dir": str(tmp_path),
               "project": "", "manager_of": ""}]
    _setup_build_state(tmp_path, monkeypatch, agents)
    fresh = _iso(datetime.now(timezone.utc) - timedelta(seconds=5))
    worker_status.write_status("alice", state="running", last_beat=fresh,
                               task_id="t-1", pid=123)
    state = fleet.build_state()
    alice = next(a for a in state["agents"] if a["name"] == "alice")
    assert alice["attention"] is False
    assert alice["attention_reason"] == ""


def test_build_state_stuck_takes_precedence_over_prompt_attention(
        tmp_path, monkeypatch):
    # If an agent looks blocked on a question AND has a stale heartbeat, the
    # stuck reason is the surfaced one — it's the more actionable signal.
    agents = [{"name": "alice", "role": "", "project_dir": str(tmp_path),
               "project": "", "manager_of": ""}]
    _setup_build_state(tmp_path, monkeypatch, agents)
    monkeypatch.setattr(fleet, "agent_activity", lambda name: "idle")
    monkeypatch.setattr(fleet, "agent_attention", lambda name: True)
    stale = _iso(datetime.now(timezone.utc) - timedelta(seconds=1200))
    worker_status.write_status("alice", state="running", last_beat=stale,
                               task_id="t-1", pid=123)
    state = fleet.build_state()
    alice = next(a for a in state["agents"] if a["name"] == "alice")
    assert alice["attention"] is True
    assert alice["attention_reason"].startswith("no heartbeat for")
