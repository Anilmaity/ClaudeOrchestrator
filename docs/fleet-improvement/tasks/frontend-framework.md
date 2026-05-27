# Task: frontend — Migrate the dashboard to a lightweight framework (Alpine.js)

You are the **frontend** agent. Follow `docs/fleet-improvement/PLAN.md` rules: **do NOT run
git**; only edit `dashboard.html`.

## Goal
Make the dashboard easier to maintain by introducing **Alpine.js** (a lightweight,
declarative, **no-build** framework) for state/reactivity — replacing the hand-written
imperative DOM-building (`renderAgents`, `renderTasks`, `renderDocs`, innerHTML string
templates) with reactive Alpine bindings driven by a single state object.

## Approach
1. Read the current `dashboard.html` fully first.
2. Load Alpine via a single CDN tag (no build step), e.g. in `<head>`:
   `<script defer src="https://unpkg.com/alpinejs@3.x.x/dist/cdn.min.js"></script>`
   (pin to the latest 3.x). If you prefer, vendor the file locally instead, but a CDN tag is
   acceptable and simplest.
3. Introduce one root `x-data` store holding the polled state (`agents`, `tasks`, docs, the
   summary, connection status, the active modal/log) and the small bits of UI state.
4. Replace the imperative render functions with Alpine templates: `x-for` over agents and
   tasks, `x-text`/`x-show`/`:class` bindings, `@click`/`@keydown` handlers, `x-model` for the
   inputs. Keep the 2.5s poll updating the store (`fetch('/api/state')` → assign to state).

## Hard constraints (do not break)
- **No build step, single `dashboard.html` file, same-origin only.**
- **Keep every `/api/*` call working unchanged**: `/api/state`, `/api/logs`, `/api/task/log`,
  `/api/docs`, `/api/tasks`, `/api/task/cancel`, `/api/agent/send`, `/api/agent/restart`,
  `/api/docs/upload`, `/api/docs/delete`.
- **Keep all current features**: new-task composer, agents list (status/role/path/current
  task + message box + live-log + restart), attention strip, tasks table (per-row log/cancel),
  documents panel (scope select / list / upload / delete / insert-into-task), the log viewer,
  connection status, refresh countdown, and keyboard shortcuts.
- Keep the existing visual design (the recent redesign) — this is a state-management refactor,
  not a visual redo.

## Verify (no browser available to you)
- Confirm exactly one `<script defer ...alpine...>` tag and that the rest of the JS is valid:
  extract the inline `<script>` (the non-Alpine one) and `node --check` it if node is present.
- Confirm every `fetch("/api/...")` endpoint above still appears in the file, and no feature's
  handler was dropped.

When done, print a one-line summary of what changed.
