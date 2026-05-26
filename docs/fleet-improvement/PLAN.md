# Fleet Self-Improvement Plan

**Goal:** Make the Claude Orchestrator fleet more **usable**, **beautiful**, and **robust** —
using the fleet itself, with work divided across agents and `orch` (the manager session)
coordinating and unsticking agents.

## Coordination rules (every agent MUST follow)
1. **Stay in your lane.** Only create/edit the files named in your task spec. Do not touch
   another agent's files or unrelated code.
2. **No git.** Do NOT run `git` (no commit/push/checkout/branch). The manager (`orch`)
   reviews the working tree and integrates. You just leave your changes on disk.
3. **Work autonomously**, make reasonable decisions, don't ask questions.
4. **Verify** your own work where possible (e.g. run `py -3.14 -m pytest -q` for code; for
   `dashboard.html`, extract the `<script>` and `node --check` it if node is available).
5. **Finish** by printing a one-line summary of what you produced.

## Manager (orch / this session) responsibilities
- Start the fleet, dispatch tasks, monitor via the dashboard + `peek`.
- If an agent stalls (kickoff not submitted, stuck, or off-track): `peek` it, then
  `fleet send`/`orch send` to nudge, or restart the agent and re-dispatch.
- Review each agent's working-tree changes, run the full suite, then commit/integrate per area.

## Phase 1 — Analyze, design, and independent robustness (parallel, disjoint files)
| Agent | Deliverable | Files |
|---|---|---|
| researcher | Prioritized improvement roadmap (usability/beauty/robustness) grounded in the code + this session's bugs | `docs/fleet-improvement/ROADMAP.md` (new) |
| designer | Concrete visual/UX redesign spec for the dashboard | `docs/fleet-improvement/DASHBOARD-DESIGN.md` (new) |
| backend | Implement the deferred robustness fixes | `tmux_backend.py`, `common.py` + their tests |
| frontend | (holds for Phase 2 — implements the design spec) | — |

## Phase 2 — Implement (after Phase 1 docs land)
- **frontend** implements `DASHBOARD-DESIGN.md` in `dashboard.html`.
- **backend / researcher** pick up the top robustness/usability items from `ROADMAP.md`.
- Manager integrates, runs tests, and merges.

## Definition of done (this initiative's first pass)
ROADMAP.md + DASHBOARD-DESIGN.md exist; backend's robustness fixes are in with tests green;
the dashboard reflects the design; full suite passes; changes integrated on `master`.
