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

The response carries `X-SwiftServe-Replica` (which replica handled it) and
`X-SwiftServe-Cache-Hit` (`true` if SwiftServe believed that session's cache
was warm on the chosen replica) for observability.

## 5. Benchmark: SwiftServe vs. round-robin vs. least-connections

Run the same live workload against the router under each policy (restart
the router with a different `SWIFTSERVE_POLICY` between runs, or run 3
routers on 3 ports against the same replica pool):

```bash
python scripts/load_test.py --router-url http://localhost:8000 \
  --num-sessions 50 --turns 4 --concurrency 10 --sla-ms 2000
```

It reports real observed latency (mean/p50/p95/p99), cache-hit rate, SLA
violation rate, and the request distribution across replicas -- compare
these numbers across `SWIFTSERVE_POLICY=swiftserve` vs. `round_robin` vs.
`least_connections` runs.

## Notes / production hardening

- This is a routing prototype, not a hardened gateway: it has no auth,
  rate limiting, or TLS termination. Put it behind a real ingress/load
  balancer or add auth middleware before exposing it beyond a trusted network.
- `SWIFTSERVE_CACHE_TTL_S` controls how long SwiftServe keeps believing a
  session's cache is warm on a replica after its last request; tune it
  against how long vLLM's own prefix cache actually stays resident under
  your memory pressure and traffic mix.
- Scaling past 3 replicas only requires adding more URLs to
  `SWIFTSERVE_REPLICAS`; nothing else in the routing logic is hardcoded to 3.
