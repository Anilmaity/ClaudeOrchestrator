import json
import socket
import sys
import time
import pytest

import agent_host


class FakeChild:
    """Stand-in for the ConPTY child: records writes, reports alive."""
    def __init__(self):
        self.written = []
        self.alive = True
    def write(self, text): self.written.append(text)
    def isalive(self): return self.alive


def _send(port, line):
    with socket.create_connection(("127.0.0.1", port), timeout=2) as s:
        s.sendall(line.encode() + b"\n")
        chunks = []
        while True:
            b = s.recv(65536)
            if not b:
                break
            chunks.append(b)
        return b"".join(chunks).decode()


def test_control_protocol_roundtrip():
    child = FakeChild()
    host = agent_host.AgentHost.for_test(child=child, cols=40, rows=6)
    host.screen.feed(b"booting\r\n? for shortcuts\r\n")
    host.mark_ready_if_seen()
    port = host.start_server()
    try:
        assert _send(port, "PING").strip() == "OK"
        st = json.loads(_send(port, "STATE"))
        assert st["ready"] is True
        assert "for shortcuts" in _send(port, "CAPTURE 50")
        assert _send(port, "SEND do the thing").strip() == "OK"
        assert child.written and child.written[-1].endswith("\r")
        assert "do the thing" in child.written[-1]
    finally:
        host.stop_server()


winpty_missing = False
try:
    import winpty  # noqa: F401
except Exception:
    winpty_missing = True


@pytest.mark.skipif(sys.platform != "win32" or winpty_missing,
                    reason="needs Windows + pywinpty")
def test_conpty_runs_child_and_captures():
    from pathlib import Path
    # Spawn a real child under ConPTY that prints a known marker then lingers,
    # and confirm the host's screen captures it. Uses cmd, not claude.
    host = agent_host.spawn_conpty(
        name="t",
        cmd=["cmd", "/c", "echo HELLO_CONPTY & ping -n 4 127.0.0.1 >nul"],
        cwd=str(Path.cwd()), cols=80, rows=24)
    host.start_pump(background=True)
    port = host.start_server()
    try:
        deadline = time.time() + 8
        seen = ""
        while time.time() < deadline:
            seen = _send(port, "CAPTURE 50")
            if "HELLO_CONPTY" in seen:
                break
            time.sleep(0.2)
        assert "HELLO_CONPTY" in seen
    finally:
        _send(port, "STOP")
        time.sleep(0.5)
