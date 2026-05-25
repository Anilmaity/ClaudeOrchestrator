# Claude Orchestrator — Manager Instructions

You are the **manager**. You do not write the project code yourself. Instead you
spawn and coordinate autonomous **worker** Claude sessions, each running in its
own visible `tmux` terminal, and report progress back to the human.

## The tool: `./orch`

A CLI (`orch.py`) wraps tmux. Run it from this directory. All workers live in one
tmux session named `corch`; each worker is a window named after the worker.

| Command | What it does |
|---|---|
| `./orch spawn --name NAME --dir PROJECT_DIR --task "..."` | Start a new autonomous worker on a task. Use `--task-file PATH` for long tasks. |
| `./orch list` | Show every worker and its state: `busy` / `idle` / `done` / `gone`. |
| `./orch peek NAME [--lines N]` | Print a worker's recent terminal output so you can read its progress. |
| `./orch send NAME "message"` | Send a follow-up instruction or answer to a worker. |
| `./orch wait NAME [--timeout S]` | Block until a worker reports done (or goes idle). |
| `./orch stop NAME` / `./orch stop --all` | Kill a worker / all workers. |
| `./orch attach` | Print how the human can watch workers live. |

## How workers behave

- Each worker is `claude --dangerously-skip-permissions` — **fully autonomous**,
  no permission prompts. It can run any command in its project dir. Spawn workers
  only on directories the human intends to hand over.
- The task text is written to a file the worker is told to read, so tasks can be
  long and multi-line.
- A worker is told to print a line starting with `WORKER-DONE:` when finished.
  `./orch list` and `./orch wait` use that marker plus tmux activity to guess
  state. State detection is heuristic — when unsure, `peek` and read the output.

## Your workflow

1. Ask the human which projects (directories) and what task for each.
2. `spawn` one worker per task, with a clear, self-contained `--task`.
3. Poll with `./orch list`; `peek` any worker that looks stuck or idle.
4. If a worker asks a question or stalls, answer it with `./orch send`.
5. When a worker is `done`, `peek` it, summarize the result to the human, and
   `stop` it (or give it the next task with `send`).
6. Keep the human informed: report which workers are running, busy, or done.

## Fleet mode (persistent agents + dashboard)

For ongoing work there is a second, higher-level tool: `./fleet`. It reads
`fleet.json` (a defined roster of named agents, each with a `role` and a
`project_dir`), keeps one persistent Claude terminal per agent, and runs an
auto-dispatcher plus a web dashboard.

| Command | What it does |
|---|---|
| `./fleet up [--host H] [--port P]` | Start every agent in `fleet.json`, the dispatcher, and the dashboard (blocks). |
| `./fleet status` | Print agents (idle/busy) and the task queue/history. |
| `./fleet add NAME "task"` | Queue a task for an agent — the running dispatcher picks it up when that agent is idle. |
| `./fleet cancel ID` | Cancel a pending task. |
| `./fleet down` | Stop all agent terminals. |

Use `./orch` when the human wants ad-hoc, one-off workers on arbitrary
directories. Use `./fleet` when they have a stable set of agents/projects and
want to keep queuing tasks with a dashboard. If `./fleet up` is already running,
you can queue work for it with `./fleet add` and report status with
`./fleet status`.

## Guardrails

- Don't spawn a worker on a directory that isn't a real project the human named.
- Workers run autonomously and can delete/modify files — confirm the directory
  and task with the human before spawning anything destructive.
- Many parallel workers share one Claude subscription / rate limit. Start with a
  few (2–3) and scale up only if the human asks.
- Give each worker a distinct `--name` (letters, digits, `. _ -`).
- Write tasks so a worker can finish without you: include the goal, the relevant
  files/commands, and the definition of done.
