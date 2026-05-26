# Fleet Dashboard — UX Refresh, Easier Logs, Document Upload

**Date:** 2026-05-26
**Status:** Approved (design)
**Scope:** `dashboard.html` (frontend) + `fleet.py` `Handler` (backend)

## Problem

The fleet dashboard works but: it looks dated, it's hard to tell at a glance which
agent needs attention or what's stuck, logs take more clicks than they should, and
there is no way to give agents reference documents from the UI.

## Goals

1. **Visual refresh** — modern, refined look on the existing dark theme; no layout upheaval.
2. **Attention first** — agents needing input and failed tasks are impossible to miss.
3. **Logs easier to reach** — one click to open; modal auto-scrolls to the newest line.
   Keep the existing snapshot behavior (no live streaming).
4. **Document upload** — upload files to a **shared** folder (all agents) and to
   **per-agent** folders; see, delete, and reference uploaded files from the dashboard.

## Non-Goals (YAGNI)

- Live/streaming log refresh while the modal is open.
- Auto-injecting an uploaded document into a task's text.
- In-browser file preview/rendering of uploaded documents.
- Any frontend framework, bundler, or new runtime dependency.

## Architecture

Approach: **evolve the single-file dashboard in place.** `dashboard.html` stays one
vanilla-JS file (restyled, plus a Docs panel). `fleet.py`'s `Handler` gains a few
endpoints backed by small, pure helper functions. No new dependencies.

### Document storage

Central, owned by the orchestrator — does **not** write into agents' project repos:

```
<orchestrator>/fleet_docs/_shared/        # shared, all agents
<orchestrator>/fleet_docs/<agent_name>/   # per-agent
```

- Root path: `fleet_docs/` resolved relative to `fleet.py` (alongside `dashboard.html`).
  Define `DOCS_ROOT = HERE / "fleet_docs"`; `_shared` is a reserved subfolder name.
- Agents reach files via **absolute path** (they already run with full filesystem
  access). The dashboard shows each file's absolute path and an **"insert into task"**
  button that drops the path into the New-task textarea.
- Folders are created on demand on first upload.

### Backend helpers (pure, unit-testable without HTTP)

In `fleet.py`:

- `_docs_dir(scope, name=None) -> Path` — resolve the target folder for a scope.
  `scope="shared"` → `DOCS_ROOT/_shared`; `scope="agent"` → `DOCS_ROOT/<name>` where
  `name` must be a current agent in `AGENTS`. Creates the folder if missing.
- `_safe_filename(filename) -> str` — return `os.path.basename(filename)`; reject
  empty, `.`/`..`, or any value that still contains a path separator after basename.
- `list_docs(scope, name=None) -> list[dict]` — `[{name, size, modified, path}]`,
  sorted by name; `path` is the absolute path as a string.
- `save_doc(scope, name, filename, data: bytes) -> dict` — validate filename and size,
  write bytes, return the file's metadata dict. Enforce `MAX_DOC_BYTES` (~25 MB).
- `delete_doc(scope, name, filename) -> bool` — validate filename, unlink if present.

### Backend endpoints (`Handler`)

- `GET  /api/docs?scope=shared`            → `{files: [...]}`
- `GET  /api/docs?scope=agent&name=NAME`   → `{files: [...]}`
- `POST /api/docs/upload`  body `{scope, name?, filename, content_base64}`
  → `{file: {...}}` on success; `{error}` + 400 on validation failure
- `POST /api/docs/delete`  body `{scope, name?, filename}` → `{ok: bool}`

Validation rules: `scope` ∈ {`shared`,`agent`}; for `agent`, `name` must be in
`{a["name"] for a in AGENTS}`; `filename` passes `_safe_filename`; decoded
`content_base64` size ≤ `MAX_DOC_BYTES`.

### Data flow — upload

1. Browser reads the chosen file with `FileReader.readAsDataURL`, strips the
   `data:...;base64,` prefix to get base64 text.
2. `POST /api/docs/upload` with `{scope, name?, filename, content_base64}`.
3. `Handler` base64-decodes, runs `save_doc` (validation + write), returns metadata.
4. UI refreshes the doc list for the current scope.

## Frontend (`dashboard.html`)

- **Visual refresh:** cleaner spacing, refined card borders/shadows, modern accent and
  typography; same dark palette and the existing two-column layout.
- **Attention strip:** a top band listing agents flagged `attention` and any `failed`
  tasks, so they surface above the fold. Hidden when nothing needs attention.
- **Logs:** keep the existing `taskLog`/`liveLog` modal and snapshot behavior; on open,
  scroll the `<pre>` to the bottom (newest line). Make the per-row/agent "log" buttons
  visually prominent.
- **Docs panel:** a new card with
  - a scope `<select>` (`Shared` + one entry per agent),
  - a file list for the selected scope (name, size, modified) with a **delete** button
    and an **insert into task** button per file,
  - a file `<input type=file>` + **Upload** button that uploads to the selected scope.

## Testing

- **TDD the storage helpers** (`_safe_filename`, `_docs_dir`, `save_doc`, `list_docs`,
  `delete_doc`): valid/invalid filenames, path-traversal rejection, size-cap rejection,
  unknown-agent rejection, round-trip save→list→delete. Pure functions, no HTTP needed.
- **Manual smoke** for the HTML: upload to shared and to an agent, see it listed,
  insert-into-task, delete, open a log and confirm auto-scroll.

## Operational note

`dashboard.html` is re-read per request, so HTML/CSS/JS changes appear on refresh.
The new `fleet.py` endpoints require restarting the running servers to take effect:
the `./fleet up` instance on 8787 and the secondary frontend on 8788.
