"""Rate-limit governor tests.

Covers
  * the helper module's pure functions (config load, 429 detection, backoff);
  * the dispatcher behaviour the T-02 brief requires: an injected 429 in
    captured worker output transitions the task to ``retrying`` (NOT
    ``failed``) and is later re-queued; ``max_concurrent`` caps the number of
    concurrent ``running`` tasks.
"""
import json

import fleet
import orch
import rate_governor


# --------------------------------------------------------------------------- #
# pure-function tests (no dispatcher, no backend)
# --------------------------------------------------------------------------- #
def test_load_rate_limit_defaults_when_block_missing(tmp_path):
    cfg = tmp_path / "fleet.json"
    cfg.write_text(json.dumps({"projects": [], "agents": []}), encoding="utf-8")
    rl = rate_governor.load_rate_limit(cfg)
    assert rl == rate_governor._merge(None)
    assert rl["max_concurrent"] == 3
    assert rl["spawn_stagger_seconds"] == 5
    assert rl["backoff"]["initial_seconds"] == 30
    assert rl["backoff"]["max_seconds"] == 600
    assert rl["backoff"]["factor"] == 2.0


def test_load_rate_limit_merges_partial_block(tmp_path):
    cfg = tmp_path / "fleet.json"
    cfg.write_text(json.dumps({
        "rate_limit": {"max_concurrent": 7,
                       "backoff": {"initial_seconds": 5}}
    }), encoding="utf-8")
    rl = rate_governor.load_rate_limit(cfg)
    assert rl["max_concurrent"] == 7
    assert rl["spawn_stagger_seconds"] == 5             # default kept
    assert rl["backoff"]["initial_seconds"] == 5        # overridden
    assert rl["backoff"]["max_seconds"] == 600          # default kept
    assert rl["backoff"]["factor"] == 2.0               # default kept


def test_load_rate_limit_missing_file_yields_defaults(tmp_path):
    rl = rate_governor.load_rate_limit(tmp_path / "does-not-exist.json")
    assert rl == rate_governor._merge(None)


def test_detect_rate_limit_matches_canonical_phrases():
    assert rate_governor.detect_rate_limit("HTTP 429 Too Many Requests")
    assert rate_governor.detect_rate_limit("anthropic api: rate limit exceeded")
    assert rate_governor.detect_rate_limit("X-RateLimit-Remaining: 0")
    assert rate_governor.detect_rate_limit("error: rate-limit hit, retry later")
    assert not rate_governor.detect_rate_limit("everything is fine")
    assert not rate_governor.detect_rate_limit("")


def test_compute_backoff_grows_then_caps():
    cfg = rate_governor._merge({
        "backoff": {"initial_seconds": 30, "max_seconds": 120, "factor": 2.0}
    })
    assert rate_governor.compute_backoff_seconds(1, cfg) == 30
    assert rate_governor.compute_backoff_seconds(2, cfg) == 60
    assert rate_governor.compute_backoff_seconds(3, cfg) == 120
    assert rate_governor.compute_backoff_seconds(4, cfg) == 120   # capped
    assert rate_governor.compute_backoff_seconds(0, cfg) == 30    # 1-indexed floor


# --------------------------------------------------------------------------- #
# dispatcher tests (reusing the FakeBackend pattern from test_dispatcher.py)
# --------------------------------------------------------------------------- #
class FakeBackend:
    def __init__(self, captures, alive=True, ready=True):
        self.captures = list(captures)
        self.i = 0
        self.alive = alive
        self.ready = ready
        self.sent: list[tuple[str, str]] = []

    def available(self):
        return True

    def worker_exists(self, name):
        return self.alive

    def capture(self, name, lines=200):
        v = self.captures[min(self.i, len(self.captures) - 1)] if self.captures else ""
        self.i += 1
        return v

    def send_text(self, name, text):
        self.sent.append((name, text))

    def spawn(self, *a, **k):
        return True, ""

    def kill(self, name):
        pass

    def kill_all(self):
        pass


def _running_task(tid="t-1", agent="a", saw_busy=True):
    """A task already mid-flight (saw_busy=True so 429 detection is armed)."""
    return {
        "id": tid, "agent": agent, "description": "x", "status": "running",
        "created_at": fleet._now(), "started_at": fleet._now(),
        "finished_at": None, "saw_busy": saw_busy, "idle_seen": 0,
        "tries": 1, "needs_attention": False, "log": "",
    }


def _pending_task(tid, agent):
    return {
        "id": tid, "agent": agent, "description": "x", "status": "pending",
        "created_at": fleet._now(), "started_at": None, "finished_at": None,
        "saw_busy": False, "needs_attention": False, "log": "",
    }


def _stub_rate_limit(monkeypatch, **over):
    """Patch the dispatcher to use a deterministic rate_limit cfg for tests."""
    cfg = rate_governor._merge({
        "max_concurrent": 3,
        "spawn_stagger_seconds": 0,           # off by default in tests
        "backoff": {"initial_seconds": 30, "max_seconds": 600, "factor": 2.0},
        **{k: v for k, v in over.items() if k != "backoff"},
        **({"backoff": {"initial_seconds": 30, "max_seconds": 600, "factor": 2.0,
                        **(over.get("backoff") or {})}}
            if over.get("backoff") else {}),
    })
    monkeypatch.setattr(rate_governor, "load_rate_limit", lambda *a, **k: cfg)
    return cfg


def _setup(tmp_path, monkeypatch, captures, tasks=None, agents=None,
           alive=True, attention=None, rate_limit_over=None):
    fb = FakeBackend(captures, alive=alive)
    monkeypatch.setattr(orch, "_backend", fb)
    monkeypatch.setattr(fleet, "STATE_DIR", tmp_path)
    monkeypatch.setattr(fleet, "TASKS_FILE", tmp_path / "tasks.json")
    monkeypatch.setattr(fleet, "TASK_LOGS", tmp_path / "logs")
    monkeypatch.setattr(fleet, "agent_attention", attention or (lambda name: False))
    # Force the TUI-ready gate true so the dispatch step doesn't skip on the
    # READY marker, which FakeBackend doesn't render. This matches how
    # test_dispatcher.py exercises the dispatch path indirectly.
    monkeypatch.setattr(fleet, "agent_ready", lambda name: True)
    agents = agents or [{"name": "a", "role": "", "project_dir": str(tmp_path)}]
    monkeypatch.setattr(fleet, "load_config", lambda *a, **k: agents)
    monkeypatch.setattr(fleet, "ensure_agent", lambda ag: "")
    _stub_rate_limit(monkeypatch, **(rate_limit_over or {}))
    fleet._save_tasks({"next_id": 99, "tasks": tasks if tasks is not None
                       else [_running_task()]})
    return fleet.Dispatcher(agents), fb


# --- T-02 done criteria #1: an injected 429 line marks the task ``retrying`` (not failed). ---
def test_injected_429_transitions_to_retrying_not_failed(tmp_path, monkeypatch):
    disp, _fb = _setup(
        tmp_path, monkeypatch,
        captures=["HTTP 429 Too Many Requests — please retry"],
    )
    disp.tick()
    t = fleet._load_tasks()["tasks"][0]
    assert t["status"] == "retrying"
    assert t["status"] != "failed"
    assert t.get("rate_retries") == 1
    assert t.get("retry_at"), "retry_at must be set so the dispatcher knows when to re-queue"
    assert "rate-limited" in (t.get("log") or "")


def test_429_without_saw_busy_is_ignored(tmp_path, monkeypatch):
    # Stale 429 text in the buffer right after re-queue (saw_busy=False) must
    # NOT trigger an immediate second retry — that would loop forever.
    task = _running_task(saw_busy=False)
    disp, _fb = _setup(
        tmp_path, monkeypatch,
        captures=["HTTP 429 Too Many Requests"],
        tasks=[task],
    )
    disp.tick()
    t = fleet._load_tasks()["tasks"][0]
    assert t["status"] == "running"        # not flipped to retrying
    assert "rate_retries" not in t or t["rate_retries"] == 0


def test_retrying_task_requeues_when_retry_at_elapsed(tmp_path, monkeypatch):
    # A task whose retry_at is already in the past must re-queue to ``pending``
    # on the next tick, then dispatch (status -> running) once the agent reads
    # ready.
    task = _running_task()
    task.update(status="retrying", retry_at="2000-01-01T00:00:00Z",
                rate_retries=1)
    disp, fb = _setup(
        tmp_path, monkeypatch,
        captures=["esc to interrupt"],     # for the dispatch path's downstream ticks
        tasks=[task],
    )
    disp.tick()
    t = fleet._load_tasks()["tasks"][0]
    # Single tick: re-queued AND dispatched in the same pass (cap not hit,
    # stagger=0, agent_ready stub returns True).
    assert t["status"] == "running"
    assert t.get("retry_at") is None
    # rate_retries is preserved so successive 429s grow the backoff.
    assert t.get("rate_retries") == 1
    # Kickoff was actually sent to the agent.
    assert any(name == "a" for name, _msg in fb.sent)


def test_retrying_task_holds_until_retry_at(tmp_path, monkeypatch):
    # retry_at in the future -> tick must NOT re-queue yet.
    task = _running_task()
    task.update(status="retrying", retry_at="9999-12-31T23:59:59Z",
                rate_retries=1)
    disp, _fb = _setup(tmp_path, monkeypatch, captures=[""], tasks=[task])
    disp.tick()
    t = fleet._load_tasks()["tasks"][0]
    assert t["status"] == "retrying"        # still sleeping
    assert t.get("retry_at") == "9999-12-31T23:59:59Z"


# --- T-02 done criteria #2: queuing more tasks than the cap leaves extras pending. ---
def test_max_concurrent_caps_running_tasks(tmp_path, monkeypatch):
    agents = [
        {"name": "a", "role": "", "project_dir": str(tmp_path)},
        {"name": "b", "role": "", "project_dir": str(tmp_path)},
        {"name": "c", "role": "", "project_dir": str(tmp_path)},
    ]
    # Three pending tasks, one per agent. Cap = 2 -> only two should advance.
    tasks = [_pending_task("t-1", "a"),
             _pending_task("t-2", "b"),
             _pending_task("t-3", "c")]
    disp, _fb = _setup(
        tmp_path, monkeypatch,
        captures=["esc to interrupt"],
        tasks=tasks, agents=agents,
        rate_limit_over={"max_concurrent": 2, "spawn_stagger_seconds": 0},
    )
    disp.tick()
    by_id = {t["id"]: t for t in fleet._load_tasks()["tasks"]}
    statuses = sorted(t["status"] for t in by_id.values())
    assert statuses == ["pending", "running", "running"]
    # The two earliest pending tasks should be the ones running (FIFO).
    assert by_id["t-1"]["status"] == "running"
    assert by_id["t-2"]["status"] == "running"
    assert by_id["t-3"]["status"] == "pending"


def test_spawn_stagger_limits_to_one_spawn_per_tick(tmp_path, monkeypatch):
    # Two ready agents and two pending tasks with a stagger > 0: at most one
    # task should transition to ``running`` in a single tick.
    agents = [
        {"name": "a", "role": "", "project_dir": str(tmp_path)},
        {"name": "b", "role": "", "project_dir": str(tmp_path)},
    ]
    tasks = [_pending_task("t-1", "a"), _pending_task("t-2", "b")]
    disp, _fb = _setup(
        tmp_path, monkeypatch,
        captures=["esc to interrupt"],
        tasks=tasks, agents=agents,
        rate_limit_over={"max_concurrent": 5, "spawn_stagger_seconds": 30},
    )
    disp.tick()
    by_id = {t["id"]: t for t in fleet._load_tasks()["tasks"]}
    running = [tid for tid, t in by_id.items() if t["status"] == "running"]
    pending = [tid for tid, t in by_id.items() if t["status"] == "pending"]
    assert len(running) == 1
    assert len(pending) == 1
