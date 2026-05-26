# Task: frontend — Implement the dashboard redesign

You are the **frontend** agent. Follow `docs/fleet-improvement/PLAN.md` rules:
**do NOT run git**; only edit `dashboard.html`.

## Deliverable
Implement the redesign specified in **`docs/fleet-improvement/DASHBOARD-DESIGN.md`** by
editing **`dashboard.html`** (the single source file). Read that spec fully first, then read
the current `dashboard.html` to see what exists.

## Hard constraints (do not break these)
- **Single file, vanilla HTML/CSS/JS, NO build step, NO new dependencies, same-origin only.**
- **Keep every existing API call working** exactly as-is: `GET /api/state` (polled every
  ~2.5s), `GET /api/logs?agent=&lines=`, `GET /api/task/log?id=`, `GET /api/docs?scope=&name=`,
  `POST /api/tasks`, `POST /api/task/cancel`, `POST /api/agent/send`, `POST /api/agent/restart`,
  `POST /api/docs/upload`, `POST /api/docs/delete`. Do not rename or drop any of these.
- **Keep all existing functionality**: new-task composer, agents list with status/role/path/
  current-task + message box + live-log + restart, the attention strip, the tasks table with
  per-row log/cancel, the documents panel (scope select, list, upload, delete, insert-into-task),
  the log modal (auto-scroll on open), the elapsed-time ticking.
- Preserve `esc` keyboard handling and the `Ctrl/Cmd+Enter` queue shortcut.

## Scope this round (highest-value visual/UX items from the spec — keep it shippable)
Prioritize, in order, and stop at a clean stopping point if time is short:
1. The refreshed **visual language** (palette/typography/spacing/radius/elevation) from the spec.
2. Redesigned **agent cards** and **task rows** per the spec.
3. **Empty states** + a **connection-lost banner** (today only `#livedot` turns red).
Defer larger behavioral additions (toasts replacing alert(), task filter/search,
live-tailing log modal) unless the above is solid — those can be a later round.

## Verify (you cannot open a browser)
- After editing, if `node` is available, extract the `<script>` block to a temp file and run
  `node --check` on it (expect clean); delete the temp file. Otherwise carefully check
  brace/paren/backtick balance.
- Confirm there is still exactly one `<style>` and one `<script>` block, all the element IDs
  the JS references still exist, and you did not remove any `fetch("/api/...")` call.

When done, print a one-line summary of what you changed.
