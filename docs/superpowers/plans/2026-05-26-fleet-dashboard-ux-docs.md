# Fleet Dashboard — UX Refresh, Easier Logs, Document Upload — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refresh the fleet dashboard's look, surface agents that need attention, make logs one click away with auto-scroll, and add per-agent + shared document upload.

**Architecture:** Evolve the existing single-file `dashboard.html` (vanilla JS, no build step) and add a handful of endpoints to `fleet.py`'s stdlib `Handler`. Documents are stored centrally under `<repo>/fleet_docs/` (a `_shared/` folder plus one folder per agent), never inside agents' project repos. Agents reach files by absolute path; the dashboard exposes an "insert into task" button to drop a path into the New-task box.

**Tech Stack:** Python 3.14 stdlib (`http.server`, `base64`, `pathlib`), vanilla HTML/CSS/JS, pytest.

**Spec:** `docs/superpowers/specs/2026-05-26-fleet-dashboard-ux-docs-design.md`

## Conventions for this plan

- **Run all commands from the repo root:** `C:\Projects\PycharmProjects\personal\ClaudeOrchestrator`.
- **Test interpreter:** pytest lives in the global `C:\Python314\python.exe`, invoked as `py -3.14`. The `.venv` python does **not** have pytest. Always run tests with `py -3.14 -m pytest ...` from the repo root (this puts the repo root on `sys.path` so `import fleet` / `import orch` resolve).
- **Branch:** work is on `feature/dashboard-ux-docs` (already created and holding the spec commit).
- `dashboard.html` is re-read by the server on every request, so HTML/CSS/JS edits show on browser refresh. `fleet.py` changes require restarting any running server to take effect (verification steps below start their own throwaway server, so they don't depend on a server you already have running).

## File Structure

| File | Responsibility | Action |
|---|---|---|
| `fleet.py` | Add document-storage helpers + 3 HTTP routes | Modify |
| `tests/test_fleet_docs.py` | Unit tests for the pure storage helpers | Create |
| `dashboard.html` | Visual refresh, attention strip, log auto-scroll, Documents panel | Modify |
| `.gitignore` | Ignore uploaded documents | Modify |

---

### Task 1: Document storage helpers (pure functions, TDD)

These are pure functions with no HTTP/threading, so they're fully unit-testable. Build them first.

**Files:**
- Modify: `fleet.py` (imports near line 23; constants near line 49; new "documents" section after `load_config`, before the `# agent terminals` section at line 77)
- Create: `tests/test_fleet_docs.py`
- Modify: `.gitignore`

- [ ] **Step 1: Ignore uploaded docs in git**

Add this block to the end of `.gitignore`:

```
# Fleet uploaded documents (user data, not code)
fleet_docs/
```

- [ ] **Step 2: Write the failing test**

Create `tests/test_fleet_docs.py` with the full contents:

```python
import pytest

import fleet


@pytest.fixture
def docs(tmp_path, monkeypatch):
    """Point DOCS_ROOT at a temp dir and define one known agent."""
    monkeypatch.setattr(fleet, "DOCS_ROOT", tmp_path / "fleet_docs")
    monkeypatch.setattr(
        fleet, "AGENTS",
        [{"name": "alice", "role": "", "project_dir": str(tmp_path)}],
    )
    return fleet


def test_safe_filename_strips_path(docs):
    assert docs._safe_filename("a/b/c.txt") == "c.txt"
    assert docs._safe_filename("..\\..\\evil.md") == "evil.md"
    assert docs._safe_filename("plain.pdf") == "plain.pdf"


@pytest.mark.parametrize("bad", ["", ".", "..", "   ", "a/..", "x/"])
def test_safe_filename_rejects_traversal(docs, bad):
    with pytest.raises(ValueError):
        docs._safe_filename(bad)


def test_save_and_list_shared(docs):
    meta = docs.save_doc("shared", None, "notes.md", b"hello")
    assert meta["name"] == "notes.md"
    assert meta["size"] == 5
    files = docs.list_docs("shared")
    assert [f["name"] for f in files] == ["notes.md"]
    assert files[0]["path"].endswith("notes.md")


def test_save_and_list_agent(docs):
    docs.save_doc("agent", "alice", "spec.txt", b"abc")
    files = docs.list_docs("agent", "alice")
    assert [f["name"] for f in files] == ["spec.txt"]


def test_unknown_agent_rejected(docs):
    with pytest.raises(ValueError):
        docs.save_doc("agent", "bob", "x.txt", b"x")


def test_bad_scope_rejected(docs):
    with pytest.raises(ValueError):
        docs.list_docs("nope")


def test_too_large_rejected(docs, monkeypatch):
    monkeypatch.setattr(fleet, "MAX_DOC_BYTES", 4)
    with pytest.raises(ValueError):
        docs.save_doc("shared", None, "big.bin", b"12345")


def test_delete_roundtrip(docs):
    docs.save_doc("shared", None, "gone.txt", b"x")
    assert docs.delete_doc("shared", None, "gone.txt") is True
    assert docs.delete_doc("shared", None, "gone.txt") is False
    assert docs.list_docs("shared") == []
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `py -3.14 -m pytest tests/test_fleet_docs.py -v`
Expected: FAIL — `AttributeError: module 'fleet' has no attribute 'DOCS_ROOT'` (and `_safe_filename`, etc.).

- [ ] **Step 4: Add the `base64` import**

In `fleet.py`, change the import line:

```python
import argparse
```

to:

```python
import argparse
import base64
```

- [ ] **Step 5: Add document constants**

In `fleet.py`, immediately after the line `AGENTS: list[dict] = []     # active fleet, populated by `up`` (line 49), add:

```python

DOCS_ROOT = HERE / "fleet_docs"          # uploaded documents live here
SHARED = "_shared"                        # reserved subfolder for shared docs
MAX_DOC_BYTES = 25 * 1024 * 1024          # 25 MB per-file upload cap
```

- [ ] **Step 6: Add the storage helpers**

In `fleet.py`, after `load_config` ends (line 74, the `return agents` and its blank line) and before the `# agent terminals` section banner (line 77), insert:

```python
# --------------------------------------------------------------------------- #
# documents
# --------------------------------------------------------------------------- #
def _safe_filename(filename: str) -> str:
    """Reduce to a bare filename; reject empty / relative / traversal names."""
    base = (filename or "").replace("\\", "/").split("/")[-1].strip()
    if base in ("", ".", ".."):
        raise ValueError("invalid filename")
    return base


def _docs_dir(scope: str, name: str | None = None) -> Path:
    """Resolve (and create) the folder for a scope. Validates scope/agent."""
    if scope == "shared":
        d = DOCS_ROOT / SHARED
    elif scope == "agent":
        if name not in {a["name"] for a in AGENTS}:
            raise ValueError("unknown agent")
        d = DOCS_ROOT / name
    else:
        raise ValueError("invalid scope")
    d.mkdir(parents=True, exist_ok=True)
    return d


def _doc_meta(p: Path) -> dict:
    st = p.stat()
    return {
        "name": p.name,
        "size": st.st_size,
        "modified": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(),
        "path": str(p),
    }


def list_docs(scope: str, name: str | None = None) -> list[dict]:
    d = _docs_dir(scope, name)
    return [_doc_meta(p) for p in sorted(d.iterdir()) if p.is_file()]


def save_doc(scope: str, name: str | None, filename: str, data: bytes) -> dict:
    if len(data) > MAX_DOC_BYTES:
        raise ValueError("file too large")
    fn = _safe_filename(filename)
    p = _docs_dir(scope, name) / fn
    p.write_bytes(data)
    return _doc_meta(p)


def delete_doc(scope: str, name: str | None, filename: str) -> bool:
    fn = _safe_filename(filename)
    p = _docs_dir(scope, name) / fn
    if p.is_file():
        p.unlink()
        return True
    return False
```

- [ ] **Step 7: Run the test to verify it passes**

Run: `py -3.14 -m pytest tests/test_fleet_docs.py -v`
Expected: PASS — all 9 test functions (the parametrized one expands to 6 cases) green.

- [ ] **Step 8: Commit**

```bash
git add fleet.py tests/test_fleet_docs.py .gitignore
git commit -m "feat: document storage helpers for fleet docs (shared + per-agent)"
```

---

### Task 2: Backend HTTP endpoints

Wire the helpers into the `Handler`. No automated HTTP test (the existing suite tests helpers, not sockets — follow that pattern); verification is a scripted manual check against a throwaway server.

**Files:**
- Modify: `fleet.py` — `Handler.do_GET` (after the `/api/task/log` branch, before the final `else`, ~line 392) and `Handler.do_POST` (after the `/api/agent/restart` branch, before the final `else`, ~line 423)

- [ ] **Step 1: Add the GET route**

In `Handler.do_GET`, immediately before the closing `else:` that returns `{"error": "not found"}` (line 393), insert:

```python
        elif u.path == "/api/docs":
            q = parse_qs(u.query)
            scope = (q.get("scope") or [""])[0]
            name = (q.get("name") or [None])[0]
            try:
                self._json({"files": list_docs(scope, name)})
            except ValueError as e:
                self._json({"error": str(e)}, 400)
```

- [ ] **Step 2: Add the POST routes**

In `Handler.do_POST`, immediately before the closing `else:` that returns `{"error": "not found"}` (line 424), insert:

```python
        elif u.path == "/api/docs/upload":
            scope = (body.get("scope") or "").strip()
            name = body.get("name") or None
            filename = (body.get("filename") or "").strip()
            try:
                data = base64.b64decode(body.get("content_base64") or "")
            except Exception:
                return self._json({"error": "bad base64 content"}, 400)
            try:
                self._json({"file": save_doc(scope, name, filename, data)})
            except ValueError as e:
                self._json({"error": str(e)}, 400)
        elif u.path == "/api/docs/delete":
            scope = (body.get("scope") or "").strip()
            name = body.get("name") or None
            filename = (body.get("filename") or "").strip()
            try:
                self._json({"ok": delete_doc(scope, name, filename)})
            except ValueError as e:
                self._json({"error": str(e)}, 400)
```

- [ ] **Step 3: Verify the endpoints against a throwaway server**

Run this in PowerShell from the repo root (starts its own server on 127.0.0.1:8799, exercises upload + list, then stops it):

```powershell
$srv = Start-Process py -PassThru -ArgumentList '-3.14','-c',"import fleet; from http.server import ThreadingHTTPServer; fleet.AGENTS=fleet.load_config(); ThreadingHTTPServer(('127.0.0.1',8799), fleet.Handler).serve_forever()"
Start-Sleep -Seconds 1
$b64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes('hi there'))
$body = @{ scope='shared'; filename='hello.txt'; content_base64=$b64 } | ConvertTo-Json
Invoke-RestMethod -Uri http://127.0.0.1:8799/api/docs/upload -Method Post -ContentType 'application/json' -Body $body
Invoke-RestMethod -Uri 'http://127.0.0.1:8799/api/docs?scope=shared'
Stop-Process -Id $srv.Id
```

Expected: the upload call returns a `file` object with `name=hello.txt` and `size=8`; the list call returns a `files` array containing `hello.txt`. (Then optionally `Remove-Item -Recurse -Force fleet_docs` to clear the test file — it's gitignored either way.)

- [ ] **Step 4: Commit**

```bash
git add fleet.py
git commit -m "feat: /api/docs upload, list, and delete endpoints"
```

---

### Task 3: Frontend — visual refresh, attention strip, log auto-scroll

Pure `dashboard.html` edits. Verification is a browser smoke test (reload the page — no server restart needed for HTML).

**Files:**
- Modify: `dashboard.html` (CSS in `<style>`, the `<header>`/`<main>` markup, and the `<script>`)

- [ ] **Step 1: Refresh core CSS variables and surfaces**

In `dashboard.html`, replace the `.card` rule (line 22):

```css
  .card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px;margin-bottom:14px}
```

with a slightly more refined surface:

```css
  .card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:16px;margin-bottom:16px;box-shadow:0 1px 0 rgba(255,255,255,.02),0 8px 24px rgba(0,0,0,.25)}
```

And replace the `header` rule (line 16):

```css
  header{display:flex;align-items:center;gap:12px;padding:14px 20px;border-bottom:1px solid var(--line);background:var(--panel)}
```

with:

```css
  header{display:flex;align-items:center;gap:12px;padding:14px 20px;border-bottom:1px solid var(--line);background:linear-gradient(180deg,#1b1f29,#171a21);position:sticky;top:0;z-index:5}
```

And add a hover affordance for buttons — directly after the `button{...}` rule (line 44), add:

```css
  button:hover{border-color:var(--accent)}
  button.primary:hover{filter:brightness(1.08)}
```

- [ ] **Step 2: Add the attention-strip CSS**

In `dashboard.html`, immediately before the `/* modal */` comment (line 53), add:

```css
  .attention{margin:12px 16px 0;padding:10px 14px;border:1px solid var(--attn);background:#241414;border-radius:12px;color:var(--attn);font-size:13px;display:flex;flex-wrap:wrap;gap:10px 16px;align-items:center}
  .attention strong{color:var(--txt)}
  .attention .chip{background:#3a1414;border:1px solid var(--attn);border-radius:999px;padding:2px 10px}
  .attention[hidden]{display:none}
```

- [ ] **Step 3: Add the attention-strip markup**

In `dashboard.html`, immediately after the closing `</header>` (line 67), add:

```html
<div id="attention" class="attention" hidden></div>
```

- [ ] **Step 4: Add the attention render function**

In `dashboard.html`, in the `<script>`, immediately before `async function refresh(){` (line 175), add:

```javascript
function renderAttention(agents, tasks){
  const need = agents.filter(a=>a.attention).map(a=>a.name);
  const failed = tasks.filter(t=>t.status==='failed');
  const el = $("#attention");
  if(!need.length && !failed.length){ el.hidden = true; return; }
  const parts = [];
  if(need.length) parts.push(`<span><strong>Needs you:</strong> ${need.map(esc).join(", ")}</span>`);
  if(failed.length) parts.push(`<span class="chip">⚠ ${failed.length} failed task${failed.length>1?'s':''}</span>`);
  el.innerHTML = parts.join("");
  el.hidden = false;
}
```

- [ ] **Step 5: Call it from `refresh()`**

In `dashboard.html`, inside `refresh()`, immediately after the line `renderTasks(s.tasks);` (line 179), add:

```javascript
    renderAttention(s.agents, s.tasks);
```

- [ ] **Step 6: Auto-scroll the log modal to the newest line**

In `dashboard.html`, replace the `openModal` function (line 220):

```javascript
function openModal(title, text){ $("#modalTitle").textContent=title; $("#modalBody").textContent=text||"(empty)"; $("#modal").classList.add("open"); }
```

with:

```javascript
function openModal(title, text){ $("#modalTitle").textContent=title; const b=$("#modalBody"); b.textContent=text||"(empty)"; $("#modal").classList.add("open"); b.scrollTop = b.scrollHeight; }
```

- [ ] **Step 7: Browser smoke test**

Reload the dashboard (any running frontend, or start one with `py -3.14 fleet.py up`). Confirm: cards look cleaner with subtle shadow; the header stays pinned on scroll; opening a task or live log scrolls to the bottom. To see the attention strip, it appears whenever an agent is flagged `needs you` or any task is `failed` (no action needed if none currently are).

- [ ] **Step 8: Commit**

```bash
git add dashboard.html
git commit -m "feat: dashboard visual refresh, attention strip, log auto-scroll"
```

---

### Task 4: Frontend — Documents panel

Adds the upload/list/delete/insert UI, backed by the Task 2 endpoints. Index-based handlers (not string interpolation) so filenames with spaces/quotes are safe.

**Files:**
- Modify: `dashboard.html` (CSS, the left `<section>` markup, the `<script>`)

- [ ] **Step 1: Add Documents-panel CSS**

In `dashboard.html`, immediately before the `/* modal */` comment (now just after the `.attention[hidden]` rule added in Task 3), add:

```css
  .doclist{display:flex;flex-direction:column;gap:6px;margin:8px 0}
  .docrow{display:flex;align-items:center;justify-content:space-between;gap:8px;padding:6px 8px;background:var(--panel2);border:1px solid var(--line);border-radius:8px}
  .docmeta{display:flex;gap:8px;align-items:baseline;min-width:0}
  .docname{font-family:ui-monospace,monospace;font-size:12px;word-break:break-all}
  .upload{display:flex;gap:6px;align-items:center;margin-top:8px}
  .upload input[type=file]{flex:1;padding:6px;font-size:12px}
```

- [ ] **Step 2: Add the Documents-panel markup**

In `dashboard.html`, inside the left `<section>`, immediately after the closing `</div>` of the Agents card (line 82, the `</div>` that closes `<div class="card"><h2>Agents</h2>...`) and before `</section>` (line 83), add:

```html
    <div class="card">
      <h2>Documents</h2>
      <label for="docScope">Scope</label>
      <select id="docScope"></select>
      <div id="docList" class="doclist"></div>
      <div class="upload">
        <input type="file" id="docFile">
        <button class="mini" id="docUpload">Upload</button>
      </div>
    </div>
```

- [ ] **Step 3: Add the Documents JS (state + render + actions)**

In `dashboard.html`, in the `<script>`, immediately after the `renderAgents` function ends (line 145, the closing `}` after the dropdown-sync block) add:

```javascript
let docCache = [];

function docScopeValue(){
  const v = $("#docScope").value;
  return v === "__shared__" ? {scope:"shared"} : {scope:"agent", name:v};
}

function renderDocScopes(agents){
  const sel = $("#docScope"), prev = sel.value;
  sel.innerHTML = `<option value="__shared__">Shared (all agents)</option>` +
    agents.map(a=>`<option value="${esc(a.name)}">${esc(a.name)}</option>`).join("");
  if([...sel.options].some(o=>o.value===prev)) sel.value = prev;
}

function fmtSize(n){
  if(n < 1024) return n+" B";
  if(n < 1048576) return (n/1024).toFixed(1)+" KB";
  return (n/1048576).toFixed(1)+" MB";
}

async function refreshDocs(){
  const s = docScopeValue();
  const q = s.scope==="shared" ? "scope=shared"
    : "scope=agent&name="+encodeURIComponent(s.name);
  const r = await api("/api/docs?"+q);
  docCache = r.files || [];
  $("#docList").innerHTML = docCache.length ? docCache.map((f,i)=>`
    <div class="docrow">
      <div class="docmeta"><span class="docname">${esc(f.name)}</span>
        <span class="muted">${fmtSize(f.size)}</span></div>
      <div class="row-actions">
        <button class="mini" onclick="insertDocPath(${i})">insert</button>
        <button class="mini" onclick="deleteDoc(${i})">delete</button>
      </div>
    </div>`).join("") : `<div class="muted">No documents.</div>`;
}

function insertDocPath(i){
  const ta = $("#desc"), path = docCache[i].path;
  const sep = ta.value && !ta.value.endsWith("\n") ? "\n" : "";
  ta.value += sep + path; ta.focus();
}

async function uploadDoc(){
  const inp = $("#docFile"), file = inp.files[0];
  if(!file) return;
  const b64 = await new Promise((res,rej)=>{
    const fr = new FileReader();
    fr.onload = () => res(String(fr.result).split(",")[1]);
    fr.onerror = rej;
    fr.readAsDataURL(file);
  });
  const r = await api("/api/docs/upload",{method:"POST",headers:{'Content-Type':'application/json'},
    body: JSON.stringify({...docScopeValue(), filename:file.name, content_base64:b64})});
  if(r.error){ alert(r.error); return; }
  inp.value = ""; refreshDocs();
}

async function deleteDoc(i){
  const f = docCache[i];
  if(!confirm("Delete "+f.name+"?")) return;
  await api("/api/docs/delete",{method:"POST",headers:{'Content-Type':'application/json'},
    body: JSON.stringify({...docScopeValue(), filename:f.name})});
  refreshDocs();
}
```

- [ ] **Step 4: Keep the scope selector in sync and wire events**

In `dashboard.html`, inside `renderAgents`, immediately after the line `if ([...sel.options].some(o=>o.value===prev)) sel.value = prev;` (line 144), add:

```javascript
  renderDocScopes(agents);
```

Then, at the very bottom of the `<script>`, replace the final line (line 229):

```javascript
refresh(); setInterval(refresh, 2500); setInterval(tick, 1000);
```

with:

```javascript
$("#docUpload").onclick = uploadDoc;
$("#docScope").addEventListener("change", refreshDocs);
refresh(); refreshDocs(); setInterval(refresh, 2500); setInterval(tick, 1000);
```

- [ ] **Step 5: Browser smoke test**

Start/refresh a frontend (`py -3.14 fleet.py up`, or a throwaway server). Then:
1. With scope **Shared (all agents)**, choose a small file and click **Upload** → it appears in the list with a size.
2. Switch the scope `<select>` to an agent name → the list changes to that agent's docs (empty at first); upload one there.
3. Click **insert** on a file → its absolute path is appended to the New-task textarea.
4. Click **delete** on a file → confirm prompt, then it disappears from the list.

- [ ] **Step 6: Commit**

```bash
git add dashboard.html
git commit -m "feat: dashboard Documents panel (upload, list, delete, insert path)"
```

---

### Task 5: Full integration smoke + restart running servers

**Files:** none (operational verification)

- [ ] **Step 1: Run the whole test suite**

Run: `py -3.14 -m pytest -q`
Expected: PASS — the existing tests plus `tests/test_fleet_docs.py` all green.

- [ ] **Step 2: End-to-end check**

Start a real dashboard: `py -3.14 fleet.py up --host 127.0.0.1`. In the browser:
1. Upload a doc to an agent's scope.
2. Click **insert** to drop its path into the New-task box, add an instruction like `Read the file at the path above and summarize it.`, and **Queue task**.
3. Confirm the task dispatches to that agent (status → running) and the agent can read the file from the inserted absolute path.
4. Open a log and confirm it auto-scrolls to the newest line.

- [ ] **Step 3: Restart any pre-existing servers so they pick up the new `fleet.py`**

Any `fleet.py` server started before this work (e.g. the dispatcher on 8787 and a secondary frontend on 8788) is running the old code and lacks the `/api/docs` endpoints. Stop and restart them: `Ctrl-C` the original `./fleet up` and relaunch it; stop and relaunch any secondary frontend process.

- [ ] **Step 4: Finish the branch**

Use the superpowers:finishing-a-development-branch skill to decide how to integrate `feature/dashboard-ux-docs` (merge to `master` / open a PR / etc.).

---

## Self-Review

**Spec coverage:**
- Visual refresh → Task 3 (Steps 1–2). ✔
- Attention first → Task 3 (Steps 2–5). ✔
- Logs easier to reach + auto-scroll → Task 3 (Step 6); the existing one-click log buttons are kept. ✔
- Document upload (shared + per-agent), see/delete/reference → Task 1 (helpers), Task 2 (endpoints), Task 4 (UI incl. insert-into-task). ✔
- Central storage under `<repo>/fleet_docs/` → Task 1 (Step 5 constant, Step 6 `_docs_dir`), gitignored in Step 1. ✔
- Non-goals (no live streaming, no auto-inject, no preview, no new deps) → respected; stdlib + vanilla JS only. ✔

**Placeholder scan:** No TBD/TODO; every code and command step shows full content. ✔

**Type/name consistency:** `_safe_filename`, `_docs_dir`, `_doc_meta`, `list_docs`, `save_doc`, `delete_doc`, `DOCS_ROOT`, `SHARED`, `MAX_DOC_BYTES` are defined in Task 1 and used identically in Tasks 2/4. Endpoint paths `/api/docs`, `/api/docs/upload`, `/api/docs/delete` and JSON keys (`scope`, `name`, `filename`, `content_base64`, `files`, `file`, `ok`, `error`) match between Task 2 (server) and Task 4 (client). Frontend functions `renderDocScopes`, `refreshDocs`, `docScopeValue`, `insertDocPath`, `uploadDoc`, `deleteDoc`, `renderAttention` are defined and called consistently. ✔
