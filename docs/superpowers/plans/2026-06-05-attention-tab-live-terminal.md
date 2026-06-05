# Attention-tab Live Terminal Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the Attention tab's polled multi-terminal list with a dropdown agent selector + one live terminal that streams the agent's screen over Server-Sent Events (no polling lag).

**Architecture:** The per-agent host (`agent_host.py`) gains a screen pub/sub plus a long-lived `STREAM` control command that pushes the rendered pyte screen the instant it changes. A `stream_screen(name)` backend generator reads those frames; a new `GET /api/agent/stream` SSE endpoint in `fleet.py` forwards them as `data:` events; the dashboard opens an `EventSource` and writes each frame into a single `<pre>`. Keystroke input is unchanged (existing `POST /api/agent/keys`).

**Tech Stack:** Python 3 stdlib (`http.server`, `socketserver`, `queue`, `socket`), pyte (screen rendering), Alpine.js + vanilla `EventSource` (frontend), pytest.

**Reference spec:** `docs/superpowers/specs/2026-06-05-attention-tab-live-terminal-design.md`

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `backend.py` | Backend interface + `HEARTBEAT` sentinel + default poll-based `stream_screen` | Modify |
| `agent_host.py` | Per-agent host: add screen pub/sub, frame writer, `STREAM` command, pump hook | Modify |
| `win_backend.py` | Windows backend: `stream_screen` via host socket + frame reader | Modify |
| `fleet.py` | Dashboard HTTP server: SSE helpers + `/api/agent/stream` endpoint | Modify |
| `dashboard.html` | Attention tab: selector + single live terminal + Alpine wiring | Modify |
| `tests/test_screen_stream.py` | Unit tests for pub/sub, framing, backend generator, SSE helpers | Create |
| `tests/test_host_conpty.py` | Add a `STREAM` round-trip integration test | Modify |

---

## Task 1: Host screen pub/sub + frame writer (`agent_host.py`)

**Files:**
- Modify: `agent_host.py` (imports; `AgentHost.__init__`; new methods; module-level `_write_frame` + `_offer` + `STREAM_HEARTBEAT_SECS`)
- Test: `tests/test_screen_stream.py` (create)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_screen_stream.py`:

```python
import io
import queue

import agent_host


class FakeChild:
    def __init__(self):
        self.written = []
        self.alive = True
    def write(self, text): self.written.append(text)
    def isalive(self): return self.alive


def test_subscribe_seeds_current_screen():
    host = agent_host.AgentHost.for_test(child=FakeChild(), cols=40, rows=6)
    host.screen.feed(b"hello world\r\n")
    q = host.subscribe()
    seeded = q.get_nowait()
    assert "hello world" in seeded
    host.unsubscribe(q)


def test_publish_screen_pushes_only_on_change():
    host = agent_host.AgentHost.for_test(child=FakeChild(), cols=40, rows=6)
    q = host.subscribe()
    q.get_nowait()                       # drain the seed frame
    host.screen.feed(b"line one\r\n")
    host.publish_screen()
    assert "line one" in q.get_nowait()
    # No screen change -> nothing new published.
    host.publish_screen()
    assert q.empty()


def test_unsubscribe_stops_delivery():
    host = agent_host.AgentHost.for_test(child=FakeChild(), cols=40, rows=6)
    q = host.subscribe()
    host.unsubscribe(q)
    host.screen.feed(b"after\r\n")
    host.publish_screen()
    # Drain whatever was seeded; no NEW frame should arrive after unsubscribe.
    try:
        while True:
            q.get_nowait()
    except queue.Empty:
        pass
    assert q.empty()


def test_offer_drops_oldest_when_full():
    q = queue.Queue(maxsize=2)
    agent_host._offer(q, "a")
    agent_host._offer(q, "b")
    agent_host._offer(q, "c")            # full -> drop "a"
    assert list(q.queue) == ["b", "c"]


def test_write_frame_length_prefixes_payload():
    buf = io.BytesIO()
    agent_host._write_frame(buf, b"screen text")
    assert buf.getvalue() == b"11\nscreen text"


def test_write_frame_zero_length_heartbeat():
    buf = io.BytesIO()
    agent_host._write_frame(buf, b"")
    assert buf.getvalue() == b"0\n"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_screen_stream.py -v`
Expected: FAIL — `AttributeError: ... has no attribute 'subscribe'` / `_offer` / `_write_frame`.

- [ ] **Step 3: Add the implementation to `agent_host.py`**

Add `import queue` near the top imports (after `import os`):

```python
import os
import queue
```

Add a module-level constant just below `HEARTBEAT_INTERVAL_SECS` (line ~29):

```python
# How long a STREAM connection waits for a screen change before emitting a
# zero-length heartbeat frame (keeps the connection + tunnel alive, and surfaces
# a dead client as a write error so the subscriber gets cleaned up).
STREAM_HEARTBEAT_SECS = 15.0
```

Add these two module-level helpers (place them just above `class AgentHost`):

```python
def _offer(q: "queue.Queue", item) -> None:
    """Put ``item`` on ``q``; if full, drop the oldest so a slow consumer can
    never block the producer (the pump thread must never stall)."""
    try:
        q.put_nowait(item)
    except queue.Full:
        try:
            q.get_nowait()
        except queue.Empty:
            pass
        try:
            q.put_nowait(item)
        except queue.Full:
            pass


def _write_frame(wfile, payload: bytes) -> None:
    """Write one length-prefixed frame: ``<len>\\n`` then the bytes. A zero-length
    payload (``0\\n``) is a heartbeat."""
    wfile.write(str(len(payload)).encode() + b"\n")
    if payload:
        wfile.write(payload)
```

In `AgentHost.__init__`, add subscriber state at the end of the method (after `self._heartbeat_stop = threading.Event()`):

```python
        # Live-screen subscribers (the dashboard's STREAM connections). Each is a
        # bounded queue fed the rendered screen text whenever it changes.
        self._subs: list[queue.Queue] = []
        self._subs_lock = threading.Lock()
        self._last_pub_text: str | None = None
```

Add three methods to `AgentHost` (place them after `observe_screen_for_status`):

```python
    def subscribe(self) -> "queue.Queue":
        """Register a live-screen subscriber and seed it with the current screen
        so a freshly-connected client paints immediately. Returns the queue."""
        q: queue.Queue = queue.Queue(maxsize=8)
        with self._subs_lock:
            self._subs.append(q)
        _offer(q, self.screen.text())
        return q

    def unsubscribe(self, q: "queue.Queue") -> None:
        with self._subs_lock:
            try:
                self._subs.remove(q)
            except ValueError:
                pass

    def publish_screen(self) -> None:
        """Fan the current rendered screen out to subscribers, but only when it
        changed since the last publish. Called from the pump after each chunk."""
        txt = self.screen.text()
        with self._subs_lock:
            if txt == self._last_pub_text:
                return
            self._last_pub_text = txt
            subs = list(self._subs)
        for q in subs:
            _offer(q, txt)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_screen_stream.py -v`
Expected: PASS (6 passed).

- [ ] **Step 5: Commit**

```bash
git add agent_host.py tests/test_screen_stream.py
git commit -m "feat(host): screen pub/sub + length-prefixed frame writer"
```

---

## Task 2: Host `STREAM` command + pump hook (`agent_host.py`)

**Files:**
- Modify: `agent_host.py` (`_Handler.handle` — add `STREAM`; `_pump` — call `publish_screen`)
- Test: `tests/test_host_conpty.py` (add a streaming round-trip test)

- [ ] **Step 1: Write the failing test**

Add to `tests/test_host_conpty.py` (after `test_control_protocol_roundtrip`):

```python
def _read_frame(rfile):
    line = rfile.readline()
    if not line:
        return None
    n = int(line.strip())
    if n == 0:
        return b""
    buf = bytearray()
    while len(buf) < n:
        chunk = rfile.read(n - len(buf))
        if not chunk:
            break
        buf += chunk
    return bytes(buf)


def test_stream_pushes_initial_and_changed_screen():
    child = FakeChild()
    host = agent_host.AgentHost.for_test(child=child, cols=40, rows=6)
    host.screen.feed(b"first screen\r\n")
    port = host.start_server()
    try:
        s = socket.create_connection(("127.0.0.1", port), timeout=2)
        s.sendall(b"STREAM\n")
        rfile = s.makefile("rb")
        initial = _read_frame(rfile).decode()
        assert "first screen" in initial
        # New output -> publish -> the stream should deliver the new screen.
        host.screen.feed(b"second screen\r\n")
        host.publish_screen()
        nxt = _read_frame(rfile).decode()
        assert "second screen" in nxt
        s.close()
    finally:
        host.stop_server()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/test_host_conpty.py::test_stream_pushes_initial_and_changed_screen -v`
Expected: FAIL — the server replies `ERR unknown` to `STREAM`, so `int(line.strip())` raises `ValueError`.

- [ ] **Step 3: Add the `STREAM` handler and pump hook**

In `agent_host.py`, inside `_Handler.handle`, add a new branch before the final `else` (after the `STOP` branch, line ~73):

```python
        elif cmd == "STREAM":
            # Long-lived push of the rendered screen. Subscribe, send frames as
            # the screen changes, emit a heartbeat on idle, and clean up when the
            # client disconnects (write raises).
            q = host.subscribe()
            try:
                while True:
                    try:
                        txt = q.get(timeout=STREAM_HEARTBEAT_SECS)
                    except queue.Empty:
                        _write_frame(self.wfile, b"")        # heartbeat
                        self.wfile.flush()
                        continue
                    _write_frame(self.wfile, txt.encode("utf-8", "replace"))
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                host.unsubscribe(q)
```

In `_pump`, publish the screen after the status observation. Change the block at the end of the `if data:` branch (line ~351) from:

```python
                host.mark_ready_if_seen()
                host._write_status()
                host.observe_screen_for_status()
```

to:

```python
                host.mark_ready_if_seen()
                host._write_status()
                host.observe_screen_for_status()
                host.publish_screen()
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest tests/test_host_conpty.py::test_stream_pushes_initial_and_changed_screen -v`
Expected: PASS.

Then run the full host suite to confirm no regressions:
Run: `python -m pytest tests/test_host_conpty.py tests/test_screen_stream.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add agent_host.py tests/test_host_conpty.py
git commit -m "feat(host): STREAM control command pushes live screen frames"
```

---

## Task 3: Backend `stream_screen` generator (`backend.py`, `win_backend.py`)

**Files:**
- Modify: `backend.py` (add `HEARTBEAT`, `STREAM_POLL_SECS`, `STREAM_HEARTBEAT_SECS`, default `stream_screen`)
- Modify: `win_backend.py` (add `_read_frame`, `_iter_frames`, `WinBackend.stream_screen`)
- Test: `tests/test_screen_stream.py` (append)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_screen_stream.py`:

```python
import io as _io

import backend as backend_mod
import win_backend


class FakeBackend(backend_mod.Backend):
    """Drives the base-class poll loop from a scripted capture sequence."""
    def __init__(self, screens, alive_calls):
        self._screens = list(screens)
        self._alive = alive_calls
    def worker_exists(self, name):
        self._alive -= 1
        return self._alive >= 0
    def capture(self, name, lines=200):
        return self._screens.pop(0) if self._screens else ""


def test_base_stream_screen_yields_changes_and_heartbeat(monkeypatch):
    # Heartbeat threshold 0 => any unchanged poll emits HEARTBEAT; poll sleep 0.
    monkeypatch.setattr(backend_mod, "STREAM_HEARTBEAT_SECS", 0)
    monkeypatch.setattr(backend_mod, "STREAM_POLL_SECS", 0)
    monkeypatch.setattr(backend_mod.time, "sleep", lambda *_: None)
    fb = FakeBackend(screens=["A", "A", "B"], alive_calls=3)
    out = list(fb.stream_screen("x"))
    assert out[0] == "A"                       # first screen (changed from None)
    assert out[1] is backend_mod.HEARTBEAT     # unchanged poll -> heartbeat
    assert out[2] == "B"                        # changed again


def test_iter_frames_parses_payloads_and_heartbeats():
    framed = b"5\nhello0\n3\nbye"
    out = list(win_backend._iter_frames(_io.BytesIO(framed)))
    assert out[0] == "hello"
    assert out[1] is backend_mod.HEARTBEAT
    assert out[2] == "bye"


def test_iter_frames_stops_at_eof():
    out = list(win_backend._iter_frames(_io.BytesIO(b"")))
    assert out == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_screen_stream.py -k "stream_screen or iter_frames" -v`
Expected: FAIL — `AttributeError: module 'backend' has no attribute 'HEARTBEAT'` / `'win_backend' has no attribute '_iter_frames'`.

- [ ] **Step 3: Implement the backend pieces**

In `backend.py`, add `import time` at the top (after `import sys`), and add the constants + sentinel just below the imports (before `class Backend`):

```python
import os
import sys
import time

# --- live screen streaming -------------------------------------------------
# Sentinel a stream_screen generator yields to mean "no new screen, just keep
# the connection warm". The SSE layer maps it to an ``: ping`` comment.
HEARTBEAT = object()
# Base (poll-based) fallback cadence. Overridable in tests.
STREAM_POLL_SECS = 0.2
STREAM_HEARTBEAT_SECS = 10.0
```

Add a default `stream_screen` method to `class Backend` (place it after `send_keys`):

```python
    def stream_screen(self, name: str):
        """Yield the agent's rendered screen text whenever it changes, and a
        ``HEARTBEAT`` sentinel when it has been idle. Generator ends when the
        worker stops existing. This default polls ``capture``; backends with a
        push channel (Windows ConPTY host) override it."""
        last = None
        last_emit = time.monotonic()
        while self.worker_exists(name):
            txt = self.capture(name)
            now = time.monotonic()
            if txt != last:
                last = txt
                last_emit = now
                yield txt
            elif now - last_emit >= STREAM_HEARTBEAT_SECS:
                last_emit = now
                yield HEARTBEAT
            time.sleep(STREAM_POLL_SECS)
```

In `win_backend.py`, add `from backend import Backend, HEARTBEAT` (the file already imports `Backend` — extend that import to include `HEARTBEAT`; if it imports differently, add `from backend import HEARTBEAT`). Then add the frame reader + generator helpers near `_ask` (module level):

```python
def _read_frame(rfile):
    """Read one length-prefixed frame. Returns the payload bytes, ``b""`` for a
    heartbeat, or ``None`` at EOF / on a malformed length line."""
    line = rfile.readline()
    if not line:
        return None
    try:
        n = int(line.strip())
    except ValueError:
        return None
    if n == 0:
        return b""
    buf = bytearray()
    while len(buf) < n:
        chunk = rfile.read(n - len(buf))
        if not chunk:
            break
        buf += chunk
    return bytes(buf)


def _iter_frames(rfile):
    """Yield screen strings / HEARTBEAT sentinels from a STREAM connection's
    length-prefixed frames until EOF."""
    while True:
        payload = _read_frame(rfile)
        if payload is None:
            return
        if payload == b"":
            yield HEARTBEAT
        else:
            yield payload.decode("utf-8", "replace")
```

Add the `stream_screen` override to `class WinBackend` (after `send_keys`):

```python
    def stream_screen(self, name: str):
        """Open the host's STREAM channel and yield screen frames as they arrive.
        Falls through to nothing (generator ends) if the agent is offline."""
        port = self._port(name)
        if port is None:
            return
        try:
            sock = socket.create_connection(("127.0.0.1", port), timeout=3.0)
        except OSError:
            return
        try:
            sock.sendall(b"STREAM\n")
            # Generous read timeout: the host heartbeats every ~15s, so 40s of
            # total silence means the socket is dead — let recv raise and end.
            sock.settimeout(40.0)
            rfile = sock.makefile("rb")
            yield from _iter_frames(rfile)
        except OSError:
            return
        finally:
            try:
                sock.close()
            except OSError:
                pass
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_screen_stream.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add backend.py win_backend.py tests/test_screen_stream.py
git commit -m "feat(backend): stream_screen generator (host push + poll fallback)"
```

---

## Task 4: SSE endpoint `/api/agent/stream` (`fleet.py`)

**Files:**
- Modify: `fleet.py` (module-level SSE helpers; `do_GET` branch)
- Test: `tests/test_screen_stream.py` (append)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_screen_stream.py`:

```python
import fleet


def test_sse_data_json_encodes_newlines():
    out = fleet._sse_data({"screen": "a\nb"})
    assert out == b'data: {"screen": "a\\nb"}\n\n'


def test_sse_ping_comment():
    assert fleet._sse_ping() == b": ping\n\n"


class _FakeBackend:
    def __init__(self, items): self._items = items
    def stream_screen(self, name):
        for it in self._items:
            yield it


def test_stream_agent_screen_maps_frames_and_heartbeats():
    writes = []
    be = _FakeBackend(["scr1", backend_mod.HEARTBEAT, "scr2"])
    fleet.stream_agent_screen(writes.append, be, "x")
    assert writes[0] == fleet._sse_data({"screen": "scr1"})
    assert writes[1] == fleet._sse_ping()
    assert writes[2] == fleet._sse_data({"screen": "scr2"})


def test_stream_agent_screen_stops_on_broken_pipe():
    calls = {"n": 0}
    def writer(_b):
        calls["n"] += 1
        if calls["n"] == 2:
            raise BrokenPipeError()
    be = _FakeBackend(["a", "b", "c"])
    fleet.stream_agent_screen(writer, be, "x")   # must not raise
    assert calls["n"] == 2                         # stopped at the broken write
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_screen_stream.py -k "sse or stream_agent_screen" -v`
Expected: FAIL — `AttributeError: module 'fleet' has no attribute '_sse_data'`.

- [ ] **Step 3: Implement the helpers + endpoint in `fleet.py`**

Add module-level helpers near the other top-level helpers (anywhere above the request-handler class; `json` is already imported):

```python
def _sse_data(obj) -> bytes:
    """One SSE ``data:`` event carrying a JSON object. JSON-encoding keeps the
    (multi-line) screen text on a single SSE data line."""
    return b"data: " + json.dumps(obj).encode("utf-8") + b"\n\n"


def _sse_ping() -> bytes:
    """An SSE comment line — keeps the connection (and the tunnel) warm."""
    return b": ping\n\n"


def stream_agent_screen(write, backend, name) -> None:
    """Pump ``backend.stream_screen(name)`` to ``write`` as SSE. A HEARTBEAT
    sentinel becomes an ``: ping`` comment; a screen string becomes a data
    event. Returns quietly when the client disconnects (write raises)."""
    from backend import HEARTBEAT
    try:
        for item in backend.stream_screen(name):
            if item is HEARTBEAT:
                write(_sse_ping())
            else:
                write(_sse_data({"screen": item}))
    except (BrokenPipeError, ConnectionResetError, OSError):
        return
```

In `do_GET`, add a branch for the stream path. Place it right after the `/api/logs` branch (line ~1620), before `/api/task/log`:

```python
            elif u.path == "/api/agent/stream":
                q = parse_qs(u.query)
                name = (q.get("agent") or [""])[0]
                if name not in {a["name"] for a in AGENTS}:
                    return self._json({"error": "unknown agent"}, 400)
                # Switch to a streaming response; we own the socket from here.
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
                self.send_header("X-Accel-Buffering", "no")
                self.end_headers()
                if not orch._window_exists(name):
                    try:
                        self.wfile.write(_sse_data({"status": "offline"}))
                        self.wfile.flush()
                    except OSError:
                        pass
                    return
                def _w(b):
                    self.wfile.write(b)
                    self.wfile.flush()
                stream_agent_screen(_w, orch._backend, name)
                return
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_screen_stream.py -v`
Expected: all PASS.

Then the whole suite (catch import/regression issues):
Run: `python -m pytest -q`
Expected: PASS (no new failures).

- [ ] **Step 5: Commit**

```bash
git add fleet.py tests/test_screen_stream.py
git commit -m "feat(dashboard): /api/agent/stream SSE endpoint for live terminal"
```

---

## Task 5: Frontend — selector + single live terminal (`dashboard.html`)

**Files:**
- Modify: `dashboard.html` (CSS block; Attention-tab markup lines 865–1008; Alpine state + methods; `init`, `setTab`, `refresh`, `sendKeys`, `sendMsg`)

No unit tests (no JS harness in this repo); verified manually in Task 6.

- [ ] **Step 1: Add CSS for the selector**

In the `<style>` near the Attention-tab CSS (just before the `/* ---- Attention tab ---- */` comment at line ~440), add:

```css
  /* ---- Live-terminal selector ---- */
  .term-select-row{display:flex;align-items:center;gap:var(--sp-2);flex-wrap:wrap}
  .term-select{background:#0d0f14;color:var(--text);border:1px solid var(--border);
    border-radius:6px;padding:5px 9px;font:inherit;max-width:48ch}
```

- [ ] **Step 2: Replace the Attention-tab markup**

Replace the entire block from line 865 (`<div x-show="tab==='attention'" x-cloak>`) through line 1008 (`</div><!-- /attention tab -->`) with:

```html
  <div x-show="tab==='attention'" x-cloak>
    <section class="card">
      <div class="section-head">
        <h2>Live terminal</h2>
        <div class="row-actions term-select-row">
          <label for="term-select" class="muted" style="font-size:var(--fs-300)">Agent</label>
          <select id="term-select" class="term-select" x-model="termAgent" @change="openTerm()">
            <optgroup label="Managers">
              <template x-for="a in termManagers" :key="a.name">
                <option :value="a.name" x-text="termLabel(a)"></option>
              </template>
            </optgroup>
            <optgroup label="Workers">
              <template x-for="a in termWorkers" :key="a.name">
                <option :value="a.name" x-text="termLabel(a)"></option>
              </template>
            </optgroup>
          </select>
          <span class="badge"
                :data-s="termStatus==='live'?'busy':(termStatus==='offline'?'offline':'idle')"
                :title="'stream: '+termStatus">
            <span class="d"></span><span x-text="termStatus"></span>
          </span>
          <span class="attn-spacer"></span>
          <button class="mini ghost" type="button" @click="openTerm()" title="Reconnect the live stream">⟳ reconnect</button>
          <button class="mini ghost" type="button" x-show="termAgent" @click="liveLog(termAgent)" title="Open full live log">⤢ full log</button>
        </div>
      </div>

      <template x-if="!termAgent">
        <div class="empty">Pick an agent from the dropdown to open its live terminal.</div>
      </template>

      <template x-if="termAgent">
        <div class="attn-row">
          <pre class="attn-tail live" tabindex="0"
               :aria-label="'live terminal — focus to type into '+termAgent"
               @keydown="handleTermKey($event, termAgent)"
               x-text="termText || '(loading terminal…)'"></pre>
          <div class="quick-keys" :aria-label="'quick keys for '+termAgent">
            <span class="qk-hint">Click the terminal above, then type — or tap:</span>
            <template x-for="k in quickKeys" :key="k.label">
              <button type="button" class="qk" :class="{danger:k.danger}"
                      :title="k.title" @click="sendKeys(termAgent, k.bytes)" x-text="k.label"></button>
            </template>
          </div>

          <div class="attn-actions">
            <div class="attn-action">
              <label for="term-reply">Reply (sent as keystrokes to <span x-text="termAgent"></span>)</label>
              <div class="attn-row-input">
                <input id="term-reply"
                       :aria-label="'reply to '+termAgent"
                       :placeholder="'answer '+termAgent+'… (Enter to send)'"
                       x-model="reply[termAgent]"
                       @keydown.enter="sendMsg(termAgent)">
                <button class="primary" type="button" @click="sendMsg(termAgent)"
                        :disabled="!(reply[termAgent]||'').trim()">Send reply</button>
              </div>
              <div class="attn-hint">Use this for one-liner answers, “1”, “yes”, or a steering nudge.</div>
            </div>

            <div class="attn-action">
              <label for="term-task">Queue a new task for <span x-text="termAgent"></span></label>
              <div class="attn-row-input">
                <textarea id="term-task"
                          :aria-label="'queue task for '+termAgent"
                          placeholder="Describe the task… (Ctrl+Enter to queue)"
                          x-model="attnTask[termAgent]"
                          @keydown.enter="if($event.ctrlKey||$event.metaKey){$event.preventDefault(); queueAttnTask(termAgent);}"></textarea>
                <button class="primary" type="button" @click="queueAttnTask(termAgent)"
                        :disabled="!(attnTask[termAgent]||'').trim()">Queue task</button>
              </div>
              <div class="attn-hint">Goes through the dispatcher; runs once the agent is idle.</div>
            </div>
          </div>
        </div>
      </template>
    </section>
  </div><!-- /attention tab -->
```

- [ ] **Step 3: Add Alpine state**

In `fleet(){ return { ... } }`, add to the reactive-state block (right after the `attnTask: {},` line ~1714):

```javascript
    // ---- live-terminal (Attention tab) ----
    termAgent: '',                  // currently-selected agent for the live terminal
    termText: '',                   // latest rendered screen for termAgent
    termStatus: 'connecting',       // 'live' | 'connecting' | 'offline'
    _termES: null,                  // active EventSource (non-reactive handle)
```

- [ ] **Step 4: Add the selector getters + stream methods**

Add these getters next to the existing `get managers()` getter (after it, ~line 2000):

```javascript
    // Managers first (PMs then global), then plain workers A–Z, for the selector.
    get termManagers(){ return this.managers; },
    get termWorkers(){
      return this.agents
        .filter(a => !(a.manager_of || this.isManager(a)))
        .slice()
        .sort((x,y) => x.name.localeCompare(y.name));
    },
    // Dropdown label: status glyph + name (+ PM tag). 🔴 needs-you, ● busy,
    // ○ idle, ⨯ offline.
    termLabel(a){
      const mark = a.attention ? '🔴'
        : (a.activity==='busy' ? '●' : (a.activity==='idle' ? '○' : '⨯'));
      const tag = a.manager_of ? ('  ·  PM '+a.manager_of) : '';
      return mark+' '+a.name+tag;
    },
```

Add these methods in the `/* ---- Attention tab ---- */` section (replace the now-obsolete `refreshAttnLog` / `refreshAttnLogs` / `_attnInflight` — see Step 6 — with the block below). Add after `quickKeys` / `handleTermKey` / `sendKeys` (they stay):

```javascript
    // Choose which agent to show when none is selected yet: last-viewed
    // (localStorage) -> first agent needing attention -> first manager -> first
    // agent overall.
    pickDefaultTerm(){
      const names = this.agents.map(a=>a.name);
      let pick = null;
      try{ const s = localStorage.getItem('fleet.termAgent'); if(s && names.includes(s)) pick = s; }catch(e){}
      if(!pick){ const att = this.agents.find(a=>a.attention); if(att) pick = att.name; }
      if(!pick && this.managers.length) pick = this.managers[0].name;
      if(!pick && this.agents.length) pick = this.agents[0].name;
      this.termAgent = pick || '';
    },
    // Ensure a terminal is selected and streaming (called on tab open + each poll).
    ensureTerm(){
      if(!this.agents.length) return;                       // wait for first state load
      if(!this.termAgent || !this.agents.find(a=>a.name===this.termAgent)) this.pickDefaultTerm();
      if(this.termAgent && (!this._termES || this._termES.readyState===2)) this.openTerm();
    },
    closeTerm(){
      if(this._termES){ try{ this._termES.close(); }catch(e){} this._termES = null; }
    },
    // One-shot capture so the terminal paints instantly while the SSE stream
    // connects (avoids a blank flash on open / agent switch).
    async refreshTermOnce(){
      const name = this.termAgent;
      try{
        const r = await this.api('/api/logs?agent='+encodeURIComponent(name)+'&lines=200');
        if(this.termAgent===name && r && r.text!=null && this.termStatus!=='live') this.termText = r.text;
      }catch(e){}
    },
    openTerm(){
      this.closeTerm();
      if(!this.termAgent) return;
      try{ localStorage.setItem('fleet.termAgent', this.termAgent); }catch(e){}
      this.termStatus = 'connecting';
      this.termText = '(connecting…)';
      this.refreshTermOnce();
      const name = this.termAgent;
      const es = new EventSource('/api/agent/stream?agent='+encodeURIComponent(name));
      this._termES = es;
      es.onmessage = (e)=>{
        if(this.termAgent !== name) return;                 // a switch raced us
        try{
          const d = JSON.parse(e.data);
          if(d.screen != null){ this.termText = d.screen; this.termStatus = 'live'; }
          else if(d.status === 'offline'){ this.termStatus = 'offline'; }
        }catch(_){}
      };
      es.onerror = ()=>{ this.termStatus = 'connecting'; }; // EventSource auto-retries
    },
```

- [ ] **Step 5: Rewire `init`, `setTab`, and the poll**

In `init()`, change the post-refresh chain (line ~1754) from:

```javascript
      this.refresh().then(()=>this.refreshDocs());
```

to:

```javascript
      this.refresh().then(()=>{ this.refreshDocs(); if(this.tab==='attention') this.ensureTerm(); });
```

Replace the 300 ms Attention-log poll (lines ~1761–1765) — change:

```javascript
      setInterval(()=>{
        if(this.tab !== 'attention') return;
        if(!this.attnWorkers.length && !this.managers.length) return;
        this.refreshAttnLogs();
      }, 300);
```

to a stream watchdog:

```javascript
      // Keep the live terminal connected while the Attention tab is open; the
      // EventSource pushes screen updates, so this only reopens a dropped stream.
      setInterval(()=>{
        if(this.tab !== 'attention') return;
        this.ensureTerm();
      }, 3000);
```

In `setTab(t)`, change the trailing Attention line (line ~1780) from:

```javascript
      if(t==='attention') this.refreshAttnLogs();
```

to:

```javascript
      if(t==='attention') this.ensureTerm();
      else this.closeTerm();
```

- [ ] **Step 6: Remove the dead polling methods and their callers**

In `refresh()`, find the line (~2103):

```javascript
        if(this.tab==='attention' && (this.attnWorkers.length || this.managers.length)) this.refreshAttnLogs();
```

and replace it with:

```javascript
        if(this.tab==='attention') this.ensureTerm();
```

In `sendKeys(name, bytesString)`, remove the post-send poll line (~2348) `this.refreshAttnLog(name);` (the SSE stream now reflects the keystroke). The method becomes:

```javascript
    async sendKeys(name, bytesString){
      if(!bytesString) return;
      let b64;
      try{ b64 = btoa(unescape(encodeURIComponent(bytesString))); }
      catch(e){ return; }
      try{
        const r = await this.api('/api/agent/keys',{method:'POST',
          headers:{'Content-Type':'application/json'},
          body: JSON.stringify({name, keys_b64: b64})});
        if(r && r.error){ this.toast(r.error,'err'); return; }
      }catch(e){ this.toast('keys send failed','err'); }
    },
```

In `sendMsg(name)`, remove the line (~2284) `if(this.tab==='attention') this.refreshAttnLog(name);` (SSE reflects it). 

Delete the now-unused `_attnInflight`, `refreshAttnLog`, and `refreshAttnLogs` definitions (lines ~2356–2380) and the `attnLogs: {},` state field (line ~1713). Confirm there are no remaining references:

Run: `grep -n "refreshAttnLog\|attnLogs\|_attnInflight\|attnWorkers" dashboard.html`
Expected: no matches (the global attention **bar** uses `attnAgents`/`attnNames`, which stay — make sure only `attnWorkers`, `attnLogs`, `refreshAttnLog*`, `_attnInflight` are gone; `attnAgents` and `attnNames` remain).

- [ ] **Step 7: Commit**

```bash
git add dashboard.html
git commit -m "feat(dashboard): Attention tab -> agent dropdown + single SSE live terminal"
```

---

## Task 6: Restart fleet + manual end-to-end verification

**Files:** none (operational + manual verification with Playwright MCP).

> ⚠️ `agent_host.py` and `fleet.py` changes only take effect for **newly spawned** hosts / a **restarted** dashboard (the running fleet serves stale code — see project memory). This restarts all 17 agents.

- [ ] **Step 1: Run the full test suite**

Run: `python -m pytest -q`
Expected: all PASS.

- [ ] **Step 2: Restart the fleet on port 8181**

Run:
```
python fleet.py down
python fleet.py up --port 8181
```
(The `up` blocks — run it in the background. Port 8181 is required: 8787 collides with the BTC5M webapp, and the `fleet-dashboard` tunnel routes `fleet.algorobos.com`→`:8181`.)

Wait for agents to come online:
Run: `python fleet.py status`
Expected: agents report `idle`/`busy` (not `offline`).

- [ ] **Step 3: Verify the live terminal in the browser (Playwright MCP)**

- Navigate to `http://127.0.0.1:8181`.
- Click the **🔔 Attention** tab.
- Confirm a **dropdown** appears with all agents (Managers optgroup, then Workers), each prefixed with a status glyph; one terminal shows below it (the last-viewed / first-attention agent).
- Pick a **busy** agent from the dropdown → confirm the terminal repaints and the stream badge reads **live**, updating as the agent prints (no 1–2 s lag, no full-list of terminals).
- Click into the `<pre>`, type a line, press Enter → confirm it lands in the agent (the streamed screen shows your input).
- Switch to another agent → confirm the previous stream closes (only one `EventSource` open; check `browser_network_requests` shows a single active `/api/agent/stream`).
- Reload the page → confirm it reopens the **last-viewed** agent.

- [ ] **Step 4: Confirm the global attention bar still works**

- With an agent flagged (or by reading the bar), confirm the always-on attention bar above the tabs still lists agents needing you (it uses `attnAgents`, untouched).

- [ ] **Step 5: Commit any fixups**

If the manual pass surfaced tweaks, fix and commit:
```bash
git add -A
git commit -m "fix(dashboard): live-terminal verification fixups"
```

---

## Self-Review Notes

- **Spec coverage:** A (host pub/sub + STREAM) → Tasks 1–2; B (`stream_screen`) → Task 3; C (SSE endpoint) → Task 4; D (frontend selector + single terminal + last-viewed default) → Task 5; testing → tests in Tasks 1–4 + manual in Task 6.
- **Heartbeat contract:** host emits `0\n` frames → `WinBackend._iter_frames` yields `HEARTBEAT` → `fleet.stream_agent_screen` writes `: ping`. Base poll fallback also yields `HEARTBEAT`. Consistent end to end.
- **Input unchanged:** keystrokes still flow over `POST /api/agent/keys`; only the per-keystroke `refreshAttnLog` poll is removed (SSE supersedes it).
- **Naming consistency:** `publish_screen`, `subscribe`, `unsubscribe`, `_offer`, `_write_frame`, `_read_frame`, `_iter_frames`, `HEARTBEAT`, `stream_screen`, `_sse_data`, `_sse_ping`, `stream_agent_screen`, `termAgent`, `openTerm`, `closeTerm`, `ensureTerm`, `pickDefaultTerm`, `refreshTermOnce`, `termLabel`, `termManagers`, `termWorkers` — used identically across tasks.
