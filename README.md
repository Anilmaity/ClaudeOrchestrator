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

## Notes

- Workers are **fully autonomous** (`--dangerously-skip-permissions`): they run
  any command without asking. Only point them at directories you mean to hand over.
- All workers share your Claude subscription rate limit — start with 2–3.
- Orchestrator state (worker registry + task files) lives in `~/.claude-orch/`.

See `CLAUDE.md` for the manager's full operating instructions.
