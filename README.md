# Claude Orchestrator

Run **multiple autonomous Claude coding terminals** in parallel, coordinated by a
single **manager Claude** you chat with.

```
            you  ⇄  manager Claude  (this project)
                         │  ./orch spawn / list / peek / send / stop
                         ▼
        ┌──────── tmux session "corch" ────────┐
        │  worker-a   worker-b   worker-c  ...  │   each = claude --dangerously-skip-permissions
        │  projectA   projectB   projectC       │   in its own project directory
        └───────────────────────────────────────┘
```

- **Workers** are real, attachable terminals (tmux windows), each running an
  autonomous `claude` in its own project directory.
- **The manager** is a normal Claude session that uses the `./orch` CLI to spawn,
  watch, message, and stop workers. You direct the manager in plain language.

## One-time setup

```bash
sudo apt install -y tmux          # required; only manual step
chmod +x orch orch.py start-manager.sh
```

> **On Windows?** Skip the tmux step — see the **[Windows](#windows)** section below instead.

## Windows

On Windows there is no tmux. The orchestrator uses a **ConPTY backend** instead,
which opens each worker in its own visible console window titled `corch:<name>`.
The backend is selected automatically on Windows; no configuration needed.

### One-time setup (Windows)

```
python -m pip install -r requirements.txt
```

### Running from PowerShell / cmd

Use the `.cmd` entry points that ship with the project:

```powershell
.\orch.cmd spawn --name api --dir C:\proj\api --task "Add a /health endpoint and tests"
.\orch.cmd list
.\orch.cmd peek api
.\orch.cmd send api "use FastAPI, not Flask"
.\orch.cmd wait api
.\orch.cmd stop api          # or:  .\orch.cmd stop --all
```

### Watching workers live (Windows)

Each worker opens in its **own console window** titled `corch:<name>`. Switch to
that window to watch it live. There is no `tmux attach` on Windows.

### Fleet + dashboard (Windows)

```powershell
.\fleet.cmd up               # start all agents + dispatcher + dashboard
                             # -> open http://localhost:8787/
.\fleet.cmd status
.\fleet.cmd add backend "Add a /health endpoint with tests"
.\fleet.cmd down
```

### Backend override

Force a specific backend with the `ORCH_BACKEND` env var:

```powershell
$env:ORCH_BACKEND = "win"    # always use ConPTY (Windows)
$env:ORCH_BACKEND = "tmux"   # always use tmux (Linux/macOS, or WSL)
```

## Use it

```bash
./start-manager.sh                # opens the manager Claude; tell it your projects/tasks
```

Or drive workers yourself:

```bash
./orch spawn --name api --dir ~/proj/api --task "Add a /health endpoint and tests"
./orch list
./orch peek api
./orch send api "use FastAPI, not Flask"
./orch wait api
./orch stop api          # or:  ./orch stop --all
```

## Watch workers live

```bash
tmux attach -t corch     # Ctrl-b w to switch windows, Ctrl-b d to detach
```

## Fleet + dashboard (persistent agents)

For an ongoing setup, define a **fleet** of named agents in `fleet.json` — each is
one long-lived Claude terminal bound to a **role** and a **project directory** —
and drive it from a **web dashboard** that queues tasks and shows status + logs.

```json
// fleet.json
{
  "agents": [
    { "name": "backend",  "role": "Senior backend engineer…", "project_dir": "~/proj/api" },
    { "name": "frontend", "role": "Frontend engineer…",        "project_dir": "~/proj/web" }
  ]
}
```

```bash
./fleet up                 # start all agents + auto-dispatcher + dashboard
                           # -> open http://localhost:8787/
```

In the dashboard you pick an agent, type a task, and queue it. An auto-dispatcher
sends each queued task to its agent when that agent is idle, detects completion,
and advances the queue. You can watch live status, read per-task logs, cancel
pending tasks, and restart agents.

Drive it from the terminal too (handy for the manager Claude):

```bash
./fleet add backend "Add a /health endpoint with tests"
./fleet status
./fleet cancel t-0003
./fleet down               # stop all agent terminals
```

- The role is injected into each agent via `--append-system-prompt`, so it
  actually shapes behavior and survives across tasks.
- `./fleet up` binds to `0.0.0.0` so other devices on your network can reach the
  dashboard. **Anyone who can reach it can queue tasks that run autonomously** —
  use `./fleet up --host 127.0.0.1` to restrict to this machine.

### Projects & groups

Agents can be organized into **projects**. A project is just a *named group of
agents* — group related agents under one (e.g. `core`, `web`), or leave an agent
**ungrouped**. The dashboard shows agents grouped under their project, with any
ungrouped agents on their own.

This is an optional layer on top of `fleet.json` and is **fully backward
compatible** — both fields are optional, so existing configs keep working unchanged:

```json
// fleet.json
{
  "projects": [
    { "name": "core", "path": "~/proj", "description": "Core services" }
  ],
  "agents": [
    { "name": "backend",  "role": "Senior backend engineer…", "project_dir": "~/proj/api", "project": "core" },
    { "name": "frontend", "role": "Frontend engineer…",        "project_dir": "~/proj/web" }
  ]
}
```

- Top-level `"projects"` is an array of `{ name, path?, description? }`. `path` and
  `description` are optional; `path` is only a convenience default directory offered
  when you add a new agent to that project. Omit the key entirely and you simply have
  no projects.
- The per-agent `"project"` field names the project an agent belongs to. Omit it (or
  leave it empty) and the agent is ungrouped. Above, `backend` is in `core` and
  `frontend` is ungrouped.

The dashboard has a dedicated **Projects tab** (`#projects`) that is the single home for
all of this — no hand-editing of `fleet.json` required. Each project is shown with its
member agents and their live status, and from it you can:

- **Create a project** — give it a name (path and description optional).
- **Add agents to a project** — assign an existing agent, or create a brand-new agent
  directly into the project; the running dispatcher hot-reloads `fleet.json` and spawns
  its terminal automatically.
- **Move an agent between projects** — regroup an existing agent, or ungroup it. No
  restart is needed, since grouping never changes the agent's `project_dir`.

You can also act on a whole project at once:

- **Task a whole group** — queue the same task to every agent in a project at once.
- **Rename a project** — its member agents move to the new name automatically.
- **Edit a project** — change its path and description (clearing either empties that field).
- **Delete a project** — removes the project and **ungroups** its agents; the agents
  themselves are not deleted.

The exact API and `fleet.json` schema for this feature live in
`docs/fleet-improvement/DASHBOARD-DESIGN.md`.

### Project managers

Every project always has exactly one **project-manager (PM) agent**, auto-named
`<project>-pm`. It is created automatically when the project is created, removed
when the project is deleted, and renamed alongside the project — so you never add
or remove it by hand.

A PM **coordinates the project's worker agents** — the agents whose `project`
field equals the project name. It is not itself a worker member of the project
(its own `project` stays empty), so it never receives group-task fan-out; it
hands work to its members instead.

To give a PM a goal, **queue a task to `<project>-pm`** — use the **Queue task to
manager** input on the project's card in the Projects tab, or from the terminal:

```bash
./fleet add core-pm "Ship the /health endpoint across the core services"
```

The PM then decomposes the goal into worker-sized tasks and assigns each one to a
member agent (e.g. `./fleet add backend "..."`), monitoring and unsticking them as
they work.

To backfill PMs for projects that predate this feature into an **already-running**
fleet, run:

```bash
./fleet sync-pms           # create any missing <project>-pm agents
```

No restart is needed — the live dispatcher hot-reloads `fleet.json` and spawns the
new PM terminals on its next tick.

## Notes

- Workers and agents are **fully autonomous** (`--dangerously-skip-permissions`):
  they run any command without asking. Only point them at directories you mean to
  hand over.
- All workers/agents share your Claude subscription rate limit — start with 2–3.
- Orchestrator state (worker registry, fleet tasks, roles, logs) lives in
  `~/.claude-orch/`.

See `CLAUDE.md` for the manager's full operating instructions.
