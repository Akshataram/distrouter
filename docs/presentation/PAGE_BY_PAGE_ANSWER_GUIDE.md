# Page-by-page answer guide

Everything the console shows, what it means for the router and its policies, where the number comes from, and how it was computed. It is about the router, not the web code. If you are asked about a number, find it here.

**Who measures what** (this decides how you answer "where did that number come from?"):

| Source | What it supplies |
|---|---|
| **The router** (`swiftserve/telemetry.py`, `state.py`, `policy.py`) | Routing decisions, which replica, TTFT, ITL, total time, in-flight counts, counters, latency averages, circuit state |
| **vLLM's response** (`usage` in the reply) | Prompt tokens, completion tokens, **cached tokens** |
| **vLLM's `/metrics`** (scraped every 2 s) | Running and waiting requests, KV-cache fill, prefix-cache hit counters |
| **`nvidia-smi` on the node** (through the sidecar, polled every 5 s or more) | GPU name, utilization, VRAM, temperature |
| **The browser** | Only its own timings for requests it sent, always labelled `browser` |

---

## 0. What you send: the request parameters

These appear on the Playground, Load test and Compare pages.

| Parameter | What it is | Does it affect routing? |
|---|---|---|
| **Model name** | The model the replicas serve (`Qwen/Qwen2.5-3B-Instruct`). Sent to vLLM unchanged. The router also uses it to namespace the prefix hashes | Only through the namespace |
| **System prompt** | The leading instruction text. It is the part many requests share, so it is what the cache can reuse | **Yes.** It is the shared prefix the policies look for |
| **Prompt** (user message) | The question. Different questions after the same system prompt leave the shared prefix intact | Yes: it is the part after the prefix |
| **max_tokens** | A cap on how many tokens the model may generate | No, but shorter answers make runs faster. It sets how long decode lasts, not prefill |
| **temperature, top_p** | Sampling randomness | No |
| **Streaming** | On: tokens arrive one at a time, so the router can measure TTFT and ITL. Off: one reply at the end, so TTFT and ITL cannot be observed (shown as n/a) | No, but it decides whether TTFT/ITL exist |
| **SLA ms** (`X-SLA-Ms` header) | The response-time target for this request. Blank means the router default, **3,000 ms**. The policies compare a replica's latency estimate against it | **Yes** for `swiftserve` and `prefix_aware` (the SLA check). Not for the two baselines |
| **Session id** (`X-Session-Id` header, or the `user` field) | Groups requests into one conversation. Blank means a random id per request | **Yes** for `swiftserve` (session affinity). `prefix_aware` records it but does not route on it |
| **Request id** (`X-Request-Id`) | The page generates one per request. The router copies it into every event, so what the browser sent can be matched to what the router did | No |

Two details worth knowing:
- The SLA check compares against **total request latency** (first dispatch to finish), not time to first token. That is what the router's latency average learns.
- Token counts: the **prompt tokens** the engine reports are slightly more than what you typed, because the chat template adds role markers around each message.

---

## 1. Playground

**Purpose:** send one request and see exactly what the router did with it.

### The Result panel (short view)
| Field | Meaning | How it is obtained |
|---|---|---|
| **State chip** | completed, failed, streaming, … | The router's final event for this request |
| **Served by** | The replica that produced the response | The router's `request.completed` event names it. It is also in the `X-SwiftServe-Replica` response header |
| **Why** | The routing reason, in words | Built only from fields the policy recorded in its own `last_decision` (see the table in section 6) |
| **TTFT** | Time to first token | Router: from the request arriving to the first stream event whose `delta.content` is non-empty |
| **ITL (mean)** | Average gap between tokens | Router: `(time of last text − time of first text) ÷ (number of text events − 1)`. Needs at least 2 text events. A mean, because the network delivers tokens in bursts, which distorts single gaps but not the average |
| **Total** | Request arrival to the end of the response | Router clock |
| **Reused from cache** | Prompt tokens vLLM did not recompute | vLLM's own `usage.prompt_tokens_details.cached_tokens`. Needs `--enable-prompt-tokens-details`; if absent it says "not reported", never 0 |
| **Router predicted** | The cached tokens the router expected *before* sending | Matched blocks on the chosen replica × 16, read before the router records the new prompt. Only `prefix_aware` makes a prediction; other policies show n/a |

### "All details" (the long view)
| Field | Meaning |
|---|---|
| **Selected by policy** | The replica the routing decision chose. (A recovering replica can be given a probe request instead, shown as `half_open_probe`) |
| **Dispatched to** | Every replica an HTTP attempt went to. Normally one; more if a connection error forced a retry (the router retries the **same** replica) |
| **Served by** | The replica whose response came back. Selected, dispatched and served can differ only when retries or failures occur |
| **Candidates table** | The state of every replica at the instant the policy ran: Usable, Circuit, Health, In flight, Queue, Est. latency, Capacity. It is read **before** the policy runs, so it shows what the policy saw |
| **TTFT / Total (browser view)** | The browser's own timing, slightly larger because it includes the browser-to-router hop |
| **TPOT** | Time per output token: the same span as ITL divided by `completion_tokens − 1` using the engine's token count |
| **Upstream time** | First dispatch to finish. **This is the value that feeds the replica's latency average**, which the SLA check uses |
| **Prompt / completion tokens** | From vLLM's usage report |
| **Prediction error** | `|predicted cached tokens − reported cached tokens|` |

**Why cached tokens are always a multiple of 16:** vLLM caches whole 16-token blocks, so a 40-token prompt can reuse at most 32 tokens (two full blocks), and a 1,617-token prompt reused 1,600.

---

## 2. Load test

**Purpose:** send many requests through the **currently active** policy and see where they go.

### Parameters
| Field | Meaning |
|---|---|
| **Requests** | How many requests to send in total |
| **Concurrency** | How many are in flight at the same time. The page keeps this many running until the total is reached |
| **max_tokens** | Cap on generated tokens |
| **Delay between launches** (advanced) | Pause before starting each new request. 0 means as fast as the concurrency allows |
| **Prompt source** | One common prompt for every request, or a list (one per line) cycled in order |
| **Shared system prompt** | The leading text every request shares. "Insert sample ~1500-token system prompt" fills ~1,500 tokens of text. Same text for every request means a cache prefix for the policies to exploit |
| **Streaming, SLA ms, model** (advanced) | As in section 0 |

### Progress
| Field | Meaning | How computed |
|---|---|---|
| **launched** | Requests started so far, out of the total | Count in the page |
| **succeeded / failed** | Finished fine / ended in error | From each request's outcome |
| **aborted** | Cancelled by the Stop button | Page-side. The router records a streamed one as `client_disconnected` |
| **elapsed** | Wall-clock time of the run | Browser clock |
| **req/s** | Finished requests ÷ elapsed | Browser arithmetic |

### Where the requests went
A bar per replica: the pale part is how many requests the policy **selected** for it; the solid part is how many it actually **served**. They differ only when requests fail. This is the page's direct view of load balance: round robin gives equal bars, `prefix_aware` gives uneven ones because it sends a prefix back to the replica that holds it.

### Summary
| Field | Meaning | How computed |
|---|---|---|
| **TTFT p50** | Median time to first token | Median of the router-measured TTFT of the completed requests (linear interpolation between the two nearest samples) |
| **TTFT p95** | The value 95% of requests were faster than | Same method. With 60 requests it sits near the 57th value, so a few slow requests move it a lot |
| **ITL mean** | Average streaming gap | Mean of each request's mean ITL |
| **tokens/s** | Generation throughput | `completion tokens (engine-reported) ÷ elapsed seconds` of the run |
| **prompt reused from cache** | Share of prompt tokens the engine reused | `Σ cached tokens ÷ Σ prompt tokens` over completed requests whose usage was reported |

### The request table
One row per request: number, state, **Served by**, TTFT, Total, **Cached / prompt tokens** (e.g. 1600 / 1617 means vLLM reused 1,600 of 1,617 prompt tokens), and the error if any.

---

## 3. Compare (the policy comparison)

**Purpose:** run the **same workload** through each policy in turn and compare them side by side.

### Parameters
| Field | Meaning |
|---|---|
| **Policies** | Which to run, in the order shown. "Reverse the policy order" runs them backwards, to check order does not explain the result |
| **Requests per policy** | Requests sent through each policy |
| **Concurrency** | Simultaneous requests |
| **Tenants** | How many different companies, each with its **own distinct long system prompt** |
| **System prompt tokens** | Approximate size of each tenant's system prompt (about 4 characters per token). The engine reported ~1,530 prompt tokens per request at 1,500 |
| **max_tokens** | Cap on generated tokens |
| **Seed** | Seeds the random choice of tenant and question for each request. The **same seed gives the identical request sequence for every policy**; a different seed gives a different sequence |

### How the comparison is kept fair
1. **Same sequence for every policy** (seeded random draw).
2. **A separate prompt namespace per policy.** A marker `[ns:<run>-<policy>]` starts every system prompt. vLLM only reuses a cache when the beginning matches exactly, so one policy cannot reuse blocks another policy warmed.
3. **Router state cleared on each switch.** `POST /api/policy` swaps in a fresh policy (empty prefix index or trie) and clears the replicas' learned latency, learned capacity and session table, so each policy starts from the same knowledge.
4. **The workload creates cache pressure on purpose.** 6 tenants × about 1,500 tokens = about 9,000 tokens, more than one node's KV cache holds (about 6,000 tokens). A policy that spreads every tenant across every replica makes the caches evict each other; a policy that keeps a tenant on the replica holding it can keep its blocks.

### Result table
| Row | Meaning | How computed |
|---|---|---|
| **TTFT median / 95th percentile** | As in Load test | Router-measured values of completed requests of that policy's run |
| **Prompt reused from cache** | Share of prompt tokens reused | `Σ cached ÷ Σ prompt` (engine-reported) |
| **Load balance (busiest ÷ average)** | 1.00 is perfectly even; higher means one replica took more than its share | `max requests on a replica ÷ mean requests per replica`. **Caveat:** only replicas that served at least one request are in the average, so a replica that got zero would make this look better than it is. In every run all 4 replicas served some |
| **ITL mean** | Average streaming gap | Mean of per-request mean ITL |
| **Completed / failed** | Request outcomes | Router's final state |
| **Requests per replica** | How many each replica served under each policy | Count of `served` by replica |

The bar chart is the median TTFT per policy. The sentence below it is generated from the numbers (best median, ratio to the worst, best cache reuse, lowest 95th percentile); it states a ranking, never a cause.

### How the numbers in the presentation were obtained
Three Compare runs on four Colab T4 nodes: seeds 1, 2, 3; run 2 in reverse policy order; each 60 requests per policy, 6 tenants, 1,500-token system prompts, concurrency 8, `max_tokens` 16. Run 1 values were read from a screenshot (rounded); runs 2 and 3 are the exported files kept in `docs/presentation/runs/`. Policies run one after another on shared Colab GPUs, so this is a controlled demonstration, not a publishable benchmark.

---

## 4. Live routing

**Purpose:** watch requests move through the router in real time and see each decision.

### The diagram
| Element | Meaning | Source |
|---|---|---|
| **Client** | Whoever sent the request | n/a |
| **1 Admission control: in flight N / max 256** | Requests currently in the router, and the cap. Beyond 256 the router rejects | Router counter |
| **2 Routing policy** | The active policy. For `prefix_aware` it also shows the prefix index size (distinct blocks), the tokenizer, the last decision and the default SLA (3,000 ms) | `policy_state` in the snapshot |
| **3 Dispatch** | The proxy, which retries connection errors on the same replica | n/a |
| **Replica box** | Name (your label), tunnel host, circuit state, queue depth, **selected** (times the policy chose it), **served** (times it returned a response), and the number: requests running on it **right now** | Router counters and state |
| **Health dot** | Green: the replica answered its last health check. Red: unreachable | Router's scrape of `/health` |
| **Moving dots** | One per real request. They move **only when the router emits the matching event** (received, decision, dispatch, completed or failed) | The router's event stream |
| **Line thickness** to a replica | That replica's share of **all** routing decisions since the router started | `selected` counters |

### The decision table (last ~18 requests)
Columns: **Time**, **Request**, **Path**, **Selected**, **Dispatched**, **Served**, **Why**. The "Why" text comes from the policy's own record. Section 6 lists every path and what it means.

### Timeline (collapsed section)
One bar per request in three segments: **routing** (received to dispatched), **waiting for first token** (dispatched to first token: queueing plus prefill), and **streaming** (first token to finished). A red segment means failed; a thin mark means a retry.

---

## 5. Dashboard

**Purpose:** the state of each replica as the router sees it, plus the real GPU readings.

### Top tiles
| Tile | Meaning | How computed |
|---|---|---|
| **completed** | Requests finished successfully since the router started | Router counter |
| **failed** | Requests that ended in an error | See "what counts as failed" below |
| **in flight now** | Requests running right now across all replicas | Sum of each replica's in-flight count |
| **req/s** | Request completion rate | Completions in the **last 30 seconds** ÷ that window |
| **tokens/s** | Generation throughput | Completion tokens (from the engines' usage reports) in the last 30 seconds ÷ that window |

**What counts as "failed":** the upstream answered with an HTTP status of 500 or more, the connection failed or timed out, or the stream broke mid-way. An HTTP 4xx (for example a bad request) is **not** counted as failed by the router. Cloudflare tunnel errors such as 502, 524 and 530 come back as 5xx, so a node that stalls or restarts shows up as failures.

### Per-replica card
| Field | Meaning | How obtained |
|---|---|---|
| **Header: name · GPU** | Your label and the GPU model | The label is yours (`--labels`); the GPU model is reported by the node |
| **healthy / circuit** | Last health check result; circuit breaker state (shown only if not closed) | Router. Five consecutive failures open the circuit; after a cooldown (10 s, doubling up to 120 s) one probe request tests the replica |
| **GPU utilization** | How busy the GPU's compute is | `nvidia-smi` on the node |
| **VRAM used / total** | GPU memory in use | `nvidia-smi` on the node. This includes the model weights |
| **Temperature** | GPU temperature | `nvidia-smi` on the node |
| **in flight** | Requests the router has sent here that have not finished | Router counter |
| **served** | Requests this replica completed successfully since the router started | Router counter |
| **failed** | Requests dispatched here that ended in error | Router counter |
| **TTFT p50** | Median time to first token for this replica | The **last 500 completed** streaming requests on this replica (a rolling window, not all time) |
| **ITL mean** | Average streaming gap | Same rolling window |
| **reused from cache** | `Σ cached ÷ Σ prompt tokens` for this replica's requests | Engine usage reports, cumulative since start |

Counters (served, failed, reused from cache) are cumulative since the router started and mix every policy that has run. Latency figures use a rolling window of the last 500. **Do not use the Dashboard to compare policies; use Compare.**

If a GPU row says "unavailable", the node's sidecar does not expose `/sidecar/gpu` (an older copy of the repo); routing is unaffected.

### The collapsed "Router settings and what is measured" section
| Field | Meaning |
|---|---|
| **in flight / queue depth** | Queue depth is the larger of (running + waiting reported by vLLM) and the router's in-flight count, using the vLLM figure only if it was scraped within the last 5 seconds |
| **vLLM running / waiting** | `vllm:num_requests_running` and `vllm:num_requests_waiting`, scraped every 2 s |
| **estimated latency** | What the router predicts a new request would take here: below capacity, the learned latency at the current load level (3 load buckets); at or above capacity, `service time × (queue depth + 1) ÷ capacity`. Starts at **800 ms** until real data arrives |
| **average latency (EWMA)** | Exponential average (weight 0.2) of completed requests' upstream time (first dispatch to finish) |
| **batch capacity** | How many concurrent requests the model assumes the replica handles: the larger of `--max-num-seqs` and the highest concurrency scraped |
| **tracked sessions** | Size of the replica's session table (used by `swiftserve`) |
| **KV-cache usage** | vLLM's gauge of KV-cache blocks in use. It is **cache occupancy, not GPU load** |
| **engine prefix hit rate** | vLLM's cumulative `prefix_cache_hits ÷ prefix_cache_queries` |
| **Policy state** | For `prefix_aware`: tokenizer, distinct blocks in the index, blocks per replica, thresholds (at least 50% of the prompt or 256 tokens), namespace. For `swiftserve`: trie size |

---

## 6. The routing "Why" strings: what each path means

| Path | Policy | Meaning |
|---|---|---|
| `round_robin` | round_robin | "rotation slot N of 4": the counter's turn |
| `least_connections` | least_connections | Shows each replica's queue depth; the shortest queue won |
| `session_affinity` | swiftserve | This session was last served by replica R and its estimate was within the SLA |
| `prefix_trie` | swiftserve | The message trie matched at least 2 messages on replica R, within the SLA |
| `least_loaded` | swiftserve | No usable session or trie match (depth shown, needs 2), so the least-loaded replica |
| `prefix_match` | prefix_aware | "R holds 30/37 prompt blocks (480 tokens); estimated X ms ≤ SLA 3000 ms": the longest eligible match, within the SLA |
| `sla_veto` | prefix_aware | The best match would exceed the SLA, so it was skipped for the least-loaded replica |
| `no_eligible_match` | prefix_aware | No replica held at least 50% of the prompt or 256 tokens, so least loaded |
| `half_open_probe` | any | A recovering replica was given this request to test it; the policy was not consulted |

---

## 7. Questions you may be asked about the numbers

**Why is TTFT 900 ms when your node test showed 74 ms?**
The node test called the replica directly with a fully cached prompt. Through the router the time also includes the tunnel (the internet round trip), any queueing, and the prefill of the part of the prompt that was **not** cached. Cached requests are the fast ones; the median blends cached and uncached.

**Is "reused from cache" a measurement or an estimate?**
A measurement. It is vLLM's own count of cached prompt tokens in its response. The router's prediction is shown separately and labelled as a prediction.

**Why is cache reuse 86% and not 100%?**
Each tenant's first request cannot hit (nothing is cached yet). With 6 tenants over 60 requests, at most about 90% can be reused. That ceiling is my calculation, not a measurement.

**Why is `prefix_aware`'s load uneven?**
It sends a request back to the replica that already holds its prefix, so busy tenants concentrate on a replica. The SLA check is meant to stop overloading, but it did not remove the imbalance (busiest ÷ average 1.5 to 1.7 against 1.0 for round robin).

**Why did `prefix_aware` win the median but not the tail?**
The median is where prefill savings show up. The 95th percentile is dominated by the few slowest requests, which can come from queueing on a busy replica or from the first (uncached) request of each tenant. Its tail rank varied between runs, so I do not claim anything about the tail.

**What is the SLA and what does it do?**
A per-request response-time target, 3,000 ms by default, set by the `X-SLA-Ms` header. `swiftserve` and `prefix_aware` compare a replica's estimated latency with it before routing there. The two baselines ignore it.

**Where does "estimated latency" come from?**
From the router's own record of how long completed requests took on that replica (an exponential average, by load level), converted to a queue estimate when the replica is at capacity. It starts at 800 ms before any data.

**What does "failed" mean and why were there failures?**
A request whose upstream answer was 5xx, whose connection failed or timed out, or whose stream broke. With tunnels and shared Colab GPUs a stalled node produces 502 or 524 errors, so failures can appear when a node restarts.

**How do you know ITL is not affected by caching?**
ITL is the pace of generation after the first token, which depends on the GPU's decode speed and how busy it is. Caching only removes prefill work, which is before the first token. In the runs ITL stayed in the same range for all four policies.

**Why can't you compare policies from the Dashboard?**
Its counters mix every policy that has run and its latency window is the last 500 requests. Compare isolates each policy's own requests.
