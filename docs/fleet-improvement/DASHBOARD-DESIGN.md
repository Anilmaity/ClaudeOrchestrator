# Dashboard Redesign Spec — `dashboard.html`

A concrete, implementable visual & UX redesign for the Claude Fleet dashboard. The
frontend agent should be able to build this **without guessing**: every token has a
hex value, every component has a wireframe + CSS, every field comes from the real API.

> **Hard constraints (non-negotiable).** One file (`dashboard.html`). Vanilla HTML +
> CSS + JS. **No build step. No new dependencies. No external/CDN requests** (same-origin
> only — the page is served by `fleet.py`'s `BaseHTTPRequestHandler` from `/`). No web
> fonts, no icon fonts, no frameworks. Everything below is achievable with plain CSS
> custom properties, grid/flexbox, the native `<dialog>` element, and inline SVG.

---

## 1. Goals & design principles

1. **"What needs me" is the first thing you see.** An idle agent blocked on a question,
   or a failed task, must be impossible to miss — color + icon + text + position.
2. **Agents are the hero.** The fleet of agents is the primary content, shown as a
   responsive card grid, not crammed into a 340px rail.
3. **Calm by default, loud on demand.** Muted dark surfaces; saturated color reserved for
   status and attention only.
4. **Never lie about state.** Status is conveyed by color *and* a glyph *and* a word
   (color-blind safe, see §11). Live durations keep ticking between polls.
5. **Don't yank the UI out from under the user.** Preserve the existing "don't re-render
   the agent column while typing in it" guard; extend it to sorting.
6. **Progressive, not destructive.** All current behavior (2.5s poll, 1s tick, doc insert,
   keyboard queue) is kept; we polish presentation and add states, not remove function.

---

## 2. Data contract (what the UI binds to)

Confirmed from `fleet.py::build_state()` and the handlers. **Do not invent fields.**

`GET /api/state` → 
```jsonc
{
  "agents": [{
    "name": "researcher",
    "role": "Prioritized roadmap",          // may be "" → render "no role set"
    "project_dir": "C:\\path\\to\\proj",     // may be long → truncate, mono
    "activity": "idle",                       // EXACTLY one of: "offline" | "busy" | "idle"
    "attention": true,                        // bool; only ever true when activity === "idle"
    "current_task": "t-0003",                 // task id or null
    "current_desc": "Read designer.md…",      // string or null
    "current_started": "2026-05-27T10:00:00"  // ISO string or null
  }],
  "tasks": [{                                 // newest first, capped 200
    "id": "t-0003",
    "agent": "designer",
    "description": "…",
    "status": "running",                      // EXACTLY one of: pending|running|done|failed|canceled
    "created_at": "ISO",
    "started_at": "ISO | null",
    "finished_at": "ISO | null"
    // (saw_busy/log exist server-side but are not for display)
  }]
}
```

> **Projects & Groups (feature t-0001)** extends `/api/state` and adds four mutation
> endpoints — see **§14** for the full contract (a top-level `projects` array, a
> per-agent `project` field, and `POST /api/projects`, `/api/projects/delete`,
> `/api/agents`, `/api/agent/group`).

Other endpoints (unchanged):

| Call | Request | Response |
|---|---|---|
| `GET /api/logs?agent=&lines=` | — | `{ "text": "…" }` (or `"(agent offline)"`) |
| `GET /api/task/log?id=` | — | `{ "text": "…" }` |
| `GET /api/docs?scope=shared` / `?scope=agent&name=` | — | `{ "files": [{name,size,modified,path}] }` or `{error}` |
| `POST /api/tasks` | `{agent, description}` | `{id}` or `{error}` |
| `POST /api/task/cancel` | `{id}` | `{ok}` |
| `POST /api/agent/send` | `{name, message}` | `{ok}` or `{error}` |
| `POST /api/agent/restart` | `{name}` | `{ok}` |
| `POST /api/docs/upload` | `{scope,name?,filename,content_base64}` | `{file}` or `{error}` |
| `POST /api/docs/delete` | `{scope,name?,filename}` | `{ok}` or `{error}` |

**Status → semantic color mapping** (used everywhere):

| Domain value | Semantic token | Glyph |
|---|---|---|
| agent `busy` / task `running` | `--st-busy` (amber) | ◐ (spinner when live) |
| agent `idle` / task `done` | `--st-ok` (green) | ● / ✓ |
| agent `offline` / task `canceled` | `--st-off` (gray) | ○ / ⊘ |
| agent `attention` / task `failed` | `--st-alert` (red) | ! / ✕ |
| task `pending` | `--st-wait` (slate) | ⋯ |

---

## 3. Design tokens (`:root`) — drop-in CSS

Replace the existing `:root` block with this. Token names are referenced throughout the
spec; please keep them.

```css
:root{
  /* ---- Surfaces (low → high elevation) ---- */
  --bg:          #0e1014;   /* app background            */
  --surface-1:   #15181f;   /* cards / panels            */
  --surface-2:   #1b1f28;   /* nested items (agent card) */
  --surface-3:   #222734;   /* hover / raised            */
  --inset:       #0b0d11;   /* inputs, <pre>, code       */
  --scrim:       rgba(8,10,14,.66);   /* modal backdrop  */

  /* ---- Borders ---- */
  --border:        #2a2f3a;
  --border-strong: #39414f;

  /* ---- Text ---- */
  --text:       #e6e9ef;
  --text-dim:   #9aa3b2;
  --text-faint: #6b7280;

  /* ---- Accent / interactive (brand blue) ---- */
  --accent:        #7aa2ff;
  --accent-hover:  #93b4ff;
  --accent-press:  #5e86e6;
  --accent-ink:    #0b1020;            /* text on a filled accent button */
  --accent-soft:   rgba(122,162,255,.14);
  --ring:          rgba(122,162,255,.50);  /* focus ring */

  /* ---- Status: solid (dot/border/icon) + soft (tinted bg) ---- */
  --st-busy:  #f0b429;  --st-busy-soft:  rgba(240,180,41,.15);
  --st-ok:    #39d98a;  --st-ok-soft:    rgba(57,217,138,.15);
  --st-off:   #6b7280;  --st-off-soft:   rgba(107,114,128,.16);
  --st-alert: #ff5c5c;  --st-alert-soft: rgba(255,92,92,.15);
  --st-wait:  #8b93a7;  --st-wait-soft:  rgba(139,147,167,.14);

  /* ---- Typography ---- */
  --font: system-ui,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  --mono: ui-monospace,"Cascadia Code","Consolas",SFMono-Regular,Menlo,monospace;
  --fs-700:18px; --fs-600:16px; --fs-500:14px; --fs-400:13px; --fs-300:12px; --fs-200:11px;

  /* ---- Spacing (4px base) ---- */
  --sp-1:4px; --sp-2:8px; --sp-3:12px; --sp-4:16px; --sp-5:20px; --sp-6:24px; --sp-8:32px;

  /* ---- Radius ---- */
  --r-sm:6px; --r-md:10px; --r-lg:14px; --r-pill:999px;

  /* ---- Elevation ---- */
  --sh-1: 0 1px 0 rgba(255,255,255,.03), 0 1px 2px rgba(0,0,0,.30);
  --sh-2: 0 8px 24px rgba(0,0,0,.35);
  --sh-pop: 0 16px 48px rgba(0,0,0,.55);

  /* ---- Motion ---- */
  --t-fast:120ms; --t-med:200ms; --ease:cubic-bezier(.2,.6,.2,1);
}

*{box-sizing:border-box}
body{margin:0;font:var(--fs-500)/1.5 var(--font);background:var(--bg);color:var(--text);
     -webkit-font-smoothing:antialiased}
:focus-visible{outline:none;box-shadow:0 0 0 3px var(--ring);border-radius:var(--r-sm)}

@media (prefers-reduced-motion:reduce){
  *{animation-duration:.001ms!important;animation-iteration-count:1!important;
    transition-duration:.001ms!important}
}
```

---

## 4. Layout & responsive grid

Two zones inside a centered, max-width shell. **Agents + Tasks** in the wide primary
column; **Compose + Documents** in a sticky right rail.

```
┌──────────────────────────────────────────────────────────────────────────┐
│  ● Claude Fleet   4 agents · 1 running · 2 pending · 1 done   [⟳ live 0:02] │  header (sticky)
├──────────────────────────────────────────────────────────────────────────┤
│  ⚠  Needs you: researcher   •   1 failed task            [ view ]          │  attention bar (conditional)
├───────────────────────────────────────────────┬──────────────────────────┤
│  AGENTS                                         │  NEW TASK                 │
│  ┌───────────┐ ┌───────────┐ ┌───────────┐     │  Agent  [▼ designer    ]  │
│  │ agent     │ │ agent     │ │ agent     │     │  ┌──────────────────────┐ │
│  │ card      │ │ card      │ │ card      │     │  │ task textarea        │ │
│  └───────────┘ └───────────┘ └───────────┘     │  └──────────────────────┘ │
│                                                 │  [+ insert doc] [Queue ⏎] │
│  TASKS                                          ├──────────────────────────┤
│  ┌──────────────────────────────────────────┐  │  DOCUMENTS                │
│  │ id  agent  status  time  task        ···  │  │  Scope [▼ Shared       ]  │
│  │ ───────────────────────────────────────── │  │  • file.md  3KB  ins del  │
│  │ …                                          │  │  ⤓ drop file / [browse]   │
│  └──────────────────────────────────────────┘  │                           │
└───────────────────────────────────────────────┴──────────────────────────┘
```

```css
.shell{max-width:1400px;margin:0 auto;padding:var(--sp-4)}
.layout{display:grid;grid-template-columns:minmax(0,1fr) 372px;
        gap:var(--sp-4);align-items:start}
.rail{position:sticky;top:76px;          /* clears the sticky header */
      display:flex;flex-direction:column;gap:var(--sp-4)}

/* Agents grid: as many ~300px cards as fit */
.agents-grid{display:grid;gap:var(--sp-3);
             grid-template-columns:repeat(auto-fill,minmax(280px,1fr))}

@media (max-width:1040px){
  .layout{grid-template-columns:1fr}      /* rail drops below board   */
  .rail{position:static}
}
@media (max-width:640px){
  .agents-grid{grid-template-columns:1fr} /* one card per row         */
  .shell{padding:var(--sp-3)}
}
```

**Above the fold (desktop ≥1040px):** header, attention bar (if any), the agents grid,
and the top of the New-task composer. Tasks live directly under the agents grid.

**Stacking order on mobile:** header → attention → New task (compose) → Agents → Tasks →
Documents. (Compose floats above agents on mobile so dispatching stays one scroll away.)

---

## 5. Header & live status

Sticky, full-bleed, subtle gradient. Shows brand, a **connection dot**, summary counts,
and a **poll indicator** that doubles as a manual refresh button.

```
●  Claude Fleet      4 agents · 1 running · 2 pending · 1 done · ⚠ 1 needs you      ⟳ 0:02
```

- **Connection dot** (`#livedot`): green when the last `/api/state` succeeded, red on
  fetch error, amber pulse during an in-flight first load. `title`/`aria-label` describes
  state ("Connected", "Connection lost — retrying").
- **Summary** (`#summary`): `N agents · R running · P pending · D done` plus
  `· ⚠ K needs you` only when `K>0`, in `--st-alert`.
- **Poll indicator** (`⟳ m:ss`): seconds since last successful poll; click = force
  `refresh()`. Keep the existing `document.title` badge `(K) Claude Fleet`.

```css
.header{position:sticky;top:0;z-index:20;display:flex;align-items:center;gap:var(--sp-3);
  padding:var(--sp-3) var(--sp-5);border-bottom:1px solid var(--border);
  background:linear-gradient(180deg,#1b1f29,#15181f);box-shadow:var(--sh-1)}
.header h1{font-size:var(--fs-600);font-weight:600;margin:0}
.header .sub{color:var(--text-dim);font-size:var(--fs-300)}
.livedot{width:9px;height:9px;border-radius:50%;background:var(--st-ok);
  box-shadow:0 0 0 4px color-mix(in srgb,var(--st-ok) 25%,transparent)}
.livedot.bad{background:var(--st-alert);box-shadow:0 0 0 4px var(--st-alert-soft)}
.poll{margin-left:auto;display:inline-flex;align-items:center;gap:6px;
  font:var(--fs-300)/1 var(--mono);color:var(--text-dim);cursor:pointer;
  background:none;border:1px solid var(--border);border-radius:var(--r-pill);
  padding:5px 10px}
.poll:hover{border-color:var(--accent);color:var(--text)}
```
> `color-mix` is supported by all evergreen browsers and needs no dependency; if you want
> to be maximally conservative, hard-code the rgba instead.

---

## 6. Attention bar — "what needs me"

Rendered **only** when `agents.some(a=>a.attention)` **or** `tasks.some(t=>t.status==='failed')`.
Full-width, sits between header and board, red-tinted, `aria-live="assertive"`.

```
⚠  Needs you: researcher, backend      ✕ 1 failed task          [ Jump to first ▸ ]
```

- "Needs you" lists the names of agents with `attention:true`.
- Failed chip shows the count; clicking it could scroll the tasks table to the first
  failed row (nice-to-have).
- "Jump to first" focuses/scrolls to the first attention agent card.

```css
.attention{display:flex;flex-wrap:wrap;align-items:center;gap:var(--sp-2) var(--sp-4);
  margin:0 0 var(--sp-4);padding:var(--sp-3) var(--sp-4);
  border:1px solid var(--st-alert);background:var(--st-alert-soft);
  border-radius:var(--r-md);color:var(--st-alert);font-size:var(--fs-400)}
.attention strong{color:var(--text)}
.attention .chip{display:inline-flex;align-items:center;gap:6px;
  background:rgba(255,92,92,.18);border:1px solid var(--st-alert);
  border-radius:var(--r-pill);padding:2px 10px}
.attention[hidden]{display:none}
```

---

## 7. Components

### 7.0 Shared primitives

**Card / panel**
```css
.card{background:var(--surface-1);border:1px solid var(--border);border-radius:var(--r-lg);
  padding:var(--sp-4);box-shadow:var(--sh-1)}
.card > h2{margin:0 0 var(--sp-3);font-size:var(--fs-200);font-weight:600;
  text-transform:uppercase;letter-spacing:.07em;color:var(--text-dim)}
.section-head{display:flex;align-items:center;justify-content:space-between;
  margin:0 0 var(--sp-3)}
```

**Status badge** (agent activity) and **status pill** (task status) — one CSS family,
both built from the tokens in §2:
```css
.badge{display:inline-flex;align-items:center;gap:6px;font-size:var(--fs-200);
  padding:3px 9px;border-radius:var(--r-pill);border:1px solid var(--border);
  background:var(--inset);text-transform:capitalize}
.badge .d{width:7px;height:7px;border-radius:50%}
.badge[data-s="busy"]{color:var(--st-busy)}    .badge[data-s="busy"]  .d{background:var(--st-busy)}
.badge[data-s="idle"]{color:var(--st-ok)}       .badge[data-s="idle"]  .d{background:var(--st-ok)}
.badge[data-s="offline"]{color:var(--text-dim)} .badge[data-s="offline"].d{background:var(--st-off)}
.badge[data-s="attn"]{color:var(--st-alert);border-color:var(--st-alert)}
.badge[data-s="attn"] .d{background:var(--st-alert);animation:pulse 1.2s ease-in-out infinite}

.pill{display:inline-flex;align-items:center;gap:6px;font-size:var(--fs-200);
  padding:3px 9px;border-radius:var(--r-pill);text-transform:capitalize}
.pill[data-s="pending"] {background:var(--st-wait-soft); color:var(--st-wait)}
.pill[data-s="running"] {background:var(--st-busy-soft); color:var(--st-busy)}
.pill[data-s="done"]    {background:var(--st-ok-soft);   color:var(--st-ok)}
.pill[data-s="failed"]  {background:var(--st-alert-soft); color:var(--st-alert)}
.pill[data-s="canceled"]{background:var(--st-off-soft);  color:var(--text-dim)}

@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
```
> Use `data-s` attributes instead of generated class names — simpler to template and it
> keeps the status string straight from the API.

**Controls (inputs / selects / textarea / buttons)**
```css
input,select,textarea{width:100%;background:var(--inset);border:1px solid var(--border);
  color:var(--text);border-radius:var(--r-sm);padding:9px 10px;font:inherit}
input:hover,select:hover,textarea:hover{border-color:var(--border-strong)}
textarea{resize:vertical;min-height:84px;line-height:1.45}
label{display:block;font-size:var(--fs-300);color:var(--text-dim);margin:var(--sp-3) 0 var(--sp-1)}

button{cursor:pointer;border:1px solid var(--border);background:var(--surface-2);
  color:var(--text);border-radius:var(--r-sm);padding:8px 12px;font:inherit;
  transition:border-color var(--t-fast),background var(--t-fast)}
button:hover{border-color:var(--accent-hover);background:var(--surface-3)}
button.primary{background:var(--accent);border-color:var(--accent);color:var(--accent-ink);
  font-weight:600}
button.primary:hover{background:var(--accent-hover);border-color:var(--accent-hover)}
button.mini{padding:4px 9px;font-size:var(--fs-200)}
button.ghost{background:transparent}
button.danger:hover{border-color:var(--st-alert);color:var(--st-alert)}
button:disabled{opacity:.5;cursor:not-allowed}
```

---

### 7.1 Agent card (redesigned)

The card carries a **status-colored left accent** (4px), a header row (name + badge),
role chip, truncated path, the current task with a live elapsed timer, a message box,
and quick actions. Attention agents get a red ring and float to the top of the grid.

```
┌▎────────────────────────────────────────┐   ▎ = 4px left accent (status color)
│ researcher                    ● idle     │   header: name + status badge
│ Prioritized roadmap                      │   role (chip / dim if empty)
│ C:\…\ClaudeOrchestrator                  │   path (mono, ellipsis, title=full)
│ ┌──────────────────────────────────────┐ │
│ │ ▶ t-0003 · Read designer.md…   0:42  │ │   current task block (only if running)
│ └──────────────────────────────────────┘ │
│ [ message researcher…            ] [Send] │   reply
│ [ ⧉ live log ]            [ ⟲ restart ]   │   quick actions
└──────────────────────────────────────────┘
```

```css
.agent{position:relative;background:var(--surface-2);border:1px solid var(--border);
  border-radius:var(--r-md);padding:var(--sp-3) var(--sp-3) var(--sp-3) calc(var(--sp-3) + 4px);
  display:flex;flex-direction:column;gap:var(--sp-2)}
.agent::before{content:"";position:absolute;left:0;top:0;bottom:0;width:4px;
  border-radius:var(--r-md) 0 0 var(--r-md);background:var(--st-off)}
.agent[data-s="busy"]::before{background:var(--st-busy)}
.agent[data-s="idle"]::before{background:var(--st-ok)}
.agent[data-s="offline"]::before{background:var(--st-off)}
.agent.attn::before{background:var(--st-alert)}
.agent.attn{border-color:var(--st-alert);box-shadow:0 0 0 1px var(--st-alert) inset}

.agent .top{display:flex;align-items:center;justify-content:space-between;gap:var(--sp-2)}
.agent .name{font-weight:600;font-size:var(--fs-500)}
.agent .role{color:var(--text-dim);font-size:var(--fs-300)}
.agent .path{color:var(--text-faint);font-size:var(--fs-200);font-family:var(--mono);
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}     /* title=full path */
.agent .cur{display:flex;align-items:center;gap:8px;font-size:var(--fs-300);
  color:var(--accent);background:var(--accent-soft);border-radius:var(--r-sm);
  padding:6px 8px}
.agent .cur .desc{flex:1;min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.agent .elapsed{color:var(--text-dim);font-variant-numeric:tabular-nums;
  font-family:var(--mono);font-size:var(--fs-200)}
.agent .reply{display:flex;gap:6px}
.agent .reply input{font-size:var(--fs-300)}
.agent .actions{display:flex;gap:6px;justify-content:flex-end}
```

**States**
- *busy*: amber accent, `◐ busy` badge, current-task block visible with ticking timer.
- *idle (no attention)*: green accent, `● idle` badge. If `current_task` is null show a
  faint line: `idle · waiting for work`.
- *attention*: red ring + pulsing red badge `! needs you`. **Card sorts to front.**
- *offline*: gray accent, `○ offline` badge, **dim the whole card to ~70% opacity**,
  disable the message Send button (sending to an offline agent will fail), keep `restart`
  enabled (restart is the recovery action).

**Sorting** (apply only when not typing in the agents region — see §8): 
`attention → busy → idle → offline`, stable by name within a group. The existing
`typingInAgents()` guard must also gate the re-sort so an input doesn't jump away.

---

### 7.2 Tasks table

Keep a table on desktop; **collapse to stacked cards under 640px**. Each row gets a thin
status-colored left edge. Long descriptions clamp to 2 lines with a hover `title` (and an
optional click-to-expand). Time column shows live duration for `running` rows.

```
ID       AGENT       STATUS     TIME    TASK                                  ···
─────────────────────────────────────────────────────────────────────────────────
t-0003   designer    ◐ running  0:42    Read designer.md and produce the…   [log]
t-0002   backend     ✓ done     3m 18s  Implement deferred robustness…      [log]
t-0001   researcher  ⋯ pending  —       Prioritized improvement roadmap…    [log][cancel]
```

```css
.tasks{width:100%;border-collapse:collapse}
.tasks th{text-align:left;padding:8px;font-size:var(--fs-200);text-transform:uppercase;
  letter-spacing:.05em;color:var(--text-dim);border-bottom:1px solid var(--border)}
.tasks td{padding:10px 8px;border-bottom:1px solid var(--border);vertical-align:top;
  font-size:var(--fs-400)}
.tasks tr{transition:background var(--t-fast)}
.tasks tbody tr:hover{background:var(--surface-2)}
.tasks .id{font-family:var(--mono);color:var(--text-dim);white-space:nowrap}
.tasks .time{font-family:var(--mono);font-variant-numeric:tabular-nums;color:var(--text-dim);
  white-space:nowrap}
.tasks .desc{color:var(--text);max-width:480px;
  display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.tasks td:first-child{border-left:3px solid var(--st-wait);padding-left:10px} /* status edge via JS-set inline or per-status class */

/* Mobile: each row becomes a card with labels */
@media (max-width:640px){
  .tasks thead{display:none}
  .tasks,.tasks tbody,.tasks tr,.tasks td{display:block;width:100%}
  .tasks tr{border:1px solid var(--border);border-radius:var(--r-md);
    margin-bottom:var(--sp-2);padding:var(--sp-2);background:var(--surface-2)}
  .tasks td{border:none;padding:4px 0}
  .tasks td[data-label]::before{content:attr(data-label) "  ";color:var(--text-dim);
    font-size:var(--fs-200);text-transform:uppercase;letter-spacing:.05em}
}
```
> For the status-colored left edge, set it per row from the status, e.g. add
> `style="border-left-color:var(--st-running)"` or a `data-s` row attribute with matching
> CSS. Add `data-label="Agent"` etc. on each `<td>` for the mobile card view.

Actions per row: `log` (always), `cancel` (only when `status==='pending'`, danger style).
Empty state: a single full-width row, see §8.

---

### 7.3 New-task composer

Top of the right rail (and sticky there on desktop). Agent select, task textarea, an
`insert doc` affordance, char counter, and a primary **Queue task** button that shows the
`⌘/Ctrl+Enter` hint.

```
NEW TASK
Agent   [▼ designer            ]
Task
┌─────────────────────────────┐
│ Describe what this agent     │
│ should do…                   │
└─────────────────────────────┘
            0 chars   [ + insert doc ]   [ Queue task  ⌘⏎ ]
```

- Disable **Queue** while the textarea is empty/whitespace.
- On success: clear textarea, **toast** `Queued t-0004 → designer` (replaces the silent
  clear), then `refresh()`.
- On `{error}`: toast the error (replaces `alert`).
- Keep `Ctrl/Cmd+Enter` to submit; keep the agent dropdown synced to the roster, preserving
  the current selection across polls (existing behavior).
- "insert doc" appends the selected document's `path` into the textarea — same behavior as
  the current Documents "insert" button; surfacing it here is a convenience (optional).

```css
.compose .meta{display:flex;align-items:center;gap:var(--sp-3);margin-top:var(--sp-2)}
.compose .count{color:var(--text-faint);font-size:var(--fs-200);margin-right:auto}
```

---

### 7.4 Documents panel

Scope select (`Shared (all agents)` + one option per agent), a file list, and an upload
zone that supports **drag-and-drop** (and a click-to-browse fallback). Reuse the existing
base64 upload flow and 25 MB guard.

```
DOCUMENTS
Scope [▼ Shared (all agents)        ]
┌──────────────────────────────────────┐
│ 📄 spec.md      3.1 KB · May 27  ins ✕ │
│ 📄 notes.txt    812 B  · May 26  ins ✕ │
└──────────────────────────────────────┘
┌──────────────────────────────────────┐
│   ⤓  Drop a file here, or [ browse ]   │   dashed drop zone
└──────────────────────────────────────┘
```

```css
.doclist{display:flex;flex-direction:column;gap:6px;margin:var(--sp-2) 0}
.docrow{display:flex;align-items:center;justify-content:space-between;gap:var(--sp-2);
  padding:7px 9px;background:var(--surface-2);border:1px solid var(--border);
  border-radius:var(--r-sm)}
.docrow .name{font-family:var(--mono);font-size:var(--fs-300);
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.docrow .meta{color:var(--text-faint);font-size:var(--fs-200);white-space:nowrap}
.dropzone{margin-top:var(--sp-2);padding:var(--sp-4);text-align:center;
  border:1.5px dashed var(--border-strong);border-radius:var(--r-md);
  color:var(--text-dim);font-size:var(--fs-300);cursor:pointer}
.dropzone.drag{border-color:var(--accent);background:var(--accent-soft);color:var(--text)}
```
Drag-and-drop with no dependency:
```js
const dz = document.querySelector('.dropzone');
['dragenter','dragover'].forEach(ev=>dz.addEventListener(ev,e=>{
  e.preventDefault(); dz.classList.add('drag'); }));
['dragleave','drop'].forEach(ev=>dz.addEventListener(ev,e=>{
  e.preventDefault(); dz.classList.remove('drag'); }));
dz.addEventListener('drop', e=>{ const f=e.dataTransfer.files[0]; if(f) uploadFile(f); });
```
Keep the existing FileReader→base64 path; just refactor `uploadDoc()` to take a `File`
so both the drop and the `<input type=file>` reuse it. Empty state: see §8.

---

### 7.5 Log modal — use native `<dialog>`

Replace the hand-rolled `.modal` div with the native `<dialog>` element: it gives
**focus trapping, Esc-to-close, and backdrop** for free, no dependency. Add a toolbar:
**Copy**, **Wrap** toggle, **Refresh**, and (for live agent logs) **Follow** (auto-refresh
every 2s while open and re-scroll to bottom). Show the source (`Task t-0003 log` /
`Live: researcher`).

```
┌─ Live: researcher ─────────────────── [Copy] [Wrap] [⟳] [Follow] [✕] ─┐
│ ...                                                                   │
│ (monospace, scrollable, scrolled to bottom on open)                   │
└───────────────────────────────────────────────────────────────────────┘
```

```css
dialog.logbox{padding:0;border:1px solid var(--border);border-radius:var(--r-lg);
  background:var(--surface-1);color:var(--text);width:min(960px,94vw);max-height:86vh;
  box-shadow:var(--sh-pop)}
dialog.logbox::backdrop{background:var(--scrim)}
.logbox .hd{display:flex;align-items:center;gap:var(--sp-2);padding:10px var(--sp-4);
  border-bottom:1px solid var(--border)}
.logbox .hd .title{font-weight:600;margin-right:auto}
.logbox pre{margin:0;padding:var(--sp-4);overflow:auto;max-height:74vh;
  font:var(--fs-300)/1.55 var(--mono);color:#d6dbe6;white-space:pre;word-break:normal}
.logbox pre.wrap{white-space:pre-wrap;word-break:break-word}
```
Open/close:
```js
const dlg = document.querySelector('dialog.logbox');
function openLog(title, text){ dlg.querySelector('.title').textContent = title;
  const pre = dlg.querySelector('pre'); pre.textContent = text || '(empty)';
  dlg.showModal(); pre.scrollTop = pre.scrollHeight; }
// Esc & backdrop click both close natively; add a backdrop-click handler:
dlg.addEventListener('click', e=>{ if(e.target === dlg) dlg.close(); });
```
**Follow** stores the source (`{kind:'live', name}` or `{kind:'task', id}`); a
`setInterval` re-fetches and replaces `pre.textContent`, re-scrolling to bottom; clear the
interval on `dlg.close` (listen to the `close` event). Copy uses
`navigator.clipboard.writeText(pre.textContent)` with a fallback selection if unavailable.

---

### 7.6 Toasts (replace `alert()` / silent successes)

A bottom-right stack for transient feedback. Dependency-free.

```css
.toasts{position:fixed;right:var(--sp-4);bottom:var(--sp-4);z-index:40;
  display:flex;flex-direction:column;gap:var(--sp-2);max-width:340px}
.toast{padding:10px 12px;border-radius:var(--r-md);background:var(--surface-3);
  border:1px solid var(--border);box-shadow:var(--sh-2);font-size:var(--fs-400);
  animation:slidein var(--t-med) var(--ease)}
.toast.ok{border-left:3px solid var(--st-ok)}
.toast.err{border-left:3px solid var(--st-alert)}
@keyframes slidein{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}
```
```js
function toast(msg, kind='ok'){ const el=document.createElement('div');
  el.className='toast '+kind; el.textContent=msg;
  document.querySelector('.toasts').append(el);
  setTimeout(()=>el.remove(), kind==='err'?6000:3500); }
```
Use `toast(...,'err')` for every `{error}` response (queue, send, upload, docs); use
`toast('Queued '+id+' → '+agent)` etc. on success. Keep a real `confirm()` only for the
two destructive actions (restart agent, delete doc) — or upgrade them to a small confirm
`<dialog>` if time permits (optional).

---

## 8. UX states (loading / empty / error)

**First-load skeletons.** Before the first `/api/state` resolves, render shimmer
placeholders so the page isn't blank.
```css
.skel{background:linear-gradient(90deg,var(--surface-2),var(--surface-3),var(--surface-2));
  background-size:200% 100%;animation:shimmer 1.2s linear infinite;border-radius:var(--r-sm)}
@keyframes shimmer{from{background-position:200% 0}to{background-position:-200% 0}}
```
Show ~3 skeleton agent cards (a couple of bars each) and 3 skeleton task rows.

**Connection lost.** On a failed poll: set `#livedot` red and show a slim banner under the
header — `Connection lost — retrying…` — auto-dismiss on the next success. Do **not**
clear already-rendered data (show last-known state, just flag it stale).

**Empty states** (centered, dim, with a hint):
- No agents: `No agents configured. Edit fleet.json and run ./fleet up.`
- No tasks: `No tasks yet — queue one from "New task".`
- No docs in scope: `No documents in this scope. Drop a file to add one.`
```css
.empty{padding:var(--sp-6);text-align:center;color:var(--text-dim);font-size:var(--fs-400)}
```

**Optimistic / busy affordances.** While a POST is in flight, disable the triggering
button and show a tiny inline spinner; re-enable on response. Don't block the whole UI.

**Preserve typing.** Keep `typingInAgents()` — never re-render/re-sort the agents grid
while focus is inside an agent's message input. Apply the same guard to the doc list if a
rename/inline field is ever added.

---

## 9. Keyboard shortcuts

| Key | Action |
|---|---|
| `n` or `/` | Focus the New-task textarea |
| `⌘/Ctrl + Enter` | Queue task (when textarea focused) — *keep existing* |
| `Enter` (in agent msg box) | Send message — *keep existing* |
| `r` | Force refresh now |
| `Esc` | Close modal — *native via `<dialog>`* |
| `?` | Toggle a small shortcuts cheat-sheet overlay |

Guard global single-key shortcuts so they don't fire while typing in an input/textarea
(`if(/^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName)) return;` except for
the modifier combos). Show a one-line hint of the key shortcuts in the header or footer.

---

## 10. Motion

- Status dot **pulse** on attention badges only; **shimmer** on skeletons; **slide-in** on
  toasts; 120–200ms color/border transitions on hover. Nothing else animates.
- All of it is gated by the `prefers-reduced-motion` block in §3 — verify it actually
  stops the pulse and shimmer.

---

## 11. Accessibility

- **Status is never color-only.** Every badge/pill includes a word; add a glyph (§2) or
  `aria-label` (`aria-label="status: failed"`) so meaning survives without color.
- **Live regions:** attention bar `aria-live="assertive"`; the summary `aria-live="polite"`.
- **Focus:** the `<dialog>` traps focus and restores it on close (native). Ensure the
  trigger that opened it is what regains focus.
- **Hit targets:** mini buttons keep ≥28px height; the whole doc/drop zone is clickable.
- **Contrast:** body text `--text` on `--bg` ≈ 13:1; `--text-dim` on surfaces stays ≥4.5:1
  for anything that is *information* (use `--text-faint` only for decorative meta). Status
  pill text on its soft tint must clear 4.5:1 — the chosen hex pairs do.
- **Labels:** every `<select>`/`<input>` has an associated `<label>` (the agent message
  box uses `aria-label="message <name>"`). Icon-only buttons get `aria-label`/`title`.
- **Keyboard:** all actions reachable by Tab; `:focus-visible` ring defined in §3.
- Respect `prefers-reduced-motion` (§10) and degrade gracefully if `color-mix`/`<dialog>`
  is unavailable (both are broadly supported; `<dialog>` has been stable in all evergreen
  browsers since 2022).

---

## 12. Implementation notes & constraints

- **One file.** All CSS in the single `<style>`; all JS in the single `<script>`. No
  imports, no `<link>`, no CDN. The server only exposes the listed same-origin endpoints.
- **No build, no deps.** Plain ES (the existing code style: `const $=…`, `fetch`,
  template strings). Don't add a framework or bundler.
- **Icons:** use Unicode glyphs (◐ ● ○ ✓ ✕ ! ⋯ ⟳ ⧉ ⟲ ⤓) *or* a tiny inline `<svg>`
  sprite defined once at the top of `<body>` and referenced with `<use href="#id">`. Both
  are dependency-free; pick one and be consistent. (Unicode is simplest.)
- **Keep all existing behaviors:** 2.5s `setInterval(refresh)`, 1s `setInterval(tick)`,
  live elapsed via `data-since` / `data-running`, doc base64 upload + 25 MB guard, agent
  dropdown sync, `esc()` HTML-escaping on **all** interpolated API strings (names, roles,
  paths, descriptions, filenames). **Do not drop the escaping** — it's the XSS guard.
- **Suggested `<script>` structure** (refactor, don't rewrite wholesale):
  `api()` → `render{Header,Attention,Agents,Tasks,Docs}()` → `refresh()` (orchestrates +
  skeleton/empty/error) → `tick()` → action handlers (`queueTask`,`sendMsg`,`cancel`,
  `restart`,`uploadFile`,`deleteDoc`) → `openLog`/`follow` → `toast` → keybindings → boot.
- **Sorting helper:** `const order={attn:0,busy:1,idle:2,offline:3};` then sort agents by
  `(a.attention?'attn':a.activity)` rank, gated by `!typingInAgents()`.
- **Verify before finishing (PLAN.md rule 4):** extract the `<script>` and run
  `node --check` on it if Node is available. Do **not** run any `git` command.

---

## 13. Acceptance checklist (for the frontend agent)

- [ ] `:root` token block from §3 is in place; no hard-coded one-off colors remain.
- [ ] Two-column layout with sticky rail; collapses correctly at 1040px and 640px.
- [ ] Agents render as a responsive grid, sorted attention→busy→idle→offline (gated by
      typing), with status accent, live timer, message box, live-log + restart.
- [ ] Attention bar appears only when an agent needs you or a task failed; `aria-live`.
- [ ] Tasks table with status pills + live time; becomes stacked cards under 640px.
- [ ] New-task composer: disabled-when-empty, success/err toasts, ⌘/Ctrl+Enter kept.
- [ ] Documents panel with scope select, list, drag-and-drop upload, 25 MB guard, delete.
- [ ] Log modal uses `<dialog>` with Copy / Wrap / Refresh / Follow; Esc + backdrop close.
- [ ] Toasts replace `alert()`; destructive actions still confirm.
- [ ] Skeleton on first load; empty states for agents/tasks/docs; connection-lost banner.
- [ ] Keyboard shortcuts (n//, r, ?, Esc, ⌘⏎) work and don't fire while typing.
- [ ] All API strings HTML-escaped; only same-origin endpoints used; single file, no deps.
- [ ] `prefers-reduced-motion` honored; status conveyed by color **and** text/glyph.
- [ ] `node --check` on the extracted script passes (if Node present); no `git` run.

---

## 14. Projects & Groups API contract (feature t-0001)

The durable, implement-from-this-alone reference for the **Projects & Groups** feature.
It mirrors `docs/fleet-improvement/tasks/projects-groups-contract.md` — the single source
of truth — so a client can be built from this section alone. A **project** is a named
group; an agent is either **assigned to a project** (grouped) or **ungrouped**. Both
additions are optional and fully backward compatible with today's `fleet.json` (no
`projects` key, agents with no `project` field).

### 14.1 `fleet.json` schema (additions)

Two additions, both optional and backward compatible:

```json
{
  "projects": [
    { "name": "core", "path": "C:/path/optional", "description": "optional text" }
  ],
  "agents": [
    { "name": "backend", "role": "...", "project_dir": "C:/...", "project": "core" }
  ]
}
```

- **Top-level `"projects"`** — array of `{ name, path?, description? }`. Missing key ⇒ `[]`.
  - `name` must match the existing agent-name rule (`orch.NAME_RE`) and be unique among
    projects.
  - `path` is optional. If non-empty it is stored as given (after `expanduser`); it is a
    convenience default for new agents, **not** required to be an existing dir.
  - `description` is optional free text.
- **Per-agent `"project"`** — a string referencing a project `name`. Missing/empty ⇒
  ungrouped (`""`). When the config is loaded, every agent dict MUST have a `"project"`
  key (default `""`). If an agent references a project that no longer exists, treat it as
  ungrouped at render time (do not crash).

### 14.2 `GET /api/state` (additions to §2)

Extends the shape documented in §2 — everything else there is unchanged:

- A top-level `"projects"` key: the list from `fleet.json`, each `{ name, path, description }`
  with `path` / `description` defaulting to `""`.
- A `"project"` field on every agent object in the `"agents"` array (the agent's project
  name, or `""`).

### 14.3 Mutation endpoints

All POST bodies are JSON; all responses are JSON via the existing `self._json(...)` helper.
Validation errors return **HTTP 400** `{"error": "..."}`.

| Call | Request body | Success response |
|---|---|---|
| `POST /api/projects` | `{ "name": str, "path"?: str, "description"?: str }` | `{ "ok": true, "name": <name> }` |
| `POST /api/projects/delete` | `{ "name": str }` | `{ "ok": true }` (unknown name ⇒ `{ "ok": false }`) |
| `POST /api/agents` | `{ "name": str, "role"?: str, "project_dir": str, "project"?: str }` | `{ "ok": true, "name": <name> }` |
| `POST /api/agent/group` | `{ "name": str, "project": str }` | `{ "ok": true }` |

**`POST /api/projects` — create a project**
- 400 if `name` missing/invalid (`orch.NAME_RE`) or already a project.
- On success: `{ "ok": true, "name": <name> }`.

**`POST /api/projects/delete` — delete a project**
- Removes the project **and** sets `project` to `""` on any agent that belonged to it
  (ungroup them; do **not** delete those agents).
- On success: `{ "ok": true }`. Unknown name ⇒ `{ "ok": false }` (not an error / not a 400).

**`POST /api/agents` — create a new agent (add to `fleet.json`)**
- 400 if `name` missing/invalid (`orch.NAME_RE`) or duplicates an existing agent.
- 400 if `project_dir` is missing or not an existing directory
  (`Path(project_dir).expanduser().is_dir()`), with message `"not a directory: <path>"`.
- 400 if `project` is non-empty but not an existing project (`"unknown project"`).
- Store `project_dir` resolved exactly like the existing config loader:
  `str(Path(project_dir).expanduser().resolve())`.
- On success: `{ "ok": true, "name": <name> }`. The running dispatcher hot-reloads
  `fleet.json` every tick and spawns the new agent's terminal automatically — callers do
  **not** spawn it themselves.

**`POST /api/agent/group` — move an agent into / out of a project**
- Body `{ "name": str, "project": str }`; `project: ""` ⇒ ungroup.
- 400 if `name` is not an existing agent (`"unknown agent"`).
- 400 if `project` is non-empty but not an existing project (`"unknown project"`).
- On success: `{ "ok": true }`. No restart needed — grouping does not change `project_dir`.

### 14.4 Persistence (atomic writes)

Every mutation of `fleet.json` MUST be atomic and UTF-8, exactly like the existing
`_set_agent_path()` in `fleet.py`: write to a `.tmp` sibling, then `tmp.replace(CONFIG)`,
with `encoding="utf-8"`, `ensure_ascii=False`, `indent=2`. After writing, refresh the
module-level `AGENTS` (call `load_config()`) so the dashboard reflects the change on the
next poll. The dispatcher already hot-reloads the file each tick.

### 14.5 Group actions (v2 — feature t-0005)

Two endpoints that make a group *actionable* — they act on a whole project at once. Same
conventions as §14.3 (JSON in/out via `self._json(...)`; validation errors are
**HTTP 400** `{"error": "..."}`). The v1 schema, `/api/state` shape, and v1 endpoints
above are unchanged.

| Call | Request body | Success response |
|---|---|---|
| `POST /api/tasks/group` | `{ "project": str, "description": str }` | `{ "ok": true, "ids": [<task ids>], "count": <n> }` |
| `POST /api/projects/rename` | `{ "name": str, "new_name": str }` | `{ "ok": true, "name": <new_name> }` |

**`POST /api/tasks/group` — queue one task to every agent in a project**
- Queues `description` as a task for each agent whose `project` matches `project`
  (member order, FIFO), reusing the normal per-agent task queue (the existing
  `add_task(agent, description)` path).
- 400 `{"error": "empty task"}` if `description` is blank.
- 400 `{"error": "unknown project"}` if `project` is not an existing project.
- 400 `{"error": "no agents in project"}` if the project has no member agents.
- On success: `{ "ok": true, "ids": [<task ids>], "count": <n> }` — `ids` are the newly
  created task ids in member order; `count` is their number.

**`POST /api/projects/rename` — rename a project**
- Renames the project and updates every member agent's `project` from `name` to
  `new_name`, persisted with the atomic write of §14.4. No `project_dir` changes, so no
  agent restart is needed.
- 400 `{"error": "invalid name"}` if `new_name` fails `orch.NAME_RE`.
- 400 `{"error": "unknown project"}` if `name` is not an existing project.
- 400 `{"error": "project exists"}` if `new_name` differs from `name` but already names an
  existing project.
- 500 `{"error": "could not update fleet.json"}` if the config write fails (`OSError`).
- On success: `{ "ok": true, "name": <new_name> }`.

> Both act on **real** projects only. The dashboard's "Ungrouped" bucket is not a project
> and exposes none of these group actions.

### 14.6 The Projects tab (v3 — feature t-0007)

The dashboard's tab bar — **Dashboard / Activity / Connections** — gains a fourth tab,
**🗂 Projects** (added after Connections; URL hash `#projects`, restored on load). It is
the single home for all project management; the Dashboard tab keeps its inline grouping
controls, so this tab is purely additive.

What the Projects tab renders:
- A **Create project** control: name + optional path + description → `POST /api/projects`.
- One **project card** per `projects` entry, showing its `name`, `path`, `description`,
  and its **member agents** (agents whose `project === name`) with live status and a
  member count. Per card:
  - **Rename** → `POST /api/projects/rename` `{name, new_name}` (§14.5).
  - **Edit** path / description → `POST /api/projects/update` (below).
  - **Delete** → `POST /api/projects/delete` `{name}` — ungroups members, does not delete
    them (§14.3).
  - **Queue task to group** → `POST /api/tasks/group` `{project, description}` (§14.5).
  - **Add an existing agent** → `POST /api/agent/group` `{name, project}` (§14.3).
  - **Create a new agent in this project** → `POST /api/agents`
    `{name, role, project_dir, project}`, defaulting `project_dir` to the project's `path`
    (§14.3).
  - Per member: **remove from project** (`POST /api/agent/group` `{name, project: ""}`)
    or move it to another project.
- An **Ungrouped agents** block (agents with `project === ""`), each with an
  "assign to project" select → `POST /api/agent/group`.
- A friendly **empty state** when there are no projects yet.

**`POST /api/projects/update` — edit a project's path / description**

| Call | Request body | Success response |
|---|---|---|
| `POST /api/projects/update` | `{ "name": str, "path"?: str, "description"?: str }` | `{ "ok": true, "name": <name> }` |

Wired like the other project handlers (`ValueError` → 400 `{"error": str(e)}`,
`OSError` → 500), and persisted with the atomic write of §14.4.

- `path`: if non-empty, stored as `str(Path(path).expanduser())`; **empty clears it (`""`)**.
- `description`: stored as given; **empty clears it (`""`)**.
- 400 `{"error": "unknown project"}` if `name` is not an existing project.
- 500 `{"error": "could not update fleet.json"}` if the config write fails (`OSError`).
- On success: `{ "ok": true, "name": <name> }`.

### 14.7 Project managers (feature t-0009)

Builds on the Projects feature (§14.6): **every project always has exactly one
project-manager (PM) agent** that coordinates the project's worker agents. A PM is
an ordinary `fleet.json` agent entry plus one extra field; it is created, renamed,
and removed automatically alongside its project. This layer is additive — the v1/v2/v3
schema, `/api/state` shape, and endpoints above are unchanged.

#### 14.7.1 The `manager_of` agent field

A PM agent is a normal agent with **one extra field**, `manager_of`:

```json
{
  "name": "<project>-pm",
  "role": "<manager role text>",
  "project_dir": "<the orchestrator directory>",
  "manager_of": "<project name>"
}
```

- **`manager_of`** — the project this agent manages. Non-empty **only** on PM
  agents; a normal worker agent has `manager_of == ""`.
- A PM is **not** a worker member of the project it manages, so its worker
  `project` field stays `""`. This is deliberate: it keeps PMs out of group-task
  fan-out (`add_group_tasks` filters on `project ==`, never `manager_of`).
- **Name convention:** `_pm_name(project) == f"{project}-pm"`. Project names pass
  `orch.NAME_RE`, so `<project>-pm` is always a valid agent name.
- **`project_dir`:** the orchestrator directory itself (`str(fleet.HERE)`), because
  the PM coordinates by running the `./fleet` / `./orch` CLIs, which live there.

Like `project`, `manager_of` is carried through `load_config()` (default `""`) and
emitted on every agent in `build_state()`:

- **`fleet.json` schema:** each agent object MAY carry a `"manager_of"` string;
  missing/empty ⇒ `""` (an ordinary worker).
- **`GET /api/state` (additions to §2 / §14.2):** every agent object in the
  `"agents"` array gains a `"manager_of"` field (the managed project name, or `""`).

#### 14.7.2 Backend (`fleet.py`)

Two helpers and one public function:

- `_pm_name(project) -> str` → `f"{project}-pm"`.
- `_pm_role(project) -> str` → the manager role string, with `<P>` replaced by the
  project name. (The PM is instructed to coordinate only its own project's members,
  to assign work via `./fleet add`, and to never run `git`, `./fleet up`/`down`,
  `./orch stop --all`, or manage agents outside its project.)
- **`ensure_project_managers() -> list[str]`** — idempotent backfill. For every
  project with no PM (no agent whose `manager_of == project`), append one
  (`name=_pm_name(p)`, `role=_pm_role(p)`, `project_dir=str(HERE)`, `manager_of=p`).
  Persists via `_write_config(data)` **only if** something was added; returns the
  list of PM names created. A second call adds nothing and returns `[]`. If an agent
  with the PM name already exists, it is left untouched (no crash on name clash).

PM lifecycle is wired into the existing project mutators (§14.3 / §14.5), each still
a single atomic `_write_config` (§14.4):

| Mutator | PM behavior (in addition to the project change) |
|---|---|
| `create_project(name, …)` | Also append the project's PM agent, unless an agent named `_pm_name(name)` already exists. |
| `delete_project(name)` | Also remove the PM agent (the one with `manager_of == name`). Member ungrouping is unchanged. |
| `rename_project(name, new_name)` | Also rename the PM (`manager_of == name`) to `_pm_name(new_name)`, set its `manager_of = new_name`, and regenerate its `role` via `_pm_role(new_name)`. A no-op rename (`new_name == name`) stays a no-op. |

These hook into the same `/api/projects`, `/api/projects/delete`, and
`/api/projects/rename` endpoints — no new mutation endpoint is added for PMs; they
are managed as a side effect of the project they belong to.

#### 14.7.3 CLI / startup

- **`cmd_up`:** calls `ensure_project_managers()` immediately after
  `AGENTS = load_config()` (before spawning), so every project's PM exists and gets
  a terminal on start.
- **`./fleet sync-pms`** (new subcommand): calls `ensure_project_managers()` and
  prints the PM names it created (or "nothing to do"). This backfills PMs into a
  **running** fleet without a restart — the live dispatcher hot-reloads `fleet.json`
  and spawns the new PM terminals on its next tick.

#### 14.7.4 Frontend (`dashboard.html`)

Read the PM from `/api/state`: a project's PM is the agent whose
`manager_of === project.name`.

- On each **project card** (Projects tab, §14.6): show a distinct **"Project
  manager"** row/badge for that project's PM — its name + live `activity` status —
  visually separated from the worker member list. The row offers:
  - **Queue task to manager** → `POST /api/tasks` `{ agent: <pmName>, description }`
    (reuse the existing task-queue plumbing + toast).
  - **Send / steer the PM** → `POST /api/agent/send` `{ name: <pmName>, message }`,
    matching how members are steered today.
- The **worker member list** stays "agents with `project === name`". The PM's
  `project` is `""`, so it never appears there — surface it only in the dedicated
  manager row.
- Wherever agents are listed with a role/status (e.g. the Dashboard tab), give any
  agent with a non-empty `manager_of` a small **"PM"** / manager badge so it is
  recognizable. Keep it additive — do not break v1/v2/v3 behavior.
- A project with no PM yet (shouldn't normally happen once the backend lands, but be
  defensive) simply shows no manager row.
