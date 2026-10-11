# Architecture

This document describes the system as it stands after the production-
hardening upgrade: SwiftServe is no longer just a routing policy prototype
sitting in front of 3 co-located replicas -- it is a small distributed
system with real multi-node deployment, a resilience layer, chaos
engineering, and observability, built to withstand the kind of scrutiny a
technical panel would apply ("what happens when a node dies", "how do you
know your policy is actually better", "what would break this in
production").

Nothing here is a simulation of these properties. Where a claim below says
a mechanism is real (a real circuit breaker, a real subprocess kill, a real
second GPU node over a real network), it is backed by a test that
exercises it as such -- see "What's actually tested, and how" at the
bottom, which is the part worth defending line-by-line in front of a
panel.

## Component map

```
                         ┌─────────────────────────────┐
                         │   SwiftServe router (CPU)    │
                         │  swiftserve/app.py            │
                         │  - policy.py (routing)         │
                         │  - resilience.py (breaker/      │
                         │    admission control)            │
                         │  - metrics.py (Prometheus)         │
                         └───────────┬─────────┬─────────┘
                                     │         │
                    real HTTP over the network (localhost in
                    a single-box demo; a public tunnel URL per
                    node in the real multi-node deployment)
                                     │         │
                 ┌───────────────────┘         └───────────────────┐
                 ▼                                                 ▼
     ┌───────────────────────┐                       ┌───────────────────────┐
     │ replica_sidecar.py      │           ...        │ replica_sidecar.py      │
     │ (real process, owns the │                       │ (real process, owns the │
     │  vLLM child via         │                       │  vLLM child via         │
     │  --supervise)            │                       │  --supervise)            │
     │  /chaos/* admin API      │                       │  /chaos/* admin API      │
     └───────────┬─────────────┘                       └───────────┬─────────────┘
                 │ real subprocess                                 │
                 ▼                                                 ▼
     ┌───────────────────────┐                       ┌───────────────────────┐
     │ real vLLM + real Qwen   │                       │ real vLLM + real Qwen   │
     │ (GPU node -- Colab/       │                       │ (GPU node -- Kaggle or  │
     │  Kaggle T4 in this        │                       │  a second Colab account)  │
     │  project's real runs)      │                       │                          │
     └───────────────────────┘                       └───────────────────────┘
```

The router is CPU-only and holds no model state, same as before. What
changed is what sits between it and each replica, and what each replica
actually is.

## 1. Resilience layer (`swiftserve/resilience.py`)

Two independent mechanisms, both pure state machines with no HTTP or vLLM
knowledge of their own -- `swiftserve/proxy.py` and `swiftserve/app.py`
drive them from the real request lifecycle:

**`CircuitBreaker`** -- one per replica (`ReplicaState.circuit`). Trips to
`OPEN` after `N` consecutive failures (`SWIFTSERVE_CIRCUIT_FAILURE_THRESHOLD`,
default 5), where "failure" means either a connect-level exception or an
HTTP 5xx from the replica. While open, the replica is excluded from
routing entirely. After a cooldown (`SWIFTSERVE_CIRCUIT_RESET_S`, doubling
on each repeat trip up to `SWIFTSERVE_CIRCUIT_MAX_RESET_S`), it moves to
`HALF_OPEN` and becomes eligible for a capped number of probe requests.
Critically, **a half-open replica gets first claim on the next eligible
request, overriding whatever the routing policy would otherwise pick**
(`app.py`'s `half_open_probes` check) -- without this, a policy that scores
replicas by estimated latency will keep preferring an already-healthy,
already-warmed-up replica forever, and a recovering node would sit
half-open indefinitely, never actually getting the traffic it needs to
prove it has recovered. This was found empirically while building the
chaos test in `tests/test_chaos_runner.py`, not designed defensively up
front -- the first version of the test never recovered within its timeout,
which is exactly the bug this fix addresses.

**`AdmissionController`** -- a single global in-flight ceiling
(`SWIFTSERVE_MAX_IN_FLIGHT`, default 256). Past it, a request is rejected
immediately with `503` + `Retry-After` rather than being queued: an
unbounded queue under a traffic spike just makes every request wait
longer together until they all time out at once, whereas fast-failing the
requests that would exceed capacity keeps the ones already admitted fast.

Retries live in `proxy.py`, not the breaker: a connect-level failure (the
replica or its sidecar is unreachable) is retried a couple of times with
jittered backoff, but only before any byte has reached the caller -- once
a streaming response has started, a failure propagates instead of retrying,
since replaying a partial reply to the client is wrong regardless of
whether the retry itself would succeed.

**Input validation** on `/v1/chat/completions` rejects malformed input with
a clean `400` before it can reach anything stateful: a non-numeric
`X-SLA-Ms` header, a JSON body that parses but isn't an object (e.g. a bare
array), or a body missing the shape a chat-completion request needs
(checked via a permissive Pydantic model, `messages` must be a list if
present) -- previously these fell through to an unhandled exception and a
raw `500`, and a malformed request could acquire (and then leak) an
admission-control slot before failing. Validation now runs, and fails
fast, before `admission.try_acquire()`. The original raw request bytes are
still what gets forwarded upstream on success -- the Pydantic model is
shape validation only, never a reserialization step, so an extra field
vLLM accepts that the model doesn't know about is never silently dropped.
Symmetrically, `proxy.py` no longer assumes an upstream response is valid
JSON: a broken replica or an intermediate proxy's HTML error page used to
crash the router *after* the circuit breaker had already correctly scored
the outcome; now a non-JSON body is passed through as-is with its real
content-type instead.

**Router API auth** is opt-in via `SWIFTSERVE_API_TOKEN` (unset by
default, matching the original unauthenticated behavior): when set,
`/v1/chat/completions`, `/status`, and `/metrics` require an exact
`Authorization: Bearer <token>` match, checked by a FastAPI dependency
(`_require_api_token`) mirroring `replica_sidecar.py`'s existing
`X-Chaos-Token` pattern -- same "warn loudly on startup if left unset"
behavior as the sidecar's own admin token. `/healthz` stays open
unconditionally, since a load balancer's health probe shouldn't need a
credential.

## 2. Chaos-capable replica sidecar (`swiftserve/replica_sidecar.py`)

Every real GPU node runs a sidecar in front of its vLLM process rather than
exposing vLLM directly. Ordinary traffic passes straight through
unmodified; an authenticated `/chaos/*` admin API (bearer-token gated via
`X-Chaos-Token`) can inject, on that one specific node, over the same
public tunnel already carrying real traffic:

- **partition** -- the sidecar refuses every request with `503` without
  even attempting the upstream, simulating the node dropping off the
  network.
- **error-rate** -- a configurable fraction of requests get a synthetic
  `500` instead of being forwarded.
- **latency** -- a configurable extra delay before forwarding, simulating
  a degraded (not dead) node.
- **kill / restart** -- only available when the sidecar was launched with
  `--supervise "<vllm launch command>"`, in which case it owns the vLLM
  process directly and can send it a real `SIGKILL` and relaunch it. This
  is the closest thing to a real crash a chaos test can safely trigger,
  and it is the same failure mode this project hit *by accident* the first
  time it ran on real GPU hardware (a CUDA/torchaudio mismatch killed vLLM
  instantly) -- the sidecar turns that into something the system is
  deliberately tested against instead of something that happened to be
  found once.

## 3. Multi-node real deployment (`notebooks/gpu_node.ipynb`)

Each real replica now runs as its own OS process, on its own machine,
reachable over the public internet rather than `localhost`: a Colab or
Kaggle free-tier T4 session runs real vLLM + real Qwen weights, fronted by
the chaos sidecar, exposed via a Cloudflare quick tunnel (no account
needed). Running the same notebook in 2-3 separate free accounts and
pointing `SWIFTSERVE_REPLICAS` at their tunnel URLs is what makes this a
genuinely distributed deployment: separate physical GPUs, separate
processes, separate network paths, with real latency and real failure
modes between the router and each node -- not 3 slices of one GPU sharing
a process boundary, as in the project's first working version.

The router itself stays wherever you control it (a laptop is fine -- it's
CPU-only and holds no state that needs to live near the GPUs).

## 4. Observability (`swiftserve/metrics.py`, `deploy/observability/`)

The router exposes its own Prometheus `/metrics` (distinct from the
per-replica vLLM metrics it already scrapes and re-exposes as gauges):
request counts by replica/policy/outcome, an end-to-end latency histogram,
cache-hit and SLA-violation counters, admission-rejection and
all-circuits-open counters, and per-replica queue depth / EWMA latency /
circuit state. `deploy/observability/docker-compose.yml` brings up
Prometheus (scraping the router) and Grafana (pre-provisioned with that
datasource and a dashboard covering all of the above) with zero manual
clicking.

### Request correlation (`swiftserve/request_context.py`)

Every request gets an `X-Request-Id` (accepted from the caller, or minted
by the router's middleware if absent), propagated via a `contextvars`
context rather than threaded through every function signature -- it's set
once per request and read anywhere in that request's async call chain,
including `proxy.py`'s retry/failure log lines several `await`s below the
route handler. The router always forwards it to the chosen replica's
sidecar, which runs the same pattern, so one ID traces a request across
both processes' logs (`[rid=<id>]` in every log line, `X-Request-Id`
echoed in every response). This is a `contextvars`-scoped correlation ID,
not a distributed tracing system (no spans, no propagation to the actual
vLLM process) -- it answers "which log lines belong to this one request",
not "where did the time go inside vLLM".

## 5. Chaos scenario runner (`scripts/chaos_runner.py`)

Orchestrates a full fault-injection scenario against a live deployment:
inject a fault on a target replica's sidecar, drive real traffic through
the router throughout, confirm the circuit actually opens and traffic
actually reroutes to the surviving replicas, clear the fault, and confirm
recovery (with a measured MTTR). This is what turns "the circuit breaker
class has unit tests" into "the deployed system survives a replica dying"
-- see below for how it's tested without needing a live multi-node cluster
on hand for every CI run.

## 6. Statistically rigorous benchmarking (`scripts/benchmark.py`)

`scripts/load_test.py` (kept, unchanged) gives a single-shot number for one
run. A single run is not evidence: scheduling jitter alone can make
round-robin look better or worse than SwiftServe on any given try, which is
exactly what the project's own earlier honest finding (SwiftServe roughly
tied with least-connections, not a clean win) depends on being able to
tell apart from noise. `benchmark.py` runs multiple independent trials
(different random seeds) per policy, reports a bootstrap 95% confidence
interval on mean latency (and on cache-hit / SLA-violation rate) instead
of a point estimate, and a permutation test for whether an observed
difference between two policies' trial means is distinguishable from
chance (`compare` subcommand). No `scipy` dependency -- both the bootstrap
and the permutation test are implemented directly (a percentile bootstrap
over per-trial means; a label-shuffle test comparing how often a random
split of the pooled data reproduces a difference at least as extreme as
the one observed).

**Tail latency percentiles (p50/p90/p95/p99) are bootstrapped and
sample-size-gated, not just indexed off a sorted list.** The earlier
version computed p95/p99 from however many requests a run happened to
produce, with no indication that a "p99" from 60 requests is really just
the single largest observation dressed up with a decimal point. Now: each
percentile gets its own bootstrap 95% CI (same `bootstrap_ci` machinery as
the mean, applied with a percentile `stat_fn`), and `summarize_percentile`
flags `reliable: False` whenever the sample count is below a documented
minimum for that percentile's tail (`min_samples_for_percentile`: roughly
"need ~10 observations past the tail", so p50 wants >=20, p90 >=100, p95
>=200, p99 >=1000). `print_comparison_table` marks an unreliable cell with
`*` rather than hiding the number -- the point is honesty, not silence.
`load_test.py` (the lightweight single-shot tool) applies the same
sample-size flag inline (`format_percentile`) without the bootstrap, since
adding CI machinery to the "quick check" tool would contradict its own
purpose.

**Goodput@N (`scripts/benchmark.py goodput`)**, DistServe's metric
(arXiv:2401.09670): the highest request rate a policy sustains while at
least N% of requests both succeed and meet their SLA. This answers a
different question than everything above it -- "how much *load* can the
system take before it falls over", not "how fast is a *typical* request
at whatever load you happened to test." `run`/`compare` use a
*closed-loop* generator (a fixed number of concurrent session workers);
under overload, offered load quietly self-throttles to match whatever the
system can keep up with, which makes "requests per second" an artifact of
`--concurrency`, not a real measurement. `goodput` instead uses an
*open-loop* generator (`run_open_loop_arrivals`): new sessions arrive as a
Poisson process at a configured target rate, independent of how fast
earlier ones finish, so a system that can't keep up actually shows it
(SLA violations, not just a lower observed rate). `goodput` sweeps a list
of target RPS levels (`--rps-levels 5,10,15,20,25,30`), measures
`sla_attainment` (fraction of *offered* requests -- including dropped
connections and non-200s, not just the ones that happened to complete --
that both succeeded and met the SLA) at each, and `find_goodput` reports
the highest *tested* RPS whose measured attainment cleared the target
(`--sla-target 0.9` = Goodput@90) -- never an interpolated guess between
tested points, and honestly `None` if even the lowest level tested
already fails. `goodput-compare` prints several saved sweeps side by side
-- the actual DistServe-style headline number: how much more load does
`swiftserve` sustain than `round_robin` before SLA compliance drops.

Fixed alongside this: `sla_violated` (both benchmark scripts) and the
router's own `SLA_VIOLATIONS_TOTAL` metric used to be computed from
elapsed time alone, so a fast `503` (an admission-control rejection)
counted as "met SLA" purely because it came back quickly. Both now
require an actual `200` in addition to being within the latency budget --
meeting an SLA means the request succeeded, not just that some response
arrived fast.

## 7. Batching-aware latency model (`ReplicaState.estimate_latency_ms`)

The project's own earlier honest finding was that `estimate_latency_ms()`
modeled each replica as a single-server serial queue (`(queue_depth + 1) *
ewma_latency`, i.e. M/M/1) while real vLLM does continuous batching --
processing several requests concurrently in the same forward passes -- so
the estimate over-triggered SLA-based rerouting under concurrency. This is
now fixed by generalizing the model to M/M/c: below the replica's
continuous-batching capacity `c`, a new request runs alongside the others
at roughly its own service time (no queueing penalty at all); at or above
capacity, it genuinely queues, and slots free up at rate `c` rather than 1,
giving `ewma_latency * (queue_depth + 1) / c`. Setting `c = 1` reproduces
the exact original formula, so this is a strict generalization, not a
behavior change, until real concurrency is actually observed.

`c` (`ReplicaState.effective_batch_capacity()`) is deliberately not a
number you have to know in advance (vLLM's real usable concurrency for a
given model/GPU/sequence-length combination is hard to predict from
`--max-num-seqs` alone, since it also depends on KV-cache memory
pressure): it is the highest concurrency this replica has actually been
observed running, a high-water mark updated from the same scraped vLLM
`/metrics` this project already pulls (`ReplicaState.record_scrape`),
seeded by a configurable floor (`SWIFTSERVE_ASSUMED_MAX_BATCH_SIZE`,
default 1) and never allowed to shrink once raised -- real capacity
doesn't disappear because load happened to be low the last time SwiftServe
scraped.

**This simplification is now fixed:** `ewma_latency_ms` used to be a
single flat per-request service time regardless of current batch
occupancy, even though per-token generation genuinely does slow down
somewhat as more sequences share GPU compute and memory bandwidth
concurrently. Each replica now tracks a separate EWMA per *occupancy
bucket* (`ReplicaState._bucket_latency_ms`, bucketed by how full the batch
was -- as a fraction of `effective_batch_capacity()` -- at the moment a
request was dispatched, not at completion), and `estimate_latency_ms()`
reads the bucket matching the occupancy a new request would actually land
into rather than one flat average across all of them. A bucket with no
observations yet falls back to the replica's overall `ewma_latency_ms`, so
with little data this collapses back to exactly the old flat estimate --
the per-bucket model only sharpens the estimate once real per-occupancy
data exists, matching the same "never look more confident than the data
supports" discipline as `effective_batch_capacity`'s high-water mark. What
it still doesn't do: fit a continuous latency-vs-occupancy curve (it's 3
discrete buckets, not a regression), and it doesn't yet account for
per-request generation length (`max_tokens`) as a second factor alongside
occupancy -- both are reasonable next refinements, not silently assumed
away.

## 8. Cross-session prefix-tree routing (`swiftserve/prefix_trie.py`)

`ReplicaState.has_warm_cache` (per-session cache affinity) only helps a
*single session's own* later turns -- it's keyed by `session_id`, so two
different users whose conversations happen to open with the same system
prompt or few-shot template get no benefit from each other's warm cache,
even though vLLM's own `--enable-prefix-caching` would happily reuse it if
routed to the same replica. `PrefixCacheTrie` closes that gap: a trie over
the exact `(role, content)` sequence of a conversation's messages
(`record`/`longest_match`), tracking which replica most recently served
each prefix, independent of `session_id`. `SwiftServePolicy.select()` now
checks it as a second signal, after per-session affinity and before the
load-based fallback: if some *other* session's messages share at least
`_MIN_SHARED_PREFIX_MESSAGES` (2) messages with this one, and the replica
that served them is still within SLA, route there.

A few honesty notes on what this trie actually is:

- **Exact-content match only**, no fuzzy or semantic similarity. That's
  deliberate, not a limitation: a paraphrased system prompt tokenizes
  differently and gets a completely different KV cache in vLLM, so
  "close enough" matching here would be a real false claim about cache
  locality, not just an imprecise one.
- **Depth-capped** (`SWIFTSERVE_PREFIX_TRIE_MAX_DEPTH`, default 6):
  matching stops after the first few messages, since only early turns
  (system prompts, few-shot examples) are realistically shared verbatim
  across independent conversations -- later turns diverge per-session by
  construction.
- **Bounded, not LRU-evicted**: past `max_nodes` (default 20,000), the
  whole trie resets rather than evicting individual stale leaves. It's
  soft optimization state, not correctness-critical (a wrong or missing
  match just means a cache-cold route, not an incorrect response), so a
  full reset is a simpler and safer tradeoff than getting per-node
  eviction right on a tree whose nodes are shared across many sessions'
  prefixes.
- **Still just a routing hint, not a KV-cache index.** Same caveat as
  per-session affinity: SwiftServe never touches vLLM's actual KV cache.
  See "Where this sits relative to DistServe / MoonCake / Preble / llm-d"
  below for exactly what would be needed to go further than a hint.

## 9. Recompute-cost-aware fallback (`ReplicaState.estimate_cold_start_penalty_ms`)

MoonCake and Preble both make the same underlying point: a cache miss
isn't free, and its cost scales with how much prior context has to be
recomputed. SwiftServe can't move KV bytes between replicas (see below),
but its *routing* decision can still weight that cost. When neither
per-session affinity nor the prefix trie produces a usable candidate, the
fallback now ranks replicas by `queue_depth()`, then by
`estimate_latency_ms() + estimate_cold_start_penalty_ms(prefix_size_tokens)`
rather than load alone -- `prefix_size_tokens` is a rough `chars/4`
estimate of the conversation-so-far (no real tokenizer dependency, precise
enough to rank candidates against each other, not to bill by the token),
and the penalty is `prefix_size_tokens * cold_start_ms_per_token`.

`SWIFTSERVE_COLD_START_MS_PER_TOKEN` defaults to `0.0` -- **off**, a
strict opt-in that changes nothing until a deployer sets it. This is
deliberately a configured constant, not self-calibrated from observed
data the way `effective_batch_capacity` is: isolating "extra latency from
a cold prefix" from "extra latency from current load" isn't something
SwiftServe can cleanly observe per-request with the telemetry it already
collects, so auto-learning this rate would be presenting a guess with
more confidence than the data actually supports -- exactly the kind of
overclaiming this project avoids everywhere else. A deployer who has
actually measured their own replicas' prefill throughput can set it from
that.

## 10. Phase 0: measurement and workloads that can actually show a cache-routing gain

Before this change, every benchmark run -- `scripts/benchmark.py`,
`scripts/load_test.py`, the numbers in this doc -- ran against 5 tiny
canned prompts (~15 tokens, no system prompt) with `max_tokens=128`, and
measured only end-to-end latency. That workload has essentially nothing
to cache (P1), and e2e latency mixes generation time (which caching never
speeds up) in with prompt-processing time (which caching does speed up),
hiding whatever gain a cache-aware policy actually produces (P2). It's not
that `swiftserve` failed to beat `least_connections` on that workload --
it's that *no routing policy could have*, on a workload with no shared
prefix structure. Phase 0 (of a larger routing-upgrade plan; see the
DistServe/Preble/MoonCake framing below, which this phase directly
motivates) fixes the measurement, not the routing.

**`scripts/workloads.py`** (new, shared by `benchmark.py` and
`load_test.py`, replacing both scripts' copy-pasted `PROMPTS`/`run_session`):
four deterministic generators. `shared_system_prompt` (Preble's own
workload shape) assigns each of N sessions to one of a handful of
long-system-prompt "apps" via Zipf-skewed popularity, so a cache-aware
policy has real cross-session shared prefixes to route on.
`long_document_qa` is DistServe's own shape: many questions against the
same long document. `sharegpt_multiturn` loads a real local ShareGPT-format
trace when one is available (no synthetic substitute for real conversation
shape). `tiny_prompts` is the original 5-prompt workload, kept verbatim as
a regression baseline -- `--workload tiny` (the default) is intentionally
unchanged behavior. The one subtlety that keeps prefix caching honest:
turn 0 sends the full seed messages, but turn N>0 sends *only* the new
user message, never a pre-scripted assistant turn -- the request runner
appends each turn's real, live-streamed assistant reply before sending the
next turn, so the bytes vLLM actually sees match what it actually
generated and cached (a scripted fake reply would silently break the real
prefix match after turn 0).

**Streaming + real per-request metrics** (`scripts/benchmark.py`): every
request now always sets `"stream": true` with
`"stream_options": {"include_usage": true}` (P2's actual fix). A new pure
function, `parse_sse_stream`, turns the raw SSE lines into `ttft_ms` (time
to first content token), `tpot_ms` (time between subsequent tokens), and
the usage fields vLLM reports in its final chunk -- `prompt_tokens`,
`completion_tokens`, and `prompt_tokens_details.cached_tokens` (null
treated as 0, since not every vLLM build/flag combination reports it).
`summarize_policy` gained TTFT p50/p90/p99 and TPOT p50/p99 (bootstrap
CI'd, same as existing e2e percentiles), plus three metrics that didn't
exist before because nothing fed them real data:

- **`goodput_rate`** (`dual_slo_attainment`): DistServe's actual
  definition of goodput -- the fraction of requests meeting *both* a TTFT
  SLO and a TPOT SLO, not just a single end-to-end number (P7).
- **`true_cache_ratio`**: Σ`cached_tokens` / Σ`prompt_tokens`, straight
  from vLLM's own reported usage -- addresses P3 directly. SwiftServe's
  `X-SwiftServe-Cache-Hit` header is still only ever the router's
  *prediction* (it can be wrong: vLLM may have evicted those blocks under
  memory pressure since the router last routed there); this is the number
  to check that prediction against, and Phase 0 wires the comparison
  without yet changing what predicts it.
- **`prediction_error`**: mean `|predicted_cached_tokens - cached_tokens|
  / prompt_tokens`. `X-SwiftServe-Predicted-Cached-Tokens` is wired
  end-to-end (client records it, `_build_trial` computes the error) but
  the router doesn't populate it yet -- it defaults to 0, so this number
  is currently just "how much true_cache_ratio differs from zero," which
  is honest, not a real prediction-accuracy result. A later phase that
  adds an actual cache-size predictor makes this number meaningful; Phase
  0 only wires the plumbing.

Also new: **`load_imbalance`** (max/mean requests-per-replica, pooled
across trials) and **`--rate`** on `benchmark.py run` (each seed becomes
one open-loop Poisson-arrival window over the chosen workload's sessions,
reusing the same arrival-generation machinery `goodput` already had,
instead of only closed-loop concurrency). `compare`'s permutation test now
runs pairwise on TTFT and goodput-rate in addition to e2e latency
(generalized into one `_print_pairwise_permutation` helper instead of
three copy-pasted blocks).

**`swiftserve/metrics_scraper.py`** now also parses
`vllm:prefix_cache_hits`/`vllm:prefix_cache_queries` (optional `_total`
suffix -- the spelling differs across vLLM versions), and
`ReplicaState.true_prefix_hit_rate` exposes their ratio in `/status` and as
a new Prometheus gauge (`swiftserve_replica_true_prefix_hit_rate`, only
set when there's real query volume -- never faked to 0). This is vLLM's
own ground truth for whether *its* prefix cache is actually being hit,
independent of and complementary to `true_cache_ratio` above (that one is
per-request, from `usage`; this one is cumulative, from vLLM's own
counters) -- two independent checks on the same prediction.

**Deployment**: `DEPLOYMENT.md` and `notebooks/gpu_node.ipynb` now suggest
`Qwen/Qwen2.5-3B-Instruct` on a free T4 (was 0.5B) specifically so prefill
is large enough to be a measurable cost rather than noise next to
generation time, plus `--enable-prompt-tokens-details` (without it, vLLM
never reports `cached_tokens` at all, so `true_cache_ratio` would silently
stay zero) and T4-appropriate `--block-size 16 --max-num-seqs 32
--max-model-len 8192 --dtype half`. `deploy/run_replicas.sh` gets the same
four flags; its own default model stays the larger 7B (it targets a real
multi-GPU box, where prefill was never the tiny fraction it is on a T4).

**Papers this phase is motivated by**: DistServe (arXiv:2401.09670,
goodput's actual definition), Preble (arXiv:2407.00023, the
shared-system-prompt/Zipf workload shape and the "gains only show up on
workloads with real shared prefixes" finding that makes Phase 0 come
before any routing change), and MoonCake (FAST'25, `cached_tokens` as
ground truth to check a router's own prediction against). None of these
change routing behavior yet -- `round_robin`, `least_connections`, and
`swiftserve` all still choose exactly as before; this phase only changes
what gets measured and what workload it's measured against.

## Where this sits relative to DistServe / MoonCake / Preble / llm-d

Three papers, mapped honestly to what's real in this repo vs. what would
need infrastructure this repo doesn't have. The dividing line throughout:
**SwiftServe is a CPU-only HTTP proxy in front of vLLM's stock
OpenAI-compatible server. It never touches vLLM's process or GPU memory.**
Nothing here reads or writes a KV-cache block; everything it does is
decide *which replica's URL* to send an HTTP request to.

**DistServe** (arXiv:2401.09670) disaggregates prefill and decode onto
separate GPU pools and measures success via Goodput -- the max request
rate sustaining a target SLO-attainment. SwiftServe does not disaggregate
prefill/decode (that needs dedicated GPU pools per phase and inter-phase
KV transfer -- out of scope for a 2-3-replica Colab-GPU cluster where
every replica runs both phases together, same as stock vLLM). What *is*
implemented for real: the Goodput@N metric itself (section 6,
`scripts/benchmark.py goodput`), measured with an open-loop load
generator the way DistServe's own evaluation methodology requires, not
approximated from the existing closed-loop trials.

**Preble** ("Efficient Distributed Prompt Scheduling for LLM Serving",
ICLR 2025) schedules requests using a prefix tree that recognizes shared
prompt prefixes across *different* requests, combined with load-aware
placement, and can fetch a needed KV cache from another replica when
recomputing would be more expensive than transferring it. The scheduling
half is implemented for real: `PrefixCacheTrie` (section 8) is exactly
this idea -- a real trie, real longest-prefix matching, real routing
decisions based on it, fully unit-tested with no vLLM dependency. What's
not implemented: the actual *fetch*. Preble's system can move KV cache
bytes between GPUs because it controls the serving engine directly; vLLM's
stock OpenAI server exposes no API to export or import raw KV blocks, so
there is nothing for an external HTTP proxy to call to make a "fetch"
real. Section 9's cold-start penalty is the honest substitute: since
SwiftServe can't fetch, it instead prices the cost of *not* having the
cache, and lets that inform routing instead of pretending to move bytes it
has no way to move.

**MoonCake** (FAST'25) goes further: a global KV-cache pool spanning GPU,
CPU DRAM, and SSD across the whole cluster, with a Conductor scheduler
deciding whether to reuse, transfer, or recompute per request. This is
**not implemented as code here**, and shouldn't be pretended at: doing it
for real means one of (a) patching vLLM's engine directly to expose its KV
blocks, or (b) running vLLM with an actual KV-transfer connector -- e.g.
LMCache, or vLLM's own disaggregated-serving `KVConnector` interface --
neither of which ships by default or is set up in this repo, and both are
a meaningfully separate infrastructure project from "an HTTP router in
front of vLLM." Concretely, though, look at what SwiftServe's control
plane *does* already have: the per-session affinity map
(`ReplicaState.has_warm_cache`) plus the prefix trie (section 8) together
are exactly the "which replica has what, and how fresh" signal a
MoonCake-style Conductor would need to decide where to route a request --
the missing piece is purely the data-plane transfer underneath it, not the
scheduling intelligence above it. If this project ever adds a real
KV-connector integration, `PrefixCacheTrie.longest_match`'s output is
already in the right shape to decide *when* a fetch would be worth
issuing; today it's used only to decide *where* to route, because that's
as far as routing decisions alone can go.

**llm-d's P2P KV-cache sharing** post makes the load-aware half of this
explicit: prefer the cache-warm replica, but reroute to a less-loaded one
when it's overloaded, fetching cache from the original replica only when
the prefix is long enough that transfer beats recomputation. SwiftServe
already had the load-aware half of this (the SLA-based reroute in
`SwiftServePolicy`, predating this change); section 9 adds the "is it
worth it" cost comparison that decides between the options *when a real
fetch path exists* -- again, without the fetch path itself, since that
requires the same vLLM-side connector as MoonCake above. The token
threshold where fetching would beat recomputation is explicitly documented
in that code as deployment-dependent (real GPU interconnect bandwidth,
real vLLM prefill throughput) rather than a number invented here.

## Known limitations (stated, not hidden)

- The circuit breaker counts *consecutive* failures rather than a failure
  rate over a sliding window. That's the right tradeoff for vLLM specifically
  (a replica is almost always either fully up or freshly dead, not
  producing a low background error rate), but it is a real simplification
  relative to what a breaker guarding, say, a flaky third-party API would
  need.
- **Fixed:** the half-open probe priority rule (section 1) used to always
  hand the *next* request to whichever recovering replica happened to sort
  first (`half_open_probes[0]`, i.e. always the lowest `replica_id`), which
  let it monopolize every probe slot before any other simultaneously-
  recovering replica got one. `app.py`'s `_pick_half_open_probe` now
  prefers the half-open replica with the fewest probes already
  outstanding (ties broken by `replica_id`), spreading probe traffic
  evenly across however many replicas are recovering at once instead of
  favoring whichever one sorts first.
- The router's own request-handling loop is entirely in-process, in-memory
  state on a single `asyncio` process (session-affinity map, per-replica
  circuit breaker state, latency EWMAs, and now the prefix trie). Running
  more than one router process for horizontal scale would need that state
  moved somewhere shared (Redis, etc.) first -- two independent router
  processes today would each form their own, disagreeing picture of which
  replica holds which session's cache and which replica's circuit is open.
- `PrefixCacheTrie` matching is exact-content only, and its bounded-size
  strategy is a full reset rather than per-node eviction (section 8) --
  both are deliberate, documented tradeoffs, not oversights, but they mean
  it won't catch a semantically-equivalent-but-differently-worded shared
  prefix, and a burst of unique prefixes past `max_nodes` briefly forgets
  everything rather than gracefully aging out the oldest entries.
- The cold-start recompute penalty (section 9) is a flat per-token rate
  the deployer configures, not something SwiftServe learns from its own
  traffic -- see section 9 for why that's a deliberate choice, not a gap
  waiting to be filled the same way `effective_batch_capacity` was.
- Genuine cross-replica KV-cache reuse (MoonCake's global pool, Preble's
  actual fetch path) is not implemented, and isn't achievable from
  SwiftServe's current vantage point (an HTTP proxy in front of vLLM's
  stock OpenAI server) without vLLM-side integration this repo doesn't
  have -- see "Where this sits relative to DistServe / MoonCake / Preble /
  llm-d" above for exactly what's real vs. what would be needed.

## What's actually tested, and how

No test in this repo needs a GPU to run (`pytest -q`, ~109 tests). CI
(`.github/workflows/ci.yml`) runs that same command, plus `ruff check .`
and `mypy swiftserve`, on every push and PR across Python 3.10-3.12. What
"real" means varies deliberately by layer, and is worth being explicit
about defending:

- **Pure logic** (circuit breaker state transitions, admission control,
  bootstrap CI, permutation test, percentile math and its sample-size
  gating, `find_goodput`/`sla_attainment`, the occupancy-bucketed latency
  model, the prefix trie's longest-match/TTL/depth-cap/reset behavior,
  cross-session routing and the cold-start cost model): plain unit tests,
  no I/O at all.
- **The open-loop load generator** (`test_benchmark_goodput.py`): a real
  local `uvicorn` server (no GPU, no vLLM -- a fake chat-completions
  endpoint) driven by the actual Poisson-arrival scheduling code, over
  real sockets -- proves the arrival mechanism itself works, not just
  that `find_goodput`'s math is right on synthetic data.
- **Router wiring** (`test_app_smoke.py`, `test_metrics.py`): the real
  FastAPI app, hit through an in-process ASGI transport against a fake
  in-process stand-in for vLLM -- this checks the request path is wired
  correctly, not inference behavior.
- **The sidecar in isolation** (`test_replica_sidecar.py`): same pattern,
  plus a test of `ProcessSupervisor` against a real OS subprocess (`sleep
  30`) to confirm kill/restart actually manages a real process, since that
  specific claim can't be honestly tested any other way.
- **The distributed system end-to-end** (`test_chaos_runner.py`): real
  separate OS processes -- a real router (uvicorn), two real sidecars each
  supervising their own real child process, all on real localhost TCP
  ports, talking real HTTP, with a real `SIGKILL` for the kill scenario.
  The only thing not real here is the model: `scripts/fake_vllm_stub.py` is
  an honestly-labeled, minimal HTTP stand-in used only because this
  sandbox has no GPU to run actual vLLM. Every mechanism upstream of "what
  generates the reply text" -- routing, the circuit breaker, admission
  control, chaos fault injection, process supervision -- runs for real as
  separate processes over real sockets; the same test would pass unchanged
  pointed at real vLLM replicas on real GPUs, and doing exactly that (via
  `notebooks/gpu_node.ipynb`) is how the project's actual benchmark numbers
  are produced.
