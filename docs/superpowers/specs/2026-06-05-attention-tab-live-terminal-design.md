# Attention tab: agent selector + single live terminal

**Date:** 2026-06-05
**Status:** Approved (design)
**Component:** Fleet dashboard (`dashboard.html`), `fleet.py`, `agent_host.py`, `win_backend.py`

## Problem

The dashboard's **Attention** tab renders one focusable `<pre>` terminal tail for
*every* flagged agent plus *every* manager, stacked in a list, each refreshed by
**polling** `/api/logs` (~1–2.5 s). Two issues:

1. **List, not focus.** You scroll a wall of terminals instead of picking the one
   you want.
2. **Lag.** Output is pulled on a timer, so it always trails the agent by up to a
   couple of seconds and the whole `<pre>` repaints on each poll.

Input is *not* the problem — keystrokes already flow live through
`/api/agent/keys`.

## Goal

Replace the list with a **dropdown selector at the top** + **one live terminal**
below it that updates the instant the agent's screen changes ("like a local
terminal, no lag"), while keeping the ability to queue a task / send a message to
the selected agent.

## Decisions (from brainstorming)

| Question | Decision |
|---|---|
| Terminal fidelity | **Fast live screen (plain text).** Push the rendered pyte screen the instant it changes. No xterm.js, no raw-byte tap. |
| Selector scope | **All fleet agents** (workers + PMs + managers), flagged agents marked. |
| Default selection | **Last-viewed agent** (localStorage) → first attention agent → first manager → first agent. |
| Selector control | **Dropdown** (`<select>`). |
| Task box | **Keep** a single queue-task / send-message box for the selected agent. |

## Current architecture (as-is)

- **`agent_host.py`** — per-agent process. Owns a ConPTY + a pyte
  `ScreenBuffer` (120×50). A localhost TCP control socket (`_Handler`) answers
  one-line commands: `PING`, `STATE`, `CAPTURE <n>` (rendered screen text),
  `SEND`, `KEYS <b64>` (raw VT bytes → PTY), `STOP`. The `_pump` thread reads raw
  ConPTY bytes, feeds the screen, runs ready/status logic.
- **`win_backend.py`** — `_ask(port, line)` opens a socket, sends one line, reads
  to EOF, returns the text. `capture()` → `CAPTURE`; `send_keys()` → `KEYS`. Host
  port comes from the agent's status file.
- **`fleet.py`** — HTTP server. `GET /api/logs?agent=&lines=` → `orch._capture`.
  `POST /api/agent/keys` → `orch._send_keys`. Serves `dashboard.html`.
- **`dashboard.html`** — Alpine app. Attention tab (`tab==='attention'`,
  ~lines 865–1008) renders `attnWorkers` + `managers`, each a `<pre>` bound to
  `attnLogs[name]`, refreshed by `refreshAttnLogs()` on a timer while the tab is
  visible. Keystrokes on the `<pre>` → `sendKeys()` → `/api/agent/keys`.

## Design

### A. Host: screen pub/sub + `STREAM` command (`agent_host.py`)

Add a subscriber registry to `AgentHost`:

- `self._subs: list[queue.Queue]` guarded by `self._subs_lock`.
- `self._last_pub_text: str | None` — last published screen text.
- `subscribe() -> Queue`: register a bounded queue (e.g. `maxsize=8`), seed it
  immediately with the **current** screen so a fresh client paints at once,
  return it.
- `unsubscribe(q)`: remove it.
- `publish_screen()`: compute `txt = self.screen.text()` once; if
  `txt != self._last_pub_text`, set it and `put_nowait(txt)` to every subscriber
  (on `queue.Full`, drop the oldest: get_nowait then put_nowait — a slow client
  must never block the pump).

Hook into `_pump`: it already renders the screen for status. After
`observe_screen_for_status()`, call `host.publish_screen()` (reuse the same
render — refactor so the screen text is computed once per iteration and shared by
ready/status/publish to avoid extra pyte passes).

New control command in `_Handler.handle`:

- `STREAM`: register a subscriber, then loop `q.get(timeout=15)`:
  - on a value → write one **length-prefixed frame**: `f"{len(b)}\n".encode()`
    then `b` (UTF-8 of the screen text), flush.
  - on `queue.Empty` (15 s idle) → write a heartbeat frame `b"0\n"` (zero-length
    payload) so a dead socket is detected and the tunnel stays warm.
  - any socket write error → break.
  - `finally`: `host.unsubscribe(q)`.

`STREAM` holds the connection open; `_Server` is already `ThreadingTCPServer`
with `daemon_threads`, so each stream gets its own thread. At most a handful of
streams are ever open (one selected terminal per browser tab).

### B. Backend: `stream_screen(name)` generator

**Contract:** `stream_screen(name)` yields either a **screen string** (new screen
to paint) or a **`HEARTBEAT` sentinel** (a module-level constant), and promises to
yield *something* at least every ~15 s so the consumer can keep the connection
warm and detect a dead client. The generator ends when the worker stops existing.

- **`Backend` (base, `backend.py`):** default `stream_screen(name)` —
  poll-based fallback: loop `capture(name)` every ~0.2 s, `yield` the text
  whenever it changes; if nothing changed for ~10 s, `yield HEARTBEAT`; stop when
  the worker stops existing. Keeps the feature working on any backend (tmux) even
  without `STREAM`.
- **`WinBackend` (`win_backend.py`):** override `stream_screen(name)` — read the
  host port, open a socket, send `STREAM\n`, then read length-prefixed frames in
  a loop: a non-empty payload → `yield` the text; a zero-length frame (the host's
  idle heartbeat) → `yield HEARTBEAT`. Add a small framed-reader helper
  (`_read_exact(sock, n)` + read the `<len>\n` line). On socket error or port
  gone → return (generator ends). Use a recv timeout so a silent socket can't
  wedge the generator forever.

### C. fleet.py: `GET /api/agent/stream?agent=NAME` (SSE)

- Auth-gate as usual (browsers attach cached Basic creds to `EventSource`
  requests automatically — same origin).
- Validate `agent` ∈ AGENTS. If offline (`not orch._window_exists`), send one
  `data: {"status":"offline"}` event and a short retry, then return.
- Headers: `Content-Type: text/event-stream`, `Cache-Control: no-cache`,
  `Connection: keep-alive`, `X-Accel-Buffering: no`.
- Loop over `orch._backend.stream_screen(name)`:
  - a **screen string** → `self.wfile.write(b"data: " + json.dumps({"screen": txt}).encode() + b"\n\n")`; flush.
    JSON-encoding handles the embedded newlines safely (SSE `data:` is line
    based; a raw multi-line screen would otherwise need per-line `data:`).
  - a **`HEARTBEAT`** sentinel → write a `: ping\n\n` comment; flush. Keeps the
    connection alive through cloudflared and surfaces a dead client as a write
    error.
  - on `BrokenPipeError`/`ConnectionResetError` (client navigated away) → return
    quietly.

This endpoint is **read-only streaming**; input still uses the existing
`POST /api/agent/keys`. SSE is one-way (server→client), which is exactly right —
keystrokes are low-volume and already have a path.

### D. Frontend (`dashboard.html`)

New Alpine state:

- `termAgent` — selected agent name (seed from `localStorage['fleet.termAgent']`).
- `termText` — current screen text for the selected agent.
- `termStatus` — `'live' | 'connecting' | 'offline'`.
- `_termES` — the live `EventSource` (non-reactive handle).

Replace the Attention tab body (the `attnWorkers`/`managers` lists) with:

1. **Selector row:** `<select x-model="termAgent" @change="openTerm()">` listing
   **all** agents. Each option label carries a status marker derived from the
   already-polled `/api/state` (`a.attention` → `🔴 needs you`, else
   `a.activity` → `● busy` / `○ idle` / `⨯ offline`). Managers listed first
   (optgroup "Managers", then "Workers"). Plus the existing per-agent `⟳` /
   restart buttons, retargeted to `termAgent`.
2. **Terminal:** one `<pre class="term" tabindex=0 ...>` bound to `termText`,
   reusing the current keystroke capture (`@keydown` → `sendKeys(termAgent, …)`)
   and the quick-key palette, retargeted to `termAgent`.
3. **Task box:** keep one input that calls the existing add-task / send-message
   action for `termAgent` (reuse `addAttnTask` / `sendMessage`).

Behavior:

- `openTerm()`: if `_termES` open, `.close()` it. Persist
  `localStorage['fleet.termAgent'] = termAgent`. Set `termStatus='connecting'`.
  Open `new EventSource('/api/agent/stream?agent='+encodeURIComponent(termAgent))`.
  - `onmessage`: parse JSON; if `.screen` → `termText = .screen`,
    `termStatus='live'`; if `.status==='offline'` → `termStatus='offline'`.
  - `onerror`: `termStatus='connecting'` (EventSource auto-retries; the host
    repaints a full screen on reconnect).
- On tab activation (`setTab('attention')`): if no valid `termAgent`, choose
  last-viewed → first attention agent → first manager → first agent, then
  `openTerm()`.
- On leaving the tab (or component teardown): `_termES?.close()`.
- **Remove** the old `refreshAttnLogs` interval/handlers for this tab. The global
  `/api/state` 2.5 s poll (status badges + the always-visible attention bar above
  the tabs) stays untouched, so flagged agents remain visible regardless of which
  terminal is open.

Optional (small): coalesce burst keystrokes within ~25 ms into one
`/api/agent/keys` POST to cut round-trips over the tunnel. Not required for
correctness; include only if cheap.

## Data flow (output)

```
ConPTY bytes ─▶ _pump ─▶ ScreenBuffer.feed ─▶ publish_screen (on change)
                                                   │
                                            subscriber queues
                                                   │
host STREAM handler ── length-prefixed frames ──▶ WinBackend.stream_screen (generator)
                                                   │
fleet.py /api/agent/stream ── SSE data: {screen} ─▶ browser EventSource ─▶ <pre> termText
```

Input path is unchanged: `<pre>` keydown ─▶ `/api/agent/keys` ─▶ `KEYS` ─▶
`host.write` ─▶ ConPTY.

## Error handling

- Slow/stalled browser → host drops the oldest queued frame (never blocks the
  pump). A wedged socket is caught on the next write and the subscriber is
  removed.
- Agent offline / restarting → generator ends; SSE sends `{"status":"offline"}`;
  EventSource retries; once the host is back the stream reattaches and repaints.
- Tunnel blip → heartbeats keep it warm; EventSource auto-reconnect handles the
  rest.
- Unknown/empty agent → 400 from the endpoint; the dropdown only ever submits
  known names.

## Testing

- **Unit (`tests/`):**
  - `AgentHost.subscribe/publish_screen/unsubscribe`: feed bytes → subscriber
    receives a frame; feeding identical content publishes nothing; after
    `unsubscribe` no further frames; a full queue drops oldest rather than
    raising.
  - Length-prefixed frame reader (`_read_exact` + len line) round-trips a
    multi-line payload, including a zero-length heartbeat.
  - SSE event formatter (pure fn): screen text → `data: {...}\n\n`, newlines
    preserved through JSON.
- **Integration:** extend `tests/test_host_conpty.py` so a `STREAM` connection
  receives an initial screen frame and a follow-up frame after new output.
- **Manual (Playwright MCP, dashboard on `:8181`):** open Attention tab, choose
  an agent from the dropdown, confirm the terminal updates live as the agent
  prints, type a line and see it land, switch agents and confirm the prior stream
  closes.

## Out of scope (YAGNI)

- xterm.js / ANSI color / cursor fidelity (chosen against — plain rendered screen).
- Raw-byte PTY fan-out.
- WebSocket (SSE + existing keystroke POST already covers the need).
- Multi-terminal split view (we are intentionally collapsing to one).
