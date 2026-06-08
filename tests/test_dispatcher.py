"""Dispatcher.tick state-machine tests, focused on the completion debounce:
a single transient idle reading must NOT mark a running task done; only
CONFIRM_IDLE consecutive idle reads should."""
import fleet
import orch


class FakeBackend:
    """Drives agent_activity via scripted CAPTURE outputs (one per read)."""
    def __init__(self, captures, alive=True):
        self.captures = list(captures)
        self.i = 0
        self.alive = alive

    def available(self):
        return True

    def worker_exists(self, name):
        return self.alive

    def capture(self, name, lines=200):
        v = self.captures[min(self.i, len(self.captures) - 1)]
        self.i += 1
        return v

    def send_text(self, name, text):
        pass

    def spawn(self, *a, **k):
        return True, ""

    def kill(self, name):
        pass

    def kill_all(self):
        pass


def _running_task():
    return {
        "id": "t-1", "agent": "a", "description": "x", "status": "running",
        "created_at": fleet._now(), "started_at": fleet._now(),
        "finished_at": None, "saw_busy": False, "idle_seen": 0,
        "tries": 1, "needs_attention": False, "log": "",
    }


def _setup(tmp_path, monkeypatch, captures, alive=True, attention=None):
    fb = FakeBackend(captures, alive=alive)
    monkeypatch.setattr(orch, "_backend", fb)
    monkeypatch.setattr(fleet, "STATE_DIR", tmp_path)
    monkeypatch.setattr(fleet, "TASKS_FILE", tmp_path / "tasks.json")
    monkeypatch.setattr(fleet, "TASK_LOGS", tmp_path / "logs")
    # agent_attention does its own terminal capture; stub it so it doesn't
    # consume the scripted activity captures. Tests exercising the attention
    # path pass their own callable.
    monkeypatch.setattr(fleet, "agent_attention", attention or (lambda name: False))
    agents = [{"name": "a", "role": "", "project_dir": str(tmp_path)}]
    monkeypatch.setattr(fleet, "load_config", lambda *a, **k: agents)
    monkeypatch.setattr(fleet, "ensure_agent", lambda ag: "")
    fleet._save_tasks({"next_id": 2, "tasks": [_running_task()]})
    return fleet.Dispatcher(agents)


def _status(tmp_path):
    return fleet._load_tasks()["tasks"][0]["status"]


def test_single_transient_idle_does_not_complete(tmp_path, monkeypatch):
    # busy, then one idle (a dropped/empty capture) -> must stay running.
    disp = _setup(tmp_path, monkeypatch, ["esc to interrupt", ""])
    disp.tick()                       # busy -> saw_busy
    disp.tick()                       # one idle -> idle_seen=1, NOT done
    assert _status(tmp_path) == "running"


def test_consecutive_idle_completes(tmp_path, monkeypatch):
    # busy, transient idle, busy again (resets), then two real idles -> done.
    disp = _setup(tmp_path, monkeypatch, [
        "esc to interrupt",   # busy   -> saw_busy
        "",                   # idle   -> idle_seen=1
        "esc to interrupt",   # busy   -> idle_seen reset to 0
        "idle prompt",        # idle   -> idle_seen=1
        "idle prompt",        # idle   -> idle_seen=2 == CONFIRM_IDLE -> done
    ])
    for _ in range(5):
        disp.tick()
    assert _status(tmp_path) == "done"


def test_write_task_log_handles_unicode(tmp_path, monkeypatch):
    # Captured TUI output contains box-drawing glyphs; the log must not crash
    # on the Windows default (cp1252) encoding.
    monkeypatch.setattr(fleet, "TASK_LOGS", tmp_path / "logs")
    text = "▐▛███▜▌ box TUI ▝▜█████▛▘"
    fleet._write_task_log("t-x", text)
    assert (tmp_path / "logs" / "t-x.log").read_text(encoding="utf-8") == text


def test_busy_task_survives_single_missed_ping(tmp_path, monkeypatch):
    # Agent looks "gone" (missed PING) but has a running task -> stay running.
    disp = _setup(tmp_path, monkeypatch, [], alive=False)
    disp.tick()
    assert _status(tmp_path) == "running"


def test_sustained_gone_fails_task(tmp_path, monkeypatch):
    disp = _setup(tmp_path, monkeypatch, [], alive=False)
    # T-06: a failure now retries first. Force max_retries=0 (via the loader
    # the dispatcher re-reads each tick) so the test still exercises the
    # immediate-failure contract it originally asserted.
    import rate_governor
    monkeypatch.setattr(rate_governor, "load_rate_limit",
                        lambda _p: {**rate_governor._merge(None), "max_retries": 0})
    for _ in range(fleet.CONFIRM_GONE):
        disp.tick()
    assert _status(tmp_path) == "failed"


def test_running_task_agent_not_respawned(tmp_path, monkeypatch):
    # Keep-alive must not respawn (displace) an agent that has a running task,
    # even when its window looks gone.
    disp = _setup(tmp_path, monkeypatch, [], alive=False)
    calls = []
    monkeypatch.setattr(fleet, "ensure_agent", lambda ag: calls.append(ag["name"]))
    disp.tick()
    assert "a" not in calls


def test_attention_holds_task_then_completes(tmp_path, monkeypatch):
    # Agent goes busy, then reads idle while blocked on a question for
    # >= CONFIRM_IDLE ticks -> stays running with needs_attention=True (NOT
    # done). When attention clears, CONFIRM_IDLE clean idles -> done, flag clear.
    attn = {"on": True}
    disp = _setup(
        tmp_path, monkeypatch,
        ["esc to interrupt"] + ["idle prompt"] * 12,
        attention=lambda name: attn["on"],
    )
    disp.tick()                                  # busy -> saw_busy
    for _ in range(fleet.CONFIRM_IDLE + 1):
        disp.tick()                              # idle but blocked on attention
    t = fleet._load_tasks()["tasks"][0]
    assert t["status"] == "running"
    assert t["needs_attention"] is True

    attn["on"] = False                           # human answered; agent resumes
    for _ in range(fleet.CONFIRM_IDLE):
        disp.tick()
    t = fleet._load_tasks()["tasks"][0]
    assert t["status"] == "done"
    assert t["needs_attention"] is False


def test_watchdog_fails_overrun_task_and_frees_agent(tmp_path, monkeypatch):
    # A running task older than TASK_TIMEOUT_SECS, the agent still showing the
    # busy marker, is failed and the agent freed -> a pending task for the same
    # agent then dispatches to running once the agent is ready.
    monkeypatch.setattr(fleet, "TASK_TIMEOUT_SECS", 1)
    disp = _setup(tmp_path, monkeypatch, ["esc to interrupt", "shift+tab to cycle"])
    # T-06: the watchdog failure now retries first. Force max_retries=0 (via
    # the loader the dispatcher re-reads each tick) so the original
    # "watchdog -> failed -> next pending dispatches" contract holds.
    import rate_governor
    monkeypatch.setattr(rate_governor, "load_rate_limit",
                        lambda _p: {**rate_governor._merge(None), "max_retries": 0})
    d = fleet._load_tasks()
    d["tasks"][0]["started_at"] = "2000-01-01T00:00:00Z"   # long overrun
    d["tasks"].append({
        "id": "t-2", "agent": "a", "description": "next", "status": "pending",
        "created_at": fleet._now(), "started_at": None, "finished_at": None,
        "saw_busy": False, "needs_attention": False, "log": "",
    })
    fleet._save_tasks(d)

    disp.tick()                                  # watchdog fires -> t-1 failed
    tasks = {t["id"]: t for t in fleet._load_tasks()["tasks"]}
    assert tasks["t-1"]["status"] == "failed"
    assert "exceeded max runtime" in tasks["t-1"]["log"]

    disp.tick()                                  # freed agent (now ready) -> t-2
    tasks = {t["id"]: t for t in fleet._load_tasks()["tasks"]}
    assert tasks["t-2"]["status"] == "running"
