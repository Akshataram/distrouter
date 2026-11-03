# Every setting: what it does, where its value comes from, and how it works inside

Tags: **[UI]** a field in the console, **[body]/[header]** how it is sent in the request, **[CLI]** a command-line flag, **[env]** an environment variable the router reads (`swiftserve/config.py`), **[notebook]** a value in the Colab notebook. Defaults are read from the code. Engine-internals explanations describe how vLLM and language models generally work; they were not re-verified against vLLM's source in this session.

---

## A. What you set per request (Playground, Load test, Compare)

### A1. `max_tokens`  [UI field, sent in the JSON body]
- **Defaults:** Playground 64, Load test 24, Compare 16. Whatever you type is sent as `max_tokens`.
- **What it means:** the **maximum number of tokens the model may generate** for the answer. It doesn't count the prompt.
- **How it works inside:** generation is a loop. The model produces **one token per step**. After every step the engine checks whether to stop:
  1. the model produced its end-of-sequence token (a natural finish), or
  2. a stop string was matched, or
  3. **the number of generated tokens reached `max_tokens`**, or
  4. prompt + generated tokens reached the engine's `--max-model-len` (4096 on your nodes).
  With `max_tokens = 10`, the loop runs at most 10 steps and then stops, even mid-sentence. The response then says `finish_reason: "length"` (a natural finish says `"stop"`). The model does **not** try to write a shorter answer; it is simply cut off.
- **Effect on the numbers you see:**
  - `completion_tokens` in the usage report is ≤ `max_tokens`.
  - **TTFT is unchanged** (the first token comes at the end of prefill regardless).
  - **Total time ≈ TTFT + (tokens − 1) × ITL.** More tokens means more decode steps and a longer total.
  - With only 1 token there is no gap to measure, so **ITL and TPOT show n/a** (they need at least 2 text events).
- **Constraint:** prompt tokens + `max_tokens` must fit in `--max-model-len` (4096). A 1,617-token prompt with `max_tokens` 24 is fine.

### A2. `temperature`  [UI, body] default 0.7
- **What it means:** how random the word choice is.
- **How it works inside:** at each step the model outputs a score (logit) for every token in its vocabulary. These scores are divided by the temperature and turned into probabilities (softmax). **Low temperature** makes the top token dominate; at **0** the engine takes the single most likely token (greedy, deterministic). **High temperature** flattens the probabilities, so less likely words get picked more often. The next token is then **sampled** from those probabilities.
- **Effect:** wording and repeatability only. It costs the same compute per token, so **latency is not affected**, and it doesn't change the prefix cache, which depends only on the prompt tokens.

### A3. `top_p`  [UI, body] default 1
- **What it means:** "nucleus" sampling limit.
- **How it works inside:** after temperature, tokens are sorted from most to least probable, and only the smallest set whose probabilities add up to at least `top_p` is kept; the rest are discarded, and the probabilities are renormalised before sampling. `top_p = 1` keeps everything. `0.9` drops the unlikely tail.
- **Effect:** wording only, no latency or cache effect.

### A4. Streaming  [UI; sets `stream` in the body]
- **What it means:** whether the answer is delivered **as it is generated**.
- **How it works inside:** with `stream: true`, the engine sends each newly decoded token (or a small group) immediately as a server-sent event, a line like `data: {...delta.content...}`, and finishes with `data: [DONE]`. The router also adds `stream_options: {include_usage: true}`, which makes vLLM append a **final chunk with the usage report** (prompt tokens, completion tokens, cached tokens). With `stream: false`, the engine waits until the whole answer is done and returns one JSON object.
- **Effect:** with streaming off, the router can't see when the first token appeared, so **TTFT, ITL and TPOT are n/a**. This is why every demo uses streaming on.

### A5. System prompt and Prompt  [UI → the `messages` list in the body]
- **What they mean:** the `system` message (standing instructions) and the `user` message (the question). Sent as `[{role: system, content: …}, {role: user, content: …}]`.
- **How it works inside:** the engine applies the model's **chat template** to turn the list into one text with role markers (for Qwen, markers like `<|im_start|>system … <|im_end|>`), then **tokenises** it into token ids. **Prompt tokens** in the usage report is the size after that, so it is a bit more than the words you typed. If you send no system message, Qwen's template typically adds a short default one, so even a one-line question has a few dozen tokens. I did not check this for your exact template version.
- **Why it matters here:** the cache is keyed on those **leading tokens**. The router applies the same template and tokeniser, so its blocks line up with vLLM's.

### A6. Model name  [UI, body]
- **What it means:** which model to call. Filled from the router (`--model`, or read from the first node's `/v1/models` if omitted).
- **How it works inside:** each vLLM node serves **one model**, and the request's `model` must match that served name, or vLLM returns an error. The router passes it through unchanged.

### A7. SLA ms  [UI; sent as the `X-SLA-Ms` header]
- **What it means:** the response-time target for this request. Blank means the router default, **3000 ms** (`SWIFTSERVE_DEFAULT_SLA_MS`).
- **How it works inside:** it is used in two places, and it is **not a timeout**:
  1. **Routing:** swiftserve and prefix_aware compare a candidate GPU's **estimated latency** with it. If the estimate is over, the match is skipped (`sla_veto` in prefix_aware).
  2. **Accounting:** after the request, the router marks it `sla_violated` if it failed or its measured latency exceeded the target, and counts it in its Prometheus metric `swiftserve_sla_violations_total`. The console doesn't show that metric.
- A non-numeric header gets HTTP 400. The separate hard timeout is `SWIFTSERVE_REQUEST_TIMEOUT_S` = 120 s.

### A8. Session id  [UI; sent as the `X-Session-Id` header]
- **What it means:** names a conversation. Blank means a random one per request.
- **How it works inside:** the router uses the header, else the body's `user` field, else a fresh random id. Swiftserve looks up which replica served this session within the last 600 s (`SWIFTSERVE_CACHE_TTL_S`) and prefers it, unless it is over the SLA. Prefix_aware records it but doesn't depend on it. Round robin and least connections ignore it.

### A9. Send, Abort in-flight, Stop  [UI]
- **How they work inside:** Abort closes the browser's connection (an `AbortController`). The router notices the disconnect, closes its own upstream connection and records `client_disconnected`. What vLLM does with the abandoned request afterwards was not verified.

---

## B. What you set for a Load test or Compare run

| Setting | Meaning and how it works |
|---|---|
| **Requests** | Total number to send. A browser loop launches them one by one. |
| **Concurrency** | The maximum number of requests in flight at once. The loop waits until fewer than that are running before launching the next. **Caution:** Chrome allows only about 6 simultaneous HTTP/1.1 connections to the same host, and the console's live event stream permanently holds one. The effective concurrency from the browser may therefore be lower than 8 (about 5). I have not measured this. Check the Dashboard's "in flight now" during a run. |
| **Delay between launches (ms)** | A pause between starting requests, to spread out arrivals. |
| **Prompt source** | *Common:* the same user prompt for every request. *List:* one prompt per line, cycled in order. |
| **Shared system prompt** | A system message added to every request, so they share a prefix. |
| **Insert sample ~1500-token system prompt** | The page generates numbered "Rule n: …" lines until the text reaches about 6,000 characters (tokens × 4). This is a rough 4-characters-per-token estimate, so the real token count differs a little. |
| **Tenants** (Compare) | The number of distinct system prompts, each standing for a different customer company. With 6 tenants × about 1,500 tokens, the set is bigger than one node's 6,000-token cache. |
| **System prompt tokens** (Compare) | The approximate length of each tenant's prompt (same 4-characters-per-token rule). |
| **Seed** (Compare) | Starts a small deterministic random generator that chooses each request's tenant and question. The same seed gives the identical request list for every policy. |
| **Policies / Reverse the policy order** | Which policies run, and in what order. Reverse is a check for order effects. |
| **Namespace** | A marker `[ns:<run>-<policy>]` put at the very start of every prompt. It changes the first block, so every chained hash after it changes too, and no policy can reuse blocks another policy created. |

---

## C. Settings on each GPU node (the Colab notebook)

| Setting | Meaning and how it works inside |
|---|---|
| `--model Qwen/Qwen2.5-3B-Instruct` [notebook `MODEL`] | The model vLLM downloads and serves. A 3B model in FP16 fits in a T4's 15 GB. |
| `--port 8000` | Where vLLM listens inside the node (the sidecar forwards to it). |
| `--enable-prefix-caching` | Turns on reuse of identical leading blocks across requests. Without it every prompt is recomputed. |
| `--enable-prompt-tokens-details` | Makes vLLM report `usage.prompt_tokens_details.cached_tokens`, the source of the cache reuse number. |
| `--block-size 16` | The cache unit: 16 tokens per block. The router's `SWIFTSERVE_BLOCK_SIZE` must equal it, or the router's hashes match nothing. |
| `--max-num-seqs 32` | The most requests vLLM runs together in one batch. More requests wait in its queue. |
| `--max-model-len 4096` | The longest prompt + answer one request may have. It must not exceed the KV cache capacity (375 × 16 = 6,000), or vLLM aborts at start-up. |
| `--dtype half` | 16-bit floating point numbers. The T4 doesn't support bfloat16. |
| `--num-gpu-blocks-override 375` [notebook `KV_BLOCKS`] | Forces the KV cache to **375 blocks = 6,000 tokens**, instead of using all free memory. This creates the cache pressure the experiment relies on. |
| `--gpu-memory-utilization 0.90` | The fraction of GPU memory vLLM may claim (weights, working memory and cache). With the override, the cache part is capped. |
| Sidecar `--port 9100`, `--upstream-url`, `--admin-token` | Where the sidecar listens, where it forwards to, and the token protecting its fault-injection endpoints. |

---

## D. Router settings (`swiftserve/config.py` defaults; `scripts/dev.py` flags set some)

| Setting | Default | Meaning |
|---|---|---|
| `SWIFTSERVE_POLICY` / `--policy` | swiftserve | Which policy starts active |
| `SWIFTSERVE_DEFAULT_SLA_MS` | 3000 | Default response-time target |
| `SWIFTSERVE_CACHE_TTL_S` | 600 | How long a session's affinity counts |
| `SWIFTSERVE_SCRAPE_INTERVAL_S` | 2 s | How often the router reads each node's vLLM `/metrics` |
| `SWIFTSERVE_HEALTH_INTERVAL_S` | 5 s | How often it checks each node's `/health`. This drives the healthy/unreachable badge. (Earlier I said 2 s for this; the config says 5 s.) |
| `SWIFTSERVE_REQUEST_TIMEOUT_S` | 120 | Hard timeout for one request |
| `SWIFTSERVE_CIRCUIT_FAILURE_THRESHOLD` | 5 | Consecutive failures before a replica's circuit opens |
| `SWIFTSERVE_CIRCUIT_RESET_S` / `_MAX_RESET_S` | 10 s / 120 s | Cooldown before a probe, doubling up to the maximum |
| `SWIFTSERVE_MAX_IN_FLIGHT` | 256 | Admission cap; more are rejected |
| `SWIFTSERVE_PROXY_MAX_RETRIES` / base delay | 2 / 0.1 s | Connect-error retries, always on the same replica |
| `SWIFTSERVE_ASSUMED_MAX_BATCH_SIZE` / `--max-num-seqs` | 1 (router default); you pass 4 | The batch capacity the router assumes per node (raised automatically if it sees more running) |
| `SWIFTSERVE_TOKENIZER` / `--tokenizer` | empty | The tokeniser for prefix_aware. Empty uses a byte fallback that doesn't match the engine's blocks. |
| `SWIFTSERVE_BLOCK_SIZE` | 16 | Must equal vLLM's `--block-size` |
| `SWIFTSERVE_INDEX_MAX_BLOCKS` / `--index-max-blocks` | 20,000 (you pass 375) | Per-replica cap on blocks the index believes are cached, imitating eviction |
| `SWIFTSERVE_CACHE_THRESHOLD` | 0.5 | A match counts if it covers at least this share of the prompt's blocks |
| `SWIFTSERVE_MIN_MATCH_TOKENS` | 256 | …or at least this many tokens (either condition is enough) |
| `SWIFTSERVE_PREFIX_TRIE_MAX_DEPTH` | 6 | Message depth swiftserve's trie tracks |
| `SWIFTSERVE_COLD_START_MS_PER_TOKEN` | 0 (off) | Optional penalty for a cold prefix in swiftserve's fallback |
| `SWIFTSERVE_REPLICA_LABELS` / `--labels` | empty | Names you give the replicas; the router doesn't guess GPUs |
| `SWIFTSERVE_ALLOW_POLICY_SWITCH` | off | Lets the Compare tab change policy; `dev.py` turns it on for localhost only |
| `SWIFTSERVE_EVENT_BUFFER` / `_RECORD_BUFFER` | 5000 / 2000 | Sizes of the event and per-request memory |
| `SWIFTSERVE_API_TOKEN` | unset | If set, the router's API requires a bearer token |

---

## E. Constants inside the code (not exposed as settings)

| Constant | Value | Where and meaning |
|---|---|---|
| EWMA α | 0.2 | `state.py`: each new request moves the latency average 20% toward its time |
| Latency seed | 800 ms | the estimate before any request has finished |
| Occupancy buckets | 3 | latency averages kept separately for a nearly-empty, half-full and nearly-full batch |
| Message trie size | 20,000 nodes; wiped when exceeded; 600 s TTL | `prefix_trie.py` |
| Session table | 20,000 per replica | `state.py` |
| Hash size | 8 bytes (blake2b) | `prefix_index.py` |
| Dashboard windows | last 500 requests per replica; 30 s for req/s and tokens/s | `dashboard_api.py` |
| GPU poll | at most every 5 s (sidecar caches for 1 s) | router and sidecar |
| Event stream connection | 30 s cap, then reconnect and resume | `dashboard_api.py` |
