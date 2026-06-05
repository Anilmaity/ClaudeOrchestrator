import io
import queue

import agent_host


class FakeChild:
    def __init__(self):
        self.written = []
        self.alive = True
    def write(self, text): self.written.append(text)
    def isalive(self): return self.alive


def test_subscribe_seeds_current_screen():
    host = agent_host.AgentHost.for_test(child=FakeChild(), cols=40, rows=6)
    host.screen.feed(b"hello world\r\n")
    q = host.subscribe()
    seeded = q.get_nowait()
    assert "hello world" in seeded
    host.unsubscribe(q)


def test_publish_screen_pushes_only_on_change():
    host = agent_host.AgentHost.for_test(child=FakeChild(), cols=40, rows=6)
    q = host.subscribe()
    q.get_nowait()                       # drain the seed frame
    host.screen.feed(b"line one\r\n")
    host.publish_screen()
    assert "line one" in q.get_nowait()
    # No screen change -> nothing new published.
    host.publish_screen()
    assert q.empty()


def test_unsubscribe_stops_delivery():
    host = agent_host.AgentHost.for_test(child=FakeChild(), cols=40, rows=6)
    q = host.subscribe()
    host.unsubscribe(q)
    host.screen.feed(b"after\r\n")
    host.publish_screen()
    # Drain whatever was seeded; no NEW frame should arrive after unsubscribe.
    try:
        while True:
            q.get_nowait()
    except queue.Empty:
        pass
    assert q.empty()


def test_offer_drops_oldest_when_full():
    q = queue.Queue(maxsize=2)
    agent_host._offer(q, "a")
    agent_host._offer(q, "b")
    agent_host._offer(q, "c")            # full -> drop "a"
    assert list(q.queue) == ["b", "c"]


def test_write_frame_length_prefixes_payload():
    buf = io.BytesIO()
    agent_host._write_frame(buf, b"screen text")
    assert buf.getvalue() == b"11\nscreen text"


def test_write_frame_zero_length_heartbeat():
    buf = io.BytesIO()
    agent_host._write_frame(buf, b"")
    assert buf.getvalue() == b"0\n"


import io as _io

import backend as backend_mod
import win_backend


class FakeBackend(backend_mod.Backend):
    """Drives the base-class poll loop from a scripted capture sequence."""
    def __init__(self, screens, alive_calls):
        self._screens = list(screens)
        self._alive = alive_calls
    def worker_exists(self, name):
        self._alive -= 1
        return self._alive >= 0
    def capture(self, name, lines=200):
        return self._screens.pop(0) if self._screens else ""


def test_base_stream_screen_yields_changes_and_heartbeat(monkeypatch):
    # Heartbeat threshold 0 => any unchanged poll emits HEARTBEAT; poll sleep 0.
    monkeypatch.setattr(backend_mod, "STREAM_HEARTBEAT_SECS", 0)
    monkeypatch.setattr(backend_mod, "STREAM_POLL_SECS", 0)
    monkeypatch.setattr(backend_mod.time, "sleep", lambda *_: None)
    fb = FakeBackend(screens=["A", "A", "B"], alive_calls=3)
    out = list(fb.stream_screen("x"))
    assert out[0] == "A"                       # first screen (changed from None)
    assert out[1] is backend_mod.HEARTBEAT     # unchanged poll -> heartbeat
    assert out[2] == "B"                        # changed again


def test_iter_frames_parses_payloads_and_heartbeats():
    framed = b"5\nhello0\n3\nbye"
    out = list(win_backend._iter_frames(_io.BytesIO(framed)))
    assert out[0] == "hello"
    assert out[1] is backend_mod.HEARTBEAT
    assert out[2] == "bye"


def test_iter_frames_stops_at_eof():
    out = list(win_backend._iter_frames(_io.BytesIO(b"")))
    assert out == []
