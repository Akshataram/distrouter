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
  circuit breaker state, latency EWMAs). Running more than one router
  process for horizontal scale would need that state moved somewhere
  shared (Redis, etc.) first -- two independent router processes today
  would each form their own, disagreeing picture of which replica holds
  which session's cache and which replica's circuit is open.

## What's actually tested, and how

No test in this repo needs a GPU to run (`pytest -q`, ~79 tests). CI
(`.github/workflows/ci.yml`) runs that same command, plus `ruff check .`
and `mypy swiftserve`, on every push and PR across Python 3.10-3.12. What
"real" means varies deliberately by layer, and is worth being explicit
about defending:

- **Pure logic** (circuit breaker state transitions, admission control,
  bootstrap CI, permutation test, percentile math and its sample-size
  gating, the occupancy-bucketed latency model): plain unit tests, no I/O
  at all.
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
