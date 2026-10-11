# SwiftServe: complete walkthrough for presenting to a teacher

Every number below was computed from this repository's code, except where it
is marked "your run" (from your real 4×T4 output) or "simulated".

---

## 0. Status — read this first

| Claim | Status |
|---|---|
| The mechanism (block-level prefix routing) is implemented | **Yes**, 206 tests pass |
| It works in a simulator that implements block caching | **Yes**: 77.3% cache reuse vs 52.9% for the baselines |
| It has been shown to beat the baselines on the real 4×T4 cluster | **No, not yet** |
| The simulator agrees with the real hardware on the baselines | **Yes**: round-robin sim 52.9%, real 52.6 / 52.3 / 52.3% |
| The real `prefix_aware` run exercised a working index | **No**: prediction error (48.4%) ≈ cache ratio (48.1%) means the index matched nothing |

Do not say "we beat the baselines on real GPUs" until a real run shows
`prediction error` far below `cache ratio` for `prefix_aware`.

---

## 1. The idea from zero

A language model request has two phases.

- **Prefill.** The GPU reads the *entire prompt* and computes, for every token,
  the attention keys and values it will need later. Cost grows with prompt
  length. Our prompts are about 2000 tokens.
- **Decode.** The GPU produces the answer one token at a time. Cost depends on
  the *answer* length (we cap it at 16 tokens), not the prompt.

The keys and values from prefill are stored in GPU memory, the **KV cache**. If
a later request starts with *exactly the same tokens*, the GPU can reuse the
stored part and prefill only the new tail. This is **prefix caching**, and
vLLM does it automatically when you pass `--enable-prefix-caching`.

Now use four GPUs. Whether request #7 gets a cache hit depends entirely on
**which GPU it lands on**. Land on a GPU that served the same system prompt
recently and you skip about 2000 tokens of prefill. Land elsewhere and you pay
for it again.

**The project is the router that picks the GPU.** It never stores a cache and
never touches the GPU. It keeps a *belief* about where each prefix lives and
routes on that belief.

### Why the metric is TTFT and not total latency

A cache hit saves prefill time and nothing else. Total latency is
`prefill + decode + network`, so the saving is buried. **TTFT** (time to first
token) ends right after prefill, so it shows the effect directly. This is also
why the original benchmark showed a tie: it measured total latency on tiny
prompts, where there was nothing to cache and nothing to see.

---

## 2. The machines and processes

```
YOUR MACBOOK                                  4 × COLAB (one account each)
┌──────────────────────────────────┐          ┌─────────────────────────────┐
│ python3 scripts/demo.py          │          │ cloudflared  (public URL)   │
│   │ spawns, one policy at a time │  HTTPS   │      │                      │
│   ▼                              │ tunnels  │      ▼                      │
│ ROUTER (uvicorn swiftserve.app)  │ ───────▶ │ sidecar :9000  (pass-through)│
│   CPU only, port chosen randomly │          │      │                      │
│   no model, no GPU, no cache     │          │      ▼                      │
└──────────────────────────────────┘          │ vLLM :8000 → Qwen2.5-3B     │
                                              │   owns the real KV cache    │
                                              └─────────────────────────────┘
```

| Process | Machine | Job |
|---|---|---|
| `scripts/demo.py` | laptop | Orchestrator and load generator. Generates the workload, starts a router per policy, fires requests, computes metrics. |
| router (`swiftserve/app.py`) | laptop | **What we built.** Receives each request, picks a replica, forwards it, streams the answer back. |
| cloudflared | each Colab | Exposes the node's port as a public `trycloudflare.com` URL. |
| sidecar (`swiftserve/replica_sidecar.py`) | each Colab | Transparent proxy to vLLM, plus `/chaos/*` fault-injection endpoints. |
| vLLM | each Colab | The real inference engine. Unmodified. Owns the KV cache. |

Consequence for how you read results: **TTFT includes the WAN round trip**
laptop → Cloudflare → Colab and back. That is why even a perfect cache hit
shows hundreds of milliseconds in your runs.

---

## 3. The vLLM side

### Launch flags

```
--model Qwen/Qwen2.5-3B-Instruct
--enable-prefix-caching          turn the cache on
--enable-prompt-tokens-details   make vLLM REPORT usage.prompt_tokens_details.cached_tokens
--block-size 16                  cache unit = 16 tokens
--num-gpu-blocks-override 375    cap the cache at 375 blocks = 6000 tokens
--max-model-len 4096             longest allowed prompt+output
--max-num-seqs 32                max concurrent sequences
--dtype half                     fp16
--gpu-memory-utilization 0.90
```

| Flag | Why it matters |
|---|---|
| `--enable-prompt-tokens-details` | Without it vLLM never tells you `cached_tokens`, so your cache column reads 0% regardless of what happens. |
| `--num-gpu-blocks-override 375` | A T4 at 0.90 utilization would hold about 100 distinct 2000-token prompts. We use 5–6. Nothing would ever be evicted and every policy would tie. Capping the cache forces real misses. |
| `--max-model-len 4096` | **Must be ≤ blocks × 16.** `375 × 16 = 6000`. With `8192` vLLM refuses to start because the cache cannot hold one maximum-length sequence. This is exactly what crashed your first launch. |
| `--block-size 16` | Must equal the router's `SWIFTSERVE_BLOCK_SIZE`, or no router hash ever corresponds to anything in the engine. |

### What vLLM does with `[system, user]`

1. **Chat template.** The messages are flattened into one string:
   `<|im_start|>system\n{system}<|im_end|>\n<|im_start|>user\n{user}<|im_end|>\n<|im_start|>assistant\n`.
   Message boundaries no longer exist.
2. **Tokenize.** About 2060 integers for our request.
3. **Cut into blocks of 16.** 2060 / 16 = 128 full blocks, and the 12 leftover
   tokens are dropped, because a partial block is never cached.
4. **Chain-hash.** `h₀ = H(salt, block₀)`, `h₁ = H(h₀, block₁)`, and so on. Each
   hash contains its parent, so one hash identifies the whole prefix from
   token zero.
5. **Look up** each hash in the cache, **stopping at the first miss**.
6. **Prefill only the rest**, then decode.

Two properties drive the entire design:

- **Prefix-only.** Matching starts at token 0 and stops at the first mismatch.
  Change anything early and every later block misses. Change something late
  and the earlier blocks still hit.
- **Block-granular.** Overlap is counted in whole blocks.

Worked example from our workload. Two sessions from the same app share the
same 8000-character system prompt, then have different user messages.
About 125 of 128 blocks are identical, and the last few differ where the user
text begins. A cache hit reuses about 2000 tokens and prefills about 60.

---

## 4. The router, in the order a request travels

File: `swiftserve/app.py`, function `chat_completions`.

| # | Step | Code idea |
|---|---|---|
| 1 | Read body, parse JSON, validate shape | reject malformed with 400 |
| 2 | Read `X-SLA-Ms` header, default **3000 ms** | the demo client sends none, so every request uses 3000 |
| 3 | **Admission control** | `admission.try_acquire()`; over 256 in flight → 503 |
| 4 | Pick the session id | `X-Session-Id` header, else `user` field, else a random uuid |
| 5 | **Filter replicas** | drop any whose circuit breaker is open |
| 6 | Half-open probes get priority | a recovering replica gets the next request so it can prove itself |
| 7 | **Ask the policy** | `policy.select_with_prediction(session_id, sla_ms, available, messages)` |
| 8 | Bookkeeping | `touch_session`, `circuit.mark_dispatched`, `in_flight += 1` |
| 9 | **Forward** | `forward_chat_completion(...)` in `proxy.py` |
| 10 | Add response headers | `X-SwiftServe-Replica`, `-Cache-Hit`, `-Predicted-Cached-Tokens` |
| 11 | On stream end | record latency EWMA, circuit success/failure, Prometheus metrics, release the slot |

### Step 9 in detail (`proxy.py`)

The raw bytes of the original request are forwarded unchanged (so fields vLLM
understands but the router doesn't are never dropped), with an `X-Request-Id`
added. The response is **streamed**: `upstream_response.aiter_bytes()` yields
each chunk straight to the client, and the replica's elapsed time is recorded
only after the stream finishes, so latency is true end-to-end. Failed sends are
retried up to 2 times with backoff.

### The load signals (`state.py`)

- `in_flight` — requests this router has sent and not finished. Never stale.
- `queue_depth()` — `max(scraped running + waiting, in_flight)` if the last
  scrape is under 5 s old, else `in_flight`.
- A background `scrape_loop` polls each replica's `/health` and `/metrics`
  (every 1 s in the demo) to read `vllm:num_requests_running`,
  `vllm:num_requests_waiting`, `vllm:gpu_cache_usage_perc`, and
  `vllm:prefix_cache_hits/queries`.
- `estimate_latency_ms()` — an M/M/c queue estimate. Below capacity it returns
  the EWMA latency for that occupancy; at or above capacity it returns
  `service_time × (depth + 1) / capacity`. Capacity is the highest concurrency
  ever seen via scraping, with a configured floor that defaults to **1**.

---

## 5. The four policies (`swiftserve/policy.py`)

| Policy | Rule | Cache-aware |
|---|---|---|
| `round_robin` | replica `i mod 4`, in turn | no |
| `least_connections` | smallest `queue_depth()` | no |
| `swiftserve` | this session's previous replica, else a *message-level* trie match, else cheapest | partly |
| `prefix_aware` | the replica holding the most *token blocks* of this prompt | **yes** |

### `prefix_aware`, step by step

```python
hashes  = block_hashes(tokenizer.encode_chat(messages), block_size, namespace=model)
matches = index.match(hashes)              # {replica_id: leading blocks it holds}
```

1. **Eligible** if `matched_blocks / total_blocks ≥ 0.5` **or**
   `matched_blocks × 16 ≥ 256` tokens. Two conditions because either alone is
   wrong (a 2000-token prompt under a 3000-token question has ratio 0.4 but is
   clearly worth routing for).
2. Among eligible replicas, take the **longest match**; ties go to the
   **shallower queue**.
3. **SLA check.** If that replica's `estimate_latency_ms() > sla_ms`, abandon
   the match.
4. Otherwise (or if nothing is eligible) fall back to
   `min(queue_depth, estimate_latency_ms, replica_id)`.
5. `predicted_cached_tokens = matches.get(chosen, 0) × 16`, taken **before**
   step 6 so it cannot flatter itself.
6. **Optimistic insert**: `index.insert(chosen, hashes)`. This records "I sent
   it there, so it is probably warm now". A burst of requests sharing a prefix
   therefore follows the first one.

### The index (`swiftserve/prefix_index.py`)

```python
_holders: dict[bytes, set[int]]          # block hash -> replicas believed to hold it
_lru:     dict[int, OrderedDict]         # per replica, imitates vLLM eviction
match(hashes):                           # walk from block 0
    alive = replicas holding block 0
    for each next hash: alive &= holders(hash); stop when alive is empty
    # a replica drops out at its first missing block and never rejoins
```

Small worked example. Replica 0 holds blocks `[a,b,c,d]`, replica 2 holds
`[a,b]`. A request hashes to `[a,b,c,x]`. The walk: block `a` → {0,2};
`b` → {0,2}; `c` → {0}; `x` → nobody. Result `{0: 3, 2: 2}`. Replica 0 wins
with 3 blocks = 48 tokens reusable.

**It is a belief, not the truth.** The router never sees the real cache. The
gap between its belief and vLLM's report is the project's measured error.

---

## 6. The tokenizer, and the bug that hid everything

The router needs the same token sequence vLLM sees, so
`swiftserve/tokenization.py` calls `AutoTokenizer.apply_chat_template(...)` for
the real model.

**The bug.** Newer `transformers` returns a dictionary-like `BatchEncoding`
from that call. The old code did `list(...)` on it, which yields its *keys*:
`['input_ids', 'attention_mask']`. A 2000-token prompt became two "tokens".
Two is below the block size of 16, so `block_hashes` returned **zero blocks**,
the index matched nothing, every prediction was 0, and `prefix_aware`
silently behaved like a load balancer while reporting 24/24 successes.

**The fix** (commit `418bdd8`): pass `return_dict=False`; normalize the three
real return shapes and raise on anything else; and the router now runs a
**startup probe** that tokenizes a ~400-word prompt and refuses to start if
it gets fewer than 2 blocks. When healthy it logs:

```
prefix index live: tokenizer=hf:Qwen/Qwen2.5-3B-Instruct, probe produced N tokens = M blocks of 16
```

---

## 7. The experiment: how "24 sessions" actually runs

### 7.1 The workload (`scripts/workloads.py`, `shared_system_prompt`)

Parameters used: `num_apps=6, zipf_s=1.1, system_prompt_tokens=2000,
sessions=24, turns=1, max_tokens=16, seed=1, ttft_slo_ms=500, tpot_slo_ms=50`.

1. Generate 6 distinct system prompts from a fixed word list with a seeded RNG
   (`_generate_text`). Each is 8000 characters, about 2000 tokens. Different
   prompts differ from their very first token.
2. Assign each of the 24 sessions to an app with a **Zipf** distribution,
   weight ∝ 1/rank^1.1. This models real multi-tenant traffic, where a few
   tenants dominate:

   | app | Zipf weight | sessions actually drawn (seed 1) |
   |---|---|---|
   | app-0 | 43.6% | **11** |
   | app-1 | 20.4% | **7** |
   | app-2 | 13.0% | 2 |
   | app-3 | 9.5% | 2 |
   | app-4 | 7.4% | **0** |
   | app-5 | 6.1% | 2 |

   So only **5 of the 6** prompts appear. Say "6 configured, 5 drawn".
3. Each session is one request: `[system(8000 chars), user(~160 chars)]`.
   The user text is also generated, so different sessions differ there.

**Ceiling on cache ratio.** The first request for each distinct prompt must be
a cold miss somewhere, and the ~40-token user message is never cached:
`(24 − 5) / 24 × ~98% ≈ 77.6%`. No policy can exceed this on this workload.

### 7.2 What `scripts/demo.py` does, per policy

1. **Isolate the namespace.** Prefix every system prompt with `[ns:<policy>] `
   and salt session ids with the policy name. This changes block 0, so every
   chained hash differs between policies, and no policy can inherit another's
   warm cache.
2. **Spawn a router** as a subprocess with environment variables
   `SWIFTSERVE_REPLICAS`, `SWIFTSERVE_MODEL`, `SWIFTSERVE_POLICY`,
   `SWIFTSERVE_TOKENIZER`, `SWIFTSERVE_BLOCK_SIZE=16`,
   `SWIFTSERVE_MIN_MATCH_TOKENS=256`, `SWIFTSERVE_SCRAPE_INTERVAL_S=1`.
3. **Wait for `/healthz`.**
4. **Run the 24 sessions with concurrency 4.** An `asyncio.Semaphore(4)` lets at
   most 4 be in flight; as one finishes the next starts. This is *closed-loop*,
   not a timed arrival process. Each session gets its own `httpx.AsyncClient`.
5. **Compute metrics**, terminate the router, move to the next policy.

Total: 4 policies × 24 requests = **96 requests**, about 10 s per policy.

### 7.3 Why the isolation step exists (the first real run)

The first real run printed a cache ratio of 52.6 → 81.5 → 90.1 → 94.5 percent in
run order. That staircase is the cluster warming up, not the policies improving:
every cache reset had failed, so each policy inherited the last one's cache and
`prefix_aware` ran last on the warmest cluster. The apparent 2.1× win was an
ordering artifact. Catching and removing it is a result worth presenting.

### 7.4 Exactly what is sent (`_stream_chat_turn` in `scripts/benchmark.py`)

```
POST {router}/v1/chat/completions
X-Session-Id: <namespaced session id>
{
  "model": "Qwen/Qwen2.5-3B-Instruct",
  "messages": [{"role":"system","content":"[ns:prefix_aware] <8000 chars>"},
               {"role":"user","content":"<~160 chars>"}],
  "max_tokens": 16,
  "stream": true,
  "stream_options": {"include_usage": true}
}
```

The reply is Server-Sent Events:

```
data: {"choices":[{"delta":{"content":"..."}}]}     <- first content token: TTFT stops here
data: {"choices":[{"delta":{"content":"..."}}]}
data: {"choices":[],"usage":{"prompt_tokens":2060,"completion_tokens":16,
        "prompt_tokens_details":{"cached_tokens":2048}}}   <- vLLM's own report
data: [DONE]
```

plus response headers set by the router:
`X-SwiftServe-Replica`, `X-SwiftServe-Predicted-Cached-Tokens`,
`X-SwiftServe-Cache-Hit`.

---

## 8. The metrics and their code

Per request (`parse_sse_stream`):

```python
ttft_ms = (first_content_chunk_time - request_start) * 1000
tpot_ms = (last_content_time - first_content_time) * 1000 / max(completion_tokens - 1, 1)
cached_tokens = usage.prompt_tokens_details.cached_tokens or 0     # null -> 0
```

Per policy (`summarize` in `demo.py`):

| Metric | Formula |
|---|---|
| TTFT p50 / p95 | linear-interpolation percentile over the 24 TTFTs |
| true cache ratio | `Σ cached_tokens / Σ prompt_tokens` (vLLM's own numbers) |
| prediction error | mean of `abs(predicted − cached) / prompt_tokens` |
| goodput | fraction of **all offered** requests with `status==200 and ttft ≤ 500ms and tpot ≤ 50ms` |
| load imbalance | `max(requests per replica) / mean(requests per replica)` |

**Goodput** is from DistServe (OSDI 2024). Two choices matter: a failed request
never counts as success, and the denominator is every offered request, so
errors cannot be hidden by averaging only over survivors.

Caveat on the 500 ms TTFT SLO: your TTFT floor through the tunnels is already
around 800 ms even for hits, so goodput in the real runs (17–33%) is dominated
by the network, not by caching. Either raise the SLO to something above the WAN
floor, or compare goodput only against the other policies.

---

## 9. Expected versus observed

### Simulation: real policy code on your real workload

Four replicas, 375-block LRU cache each, concurrency 4, the real
`RoundRobinPolicy` / `LeastConnectionsPolicy` / `SwiftServePolicy` /
`PrefixAwarePolicy` classes, byte-chunk tokenizer (chars/4 stand-in).
**Simulated routing decisions and caches, no GPU, no timing.**

| policy | cache ratio | prediction error | max:mean | requests per replica |
|---|---|---|---|---|
| round_robin | 52.9% | 52.9% | 1.00 | 6, 6, 6, 6 |
| least_connections | 52.9% | 52.9% | 1.00 | 6, 6, 6, 6 |
| swiftserve | 52.5% | 52.5% | 1.00 | 6, 6, 6, 6 |
| **prefix_aware** | **77.3%** | **0.0%** | 1.83 | 11, 4, 2, 7 |

Ceiling: 77.6%. A working index lands essentially on it.

### Your real runs

| policy | cache ratio | prediction error |
|---|---|---|
| round_robin | 52.6 / 52.3 / 52.3% (three runs) | ≈ same |
| least_connections | 60.5 / 44.2% | ≈ same |
| swiftserve | 44.3 / 44.2% | ≈ same |
| **prefix_aware** | **48.1%** | **48.4%** |

### What the comparison proves

1. **The simulator is trustworthy.** Round-robin: simulated 52.9%, real 52.6 /
   52.3 / 52.3%.
2. **Prediction error equal to cache ratio means predicted ≈ 0.** The router
   claimed nothing was cached on every request. For the baselines that is
   expected: they have no index. For `prefix_aware` it means the index matched
   nothing.
3. **A working `prefix_aware` should give about 77% and about 0% error.** It
   gave 48% and 48%.
4. **The cause is the index, not noise**, because a different failure leaves a
   different fingerprint. If the *SLA gate* were vetoing good matches, the
   policy would route to a replica that lacks the prefix, predict 0 for it, and
   be *right* — low prediction error with a low cache ratio. Real data shows
   high prediction error, which points at the index.

### The second, separate risk: the SLA gate (W8)

Even with a working index, step 3 of `prefix_aware` can veto a match. Same
simulation, varying the latency estimate and learned batch capacity:

| EWMA latency | batch capacity | cache ratio |
|---|---|---|
| 800 ms | any | 76.7% |
| 1300 ms | 1 | **64.6%** |
| 1300 ms | 4 | 76.7% |
| 2000 ms | 1 | **52.5%** (same as round-robin) |
| 2000 ms | 4 or 32 | 76.7% |

Your real end-to-end latencies are about 1400–1500 ms, and capacity starts at
a floor of 1 until the scraper observes concurrency. So this can cost a real
run 10–25 points of cache ratio. It is an issue in the plan (W8:
`SWIFTSERVE_MAX_NUM_SEQS` as the capacity floor) and not yet fixed.

### The cost you must report: load concentration

Cache-aware routing deliberately sends popular prompts to one GPU.
Simulation: `[11, 4, 2, 7]`, max:mean 1.83. Your real `prefix_aware` runs
showed p95 TTFT of 3.7 s against about 1.8 s for the others — consistent with
one replica queuing. That is the well-known tradeoff in the Preble and SGLang
papers. State it as a measured cost.

---

## 10. How to verify the real run is using a working index

Run this on the laptop after the next demo:

```bash
cd distrouter
git log --oneline -1                       # must show 418bdd8 or later
grep -h "prefix index live\|produced only\|ByteChunkTokenizer" demo_logs/router-prefix_aware.log
```

| Output | Meaning |
|---|---|
| `prefix index live: tokenizer=hf:Qwen/... N tokens = M blocks` with M ≈ 120+ | the fix is running; the index should work |
| no such line | you are running old code; `git pull` did not take |
| `ByteChunkTokenizer fallback` warning | `--tokenizer` did not reach the router |
| the demo exited with "router never became healthy" | the startup probe rejected the tokenizer |

A good next real result looks like: `prefix_aware` prediction error **well below**
its cache ratio, cache ratio **in the 65–77% range**, and the baselines near
**50%**.

---

## 11. What to claim, and what not to

**Can claim**
- Real vLLM serving real Qwen on four real T4s behind real tunnels.
- Real TTFT measured off a real token stream; `cached_tokens` is vLLM's own
  report, not an estimate.
- A working block-level index (verified in simulation and by 206 tests).
- A measured, honest prediction-error metric.
- You found and removed a measurement confound that had produced a false 2×
  win, and found a silent tokenizer bug by reasoning from the metrics.

**Cannot claim yet**
- That it beats the baselines on real hardware.
- That the router "knows" the cache. It holds an optimistic belief.
- Any statistical significance. Policies ran once, sequentially, on one seed.
  `scripts/benchmark.py` has bootstrap confidence intervals and permutation
  tests for a proper run.
- Scale conclusions from four nodes.

---

## 12. Questions a teacher is likely to ask

| Question | Answer |
|---|---|
| Why not just look inside vLLM's cache? | There is no read-only peek: a lookup is also a store, so probing a cache changes it. And modifying vLLM would make the router engine-specific. So the router predicts. |
| How wrong can the prediction be? | We measure it per request as `prediction error`; that is why the metric exists. |
| Why chained hashes? | A hash that includes its parent identifies the whole prefix, so a flat `dict[hash → replicas]` is equivalent to a radix tree and simpler. |
| Why 16 tokens per block? | vLLM's block size; it must match or hashes never correspond. |
| Why cap the cache at 375 blocks? | Otherwise nothing evicts and every policy ties, because the working set fits in memory. |
| What is the downside? | Load concentrates: simulated max:mean 1.83, real p95 TTFT about 2× worse. |
| Why was the first result wrong? | Policies ran in sequence on shared GPUs, so later ones inherited warm caches. Fixed with per-policy prefix namespaces. |
| Is it statistically valid? | Not yet; one seed, sequential. The plan in `RESEARCH_PLAN_V2.md` section 6.2 specifies interleaved paired evaluation. |
| What is novel here? | Block-level indexing on tokens rather than text, an explicit measured prediction-error metric, and the confound and silent-failure analysis. Not novel: prefix-aware routing itself (Preble, SGLang, llm-d). |

## 13. Glossary

- **Prefill / decode** — process the prompt / generate the answer.
- **KV cache** — stored attention keys and values for already-processed tokens.
- **Prefix caching** — reuse the KV cache when a new prompt starts with the same tokens.
- **Block** — 16 tokens, the unit vLLM caches and hashes.
- **Chained hash** — each block's hash includes its parent's hash.
- **TTFT** — time to first token. **TPOT** — time per output token.
- **Goodput** — fraction of requests meeting both TTFT and TPOT deadlines.
- **Zipf** — a skewed popularity distribution: a few items get most of the traffic.
- **Closed-loop** — a new request starts only when one finishes.
- **Circuit breaker** — stops sending to a replica after repeated failures.
- **EWMA** — exponentially weighted moving average.
- **Namespace isolation** — a marker that makes one policy's prefixes unmatchable by another.
