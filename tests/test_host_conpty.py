import json
import socket
import sys
import time
import types
import pytest

import agent_host
import common


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


def test_write_status_records_child_pid(tmp_path):
    # The ConPTY child pid is recorded so the backend can kill the right tree.
    child = FakeChild()
    status = tmp_path / "status.json"
    host = agent_host.AgentHost(name="t", child=child, cols=40, rows=6,
                                status_path=status)
    host._pty = types.SimpleNamespace(pid=4321)
    host._write_status()
    data = json.loads(status.read_text())
    assert data["child_pid"] == 4321


def test_write_status_child_pid_none_without_pty(tmp_path):
    child = FakeChild()
    status = tmp_path / "status.json"
    host = agent_host.AgentHost(name="t", child=child, cols=40, rows=6,
                                status_path=status)
    host._write_status()
    data = json.loads(status.read_text())
    assert data["child_pid"] is None


def test_ready_gated_while_dialog_present():
    # A READY footer can be on screen while a trust/bypass dialog is still up;
    # readiness must wait until the dialog is gone so the kickoff isn't eaten.
    child = FakeChild()
    host = agent_host.AgentHost.for_test(child=child, cols=80, rows=10)
    host.screen.feed(
        ("Do you trust the files in this folder?\r\n"
         "? for shortcuts\r\n").encode())
    host.mark_ready_if_seen()
    assert host.ready is False


def test_ready_marks_once_dialog_settled():
    child = FakeChild()
    host = agent_host.AgentHost.for_test(child=child, cols=80, rows=10)
    # No trust/bypass markers on screen, only the READY footer -> ready.
    host.screen.feed(b"booting\r\n? for shortcuts\r\n")
    host.mark_ready_if_seen()
    assert host.ready is True


def test_bypass_marker_blocks_ready():
    # Sanity: the bypass option text on screen also gates readiness.
    child = FakeChild()
    host = agent_host.AgentHost.for_test(child=child, cols=80, rows=10)
    host.screen.feed(
        ("2. Yes, I accept\r\n"
         "shift+tab to cycle\r\n").encode())
    host.mark_ready_if_seen()
    assert host.ready is False
    assert common.BYPASS_MARKER == "yes, i accept"


def test_key_to_bytes_regular_and_control():
    assert agent_host._key_to_bytes("a") == "a"
    assert agent_host._key_to_bytes("\r") == "\r"
    assert agent_host._key_to_bytes("\n") == "\r"
    assert agent_host._key_to_bytes("\x08") == "\x7f"   # Backspace -> DEL


@pytest.mark.parametrize("prefix", ["\x00", "\xe0"])
def test_key_to_bytes_arrows(prefix):
    assert agent_host._key_to_bytes(prefix, "H") == "\x1b[A"   # Up
    assert agent_host._key_to_bytes(prefix, "P") == "\x1b[B"   # Down
    assert agent_host._key_to_bytes(prefix, "M") == "\x1b[C"   # Right
    assert agent_host._key_to_bytes(prefix, "K") == "\x1b[D"   # Left


def test_key_to_bytes_unknown_special_dropped():
    assert agent_host._key_to_bytes("\x00", "\x99") == ""


def test_forward_console_input_feeds_child(monkeypatch):
    # Fake msvcrt that yields keystrokes then raises to end the loop:
    # h, i, Enter, Up-arrow (prefix + 'H').
    keys = iter(["h", "i", "\r", "\x00", "H"])

    class FakeMsvcrt:
        @staticmethod
        def getwch():
            try:
                return next(keys)
            except StopIteration:
                raise EOFError
    monkeypatch.setitem(sys.modules, "msvcrt", FakeMsvcrt)
    child = FakeChild()
    host = agent_host.AgentHost.for_test(child=child)
    agent_host._forward_console_input(host)   # returns when getwch raises
    assert "".join(child.written) == "hi\r\x1b[A"


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
