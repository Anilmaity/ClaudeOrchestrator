# Task: researcher — Fleet improvement roadmap

You are the **researcher** agent. Follow the coordination rules in
`docs/fleet-improvement/PLAN.md` (especially: **do NOT run git**, and only create the one
file below).

## Deliverable
Create **`docs/fleet-improvement/ROADMAP.md`** — a prioritized, actionable roadmap to make
this Claude Orchestrator more **usable, beautiful, and robust**.

## How
1. Read the codebase to ground your analysis: `orch.py`, `fleet.py`, `win_backend.py`,
   `agent_host.py`, `common.py`, `backend.py`, `tmux_backend.py`, `dashboard.html`, and
   `CLAUDE.md`. Skim the tests in `tests/`.
2. Identify concrete improvement opportunities across three themes:
   - **Usability** — CLI ergonomics, the dashboard workflow, error messages, docs, onboarding.
   - **Beauty** — dashboard look & feel (defer detailed visual design to the designer; just
     list what should improve).
   - **Robustness** — process lifecycle, state detection, cross-platform behavior, the
     Windows ConPTY backend, error handling, tests.
3. Note known rough edges already observed: the Windows `.venv` python is a launcher stub
   that doubles process counts (confuses `ps`-style counting); single-PING liveness can
   flap; long task kickoffs needed a separate Enter to submit; `fleet down` had to also
   stop the dispatcher. Capture lessons and any remaining risks you find.

## Format of ROADMAP.md
- A short intro (what the tool is, current state).
- Three sections (Usability / Beauty / Robustness). In each: a table of items with
  **Priority (P0/P1/P2)**, **Effort (S/M/L)**, **What & why**, and **Suggested owner**
  (which agent should do it).
- A final "Top 5 next actions" list.

Be specific and reference files/functions. When done, print a one-line summary.
