"""Shared constants and tiny helpers for the orchestrator and its backends.

Extracted from orch.py so backends can import them without importing orch
(which would create an import cycle: orch -> backend -> tmux/win backend).
"""
from __future__ import annotations

import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

SESSION = "corch"
STATE_DIR = Path.home() / ".claude-orch"
TASKS_DIR = STATE_DIR / "tasks"
REGISTRY = STATE_DIR / "workers.json"

# Footer text Claude's TUI shows once the input box is ready for typing.
READY_MARKERS = ("shift+tab to cycle", "for shortcuts")
# Text shown while Claude is actively working on a turn.
BUSY_MARKERS = ("esc to interrupt", "interrupt)")
# Folder-trust dialog we auto-accept (default option = trust).
TRUST_MARKERS = (
    "do you trust", "trust the files", "trust this folder",
    "is this a project you", "quick safety check",
)
# One-time "Bypass Permissions mode" acceptance; option 2 is "Yes, I accept".
BYPASS_MARKER = "yes, i accept"
# Marker we instruct workers to print when finished.
DONE_MARKER = "worker-done"

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _die(msg: str, code: int = 1):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(code)


def clean_child_env() -> dict:
    """Environment for launching claude: claude refuses to start inside a venv,
    so strip VIRTUAL_ENV and its bin/Scripts dir from PATH."""
    env = dict(os.environ)
    venv = env.pop("VIRTUAL_ENV", None)
    if venv:
        parts = env.get("PATH", "").split(os.pathsep)
        drop = {
            str(Path(venv) / "bin").lower().rstrip("/\\"),
            str(Path(venv) / "Scripts").lower().rstrip("/\\"),
        }
        env["PATH"] = os.pathsep.join(
            p for p in parts
            if p.lower().rstrip("/\\") not in drop
        )
    return env
