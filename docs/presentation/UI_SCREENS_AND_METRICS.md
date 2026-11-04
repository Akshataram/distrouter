# Every console screen, every metric, and how each metric is produced

Source files: `swiftserve/ui/js/*.js` (the pages), `swiftserve/dashboard_api.py` (snapshot the pages read), `swiftserve/telemetry.py` (timing and events), `swiftserve/state.py` (per-replica state), `swiftserve/metrics_scraper.py` (vLLM /metrics), `swiftserve/replica_sidecar.py` (GPU data). Everything below was read from that code. Nothing here was run against your GPUs from the sandbox.

---

## Part 1: The eight kinds of metric (how to answer "how did you get that number?")

| # | Kind | Examples | Where it is produced | How |
|---|---|---|---|---|
| A | **Router-measured timing** | TTFT, ITL, TPOT, total latency, upstream time | `StreamTimer` and `RequestTrace` in `telemetry.py` | The router starts a monotonic clock when the request arrives and records a timestamp at each event as the response streams through it. TTFT = arrival → first stream event with non-empty `delta.content`. ITL = (time of last text event − time of first) ÷ (text events − 1). TPOT = same span ÷ (completion tokens − 1). Total = arrival → response finished. Upstream time = first dispatch → finished (this feeds the latency average). |
| B | **Engine-reported counts** | prompt tokens, completion tokens, cached tokens | vLLM's `usage` object in the **last** stream chunk | Needs `stream_options.include_usage` (the router/page asks for it). `cached_tokens` is `usage.prompt_tokens_details.cached_tokens` and exists only because the nodes run with `--enable-prompt-tokens-details`. |
| C | **Router's own bookkeeping** | in flight, served, failed, selected, queue depth, circuit state, admission count | `ReplicaState` in `state.py`, counters in `app.py`/`proxy.py` | Counters incremented by the router as it dispatches and finishes requests. Queue depth = max(vLLM running+waiting, router in-flight) if the scrape is under 5 s old, otherwise just in-flight. |
| D | **Learned estimates** | est. latency, batch capacity | `estimate_latency_ms()` and `effective_batch_capacity()` in `state.py` | EWMA of observed request time (α = 0.2, 3 occupancy buckets, seed 800 ms) fed through an M/M/c-style formula; capacity = max(`--max-num-seqs` given to the router, highest running count scraped). It is an estimate, not a measurement. |
| E | **Scraped from vLLM** | running/waiting requests, KV-cache usage, engine prefix hit/query counters, health | `metrics_scraper.py` | Every 2 s the router calls each replica's `/health` and `/metrics` through the tunnel and parses vLLM's Prometheus text. |
| F | **Hardware** | GPU name, utilisation, VRAM, temperature | `replica_sidecar.py` `GET /sidecar/gpu` | The sidecar on each node runs `nvidia-smi`; the router asks at most every 5 s. If that call fails the console shows "GPU: unavailable" with the reason. |
| G | **Policy decision facts** | path, matched blocks, thresholds, estimated latency, SLA, predicted cached tokens | `last_decision` in `policy.py` | Each policy records what it computed at decision time; the router reads it right after `select()`. The "Why" text is built only from these fields. |
| H | **Derived aggregates** | p50, p95, cache reuse %, load balance, req/s, tokens/s, cold-after-warm-up, prompt tokens computed, prediction error | `compare_logic.js`, `loadtest.js`, `dashboard_api.py` | Computed from the per-request records above. Percentiles use linear interpolation. Browser-side values (the browser's own TTFT and total) are labelled `browser`, everything else `router`. |

A value nobody measured is shown as **n/a** (never 0), with the reason.

---

## Part 2: The header (on every screen)

| Item | Meaning | Source |
|---|---|---|
| **live replicas** (green) or **SIMULATED replicas** (yellow) | Whether the replicas are real or fake (development mode) | `snapshot.mode.simulated` |
| "4 replicas · policy prefix_aware · model Qwen/Qwen2.5-3B-Instruct" | How many replicas the router was started with, the active policy, the model name | router settings |
| **events: live** (green) | The live event stream from the router is connected. "connecting" or "reconnecting" means it is not. | the SSE connection |

---

## Part 3: Playground (one request, fully explained)

**Purpose:** send one request through the real router and see exactly where it went and why.

**Inputs**
| Field | Meaning |
|---|---|
| System prompt (optional) | Sent as the `system` message. A long shared one is what a cache-aware policy can exploit. |
| Prompt | The `user` message |
| max_tokens (64) | Maximum tokens to generate |
| Streaming (on) | Needed for TTFT and ITL. With it off, the router sees the whole body at once, so those show n/a. |
| Advanced: Model name | Filled from the router |
| temperature (0.7), top_p (1) | Sampling settings passed to vLLM |
| SLA ms | Response-time target for this request; blank = router default (3000 ms) |
| Session id | Identifies a conversation (used by swiftserve's session affinity); blank = random |
| Send / Abort in-flight | Abort cancels in the browser; the router sees a disconnect |

**Panels**
- **Response:** the text as it streams.
- **Result** (short view): Served by, Why, TTFT, ITL (mean), Total, Reused from cache ("X of Y prompt tokens"), Router predicted (tokens).
- **All details** (click to expand):
  - *Selected by policy / Dispatched to / Served by*: three different facts. Selected = what the policy chose. Dispatched = every replica an HTTP attempt went to (retries are on the same replica). Served = who produced the response.
  - *Routing decision*: the path (badge) and the Why sentence, plus a **candidate table** with, per replica: Usable, Circuit, Health, In flight, Queue, Est. latency, Capacity (the state the policy saw).
  - *Latency*: TTFT (router) and TTFT (browser view, slightly larger because it includes the browser-to-router hop), ITL, TPOT, Total (router and browser), Upstream time, Received at.
  - *Tokens*: Prompt tokens, Completion tokens, Reused from engine cache, Router's predicted reuse and the prediction error.
- **Requests:** the last 25 requests of this page (State, Served by, TTFT, Total); click to inspect.

**Metrics here:** TTFT/ITL/TPOT/total/upstream (kind A), tokens and cached tokens (B), candidate table values (C, D, E), predicted tokens and Why (G).

---

## Part 4: Load test (many requests, per-request accounting)

**Inputs:** Requests (60), Concurrency (8: real simultaneous fetches), max_tokens (24), Prompt source (*one common prompt* or *list of prompts, cycled*), Prompt, Shared system prompt (optional; "Insert sample ~1500-token system prompt" fills a long one), Advanced (delay between launches, temperature, top_p, streaming, SLA ms, model). Buttons: Start, Stop, Clear results.

**Panels**
| Panel | What it shows | How computed |
|---|---|---|
| **Progress** | launched / succeeded / failed / aborted in browser / router saw disconnect / elapsed / req/s | Browser counters. "Router saw disconnect" counts requests the router recorded as `client_disconnected`. req/s = settled requests ÷ elapsed. |
| **Where the requests went** | Per replica: *selected* (policy chose it) and *served* (it completed), and failed | Tally of the router's events per request |
| **Summary** | TTFT p50, TTFT p95, ITL mean, tokens/s, prompt reused from cache | p50/p95 over completed requests' router-measured TTFT; tokens/s = Σ completion tokens ÷ elapsed; reuse = Σ cached ÷ Σ prompt tokens (usage) |
| **Every request** | #, State, Served by, TTFT, Total, Cached / prompt tokens, Error. "only failures" filter. Export JSON / CSV. | One row per request from the store |

**Stop is honest:** no more requests launch, in-flight ones are aborted in the browser, the router notices the disconnect and records it. Whether vLLM stops generating afterwards is not verified.

---

## Part 5: Compare (the four-policy experiment)

**Inputs:** which policies (run in the order shown, "Reverse the policy order" in Advanced), Requests per policy (60), Concurrency (8), Tenants (6), System prompt tokens (1500), max_tokens (16), Seed (1).

**What it does:** switches the router's policy with `POST /api/policy`, sends the **same seeded workload** through each policy in turn (each request uses one of the tenants' system prompts plus one of 8 questions), gives each policy its own prompt namespace (`[ns:<run>-<policy>]` at the start of every prompt), and the router resets its learned state at every switch. It restores the original policy at the end.

**Result table (per policy)**
| Row | Definition |
|---|---|
| TTFT median / 95th percentile | p50 / p95 of router-measured TTFT over completed requests |
| Prompt reused from cache | Σ cached tokens ÷ Σ prompt tokens (vLLM usage) |
| Load balance (busiest ÷ average) | max requests on any replica ÷ mean over replicas that served ≥1; 1.00 = even |
| ITL mean | mean of per-request ITL |
| Completed / failed | counts |

**"What the cache saved" rows (mechanism metrics)**
| Row | Definition |
|---|---|
| Prompt tokens computed per request | mean of (prompt tokens − cached tokens) |
| TTFT median, requests that hit the cache | median TTFT of requests with ≥ 50% of the prompt cached |
| TTFT median, requests that missed it | median TTFT of requests with < 50% cached |
| Cold requests after each tenant's first | of the requests after a tenant's first, how many had < 50% cached |
| Requests per second (whole run) | completed ÷ wall-clock from first launch to last finish |
| Router's cache prediction, average error | mean \|predicted − actual cached\| in tokens, and share within 16 (one block); only policies that predict |

Also: **bar chart** of median TTFT, a **plain-language verdict** built only from the measured numbers, **Requests per replica** table, **Export JSON**.

---

## Part 6: Live routing

**Live request flow (diagram)**
- **Client / UI** → **Router** (three stages) → **four replica boxes**.
  - *1 · Admission control:* "in flight N / max 256" (the router rejects above 256).
  - *2 · Routing policy:* the active policy, plus its state: for prefix_aware "index: N distinct blocks · tokenizer …"; for swiftserve "message trie: N nodes · session TTL"; and "last: <path> → R<n>" (the last decision), and "SLA default 3000 ms".
  - *3 · Dispatch (proxy.py):* "retries the same replica on connect errors".
- **Replica box:** `R<n> · <label>`, the host, `circuit <state> · queue <n>`, `selected <n> · served <n>`, and a big number = requests running on it right now (from events).
- **Health dot:** green = healthy and circuit not open; red = down (health check failing or circuit open); grey = unknown.
- **Lines:** thickness = that replica's share of all routing decisions since router start.
- **Dots:** each is a real request, moved only when the matching event arrives (received → decision → dispatch → completed; red for failed; flash for retry).

**Recent routing decisions (table):** Time, Request id (`cmp-…` = sent by Compare), **Path** (`prefix_match`, `sla_veto`, `no_eligible_match`, `session_affinity`, `prefix_trie`, `least_loaded`, `round_robin`, `least_connections`, `half_open_probe`), Selected, Dispatched, Served ("–" = still running), and **Why**, text built only from what the policy recorded.

**Timeline and health changes (expandable)**
- *Timeline of recent requests:* one bar per request. Segments: **routing** (received → dispatched), **waiting for first token** (dispatched → first token: queueing + prefill), **streaming** (first token → finished), **failed**, and small marks for connect retries.
- *Replica health changes:* a log such as "R3 · colab-d T4 health up → down" or "circuit closed → open", from `replica.state` events (sampled each second).

---

## Part 7: Dashboard

**Banners:** SIMULATED notice if fake replicas; yellow warnings such as "tokenizer not set".

**Tiles**
| Tile | Meaning |
|---|---|
| completed | Requests finished successfully since router start (all policies) |
| failed | HTTP ≥ 500, connection error, timeout or broken stream since router start |
| in flight now | Sum of in-flight across replicas |
| req/s | Completions over the last 30 s |
| tokens/s | Generated tokens per second over the last 30 s |

**Replica card (one per replica)**
| Field | Meaning | Kind |
|---|---|---|
| `R0 colab-a T4` | Replica id and your label (from `--labels`); the GPU model is shown from the node's own report when available | C / F |
| healthy / unreachable / unknown | Result of the router's last `/health` scrape. **Not** what routing uses: routing uses the circuit breaker. | E |
| circuit badge (only when not closed) | open or half-open | C |
| host | The tunnel hostname | config |
| GPU utilization % and bar | `nvidia-smi` utilization | F |
| VRAM used / total MiB | `nvidia-smi` memory | F |
| °C | `nvidia-smi` temperature | F |
| "GPU: unavailable (hover for reason)" | The sidecar didn't answer. In your screenshot the reason was `/sidecar/gpu answered HTTP 530` (Cloudflare: tunnel not found, i.e. node off) | F |
| in flight | Requests sent there and not finished | C |
| served / failed | Cumulative completed / failed on that replica | C |
| TTFT p50, ITL mean | Median TTFT and mean ITL over that replica's last 500 completed streaming requests | A, H |
| reused from cache | Σ cached ÷ Σ prompt tokens for that replica (all policies) | B, H |

**Router settings and what is measured (expandable)**
- *Policy and router state:* policy, default SLA, model, block size (16), admission (in flight / max 256), and for prefix_aware: tokenizer (`hf:Qwen/...` means the real tokenizer loaded), **index: distinct blocks**, **index cap per replica** (375 with your flag), index blocks per replica, **match thresholds** (≥50% of prompt or ≥256 tokens), hash namespace; for swiftserve: message trie nodes; session-affinity TTL (600 s); events buffered.
- *What is and is not measured:* a table that says for each metric whether it is measured or unavailable and why: GPU utilization / VRAM (from the sidecar), KV-cache usage (vLLM gauge; NOT GPU compute), TTFT (streaming only, router), ITL, token counts (only when the engine reports usage), engine prefix hit rate (`vllm:prefix_cache_hits` / `queries`).

---

## Part 8: What your screenshots show (taken with the nodes off)

- **Dashboard:** all four cards say **unreachable**, 0 requests, GPU unavailable with reason "HTTP 530 | not polled yet". This is expected: you had stopped the Colab nodes to save credits, so the tunnels no longer existed and Cloudflare answered 530.
- **Router settings panel:** `tokenizer hf:Qwen/Qwen2.5-3B-Instruct` (the tokenizer loaded), `index cap per replica 375` (your `--index-max-blocks` flag took effect), `index: distinct blocks 0` (nothing routed yet), `events: 2 buffered, 1 stream open`.
- **Live routing:** all replica dots are red, "circuit closed · queue 0", "last decision: none yet", and the health log shows "R3 colab-d T4 health up → down". **The circuit is closed even though the replica is down** because no request has been sent yet; the circuit counts failures of real requests.
- **Load test:** defaults only, no run yet.
- These screens are valid only for showing the layout; no results are in them.

---

## Part 9: Where each metric appears in the console

Legend: **PG** = Playground, **LT** = Load test, **CMP** = Compare, **LR** = Live routing, **DB** = Dashboard. "JSON" = visible in `/api/dashboard` or an export file, but not drawn on screen.

| Metric | Where it is shown, and under which heading |
|---|---|
| **TTFT** | PG: *Result* → "TTFT", the *Requests* table, *All details* → Latency (router and browser view). LT: *Summary* → "TTFT p50 / p95", *Every request* → "TTFT". CMP: main table "TTFT median" and "TTFT 95th percentile", the median bar chart, and the "TTFT median, requests that hit / missed the cache" rows. LR: the *Timeline* (the "waiting for first token" segment ends at it). DB: replica card "TTFT p50". |
| **ITL (mean)** | PG: *Result* → "ITL (mean)". LT: *Summary* → "ITL mean". CMP: main table "ITL mean". DB: replica card "ITL mean". |
| **TPOT** | PG: *All details* → Latency only. |
| **Total latency** | PG: *Result* → "Total", *Requests* table, *All details*. LT: *Every request* → "Total". LR: *Timeline* (bar length). CMP: only in Export JSON. |
| **Upstream time** | PG: *All details* → Latency only. |
| **TTFB** | Not drawn. Recorded in the router's events (JSON). |
| **p50 / p95** | LT *Summary*, CMP main table, DB (p50 only). |
| **Prompt tokens, completion tokens** | PG: *All details* → Tokens. LT: *Every request* → "Cached / prompt tokens", Export JSON/CSV. CMP: Export JSON. |
| **Cached tokens** | PG: *Result* → "Reused from cache" ("X of Y prompt tokens"). LT: *Every request* → "Cached / prompt tokens". |
| **Prompt reused from cache (%)** | LT: *Summary* → "prompt reused from cache". CMP: main table. DB: replica card "reused from cache". |
| **Prompt tokens computed per request** | CMP: "What the cache saved" section. |
| **TTFT for cache hits / misses** | CMP: "What the cache saved" section. |
| **Cold requests after each tenant's first** | CMP: "What the cache saved" section. |
| **Router's predicted cached tokens** | PG: *Result* → "Router predicted", *All details* → Tokens (with the error). CMP: "Router's cache prediction, average error". LT: Export only. |
| **Load balance (busiest ÷ average)** | CMP: main table and the *Requests per replica* table (raw counts). LT: *Where the requests went* (bars). LR: line thickness (share of decisions since the router started). |
| **Selected / served** | LR: replica box "selected N · served N", and the decision table. LT: *Where the requests went*. DB: card "served". CMP: *Requests per replica*. PG: *Result* → "Served by"; *All details* → Selected / Dispatched / Served. |
| **Failed** | LT: *Progress* → "failed", the *Every request* table. CMP: "Completed / failed". DB: the "failed" tile and card. |
| **In flight** | DB: "in flight now" tile and card "in flight". LR: the big number in each replica box, and Admission "in flight N / max 256". PG: *All details* → candidate table "In flight". |
| **Queue depth** | LR: replica box "queue N". PG: *All details* → candidate table "Queue". |
| **Estimated latency, batch capacity** | PG: *All details* → candidate table "Est. latency" and "Capacity". JSON only elsewhere (`/api/dashboard`). |
| **req/s** | DB: tile (last 30 s). LT: *Progress* → "req/s". CMP: "Requests per second (whole run)". |
| **tokens/s** | DB: tile (last 30 s). LT: *Summary* → "tokens/s". |
| **GPU name, utilisation, VRAM, temperature** | DB: replica cards. (The replica box on LR shows only your label.) |
| **Running / waiting requests, KV-cache usage, engine prefix hit rate** | DB: *Router settings and what is measured* → "What is and is not measured" shows only whether each is measured. The values are in `/api/dashboard` JSON and the router's `/metrics`, not drawn. |
| **Healthy / unreachable** | DB: card badge. LR: replica health dot, and *Replica health changes*. PG: candidate table "Health". |
| **Circuit state** | LR: replica box "circuit …". DB: card badge (shown only when not closed). PG: candidate table "Circuit". |
| **Path and Why** | PG: *Result* → "Why", *All details* → Routing decision. LR: *Recent routing decisions*. LT: Export only. |
| **SLA** | PG: the "SLA ms" field. DB: settings "default SLA". LR: policy box "SLA default". **SLA violated** is only in the router's Prometheus metric, not in the console. |
| **Admission** | LR: box 1. DB: settings "admission". |
| **Policy state** (index blocks, tokenizer, thresholds, namespace, trie nodes, session TTL) | DB: *Router settings*. LR: the policy box (a short version). |
