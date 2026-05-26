# Task: designer — Dashboard redesign spec

You are the **designer** agent. Follow the coordination rules in
`docs/fleet-improvement/PLAN.md` (especially: **do NOT run git**, **do NOT edit
`dashboard.html`** — frontend will implement from your spec; only create the one file below).

## Deliverable
Create **`docs/fleet-improvement/DASHBOARD-DESIGN.md`** — a concrete, implementable visual &
UX redesign spec for the fleet dashboard, so the frontend agent can build it without guessing.

## Context
Read the current `dashboard.html` to understand what exists: a dark-theme dashboard that
polls `/api/state` every 2.5s and shows: a header summary, an "attention" strip, a New-task
card, an Agents column (status, role, path, current task, message box, live-log/restart
buttons), a Documents panel (scope select, file list, upload, insert-into-task), and a Tasks
table. API endpoints available: `GET /api/state`, `GET /api/logs?agent=&lines=`,
`GET /api/task/log?id=`, `GET /api/docs?scope=&name=`, `POST /api/tasks`,
`POST /api/task/cancel`, `POST /api/agent/send`, `POST /api/agent/restart`,
`POST /api/docs/upload|delete`. Keep it **vanilla HTML/CSS/JS, single file, no build step,
no new dependencies** (a hard constraint).

## What to specify
- **Visual language:** refined dark palette (give exact hex values / CSS variables),
  typography scale, spacing, radius, elevation/shadows, accent + status colors.
- **Layout:** overall grid, responsive behavior, what's above the fold, how the
  attention strip / agents / tasks / documents are arranged.
- **Components:** redesigned agent card (status, activity, current task, quick actions),
  task row/table, the New-task composer, the Documents panel, the log modal.
- **Interaction/UX:** clearer status affordances, empty states, loading/error states,
  keyboard shortcuts, at-a-glance "what needs me" emphasis.
- Provide small ASCII wireframes and exact CSS snippets where helpful.
- Call out only same-origin, dependency-free techniques.

Make it specific enough to implement directly. When done, print a one-line summary.
