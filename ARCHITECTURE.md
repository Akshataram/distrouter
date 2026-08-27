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

## Known limitations (stated, not hidden)

- `estimate_latency_ms()` in `state.py` still assumes serial processing
  (`(queue_depth + 1) * ewma_latency`) against a vLLM backend that actually
  does continuous batching -- this is the project's own earlier honest
  finding about why SwiftServe's SLA-based rerouting over-triggers under
  concurrency, and it is unchanged by this upgrade. Fixing the estimator
  itself (e.g. modeling batched throughput rather than a serial queue) is
  future work, not something this round of hardening addressed.
- The circuit breaker counts *consecutive* failures rather than a failure
  rate over a sliding window. That's the right tradeoff for vLLM specifically
  (a replica is almost always either fully up or freshly dead, not
  producing a low background error rate), but it is a real simplification
  relative to what a breaker guarding, say, a flaky third-party API would
  need.
- The half-open probe priority rule (Â§1) always sends the *next* request to
  a recovering replica, with no cap beyond `half_open_max_probes`
  concurrent probes. With more replicas simultaneously recovering than
  `half_open_max_probes`, only one gets probed per request cycle; this
  hasn't been a problem at the scale this project tests at (2-3 replicas)
  but would need revisiting at higher replica counts.

## What's actually tested, and how

No test in this repo needs a GPU to run (`pytest -q`, ~47 tests). What
"real" means varies deliberately by layer, and is worth being explicit
about defending:

- **Pure logic** (circuit breaker state transitions, admission control,
  bootstrap CI, permutation test): plain unit tests, no I/O at all.
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
