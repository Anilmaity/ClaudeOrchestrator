"""Stress test for the fleet.json single-writer lock (T-04).

Before the lock, the dispatcher hot-reloading fleet.json and any number of
ThreadingHTTPServer handlers could each read the file, mutate their own copy,
then atomic-replace — and a second writer's replace would wipe the first
writer's change. This test issues N concurrent create_agent / create_project /
set_agent_project / add_task calls against a temp fleet.json and asserts:

  (a) no write is lost — every successful agent/project landed in the file,
  (b) fleet.json always parses as valid JSON throughout the run,
  (c) the existing atomic .tmp + replace pattern still holds.

All writes go through fleet.CONFIG so monkeypatching it redirects the whole
test off the live config. Task-store writes hit fleet.TASKS_FILE — also
redirected — and are guarded by fleet._LOCK independently of _CONFIG_LOCK.
"""
from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pytest

import fleet


def _write_cfg(path: Path, config: dict) -> None:
    path.write_text(json.dumps(config), encoding="utf-8")


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    """A temp fleet.json with one project + agent, plus a temp task store."""
    pdir = tmp_path / "p"
    pdir.mkdir()
    path = tmp_path / "fleet.json"
    _write_cfg(path, {
        "projects": [{"name": "core", "path": "", "description": ""}],
        "agents": [{"name": "alice", "role": "", "project_dir": str(pdir), "project": "core"}],
    })
    monkeypatch.setattr(fleet, "CONFIG", path)
    monkeypatch.setattr(fleet, "AGENTS", fleet.load_config(path))
    monkeypatch.setattr(fleet, "STATE_DIR", tmp_path)
    monkeypatch.setattr(fleet, "TASKS_FILE", tmp_path / "fleet_tasks.json")
    return path, pdir


def test_concurrent_create_agent_no_lost_writes(cfg):
    """N threads each create one agent — every agent must land."""
    path, pdir = cfg
    n = 40

    def worker(i: int) -> tuple[bool, str | None]:
        try:
            fleet.create_agent(f"w_{i:03d}", "", str(pdir))
            return True, None
        except Exception as e:  # noqa: BLE001
            return False, repr(e)

    with ThreadPoolExecutor(max_workers=16) as ex:
        results = [f.result() for f in as_completed(ex.submit(worker, i) for i in range(n))]

    failures = [err for ok, err in results if not ok]
    assert not failures, f"agent creations raised: {failures[:5]}"

    agents = {a["name"] for a in fleet.load_config(path)}
    expected = {f"w_{i:03d}" for i in range(n)}
    missing = expected - agents
    assert not missing, f"{len(missing)} agent writes were lost: {sorted(missing)[:10]}"
    assert "alice" in agents       # seed survived


def test_concurrent_create_project_no_lost_writes(cfg):
    """N threads each create one project — every project must land."""
    path, _pdir = cfg
    n = 30

    def worker(i: int) -> tuple[bool, str | None]:
        try:
            fleet.create_project(f"pr_{i:03d}")
            return True, None
        except Exception as e:  # noqa: BLE001
            return False, repr(e)

    with ThreadPoolExecutor(max_workers=12) as ex:
        results = [f.result() for f in as_completed(ex.submit(worker, i) for i in range(n))]

    failures = [err for ok, err in results if not ok]
    assert not failures, f"project creations raised: {failures[:5]}"

    projects = {p["name"] for p in fleet.load_projects(path)}
    expected = {f"pr_{i:03d}" for i in range(n)}
    missing = expected - projects
    assert not missing, f"{len(missing)} project writes were lost: {sorted(missing)[:10]}"
    assert "core" in projects      # seed survived
    # every created project also gets its PM agent — none of those got lost either
    agents = {a["name"] for a in fleet.load_config(path)}
    pm_missing = {f"pr_{i:03d}-pm" for i in range(n)} - agents
    assert not pm_missing, f"PM agents lost: {sorted(pm_missing)[:10]}"


def test_concurrent_mixed_writers_and_tasks(cfg):
    """Interleave create_agent + create_project + add_task on many threads.

    Mirrors the production race: the HTTP server's ThreadingHTTPServer can run
    /api/agents, /api/projects, and /api/tasks concurrently while the
    dispatcher hot-reloads. Asserts the file parses at every poll and that all
    successful writes are observed.
    """
    path, pdir = cfg
    per_kind = 20
    parse_errors: list[str] = []
    parser_stop = threading.Event()

    def parser():
        """Continuously parse fleet.json while writers churn — must never see torn JSON."""
        while not parser_stop.is_set():
            try:
                json.loads(path.read_text(encoding="utf-8"))
            except (FileNotFoundError, PermissionError, OSError):
                # Windows: Path.replace briefly races with open(); the file
                # also doesn't exist for an instant. Tolerate both — only a
                # JSONDecodeError proves a torn write.
                pass
            except json.JSONDecodeError as e:
                parse_errors.append(repr(e))
                return

    def make_agent(i: int) -> tuple[str, bool]:
        try:
            fleet.create_agent(f"ma_{i:03d}", "", str(pdir))
            return f"ma_{i:03d}", True
        except Exception:  # noqa: BLE001
            return f"ma_{i:03d}", False

    def make_project(i: int) -> tuple[str, bool]:
        try:
            fleet.create_project(f"mp_{i:03d}")
            return f"mp_{i:03d}", True
        except Exception:  # noqa: BLE001
            return f"mp_{i:03d}", False

    def queue_task(i: int) -> tuple[str, bool]:
        try:
            fleet.add_task("alice", f"task-{i:03d}")
            return f"task-{i:03d}", True
        except Exception:  # noqa: BLE001
            return f"task-{i:03d}", False

    pt = threading.Thread(target=parser, daemon=True)
    pt.start()
    try:
        with ThreadPoolExecutor(max_workers=24) as ex:
            futs = []
            for i in range(per_kind):
                futs.append(ex.submit(make_agent, i))
                futs.append(ex.submit(make_project, i))
                futs.append(ex.submit(queue_task, i))
            results = [f.result() for f in as_completed(futs)]
    finally:
        parser_stop.set()
        pt.join(timeout=5)

    assert not parse_errors, f"parser saw torn JSON: {parse_errors[:3]}"

    successful_agents = {n for n, ok in results if ok and n.startswith("ma_")}
    successful_projects = {n for n, ok in results if ok and n.startswith("mp_")}
    successful_tasks = {n for n, ok in results if ok and n.startswith("task-")}

    agents = {a["name"] for a in fleet.load_config(path)}
    projects = {p["name"] for p in fleet.load_projects(path)}

    missing_agents = successful_agents - agents
    missing_projects = successful_projects - projects
    assert not missing_agents, f"agent writes lost: {sorted(missing_agents)[:10]}"
    assert not missing_projects, f"project writes lost: {sorted(missing_projects)[:10]}"

    # task store: every successful add_task must be persisted
    queued = {t["description"] for t in fleet._load_tasks()["tasks"]}
    missing_tasks = successful_tasks - queued
    assert not missing_tasks, f"task writes lost: {sorted(missing_tasks)[:10]}"


def test_atomic_replace_pattern_preserved(cfg, monkeypatch):
    """Spy on Path.replace to confirm every write still uses the .tmp + replace dance."""
    path, pdir = cfg
    replaced: list[tuple[str, str]] = []
    real_replace = Path.replace

    def spy_replace(self, target):
        if Path(target).name == path.name:
            replaced.append((self.name, Path(target).name))
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", spy_replace)
    fleet.create_agent("solo", "", str(pdir))
    assert replaced, "expected at least one atomic replace into fleet.json"
    src, dst = replaced[-1]
    assert src.endswith(".tmp") and dst == "fleet.json"


def test_concurrent_rename_and_update_project(cfg):
    """Concurrent rename + update on the same project — last writer wins, file still valid."""
    path, _pdir = cfg
    # seed extra projects to rename / update so threads have disjoint targets
    for i in range(8):
        fleet.create_project(f"rp_{i:02d}")

    def renamer(i: int):
        try:
            fleet.rename_project(f"rp_{i:02d}", f"rn_{i:02d}")
        except ValueError:
            pass

    def updater(i: int):
        try:
            fleet.update_project(f"rp_{i:02d}", "", f"desc-{i:02d}")
        except ValueError:
            # if the renamer beat us, rp_{i} is gone — fine
            pass

    with ThreadPoolExecutor(max_workers=16) as ex:
        futs = []
        for i in range(8):
            futs.append(ex.submit(renamer, i))
            futs.append(ex.submit(updater, i))
        for f in as_completed(futs):
            f.result()

    # file still parses (no torn JSON, no lost structure)
    data = json.loads(path.read_text(encoding="utf-8"))
    names = {p["name"] for p in data["projects"]}
    # every original rp_NN must be EITHER still rp_NN (rename failed because
    # updater was mid-flight — actually rename can't fail that way, but
    # rename-then-update on the renamed name does succeed cleanly) OR renamed
    # to rn_NN. Either way, exactly one of the two ends up in projects.
    for i in range(8):
        assert (f"rp_{i:02d}" in names) ^ (f"rn_{i:02d}" in names), (
            f"project {i} ended up in inconsistent state: rp present="
            f"{f'rp_{i:02d}' in names}, rn present={f'rn_{i:02d}' in names}"
        )
