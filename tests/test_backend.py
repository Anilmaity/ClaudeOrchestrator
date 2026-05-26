import importlib
import backend

def test_env_override_selects_named_backend(monkeypatch):
    monkeypatch.setenv("ORCH_BACKEND", "tmux")
    importlib.reload(backend)
    b = backend.get_backend()
    assert type(b).__name__ == "TmuxBackend"

def test_default_is_win_on_win32(monkeypatch):
    monkeypatch.delenv("ORCH_BACKEND", raising=False)
    monkeypatch.setattr(backend.sys, "platform", "win32")
    b = backend.get_backend()
    assert type(b).__name__ == "WinBackend"

def test_default_is_tmux_off_win32(monkeypatch):
    monkeypatch.delenv("ORCH_BACKEND", raising=False)
    monkeypatch.setattr(backend.sys, "platform", "linux")
    b = backend.get_backend()
    assert type(b).__name__ == "TmuxBackend"

def test_unknown_backend_raises(monkeypatch):
    monkeypatch.setenv("ORCH_BACKEND", "nope")
    with __import__("pytest").raises(ValueError):
        backend.get_backend()
