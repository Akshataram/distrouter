# System architecture and complete UI reference

This is the "know it all" file. Section 1 and 2 are the flow: what talks to what, in what format, in what order. Section 5 is every element on every page. Section 7 is the honest list of what you can and cannot claim. Formulas for each metric are in `PAGE_BY_PAGE_ANSWER_GUIDE.md`.

---

## 1. System architecture

### 1.1 The machines and processes

```
 YOUR LAPTOP                                              THE INTERNET                COLAB (x4, one per GPU)
┌──────────────────────────────────────────────┐                                  ┌────────────────────────────┐
│ Browser: the console (HTML/CSS/JS)           │                                  │ cloudflared (tunnel client) │
│     │  HTTP + server-sent events             │                                  │        │ localhost          │
│     ▼                                        │                                  │        ▼                    │
│ Router process  (uvicorn + FastAPI)          │   HTTPS      ┌──────────────┐    │ Sidecar  (FastAPI, :9000)   │
│   ├ chat endpoint ─ admission ─ POLICY ─ proxy├────────────►│  Cloudflare   │───►│   ├ /sidecar/gpu (nvidia-smi)│
│   ├ metrics scraper (background task)        │◄────────────│  quick tunnel │◄───│   ├ /chaos/* (fault inject) │
│   ├ event bus + request traces               │              └──────────────┘    │   └ everything else ─proxy─┐│
│   └ dashboard API + serves the console files │                                  │                             ││
└──────────────────────────────────────────────┘                                  │ vLLM  (:8000, Qwen2.5-3B)  ◄┘│
                                                                                  │ Tesla T4 GPU                 │
                                                                                  └────────────────────────────┘
```

| Component | What it is | Where it runs | Port |
|---|---|---|---|
| **Console** | The web page. Plain HTML/CSS/JavaScript, no framework | Your browser | n/a |
| **Router** | A FastAPI application started with uvicorn. It holds the routing policies, the per-replica state, the proxy, the event bus, and serves the console | Your laptop | 8000 |
| **Cloudflare quick tunnel** | Gives each Colab node a public HTTPS address (`*.trycloudflare.com`), because Colab has no public IP. The URL changes every time the tunnel restarts | Cloudflare | 443 |
| **Sidecar** | A small FastAPI proxy in front of vLLM on every node. It forwards ordinary requests to vLLM, reports the node's GPU (`/sidecar/gpu`), and has fault-injection endpoints (`/chaos/*`) used only for failure experiments | Each Colab node | 9000 (9100 in your copy) |
| **vLLM** | The inference engine. Loads Qwen2.5-3B-Instruct, runs prefill and decode, keeps the prefix cache, exposes an OpenAI-compatible API and Prometheus `/metrics` | Each Colab node | 8000 |
| **GPU** | Tesla T4, 15 GB. Holds the model weights and the KV cache | Each Colab node | n/a |

The tunnel points at the **sidecar**, not at vLLM. So every request to a replica goes laptop → Cloudflare → sidecar → vLLM, and every response comes back the same way.

### 1.2 Every connection, with the type of data on it

| # | From → To | Protocol | What travels | Format |
|---|---|---|---|---|
| 1 | Browser → Router | HTTP `POST /v1/chat/completions` | The user's request | JSON body `{model, messages:[{role,content}], stream, stream_options, max_tokens, temperature, top_p}` plus headers `X-Request-Id`, `X-SLA-Ms`, `X-Session-Id` (and `Authorization` if a token is set) |
| 2 | Router → Replica | HTTPS `POST {tunnel}/v1/chat/completions` (streaming) | The same request, forwarded | The same JSON, sent with an `X-Request-Id` |
| 3 | Sidecar → vLLM | HTTP on localhost | The same request | The same JSON |
| 4 | vLLM → Sidecar → Router | HTTP stream | The answer, token by token | Server-sent events: lines `data: {json}` ending with `data: [DONE]`. Each JSON chunk has `choices[0].delta.content`; the last chunk carries `usage` (prompt, completion, **cached tokens**) |
| 5 | Router → Browser | HTTP stream | The same stream, relayed, plus headers `X-SwiftServe-Replica`, a predicted-cached-tokens header, and `X-Request-Id` | Server-sent events |
| 6 | Router → Replica (background) | HTTPS `GET {tunnel}/health` and `/metrics` every 2 s | The replica's health, running and waiting requests, KV-cache fill, prefix-cache hit counters | Plain text in Prometheus format, parsed with regular expressions |
| 7 | Router → Sidecar (background) | HTTPS `GET {tunnel}/sidecar/gpu` at most every 5 s | GPU name, utilization, memory used and total, temperature | JSON from `nvidia-smi` (a 404 means that node's repo copy is old, and the console says "unavailable") |
| 8 | Router → Browser (live) | HTTP `GET /api/events/stream` | An event for every step of every request | Server-sent events, each `data:` is JSON `{seq, ts, type, request_id, …}` |
| 9 | Router → Browser (polled) | HTTP `GET /api/dashboard` every 2 s | The state of the whole router | One JSON object (replicas, totals, throughput, policy state, warnings) |
| 10 | Browser → Router | HTTP `GET /api/requests`, `/api/requests/{id}` | Recent request records | JSON |
| 11 | Browser → Router | HTTP `POST /api/policy` | "Switch to policy X" (Compare tab only) | JSON `{name}`. Disabled unless the launcher enables it; refused if requests are in flight |
| 12 | Browser → Router | HTTP `GET /ui/…` | The console files | HTML, CSS, JS |

### 1.3 The path of one request (in order)

1. **Browser** sends the request (#1). The page put its own `X-Request-Id` on it.
2. **Router, middleware:** reads or creates the request id, makes it available to all log lines, and will echo it in the response.
3. **Router, `chat_completions`:** reads the body and headers, works out the session id (header, else the `user` field, else a random id) and the SLA (header, else 3,000 ms). It opens a **trace** and emits `request.received`. An invalid body is rejected here with a 400.
4. **Admission control:** if 256 requests are already in flight, the request is rejected with a 503 and `request.rejected` is emitted.
5. **Snapshot:** the router reads the state of every replica (circuit, health, in flight, queue, estimated latency, capacity) and keeps it for the decision record.
6. **Candidates:** replicas whose circuit is open are excluded. If none are left, the request is rejected. A recovering ("half-open") replica may be given this request as a probe instead of asking the policy.
7. **Policy:** `select()` (or `select_with_prediction()` for `prefix_aware`) returns a replica. The policy writes its reason into `last_decision`. The router emits `routing.decision` with the reason, the candidates snapshot and the predicted cached tokens.
8. **Proxy:** sends the request to that replica (#2). Emits `dispatch`. On a connection error it retries the **same** replica up to 2 more times (3 attempts in all, with a short growing delay starting at 0.1 s) and emits `dispatch.retry`.
9. **Stream:** as bytes come back, a `StreamTimer` parses the server-sent events. At the first non-empty text chunk it emits `first_token` (this is TTFT). Every chunk is relayed to the browser (#5).
10. **Finish:** when the stream ends the router records the outcome **once**: `request.completed` (status below 500 and stream intact), `request.failed` (status 500 or above, connection error, timeout or a broken stream) or `request.client_disconnected` (the browser went away). It updates the replica's counters, latency windows, latency average and the circuit breaker, and releases the admission slot.
11. **Console:** in parallel, steps 3 to 10 each produced an event that reached the page over #8, which updated the tables, the moving dots and the per-replica numbers.

### 1.4 Things that happen in the background (not tied to one request)

| What | How often | What it does | Where |
|---|---|---|---|
| **Metrics scrape** | every 2 s per replica | Reads `/health` and `/metrics`; stores running, waiting, KV-cache fill, prefix-cache counters; raises the learned capacity to the highest concurrency seen | `metrics_scraper.scrape_loop` |
| **GPU poll** | at most every 5 s per replica | Reads `/sidecar/gpu` and stores GPU name, utilization, memory, temperature | `metrics_scraper.poll_gpu` |
| **Replica state sampler** | every 1 s | Emits a `replica.state` event only when a replica's circuit state or health changed; fills the "Replica health changes" log | `telemetry.watch_replica_state` |
| **Snapshot poll** | every 2 s (browser) | Refreshes the Dashboard and the replica boxes | `main.js` |
| **Event stream reconnect** | when the connection drops, or every 30 s by design | Resumes from the last event it saw | `api.js` |

### 1.5 What is stored where (all in the router's memory, lost on restart)

| Data | Structure | Bound |
|---|---|---|
| Events | Ring buffer, each with an increasing sequence number | 5,000 |
| Request records | Ordered dictionary by request id | 2,000 |
| Per-replica latency windows (TTFT, ITL, total) | Deques of recent values | Last 500 per replica |
| Per-replica counters (selected, served, failed, tokens, cached tokens) | Integers | Cumulative since start |
| Per-replica learned state (latency average, three load buckets, capacity, sessions) | Floats and an ordered dictionary | Sessions capped at 20,000 |
| `prefix_aware` index | `hash → set of replicas` plus a per-replica LRU | 20,000 blocks per replica |
| `swiftserve` trie | Tree of whole messages | 20,000 nodes, depth 6; whole tree reset when exceeded |
| Throughput | Recent completions with timestamps | 30-second window |

Nothing is written to disk. Restarting the router clears all of it. The console keeps at most 3,000 requests in the browser.

### 1.6 Startup and shutdown flow (`scripts/dev.py`)

1. Check that the Python packages are installed and the port is free.
2. For every replica URL: probe it. A Cloudflare 530 means the tunnel is gone; a 502/503/504 means the tunnel is up with nothing behind it. Detect or check the model name from `/v1/models`. Print warnings (for example no tokenizer, or `--max-num-seqs` not given).
3. Build the environment variables the router reads (replicas, policy, model, tokenizer, block size, assumed batch size, labels, policy switching on for localhost).
4. Start `uvicorn swiftserve.app:app`. On startup the router builds one `ReplicaState` per replica, builds the policy (for `prefix_aware` it loads the tokenizer and runs a self-check that the tokenizer yields at least 2 blocks), and starts the scrape and state tasks.
5. Wait until the router answers `/healthz`, then print the console address.
6. On Ctrl+C: the router stops accepting, gives in-flight requests up to 8 seconds, and exits. The launcher never starts vLLM or touches a GPU.

---

## 2. The routing decision in one picture

```
 request
   │
   ▼
 admission (≤256 in flight) ──no──► 503 rejected
   │yes
   ▼
 candidates = replicas with circuit not open ──none──► rejected
   │
   ▼
 half-open replica waiting for a probe? ──yes──► send it there (path: half_open_probe)
   │no
   ▼
 ┌─────────────────────── active policy ───────────────────────┐
 │ round_robin       counter mod N                               │
 │ least_connections shortest queue (ties: lowest id)            │
 │ swiftserve        session owner? → message trie? → least load│
 │ prefix_aware      block hashes → longest eligible match →     │
 │                   SLA check → else least loaded               │
 └──────────────────────────────────────────────────────────────┘
   │
   ▼
 proxy to the chosen replica (retry connection errors on the SAME replica)
   │
   ▼
 stream back; record outcome once; update counters, latency average, circuit breaker
```

---

## 3. Numbers to know by heart

| Fact | Value |
|---|---|
| Cache block size | **16 tokens** |
| Eligible match (`prefix_aware`) | at least **50%** of the prompt's blocks **or** at least **256 tokens** |
| Default response-time target (SLA) | **3,000 ms**, per request via `X-SLA-Ms` |
| Admission cap | **256** requests in flight |
| Retries | up to **2** more attempts on the same replica |
| Circuit breaker | opens after **5** consecutive failures; cooldown **10 s**, doubling to **120 s**; then one probe |
| Metrics scrape | every **2 s**; vLLM figures trusted for **5 s** |
| GPU poll | at most every **5 s** |
| Latency average | exponential, weight **0.2**; **3** load buckets; starts at **800 ms** |
| Queue estimate at capacity | `service time × (queue depth + 1) ÷ capacity` |
| `swiftserve` message match | needs at least **2** whole messages |
| Index and trie limits | 20,000 blocks per replica / 20,000 nodes, depth 6 |
| Event buffer, records | 5,000 events, 2,000 records |
| Console reconnect | event stream capped at **30 s** per connection, then resumes |
| Dashboard windows | latency: last **500** requests; req/s and tokens/s: last **30 s** |
| Your setup | 4 × Tesla T4 (15 GB), Qwen2.5-3B-Instruct, KV cache set to 375 blocks (about 6,000 tokens), `--max-num-seqs 32` on the node, `--max-num-seqs 4` given to the router |

---

## 4. Router and sidecar endpoints

**Router**
| Endpoint | Purpose |
|---|---|
| `POST /v1/chat/completions` | The routed chat endpoint |
| `POST /api/policy` | Swap the active policy (off by default) |
| `GET /api/dashboard` | The whole-router snapshot |
| `GET /api/events`, `/api/events/stream` | Buffered events / live event stream |
| `GET /api/requests`, `/api/requests/{id}` | Recent request records / one record |
| `GET /healthz` | The router's own liveness |
| `GET /metrics` | Prometheus metrics for monitoring |
| `GET /status` | A plain JSON status summary |
| `GET /ui/` | The console |

**Sidecar (on every node)**
| Endpoint | Purpose |
|---|---|
| `GET /sidecar/gpu` | The node's GPU from `nvidia-smi` |
| `/chaos/*` | Fault injection (partition, latency, error rate, kill, restart), protected by an admin token. The tokens printed by the node cell are for this only; **routing never uses them** |
| everything else | Forwarded to vLLM |

---

## 5. Every element in the console

The **header** appears on every page:
| Element | Meaning |
|---|---|
| **SwiftServe Console** | The page title |
| **Mode badge** | "live replicas" means the router is pointed at real servers. "SIMULATED replicas" appears only if the launcher said the replicas are fake. The router cannot detect fake replicas itself |
| **Meta text** | "4 replicas · policy prefix_aware · model Qwen/Qwen2.5-3B-Instruct": how many replicas the router has, the **active** policy, the model name |
| **Events badge** | "events: live" (the stream is connected), "connecting", "reconnecting", "unauthorized" (a token is needed) or "error" |
| **API token box** | Appears only if the router requires `SWIFTSERVE_API_TOKEN`. The token is kept in memory and never saved |
| **Tabs** | Playground, Load test, Compare, Live routing, Dashboard |

### 5.1 Playground
| Element | Meaning |
|---|---|
| System prompt | Leading instruction text; the shared prefix the cache can reuse |
| Prompt | The user's question |
| max_tokens | Cap on generated tokens |
| Streaming | On: TTFT and ITL are measurable. Off: they show n/a |
| Advanced: Model name, temperature, top_p, SLA ms, Session id | See the parameter table in the page guide |
| **Send** | Sends one request |
| **Abort in-flight** | Cancels the page's request. The router records a streamed one as `client_disconnected`. Whether vLLM stops generating afterwards was **not verified** |
| Status line | Request sent, or an error |
| **Response** box | The model's text, as it streams in |
| **Result** panel | State, Served by, Why, TTFT, ITL (mean), Total, Reused from cache, Router predicted; "All details" for the full view |
| **Requests** table | One row per request this page sent: State, Served by, TTFT, Total. Click a row to see its Result |

### 5.2 Load test
| Element | Meaning |
|---|---|
| Requests, Concurrency, max_tokens | How many, how many at once, answer cap |
| Prompt source: common or list | One prompt for all, or a list cycled |
| Prompt / Prompts | The question text |
| Shared system prompt | The leading text shared by all requests |
| **Insert sample ~1500-token system prompt** / **Clear** | Fills or clears the system prompt box |
| Advanced: Delay, temperature, top_p, Streaming, SLA ms, Model | See the page guide |
| **Start** | Begins the run |
| **Stop** | Stops launching new requests and **aborts in-flight ones in the browser**. Streamed ones are recorded by the router as `client_disconnected`. Non-streaming requests are not cancelled upstream |
| **Clear results** | Removes this run's rows from the page (only when no run is active) |
| Progress | launched, succeeded, failed, aborted (only if any), router saw disconnect (only if any), elapsed, req/s |
| Where the requests went | Selected vs served per replica |
| Summary | TTFT p50, TTFT p95, ITL mean, tokens/s, prompt reused from cache |
| **Every request** table | #, State, Served by, TTFT, Total, Cached / prompt tokens, Error |
| **only failures** checkbox | Filters the table |
| **Export JSON / CSV** | Downloads the run's rows, including routing path and predicted vs cached tokens |

### 5.3 Compare
| Element | Meaning |
|---|---|
| Policy checkboxes | Which policies to run, in the listed order |
| Requests per policy, Concurrency, Tenants, System prompt tokens, max_tokens, Seed | See the page guide |
| Advanced: **Reverse the policy order**, Model name | Order check; model |
| Yellow banner | "This router does not allow switching policy while running": it was not started with `dev.py`, or the flag is off |
| **Run comparison** | Runs each policy in turn: switches policy, sends the seeded workload, summarizes, then restores the original policy at the end |
| **Stop** | Stops; the policy that was running is discarded and the finished ones are kept |
| Status line | "Running X (n of N)…", "Finished.", or an error |
| Result table | TTFT median, TTFT 95th percentile, Prompt reused from cache, Load balance, ITL mean, Completed / failed |
| Bar chart | Median TTFT per policy |
| Sentence under the chart | Generated from the numbers: lowest median, the ratio, highest cache reuse, lowest 95th percentile. It states a ranking, never a cause |
| Requests per replica | How many each replica served under each policy |
| **Export JSON** | Config, order, per-policy results and **one row per request** (replica, routing path, TTFT, cached tokens, predicted cached tokens) |

### 5.4 Live routing
| Element | Meaning |
|---|---|
| Diagram: Client → 1 Admission → 2 Routing policy → 3 Dispatch → replica boxes | The real request path; boxes show live state from the snapshot |
| Moving dots | One per request, moved only by the router's events; colour is stable per request id; green back-trip is a completion; red is a failure |
| Replica box | Label, host, circuit, queue, selected, served, running now, health dot |
| Line thickness | Share of all routing decisions to that replica since the router started |
| **Recent routing decisions** | Time, Request, Path, Selected, Dispatched, Served, Why |
| Collapsed: **Timeline** | Per-request bar: routing, waiting for first token, streaming, failed, retry marks |
| Collapsed: **Replica health changes** | Log of circuit and health changes from the 1-second sampler |

### 5.5 Dashboard
| Element | Meaning |
|---|---|
| Warning banners | Shown only when something makes the numbers less trustworthy: `--max-num-seqs` not set (the latency model assumes 1), no tokenizer for `prefix_aware`, or no replica labels. GPU unavailability is shown on each replica card with its reason |
| Tiles | completed, failed, in flight now, req/s, tokens/s |
| Replica card | Label · GPU name, healthy badge, circuit badge (if not closed), GPU utilization, VRAM, temperature, in flight, served, failed, TTFT p50, ITL mean, reused from cache |
| **Router settings and what is measured** (collapsed) | Policy and router state, and a table saying which metrics are measured, partial or unavailable and why |

---

## 6. Reading the "unavailable" and "n/a" cases honestly

| You see | It means |
|---|---|
| **n/a** for TTFT / ITL | The request was not streamed (no first token to observe), or fewer than 2 text chunks arrived |
| **"not reported"** for Reused from cache | vLLM was not started with `--enable-prompt-tokens-details`. It is never shown as 0 |
| **"unavailable"** for a GPU | That node's sidecar does not expose `/sidecar/gpu` (older repo copy), or `nvidia-smi` failed. The console shows the reason |
| **n/a (this policy makes no prediction)** | Only `prefix_aware` predicts cached tokens |
| **SIMULATED** | The launcher was run with `--simulate`. Never use those numbers |

---

## 7. What you can say, and what you cannot (be straight about this)

### Verified (observed on the real four-T4 setup)
- The router routes real requests to four real GPUs through tunnels, and the console shows which replica served each one.
- vLLM's prefix cache works on the nodes: a node-level test showed 2,061 ms cold against 74 ms with 1,600 of 1,617 prompt tokens cached.
- On a test request the router predicted 16 cached tokens and vLLM reported 16.
- Three Compare runs of all four policies (60 requests each): `prefix_aware` had the lowest median TTFT in all three and the highest cache reuse, and the most uneven load. Its tail latency was **not** consistently better. `swiftserve` beat the load-based baselines on median in two runs and lost to both in the third.
- GPU name, memory and temperature on the Dashboard come from each node's `nvidia-smi`.

### Tested only with mocked or fake replicas (not GPU evidence)
- The 371 automated tests, the event stream behaviour, retries, the circuit breaker, client-disconnect accounting, the policy switch, and the browser end-to-end test. Say "tested against mocked and fake replicas, and checked by hand on the real GPUs".

### Not verified: say "I don't know" or "not done"
- **Why the 6 failures happened** on the Dashboard card, until you run the failed-request command and read the cause. Do not guess.
- **Why `prefix_aware`'s tail latency varied** between runs. The load imbalance suggests queueing on a busy replica, but that is not confirmed.
- **Why `swiftserve` reused more cache than the baselines.** It is a reasonable reading of the code (repeated tenant and question pairs give message-level matches), but it was not checked request by request.
- **Whether vLLM stops generating after a browser disconnect.** Not tested.
- **Failover to a different replica.** Not implemented. Connection errors retry the same replica.
- **Other models, other cache sizes, longer runs, more replicas.** Not tested.
- **Results are a controlled demonstration, not a benchmark:** policies run one after another on shared Colab GPUs with tunnel noise (the baselines' own median varied from about 800 to 1,066 ms between runs).
- **Run 1 values** were read from a screenshot and are rounded.

### Known limitations of the design (you can state these confidently)
- The prefix index is a **belief**: blocks are recorded optimistically when a request is routed. vLLM may evict them, and a failed request leaves a stale entry. The router does not see engine evictions.
- The router routes on circuit state, not on the scraped health flag.
- The index cap (20,000 blocks) is far above the engine's real capacity (375 blocks here).
- Everything is in memory and lost on restart.
- The prediction is only meaningful if the router's tokenizer and block size match the engine's.

---

## 8. Architecture questions you may be asked

**Why a router at all, not just a load balancer?**
A load balancer sees connections; this router reads the prompt, hashes it into the units the engine caches, and keeps a belief about which replica holds which blocks. That is the only way to send a request to the replica that can skip prefill.

**Why a sidecar on each node?**
Colab exposes one port through the tunnel. The sidecar owns that port, forwards normal traffic to vLLM, and adds what vLLM does not give: GPU readings from `nvidia-smi` and fault injection for failure experiments.

**Why is the router on a laptop and not on a server?**
It is a development and demonstration setup. The router is CPU-only and light, so it runs where the console is. The cost is that every request crosses the internet twice, which is why tunnel latency is part of every TTFT.

**Where does the router get the replicas' load from?**
Two sources: its own in-flight counter (exact, instant) and vLLM's `/metrics` (running and waiting, scraped every 2 s and trusted for 5 s). The queue depth is the larger of the two.

**What happens if a replica dies?**
Connection errors are retried on the same replica. Five consecutive failures open that replica's circuit, so the policy no longer sees it. After a cooldown one probe request tests it, and success closes the circuit. There is no failover of an in-progress request to another replica.

**What happens if the router restarts?**
All its memory is lost: the prefix index, the learned latencies, the sessions, the events. vLLM's own caches are unaffected, so the router has to relearn which replica holds what.

**How does the console stay in sync?**
It receives the router's events live, and polls a snapshot every 2 seconds for replica state. If it disconnects it resumes from the last event number; if the router has already dropped those events, it reloads recent requests.

**Can the console change how requests are routed?**
Only through `POST /api/policy` (the Compare tab). That swaps one existing policy for another; it adds no routing logic. It is off by default, enabled by the launcher only for localhost, and refused while requests are in flight.

**Is anything faked?**
No data on the console is invented. A metric nobody measured shows as unavailable with a reason. The worked example on the slides is labelled illustrative. The simulated mode exists only for developing the console and is labelled SIMULATED.
