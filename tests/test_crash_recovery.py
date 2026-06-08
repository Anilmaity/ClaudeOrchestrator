"""Tests for `fleet.reattach_on_startup` (T-07 crash recovery on ./fleet up).

Each test drives the recovery code path directly with mocks — no real
dispatcher, no real backend. We simulate three situations that a restarted
``fleet up`` must handle without losing state or deleting agents from
fleet.json:

  * a worker whose terminal survived the dispatcher crash (reattach, no respawn);
  * a worker whose terminal died (fresh spawn);
  * an agent whose ``project_dir`` no longer exists (offline, but kept in config).

We also assert that a task that finished while the dispatcher was down — and
whose worker dropped a ``done`` entry in its status file — is rolled forward
on restart instead of being stuck as ``running`` forever.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import fleet
import worker_status


def _write_cfg(path: Path, config: dict) -> None:
    path.write_text(json.dumps(config), encoding="utf-8")


@pytest.fixture
def recovery_env(tmp_path, monkeypatch):
    """Three agents on a temp config + temp task store + temp status dir.

      alice  — terminal survived the crash (reattach)
      bob    — terminal died (must respawn)
      carol  — project_dir was deleted (offline)
    """
    pdir = tmp_path / "proj"
    pdir.mkdir()
    gone = tmp_path / "deleted-after-config-wrote-it"   # NEVER created -> offline
    cfg = tmp_path / "fleet.json"
    _write_cfg(cfg, {
        "projects": [],
        "agents": [
            {"name": "alice", "role": "", "project_dir": str(pdir)},
            {"name": "bob", "role": "", "project_dir": str(pdir)},
            {"name": "carol", "role": "", "project_dir": str(gone)},
        ],
    })
    monkeypatch.setattr(fleet, "CONFIG", cfg)
    monkeypatch.setattr(fleet, "AGENTS", fleet.load_config(cfg))
    monkeypatch.setattr(fleet, "STATE_DIR", tmp_path)
    monkeypatch.setattr(fleet, "TASKS_FILE", tmp_path / "fleet_tasks.json")
    # status files go into a temp dir so tests don't touch ~/.claude-orch
    monkeypatch.setattr(worker_status, "STATUS_DIR", tmp_path / "status")

    return {
        "cfg_path": cfg,
        "proj_dir": pdir,
        "alive": {"alice"},          # backend reports these as live workers
    }


@pytest.fixture
def fake_backend(monkeypatch, recovery_env):
    """Pretend alice is alive; spawn() records calls and succeeds for bob,
    fails for carol (project_dir missing — ensure_agent catches that before
    even calling spawn, so this never fires)."""
    alive = recovery_env["alive"]
    spawn_calls: list[str] = []

    monkeypatch.setattr(fleet.orch, "_windows", lambda: sorted(alive))
    monkeypatch.setattr(fleet.orch, "_window_exists", lambda name: name in alive)

    class FakeBackend:
        def set_scrollback(self, lines):
            pass

        def spawn(self, name, project_dir, role_file="", ready_timeout=45.0):
            spawn_calls.append(name)
            alive.add(name)
            return True, ""

    monkeypatch.setattr(fleet.orch, "_backend", FakeBackend())
    return spawn_calls


def test_live_worker_is_reattached_not_respawned(recovery_env, fake_backend):
    spawn_calls = fake_backend
    report = fleet.reattach_on_startup(fleet.AGENTS)

    by_name = {r["name"]: r for r in report}
    assert by_name["alice"]["state"] == "reattached"
    assert by_name["alice"]["note"] == "already running"
    assert by_name["alice"]["live_listed"] is True
    # spawn was NOT called for alice — that's the whole point of reattach
    assert "alice" not in spawn_calls


def test_dead_worker_is_respawned(recovery_env, fake_backend):
    spawn_calls = fake_backend
    report = fleet.reattach_on_startup(fleet.AGENTS)

    by_name = {r["name"]: r for r in report}
    assert by_name["bob"]["state"] == "spawned"
    assert "bob" in spawn_calls


def test_missing_project_dir_marks_offline_not_deleted(recovery_env, fake_backend):
    """An agent whose project_dir vanished is reported offline but stays in
    fleet.json — the dispatcher can recover it later if the directory comes
    back, and the user never silently loses an agent definition."""
    cfg_path = recovery_env["cfg_path"]
    config_before = json.loads(cfg_path.read_text(encoding="utf-8"))

    report = fleet.reattach_on_startup(fleet.AGENTS)

    by_name = {r["name"]: r for r in report}
    assert by_name["carol"]["state"] == "offline"
    assert "project_dir missing" in by_name["carol"]["note"]

    # the config file is unchanged — carol must NOT have been removed
    config_after = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert config_after == config_before
    names_in_cfg = {a["name"] for a in config_after["agents"]}
    assert "carol" in names_in_cfg


def test_inflight_task_rolled_forward_from_status_file(
        recovery_env, fake_backend):
    """Worker finished a task while the dispatcher was down. Its status file
    says ``done``. ``reattach_on_startup`` must mark the task done — otherwise
    the task stays ``running`` forever and head-of-line-blocks alice's queue.
    """
    # 1) seed a running task for alice in the temp task store
    fleet._save_tasks({
        "next_id": 2,
        "tasks": [{
            "id": "t-0001",
            "agent": "alice",
            "description": "go do the thing",
            "status": "running",
            "created_at": fleet._now(),
            "started_at": fleet._now(),
            "finished_at": None,
            "saw_busy": True,
            "needs_attention": False,
            "log": "",
        }],
    })
    # 2) alice's status file claims she finished it while we were dead
    worker_status.write_status(
        "alice", state="done", task_id="t-0001",
        progress_note="all done", pid=4242)

    report = fleet.reattach_on_startup(fleet.AGENTS)

    by_name = {r["name"]: r for r in report}
    assert by_name["alice"]["state"] == "reattached"
    assert by_name["alice"].get("task_id") == "t-0001"
    assert by_name["alice"].get("task_rolled_to") == "done"

    # task store reflects the roll-forward
    tasks = fleet._load_tasks()["tasks"]
    by_id = {t["id"]: t for t in tasks}
    assert by_id["t-0001"]["status"] == "done"
    assert by_id["t-0001"]["finished_at"] is not None
    assert "reconciled" in by_id["t-0001"]["log"]


def test_inflight_task_with_no_status_file_left_alone(
        recovery_env, fake_backend):
    """When the worker survived but never wrote a status file, the task stays
    ``running``: the live dispatcher loop will keep watching it."""
    fleet._save_tasks({
        "next_id": 2,
        "tasks": [{
            "id": "t-0001",
            "agent": "alice",
            "description": "still running",
            "status": "running",
            "created_at": fleet._now(),
            "started_at": fleet._now(),
            "finished_at": None,
            "saw_busy": True,
            "needs_attention": False,
            "log": "",
        }],
    })
    # NO status file written for alice

    report = fleet.reattach_on_startup(fleet.AGENTS)
    by_name = {r["name"]: r for r in report}
    assert by_name["alice"]["state"] == "reattached"
    assert "task_rolled_to" not in by_name["alice"]

    tasks = fleet._load_tasks()["tasks"]
    assert tasks[0]["status"] == "running"


def test_status_file_for_different_task_ignored(recovery_env, fake_backend):
    """If the status file's task_id doesn't match the running task (stale
    file from a previous task), we must NOT clobber the current task."""
    fleet._save_tasks({
        "next_id": 5,
        "tasks": [{
            "id": "t-0004",
            "agent": "alice",
            "description": "newer task",
            "status": "running",
            "created_at": fleet._now(),
            "started_at": fleet._now(),
            "finished_at": None,
            "saw_busy": True,
            "needs_attention": False,
            "log": "",
        }],
    })
    # status file points at the PREVIOUS task that wasn't cleared
    worker_status.write_status(
        "alice", state="done", task_id="t-0001",   # stale id
        progress_note="from a previous run", pid=4242)

    fleet.reattach_on_startup(fleet.AGENTS)

    tasks = fleet._load_tasks()["tasks"]
    assert tasks[0]["status"] == "running"        # NOT rolled forward


def test_simulated_restart_round_trip(recovery_env, fake_backend, monkeypatch):
    """End-to-end restart simulation: build a 'before crash' world, drop the
    Dispatcher object, call reattach_on_startup again with the same backend
    state, and verify the agents survive with correct attachment state. This
    is the test the brief explicitly calls for (kill + recreate the
    dispatcher object, fake live session, assert reattached and not deleted).
    """
    cfg_path = recovery_env["cfg_path"]

    # before-crash dispatcher state. The real Dispatcher takes an agents list;
    # we don't need to start its thread, only construct it.
    monkeypatch.setattr(fleet, "_running_dispatcher", None, raising=False)
    disp_before = fleet.Dispatcher(fleet.AGENTS)
    assert "alice" in {a for a in disp_before.agents}
    del disp_before              # "kill" the dispatcher object

    # ---- crash + restart ----
    report = fleet.reattach_on_startup(fleet.AGENTS)
    disp_after = fleet.Dispatcher(fleet.AGENTS)

    by_name = {r["name"]: r for r in report}
    assert by_name["alice"]["state"] == "reattached"     # NOT respawned
    assert by_name["bob"]["state"] == "spawned"          # was dead, restarted
    assert by_name["carol"]["state"] == "offline"        # dir missing -> offline

    # fleet.json still has all three agents — nothing was deleted on restart
    config_after = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert {a["name"] for a in config_after["agents"]} == {"alice", "bob", "carol"}

    # the post-restart dispatcher carries the same roster
    assert set(disp_after.agents) == {"alice", "bob", "carol"}
