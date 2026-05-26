import os
import re
import common


def test_name_re_accepts_valid_names():
    assert common.NAME_RE.match("web-1")
    assert common.NAME_RE.match("Agent_2.test")

def test_name_re_rejects_invalid_names():
    assert not common.NAME_RE.match("-leading")
    assert not common.NAME_RE.match("has space")
    assert not common.NAME_RE.match("bad/slash")

def test_now_is_utc_iso_z():
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", common._now())

def test_markers_present():
    assert common.DONE_MARKER == "worker-done"
    assert any("interrupt" in m for m in common.BUSY_MARKERS)
    assert any("shortcuts" in m or "shift+tab" in m for m in common.READY_MARKERS)


def test_clean_child_env_strips_venv_path(monkeypatch):
    """FIX 3: PATH entries with trailing backslash / different casing are stripped."""
    venv = r"C:\proj\.venv"
    scripts = r"C:\proj\.venv\Scripts"
    other = r"C:\Windows\System32"

    # trailing backslash on the Scripts entry
    path_with_trailing = os.pathsep.join([scripts + "\\", other])
    monkeypatch.setenv("VIRTUAL_ENV", venv)
    monkeypatch.setenv("PATH", path_with_trailing)

    env = common.clean_child_env()
    path_parts = env["PATH"].split(os.pathsep)
    assert scripts not in path_parts, "Scripts dir with trailing backslash must be stripped"
    assert (scripts + "\\") not in path_parts, "trailing-backslash entry must be stripped"
    assert other in path_parts, "unrelated PATH entry must survive"
    assert "VIRTUAL_ENV" not in env, "VIRTUAL_ENV must be removed"


def test_clean_child_env_no_venv(monkeypatch):
    """When VIRTUAL_ENV is unset, PATH must pass through unchanged."""
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    original_path = r"C:\Windows\System32" + os.pathsep + r"C:\Windows"
    monkeypatch.setenv("PATH", original_path)

    env = common.clean_child_env()
    assert env["PATH"] == original_path
