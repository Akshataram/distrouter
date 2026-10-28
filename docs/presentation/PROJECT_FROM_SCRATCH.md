# SwiftServe, from scratch: chapter by chapter

Read the chapters in order. Each one builds on the one before. Every chapter ends with **"Tough questions"**: the questions a teacher can ask at that level, with answers you can say out loud. Chapter 14 is a bank of the hardest questions across the whole project.

Tags used throughout:
- **[Verified]** = observed in your real runs on the GPUs.
- **[In code]** = implemented in the repository, checked by reading it or by tests with fake replicas.
- **[Belief]** = the router's guess; not directly observed.
- **[Not done]** = not implemented or not explained.

---

## Chapter 1: The problem

**What the project is, in one sentence.** A router that sits in front of several GPUs running the same language model, and decides which GPU should answer each request, trying to pick the GPU that already did part of the work.

**Why several GPUs exist.** One GPU can only serve so many users. So companies run the same model on many GPUs ("replicas") and put one entrance in front of them. That entrance is the router.

**What a normal router does.** Round robin sends request 1 to GPU 1, request 2 to GPU 2, and so on. Least connections sends the request to the GPU with the fewest running requests. Both treat every request as the same size and every GPU as equally prepared. Neither reads the request.

**Why that is wasteful for LLMs.** Many requests start with the same long text: a company's instructions, a document, a chat history. A GPU that has already processed that text can skip it next time. A GPU that hasn't must read it all again. Reading the long prompt is what makes the first word slow. A router that ignores this sends the request to a GPU that has to redo the work.

**What this project does.** The router reads the prompt, works out which GPU has already processed its beginning, and sends the request there, unless that GPU is too busy.

**Your setup.** Four free Google Colab Tesla T4 GPUs, each running Qwen2.5-3B-Instruct through vLLM. The router runs on your laptop. You compare four routing policies on the same traffic.

### Tough questions

**Q: Why not just use nginx?**
A: Nginx balances by rotation or connection count. It can't read the prompt, so it can't know which GPU holds the cache. Mine reads the prompt and routes on cache contents. I didn't run nginx; my round robin and least connections baselines have the same logic as its defaults.

**Q: Isn't this already solved by big companies?**
A: The idea exists (Chapter 13 lists related work). I'm not claiming to have invented it. My work is a small, readable implementation of it, measured on real GPUs, with a console that shows each decision.

---

## Chapter 2: How an LLM produces text

You need five ideas.

**1. Tokens.** The model doesn't read letters or words. It reads tokens: pieces of words. "reset my password" might be 3 or 4 tokens. A prompt of about 1,500 tokens is roughly 1,100 words.

**2. Prefill.** Before the model writes anything, it reads the whole prompt in one pass. This is heavy work, proportional to the prompt length. The time this takes is the main part of **TTFT**.

**3. Decode.** The model then writes one token at a time. Each new token needs the model to look at everything before it. This is slower per token than it sounds because the GPU is waiting on memory. The time between tokens is **ITL**.

**4. KV cache.** To avoid recomputing, the model stores intermediate results (called keys and values) for every token it has already processed. This is the KV cache. It lives in GPU memory and grows with the prompt and with each generated token.

**5. Why the cache is reusable.** If a new prompt begins with exactly the same tokens as an old one, the old tokens' results are identical, so the GPU can skip prefill for them. That saving is what the router tries to cause.

### Tough questions

**Q: Why can't it reuse the cache if only the middle of the prompt matches?**
A: The result for each token depends on all tokens before it. If an earlier token differs, every later result differs. So reuse works only for an identical **beginning** of the prompt.

**Q: What makes the first token slow?**
A: Prefill of the whole prompt, plus any waiting in the queue behind other requests.

**Q: Does caching make writing faster?**
A: No. It removes work before the first token. ITL (the pace after that) is not changed by caching. That is why ITL stayed in the same range for different policies in my early runs. [Verified in the first three runs; in the fourth run prefix_aware's ITL was higher, which I can't attribute to caching. See Chapter 12.]

---

## Chapter 3: vLLM and its prefix cache

**What vLLM is.** The inference engine that actually runs the model on each GPU. It receives HTTP requests in the OpenAI chat format and streams back tokens.

**How its cache is organised.** The KV cache is divided into fixed **blocks of 16 tokens**. vLLM keeps a table of blocks. When "automatic prefix caching" is on, a new prompt's blocks are compared with stored ones, and identical leading blocks are reused instead of recomputed.

**Three rules that matter for the router:**
1. Only **full** blocks of 16 are cached. A prompt of 40 tokens has 2 full blocks (32 tokens) cacheable and 8 leftover tokens that are not.
2. Reuse is **prefix-only**. Block 3 is reusable only if blocks 1 and 2 are also matched.
3. Old blocks are **evicted** when memory runs out (least recently used first). So a cached prefix can vanish.

**Your settings.**
- Prefix caching on, block size 16.
- `--enable-prompt-tokens-details`: makes vLLM report, in each reply, how many prompt tokens came from cache (`usage.prompt_tokens_details.cached_tokens`). This is how you measure real cache reuse.
- Cache deliberately small: 375 blocks, about 6,000 tokens. Six tenants with about 1,500-token prompts don't all fit, so where the router sends a request matters. Without this, every GPU would hold everything and routing wouldn't matter much.
- `--max-num-seqs 32` on the node (max requests batched together). You tell the router `--max-num-seqs 4`, a smaller, conservative capacity assumption.

**Evidence from your self-test.** [Verified] The same long prompt sent three times to a node: the first request took about 2,061 ms to the first token, the next ones about 74 ms, with 1,600 of 1,617 prompt tokens cached. That is the effect the router exploits.

### Tough questions

**Q: Why 16?** A: That is vLLM's block size setting. The router uses the same size so its idea of a block matches the engine's.

**Q: Why is cached_tokens always a multiple of 16?** A: Only full blocks can be cached.

**Q: How do you know the cache is actually used and not your imagination?** A: vLLM reports it itself, per request. The node-level test showed 74 ms against 2,061 ms and 1,600 of 1,617 tokens cached.

**Q: Why did you shrink the cache?** A: To create pressure. On a big cache, any GPU would eventually hold every prompt, and routing wouldn't change cache hits much. With a small cache, spreading six tenants across four GPUs naively causes evictions.

**Q: Why does `--max-num-seqs 4` on the router differ from 32 on the node?** A: The router's value is its assumed batch capacity, used for the latency estimate (Chapter 6). It is raised automatically if the router sees more running requests than that. It is a conservative setting, not a measurement.

---

## Chapter 4: The metrics, defined exactly

All measured **by the router** with a monotonic clock (`StreamTimer` in `telemetry.py`) while the response streams through it. The browser also measures its own view, which is slightly larger because it includes the browser-to-router hop; the UI labels which is which.

| Metric | Definition | What it tells you |
|---|---|---|
| **TTFT** (time to first token) | Request arrival at the router → first streamed event with non-empty text | How long the user waits before seeing anything. Includes queueing and prefill. Caching shortens it. |
| **ITL** (inter-token latency), mean | (time of last text event − time of first text event) ÷ (number of text events − 1) | The pace of writing after the first word |
| **TPOT** | Same time span ÷ (completion tokens − 1) | Per-token time using vLLM's token count |
| **Total latency** | Arrival → end of response | Whole request duration |
| **Cache reuse** | Σ cached tokens ÷ Σ prompt tokens, from vLLM's `usage` | Fraction of prompt work skipped |
| **p50 / median** | The middle value | Typical experience |
| **p95** | 95% of requests were faster than this | The tail: slow-case experience |
| **Load balance** | Busiest replica's request count ÷ the average | 1.00 = perfectly even; higher = uneven |
| **Throughput** | Requests per second, tokens per second, over the last 30 s on the Dashboard | Volume handled |

Details worth knowing:
- Percentiles use linear interpolation.
- ITL is a **mean** because network delivery bunches events; single gaps are noisy.
- Cache reuse comes from the final `usage` chunk, which needs `stream_options.include_usage`.
- Load balance excludes replicas that served 0.

### Tough questions

**Q: Why median and not average TTFT?** A: A few very slow requests drag the average; the median shows the typical case. I also report p95 for the slow cases.

**Q: What's p95 and why does it matter?** A: The value 95% of requests beat. It shows whether slow requests exist. My p95 results were not consistently better with prefix_aware, so I make no tail claim.

**Q: Is TTFT measured at the browser or the router?** A: The router; the browser's number is separate and slightly larger.

**Q: How do you know cache reuse isn't just your estimate?** A: The reuse figure uses vLLM's own count. The router's separate prediction is shown next to it for comparison.

**Q: Could the router's timing be wrong because of streaming buffers?** A: Possibly small errors: tunnel and proxy buffering can bunch events, which is why ITL is a mean over the whole stream. TTFT can be slightly late if a proxy delays the first chunk. I state that these are controlled-demonstration numbers.

---

## Chapter 5: The router itself

**What it is.** A FastAPI (Python, asynchronous) application. Clients call it exactly like OpenAI: `POST /v1/chat/completions`. It forwards to one of the replicas and streams the answer back.

**The life of one request** (`chat_completions` in `app.py`):

1. **Arrive.** Assign a request id (or keep the one the page sends in `X-Request-Id`) and start a timer.
2. **Admission.** If more than 256 requests are in flight, reject immediately instead of building an unbounded queue.
3. **Snapshot.** Copy every replica's current state (in-flight, scraped load, estimate, circuit state). The policy sees this snapshot, and the console shows the same one.
4. **Policy.** The active policy chooses a replica and records why in `last_decision`.
5. **Dispatch.** The proxy (`proxy.py`) sends the request to that replica and streams back the chunks.
6. **Retry.** Connection errors are retried up to 2 more times, on the **same** replica.
7. **Record.** On finish, record the outcome once (completed, failed, client disconnected, or rejected) and update the replica's latency estimate.

**Resilience rules.**
- "Failed" means HTTP 5xx, connection error, timeout, or a stream that broke. A 4xx counts as completed (the replica answered).
- **Circuit breaker.** 5 consecutive failures open the replica's circuit; traffic stops. After a cooldown (10 s, doubling up to 120 s) one probe request tests it (the `half_open_probe` path); success closes it.
- If all circuits are open, the router returns an error. **[Not done]** There is no failover of an in-progress request to a different replica.

**Why asynchronous.** Many requests wait on slow GPUs. Async lets one Python process hold hundreds of open streams without a thread each.

### Tough questions

**Q: What happens if a GPU dies mid-request?** A: That request fails (broken stream). Later requests see failures and, after 5 in a row, the circuit opens and traffic avoids it. The failed request is not re-sent elsewhere.

**Q: Why retry on the same replica?** A: A connect error is usually a short tunnel hiccup. Re-sending to another replica would also lose any cache benefit. It's a simple policy; true failover is future work.

**Q: What is the circuit breaker for?** A: To stop sending requests to a replica that keeps failing, so users don't keep waiting on a dead node.

**Q: What stops overload?** A: The admission cap of 256 in-flight requests, plus each policy's latency/SLA awareness.

**Q: Is the router a single point of failure?** A: Yes. It's one process on my laptop. A production design would run several behind another balancer.

---

## Chapter 6: What the router knows about each GPU (EWMA, batch, latency)

This is the part you were asking about. Each replica has a `ReplicaState` object (`state.py`).

**1. In-flight.** Requests the router has dispatched and not yet finished. The router counts this itself.

**2. Scraped vLLM load.** Every 2 s the router calls the replica's `/health` and `/metrics` and reads vLLM's own running and waiting request counts. **Queue depth** = the larger of (vLLM running + waiting) and the router's in-flight count, if the scrape is under 5 s old; otherwise just the router's in-flight count.

**3. Request latency, learned: the EWMA.**
- EWMA = exponentially weighted moving average. Each time a request completes, the router updates: `new = 0.2 × observed + 0.8 × old`. The factor 0.2 is α. Recent requests count more, old ones fade.
- It starts from a seed value of 800 ms before any data exists.
- The observed value is the elapsed time of the request as measured in `proxy.py` (see `record_completion_latency` calls at about lines 173 and 218; open those lines to confirm exactly which interval is recorded before you describe it as TTFT or as total).
- Instead of one flat number, there are **3 occupancy buckets**: a request that landed when the batch was nearly empty, half full, or nearly full is recorded in separate averages. A request on a quiet GPU is faster than one on a busy GPU, so mixing them would blur the estimate. A bucket with no data yet falls back to the overall average.

**4. Batch capacity (the "batch" part).**
- vLLM processes many requests together (continuous batching). Below some number of concurrent requests, an extra request doesn't wait; it joins the batch.
- The router's capacity = max(the `--max-num-seqs` you gave it, the highest running count it ever scraped). It never shrinks.

**5. The latency estimate (`estimate_latency_ms`).** A queueing model (M/M/c, where c = batch capacity):
- If current queue depth is below capacity: the estimate is the bucket's learned latency for that occupancy (no waiting expected).
- At or above capacity: `service time × (depth + 1) ÷ capacity`, a growing queueing delay.
- Optional cold-start penalty: `cold_start_ms_per_token × prefix tokens` (default 0, off).

**Where it is used.**
- `prefix_aware` (`policy.py` around lines 285 and 300): the SLA veto compares the best-match replica's estimate with the SLA (default 3000 ms, per request via `X-SLA-Ms`). Also in least-loaded fallback ordering `(queue_depth, estimate, replica_id)`.
- `swiftserve` (lines ~133, 155, 168): checks owner/shared replica estimates before following affinity and orders the least-loaded fallback by estimate plus cold-start penalty.
- `round_robin` and `least_connections` don't use it.
- Dashboard and `/metrics` expose `est_latency_ms` and `batch_capacity`.
- When you switch policies in Compare, `reset_learned_state()` resets the EWMA, buckets, capacity and sessions to start every policy equally.

### Tough questions

**Q: Why EWMA and not a plain average?** A: A plain average reacts slowly when a node changes behaviour. EWMA weighs recent requests more, so the estimate follows the GPU's current state.

**Q: Why α = 0.2?** A: It's a common default that smooths noise while still adapting in a few requests. I didn't tune it; I can't claim it's optimal.

**Q: Is the estimate accurate?** A: I haven't measured how well it predicts latency. It's a heuristic whose job is to catch overload, not to predict precisely.

**Q: Why an M/M/c model?** A: vLLM batches requests, so a GPU can serve several at once. M/M/1 would assume one at a time and overstate delay. M/M/c with c = capacity says "no waiting below capacity, growing delay above".

**Q: Why seed at 800 ms?** A: Something is needed before data exists. It's arbitrary, and it is reset on policy switches, so each policy starts from the same value.

**Q: Where do the "reset" changes matter?** A: Without it, learned latency, capacity and sessions from one policy leaked into the next in the Compare run. I added the reset after my first run, so only later runs have identical starting state.

---

## Chapter 7: The two baseline policies

**Round robin** (`policy.py`). One integer counter; each request goes to the next replica in turn. Perfectly even load. Never looks at the prompt, so a cache hit is luck. Measured load balance: 1.00.

**Least connections.** `min(replicas, key=(queue_depth, replica_id))`: the replica with the shortest queue, ties broken by lowest id. Adapts to load, still blind to caches.

Both exist as baselines: they're the standard strategies, so any gain must be measured against them.

### Tough questions

**Q: Are these baselines fair?** A: They use the same circuit-breaker rules and the same queue-depth signal as my policies, and they run on identical traffic.

**Q: Why does least connections have worse balance (1.35 to 1.47) than round robin (1.00)?** A: Queue depth is a noisy signal with ties and delayed updates; it doesn't guarantee equal counts, only that a new request avoids the busiest replica at that instant.

---

## Chapter 8: The swiftserve policy (the earlier version)

Swiftserve tries to keep conversations on one replica. In order:

1. **Session affinity** (`session_affinity`): if this conversation was served by a replica before, prefer it (unless too slow).
2. **Message trie** (`prefix_trie`): a trie (a tree of prefixes) over **whole messages**, keyed by (role, content). If a replica saw the same first messages, prefer it. It needs **at least 2 matching messages**.
3. **Least loaded** (`least_loaded`): otherwise the replica with the lowest (queue depth, estimated latency + cold-start penalty).

Limits: trie max depth 6, 20,000 nodes, 20,000 sessions. Each node stores only the **last** replica that served that path.

**Why it is weaker for this workload.** The unit it matches is a whole message. A long shared system prompt is **one** message, below the required 2, so it is ignored (the "trie depth 1, needs ≥2" text you saw). Also vLLM caches 16-token blocks, a different unit than whole messages. And each trie node remembers only one replica.

### Tough questions

**Q: Why did swiftserve sometimes win?** A: In run 4 it had the lowest median TTFT (651 ms). I haven't confirmed why. My unverified guess is that repeated tenant-and-question pairs produced 2-message matches, but I haven't checked.

**Q: Why did you build prefix_aware if swiftserve works?** A: Matching in the engine's own block unit lets the router predict how many tokens will be reused, identify several holders, and handle a single long shared prompt.

---

## Chapter 9: The prefix_aware policy (your main contribution)

### Step 1: Tokenize
Apply the model's chat template to the messages and tokenize with the model's tokenizer (`--tokenizer Qwen/Qwen2.5-3B-Instruct`). The router sees the same tokens vLLM will. Without a tokenizer, predictions would be meaningless, so `dev.py` warns.

### Step 2: Cut into full blocks of 16
Partial leftover tokens are dropped, because vLLM only caches full blocks.

### Step 3: Chained hashes
```
h0 = model name (namespace)
h1 = blake2b(h0 + tokens of block 1)   (8 bytes)
h2 = blake2b(h1 + tokens of block 2)
...
```
Each hash includes the previous one, so one hash represents the **entire prefix** up to that block. Matching a hash means all earlier blocks match too. Hashing a block alone would wrongly match blocks with the same text but a different beginning.

### Step 4: The index (`prefix_index.py`)
- `_holders`: dictionary `hash → set of replicas believed to hold it`.
- Per replica, an LRU list capped at 20,000 hashes, imitating vLLM's eviction.

### Step 5: Match
Walk the hashes in order, narrowing the set of replicas that hold every hash so far. The result: for each replica, how many leading blocks it matches.

### Step 6: Eligibility
A replica is eligible only if its match covers **≥ 50% of the prompt's blocks or ≥ 256 tokens**, so tiny accidental matches don't distort routing.

### Step 7: Pick
`max(eligible, key=(matched blocks, −queue depth, −replica id))`: longest match, then lighter load, then lower id.

### Step 8: SLA veto
Compare the chosen replica's `estimate_latency_ms()` with the SLA (default 3000 ms, or `X-SLA-Ms`). If it exceeds it, ignore the match (`sla_veto`) and use least loaded.

### Step 9: Fallback
No eligible replica → `no_eligible_match` → least loaded (queue depth, then estimate, then id).

### Step 10: Optimistic insert
After choosing, immediately record this prompt's hashes under the chosen replica. A burst of similar requests then follows the first one instead of scattering. This is a **belief**: vLLM hasn't necessarily cached it yet.

### Paths you'll see in the console
`prefix_match`, `sla_veto`, `no_eligible_match`, plus `half_open_probe` (router-level circuit test).

### Prediction vs reality
The router's prediction ("expected cached tokens") is shown beside vLLM's reported `cached_tokens`. [Verified on one test request: 16 predicted, 16 reported.] **[Not done]** Agreement across a whole run has not been measured.

### Why it can lose
- It concentrates load: tenants return to the replica that holds their cache. Load balance was 1.40 to 1.58 in my runs.
- The index can be wrong after evictions.
- In run 4 the highest cache reuse didn't give lower median latency.

### Tough questions

**Q: Why hash instead of comparing text?** A: Hashes are small, fixed-size and fast to look up. Comparing long prompts to every stored prompt would be slow.

**Q: Hash collisions?** A: 8-byte blake2b hashes make accidental collisions very unlikely at this scale; a collision would produce a wrong prediction, not wrong output, since vLLM decides real reuse.

**Q: Why 50% or 256 tokens?** A: Thresholds I chose to ignore trivial matches. I haven't tuned them.

**Q: Why does the SLA exist?** A: Following a cache blindly can send many requests to one busy GPU. The check skips the match if that GPU's estimated latency exceeds the target. In run 4 prefix_aware's tail was bad, so this safeguard didn't prevent it. I don't claim it works in all cases.

**Q: What if the router's belief is wrong?** A: The request just runs without reuse, slower, but still correct. Only speed is affected.

**Q: Why record before vLLM has cached it?** A: So concurrent similar requests follow each other. Risk: if the first request fails or is evicted, later ones are routed on a false belief.

**Q: What happens with multiple replicas holding the same prefix?** A: The one with the longest match wins; ties go to lighter load.

**Q: Could you share cache between replicas?** A: Not here. Each GPU's cache is separate. Systems that move KV cache across machines exist (Chapter 13), but that's different work.

---

## Chapter 10: Deployment: Colab, tunnels, sidecar, launcher

**Each node (a Colab notebook):**
1. Installs vLLM and starts it on port 8000 with prefix caching on, block size 16, prompt-token details on, 375 cache blocks, `--max-num-seqs 32`.
2. Starts the **sidecar** on port 9000.
3. Starts a Cloudflare quick tunnel pointing at the sidecar, printing a public URL.
4. Runs a self-test (the 2,061 ms vs 74 ms check).
5. Keeps running (keep-alive cell).

**Why a tunnel.** Colab machines have no public address. A Cloudflare tunnel gives each a temporary public URL. The URL changes on every restart, so you copy four fresh URLs each time. Cloudflare returns 530 when a tunnel is gone and 502/503/504 when nothing is behind it.

**The sidecar** (`replica_sidecar.py`). A small FastAPI program in front of vLLM:
- Passes through requests (including `/v1/chat/completions` and `/health`) to vLLM and streams back.
- `GET /sidecar/gpu`: runs `nvidia-smi` and returns GPU name, utilisation, VRAM, temperature (cached 1 s; the router polls at most every 5 s).
- Optional `/chaos/...` endpoints for fault injection. They weren't used for your results.
- Why it exists: one tunnel exposes one port, and vLLM doesn't report GPU telemetry.

**The launcher** (`scripts/dev.py`). One command:
```
python3 scripts/dev.py --replicas "<4 urls>" --policy prefix_aware \
  --model Qwen/Qwen2.5-3B-Instruct --tokenizer Qwen/Qwen2.5-3B-Instruct \
  --max-num-seqs 4 --labels "colab-a T4,colab-b T4,colab-c T4,colab-d T4"
```
It checks each replica before starting (tunnel alive, model name matches), warns about missing tokenizer, enables policy switching only for a loopback bind, and **never** starts vLLM or downloads models. `--check-only` validates without starting.

### Tough questions

**Q: Why does the router run on your laptop?** A: Colab is for GPUs; the router is light. A laptop is simple for a demo. A tunnel adds latency to every request, which is part of why numbers are noisy.

**Q: How does the router know the GPU model?** A: The sidecar reports it from `nvidia-smi`. The router never infers it from the replica number.

**Q: Are tunnels secure?** A: Quick tunnels are public URLs with no authentication; fine for a demo, not for production. The router supports an optional bearer token (`SWIFTSERVE_API_TOKEN`).

**Q: Does the tunnel affect your results?** A: Yes: it adds latency and jitter. All policies share the same conditions, but run one after another, so conditions can drift between them.

---

## Chapter 11: Observability: events, console, Dashboard, Prometheus

**Events.** At each step the router emits an event with a sequence number into a ring buffer (5,000): `request.received`, `routing.decision`, `dispatch`, `dispatch.retry`, `upstream.headers`, `first_token`, `request.completed` / `failed` / `client_disconnected` / `rejected`, `replica.state`, `policy.changed`. Emitting never blocks and never raises, so the console can't slow a request.

**How the browser gets them.** `GET /api/events/stream` (server-sent events). A reconnecting browser resumes from the last sequence number; if events were dropped it receives a `gap` marker. Each connection is capped at 30 s so shutdown never hangs; the page reconnects. A separate snapshot (`GET /api/dashboard`) is polled every 2 s for replica state.

**The tabs.**
- **Playground:** one request; shows Served by, Why, TTFT, ITL, Reused from cache, Router predicted.
- **Load test:** N requests at chosen concurrency; per-request table; export.
- **Compare:** the four-policy experiment (Chapter 12).
- **Live routing:** diagram and the "Recent routing decisions" table. Dots move only when the matching event arrives.
- **Dashboard:** tiles and a card per replica.

**Dashboard meanings.**
- Counters (completed, failed, per-replica served/failed, reused from cache) are cumulative since router start, across all policies.
- TTFT p50 and ITL mean on a card = rolling last 500 completed requests on that replica.
- req/s and tokens/s = last 30 s.
- Healthy/unreachable = the router's `/health` scrape result; **routing uses the circuit breaker**, not this flag.
- GPU values come from `nvidia-smi` via the sidecar and may be stale.
- A metric nobody measured is shown as "unavailable", never zero.

**Prometheus and Grafana (optional).** The router exposes `/metrics` (`swiftserve_requests_total`, `request_latency_ms` histogram, `sla_violations_total`, `admission_rejected_total`, per-replica gauges). `deploy/observability/` has a Prometheus config (scrapes every 5 s) and a Grafana dashboard. `dev.py` doesn't start them, and your results don't use them. I never ran that stack. Don't quote `swiftserve_cache_hit_total` or the `X-SwiftServe-Cache-Hit` header: they are session-based beliefs.

### Tough questions

**Q: Is the Live routing view real or an animation?** A: Real. Prove it: `curl -N "http://127.0.0.1:8000/api/events/stream?since=0"` and send a request; the same events appear.

**Q: Why server-sent events and not WebSockets?** A: The data flows one way (router to browser), and SSE reconnects and resumes simply over plain HTTP.

**Q: Why no React?** A: Small page, no build step. The project is about the router; the UI is plain JavaScript modules.

**Q: Where does the "Why" text come from?** A: From fields the policy recorded at decision time (e.g. matched blocks, estimated latency, SLA). If the policy recorded nothing, the page shows nothing.

**Q: Why does the Dashboard show "unreachable" yet traffic flowed?** A: The badge is the health scrape; the router uses circuit state. I haven't established why one scrape failed in my screenshot. Don't invent a cause.

---

## Chapter 12: The experiment and the results

**Design (Compare tab).**
- One seeded workload: 60 requests per policy, 6 tenants, each with a different ~1,500-token system prompt, many questions, concurrency 8, `max_tokens` 16.
- The **seed** fixes which tenant asks what and in what order; the same seed gives the same list for every policy.
- **Cache isolation:** each policy's prompts begin with `[ns:<run>-<policy>]`; chaining changes every hash after it, and vLLM's real cache also treats the prompts as different, so no policy can reuse another's cache.
- **State reset** between policies (`reset_learned_state`).
- Policy switching is via `POST /api/policy`, off unless `SWIFTSERVE_ALLOW_POLICY_SWITCH` is set; refused while requests are in flight.
- Policies run **one after another**, not simultaneously.

**Runs.**

| Run | Seed | Order | Notes |
|---|---|---|---|
| 1 | 1 | normal | read from a screenshot (rounded); before the reset fix |
| 2 | 2 | reversed | exported file |
| 3 | 3 | normal | exported file |
| 4 | 1 | normal | screenshot; current code with reset |

**Results, three-run means (runs 1 to 3).**

| | round_robin | least_conn | swiftserve | prefix_aware |
|---|---|---|---|---|
| Median TTFT | 966 | 993 | 765 | 451 ms |
| Cache reuse | 53% | 52% | 66% | 86% |
| Load balance | 1.00 | 1.35 | 1.27 | 1.58 |

**Run 4 (counterexample).**

| | round_robin | least_conn | swiftserve | prefix_aware |
|---|---|---|---|---|
| Median TTFT | 1325 | 1275 | 651 | 1377 ms |
| p95 TTFT | 2379 | 2133 | 1731 | 6099 ms |
| Cache reuse | 56% | 52% | 65% | 72% |
| Load balance | 1.00 | 1.47 | 1.27 | 1.40 |
| ITL mean | 31.1 | 37.9 | 36.8 | 50.4 ms |

**What to conclude.**
- [Verified] Prefix_aware had the highest cache reuse in all four runs.
- [Verified] Lowest median TTFT in 3 of 4 runs; it lost run 4.
- [Verified] Prefix_aware's load balance averaged 1.58 over runs 1 to 3 (the highest mean) and was 1.40 in run 4, where least_connections was higher (1.47). Its p95 rank was 4, 2, 2 in runs 1 to 3 (no tail claim) and it was the worst in run 4.
- [Not done] Cause of run 4 is unexplained. Prefix_aware's ITL was higher (50 ms), and caching doesn't change ITL, which hints at environment (slower GPU/tunnel during its slot) or heavier load on a hot replica. Neither is checked.
- It's a **controlled demonstration** on shared Colab GPUs with tunnel noise, not a benchmark.

### Tough questions

**Q: How do you know the gain is real and not noise?** A: Three different seeds, one reversed order, and consistent cache-reuse ordering. But run 4 didn't reproduce the latency gain, so I only claim a tendency, not a guarantee.

**Q: Why did it fail in run 4?** A: I don't know. I can say what the data hints at (higher ITL, uneven load) and what I'd test (reversed-order repeat, per-replica request counts).

**Q: Why is cache reuse high but latency not lower?** A: Cache cuts prefill, but TTFT also includes queueing. If a hot replica queued requests, queueing could cancel the gain.

**Q: Why not run policies simultaneously?** A: They'd share GPUs and caches and disturb each other. Sequential runs with isolated namespaces are cleaner but allow drift over time.

**Q: Why trust screenshot data for run 1?** A: I don't rely on it alone, and I flag it as rounded.

**Q: Is 60 requests enough?** A: It's small. That's why I describe the results as a demonstration.

---

## Chapter 13: Related work and limits

**Related work (check citations before using).**
- vLLM / PagedAttention (block-based KV cache, prefix caching); SGLang RadixAttention (prefix tree within one engine); Orca (continuous batching).
- Locality-aware load balancing: LARD (Pai et al., 1998) routes requests for the same content to the same back-end unless overloaded, the same idea as prefix-aware routing with an overload check.
- Cache-aware routing across GPUs: Preble, Mooncake, and open-source routers (Kubernetes Gateway API Inference Extension, llm-d, vLLM production-stack, NVIDIA Dynamo, Ray Serve).
- Not claimed: novelty.

**Limits to state openly.**
- [Not done] Failover to another replica.
- [Not done] Explanation of run 4 and of prefix_aware's tail variance.
- [Not done] Prediction-vs-actual agreement over a whole run.
- [Not done] Tuning of α, thresholds (50% / 256 tokens), SLA.
- [Belief] The index is a guess about vLLM's cache.
- The 371 tests use fake or mocked replicas; they prove router logic, not GPU performance.
- Prometheus/Grafana stack never run by me.
- The earlier "failed 6" count on a Dashboard card has an unconfirmed cause.

---

## Chapter 14: The hardest questions, with answers

**1. What is your contribution?**
A readable, tested router that predicts per-replica cache contents using vLLM-compatible chained block hashes, guards with an SLA check, exposes every decision in a live console, and compares four policies on real GPUs. I don't claim a new idea.

**2. If prefix_aware lost in run 4, why present it?**
Because the honest result is mixed: best cache reuse every time, best median latency in three of four, worst load balance and unstable tail. It teaches that cache affinity trades load balance for reuse.

**3. What would you change to fix the uneven load?**
Strengthen the load term: lower the SLA, add a hard cap on queue depth per replica, or break ties by load more aggressively. I haven't implemented or tested these.

**4. Why not just increase cache size?**
Cache memory is limited by the GPU. More tenants or longer prompts always exceed it eventually, and routing decides how well the limited cache is used.

**5. How would this scale to 100 GPUs?**
The index and router would need to be distributed, and the router isn't. Not tested.

**6. What if two replicas have exactly the same match?**
The one with lower queue depth wins; then lower id.

**7. What if the tokenizer in the router differs from vLLM's?**
Hashes won't match vLLM's blocks, so predictions degrade. That's why the router uses the model's own tokenizer and chat template, and warns when none is given.

**8. How do you know the router's cache index isn't stale?**
I don't. It's a belief, updated optimistically and capped by LRU. I compare prediction with vLLM's `cached_tokens` per request and I haven't aggregated this over a run.

**9. Is round robin a strawman?**
It's the default strategy in many load balancers, and it has the perfect balance score. It's a fair baseline, not a straw one.

**10. What did you actually verify on real GPUs?**
Real routing across four T4s, cache effect on nodes, one prediction match (16 vs 16), and four comparison runs. Everything else (371 tests) used fake replicas.

**11. What did you not verify?**
Failover (not implemented), cause of run 4, cause of earlier "failed 6", prediction agreement over a run, vLLM's behaviour after a client disconnect, Prometheus/Grafana stack.

**12. How is your Dashboard different from Grafana?**
My console shows request-level decisions and reasons live; Prometheus stores aggregate time series. I built Prometheus metrics and a Grafana dashboard as optional, and didn't use them in results.

**13. How do you prevent one policy's cache from helping another?**
Per-policy namespace marker at the start of every prompt, state reset between policies, sequential runs. It's isolation by design, not by checking the GPUs' internal cache.

**14. Why 6 tenants and 1,500-token prompts?**
To exceed the 6,000-token cache: 6 × 1,500 = 9,000 tokens, so no single GPU can hold all tenants. The numbers are a design choice, not tuned.

**15. How does the router get GPU temperature?**
From the sidecar, which runs `nvidia-smi`. It isn't used for routing.

**16. What's the circuit breaker's limit?**
It reacts only after 5 consecutive failures, and it doesn't retry the failed request elsewhere.

**17. What happens when the client disconnects?**
Recorded as `client_disconnected`, separate from failures. What vLLM does with the abandoned request is not verified.

**18. Why does least_connections show 1.47 imbalance?**
Queue depth is a noisy, delayed signal, and it can pile requests onto a replica with brief gaps.

**19. How are percentiles computed?**
Linear interpolation over the sorted values.

**20. What is the single most important thing your project shows?**
That cache reuse and latency are linked by mechanism, but cache reuse alone doesn't guarantee lower latency; load also matters, and my results show both sides.

---

## Chapter 15: Glossary and the pitch

**Glossary.**
- **Replica**: one GPU node running the model.
- **Prefill / decode**: reading the prompt / writing tokens.
- **KV cache**: stored results for processed tokens.
- **Block**: 16 tokens, vLLM's caching unit.
- **Prefix caching**: reusing leading blocks.
- **TTFT / ITL / TPOT**: time to first token / gap between tokens / time per output token.
- **p50 / p95**: median / slow-case value.
- **EWMA**: moving average favouring recent values.
- **M/M/c**: queueing model with c servers.
- **SLA**: response-time target (default 3000 ms).
- **Circuit breaker**: stops sending to a failing node.
- **Sidecar**: helper process beside vLLM.
- **Tunnel**: Cloudflare quick tunnel giving a public URL.
- **Namespace**: marker preventing cache sharing between policies.
- **Seed**: fixes the workload's randomness.
- **SSE**: server-sent events (one-way live stream to the browser).

**60-second pitch.**
"I built a router for serving a language model across four GPUs. Normal routers spread requests evenly but ignore the fact that each GPU caches the beginning of prompts it has already processed. My router tokenizes the prompt, hashes it in the same 16-token blocks the engine caches, and sends the request to the GPU that holds the longest matching prefix, unless that GPU is too slow. I implemented four policies, a live console that shows every decision, and a harness that runs identical traffic through each policy on four real T4 GPUs. Prefix-aware had the highest cache reuse in every run and the lowest median time to first token in three of four, but it loaded GPUs unevenly and one run went the other way, so I present it as a demonstration with trade-offs, not a benchmark."
