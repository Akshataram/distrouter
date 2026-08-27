# SwiftServe

Cache- and SLA-aware request routing for multi-turn LLM serving, deployed
against a 3-replica Qwen cluster running vLLM.

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

- `swiftserve/` -- the control plane itself (FastAPI). It proxies
  OpenAI-compatible `/v1/chat/completions` calls to whichever real vLLM
  replica it selects, tracks per-replica queue depth (both from its own
  in-flight counter and by scraping each replica's vLLM `/metrics`
  endpoint), and maintains the cache-affinity heatmap as soft state.
- `deploy/` -- how to actually stand up 3 GPU replicas: a bare-metal launch
  script and a Docker Compose file, both serving real Qwen weights via vLLM.
- `scripts/load_test.py` -- a live load generator that drives real
  multi-turn conversations through a running router and reports observed
  latency, cache-hit rate, and per-replica load distribution.
- `tests/` -- unit tests for the routing/state logic plus end-to-end smoke
  tests that exercise the FastAPI app against a fake in-process backend (no
  GPU needed to verify the wiring is correct).

**SwiftServe needs no GPU to run** -- it's a thin CPU proxy. Actually serving
Qwen requires real GPUs for the 3 vLLM replicas; see `DEPLOYMENT.md` for the
full step-by-step guide (this includes a note that none of those GPU-side
commands could be executed while building this repo, since the environment
it was assembled in has no GPU -- verify them on your own hardware).

## Quickstart

```bash
pip install -r requirements.txt -r requirements-dev.txt
pytest -q                      # unit + smoke tests, no GPU required

# once you have 3 real vLLM/Qwen replicas up (see DEPLOYMENT.md):
export SWIFTSERVE_REPLICAS=http://localhost:8001,http://localhost:8002,http://localhost:8003
uvicorn swiftserve.app:app --port 8000

python scripts/load_test.py --router-url http://localhost:8000 --num-sessions 50 --turns 4
```

See `DEPLOYMENT.md` for the full guide to launching real Qwen replicas and
comparing SwiftServe against round-robin/least-connections baselines.
