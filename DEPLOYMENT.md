# Deploying SwiftServe against 3 real Qwen replicas

SwiftServe itself is CPU-only and has no model weights -- it's an HTTP
control plane that decides *which* replica handles a request. The 3 replicas
running Qwen are real vLLM processes that need real GPUs. Run all of this on
your own GPU machine or cloud GPU VM; it will not run in a plain CPU
container (this repo was assembled in one, so none of the steps below have
been executed here -- verify each command on your own hardware).

## 1. Prerequisites

- 3 NVIDIA GPUs, either on one multi-GPU box or as 3 separate hosts/VMs
  (RunPod, Lambda Labs, AWS `g5`/`p4`, GCP `a2`, etc.)
- NVIDIA drivers + CUDA installed, `nvidia-smi` working
- Python 3.10+ and `pip install vllm` (or the `vllm/vllm-openai` Docker image
  if using Docker Compose)
- Enough VRAM per GPU for the Qwen size you pick:

  | Model | Approx. VRAM (fp16) |
  |---|---|
  | `Qwen/Qwen2.5-0.5B-Instruct` | ~2 GB (good for a first smoke test) |
  | `Qwen/Qwen2.5-1.5B-Instruct` | ~4 GB |
  | `Qwen/Qwen2.5-7B-Instruct`   | ~16-20 GB |
  | `Qwen/Qwen2.5-14B-Instruct`  | ~32 GB+ |

## 2. Launch the 3 Qwen replicas

**Option A -- bare metal, one multi-GPU box:**

```bash
MODEL=Qwen/Qwen2.5-7B-Instruct ./deploy/run_replicas.sh
```

This starts 3 `vllm.entrypoints.openai.api_server` processes, one per GPU
(`CUDA_VISIBLE_DEVICES=0/1/2`), on ports 8001-8003, each with
`--enable-prefix-caching` (vLLM's own automatic KV-cache reuse for shared
prompt prefixes -- this is what actually makes a "cache hit" fast; SwiftServe
just makes sure repeat requests for a session keep landing on the replica
that already has that prefix cached).

**Option B -- Docker Compose (needs the NVIDIA Container Toolkit):**

```bash
MODEL=Qwen/Qwen2.5-7B-Instruct docker compose -f deploy/docker-compose.yml up -d
docker compose -f deploy/docker-compose.yml ps
```

**Option C -- 3 separate GPU VMs:** run the single-replica half of Option A's
command (just one `python -m vllm.entrypoints.openai.api_server ...`
invocation) on each VM, and note each VM's reachable address for step 3.

Verify each replica is actually serving before moving on:

```bash
curl http://localhost:8001/health
curl http://localhost:8001/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"Qwen/Qwen2.5-7B-Instruct","messages":[{"role":"user","content":"say hi"}]}'
```

## 3. Launch the SwiftServe router

```bash
pip install -r requirements.txt
export SWIFTSERVE_MODEL=Qwen/Qwen2.5-7B-Instruct
export SWIFTSERVE_REPLICAS=http://localhost:8001,http://localhost:8002,http://localhost:8003
export SWIFTSERVE_POLICY=swiftserve   # or round_robin / least_connections for A/B comparison
# export SWIFTSERVE_API_TOKEN=some-real-secret   # optional; unset = unauthenticated (see "Notes" below)
uvicorn swiftserve.app:app --host 0.0.0.0 --port 8000
```

If the replicas are on separate hosts, use their real addresses in
`SWIFTSERVE_REPLICAS` instead of `localhost`.

Check `GET /status` for live per-replica state (queue depth, GPU cache
usage, tracked sessions, EWMA latency):

```bash
curl http://localhost:8000/status | python3 -m json.tool
```

## 4. Send real multi-turn traffic through it

Point any OpenAI-compatible client at `http://<router-host>:8000/v1`, and
pass a stable `X-Session-Id` header per conversation so follow-up turns get
routed back to the replica holding that session's warm KV-cache:

```bash
curl http://localhost:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -H 'X-Session-Id: user-42-conversation-7' \
  -H 'X-SLA-Ms: 2000' \
  -d '{"model":"Qwen/Qwen2.5-7B-Instruct","messages":[{"role":"user","content":"hi"}]}'
```

The response carries `X-SwiftServe-Replica` (which replica handled it),
`X-SwiftServe-Cache-Hit` (`true` if SwiftServe believed that session's cache
was warm on the chosen replica), and `X-Request-Id` (generated if you
didn't send one) for observability -- grep the router's and the sidecar's
logs for `[rid=<that id>]` to trace one request across both processes.
If `SWIFTSERVE_API_TOKEN` is set, add `-H 'Authorization: Bearer <token>'`.

## 5. Benchmark: SwiftServe vs. round-robin vs. least-connections

For a quick one-off number:

```bash
python scripts/load_test.py --router-url http://localhost:8000 \
  --num-sessions 50 --turns 4 --concurrency 10 --sla-ms 2000
```

It reports real observed latency (mean/p50/p90/p95/p99), cache-hit rate,
SLA violation rate, and the request distribution across replicas. A
percentile prints `(unreliable: n=.., want >=..)` instead of a bare number
when the run didn't produce enough samples for that percentile's tail to
mean anything -- p99 in particular needs real volume (~1000+ requests) to
be more than a restatement of the single largest observation; see
`ARCHITECTURE.md` section 6 for the exact rule.

**For anything you'd defend in front of a panel, use `scripts/benchmark.py`
instead** -- one run of `load_test.py` can make round-robin look better or
worse than SwiftServe purely from scheduling jitter. `benchmark.py` runs
several independent trials (different seeds) per policy and reports a
bootstrap confidence interval plus a permutation-test significance check
between policies -- including a bootstrap 95% CI on each latency
percentile itself (not just the mean), with an unreliable one flagged `*`
in the comparison table rather than hidden:

```bash
# once per policy, against a router already configured with that policy
# (restart the router with a different SWIFTSERVE_POLICY between runs, or
# run 3 routers on 3 ports against the same replica pool):
python scripts/benchmark.py run --router-url http://localhost:8000 \
  --policy-label swiftserve --num-sessions 30 --turns 4 --concurrency 6 \
  --sla-ms 2000 --seeds 1,2,3,4,5 --output reports/swiftserve.json

python scripts/benchmark.py run --router-url http://localhost:8001 \
  --policy-label round_robin ... --output reports/round_robin.json
python scripts/benchmark.py run --router-url http://localhost:8002 \
  --policy-label least_connections ... --output reports/least_connections.json

python scripts/benchmark.py compare reports/*.json
```

**For "how much load can this actually take" (DistServe's Goodput
metric), use `scripts/benchmark.py goodput`** instead of either of the
above -- `run`/`compare` measure latency at a load level you pick
(`--concurrency`); `goodput` sweeps *offered* load itself (an open-loop
Poisson arrival process, independent of how fast the system responds) to
find the highest request rate sustaining a target SLA-attainment
percentage:

```bash
python scripts/benchmark.py goodput --router-url http://localhost:8000 \
  --policy-label swiftserve --rps-levels 5,10,15,20,25,30 \
  --duration-s 20 --turns 4 --sla-ms 2000 --sla-target 0.9 \
  --output reports/goodput_swiftserve.json

python scripts/benchmark.py goodput --router-url http://localhost:8001 \
  --policy-label round_robin --rps-levels 5,10,15,20,25,30 \
  --duration-s 20 --turns 4 --sla-ms 2000 --sla-target 0.9 \
  --output reports/goodput_round_robin.json

python scripts/benchmark.py goodput-compare reports/goodput_*.json
```

The report prints a per-RPS-level table (offered/completed requests, SLA
attainment, mean/p95 latency) and the resulting Goodput@90 -- the highest
*tested* RPS that actually cleared the target, never an interpolated
guess, and honestly `None` if no tested level cleared it.

## 6. Real multi-node deployment (Colab / Kaggle, no local GPU needed)

Everything above assumes GPUs you already control. If you don't have any,
`notebooks/gpu_node.ipynb` turns a free Colab or Kaggle T4 session into one
real replica node -- real vLLM, real Qwen weights, fronted by
`swiftserve/replica_sidecar.py`, exposed over a public Cloudflare quick
tunnel (no account needed). Run the same notebook in 2-3 separate free
accounts (a second Colab account, a Kaggle account) to get a genuinely
multi-node cluster -- separate physical GPUs, separate processes, real
network latency between the router and each node, rather than 3 slices of
one shared GPU.

On a free T4 (~16GB), the notebook launches `Qwen/Qwen2.5-3B-Instruct`
(rather than the 7B default `run_replicas.sh` uses on a real multi-GPU box)
so prefill is still a real, measurable cost rather than the entire request
being generation time -- see problem P1 in `ARCHITECTURE.md`'s Phase 0
section for why that matters for anything claiming to measure cache-aware
routing. Its vLLM launch flags:

```
--enable-prefix-caching --enable-prompt-tokens-details --block-size 16 \
--max-num-seqs 32 --max-model-len 8192 --dtype half
```

`--enable-prompt-tokens-details` is the one that actually makes
`scripts/benchmark.py`'s `true_cache_ratio` real instead of always zero --
without it, vLLM never reports `usage.prompt_tokens_details.cached_tokens`
in its response at all. The rest are T4-appropriate defaults: `--dtype half`
(T4 doesn't have fast bf16), `--block-size 16` / `--max-num-seqs 32` sized
for ~16GB of VRAM, `--max-model-len 8192` enough headroom for the
`shared_system`/`long_doc` workloads' longer prompts (see
`scripts/workloads.py`) without running out of KV-cache blocks.

Each run of the notebook prints a public URL and an admin token. Point the
router at the URLs (from wherever you're running it -- a laptop is fine,
the router is CPU-only):

```bash
export SWIFTSERVE_REPLICAS=https://node-a.trycloudflare.com,https://node-b.trycloudflare.com
export SWIFTSERVE_MODEL=Qwen/Qwen2.5-3B-Instruct
uvicorn swiftserve.app:app --port 8000
```

To actually see a difference between policies, benchmark against a workload
with real shared-prefix structure instead of the default `tiny` one (see
problem P1 above -- 5 short canned prompts give a cache-aware policy
nothing to exploit):

```bash
python scripts/benchmark.py run --router-url http://localhost:8000 \
  --policy-label swiftserve --workload shared_system --workload-num-apps 6 \
  --seeds 1,2,3,4,5 --output reports/swiftserve_shared.json
```

The comparison table's second block (`ttft_p50`/`true_cache`/etc.) is only
populated by workloads with real shared prefixes -- `tiny` will show it as
`n/a`, which is the honest answer, not a bug.

## 7. Observability: Prometheus + Grafana

```bash
docker compose -f deploy/observability/docker-compose.yml up -d
open http://localhost:3000   # Grafana, anonymous viewer access, dashboard pre-provisioned
open http://localhost:9090   # Prometheus, for raw PromQL
```

Prometheus scrapes the router's own `/metrics` (request rate by outcome,
latency percentiles, cache-hit/SLA-violation rate, admission rejections,
per-replica queue depth/EWMA latency/circuit state) -- see
`deploy/observability/prometheus.yml` if the router isn't reachable at
`host.docker.internal:8000` (e.g. it's running on a separate machine).

## 8. Chaos engineering: prove failover actually works

With the router and each replica's sidecar reachable (single-box or
multi-node), run a real fault-injection scenario:

```bash
python scripts/chaos_runner.py \
  --router-url http://localhost:8000 \
  --sidecar-urls http://localhost:9001,http://localhost:9002,http://localhost:9003 \
  --target-replica 0 --scenario kill --admin-token "$SIDECAR_ADMIN_TOKEN" \
  --fault-duration-s 15 --output chaos_report.json
```

`--scenario` is one of `partition`, `kill`, `latency`, `error-rate`. The
runner drives real traffic through the router throughout, and reports
whether the circuit breaker actually tripped, whether traffic actually
rerouted to the surviving replicas, and how long recovery took (MTTR) once
the fault cleared -- `tests/test_chaos_runner.py` runs the same scenarios
end-to-end against real (if GPU-free) processes on every `pytest` run, so
this is not new-to-you code the first time you run it live.

## Notes / production hardening

- The router's `/v1/chat/completions`, `/status`, and `/metrics` are
  bearer-token gated when `SWIFTSERVE_API_TOKEN` is set (unset by default,
  matching the original unauthenticated behavior) -- set a real token and
  send `Authorization: Bearer <token>` for anything beyond a private demo.
  `/healthz` always stays open for load-balancer probes. This still isn't
  TLS termination or rate limiting -- put the router behind a real
  ingress/load balancer for that. (The admission controller is
  backpressure, not authorization: it protects the cluster from being
  overwhelmed, it doesn't gate who's allowed to send requests.) The
  replica sidecar's `/chaos/*` API is separately bearer-token gated
  (`--admin-token` / `SIDECAR_ADMIN_TOKEN`) since it's reachable at a
  public tunnel URL in the multi-node deployment -- always set a real
  token for anything beyond a private demo, on both the router and every
  sidecar.
- `SWIFTSERVE_CIRCUIT_FAILURE_THRESHOLD` / `_CIRCUIT_RESET_S` /
  `_CIRCUIT_MAX_RESET_S` tune the per-replica circuit breaker;
  `SWIFTSERVE_MAX_IN_FLIGHT` tunes the global admission ceiling.
  `SWIFTSERVE_ASSUMED_MAX_BATCH_SIZE` sets the initial floor for each
  replica's learned continuous-batching capacity (default 1 -- safe/serial
  until real concurrency is observed; set it higher if you already know
  roughly what `--max-num-seqs` your replicas can sustain, to skip the
  learning period). See `ARCHITECTURE.md` for what each actually does.
- `SWIFTSERVE_CACHE_TTL_S` controls how long SwiftServe keeps believing a
  session's cache is warm on a replica after its last request; tune it
  against how long vLLM's own prefix cache actually stays resident under
  your memory pressure and traffic mix. The cross-session prefix trie
  (`ARCHITECTURE.md` section 8) uses the same TTL.
- `SWIFTSERVE_PREFIX_TRIE_MAX_DEPTH` (default 6) caps how many messages
  the cross-session prefix trie compares -- only early turns (system
  prompts, few-shot examples) are realistically shared verbatim across
  independent conversations.
- `SWIFTSERVE_COLD_START_MS_PER_TOKEN` (default `0.0`, off) prices the
  routing fallback's estimate of recomputing a cold prefix on a per-token
  basis; leave it at 0 unless you've actually measured your replicas'
  prefill throughput (see `ARCHITECTURE.md` section 9 for why this isn't
  self-calibrated the way batch capacity is).
- Scaling past 3 replicas only requires adding more URLs to
  `SWIFTSERVE_REPLICAS`; nothing else in the routing logic is hardcoded to 3.
