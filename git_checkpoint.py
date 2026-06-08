"""Per-task git checkpointing helpers.

Each dispatched task gets its own branch (``orch/<task-id>``) inside the agent's
``project_dir`` so the worker's --dangerously-skip-permissions changes land on
that branch and can be reviewed or reverted as one unit. The base SHA is
captured at dispatch time, the head SHA on the task's terminal-state transition
(done/failed/canceled), and the pair is exposed to the dashboard as a diff
range — the actual diff viewer is Phase 3.

All helpers are *best-effort*: they never raise. Callers that get ``None`` for
``branch`` or a SHA should treat the task as "no git context" (project_dir is
not a repo, git is missing, working tree is in a state we can't checkout into,
etc.) and simply skip the diff link.
"""
from __future__ import annotations

import subprocess
from pathlib import Path


# Timeout for every git invocation. The dispatcher runs these inline on its
# tick thread, so a wedged git process must not stall the whole fleet.
_GIT_TIMEOUT = 10


def _git(project_dir: str, *args: str) -> tuple[int, str, str]:
    """Run ``git *args`` inside ``project_dir``.

    Returns ``(returncode, stdout, stderr)``. A timeout / missing-binary /
    exec error is mapped to ``(-1, "", reason)`` so callers can branch on the
    return code without try/except.
    """
    try:
        r = subprocess.run(
            ["git", *args], cwd=project_dir,
            capture_output=True, text=True, timeout=_GIT_TIMEOUT,
        )
        return r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip()
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as e:
        return -1, "", str(e)


def is_git_repo(project_dir: str) -> bool:
    """True iff ``project_dir`` is inside a git work tree we can talk to."""
    if not project_dir:
        return False
    p = Path(project_dir)
    if not p.is_dir():
        return False
    rc, out, _ = _git(project_dir, "rev-parse", "--is-inside-work-tree")
    return rc == 0 and out == "true"


def head_sha(project_dir: str) -> str | None:
    """Return the current HEAD SHA, or ``None`` if unavailable."""
    if not is_git_repo(project_dir):
        return None
    rc, out, _ = _git(project_dir, "rev-parse", "HEAD")
    if rc != 0 or not out:
        return None
    return out


def branch_name(task_id: str) -> str:
    """The branch name we use for a task: ``orch/<task-id>``."""
    return f"orch/{task_id}"


def create_task_branch(project_dir: str, task_id: str
                       ) -> tuple[str | None, str | None]:
    """Create-and-check-out ``orch/<task-id>`` from HEAD inside ``project_dir``.

    Returns ``(branch, base_sha)``. Both are ``None`` when ``project_dir`` is
    not a git repo, or when the branch could not be created for any reason
    (existing branch with same name, dirty working tree blocking checkout,
    detached HEAD without commits, etc.). Best-effort: no exception escapes.
    """
    if not is_git_repo(project_dir):
        return None, None
    sha = head_sha(project_dir)
    if sha is None:
        # Repo with no commits yet — nothing to base a branch on.
        return None, None
    branch = branch_name(task_id)
    rc, _, err = _git(project_dir, "checkout", "-b", branch, sha)
    if rc != 0:
        # Most common cause: the branch already exists (task re-dispatch after
        # a crash). Try a plain checkout so the worker still lands on it; if
        # that also fails we give up.
        rc2, _, _ = _git(project_dir, "checkout", branch)
        if rc2 != 0:
            return None, None
    return branch, sha
