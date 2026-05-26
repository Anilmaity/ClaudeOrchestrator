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

def test_send_delegates(monkeypatch):
    fb = FakeBackend()
    monkeypatch.setattr(orch, "_backend", fb)
    orch._send_text("w1", "hello")
    assert fb.sent == [("w1", "hello")]
