# Windows backend for the Claude Orchestrator — Design

**Date:** 2026-05-26
**Status:** Approved (design); pending implementation plan
**Author:** Manager session

## Problem

The orchestrator (`orch.py`) and the persistent-agent fleet (`fleet.py`) are
built entirely on **tmux**: every worker is a tmux window, output is read with
`capture-pane`, input is injected with `send-keys`, and lifecycle is managed via
tmux sessions/windows. On the target machine (native Windows 11, git-bash +
PowerShell) **tmux is not installed and cannot easily be installed** (no
msys2/pacman; WSL present but with no Linux distro). As a result:

- `./orch spawn` (and everything in `fleet.py`) hard-fails with
  `tmux is not installed`.
- The `orch`/`fleet` bash wrappers call `python3`, which on this box resolves to
  the non-functional Microsoft Store alias stub; real Python is `python`.
- The bash wrappers don't run from PowerShell, the user's default shell.

## Goal

Make the orchestrator run natively on Windows with the **same command surface**
(`spawn`, `list`, `peek`, `send`, `wait`, `stop`, `attach`, and all of `fleet`)
and the **same behavior**: a live, watchable Claude TUI per worker, readable
output, injectable follow-up messages, and accurate busy/idle/done state — while
keeping Linux/macOS (tmux) working unchanged.

### Chosen approach

A faithful tmux-equivalent: drive a **real interactive `claude` TUI** per worker
through a **Windows pseudo-console (ConPTY)**, rendering its screen with a
terminal emulator so existing text-marker state detection keeps working. This is
"Approach B" from brainstorming, selected deliberately over the lower-risk
headless model for maximum fidelity to current behavior.

### Non-goals

- Replacing the interaction model with headless `claude -p` turns (rejected).
- Supporting WSL/tmux on Windows (rejected; defeats the purpose).
- Changing `fleet.json`, the dashboard UI, or the public CLI surface.

## Constraints & environment

- Python 3.14.2, pip 25.3. Windows 11 build 26200 (ConPTY fully supported).
  Windows Terminal (`wt.exe`) is installed. `claude` v2.1.144 on PATH.
- New third-party dependencies are accepted (drops the prior "stdlib only"
  rule): **`pywinpty`** (ConPTY) and **`pyte`** (ANSI screen rendering),
  pinned in `requirements.txt`.
- **Risk:** Python 3.14 is new; a `pywinpty` wheel for 3.14 may not exist yet.
  Mitigation: a `ctypes`-based ConPTY fallback behind the same backend seam
  (see "Risks").

## Architecture

### 1. Pluggable backend seam

Extract all terminal operations from `orch.py` into a backend interface chosen
by platform. New/changed modules:

- **`backend.py`** — abstract `Backend` interface + `get_backend()`. Selects by
  `sys.platform` (`win32` → Windows backend, else tmux), overridable with the
  `ORCH_BACKEND` environment variable (`tmux` | `win`) for testing.
- **`tmux_backend.py`** — today's tmux logic moved verbatim out of `orch.py`
  (Linux/macOS unchanged).
- **`win_backend.py`** — new ConPTY-based implementation.

`orch.py` keeps its existing private helper names (`_capture`, `_send_text`,
`_window_exists`, `_spawn_window`, `_session_exists`, `_windows`, `_worker_state`,
`_have_tmux` → `_backend_available`, etc.) as **thin delegations** to the active
backend. Because `fleet.py` imports those helpers from `orch`, **`fleet.py` and
`dashboard.html` need no logic changes** (only the few direct `orch._tmux(...)`
calls in `fleet.py` are replaced with backend methods — see below).

#### Backend interface (proposed)

```python
class Backend:
    def available(self) -> bool: ...                 # deps present?
    def install_hint(self) -> str: ...               # message when unavailable
    def session_exists(self) -> bool: ...
    def list_workers(self) -> list[str]: ...
    def worker_exists(self, name: str) -> bool: ...
    def spawn(self, name, project_dir, role_file="",
              ready_timeout=45.0) -> tuple[bool, str]:  # (ready, error)
    def capture(self, name: str, lines: int = 200) -> str:   # rendered text
    def send_text(self, name: str, text: str) -> None:       # inject + Enter
    def kill(self, name: str) -> None:
    def kill_all(self) -> None:
    def set_scrollback(self, lines: int) -> None: ...        # tmux-only; noop on win
    def attach_hint(self) -> str: ...                        # how to watch
```

State classification (`busy`/`idle`/`done`/`gone`) stays in `orch.py`/`fleet.py`
on top of `capture()` + the existing `BUSY_MARKERS`/`READY_MARKERS`/`DONE_MARKER`
/`TRUST_MARKERS`/`ATTENTION_MARKERS`, since `capture()` returns real rendered
TUI text in both backends.

### 2. The Windows worker: a per-worker host process in a visible console

`./orch` commands are short-lived, but a ConPTY + its child must persist between
invocations. tmux solved this with a long-lived server; on Windows each worker
is a long-lived **`agent_host.py`** process.

`win_backend.spawn()` launches `agent_host.py` in its **own visible console
window** via `subprocess.Popen(..., creationflags=CREATE_NEW_CONSOLE)`
(`subprocess.CREATE_NEW_CONSOLE`, stdlib). Window title = worker name.
(Optional later refinement: launch as a `wt.exe` tab for a tmux-like grouped
window; not required for v1.)

Each `agent_host.py`:

1. Allocates a **ConPTY via `pywinpty`** and starts
   `claude --dangerously-skip-permissions [--append-system-prompt <role>]`
   inside it. Inherits the venv-sanitizing behavior of today's launcher (unset
   `VIRTUAL_ENV`, strip its `bin`/`Scripts` from PATH) so claude will start.
2. Pump loop (thread): read pty bytes → (a) write raw to the host's own console
   stdout so the window shows the **live claude TUI** (host enables
   `ENABLE_VIRTUAL_TERMINAL_PROCESSING` on its console), and (b) feed bytes to a
   **`pyte` screen** kept as the current rendered snapshot.
3. **Control server** (daemon thread) on a localhost socket (`127.0.0.1`,
   ephemeral port recorded in the registry). Line-oriented protocol:
   - `PING` → `OK`
   - `STATE` → JSON `{state, started_at, pid}`
   - `CAPTURE <n>` → last `n` rendered lines from the pyte screen
   - `SEND <text>` → write `text` + `\r` into the pty
   - `STOP` → terminate claude + ConPTY, then exit (closes the window)
4. **Readiness + trust handling moved into the host:** the host watches its own
   pyte screen for the "Bypass Permissions" prompt (`BYPASS_MARKER` → send `2`,
   Enter), the folder-trust dialog (`TRUST_MARKERS` → Enter), and the ready
   footer (`READY_MARKERS`). It marks itself ready over the socket. This is more
   reliable than today's external polling.

### 3. Data flow per command

- **spawn:** write task file → `win_backend.spawn()` launches the host in a new
  console → host boots claude, auto-accepts dialogs, reports ready → CLI sends
  the kickoff message via `SEND` → registry records `{name, project_dir,
  task_file, pid, port, created_at, status}`.
- **list / peek:** CLI connects to each host socket; `STATE` + `CAPTURE` feed the
  existing marker heuristics to classify state and print output. Unreachable
  socket / dead PID → `gone`.
- **send:** CLI → `SEND <text>` → host injects into the pty (a new prompt
  submission, exactly like `send-keys`).
- **wait:** unchanged polling loop over `_worker_state`.
- **stop / stop --all:** CLI sends `STOP` (falls back to killing the PID from the
  registry if the socket is unreachable); `--all` iterates the registry.
- **attach:** prints guidance — the worker windows are already visible; lists
  worker window titles. (No tmux-style attach needed.)

### 4. State & registry

A `win` area under `STATE_DIR` (`~/.claude-orch/win/`):

- `workers.json` already exists for orch's registry; extend per-worker entries
  with `pid` and `port`.
- `~/.claude-orch/win/<name>/status.json` written by each host:
  `{pid, port, state, started_at, last_render}`.

Liveness:
- `worker_exists(name)` = registry/status PID alive (`os.kill(pid, 0)` /
  `OpenProcess`) **and** socket answers `PING`.
- `session_exists()` = at least one live worker.
- `kill_all()` = iterate registry, `STOP` each (PID-kill fallback).

### 5. Entry points (`python3` / PowerShell gap)

- Add **`orch.cmd`** and **`fleet.cmd`** (call `python "%~dp0orch.py" %*`) so
  `orch ...` / `fleet ...` work directly from PowerShell and cmd.
- Update the existing bash `orch`/`fleet` wrappers to prefer `python` when
  `python3` is the Windows Store stub (probe and fall back), keeping git-bash
  usage working without breaking real-`python3` Unix systems.
- Add **`requirements.txt`** (`pywinpty`, `pyte`) and a README note:
  `pip install -r requirements.txt`.

## Error handling & edge cases

- **Deps missing:** `win_backend.available()` returns False if `winpty`/`pyte`
  can't import; `spawn`/`fleet up` die with a clear
  `pip install -r requirements.txt` message (mirrors today's tmux message).
- **Host crash / claude exits:** PID dies and/or socket refuses → `list`/`status`
  report `gone`; running fleet tasks transition to `failed` exactly as today
  (the existing `[agent terminal closed]` path), since `worker_exists` returns
  False.
- **Socket port in use:** host retries another ephemeral port and rewrites its
  `status.json`/registry entry before reporting ready.
- **Stale registry entries** (PID reused / dead): liveness check requires both
  PID alive and `PING` success, so a reused PID without our socket reads as gone.
- **Orphan windows on stop --all:** registry-driven PID kill guarantees cleanup
  even if a socket is unresponsive.
- **Name validation / duplicate names:** unchanged (`NAME_RE`, existing
  duplicate checks).

## Testing

1. **Backend selection + registry** (cross-platform, no claude): `get_backend()`
   honors `ORCH_BACKEND`; registry read/write round-trips; liveness logic with a
   fake PID/port.
2. **pyte rendering + marker detection** (no claude): feed canned ANSI byte
   streams (including a simulated ready footer and trust dialog) to the host's
   pyte screen; assert `CAPTURE` lines and that `READY_MARKERS`/`TRUST_MARKERS`
   are detected.
3. **Control protocol** (no claude): start a host loop with a dummy child (e.g.,
   `cmd /k` or a tiny echo script) under ConPTY; assert `PING`, `STATE`,
   `CAPTURE`, `SEND`, `STOP` behave.
4. **Integration smoke** (real claude, manual/opt-in): spawn a worker on a
   throwaway dir with a trivial task ("create hello.txt then print WORKER-DONE");
   assert `peek` shows output, `list` reports `done`, the file exists, `stop`
   closes the window and cleans the registry.

## Risks

- **`pywinpty` on Python 3.14:** wheel may be unavailable; building from source
  needs Rust/maturin. *Mitigation:* attempt `pip install pywinpty` first at
  implementation time; if it fails, implement a **`ctypes` ConPTY fallback**
  (`CreatePseudoConsole`, `STARTUPINFOEX` with `PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE`,
  overlapped/threaded pipe reads) behind the same `win_backend` seam — `pyte`
  rendering and the rest of the design are unchanged. Decision point recorded in
  the implementation plan.
- **Raw VT passthrough to the host console:** relies on
  `ENABLE_VIRTUAL_TERMINAL_PROCESSING`; supported on Win11/Windows Terminal. If a
  console doesn't support it, fall back to redrawing the pyte screen each tick.
- **`pyte` fidelity for claude's TUI:** complex redraws could render imperfectly;
  state detection only needs the footer markers to survive, which is robust.

## Out of scope / future

- `wt.exe`-tabbed grouped window (nice-to-have).
- Resize propagation (SIGWINCH-equivalent) to the ConPTY on console resize.
- Reattaching to orphaned hosts after a manager restart beyond what the registry
  already enables.
