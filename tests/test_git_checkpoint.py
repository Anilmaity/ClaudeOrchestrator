"""Tests for the T-03 per-task git checkpointing path.

Covers:
  * ``git_checkpoint`` helpers against a real temp git repo;
  * a no-repo project_dir transparently downgrades to (None, None);
  * the dispatcher records ``branch`` + ``base_sha`` on dispatch and
    ``head_sha`` on the terminal done/failed transition for the same task.

Each test that exercises git uses a temp repo (tmp_path) and runs real git
commands — skipped when the git binary isn't on PATH.
"""
from __future__ import annotations

import shutil
import subprocess

import pytest

import fleet
import git_checkpoint
import orch
import worker_status


pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git binary required"
)


# --------------------------------------------------------------------------- #
# tiny test doubles
# --------------------------------------------------------------------------- #
class _BackendStub:
    """Drives orch._capture / worker_exists with scripted values."""
    def __init__(self, alive=True, captures=("",)):
        self.alive = alive
        self.captures = list(captures)
        self._i = 0

    def available(self): return True
    def install_hint(self): return ""
    def session_exists(self): return True
    def list_workers(self): return ["a"]
    def worker_exists(self, n): return self.alive
    def spawn(self, *a, **k): return True, ""
    def kill(self, n): pass
    def kill_all(self): pass
    def set_scrollback(self, n): pass
    def attach_hint(self): return ""

    def capture(self, name, lines=200):
        v = self.captures[min(self._i, len(self.captures) - 1)]
        self._i += 1
        return v

    def send_text(self, name, text): pass


def _init_repo(path) -> str:
    """Init a repo at ``path`` with one commit; return the HEAD SHA."""
    def run(*args):
        subprocess.run(["git", *args], cwd=path, check=True,
                       capture_output=True, text=True)
    run("init", "-q", "-b", "main")
    run("config", "user.email", "test@example.com")
    run("config", "user.name", "test")
    (path / "README.md").write_text("hello\n")
    run("add", "README.md")
    run("commit", "-q", "-m", "init")
    r = subprocess.run(["git", "rev-parse", "HEAD"], cwd=path,
                       capture_output=True, text=True, check=True)
    return r.stdout.strip()


def _add_commit(path, msg="work") -> str:
    """Create one more commit so HEAD advances; return new HEAD SHA."""
    f = path / "work.txt"
    f.write_text((f.read_text() if f.exists() else "") + msg + "\n")
    subprocess.run(["git", "add", "work.txt"], cwd=path, check=True,
                   capture_output=True, text=True)
    subprocess.run(["git", "commit", "-q", "-m", msg], cwd=path, check=True,
                   capture_output=True, text=True)
    r = subprocess.run(["git", "rev-parse", "HEAD"], cwd=path,
                       capture_output=True, text=True, check=True)
    return r.stdout.strip()


# --------------------------------------------------------------------------- #
# git_checkpoint module
# --------------------------------------------------------------------------- #
def test_is_git_repo_true_and_false(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    plain = tmp_path / "plain"
    plain.mkdir()
    assert not git_checkpoint.is_git_repo(str(repo))
    _init_repo(repo)
    assert git_checkpoint.is_git_repo(str(repo))
    assert not git_checkpoint.is_git_repo(str(plain))
    assert not git_checkpoint.is_git_repo("")


def test_create_task_branch_returns_branch_and_base_sha(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    base = _init_repo(repo)
    branch, sha = git_checkpoint.create_task_branch(str(repo), "t-0042")
    assert branch == "orch/t-0042"
    assert sha == base
    # The working tree is actually on the new branch.
    r = subprocess.run(["git", "branch", "--show-current"], cwd=repo,
                       capture_output=True, text=True, check=True)
    assert r.stdout.strip() == "orch/t-0042"


def test_create_task_branch_no_repo_returns_none(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert git_checkpoint.create_task_branch(str(plain), "t-1") == (None, None)


# --------------------------------------------------------------------------- #
# dispatcher end-to-end against a real repo
# --------------------------------------------------------------------------- #
def _setup_dispatcher(tmp_path, monkeypatch, repo_path):
    """Wire the dispatcher to a fake backend + tmp state dir + the temp repo."""
    monkeypatch.setattr(worker_status, "STATUS_DIR", tmp_path / "status")
    monkeypatch.setattr(fleet, "STATE_DIR", tmp_path)
    monkeypatch.setattr(fleet, "TASKS_FILE", tmp_path / "tasks.json")
    monkeypatch.setattr(fleet, "TASK_LOGS", tmp_path / "logs")
    monkeypatch.setattr(fleet, "agent_attention", lambda name: False)
    monkeypatch.setattr(fleet, "ensure_agent", lambda ag: "")
    monkeypatch.setattr(fleet, "agent_ready", lambda name: True)
    fb = _BackendStub(alive=True, captures=("? for shortcuts",))
    monkeypatch.setattr(orch, "_backend", fb)
    agents = [{"name": "a", "role": "", "project_dir": str(repo_path),
               "project": "", "manager_of": ""}]
    monkeypatch.setattr(fleet, "load_config", lambda *a, **k: agents)
    return fleet.Dispatcher(agents)


def test_dispatcher_records_branch_base_sha_head_sha(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    base = _init_repo(repo)
    disp = _setup_dispatcher(tmp_path, monkeypatch, repo)

    tid = fleet.add_task("a", "do something")
    # Tick 1: pending -> running; branch is created.
    disp.tick()
    t = next(x for x in fleet._load_tasks()["tasks"] if x["id"] == tid)
    assert t["status"] == "running"
    assert t["branch"] == f"orch/{tid}"
    assert t["base_sha"] == base
    assert t["head_sha"] is None

    # Worker "commits" on the branch (simulated by advancing HEAD), then the
    # worker_status file flips state=done with the task id — the dispatcher's
    # next tick should close the task and snapshot head_sha.
    new_head = _add_commit(repo, "worker commit")
    worker_status.write_status("a", state="done", task_id=tid,
                               progress_note="", pid=0)
    disp.tick()
    t = next(x for x in fleet._load_tasks()["tasks"] if x["id"] == tid)
    assert t["status"] == "done"
    assert t["base_sha"] == base
    assert t["head_sha"] == new_head
    # Branch is named consistently for the dashboard's diff link.
    assert t["branch"] == f"orch/{tid}"


def test_dispatcher_no_repo_records_null_branch(tmp_path, monkeypatch):
    plain = tmp_path / "plain"
    plain.mkdir()
    disp = _setup_dispatcher(tmp_path, monkeypatch, plain)

    tid = fleet.add_task("a", "do something")
    disp.tick()
    t = next(x for x in fleet._load_tasks()["tasks"] if x["id"] == tid)
    assert t["status"] == "running"
    assert t["branch"] is None
    assert t["base_sha"] is None


def test_kickoff_includes_git_instruction_when_branch_set():
    t = {"id": "t-0099", "description": "do x", "branch": "orch/t-0099"}
    msg = fleet._kickoff(t)
    assert "orch/t-0099" in msg
    # The git block must come *before* the existing kickoff body so it is
    # impossible for claude to miss when scanning the prompt.
    assert msg.index("orch/t-0099") < msg.index("New task")


def test_kickoff_omits_git_block_when_no_branch():
    t = {"id": "t-0100", "description": "do x", "branch": None}
    msg = fleet._kickoff(t)
    assert "Git:" not in msg
    assert "task branch" not in msg.lower()
