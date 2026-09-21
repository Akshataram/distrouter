# SwiftServe

Cache- and SLA-aware request routing for multi-turn LLM serving, deployed
against a real multi-node Qwen/vLLM cluster -- with a resilience layer
(circuit breaking, admission control), chaos engineering against real
process kills and network partitions, Prometheus/Grafana observability,
and a statistically rigorous benchmark harness. See `ARCHITECTURE.md` for
the full system design and what's actually tested, and how.

## Abstract

Modern large language model (LLM) inference platforms scale horizontally by
deploying multiple GPU replicas of the same model to absorb concurrent user
traffic. In conversational, multi-turn applications, each replica's
inference engine (vLLM) maintains a local key-value (KV) cache -- the stored
attention states from previously processed tokens -- so that later turns in
a conversation can be generated without recomputing the entire prior context
from scratch. This caching mechanism is one of the most significant
efficiency gains in modern LLM serving, but it only helps if a session's
follow-up requests are consistently routed back to the replica that already
holds its cache.

In practice, this rarely happens by default. Standard load-balancing
strategies -- round-robin, least-connections, and similar generic policies --
are cache-agnostic: they distribute requests based on ordering or current
connection count, with no knowledge of which replica holds a warm cache for
a given session. As a result, a significant fraction of follow-up requests
in multi-turn conversations are routed to a replica that has never seen that
conversation before, forcing a full, expensive recomputation of the context.
This produces three compounding problems: increased per-request latency,
redundant GPU compute cycles that reduce overall cluster throughput, and
higher serving cost per completed request -- all of which worsen as the
number of concurrent multi-turn sessions grows.

**SwiftServe** is a standalone request-routing service that sits between
clients and a pool of **3 LLM inference replicas running the Qwen model
family**. SwiftServe makes routing decisions using three pieces of live
system state: (1) which of the 3 replicas currently holds the warm KV-cache
for each active session, (2) the current queue depth / load on each replica,
and (3) the latency Service-Level Agreement (SLA) attached to each incoming
request. Rather than optimizing for a single variable, SwiftServe's routing
policy treats cache-affinity and SLA compliance as two competing objectives
balanced on every request: it prefers the cache-warm replica for a session
when doing so will not violate the request's latency SLA, but deliberately
reroutes to a different, less-loaded replica among the 3 when the
cache-optimal replica is congested enough that using it would cause an SLA
violation. Critically, SwiftServe requires no modification to the underlying
model or inference engine -- it is a pure control-plane addition that
operates entirely on ordinary CPU infrastructure, while all GPU-bound
inference computation is left untouched on the data plane.

## Experimental setup

- **Target model:** Qwen (`Qwen/Qwen2.5-*-Instruct`, sized to fit your GPUs)
- **Cluster scale:** 3 replicas -- 3 GPU worker instances, each running vLLM
- **Topology:** SwiftServe is a lightweight CPU-level control plane sitting
  directly in front of the 3 Qwen replicas
- **Cache tracking & load balancing:** SwiftServe monitors live system state
  across all 3 replicas -- a local KV-cache heatmap per session and current
  worker queue depth -- and evaluates each incoming query against its
  latency SLA:
  - **Optimal case (cache hit + within SLA):** route to the replica holding
    the warm cache for that session.
  - **Congestion / violation risk:** if the cache-warm replica is overloaded
    and would breach the SLA, reroute to a less-loaded replica among the
    remaining 2.
- **Baselines:** round-robin and least-connections, both cache-agnostic.
- **Evaluation metrics:** end-to-end latency (mean/p50/p95/p99), cache-hit
  rate, SLA-violation rate, and cost-per-request.

## What's actually in this repo

This is a real, runnable system, not a paper simulation:

- `swiftserve/app.py`, `policy.py`, `state.py`, `config.py`,
  `metrics_scraper.py` -- the control plane itself (FastAPI), unchanged in
  spirit from the first version: it proxies OpenAI-compatible
  `/v1/chat/completions` calls to whichever real vLLM replica it selects,
  tracks per-replica queue depth, and maintains the cache-affinity heatmap
  as soft state.
- `swiftserve/resilience.py` -- per-replica circuit breaking and global
  admission control, wired into the routing and proxy path.
- `swiftserve/request_context.py` -- a `contextvars`-based request
  correlation ID (`X-Request-Id`), propagated through the router's and
  sidecar's logs without threading it through every function signature.
- `swiftserve/metrics.py` -- the router's own Prometheus instrumentation
  (`/metrics`), separate from the per-replica vLLM metrics it scrapes.
- `swiftserve/replica_sidecar.py` -- runs next to real vLLM on each GPU
  node; proxies normal traffic through unchanged, and exposes an
  authenticated `/chaos/*` API (partition / latency / error-rate / real
  process kill+restart) for fault injection.
- `scripts/chaos_runner.py` -- orchestrates a full fault-injection scenario
  against a live deployment and reports whether it detected the fault,
  rerouted, and recovered (with MTTR).
- `scripts/benchmark.py` -- multi-seed statistical benchmark: bootstrap
  confidence intervals and a permutation-test significance check between
  policies, instead of one single-shot number.
- `scripts/load_test.py` -- the original single-shot live load generator;
  kept for a quick one-off check.
- `scripts/fake_vllm_stub.py` -- an honestly-labeled, minimal HTTP
  stand-in for vLLM, used only in this project's own GPU-free tests (see
  `ARCHITECTURE.md`) -- never used for the project's actual benchmark
  numbers, which come from real vLLM on real GPUs.
- `notebooks/gpu_node.ipynb` -- turns a free Colab/Kaggle GPU session into
  one real replica node (real vLLM + real Qwen, fronted by the chaos
  sidecar, exposed via a public tunnel) -- run it in 2-3 separate free
  accounts for a genuinely multi-node deployment.
- `deploy/` -- bare-metal and Docker Compose launch scripts for a
  single-box multi-GPU deployment, plus `deploy/observability/` (a
  Prometheus + Grafana stack, pre-provisioned, scraping the router).
- `tests/` -- unit tests for the routing/resilience/statistics logic, smoke
  tests against a fake in-process backend, and a full end-to-end chaos test
  that runs the router and sidecars as real separate OS processes over real
  sockets (see `ARCHITECTURE.md`'s testing section for exactly what's real
  vs. stood-in, and why).

**SwiftServe needs no GPU to run** -- it's a thin CPU proxy, and `pytest -q`
(79 tests) needs no GPU either. `ruff check .` and `mypy swiftserve` are
clean, and `.github/workflows/ci.yml` runs all three on every push/PR
across Python 3.10-3.12. Actually serving Qwen requires real GPUs;
`DEPLOYMENT.md` covers both the original single-box path and the real
multi-node path via `notebooks/gpu_node.ipynb`.

## Quickstart

```bash
pip install -r requirements.txt -r requirements-dev.txt
pytest -q                      # unit + smoke + end-to-end chaos tests, no GPU required
ruff check .                   # lint
mypy swiftserve                # type-check

# once you have real vLLM/Qwen replicas up (single-box: see DEPLOYMENT.md
# Option A/B/C; multi-node: notebooks/gpu_node.ipynb per node):
export SWIFTSERVE_REPLICAS=http://localhost:8001,http://localhost:8002,http://localhost:8003
uvicorn swiftserve.app:app --port 8000

python scripts/load_test.py --router-url http://localhost:8000 --num-sessions 50 --turns 4
```

See `DEPLOYMENT.md` for the full guide -- single-box and real multi-node
deployment, the observability stack, and running a chaos scenario -- and
`ARCHITECTURE.md` for the system design and known limitations.
