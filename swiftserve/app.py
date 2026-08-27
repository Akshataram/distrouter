"""SwiftServe control plane: a CPU-only FastAPI proxy that sits in front of
a pool of real vLLM replicas (e.g. 3 GPU workers each serving Qwen) and
routes each request using cache-affinity + SLA-aware logic.

This process does no GPU/model work itself -- it only makes a routing
decision per request and forwards the HTTP call to the chosen replica's
OpenAI-compatible vLLM endpoint. Run it on any CPU host that can reach your
GPU replicas over the network; see DEPLOYMENT.md for how to stand up the
replicas themselves.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from swiftserve import metrics
from swiftserve.config import settings
from swiftserve.metrics_scraper import scrape_loop
from swiftserve.policy import POLICIES
from swiftserve.resilience import AdmissionController, CircuitState
from swiftserve.state import ReplicaState
from swiftserve.proxy import forward_chat_completion

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("swiftserve.app")

replicas: list[ReplicaState] = [
    ReplicaState(
        replica_id=i,
        base_url=url,
        cache_ttl_s=settings.cache_affinity_ttl_s,
        circuit_failure_threshold=settings.circuit_failure_threshold,
        circuit_reset_timeout_s=settings.circuit_reset_timeout_s,
        circuit_max_reset_timeout_s=settings.circuit_max_reset_timeout_s,
    )
    for i, url in enumerate(settings.replica_urls)
]

if settings.policy not in POLICIES:
    raise ValueError(f"Unknown SWIFTSERVE_POLICY={settings.policy!r}; choose from {list(POLICIES)}")
policy = POLICIES[settings.policy]()
admission = AdmissionController(max_in_flight=settings.admission_max_in_flight)

_http_client: httpx.AsyncClient | None = None
_scrape_stop_event = asyncio.Event()
_scrape_task: asyncio.Task | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _http_client, _scrape_task
    _http_client = httpx.AsyncClient()
    _scrape_task = asyncio.create_task(scrape_loop(replicas, settings.metrics_scrape_interval_s, _scrape_stop_event))
    logger.info(
        "SwiftServe up | model=%s | policy=%s | replicas=%s",
        settings.model_name, policy.name, [r.base_url for r in replicas],
    )
    yield
    _scrape_stop_event.set()
    if _scrape_task:
        await _scrape_task
    await _http_client.aclose()


app = FastAPI(title="SwiftServe", lifespan=lifespan)


@app.get("/healthz")
async def healthz():
    return {"status": "ok", "policy": policy.name, "model": settings.model_name}


@app.get("/metrics")
async def router_metrics():
    metrics.refresh_replica_gauges(replicas)
    return Response(content=metrics.render_latest(), media_type=metrics.CONTENT_TYPE_LATEST)


@app.get("/status")
async def status():
    return {
        "model": settings.model_name,
        "policy": policy.name,
        "admission": admission.status(),
        "replicas": [r.status() for r in replicas],
    }


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    raw_body = await request.body()
    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError:
        return JSONResponse(status_code=400, content={"error": "invalid JSON body"})

    if not admission.try_acquire():
        metrics.record_admission_rejected()
        return JSONResponse(
            status_code=503,
            content={"error": "server busy: at max in-flight capacity, retry shortly"},
            headers={"Retry-After": "1"},
        )

    session_id = request.headers.get("x-session-id") or payload.get("user") or str(uuid.uuid4())
    sla_ms = float(request.headers.get("x-sla-ms", settings.default_sla_ms))
    is_streaming = bool(payload.get("stream", False))

    available = [r for r in replicas if not r.circuit.is_open() and r.circuit.has_probe_capacity()]
    if not available:
        admission.release()
        metrics.record_all_circuits_open()
        return JSONResponse(
            status_code=503,
            content={"error": "all replicas circuit-open: cluster is unhealthy"},
            headers={"Retry-After": "2"},
        )

    # A half-open replica gets first claim on the next eligible request
    # regardless of what the routing policy would otherwise pick: it needs
    # real traffic to prove it has recovered, and a policy that keeps
    # scoring the already-healthy replicas better (e.g. by latency
    # estimate) would otherwise never route anything there again, leaving
    # it half-open forever instead of closing or re-opening.
    half_open_probes = [r for r in available if r.circuit.state is CircuitState.HALF_OPEN]
    chosen = half_open_probes[0] if half_open_probes else policy.select(session_id, sla_ms, available)
    was_cache_hit = chosen.has_warm_cache(session_id)
    chosen.touch_session(session_id)
    chosen.circuit.mark_dispatched()
    chosen.in_flight += 1

    def release(latency_ms: float, success: bool):
        # Single release point for this request's slots: forward_chat_completion
        # guarantees this fires exactly once (success, HTTP error, or streaming
        # failure alike), so nothing else in this handler should call it again.
        chosen.in_flight = max(0, chosen.in_flight - 1)
        admission.release()
        metrics.record_request(
            replica_id=chosen.replica_id,
            policy=policy.name,
            outcome="success" if success else "error",
            latency_ms=latency_ms,
            was_cache_hit=was_cache_hit,
            sla_violated=latency_ms > sla_ms,
        )

    forward_headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in {"host", "content-length"}
    }
    try:
        response = await forward_chat_completion(
            client=_http_client,
            replica=chosen,
            path="/v1/chat/completions",
            body=raw_body,
            headers=forward_headers,
            is_streaming=is_streaming,
            timeout_s=settings.request_timeout_s,
            on_complete=release,
            max_retries=settings.proxy_max_retries,
            retry_base_delay_s=settings.proxy_retry_base_delay_s,
        )
    except httpx.HTTPError as exc:
        logger.error("upstream request to replica %s failed: %s", chosen.replica_id, exc)
        return JSONResponse(status_code=502, content={"error": f"upstream replica unreachable: {exc}"})

    response.headers["X-SwiftServe-Cache-Hit"] = "true" if was_cache_hit else "false"
    return response
