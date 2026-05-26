# Claude Orchestrator — Improvement Roadmap

## What this tool is, and where it stands

The Claude Orchestrator runs **multiple autonomous `claude` coding terminals in
parallel**, coordinated from one manager session. Two layers sit on the same
terminal primitives:

- **`orch`** (`orch.py`) — ad-hoc workers: `spawn` / `list` / `peek` / `send` /
  `wait` / `stop` on arbitrary directories.
- **`fleet`** (`fleet.py`) — a persistent roster from `fleet.json`, an
  auto-dispatcher (`Dispatcher.tick`), a task queue, and a web dashboard
  (`dashboard.html`).

A pluggable backend (`backend.get_backend`) abstracts the terminal: **tmux** on
Unix (`tmux_backend.py`), **ConPTY** on Windows (`win_backend.py` driving
`agent_host.py`, which renders the TUI with `pyte` via `screen_buffer.py` and
exposes a localhost control socket). Worker/agent **state is heuristic** —
inferred from TUI footer text (`common.BUSY_MARKERS`, `READY_MARKERS`,
`TRUST_MARKERS`, `BYPASS_MARKER`).

**Current state:** functional on both platforms with a usable dashboard, atomic
JSON state stores, and good unit coverage of the *leaf* pieces (backend
selection, ConPTY control protocol, doc store, env scrubbing, kill semantics).
The **gaps are concentrated in the dispatcher state machine** (untested,
single-read state transitions) and in **resilience to TUI drift** (every state
decision keys off literal on-screen strings). The recent bug sweep —
double-process counting under the Windows `.venv` python stub, single-PING
liveness flap, kickoff submission needing a separate Enter, `fleet down` not
stopping the dispatcher — points at exactly these two seams. Everything below is
prioritized against that.

Priority: **P0** ship-blocking / correctness · **P1** important · **P2** nice to
have. Effort: **S** <½ day · **M** ~1 day · **L** multi-day. Owners refer to
fleet agents in `fleet.json` (`backend`, `frontend`, `researcher`, `designer`).

---

## Usability

CLI ergonomics, dashboard workflow, error messages, docs, onboarding.

| Pri | Eff | Item — what & why | Owner |
|-----|-----|-------------------|-------|
| P1 | M | **Long/multiline tasks for `fleet`.** `orch` writes the task to a file the worker reads (`orch.py:122`, kickoff at `:133`), so prompts can be long. `fleet` has no equivalent: `_kickoff` (`fleet.py:217`) inlines `task['description']`, and both senders flatten whitespace via `" ".join(text.split())` (`agent_host.py:116`, `tmux_backend.py:66`), collapsing any markdown/newlines to one line. Add `fleet add --task-file PATH` (and a dashboard file-or-textarea) that persists the task to disk and kicks the agent off by *pointing it at the file*, mirroring `orch`. | backend |
| P1 | S | **`fleet logs NAME` / `fleet peek NAME` CLI.** The dashboard can show live + per-task logs, but from the terminal the manager must drop to `orch peek` to read a fleet agent. Add a thin command over `orch._capture` so the manager can inspect a fleet agent without switching tools. | researcher |
| P1 | M | **Onboarding gaps in `README.md`.** Three things bite new users and are only encoded in code/comments: (1) `claude` refuses to launch inside a virtualenv, handled silently by `common.clean_child_env` / the tmux launcher — document it; (2) the Windows `python3` Store-alias stub, worked around in the `orch`/`fleet` bash wrappers — document the `python` vs `python3` caveat; (3) there is no `fleet init` to scaffold `fleet.json`, so first run dies in `load_config` (`fleet.py:66`). Add a `fleet init` that writes a template, and a "Troubleshooting" section. | researcher |
| P1 | S | **`orch list` / `fleet status` are one-shot.** The manager polls by re-running. Add `--watch [secs]` (reuse `_worker_state` / `build_state_one`) for a refreshing table, so "is anyone idle yet?" doesn't require a loop. | backend |
| P2 | S | **`orch stop` dead branch + confusing arg.** `cmd_stop` tests `a.name == "--all"` (`orch.py:209`) but argparse already binds `--all` to `a.all` and leaves `name` defaulting to `""`; the string compare never fires. Remove it and make "no name and no `--all`" the single clear error. | backend |
| P2 | S | **Reused `orch` worker reads "done" forever.** `_worker_state` returns `done` whenever `DONE_MARKER` appears anywhere in scrollback (`orch.py:73`). After a worker finishes one task and is handed another via `send`, `list` still says `done` until the marker scrolls off. Track a per-worker "last kickoff time" in the registry and only honor a `WORKER-DONE:` line newer than it. | backend |
| P2 | M | **Live-log modal is a static snapshot.** `liveLog` (`dashboard.html:331`) fetches `/api/logs` once; watching a working agent means re-clicking. Make the open modal poll/tail while visible (stop on close). *(Coordinate visual treatment with the designer.)* | frontend |
| P2 | S | **Surface dispatcher delivery state.** Tasks silently retry up to `MAX_TRIES` (`fleet.py:363`) then fail with "agent never started". Expose `tries` / `saw_busy` in `build_state` and the task row so a stuck delivery is visible before it fails. | frontend |

---

## Beauty

Dashboard look & feel. **Detailed visual design is owned by the designer in
`docs/fleet-improvement/DASHBOARD-DESIGN.md`** — this is only the punch list of
*what* should improve; the *how* lives there.

| Pri | Eff | Item — what & why | Owner |
|-----|-----|-------------------|-------|
| P1 | M | **Replace `alert()` / `confirm()` with in-app UI.** Errors and destructive confirms use native dialogs (`uploadDoc`, `deleteDoc`, `restart` in `dashboard.html`) — jarring and unstyled. Move to inline toasts + a styled confirm. | designer → frontend |
| P1 | M | **Task table needs filter / sort / search.** `build_state` returns the last 200 tasks (`fleet.py:412`) rendered as one flat table (`renderTasks`). Add status filter, agent filter, and text search; show a count when truncated. | designer → frontend |
| P2 | S | **First-run / empty states.** Only "No tasks yet." and "No documents." exist; the agents column and a fresh fleet have no guidance. Add empty-state copy + a connection-lost banner (today only `#livedot` turns red on poll failure, `refresh()` catch). | designer → frontend |
| P2 | M | **Theme & density.** The palette is a fixed dark theme (`:root` vars). Offer a light theme and a compact density toggle. | designer → frontend |
| P2 | S | **Mobile/responsive polish.** Below 820px the grid collapses to one column (`@media`), but agent reply rows and the doc upload control get cramped. Tighten the small-screen layout. | designer → frontend |

---

## Robustness

Process lifecycle, state detection, cross-platform behavior, the ConPTY backend,
error handling, tests.

| Pri | Eff | Item — what & why | Owner |
|-----|-----|-------------------|-------|
| P0 | M | **False "done" from a transient capture.** In `Dispatcher.tick` a running task that has seen busy is marked **done** on the *first* idle reading (`fleet.py:354-360`). But `agent_activity` (`fleet.py:172`) returns `idle` whenever `orch._capture` yields `""`, and `WinBackend.capture` returns `""` on any dropped socket read (`win_backend.py:119-123`) — with **no retry**, unlike `worker_exists`, which already learned this lesson and pings 3× (`win_backend.py:86-90`). One hiccup ends a task early with a truncated log. Fix: require **N consecutive idle reads** to confirm completion, and/or retry `capture` like `PING`. | backend |
| P0 | M | **The dispatcher state machine is untested.** Tests cover backends, the control protocol, the doc store, kill semantics, and `_stop_dispatcher`, but **nothing exercises `Dispatcher.tick`** — the highest-bug-density code (pending→running→done/failed, `saw_busy`, redelivery, `MAX_TRIES`, terminal-closed→failed). Add tests with a fake backend driving scripted `agent_activity`/`worker_exists` sequences; pair them with the debounce fix above so the regression is locked in. | backend + researcher |
| P1 | M | **Done vs. blocked-on-a-question.** `agent_attention` (`fleet.py:189`) detects an agent paused on a prompt, but `tick` never consults it: an agent that stops to ask the human goes idle and is recorded **done** with a misleading log. Make `tick` treat attention-while-idle as *still running / needs-you*, not done, and reflect it in the task row. | backend |
| P1 | M | **No max-runtime watchdog.** A wedged agent that keeps showing the busy marker stays `running` forever — the queue head-of-line-blocks that agent and nothing ever fails. Add a configurable per-task timeout (from `started_at`) that marks the task failed/needs-attention and frees the agent. | backend |
| P1 | M | **State detection is brittle to TUI drift.** *Every* lifecycle decision keys off literal footer/dialog strings (`common.BUSY_MARKERS` etc.); auto-accept of the trust/bypass dialogs matches on-screen text in `_pump` (`agent_host.py:228-239`) and `_wait_ready` (`tmux_backend.py:81-94`). If Claude reworps any of these, agents hang unaccepted (spawn times out to "started (not confirmed ready)") or read busy/idle wrong. Centralization in `common.py` is good; add a smoke check that flags when markers stop matching and a bounded fallback so a never-settling dialog doesn't wedge spawn. | backend + researcher |
| P2 | S | **Silent dashboard 500s.** `Handler.do_GET`/`do_POST` wrap everything in `except Exception` → opaque `{"error":"internal error"}` (`fleet.py:478,539`) and `log_message` is silenced (`:417`). Keep the opaque client response, but log the traceback to stderr so dashboard failures are diagnosable. | backend |
| P2 | S | **Kill can leave a responding survivor + stale status file.** `WinBackend.kill` warns and *keeps* the status file if the worker still answers PING after two taskkill passes (`win_backend.py:160-168`); `list` then keeps showing a "live" agent that won't die. Add an escalation/retry path (or a `--force` reap) and document the manual recovery. | backend |
| P2 | S | **Whitespace flattening loses fidelity on follow-ups.** `inject` / `send_text` collapse runs of whitespace and newlines (`agent_host.py:116`, `tmux_backend.py:66`). Necessary to defeat paste-swallowing for kickoffs, but it also mangles `fleet send` / `orch send` messages that carry code or structure. Consider a `--raw`/file path for follow-ups that need exact text. | backend |
| P2 | S | **Document the `.venv` double-process lesson.** Under the Windows Store/`py`-launcher stub, `WinBackend.spawn` via `sys.executable` (`win_backend.py:99`) produces a stub parent + real child, doubling `ps`-style counts. The code correctly avoids process counting (liveness = control-socket PING; kill = recorded `pid`/`child_pid` from `agent_host._write_status`). Record this as a standing rule so no future change reintroduces count-based liveness. | researcher |

### Remaining risks / lessons carried from the bug sweep

- **Heuristic state is the load-bearing assumption.** Busy/idle/ready/done all
  derive from TUI text. This is the single biggest fragility; the debounce,
  attention, watchdog, and drift-smoke items above all harden the same seam.
- **Liveness must never count processes** — the `.venv` stub proved why. PING +
  recorded pids only.
- **Single reads flap; require confirmation.** `worker_exists` already retries
  PING 3×; `capture`-driven completion does not, and should.
- **Lifecycle ordering matters.** `fleet down` now stops the dispatcher *before*
  killing agents (`_stop_dispatcher`, `fleet.py:656`) so the keep-alive loop
  can't respawn them; the single-dispatcher pidfile guard (`cmd_up`,
  `fleet.py:572`) prevents two dispatchers fighting. Preserve both invariants in
  any lifecycle refactor.
- **Kickoff submission is timing-sensitive.** Body then a *separate, delayed*
  Enter (`agent_host.inject`, `tmux_backend.send_text`) is what makes long
  prompts actually submit; don't re-bundle them into one write.

---

## Top 5 next actions

1. **Debounce task completion (P0, backend).** Require N consecutive idle reads
   and/or retry `capture` so a dropped socket read can't end a task early
   (`fleet.py:354-360`, `win_backend.py:119-123`).
2. **Test the dispatcher (P0, backend + researcher).** Cover `Dispatcher.tick`'s
   full state machine with a scripted fake backend; land it alongside #1 so the
   false-done regression is pinned.
3. **Respect `agent_attention` in `tick` (P1, backend).** Stop marking
   blocked-on-a-question agents as done; surface them as needs-you
   (`fleet.py:189`).
4. **Add a per-task runtime watchdog (P1, backend).** Time out wedged `running`
   tasks so a stuck agent can't head-of-line-block its queue forever.
5. **Implement the dashboard redesign + onboarding docs (P1, frontend +
   researcher).** Frontend builds out `DASHBOARD-DESIGN.md` (toasts over
   `alert()`, task filter/search, live-tailing log modal); researcher closes the
   README gaps (`fleet init`, venv caveat, `python3` stub, troubleshooting).
