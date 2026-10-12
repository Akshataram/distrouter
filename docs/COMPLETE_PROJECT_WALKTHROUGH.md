# SwiftServe / distrouter — Complete Project Walkthrough

> Status labels used throughout
> - **Verified (mock/simulated)**: exercised by the automated tests against mocked or fake replicas (`httpx.MockTransport`, `scripts/fake_vllm_stub.py`). Not evidence about real GPUs.
> - **Unverified on real GPU**: implemented, but nobody has observed it against the real Colab T4 nodes yet. Do not claim it works until you have run it and seen it.
> - Any numeric example marked *hypothetical* is invented to teach the arithmetic; it is not a measurement.

## 1. The problem, from first principles

An LLM server (vLLM) turns a prompt into tokens in two phases:

- **Prefill**: read the whole prompt, build the **KV cache** (the attention keys/values per token). Cost grows with prompt length.
- **Decode**: generate one token at a time, reusing the KV cache.

vLLM's **automatic prefix caching** keeps KV blocks (16 tokens each) for finished prompts. If a new prompt starts with the *same* leading full blocks, prefill for those blocks is skipped. It is **prefix-only** (a difference at token 10 invalidates everything after) and **full blocks only**.

With several replicas, each has its **own** cache. A plain load balancer sends a follow-up request to a random replica, which has not seen the prefix, so the cache is wasted. The idea of this project: **the router remembers which replica holds which prefix blocks and sends matching requests there**, unless that replica is overloaded.

Metrics: **TTFT** (time to first token), **ITL** (gap between tokens), **TPOT** (time per output token), goodput (requests meeting an SLA).

## 2. Architecture

```
 Browser (swiftserve/ui)                       your machine
 ┌──────────────┐  POST /v1/chat/completions   ┌──────────────────────────────────────────┐
 │ Playground   │ ───────────────────────────► │ FastAPI router (swiftserve/app.py)        │
 │ Load test    │ ◄─── SSE stream (tokens) ─── │  admission → policy → proxy → replica     │
 │ Routing view │ ◄─── GET /api/events/stream ─│  EventBus ◄─ RequestTrace (telemetry.py)  │
 │ Dashboard    │ ◄─── GET /api/dashboard ──── │  metrics scraper (/health,/metrics,       │
 └──────────────┘                              │                  /sidecar/gpu)            │
                                               └───────┬──────────┬──────────┬────────┬───┘
                                       Cloudflare tunnels (HTTPS, public internet)
                                               ┌───────▼──┐ ┌─────▼────┐ ┌───▼──────┐ ┌▼─────────┐
                                               │ Colab A  │ │ Colab B  │ │ Colab C  │ │ Colab D  │
                                               │ T4       │ │ T4       │ │ T4       │ │ T4       │
                                               │ sidecar  │ │ sidecar  │ │ sidecar  │ │ sidecar  │
                                               │  :9100 → vLLM :8000 (Qwen2.5-3B)   │ │ (same)   │
                                               └──────────┘ └──────────┘ └──────────┘ └──────────┘
```

Each node: vLLM on `:8000` plus the repo's `replica_sidecar` on a port of your choice (9000 in `notebooks/gpu_node.ipynb`, 9100 in your Colab copy); the tunnel points at the sidecar port, and the router never needs to know it, the single port exposed by the tunnel. The sidecar proxies vLLM, adds chaos endpoints (admin token — unrelated to routing) and `GET /sidecar/gpu` (that node's own `nvidia-smi`).

## 3. Repo map (what is new vs. pre-existing)

| Path | Role |
|---|---|
| `swiftserve/app.py` | FastAPI app, chat handler, lifespan (pre-existing; instrumented) |
| `swiftserve/policy.py` | the four routing policies (pre-existing; each now records `last_decision`) |
| `swiftserve/proxy.py` | httpx streaming proxy + same-replica connect retries (pre-existing; instrumented) |
| `swiftserve/state.py` | `ReplicaState`, circuit breaker, EWMA latency, M/M/c model (pre-existing) |
| `swiftserve/metrics_scraper.py` | polls `/health`, `/metrics`, and now `/sidecar/gpu` |
| `swiftserve/replica_sidecar.py` | node-side proxy; new `/sidecar/gpu` |
| `swiftserve/config.py` | env-driven `Settings`; new labels / simulated / buffer sizes |
| `swiftserve/events.py` | **new** `EventBus` (ring buffer, bounded subscriber queues) |
| `swiftserve/telemetry.py` | **new** `RequestTrace`, `StreamTimer`, `Telemetry`, replica-state sampler |
| `swiftserve/dashboard_api.py` | **new** `/api/*` endpoints, SSE stream, static `/ui` |
| `swiftserve/ui/` | **new** dependency-free ES-module frontend |
| `scripts/dev.py` | **new** one-command launcher/validator |
| `scripts/playground.py` | older multi-policy comparison launcher (kept; separate purpose) |
| `tests/…` | new: events, telemetry, observability_router, policy_explanations, dashboard_api, live_router, sidecar_gpu, dev_script, ui, `ui/store.test.mjs`, `e2e/` |

## 4. File-by-file: the parts that matter

**`policy.py`** — every policy picks a replica *and* fills `self.last_decision` (a dict with `path`, and path-specific fields). Selection logic was not changed. The UI only describes a decision using fields the policy recorded (`describeDecision` in `ui/js/util.js`); a policy that predicts nothing yields `predicted_cached_tokens = None`, shown as "n/a", not 0.

**`proxy.py`** — `forward_chat_completion(..., trace)`. `_send_with_retry` retries connect errors on the **same replica only** (no cross-replica failover — a limitation). For streaming, the generator feeds each chunk to `StreamTimer` and handles `CancelledError`/`GeneratorExit` as `trace.client_disconnected`.

**`telemetry.py`** — `RequestTrace` is an explicit per-request object (no context vars), created by `Telemetry.begin`. Its methods emit events and set a terminal state exactly once: `routed, dispatch, retry, upstream_headers, first_token, completed, failed, client_disconnected, rejected`. Every call is wrapped (`_safe`) so telemetry can never break a request. `NULL_TRACE` is the no-op default.

**`events.py`** — `EventBus.emit` never blocks or raises. A slow SSE subscriber has a bounded queue; on overflow it is flagged and the client sees a **gap** event and refetches rather than silently missing data. History is a ring buffer (default 5000, `SWIFTSERVE_EVENT_BUFFER`).

**`dashboard_api.py`** — `build_snapshot` assembles replicas, scraped metrics, policy state, throughput, and a `metric_availability` map saying for each metric whether it is available and, if not, why.

## 5. The routing algorithm (`prefix_aware`) — trace

1. Tokenize the chat (real HF tokenizer if `--tokenizer`; otherwise a fake byte tokenizer whose block boundaries won't match vLLM — the launcher warns).
2. Split tokens into 16-token blocks; compute **chained** blake2b hashes (block *i*'s hash includes block *i-1*'s), so a hash identifies the whole prefix up to it.
3. `PrefixIndex` maps hash → set of replicas believed to hold it (plus a per-replica LRU).
4. Walk the hashes; for each replica find the longest consecutive matched run.
5. A match is **eligible** if matched/total ≥ 0.5 **or** ≥ 256 tokens.
6. Among eligible, pick the longest match; tie-break by shortest queue.
7. **SLA veto**: if the model predicts that replica's latency > SLA, skip it → fall back to least-loaded.
8. **Optimistic insert**: the prompt's blocks are added to the chosen replica's index *before* the response (so concurrent same-prefix requests co-locate). Known cost: if the request then fails, the index keeps a stale entry (no undo — limitation).

*Hypothetical example*: 4 replicas, prompt = 600 tokens → 37 full blocks. Replica B's index holds the first 30 blocks → match 480 tokens, ratio 0.81 → eligible. Replica B has 3 in flight, predicted latency under SLA → choose B, `predicted_cached_tokens = 480`. If B had been vetoed, the decision path would be `sla_veto` and the UI would say so.

Decision paths the UI can show: `round_robin`, `least_connections`, `session_affinity`, `prefix_trie`, `least_loaded` (swiftserve), `prefix_match`, `no_eligible_match`, `sla_veto` (prefix_aware), and `half_open_probe` (router override for circuit-breaker probes).

## 6. State management

- **Router, authoritative**: `ReplicaState` per replica — in-flight count, EWMA latency buckets, circuit breaker (closed/open/half-open; opens after 5 consecutive failures, cooldown 10 s doubling to 120 s), scraped metrics. The router routes on **circuit state**, not on the scraped `healthy` flag (limitation).
- **Admission control**: max 256 concurrent; beyond that → rejected (503) and traced as `request.rejected`.
- **Browser**: `ui/js/store.js` merges two evidence streams per request id: `server` (router events) and `client` (what this page measured). They are never blended into one number.
- **Prefix index**: in router memory only; lost on restart.

## 7. Measuring TTFT / ITL / TPOT

`StreamTimer` incrementally parses the SSE the router relays.
- **TTFB**: first byte of the response from the replica.
- **TTFT**: request received → first event whose `delta.content` is non-empty (the role-only first chunk doesn't count).
- **ITL (mean)** = (t_last_content − t_first_content) / (n_content_events − 1). Mean over the span is used because tunnels coalesce chunks, making per-gap values unreliable.
- **TPOT** = same span divided by the engine-reported `completion_tokens − 1`.
- Non-streaming requests: no TTFT/ITL (shown "n/a" with the reason).

*Hypothetical timeline*: received t=0; first byte 180 ms; first content 210 ms (TTFT=210); last content 2 210 ms with 41 content events → ITL mean = 2000/40 = 50 ms.

`cached_tokens` comes from vLLM's `usage.prompt_tokens_details`, which exists only if vLLM was started with `--enable-prompt-tokens-details`; otherwise shown "not reported".

## 8. Request lifecycle

```
Browser           Router                       Replica (via tunnel)
  │ POST (X-Request-Id: r1)                          │
  │────────────►  telemetry.begin → request.received │
  │               admission check                    │
  │               candidates snapshot                │
  │               policy.select → routing.decision   │
  │               dispatch (attempt 1) ─────────────►│
  │               [connect error → dispatch.retry ─►│ same replica]
  │               upstream.headers ◄────────────────│
  │◄─ stream ──── first_token (TTFT) ◄─ first content
  │◄─ stream ──── …                                  │
  │               request.completed (served=replica)│
```
Concurrent requests are independent traces keyed by request id; they interleave on the event stream. Failure branches: `request.rejected` (admission / all circuits open / invalid body), `request.failed` (stage names where it broke), `request.client_disconnected` (browser aborted a stream).

**Three distinct replica notions** are never conflated: *selected* (policy choice), *dispatched* (every replica an HTTP attempt went to), *served* (replica that produced the response).

## 9. UI → backend data flow

- Playground / load test call `POST /v1/chat/completions` with a caller-chosen `X-Request-Id`.
- Everything the routing view and request tables show arrives via `GET /api/events/stream` (SSE, resumable with `Last-Event-ID`; each connection capped at 30 s and the UI reconnects, so server shutdown never hangs).
- `GET /api/dashboard` (polled), `GET /api/requests[/id]` (seed and detail).
- Stop in the load test aborts in the **browser** (`AbortController`); the router then records `client_disconnected` for streams. Non-streaming requests are not cancelled upstream. Whether vLLM stops generating after a disconnect is **unverified**.
- Auth: if `SWIFTSERVE_API_TOKEN` is set, `/api/*` and chat need a bearer token; the UI shell itself loads without it and asks for the token (kept in memory, never in the page source).

## 10. Running it against your real cluster (4 Colab T4 + Cloudflare)

```bash
pip3 install -r requirements.txt transformers
python3 scripts/dev.py --replicas "<urlA>,<urlB>,<urlC>,<urlD>" --policy prefix_aware \
  --model Qwen/Qwen2.5-3B-Instruct --tokenizer Qwen/Qwen2.5-3B-Instruct \
  --max-num-seqs 4 --labels "colab-a T4,colab-b T4,colab-c T4,colab-d T4"
```
Open `http://127.0.0.1:8000/ui/`. Notes:
- `scripts/dev.py` validates first: Cloudflare 530 ⇒ tunnel is gone (quick-tunnel URLs change on restart); 502/503/504 ⇒ tunnel up, nothing behind it; wrong `--model` ⇒ caught before your first request.
- GPU identity: only from `--labels` (what *you* assert) and the node's own `nvidia-smi` via `/sidecar/gpu`. The router never infers a GPU from replica number. Nodes cloned before this change return 404 there and the console says "unavailable".
- KV-cache usage is **cache occupancy**, not GPU utilisation; CPU is never shown as GPU.
- The nodes' ADMIN_TOKEN is only for chaos endpoints, not routing.
- `--simulate` exists for console development only and labels everything SIMULATED.

### Comparing the four policies in the console (Compare tab)

`POST /api/policy {"name": ...}` replaces the router's policy with a fresh instance (empty prefix index or trie) and clears what earlier traffic taught the router about each replica (latency average, latency-by-load table, highest concurrency seen, session table; the circuit breaker is untouched), so every policy starts from the same knowledge. It is
disabled unless `SWIFTSERVE_ALLOW_POLICY_SWITCH=1` (set by `scripts/dev.py` on a loopback bind), returns 409 while requests
are in flight, and only builds one of the existing policies. The Compare tab (`ui/js/compare.js`, pure logic in
`compare_logic.js`) uses it to run, for each policy in turn:

1. the **same seeded request sequence** (tenant and question drawn from a fixed PRNG) over N tenants, each with its own
   ~1,500-token system prompt;
2. a **separate namespace per policy** (`[ns:<run>-<policy>]` at the very front of every system prompt, which changes every
   chained block hash), so no policy reuses blocks another policy warmed on the replicas;
3. a summary computed from the router's own measurements: TTFT median and 95th percentile, cache reuse (engine-reported cached
   tokens over prompt tokens), ITL, and load balance (busiest replica over the average).

Why the workload is built this way: with more tenants than one replica can cache (about 6,000 tokens of KV cache per node here),
a policy that spreads every tenant across every replica makes the caches evict each other, whereas a policy that keeps a tenant on
the replica that already holds it can keep its blocks resident. Whether `prefix_aware` actually wins on your cluster is what the
table measures; it is not assumed. Policies run sequentially, so this is a controlled demonstration, not a publishable benchmark.

## 11. Debugging guide

| Symptom | Likely cause | Where to look |
|---|---|---|
| `dev.py` says 530 | tunnel URL expired | re-run Colab cell, copy new URL |
| 404 model not found | `--model` ≠ vLLM `--served-model-name` | `/v1/models`, launcher validation |
| predictions always wrong | tokenizer/block-size mismatch | router startup warning; `--block-size` |
| `cached_tokens` n/a | vLLM lacks `--enable-prompt-tokens-details` | node launch cell |
| GPU rows unavailable | node lacks `/sidecar/gpu` | re-clone repo on node |
| replica never chosen | circuit open or SLA veto | Routing → decision log, candidate table |
| UI stuck "connecting" | router down / token needed | `dev_logs/router.log` |

## 12. Testing — what was actually run

Latest local run: **357 passed, 1 skipped; ruff and mypy clean.** All of it uses mocked or simulated replicas:
- Mocked upstreams (`httpx.MockTransport`): event ordering, retries, circuit behaviour, attribution, header de-duplication.
- Real uvicorn + fake replicas (`tests/live_server.py`, `fake_vllm_stub.py`): SSE streaming, ITL/TTFT plausibility, graceful shutdown, client-disconnect accounting (no leaked `in_flight`).
- Node unit tests for the UI store; Playwright browser e2e (gated by `SWIFTSERVE_E2E=1`) was run once against the simulated stack and passed.

**Observed on a real node (replica-a, Tesla T4 15 GB, vLLM Qwen2.5-3B-Instruct, `--enable-prefix-caching --enable-prompt-tokens-details --block-size 16 --num-gpu-blocks-override 375`)** — from the node notebook's own output, calling the node's sidecar *directly*, not through the router:

| call | TTFT | prompt tokens | cached tokens |
|---|---|---|---|
| 1st (cold prefix) | 2061 ms | 1617 | 0 |
| 2nd (same system prompt) | 74 ms | 1618 | 1600 |
| 3rd (same system prompt) | 73 ms | 1617 | 1600 |

This confirms on real hardware that vLLM's prefix cache works, that `cached_tokens` is reported (1600 = 100 full 16-token blocks), and that vLLM reported a 6 000-token KV cache (375 × 16). It says nothing about the router: **routing across the four nodes through the router has not yet been observed.** Only replica-a's output has been seen; the other three nodes and the tunnels have not been tested from the router.

Also note the capacity consequence: with a 6 000-token KV cache and ~1.6k-token prompts, only about 3 such requests fit concurrently even though `--max-num-seqs` is 32. For the router's `--max-num-seqs` (used for its latency model) a value near 4 is more honest than 32 for long-prompt tests; pick it deliberately and note it.

**Observed through the router (policy `prefix_aware`, four Colab T4 nodes via Cloudflare tunnels, Playground, short ~25-token prompt):**
- Dashboard: all 4 replicas healthy, circuit closed; each card showed "Tesla T4 (reported by the node)", VRAM 6,727 / 15,360 MiB, 45-69 °C, GPU utilization 0% while idle (from each node's own `nvidia-smi` via `/sidecar/gpu`).
- Three sequential requests were all selected and served by replica R0 (colab-a); the `X-SwiftServe-Replica` header agreed with the router's event stream.
- Request detail: decision `prefix_match` ("R0 holds 1/1 prompt blocks (16 tokens)"); router TTFT 752 ms (browser 782 ms); ITL = TPOT = 28.4 ms; completion tokens 24; **router-predicted reuse 16 tokens vs engine-reported `cached_tokens` 16 (prediction error 0)**.
- The TTFT drop across those three requests (1993 → 972 → 752 ms) is **not** attributed to caching: with a 25-token prompt only one block exists to cache; first-request/tunnel warm-up is the likelier cause.
- **Load test, 60 requests, concurrency 8, `prefix_aware`, one identical 40-token prompt (no long shared system prompt), streaming, `max_tokens` 24:** 60/60 succeeded, 0 failed/aborted, 25.9 s elapsed (2.3 req/s). Router-reported distribution: R0 18, R1 12, R2 22, R3 8 (selected = served for every request, 60 total). TTFT p50 374 ms / p95 4944 ms; total p50 1074 ms / p95 5714 ms; ITL mean 34.2 ms (28.4 ms for the single unloaded request); 1,400 completion tokens, 54 tok/s engine-reported. The rows inspected (#49-#60) each show prompt 40 / completion ~24 / cached 32 (two full 16-token blocks). Caveat: with a 40-token prompt there is almost nothing to cache, so this shows the router spreading real load across all four GPUs correctly, **not** a cache speed-up and not a comparison with any baseline policy. Why traffic spread under `prefix_aware` (load/SLA fallback vs. other cause) was not yet inspected per request; the p95 TTFT tail was not yet traced to specific requests.
- **Compare tab, real run on four Colab T4 nodes (one run, seed 1, 60 requests per policy, 6 tenants with ~1,500-token system prompts, concurrency 8, each policy in its own prompt namespace, policies run one after another; values as displayed):**

  | | round_robin | least_connections | swiftserve | prefix_aware |
  |---|---|---|---|---|
  | TTFT median | 1033 ms | 1063 ms | 537 ms | **464 ms** |
  | TTFT 95th percentile | 1887 ms | 1921 ms | **1690 ms** | 2671 ms |
  | Prompt reused from cache | 55% | 48% | 69% | **87%** |
  | Load balance (busiest ÷ average) | **1.00** | 1.53 | 1.33 | 1.47 |
  | ITL mean | 41.0 ms | 37.1 ms | 53.7 ms | 43.3 ms |
  | Requests per replica R0/R1/R2/R3 | 15/15/15/15 | 23/12/16/9 | 12/13/15/20 | 22/12/19/7 |

  All 240 requests completed. `prefix_aware` had the lowest median TTFT (about 2.2-2.3x lower than the two load-based baselines) and the highest cache reuse; 87% is close to the ceiling for this workload (each tenant's prompt must miss once, about 90% with 6 tenants over 60 requests; this ceiling is a calculation, not a measurement). **Its 95th-percentile TTFT was the worst of the four**; it also placed 22 requests on one replica and 7 on another, which is consistent with queueing on the busy replica but has not been confirmed per request. `swiftserve` was a close second on median and cache reuse; why was not investigated. The run is a single seed in a fixed policy order on shared Colab GPUs, so it does not yet establish that `prefix_aware` is better overall; the next check is a repeat with another seed and the order reversed, and the per-request export (Compare tab, "Export JSON") to trace the tail.
- Still **not** observed: the effect of a long shared system prompt through the router, traffic spreading across R1-R3 under concurrency, and any comparison against the baseline policies.

The node was cloned at commit `32d1160` (before this console existed), so it has no `/sidecar/gpu` yet; GPU rows will say "unavailable" until the node's repo is updated.

## 13. Limitations

**Implemented and verified (mock/simulated)**: event bus and gap handling; request traces and attribution; TTFT/ITL/TPOT computation; policy decision explanations; dashboard API; SSE resume; load generator; dev launcher validation; graceful shutdown; client-disconnect accounting.

**Implemented, unverified on real GPU**: routing across your four Colab T4s through Cloudflare; real `cached_tokens` vs. prediction; `/sidecar/gpu` readings; ITL through the tunnels; effect of tunnel latency on SLA veto; prefix-cache hit gains on real vLLM.

**Partial**: GPU metrics (only if the node exposes the sidecar endpoint); `replica.state` is a 1 s sampler, not an event for every transition; cancel semantics (browser-side only).

**Not implemented**: cross-replica failover on connect failure; undo of optimistic index insert on failure; routing on scraped `healthy`; semantic (meaning-based) matching; persistence of events/prefix index across restarts; upstream cancellation of non-streaming requests; per-token ITL distributions (only the mean is trustworthy through tunnels). Also: `x-swiftserve-cache-hit` header is always "true" for `prefix_aware` (the UI ignores it); index cap (20 000 blocks) is far above the engine's real capacity (~375 blocks), so the index can over-claim.
