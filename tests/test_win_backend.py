import json
import socket
import socketserver
import threading
from pathlib import Path

import win_backend


class _StubHandler(socketserver.StreamRequestHandler):
    def handle(self):
        line = self.rfile.readline().decode().strip()
        cmd, _, arg = line.partition(" ")
        store = self.server.store
        if cmd == "PING":
            self.wfile.write(b"OK")
        elif cmd == "STATE":
            self.wfile.write(json.dumps({"ready": True, "alive": True}).encode())
        elif cmd == "CAPTURE":
            self.wfile.write(b"esc to interrupt")
        elif cmd == "SEND":
            store.append(arg); self.wfile.write(b"OK")
        elif cmd == "STOP":
            self.wfile.write(b"OK")


def _stub_server():
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _StubHandler)
    srv.store = []
    srv.allow_reuse_address = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _write_status(tmp_state, name, port, child_pid=None):
    d = tmp_state / "win" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "status.json").write_text(json.dumps(
        {"name": name, "pid": 999999, "port": port, "child_pid": child_pid,
         "ready": True}))


class _KillableHandler(socketserver.StreamRequestHandler):
    def handle(self):
        line = self.rfile.readline().decode().strip()
        cmd, _, _ = line.partition(" ")
        srv = self.server
        if cmd == "PING":
            self.wfile.write(b"OK")
        elif cmd == "STOP":
            self.wfile.write(b"OK")
            if srv.die_on_stop:
                # Simulate the worker process dying: tear the socket down so
                # subsequent PINGs are refused (matching a real killed host).
                threading.Thread(target=srv.shutdown, daemon=True).start()


def _killable_server(die_on_stop=True):
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _KillableHandler)
    srv.die_on_stop = die_on_stop
    srv.allow_reuse_address = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_capture_send_exists(tmp_path, monkeypatch):
    monkeypatch.setattr(win_backend.common, "STATE_DIR", tmp_path)
    srv = _stub_server()
    port = srv.server_address[1]
    _write_status(tmp_path, "w1", port)
    b = win_backend.WinBackend()
    try:
        assert b.worker_exists("w1") is True
        assert "interrupt" in b.capture("w1", 50)
        b.send_text("w1", "hello there")
        assert srv.store == ["hello there"]
        assert b.list_workers() == ["w1"]
        assert b.session_exists() is True
    finally:
        srv.shutdown()


def test_missing_worker_is_gone(tmp_path, monkeypatch):
    monkeypatch.setattr(win_backend.common, "STATE_DIR", tmp_path)
    b = win_backend.WinBackend()
    assert b.worker_exists("ghost") is False
    assert b.list_workers() == []


def test_spawn_launched_but_not_ready_returns_false_no_error(tmp_path, monkeypatch):
    monkeypatch.setattr(win_backend.common, "STATE_DIR", tmp_path)
    monkeypatch.setattr(win_backend.WinBackend, "available", lambda self: True)
    def fake_popen(args, **kw):
        name = args[2]
        p = tmp_path / "win" / name / "status.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"name": name, "pid": 1, "port": 1, "ready": False}))
        return type("P", (), {})()
    monkeypatch.setattr(win_backend.subprocess, "Popen", fake_popen)
    ready, err = win_backend.WinBackend().spawn("w2", str(tmp_path), ready_timeout=0.5)
    assert ready is False
    assert err == ""

def test_spawn_host_never_starts_returns_error(tmp_path, monkeypatch):
    monkeypatch.setattr(win_backend.common, "STATE_DIR", tmp_path)
    monkeypatch.setattr(win_backend.WinBackend, "available", lambda self: True)
    monkeypatch.setattr(win_backend.subprocess, "Popen",
                        lambda *a, **k: type("P", (), {})())
    ready, err = win_backend.WinBackend().spawn("w3", str(tmp_path), ready_timeout=0.5)
    assert ready is False
    assert "failed to start" in err


def test_kill_taskkills_both_trees_and_unlinks_when_dead(tmp_path, monkeypatch):
    monkeypatch.setattr(win_backend.common, "STATE_DIR", tmp_path)
    calls = []
    monkeypatch.setattr(win_backend.subprocess, "run",
                        lambda args, **kw: calls.append(args) or
                        type("R", (), {"returncode": 0})())
    srv = _killable_server(die_on_stop=True)
    port = srv.server_address[1]
    _write_status(tmp_path, "w1", port, child_pid=12345)
    b = win_backend.WinBackend()
    b.kill("w1")
    # both the child tree (12345) and host tree (999999) get a taskkill
    killed = {args[2] for args in calls if args[:2] == ["taskkill", "/PID"]}
    assert "12345" in killed and "999999" in killed
    # worker stopped responding -> status file removed
    assert not (tmp_path / "win" / "w1" / "status.json").exists()


def test_kill_keeps_status_when_survivor_still_responds(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(win_backend.common, "STATE_DIR", tmp_path)
    # taskkill is a no-op (the survivor ignores it); the socket keeps answering.
    monkeypatch.setattr(win_backend.subprocess, "run",
                        lambda args, **kw: type("R", (), {"returncode": 0})())
    srv = _killable_server(die_on_stop=False)
    port = srv.server_address[1]
    _write_status(tmp_path, "w1", port, child_pid=12345)
    b = win_backend.WinBackend()
    try:
        b.kill("w1")
        # still responding -> status file left in place, warning emitted
        assert (tmp_path / "win" / "w1" / "status.json").exists()
        assert "still responding" in capsys.readouterr().err
    finally:
        srv.shutdown()
