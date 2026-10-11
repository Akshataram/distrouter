# SwiftServe / distrouter: session handoff

Paste this (or point the next session at it) to resume. It is self-contained.
Anything marked **UNVERIFIED** has not been confirmed by an actual run.

---

## 0. Resume in one paragraph

Repo `Akshataram/distrouter`, branch `claude/friendly-turing-v9y7vj`, pushed and
clean. A block-level prefix-cache-aware LLM router is implemented and tested
(209 tests, ruff and mypy clean). It is verified to work in simulation (77% prompt
reuse vs 53% for baselines) but **has not yet been shown to beat the baselines on
the user's real 4×T4 Colab cluster**: the real `prefix_aware` runs show a
signature (prediction error ≈ cache ratio) meaning its index matched nothing. A
tokenizer bug that causes exactly that was fixed, but **no real run since the fix
has been reported back**, and it is unknown whether the user's laptop pulled it.
The user has a teacher presentation (30 minutes on implementation) and is easily
overwhelmed by jargon.

**First thing to do next session:** ask the user to run, on their laptop,

```bash
cd distrouter && git pull && git log --oneline -1
grep -h "prefix index live" demo_logs/router-prefix_aware.log
```

and paste the result, then re-run the real demo and read the table with section 6.

---

## 1. Repo, branch, environment

| Item | State |
|---|---|
| Repo | `https://github.com/Akshataram/distrouter` |
| Working branch | `claude/friendly-turing-v9y7vj`, in sync with origin |
| Base | cut from `4ce7412`, the tip of `dist/chaos` |
| PR #1 | `dist/chaos` → `main`, **open and unmerged**. `main` has none of this work. **No PR has been opened for this branch** (the user never asked). |
| Checks | `pytest` 209 passed, `ruff check .` clean, `mypy swiftserve` clean |
| mypy scope | only `swiftserve/`; `scripts/` is not type-checked (pre-existing) |
| Python | user is on macOS and uses `python3` / `pip3`. The sandbox uses `python`. |

**Sandbox limits:** no GPU. Outbound network policy **denies `trycloudflare.com`**
(403 on CONNECT), so the assistant **cannot reach the user's Colab nodes** and
cannot run the real demo. The user must run real experiments on their own laptop
and paste output back. The GitHub MCP server disconnects intermittently; plain
`git push` works.

**Attribution lines** for commits follow the latest system reminder in the session
(it has changed model names across the session; use the one most recently given).

---

## 2. The user and how to work with them

- A student; **their grade depends on presenting this to a teacher**, with a
  30-minute implementation walkthrough, a terminal, and four Colab T4 GPUs.
- Prefers **literal, paste-able content in chat** (not "go open the file").
- **Gets overwhelmed by jargon and speed.** When they said "I don't understand
  this shit, explain properly", the fix that worked was: one idea at a time, an
  analogy, then a program that shows it on screen (`scripts/explain.py`). Keep
  explanations plain, short, and tied to something runnable.
- Wants honesty about what is real vs simulated. Never let them claim an
  unverified result to the teacher.

---

## 3. The project in brief

**Problem.** A model must *prefill* (read the whole prompt, slow) before it
*decodes* (writes the answer). vLLM keeps the result in a KV cache and reuses it
when a new prompt starts with identical tokens (prefix caching). With 4 GPUs a
request only benefits if it lands on a GPU that already holds its prefix. The
router decides that.

**Why TTFT.** A cache hit only shortens prefill, so time-to-first-token shows it
and total latency hides it. The original benchmark measured total latency on tiny
prompts, so every policy tied.

**How vLLM caches (the part to get right).** The chat template flattens messages
into one token list; tokens are cut into **16-token blocks** (partial last block
dropped); each block's hash includes its parent's hash (**chained**), so one hash
identifies the whole prefix; lookup walks from block 0 and stops at the first
miss. So matching is **prefix-only** and **block-granular**.

**The router** has no cache. It keeps a *belief* (`PrefixIndex`: block hash →
set of replicas believed to hold it) built with the same hashing, routes to the
longest match, and inserts optimistically after each decision. The gap between its
predicted cached tokens and vLLM's reported `cached_tokens` is the measured
`prediction_error`.

**Thesis from the survey** (`RESEARCH_PLAN_V2.md`): published prefix routers
assume a datacenter; the user's setup (free Colab T4s over WAN tunnels, ephemeral
nodes) has a control plane slower than the data plane, which is the interesting
regime. Not yet exploited beyond the framing.

---

## 4. What exists, by file

### Router (`swiftserve/`)
| File | Role |
|---|---|
| `app.py` | FastAPI router. `chat_completions` (line ~220): validate → SLA header (default 3000 ms) → admission control (256) → filter open circuits → half-open probes → `policy.select_with_prediction` → forward → add headers. Startup probe refuses to start `prefix_aware` if the tokenizer yields < 2 blocks. |
| `policy.py` | `round_robin`, `least_connections`, `swiftserve` (message-level trie), **`prefix_aware`** (block-level). |
| `prefix_index.py` | `block_hashes()` (chained blake2b, full blocks only, model-namespaced) and `PrefixIndex` (hash → holder set, per-replica LRU, `match()`). |
| `tokenization.py` | `HFChatTokenizer` (real, lazy `transformers`), `ByteChunkTokenizer` (no-download fallback, content-derived pseudo-tokens), `_normalize_token_ids()`. |
| `prefix_trie.py` | the **old** message-level trie, still used by `swiftserve`. |
| `proxy.py` | streams upstream bytes unchanged, retries (2), records latency EWMA and circuit outcome after the stream ends. |
| `state.py` | `ReplicaState`: in-flight, scraped load, M/M/c latency estimate, `true_prefix_hit_rate`. |
| `resilience.py` | circuit breaker (5 failures → open, 10 s, doubling to 120 s, 1 half-open probe), admission controller. |
| `metrics_scraper.py`, `metrics.py` | polls `/health` + `/metrics` (incl. `vllm:prefix_cache_hits/queries`, with or without `_total`); Prometheus gauges. |
| `replica_sidecar.py` | transparent proxy with `/chaos/*` (partition, latency, error-rate; kill/restart only with `--supervise`). |

### Scripts
| File | Role |
|---|---|
| `scripts/explain.py` | **Teaching demo.** Real code on 5 toy requests (4-token blocks, word tokens). `--step` pauses on Enter. Reuse: prefix_aware 32 tokens, baselines 0. |
| `scripts/demo.py` | **The 4-policy comparison.** Spawns a router per policy, runs the workload, prints the table. Fake replicas if `--replicas` omitted. Isolates each policy's prefix namespace. Passes `--max-num-seqs` (default 32) to routers. |
| `scripts/benchmark.py` | streaming harness: `parse_sse_stream`, `dual_slo_attainment` (goodput), bootstrap CIs, permutation tests, `run`/`goodput`/`compare` subcommands, `--workload`, `--rate`. |
| `scripts/workloads.py` | deterministic generators: `shared_system_prompt` (Zipf), `long_document_qa`, `sharegpt_multiturn`, `tiny_prompts`, `poisson_arrivals`. |
| `scripts/fake_vllm_stub.py` | fake vLLM with **real block-level LRU caching**, real SSE and `usage.cached_tokens`, opt-in timing model. Labeled SIMULATED everywhere. |
| `scripts/load_test.py`, `chaos_runner.py` | thin load test; chaos scenario runner (pre-existing). |

### Docs and deployment
`RESEARCH_PLAN_V2.md` (survey + redesign), `PRESENTATION_GUIDE.md` (full
walkthrough with evidence), `DEMO_RUNBOOK.md`, `notebooks/COLAB_CELLS.md`
(**paste-in Colab cells**), `notebooks/gpu_node.ipynb`, `DEPLOYMENT.md`,
`ARCHITECTURE.md` (**only has a Phase 0 section; NOT updated for the prefix index,
tokenizer, or `prefix_aware`**).

---

## 5. Commit history (this session, oldest first)

| Commit | What |
|---|---|
| `54c7039` | Phase 0: workloads, streaming metrics, `true_cache_ratio`, `vllm:prefix_cache_*` scraping |
| `f84fdb0`, `c7ed451` | `RESEARCH_PLAN_V2.md` survey and ledger |
| `d00bcff` | recorded defects W14 and W15 (verified against real code) |
| `0494d1c` | block-level `prefix_index.py` + `tokenization.py` |
| `5894f4d` | `prefix_aware` policy; prediction header; `/status` index info |
| `13ccff3` | fake vLLM stub with real block caching |
| `920f9df` | `scripts/demo.py` + `DEMO_RUNBOOK.md` |
| `d6138ab`, `2f9904a`, `2cd2690` | Colab notebook fixes, `COLAB_CELLS.md`, idempotent launch cell |
| `b1fb6af` | **demo: per-policy namespace isolation** (fixes ordering confound) |
| `418bdd8` | **fix `HFChatTokenizer` returning 2 tokens** + startup probe |
| `f1e2b60` | `PRESENTATION_GUIDE.md` |
| `1e6f046` | demo passes `SWIFTSERVE_ASSUMED_MAX_BATCH_SIZE=32` |
| `ece8d51` | `scripts/explain.py` + tests |
| `9551d18` | Colab cells: launch config that actually worked |

---

## 6. Evidence

### Real runs on the user's 4×T4 (all 24 sessions, concurrency 4, 4 policies)

| Run | Condition | round_robin | least_conn | swiftserve | prefix_aware | `prefix_aware` pred. err |
|---|---|---|---|---|---|---|
| 1 | caches NOT isolated (reset failed) | 52.6 | 81.5 | 90.1 | **94.5** | 94.6 |
| 2 | isolated, tokenizer bug present | 52.3 | 60.5 | 44.3 | 48.1 | 48.4 |
| 3 | isolated, post-fix commit **unknown** | 52.3 | 44.2 | 44.2 | 48.1 | 48.4 |

(cache ratio %). Run 1 is monotonic **in run order**: the cluster warming up, not
policies improving. Its apparent 2.1× win was an artifact. After isolation
everything ties. In runs 2 and 3, `prefix_aware`'s prediction error ≈ its cache
ratio, meaning it predicted ~0 cached tokens on every request: **the index matched
nothing**. Real TTFT p50 ≈ 800–950 ms for all (WAN-dominated). `prefix_aware` p95
TTFT ≈ 3.7–3.8 s vs ~1.8 s for the others.

### Simulation (real policy classes, real workload, 4 replicas × 375-block LRU, concurrency 4, byte tokenizer)

| policy | cache ratio | pred. error | max:mean | per replica |
|---|---|---|---|---|
| round_robin | 52.9 | 52.9 | 1.00 | 6,6,6,6 |
| least_connections | 52.9 | 52.9 | 1.00 | 6,6,6,6 |
| swiftserve | 52.5 | 52.5 | 1.00 | 6,6,6,6 |
| **prefix_aware** | **77.3** | **0.0** | 1.83 | 11,4,2,7 |

Workload ceiling = **77.6%** (first request per distinct prompt is cold; ~40-token
user turn never cached; only **5 of 6** configured prompts are drawn with seed 1:
sessions 11/7/2/2/0/2).

**Simulator validated:** simulated round-robin 52.9% vs real 52.6 / 52.3 / 52.3%.

### SLA-gate sensitivity (working index, simulated)

| EWMA latency | batch capacity floor | cache ratio |
|---|---|---|
| 800 ms | any | 76.7 |
| 1300 ms | 1 | 64.6 |
| 2000 ms | 1 | 52.5 (= round-robin) |
| 1300 / 2000 ms | 4+ | 76.7 |

---

## 7. Diagnosis logic (so it can be reused)

- **prediction error ≈ cache ratio** ⇒ router predicted ~0 ⇒ **index empty/matching
  nothing** (tokenizer/index problem).
- **low cache ratio but low prediction error** ⇒ index fine, the **SLA gate vetoed**
  matches (W8) and the router routed to non-holders correctly predicting 0.
- The demo completing normally with the broken signature, when the fixed code has a
  startup probe that would crash on a bad tokenizer, **suggests the user's laptop
  was running old code**. Not confirmed.

---

## 8. Bugs found and fixed (worth telling the teacher)

1. **Ordering confound.** Policies ran sequentially against shared caches and resets
   failed. Fixed by per-policy prefix-namespace isolation (`isolate_workload`).
2. **Silent tokenizer bug.** New `transformers` returns a `BatchEncoding`;
   `list()` on it yields its keys (`['input_ids','attention_mask']`) = 2 "tokens" <
   block size 16 ⇒ zero blocks ⇒ index never matches ⇒ `prefix_aware` degenerates
   to least-loaded while reporting 24/24 success. Fixed with `return_dict=False`,
   `_normalize_token_ids`, and a startup probe.
3. **W14.** Message-count gate (`_MIN_SHARED_PREFIX_MESSAGES=2`) rejected the single
   most valuable signal (one long shared system prompt = depth 1). Replaced in
   `prefix_aware` by ratio OR absolute-token threshold. (`swiftserve` unchanged.)
4. **vLLM refuses to start** if `--max-model-len` > `KV_BLOCKS × 16`. 8192 vs 6000
   crashed the node; now 4096 with an assert.

---

## 9. Open issues, in priority order

1. **Re-run the real demo and read it** (section 0). Nothing proves the real-GPU win.
2. **W8: batch-capacity floor defaults to 1.** Mitigated only inside `demo.py`
   (passes 32). The router default is unchanged and still wrong for real use.
3. **Index cap vs engine cap.** `PrefixIndex` defaults to 20 000 blocks/replica but
   the engine is capped at 375, so the router can believe blocks survive that were
   evicted. Consider `SWIFTSERVE_INDEX_MAX_BLOCKS=375` for the demo. **Not done,
   not tested on real hardware.**
4. **Goodput SLO (500 ms TTFT) is below the WAN floor** (~800 ms even for hits), so
   real goodput (17–33%) reflects network distance. Raise the SLO or compare only
   relatively.
5. **W9 stale-owner bug** in `SwiftServePolicy` (`next()` takes list order, not most
   recent) and **W4 static membership** (replica list fixed at router start; a
   restarted Colab node needs a router restart). Both pending.
6. **Statistics:** demo is one seed, sequential policies. `benchmark.py` has
   bootstrap CIs and permutation tests but the multi-seed paired run was never done.
7. **`ARCHITECTURE.md`** lacks the prefix index / tokenizer / `prefix_aware` sections.
8. `swiftserve` baseline is intentionally unchanged (still message-level), so it
   stays a valid ablation point.

### `RESEARCH_PLAN_V2.md` phases

| Phase | Status |
|---|---|
| Phase 0 (measurement, workloads) | **done** |
| D (block index, tokenizer) | **done**, plus `prefix_aware` policy |
| A (bug fixes W8/W9), B (dynamic membership, φ-accrual), C (interleaved evaluation), E (RTT/staleness cost model), F (ski-rental replication, hedging), G (Bloom digests), H (offline-optimal bound, cache-size sweep), I (late binding), J (rewrite ARCHITECTURE) | **not started** |

---

## 10. How to run everything

```bash
# Tests (no GPU)
python -m pytest -q && ruff check . && python -m mypy swiftserve

# Teaching demo (no GPU, no Colab) - best thing to rehearse
python3 scripts/explain.py --step

# Rehearsal of the full comparison with fake replicas (no GPU, ~1 min)
python3 scripts/demo.py --policies round_robin,least_connections,swiftserve,prefix_aware --kv-blocks 375

# REAL run: first start 4 Colab nodes (notebooks/COLAB_CELLS.md), then on the laptop:
pip3 install -r requirements.txt transformers
export SWIFTSERVE_REPLICAS="https://a.trycloudflare.com,https://b...,https://c...,https://d..."
python3 scripts/demo.py --replicas "$SWIFTSERVE_REPLICAS" \
  --model Qwen/Qwen2.5-3B-Instruct --tokenizer Qwen/Qwen2.5-3B-Instruct \
  --policies round_robin,least_connections,swiftserve,prefix_aware \
  --num-apps 6 --sessions 24 --turns 1 --system-prompt-tokens 2000 --max-tokens 16
```

Healthy real-run signs: router log has
`prefix index live: tokenizer=hf:Qwen/..., probe produced N tokens = M blocks`
with M ≈ 120+; `prefix_aware` prediction error **well below** its cache ratio;
cache ratio ~65–77% vs ~50% for baselines.

### Colab (per account, 6 cells in `notebooks/COLAB_CELLS.md`)
Cell 1 config (`REPLICA_TAG`, `KV_BLOCKS=375`) → 2 clone `--branch` + `pip install vllm`
→ 3 launch vLLM alone with live progress → **3b** sidecar → 4 self-test (cold vs warm)
→ 5 tunnel + keep-alive. Model **Qwen/Qwen2.5-3B-Instruct**.

---

## 11. Colab operational lessons

- "Too many sessions" ⇒ use **separate Chrome profiles**, one per Google account
  (tabs share one account).
- `address already in use`: a previous attempt is still alive. Cell 3 now kills
  stale `replica_sidecar`/`vllm` processes first.
- **Do not press stop** on a running cell: Colab's interrupt kills child
  processes (it killed the user's sidecar).
- Needed flags: `--enable-prompt-tokens-details` (else `cached_tokens` never
  reported), `--block-size 16` (must equal `SWIFTSERVE_BLOCK_SIZE`),
  `--num-gpu-blocks-override 375` (else nothing evicts and all policies tie),
  `--max-model-len` ≤ blocks×16.
- ~90 min idle disconnect (background compute does not count) and 12 h cap. Quick
  tunnel URLs change on restart, and `SWIFTSERVE_REPLICAS` is read once.
- Sidecar runs **without `--supervise`**, so `/chaos/kill` returns 400.

---

## 12. Decisions and why

- New policy name (`prefix_aware`) rather than editing `swiftserve`, so baselines stay
  comparable (the plan's own rule).
- Per-policy namespace isolation rather than relying on cache reset (vLLM's reset
  endpoint is not always present).
- Cap the KV cache (375 blocks ≈ 0.5× the 6×2000-token working set) so there is real
  eviction pressure.
- Qwen2.5-3B: real prefill cost on a T4 yet fits.
- Bloom-filter digests (Summary Cache) chosen over exact ZMQ KV events for the future
  exact-state phase, because event streams suit a LAN, not flaky tunnels.

---

## 13. Do not claim

- That `prefix_aware` beats the baselines on the **real** GPUs (not yet shown).
- That the router "knows" the cache (it predicts, optimistically).
- Statistical significance (one seed, sequential).
- Simulator numbers as real (they are labeled SIMULATED).
- Scale conclusions from 4 nodes.

## 14. Honest, strong things the user can say

Real vLLM/Qwen on four real T4s over real tunnels; real TTFT from a real token
stream and `cached_tokens` from the engine; a working block-level index verified by
tests and a simulator that matches real round-robin to within 0.6 points; a
measured prediction-error metric; and the self-found confound and silent tokenizer
bug.
