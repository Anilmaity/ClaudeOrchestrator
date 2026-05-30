"""Tests for the Projects & Groups data model + mutation helpers in fleet.py.

Every test operates on a *temporary* fleet.json (monkeypatch fleet.CONFIG /
fleet.AGENTS) — never the live one. The mutation helpers reference the
module-global CONFIG, so monkeypatching it is enough to redirect all reads/writes.
"""

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

import fleet
import orch


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #
def _write_cfg(path, config):
    path.write_text(json.dumps(config), encoding="utf-8")


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    """A temp fleet.json: one project ('core') and two agents.

    'alice' belongs to 'core'; 'bob' is ungrouped. CONFIG/AGENTS are pointed at
    the temp file so every helper read/write hits it instead of the real config.
    """
    proj_a = tmp_path / "proj_a"
    proj_a.mkdir()
    proj_b = tmp_path / "proj_b"
    proj_b.mkdir()
    path = tmp_path / "fleet.json"
    _write_cfg(path, {
        "projects": [
            {"name": "core", "path": "", "description": "core project"},
        ],
        "agents": [
            {"name": "alice", "role": "dev", "project_dir": str(proj_a), "project": "core"},
            {"name": "bob", "role": "qa", "project_dir": str(proj_b)},
        ],
    })
    monkeypatch.setattr(fleet, "CONFIG", path)
    monkeypatch.setattr(fleet, "AGENTS", fleet.load_config(path))
    return path


@pytest.fixture
def tmp_queue(tmp_path, monkeypatch):
    """Redirect the task store to a temp file so add_task never touches the live queue."""
    qfile = tmp_path / "fleet_tasks.json"
    monkeypatch.setattr(fleet, "STATE_DIR", tmp_path)
    monkeypatch.setattr(fleet, "TASKS_FILE", qfile)
    return qfile


# --------------------------------------------------------------------------- #
# load_config / load_projects
# --------------------------------------------------------------------------- #
def test_load_projects_defaults_to_empty(tmp_path, monkeypatch):
    path = tmp_path / "fleet.json"
    _write_cfg(path, {"agents": [{"name": "a", "role": "", "project_dir": str(tmp_path)}]})
    monkeypatch.setattr(fleet, "CONFIG", path)
    assert fleet.load_projects(path) == []


def test_load_projects_normalizes_missing_fields(tmp_path, monkeypatch):
    path = tmp_path / "fleet.json"
    _write_cfg(path, {
        "projects": [{"name": "bare"}],   # no path / description
        "agents": [{"name": "a", "role": "", "project_dir": str(tmp_path)}],
    })
    monkeypatch.setattr(fleet, "CONFIG", path)
    assert fleet.load_projects(path) == [
        {"name": "bare", "path": "", "description": "", "dashboard_url": ""}
    ]


def test_load_projects_rejects_duplicate(tmp_path):
    path = tmp_path / "fleet.json"
    _write_cfg(path, {
        "projects": [{"name": "x"}, {"name": "x"}],
        "agents": [{"name": "a", "role": "", "project_dir": str(tmp_path)}],
    })
    with pytest.raises(SystemExit):       # load_config-style validation via orch._die
        fleet.load_projects(path)


def test_load_projects_rejects_invalid_name(tmp_path):
    path = tmp_path / "fleet.json"
    _write_cfg(path, {
        "projects": [{"name": "bad name!"}],
        "agents": [{"name": "a", "role": "", "project_dir": str(tmp_path)}],
    })
    with pytest.raises(SystemExit):
        fleet.load_projects(path)


def test_agents_always_have_project_key(tmp_path):
    path = tmp_path / "fleet.json"
    _write_cfg(path, {
        "agents": [
            {"name": "a", "role": "", "project_dir": str(tmp_path)},          # no project
            {"name": "b", "role": "", "project_dir": str(tmp_path), "project": "core"},
        ],
    })
    agents = fleet.load_config(path)
    assert all("project" in a for a in agents)
    by_name = {a["name"]: a for a in agents}
    assert by_name["a"]["project"] == ""        # default when absent
    assert by_name["b"]["project"] == "core"


# --------------------------------------------------------------------------- #
# create_project / delete_project
# --------------------------------------------------------------------------- #
def test_create_project_appends_and_normalizes(cfg):
    fleet.create_project("web", path="~/somewhere", description="ui")
    projects = {p["name"]: p for p in fleet.load_projects(cfg)}
    assert "web" in projects
    assert projects["web"]["description"] == "ui"
    assert projects["web"]["path"] and "~" not in projects["web"]["path"]   # expanduser applied


def test_create_project_empty_path_stays_empty(cfg):
    fleet.create_project("blank")
    projects = {p["name"]: p for p in fleet.load_projects(cfg)}
    assert projects["blank"] == {"name": "blank", "path": "", "description": "", "dashboard_url": ""}


def test_create_project_rejects_duplicate(cfg):
    with pytest.raises(ValueError):
        fleet.create_project("core")


def test_create_project_rejects_invalid_name(cfg):
    with pytest.raises(ValueError):
        fleet.create_project("bad name!")


def test_delete_project_ungroups_members(cfg):
    assert fleet.delete_project("core") is True
    assert all(p["name"] != "core" for p in fleet.load_projects(cfg))
    agents = {a["name"]: a for a in fleet.load_config(cfg)}
    assert agents["alice"]["project"] == ""     # was 'core', now ungrouped (not deleted)
    assert "alice" in agents                     # agent itself survives


def test_delete_project_unknown_returns_false(cfg):
    assert fleet.delete_project("ghost") is False


# --------------------------------------------------------------------------- #
# create_agent
# --------------------------------------------------------------------------- #
def test_create_agent_success(cfg, tmp_path):
    newdir = tmp_path / "newproj"
    newdir.mkdir()
    fleet.create_agent("carol", "writer", str(newdir), project="core")
    agents = {a["name"]: a for a in fleet.load_config(cfg)}
    assert agents["carol"]["role"] == "writer"
    assert agents["carol"]["project"] == "core"
    assert agents["carol"]["project_dir"] == str(newdir.resolve())
    # module-level AGENTS refreshed so the dashboard sees the new agent
    assert any(a["name"] == "carol" for a in fleet.AGENTS)


def test_create_agent_bad_dir(cfg, tmp_path):
    missing = tmp_path / "does-not-exist"
    with pytest.raises(ValueError) as ei:
        fleet.create_agent("carol", "", str(missing))
    assert "not a directory" in str(ei.value)


def test_create_agent_empty_dir_rejected(cfg):
    with pytest.raises(ValueError) as ei:
        fleet.create_agent("carol", "", "")
    assert "not a directory" in str(ei.value)


def test_create_agent_duplicate_name(cfg, tmp_path):
    d = tmp_path / "d"
    d.mkdir()
    with pytest.raises(ValueError):
        fleet.create_agent("alice", "", str(d))


def test_create_agent_unknown_project(cfg, tmp_path):
    d = tmp_path / "d2"
    d.mkdir()
    with pytest.raises(ValueError) as ei:
        fleet.create_agent("dave", "", str(d), project="ghost")
    assert "unknown project" in str(ei.value)


# --------------------------------------------------------------------------- #
# set_agent_project
# --------------------------------------------------------------------------- #
def test_set_agent_project_assign(cfg):
    fleet.create_project("web")
    fleet.set_agent_project("bob", "web")
    agents = {a["name"]: a for a in fleet.load_config(cfg)}
    assert agents["bob"]["project"] == "web"


def test_set_agent_project_ungroup(cfg):
    fleet.set_agent_project("alice", "")
    agents = {a["name"]: a for a in fleet.load_config(cfg)}
    assert agents["alice"]["project"] == ""


def test_set_agent_project_unknown_agent(cfg):
    with pytest.raises(ValueError) as ei:
        fleet.set_agent_project("ghost", "core")
    assert "unknown agent" in str(ei.value)


def test_set_agent_project_unknown_project(cfg):
    with pytest.raises(ValueError) as ei:
        fleet.set_agent_project("alice", "ghost")
    assert "unknown project" in str(ei.value)


# --------------------------------------------------------------------------- #
# build_state extension
# --------------------------------------------------------------------------- #
def test_build_state_includes_projects_and_agent_project(cfg, monkeypatch):
    # avoid touching the live tmux backend / task store
    monkeypatch.setattr(fleet, "agent_activity", lambda name: "idle")
    monkeypatch.setattr(fleet, "agent_attention", lambda name: False)
    monkeypatch.setattr(fleet, "_load_tasks", lambda: {"next_id": 1, "tasks": []})
    state = fleet.build_state()
    assert state["projects"] == [
        {"name": "core", "path": "", "description": "core project", "dashboard_url": ""}
    ]
    by_name = {a["name"]: a for a in state["agents"]}
    assert by_name["alice"]["project"] == "core"
    assert by_name["bob"]["project"] == ""


# --------------------------------------------------------------------------- #
# v2: add_group_tasks
# --------------------------------------------------------------------------- #
def test_add_group_tasks_success_queues_per_member(cfg, tmp_queue):
    fleet.set_agent_project("bob", "core")          # core now has alice + bob
    ids = fleet.add_group_tasks("core", "do the thing")
    assert len(ids) == 2
    queued = fleet._load_tasks()["tasks"]
    assert {t["id"] for t in queued} == set(ids)            # actually persisted
    assert {t["agent"] for t in queued} == {"alice", "bob"}  # one per member
    assert all(t["description"] == "do the thing" for t in queued)
    assert all(t["status"] == "pending" for t in queued)


def test_add_group_tasks_unknown_project(cfg, tmp_queue):
    with pytest.raises(ValueError) as ei:
        fleet.add_group_tasks("ghost", "x")
    assert "unknown project" in str(ei.value)
    assert fleet._load_tasks()["tasks"] == []       # nothing queued


def test_add_group_tasks_no_members(cfg, tmp_queue):
    fleet.create_project("empty")
    with pytest.raises(ValueError) as ei:
        fleet.add_group_tasks("empty", "x")
    assert "no agents in project" in str(ei.value)
    assert fleet._load_tasks()["tasks"] == []


# --------------------------------------------------------------------------- #
# v2: rename_project
# --------------------------------------------------------------------------- #
def test_rename_project_renames_and_updates_members(cfg):
    fleet.rename_project("core", "core2")
    names = {p["name"] for p in fleet.load_projects(cfg)}
    assert "core2" in names and "core" not in names
    agents = {a["name"]: a for a in fleet.load_config(cfg)}
    assert agents["alice"]["project"] == "core2"    # member re-pointed
    assert agents["bob"]["project"] == ""           # untouched


def test_rename_project_invalid_name(cfg):
    with pytest.raises(ValueError) as ei:
        fleet.rename_project("core", "bad name!")
    assert "invalid name" in str(ei.value)


def test_rename_project_unknown(cfg):
    with pytest.raises(ValueError) as ei:
        fleet.rename_project("ghost", "newname")
    assert "unknown project" in str(ei.value)


def test_rename_project_duplicate_new_name(cfg):
    fleet.create_project("web")
    with pytest.raises(ValueError) as ei:
        fleet.rename_project("core", "web")
    assert "project exists" in str(ei.value)


def test_rename_project_noop_same_name_allowed(cfg):
    fleet.rename_project("core", "core")            # valid name + exists -> no-op
    names = {p["name"] for p in fleet.load_projects(cfg)}
    assert "core" in names
    agents = {a["name"]: a for a in fleet.load_config(cfg)}
    assert agents["alice"]["project"] == "core"


# --------------------------------------------------------------------------- #
# v3: update_project (edit path/description)
# --------------------------------------------------------------------------- #
def test_update_project_sets_path_and_description(cfg):
    fleet.update_project("core", "~/somewhere", "new desc")
    proj = {p["name"]: p for p in fleet.load_projects(cfg)}["core"]
    assert proj["description"] == "new desc"
    assert proj["path"] and "~" not in proj["path"]     # expanduser applied
    assert proj["path"] == str(Path("~/somewhere").expanduser())


def test_update_project_clears_when_empty(cfg):
    fleet.update_project("core", "~/x", "filled")       # set first
    fleet.update_project("core", "", "")                # then clear both
    proj = {p["name"]: p for p in fleet.load_projects(cfg)}["core"]
    assert proj["path"] == "" and proj["description"] == ""


def test_update_project_unknown(cfg):
    with pytest.raises(ValueError) as ei:
        fleet.update_project("ghost", "x", "y")
    assert "unknown project" in str(ei.value)


# --------------------------------------------------------------------------- #
# t-0086: optional per-project dashboard_url (conditional "↗ dashboard" CTA)
# --------------------------------------------------------------------------- #
def test_create_project_round_trips_dashboard_url(cfg):
    fleet.create_project("web", dashboard_url="http://localhost:9999")
    proj = {p["name"]: p for p in fleet.load_projects(cfg)}["web"]
    assert proj["dashboard_url"] == "http://localhost:9999"


def test_create_project_defaults_dashboard_url_empty(cfg):
    fleet.create_project("web")          # no dashboard_url passed
    proj = {p["name"]: p for p in fleet.load_projects(cfg)}["web"]
    assert proj["dashboard_url"] == ""


def test_update_project_sets_and_clears_dashboard_url(cfg):
    fleet.update_project("core", "", "", dashboard_url="https://example.com/dash")
    proj = {p["name"]: p for p in fleet.load_projects(cfg)}["core"]
    assert proj["dashboard_url"] == "https://example.com/dash"
    fleet.update_project("core", "", "", dashboard_url="")     # clear it
    proj = {p["name"]: p for p in fleet.load_projects(cfg)}["core"]
    assert proj["dashboard_url"] == ""


@pytest.mark.parametrize("bad", ["javascript:alert(1)", "ftp://x", "localhost:8787", "  "])
def test_dashboard_url_rejects_non_http(cfg, bad):
    # junk / unsafe schemes never persist — they collapse to "" (validation).
    fleet.create_project("web", dashboard_url=bad)
    proj = {p["name"]: p for p in fleet.load_projects(cfg)}["web"]
    assert proj["dashboard_url"] == ""
    # update_project applies the same single-source-of-truth normalization.
    fleet.update_project("web", "", "", dashboard_url="http://ok")   # set a good one
    fleet.update_project("web", "", "", dashboard_url=bad)           # then a bad one
    proj = {p["name"]: p for p in fleet.load_projects(cfg)}["web"]
    assert proj["dashboard_url"] == ""


def test_load_projects_missing_dashboard_url_defaults_empty(tmp_path, monkeypatch):
    # a pre-existing project written before the field existed still loads cleanly.
    path = tmp_path / "fleet.json"
    _write_cfg(path, {
        "projects": [{"name": "legacy", "path": "", "description": "old"}],
        "agents": [{"name": "a", "role": "", "project_dir": str(tmp_path)}],
    })
    monkeypatch.setattr(fleet, "CONFIG", path)
    proj = {p["name"]: p for p in fleet.load_projects(path)}["legacy"]
    assert proj["dashboard_url"] == ""


# --------------------------------------------------------------------------- #
# t-0009: per-project project-manager (PM) agents
# --------------------------------------------------------------------------- #
def test_pm_name_and_role():
    assert fleet._pm_name("web") == "web-pm"
    role = fleet._pm_role("web")
    assert "'web'" in role                       # <P> substituted
    assert "<P>" not in role
    assert "<agent>" in role                      # literal placeholder left intact


def test_create_project_also_creates_pm(cfg):
    fleet.create_project("web")
    agents = {a["name"]: a for a in fleet.load_config(cfg)}
    assert "web-pm" in agents
    pm = agents["web-pm"]
    assert pm["manager_of"] == "web"
    assert pm["project"] == ""                    # PM is not a worker member
    assert pm["project_dir"] == str(fleet.HERE)   # orchestrator dir


def test_delete_project_removes_pm(cfg):
    fleet.create_project("web")
    assert "web-pm" in {a["name"] for a in fleet.load_config(cfg)}
    assert fleet.delete_project("web") is True
    names = {a["name"] for a in fleet.load_config(cfg)}
    assert "web-pm" not in names
    assert all(p["name"] != "web" for p in fleet.load_projects(cfg))


def test_rename_project_renames_pm(cfg):
    fleet.ensure_project_managers()               # gives 'core' a PM (core-pm)
    fleet.rename_project("core", "core2")
    agents = {a["name"]: a for a in fleet.load_config(cfg)}
    assert "core-pm" not in agents and "core2-pm" in agents
    pm = agents["core2-pm"]
    assert pm["manager_of"] == "core2"
    assert "'core2'" in pm["role"] and "'core'" not in pm["role"]


def test_ensure_project_managers_backfills_and_is_idempotent(cfg):
    # the cfg fixture hand-writes 'core' WITHOUT a PM
    created = fleet.ensure_project_managers()
    assert created == ["core-pm"]
    agents = {a["name"]: a for a in fleet.load_config(cfg)}
    assert agents["core-pm"]["manager_of"] == "core"
    # a second call adds nothing
    assert fleet.ensure_project_managers() == []
    assert sum(1 for a in fleet.load_config(cfg) if a.get("manager_of") == "core") == 1


def test_ensure_project_managers_skips_existing_name(cfg, tmp_path):
    # an agent literally named 'core-pm' already exists but isn't a manager
    d = tmp_path / "pmclash"
    d.mkdir()
    fleet.create_agent("core-pm", "not a manager", str(d))
    created = fleet.ensure_project_managers()
    assert created == []                          # name clash -> skip, no crash
    agents = {a["name"]: a for a in fleet.load_config(cfg)}
    assert agents["core-pm"]["manager_of"] == ""  # existing agent left untouched


def test_load_config_carries_manager_of(tmp_path):
    path = tmp_path / "fleet.json"
    _write_cfg(path, {
        "projects": [{"name": "core"}],
        "agents": [
            {"name": "core-pm", "role": "m", "project_dir": str(tmp_path), "manager_of": "core"},
            {"name": "alice", "role": "", "project_dir": str(tmp_path), "project": "core"},
        ],
    })
    agents = {a["name"]: a for a in fleet.load_config(path)}
    assert agents["core-pm"]["manager_of"] == "core"
    assert agents["alice"]["manager_of"] == ""    # default when absent


def test_build_state_emits_manager_of(cfg, monkeypatch):
    monkeypatch.setattr(fleet, "agent_activity", lambda name: "idle")
    monkeypatch.setattr(fleet, "agent_attention", lambda name: False)
    monkeypatch.setattr(fleet, "_load_tasks", lambda: {"next_id": 1, "tasks": []})
    fleet.ensure_project_managers()               # creates core-pm and refreshes AGENTS
    by_name = {a["name"]: a for a in fleet.build_state()["agents"]}
    assert by_name["core-pm"]["manager_of"] == "core"
    assert by_name["alice"]["manager_of"] == ""   # workers carry an empty manager_of


def test_pm_excluded_from_group_fanout(cfg, tmp_queue):
    fleet.ensure_project_managers()               # core now has core-pm (project="")
    ids = fleet.add_group_tasks("core", "do the thing")
    queued = fleet._load_tasks()["tasks"]
    assert {t["agent"] for t in queued} == {"alice"}   # only the worker member, not core-pm
    assert len(ids) == 1


# --------------------------------------------------------------------------- #
# HTTP-level integration test: drive fleet.Handler end-to-end over a real socket
# --------------------------------------------------------------------------- #
def _get(url):
    with urllib.request.urlopen(url) as r:               # noqa: S310 (localhost test)
        return r.status, json.loads(r.read() or b"{}")


def _post(url, payload):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req) as r:           # noqa: S310 (localhost test)
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:                  # 4xx/5xx still carry a JSON body
        return e.code, json.loads(e.read() or b"{}")


def test_http_endpoints_end_to_end(tmp_path, monkeypatch):
    pdir = tmp_path / "pdir"
    pdir.mkdir()
    gdir = tmp_path / "gdir"
    gdir.mkdir()
    cfg_path = tmp_path / "fleet.json"
    _write_cfg(cfg_path, {
        "projects": [{"name": "alpha", "path": "", "description": "a"}],
        "agents": [
            {"name": "ht_alpha", "role": "", "project_dir": str(pdir), "project": "alpha"},
            {"name": "ht_beta", "role": "", "project_dir": str(pdir)},
        ],
    })
    monkeypatch.setattr(fleet, "CONFIG", cfg_path)
    monkeypatch.setattr(fleet, "AGENTS", fleet.load_config(cfg_path))
    monkeypatch.setattr(fleet, "STATE_DIR", tmp_path)
    monkeypatch.setattr(fleet, "TASKS_FILE", tmp_path / "fleet_tasks.json")
    # never read/spawn live agent terminals: every temp agent reads as offline
    monkeypatch.setattr(fleet, "agent_activity", lambda name: "offline")
    monkeypatch.setattr(fleet, "agent_attention", lambda name: False)

    # snapshot the live files we must not disturb (real paths, not the temp ones)
    real_cfg = Path(fleet.__file__).resolve().parent / "fleet.json"
    real_cfg_before = real_cfg.read_bytes()
    real_queue = orch.STATE_DIR / "fleet_tasks.json"
    marker = "v2-http-test-marker-do-not-dispatch"

    srv = ThreadingHTTPServer(("127.0.0.1", 0), fleet.Handler)
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        # GET /api/state — projects present, every agent has a project field
        st, body = _get(base + "/api/state")
        assert st == 200
        assert any(p["name"] == "alpha" for p in body["projects"])
        assert all("project" in a for a in body["agents"])
        agp = {a["name"]: a["project"] for a in body["agents"]}
        assert agp == {"ht_alpha": "alpha", "ht_beta": ""}

        # POST /api/projects — dashboard_url flows through the handler into storage
        st, body = _post(base + "/api/projects",
                         {"name": "beta", "description": "b", "dashboard_url": "http://localhost:8787"})
        assert st == 200 and body == {"ok": True, "name": "beta"}
        beta = {p["name"]: p for p in fleet.load_projects(cfg_path)}["beta"]
        assert beta["dashboard_url"] == "http://localhost:8787"

        # POST /api/agents (new agent into beta)
        st, body = _post(base + "/api/agents",
                         {"name": "ht_gamma", "project_dir": str(gdir), "project": "beta"})
        assert st == 200 and body == {"ok": True, "name": "ht_gamma"}

        # POST /api/agent/group (move ht_beta into beta)
        st, body = _post(base + "/api/agent/group", {"name": "ht_beta", "project": "beta"})
        assert st == 200 and body == {"ok": True}

        # POST /api/tasks/group (beta now has ht_gamma + ht_beta)
        st, body = _post(base + "/api/tasks/group", {"project": "beta", "description": marker})
        assert st == 200
        assert body["ok"] is True and body["count"] == 2 and len(body["ids"]) == 2

        # empty description -> 400
        st, body = _post(base + "/api/tasks/group", {"project": "beta", "description": "  "})
        assert st == 400 and body == {"error": "empty task"}

        # POST /api/projects/rename
        st, body = _post(base + "/api/projects/rename", {"name": "beta", "new_name": "beta2"})
        assert st == 200 and body == {"ok": True, "name": "beta2"}

        # invalid rename -> 400 ValueError mapping
        st, body = _post(base + "/api/projects/rename",
                         {"name": "beta2", "new_name": "bad name!"})
        assert st == 400 and "invalid name" in body["error"]

        # POST /api/projects/update — edit alpha's path/description/dashboard_url
        st, body = _post(base + "/api/projects/update",
                         {"name": "alpha", "path": str(gdir), "description": "edited",
                          "dashboard_url": "https://alpha.example.com"})
        assert st == 200 and body == {"ok": True, "name": "alpha"}
        alpha = {p["name"]: p for p in fleet.load_projects(cfg_path)}["alpha"]
        assert alpha["path"] == str(Path(str(gdir)).expanduser())  # temp config updated
        assert alpha["description"] == "edited"
        assert alpha["dashboard_url"] == "https://alpha.example.com"

        # update unknown project -> 400
        st, body = _post(base + "/api/projects/update", {"name": "ghost", "description": "x"})
        assert st == 400 and "unknown project" in body["error"]

        # POST /api/projects/delete
        st, body = _post(base + "/api/projects/delete", {"name": "beta2"})
        assert st == 200 and body == {"ok": True}

        # the group tasks landed in the TEMP queue, not the live one
        temp_tasks = fleet._load_tasks()["tasks"]
        assert sum(1 for x in temp_tasks if x["description"] == marker) == 2
    finally:
        srv.shutdown()
        t.join(timeout=5)

    # live fleet.json is byte-identical (the dispatcher only reads it; we wrote temp)
    assert real_cfg.read_bytes() == real_cfg_before
    # our group tasks never reached the live queue
    if real_queue.exists():
        assert marker not in real_queue.read_text(encoding="utf-8")


def test_http_project_managers_end_to_end(tmp_path, monkeypatch):
    pdir = tmp_path / "pdir"
    pdir.mkdir()
    wdir = tmp_path / "wdir"
    wdir.mkdir()
    cfg_path = tmp_path / "fleet.json"
    _write_cfg(cfg_path, {
        "projects": [],
        "agents": [{"name": "ht_solo", "role": "", "project_dir": str(pdir)}],
    })
    monkeypatch.setattr(fleet, "CONFIG", cfg_path)
    monkeypatch.setattr(fleet, "AGENTS", fleet.load_config(cfg_path))
    monkeypatch.setattr(fleet, "STATE_DIR", tmp_path)
    monkeypatch.setattr(fleet, "TASKS_FILE", tmp_path / "fleet_tasks.json")
    monkeypatch.setattr(fleet, "agent_activity", lambda name: "offline")
    monkeypatch.setattr(fleet, "agent_attention", lambda name: False)

    real_cfg = Path(fleet.__file__).resolve().parent / "fleet.json"
    real_cfg_before = real_cfg.read_bytes()
    marker = "pm-http-fanout-marker"

    srv = ThreadingHTTPServer(("127.0.0.1", 0), fleet.Handler)
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        # creating a project auto-creates its PM agent
        st, body = _post(base + "/api/projects", {"name": "beta"})
        assert st == 200 and body == {"ok": True, "name": "beta"}

        # /api/state surfaces beta-pm with manager_of=="beta" and an empty project
        st, body = _get(base + "/api/state")
        assert st == 200
        by_name = {a["name"]: a for a in body["agents"]}
        assert "beta-pm" in by_name
        assert by_name["beta-pm"]["manager_of"] == "beta"
        assert by_name["beta-pm"]["project"] == ""

        # add a worker into beta, then group fan-out hits the worker only (PM excluded)
        st, body = _post(base + "/api/agents",
                         {"name": "ht_worker", "project_dir": str(wdir), "project": "beta"})
        assert st == 200 and body == {"ok": True, "name": "ht_worker"}
        st, body = _post(base + "/api/tasks/group", {"project": "beta", "description": marker})
        assert st == 200 and body["count"] == 1
        queued = [x for x in fleet._load_tasks()["tasks"] if x["description"] == marker]
        assert {x["agent"] for x in queued} == {"ht_worker"}   # PM not in fan-out

        # deleting the project removes its PM agent
        st, body = _post(base + "/api/projects/delete", {"name": "beta"})
        assert st == 200 and body == {"ok": True}
        st, body = _get(base + "/api/state")
        assert "beta-pm" not in {a["name"] for a in body["agents"]}
    finally:
        srv.shutdown()
        t.join(timeout=5)

    # the live fleet.json is untouched
    assert real_cfg.read_bytes() == real_cfg_before
