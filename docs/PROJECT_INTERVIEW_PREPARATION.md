# SwiftServe / distrouter — Interview Preparation

Read `docs/COMPLETE_PROJECT_WALKTHROUGH.md` first. Rule for every answer: say what you **built**, what you **measured**, and what you have **not** verified. Do not claim more than the evidence below.

## What you can honestly claim

- **Built**: a FastAPI router in front of vLLM replicas with four policies (round_robin, least_connections, swiftserve, prefix_aware); block-level chained-hash prefix index; M/M/c latency model; circuit breaker; admission control; Prometheus metrics; and an observability layer (event bus, request traces, TTFT/ITL measurement) with a web console.
- **Observed on real hardware**: on one Colab Tesla T4 node running vLLM + Qwen2.5-3B-Instruct, a cold ~1.6k-token prompt had TTFT 2061 ms; the same system prompt warm had TTFT ~74 ms with `cached_tokens` = 1600. That is vLLM's prefix cache, called directly, not your router.
- **Tested with mocks/fake replicas**: the router's routing, retries, circuit behaviour, event ordering, telemetry, UI store, graceful shutdown (357 tests).
- **Not yet observed**: routing across your four real nodes via Cloudflare tunnels; prefix_aware beating the baselines on real GPUs through the router. Say "I built the instrument to measure it and ran the node-level check; the cross-node router run is the next step" unless you've since run it.

## 60-second explanation

"vLLM caches the KV state of prompt prefixes per replica, but a normal load balancer ignores that, so repeated prefixes miss the cache. I built a router that hashes each prompt into 16-token blocks, remembers which replica holds which blocks, and sends a request to the replica with the longest cached prefix — unless that replica is too loaded, in which case an SLA check falls back to the least-loaded one. I compared it with round-robin, least-connections and an earlier trie-based policy, and I built a live console that shows, per request, which replica was selected, dispatched to and served it, plus TTFT and inter-token latency."

## 3-minute explanation

Add to the above:
1. **Why prefix-only and block-aligned**: vLLM's automatic prefix caching only reuses leading full blocks; a router that predicts reuse must use the same boundaries, hence the same tokenizer and `--block-size 16`.
2. **Chained hashing**: each block's hash includes the previous hash, so one hash identifies an entire prefix; the index is hash → set of replicas, with per-replica LRU to bound memory.
3. **Eligibility and veto**: a match counts if ≥50% of the prompt blocks or ≥256 tokens match; an M/M/c latency estimate vetoes a warm replica that would blow the SLA.
4. **Optimistic insert**: concurrent same-prefix requests should co-locate, so blocks are indexed at routing time, before the replica confirms. Cost: failures leave stale entries.
5. **Observability**: every request gets an id and a trace object; the router emits structured events (received, decision, dispatch, retry, first_token, completed/failed/disconnected) to an in-process bus streamed over SSE. The UI joins them with browser-side timings.

## 5-minute explanation

Add: the three replica notions (selected/dispatched/served) and why they differ (half-open probes, retries); the TTFT/ITL definitions (TTFT = first non-empty `delta.content`; ITL reported as a mean because tunnels coalesce chunks); the honesty rules (unavailable metrics are labelled with a reason, never zero; GPU identity comes only from operator labels or the node's own `nvidia-smi`; KV-cache usage is occupancy, not utilisation); the testing strategy (mocked upstreams, a live uvicorn server against fake replicas, Node store tests, a gated Playwright e2e) and its limits; the limitations list.

## Q&A

**Why not just least-connections?** It ignores cache state. With long shared system prompts (≈1600 tokens here), a warm replica prefills in ~70 ms vs ~2 s cold, so locality matters. The earlier swiftserve trie only matches at message depth ≥2, so on single-turn shared-system-prompt traffic it ties the baselines.

**What is prefill vs decode, and what does the cache save?** Prefill processes the whole prompt (compute-bound); decode generates tokens one by one (memory-bound). The prefix cache skips prefill for matched blocks, so it improves TTFT, not per-token latency.

**Why is prediction ever wrong?** The router's index is a belief: the engine may have evicted blocks (375-block cache here), the index cap (20 000) far exceeds that, optimistic inserts can record a failed request, and a tokenizer/block-size mismatch shifts block boundaries. The router logs predicted vs `cached_tokens` per request so you can measure the error.

**How do you measure ITL honestly?** Mean = (last − first content time)/(events − 1) at the router. Through Cloudflare, events arrive coalesced, so per-gap values are unreliable; the mean across the span is not. TPOT uses the engine's completion token count.

**What happens when a replica dies?** Connect errors retry on the *same* replica (not a different one — a known gap); consecutive failures open the circuit (5 failures, 10 s cooldown doubling to 120 s); a half-open probe re-tests. The trace records dispatch attempts and the final failure stage.

**What happens when the client disconnects?** Streaming: the router sees cancellation and records `client_disconnected`; in-flight and admission counters are released (tested). Whether vLLM stops generating is not verified. Non-streaming requests aren't cancelled.

**Is concurrency safe?** One asyncio event loop; policy selection is synchronous (no awaits inside), so there's no race between choosing and updating counters. Event bus emits never block; slow SSE consumers get a gap marker rather than back-pressuring requests.

**Why SSE and not WebSockets?** One-directional, auto-reconnect with `Last-Event-ID`, works through proxies. Connections are capped at 30 s so graceful shutdown can't hang.

**How do you know the GPU a request ran on?** Only from what you declare (`--labels`) and, if the node exposes it, that node's own `nvidia-smi` via `/sidecar/gpu`. The router never infers GPU from replica number.

**Why a mock-heavy test suite — is that valid?** It proves router logic (ordering, accounting, attribution). It cannot prove GPU performance; that is why the walkthrough separates "verified (mock)" from "unverified on real GPU".

**Biggest weakness?** Cross-node results through the router are not yet measured; no failover across replicas; the prefix index is in memory, lost on restart, and not synchronized with actual engine eviction.

## Hard follow-ups

- *"Could you make the router know the real cache contents?"* Poll or subscribe to engine KV events (vLLM can emit them) instead of guessing; this removes optimistic-insert drift. Not implemented.
- *"Semantic matching?"* Doesn't help KV reuse: attention caches are exact token prefixes. A semantic cache would be a separate response cache with different correctness guarantees.
- *"Scale to many routers?"* The index and counters are per-process; you'd need a shared index or consistent hashing on a prefix key.
- *"Why M/M/c?"* A cheap queueing estimate to avoid overloading a warm replica; its accuracy for LLM service times is limited, and the capacity floor defaults to 1 unless you pass `--max-num-seqs`.

## Live demo script (≈5 min)

1. Start the four Colab nodes; copy the four URLs. Run `scripts/dev.py` with `--replicas …` (see README). Show the startup validation output.
2. Open `/ui/` → **Dashboard**: show four replicas, labels (your own), health; point out unavailable GPU metrics and their reasons if the nodes lack `/sidecar/gpu`.
3. **Playground**: send one request with a long system prompt; show Selected/Dispatched/Served, TTFT, `cached_tokens` (0 first time).
4. Send a second request with the same system prompt: show it routes to the same replica and `cached_tokens` > 0, with the decision text "matched N blocks".
5. **Load test**: 40 requests, concurrency 8, common prefix; watch the **Routing** tab and the per-replica tally. Click a request to show its trace.
6. Open **Compare**, leave the defaults (60 requests, concurrency 8, 6 tenants, 1,500-token system prompts) and press Run. It runs all four policies on the same requests, each in its own prompt namespace, and fills one table. Say only what the table shows; if `prefix_aware` is not best on some row, say so and explain it from the routing decisions.
7. Stop a node deliberately and show retry/circuit events (only if you've rehearsed it).

## Files and functions to study first

1. `swiftserve/policy.py` — `PrefixAwarePolicy.select`, `PrefixIndex`, chained block hashing.
2. `swiftserve/app.py` — the chat handler: admission → candidates → policy → trace → proxy.
3. `swiftserve/proxy.py` — `forward_chat_completion`, `_send_with_retry`, the streaming generator.
4. `swiftserve/state.py` — circuit breaker, EWMA, M/M/c.
5. `swiftserve/telemetry.py` — `StreamTimer`, `RequestTrace`.
6. `swiftserve/events.py` — `EventBus`, overflow/gap behaviour.
7. `swiftserve/dashboard_api.py` — snapshot and SSE endpoint.
8. `swiftserve/ui/js/store.js` — how server and browser evidence are merged.
9. `scripts/dev.py` — validation (`classify_probe`).
10. `tests/test_observability_router.py`, `tests/test_live_router.py` — what the guarantees actually are.
