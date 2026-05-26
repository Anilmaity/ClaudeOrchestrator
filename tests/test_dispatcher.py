"""Dispatcher.tick state-machine tests, focused on the completion debounce:
a single transient idle reading must NOT mark a running task done; only
CONFIRM_IDLE consecutive idle reads should."""
import fleet
import orch


class FakeBackend:
    """Drives agent_activity via scripted CAPTURE outputs (one per read)."""
    def __init__(self, captures):
        self.captures = list(captures)
        self.i = 0

    def available(self):
        return True

    def worker_exists(self, name):
        return True

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
        "tries": 1, "log": "",
    }


def _setup(tmp_path, monkeypatch, captures):
    fb = FakeBackend(captures)
    monkeypatch.setattr(orch, "_backend", fb)
    monkeypatch.setattr(fleet, "STATE_DIR", tmp_path)
    monkeypatch.setattr(fleet, "TASKS_FILE", tmp_path / "tasks.json")
    monkeypatch.setattr(fleet, "TASK_LOGS", tmp_path / "logs")
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
