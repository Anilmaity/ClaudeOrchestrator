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
