# The console (UI): how it is built, where to show it, and what you will be asked

Open `swiftserve/ui/` and `swiftserve/dashboard_api.py` for everything below. Line numbers are for the current repository.

---

## 1. The whole picture in one paragraph

The console is a plain HTML, CSS and JavaScript web page (no framework, no build step, 1,900 lines) in `swiftserve/ui/`. The router serves those files itself at `/ui/`. The page talks to the router over three channels:

1. **A live event stream** (`GET /api/events/stream`, server-sent events). The router publishes an event at every step of every request, and the page draws from those events.
2. **A polled snapshot** (`GET /api/dashboard`, every 2 seconds) for replica state: health, GPU data, load, per-replica counters.
3. **The normal chat endpoint** (`POST /v1/chat/completions`) when you press Send or run a test. The page puts its own `X-Request-Id` on each request, and the router copies that id into every event, so the page can join "what I sent" with "what the router did".

```
Browser (ui/)                                   Router (FastAPI)
 Playground / Load test / Compare ──POST /v1/chat/completions──► app.py  (policy → proxy → replica)
                                                      │ publishes events (events.py, telemetry.py)
 Live routing / tables ◄── GET /api/events/stream (SSE) ─────────┘
 Dashboard           ◄── GET /api/dashboard  (every 2 s)  ◄── per-replica state, scraped metrics, GPU data
 Compare             ──POST /api/policy──►  swap the active policy
```

---

## 2. File by file (what each one is)

| File | What it does | Where to look |
|---|---|---|
| `ui/index.html` | The page shell: tab bar and one empty panel per tab | 39 lines |
| `ui/js/main.js` | Starts everything: builds the tabs, polls the snapshot every 2 s, starts the event stream, shows connection state | `start`, `begin`, `refreshSnapshot` |
| `ui/js/api.js` | All network code. `getJSON`, `postJSON`, the SSE parser, `startEventStream` (reconnect and resume), `chat` (sends a request and measures what the browser saw) | `startEventStream` line 67, `chat` line 106 |
| `ui/js/store.js` | The single source of truth about requests. Merges what the **router** said (events) with what the **browser** saw, per request id | `class Store` line 38, `applyEvent` |
| `ui/js/playground.js` | One request at a time, with the result panel | `initPlayground` |
| `ui/js/loadtest.js` | N requests at a chosen concurrency, per-request table, summary, export | `start`, `launch` |
| `ui/js/compare.js` + `compare_logic.js` | Runs the same seeded workload through each policy and tabulates the result. The pure logic is separate so it can be unit tested | `runPolicy`, `buildWorkload`, `summarize` |
| `ui/js/routing.js` | The Live routing tab: an SVG diagram, moving dots, decision table, timeline | `store.onEvent(...)` line 149 |
| `ui/js/dashboard.js` | The Dashboard tab: a card per replica plus tiles | `replicaCard`, `render` |
| `ui/js/detail.js` | The per-request result panel ("Served by", "Why", TTFT, cache reuse, All details) | `renderDetail` |
| `ui/js/util.js` | Small helpers: `h()` builds elements, `describeDecision`, formatters, `fill` | `describeDecision` line 93 |
| `dashboard_api.py` | The router side of the console: `/api/dashboard`, `/api/events`, `/api/events/stream`, `/api/requests`, `/api/requests/{id}`, and serves `/ui/` | `register_dashboard` line 259, `events_stream` line 296 |
| `events.py` | `EventBus`: a ring buffer of events with a sequence number, plus subscriber queues | `EventBus` line 99 |
| `telemetry.py` | `RequestTrace`: one object per request that emits the events and records the final outcome once | `RequestTrace` line 383, `routed` line 415, `completed` line 490 |

---

## 3. How the Live routing tab works (the part you will be asked about most)

**What you see:** an SVG diagram: Client, then the router with its three stages (Admission, Routing policy, Dispatch), then the four replica boxes. Coloured dots travel along the lines. Each replica box shows its health, circuit state, queue, how many requests were selected and served, and a number for requests running on it now. Below it: a table of recent routing decisions.

**How it works, in order:**
1. The router emits an event at each step of a request: `request.received`, `routing.decision`, `dispatch` (and `dispatch.retry`), `first_token`, `request.completed` (or `failed`, `client_disconnected`, `rejected`).
2. `events.py` stamps each with an increasing sequence number and stores it in a ring buffer (5,000 by default).
3. `GET /api/events/stream` sends them to the browser as server-sent events.
4. `api.js` `startEventStream` reads the stream and calls `store.applyEvent(event)`.
5. `store.js` updates that request's record. `routing.js` listens with `store.onEvent`:
   - `request.received` makes a dot appear on the client-to-admission line.
   - `routing.decision` moves a dot to the policy box.
   - `dispatch` moves it to the chosen replica.
   - `request.completed` sends a green dot back.
   - `request.failed` shows a red dot that fades.
   - `dispatch.retry` flashes the replica box.
6. **Dots move only when the matching event arrives.** Nothing is simulated by the page.
7. **Edge thickness** (`routing.js` line 199) is each replica's share of all routing decisions since the router started.
8. **The number in a replica box** is how many requests are dispatched to it and not yet finished, computed from events.
9. **The decision table** shows, for each request, the path the policy recorded and the replica. The text comes from `describeDecision` in `util.js`, and is built **only from fields the policy actually recorded**. If a policy recorded nothing, the page shows nothing.

**What keeps it honest:**
- Each policy writes its reason into `last_decision` (a plain dict). The router reads it right after `select()` returns, in the same synchronous step, so concurrent requests cannot mix them up.
- The router snapshots every replica's state **before** the policy runs and sends that with the decision, so the console shows what the policy actually saw.

---

## 4. Where to show "proper implementation" (a 3-minute code tour, in this order)

1. **`swiftserve/telemetry.py`, `RequestTrace.routed` (line 415) and `completed` (line 490).** "This is where an event is created. One trace object per request, and the final outcome is recorded exactly once."
2. **`swiftserve/events.py`, `EventBus.emit` (line 113).** "The bus never blocks and never raises, so the console can never slow down or break a request. Each subscriber has a bounded queue, and a slow browser gets a 'gap' notice instead of back-pressure."
3. **`swiftserve/dashboard_api.py`, `events_stream` (line 296).** "Server-sent events. A reconnecting browser sends the last sequence number it saw and receives what it missed. Each connection is capped at 30 seconds so server shutdown never hangs, and the page reconnects at once."
4. **`swiftserve/ui/js/api.js`, `startEventStream` (line 67).** "The reconnect loop with exponential backoff, resuming from the last event."
5. **`swiftserve/ui/js/store.js`, `class Store` (line 38) and its `applyEvent` method.** "Each request has two sides: `server`, from router events, and `client`, from what this browser measured. They are kept apart so the page can say where each number came from."
6. **`swiftserve/ui/js/routing.js`, `store.onEvent` (line 149).** "Event type to animation. This is the entire logic for the moving dots."

---

## 5. Questions you may be asked, and the answer

**Is the Live routing view real, or an animation?**
Real. Every dot is created when the router emits the matching event, and the page does nothing on its own. You can prove it live: in a terminal run `curl -N "http://127.0.0.1:8000/api/events/stream?since=0"`, send a request, and watch the same events print as the dot moves.

**How does the page know which replica a request went to?**
From the router's events, not from guessing. The `dispatch` event names the replica, and `request.completed` names the replica that served it. The page also reads the `X-SwiftServe-Replica` header on the response, and the console shows both.

**What if the browser disconnects or refreshes?**
The page reconnects automatically (backoff 0.5 s up to 8 s) and resumes from the last sequence number it saw. If the router's buffer has already dropped those events, it sends a `gap` marker and the page reloads the recent requests from `/api/requests`.

**Can the console slow the router down?**
No. `EventBus.emit` never blocks or raises, subscriber queues are bounded, and the router never reads from the console.

**Does it show fake data?**
No. A metric that nobody measured is shown as "unavailable" with a reason, never as zero. GPU numbers appear only when a node reports them through its sidecar. A simulated mode exists for development, and when it is on, the page says SIMULATED.

**How is TTFT measured?**
At the router, by a `StreamTimer` in `telemetry.py` that parses the response stream. It starts when the request arrives and stops at the first text chunk. The browser also records its own view, which is slightly larger because it includes the browser-to-router hop. The result panel labels each as `router` or `browser`.

**Why no React or other framework?**
The page is small, has no build step, and runs by just being served. It is plain ES modules, builds elements with a small `h()` helper, and never uses `innerHTML`, which a test enforces so request text can never become markup.

**What stops memory growing forever?**
The server's event buffer and per-request records are bounded rings (5,000 events, 2,000 records by default). The page keeps at most 3,000 requests and 60 throughput samples.

**What about security?**
If the router is started with `SWIFTSERVE_API_TOKEN`, the API requires a bearer token. The page then asks for it and keeps it in memory only. Policy switching is off by default and `scripts/dev.py` turns it on only for a localhost bind.

**How was the UI tested?**
Node unit tests for the request store and the Compare logic, a syntax check of every module, a check that no view calls a helper it did not import, and a browser end-to-end test (Playwright) that runs the real stack with fake replicas. Be clear that the browser test used simulated replicas, and the real GPUs were checked by hand.

**What does the Compare tab do differently from Load test?**
Load test sends one workload through the active policy. Compare calls `POST /api/policy` to switch policy between runs, sends the same seeded multi-tenant workload through each, gives each its own prompt namespace so no policy reuses another's cache, and tabulates the results from router-measured values.

---

## 6. Proof commands to have ready

```bash
# live events, exactly what Live routing draws from (each connection ends after 30 s by design; run it again)
curl -N "http://127.0.0.1:8000/api/events/stream?since=0"

# the snapshot the Dashboard polls (replicas, GPU data, counters)
curl -s http://127.0.0.1:8000/api/dashboard | python3 -m json.tool | head -60

# recent requests with their decisions
curl -s "http://127.0.0.1:8000/api/requests?limit=5" | python3 -m json.tool | head -60
```

In the browser, open Developer Tools, Network tab, and filter on `events`: you will see the one long-lived `stream` request, which is the whole live feed.
