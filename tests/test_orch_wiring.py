import orch

class FakeBackend:
    def __init__(self): self.sent = []
    def available(self): return True
    def install_hint(self): return "install"
    def session_exists(self): return True
    def list_workers(self): return ["w1"]
    def worker_exists(self, n): return n == "w1"
    def spawn(self, *a, **k): return True, ""
    def capture(self, n, lines=200): return "esc to interrupt"
    def send_text(self, n, t): self.sent.append((n, t))
    def kill(self, n): pass
    def kill_all(self): pass
    def set_scrollback(self, n): pass
    def attach_hint(self): return "hint"

def test_worker_state_uses_capture(monkeypatch):
    fb = FakeBackend()
    monkeypatch.setattr(orch, "_backend", fb)
    assert orch._worker_state("w1") == "busy"   # 'esc to interrupt'
    fb.capture = lambda n, lines=200: "worker-done: built it"
    assert orch._worker_state("w1") == "done"
    fb.capture = lambda n, lines=200: "idle prompt"
    assert orch._worker_state("w1") == "idle"
    assert orch._worker_state("nope") == "gone"


def test_done_marker_requires_line_start(monkeypatch):
    """FIX 1: kickoff message contains 'WORKER-DONE:' mid-sentence -> NOT done."""
    fb = FakeBackend()
    monkeypatch.setattr(orch, "_backend", fb)
    # Simulate scrollback that contains the kickoff instruction mid-sentence
    kickoff_cap = (
        "You are an autonomous worker. Read the file /tmp/task.md for your full "
        "task. When finished, print one line starting with 'WORKER-DONE:' followed "
        "by a short summary of what you changed."
    )
    fb.capture = lambda n, lines=200: kickoff_cap
    # The kickoff has 'worker-done:' mid-sentence (after lowercasing), must NOT be "done"
    assert orch._worker_state("w1") == "idle"

    # A line that starts with the marker (after stripping) -> must be "done"
    fb.capture = lambda n, lines=200: "some prior output\nWORKER-DONE: built it\nmore output"
    assert orch._worker_state("w1") == "done"

    # Indented marker line (lstrip should handle it) -> "done"
    fb.capture = lambda n, lines=200: "  WORKER-DONE: finished"
    assert orch._worker_state("w1") == "done"

def test_send_delegates(monkeypatch):
    fb = FakeBackend()
    monkeypatch.setattr(orch, "_backend", fb)
    orch._send_text("w1", "hello")
    assert fb.sent == [("w1", "hello")]

import fleet

def test_fleet_uses_backend_for_activity(monkeypatch):
    fb = FakeBackend()
    monkeypatch.setattr(orch, "_backend", fb)
    # agent_activity reads orch._capture -> backend.capture ('esc to interrupt')
    assert fleet.agent_activity("w1") == "busy"
    fb.capture = lambda n, lines=200: "? for shortcuts"
    assert fleet.agent_activity("w1") == "idle"
