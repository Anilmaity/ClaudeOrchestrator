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

## Notes

- Workers and agents are **fully autonomous** (`--dangerously-skip-permissions`):
  they run any command without asking. Only point them at directories you mean to
  hand over.
- All workers/agents share your Claude subscription rate limit — start with 2–3.
- Orchestrator state (worker registry, fleet tasks, roles, logs) lives in
  `~/.claude-orch/`.

See `CLAUDE.md` for the manager's full operating instructions.
