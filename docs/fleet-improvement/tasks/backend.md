# Task: backend — Deferred robustness fixes

You are the **backend** agent. Follow the coordination rules in
`docs/fleet-improvement/PLAN.md` (especially: **do NOT run git**; only edit the files below).
Test interpreter: **`py -3.14 -m pytest -q`** (NOT `python` — the `.venv` lacks pytest).
Run the full suite before and after; it must stay green.

These are real, previously-deferred bugs. Fix them with a test for each where feasible.
**Only edit:** `tmux_backend.py`, `common.py`, and their tests
(`tests/test_common.py`, and a tmux test if you add one). Do NOT touch `dashboard.html`,
`fleet.py`, `orch.py`, `win_backend.py`, or `agent_host.py`.

## Fixes
1. **`tmux_backend._tmux` only catches `FileNotFoundError`.** A broken/non-executable tmux
   raises other `OSError` subclasses and crashes the caller. Catch `OSError` broadly and
   return the same `CompletedProcess(args, 127, "", "tmux: not found or not executable")`.

2. **tmux launcher PATH strip misses the venv `bin` when it is the LAST PATH entry.** The
   bash one-liner `export PATH="${PATH//$VIRTUAL_ENV\/bin:/}"` only matches `.../bin:` (with
   a trailing colon), so a trailing `bin` entry (no colon) is not removed. Replace the
   one-liner in `_LAUNCHER_BODY` with a POSIX loop that splits PATH on `:` and drops the
   exact `$VIRTUAL_ENV/bin` entry regardless of position, then `unset VIRTUAL_ENV`.

3. **`READY_MARKERS` contains the over-generic `"for shortcuts"`** which can false-match
   arbitrary tool help text. IMPORTANT: tests rely on the substring `"for shortcuts"`
   (e.g. `test_host_conpty` feeds `"? for shortcuts"`), so do NOT simply delete it — instead
   make it more specific by changing it to `"? for shortcuts"` (the actual Claude footer),
   and confirm the full suite still passes. If any test breaks, adjust the marker minimally
   so both the test and the intent hold.

Do NOT change `NAME_RE` (it would ripple into many call sites) — leave it for a later pass.

When done: run `py -3.14 -m pytest -q`, confirm green, and print a one-line summary of what
you changed and the test result.
