# Task: backend — Dispatcher P1 robustness (attention-aware + runtime watchdog)

You are the **backend** agent. Follow the coordination rules in
`docs/fleet-improvement/PLAN.md` (especially: **do NOT run git**; work autonomously; finish
with a one-line summary). Test interpreter: **`py -3.14 -m pytest -q`** (fall back to
`python -m pytest -q` if `py -3.14` is unavailable). Run the full suite before and after; it
must stay green.

**Only edit:** `fleet.py` and `tests/test_dispatcher.py`. Do NOT touch `dashboard.html`,
`orch.py`, `win_backend.py`, `tmux_backend.py`, `agent_host.py`, or `common.py`.

Read `fleet.py` around `Dispatcher.tick` (currently `fleet.py:331-412`), `agent_activity`
(`:172`), `agent_attention` (`:191`), `build_state` (`:418`), and the helpers `_age`/`_now`
and the constants block (`MAX_TRIES`, `CONFIRM_IDLE`, `CONFIRM_GONE`, `DELIVER_GRACE`). Read
`tests/test_dispatcher.py` fully and reuse its existing fake-backend harness (scripted
`agent_activity`/`worker_exists` via monkeypatch) for new tests.

## Fix 1 — Respect `agent_attention` in `tick` (don't mark a blocked agent "done")
**Problem:** In `tick`, a running task whose agent reads **idle** advances toward `done` after
`CONFIRM_IDLE` consecutive idle reads (`fleet.py:372-381`). But an agent **paused on a
question** (trust/permission prompt or asking the human) also reads idle. `agent_attention(name)`
already detects this, and `build_state` already exposes it per-agent (`:429`), but `tick`
never consults it — so an agent that stops to ask gets recorded **done** with a misleading,
truncated log.

**Fix:** In the running-task loop, when the agent reads idle, first check
`agent_attention(name)`. If it is blocked on attention:
- Do **not** increment `idle_seen` / do not complete the task. Reset `idle_seen` to 0 so it
  can't drift to done while blocked.
- Set a boolean field on the task, e.g. `t["needs_attention"] = True`, and keep
  `status == "running"`. (Do not invent a new status value — keep the existing state machine;
  the flag is what surfaces "needs you".)
- When the agent later resumes (reads `busy` again, or reads idle with attention now False),
  clear `t["needs_attention"]` back to `False` so normal completion can proceed.

Also surface the flag on the task row: include `needs_attention` (default `False`) when tasks
are created/dispatched and ensure it rides along in the task dict that `build_state` returns
(it returns the raw task dicts, so just make sure the field exists). The dashboard wiring is a
later round — this task only needs to **expose the data**, not render it.

## Fix 2 — Per-task runtime watchdog
**Problem:** A wedged agent that keeps showing the busy marker stays `running` forever and
head-of-line-blocks its queue; nothing ever fails it.

**Fix:** Add a configurable per-task timeout measured from `started_at`. Add a constant near
the others, e.g. `TASK_TIMEOUT_SECS`, overridable via env var `FLEET_TASK_TIMEOUT` (seconds);
pick a generous default (e.g. 1800s) so normal long work is never killed. In the running-task
loop, if `_age(t["started_at"]) > TASK_TIMEOUT_SECS`, mark the task `failed` with
`finished_at` set and a clear log line (e.g. `"exceeded max runtime (Ns)"`), and free the
agent so the next pending task for it can dispatch on the same/next tick. Decide sensibly how
this interacts with Fix 1 (a task left blocked-on-attention past the timeout): timing out and
freeing the agent is acceptable — just make the log say it was waiting on attention if
`needs_attention` was set. Guard against `started_at is None`.

## Tests (add to `tests/test_dispatcher.py`)
- **Attention:** agent goes busy (→ `saw_busy`), then reads idle **while `agent_attention`
  returns True** for ≥`CONFIRM_IDLE` ticks → task stays `running` with `needs_attention=True`
  (NOT done). Then agent reads idle with attention False ×`CONFIRM_IDLE` → task completes
  (`done`) and `needs_attention` is cleared.
- **Watchdog:** a `running` task whose `started_at` is older than `TASK_TIMEOUT_SECS` (set a
  small timeout via monkeypatching the constant or the env var) with the agent still busy →
  marked `failed` and the agent freed; assert a `pending` task for the same agent then
  dispatches to `running`.

Keep changes minimal and focused; don't refactor unrelated code. When done: run
`py -3.14 -m pytest -q`, confirm green, and print a one-line summary of what you changed and
the test result.
