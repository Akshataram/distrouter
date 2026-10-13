"""Read-only observability API and the static UI, served by the router itself.

Everything here *reads* router state; nothing here makes or changes a routing
decision. The UI talks to the very same process that routes requests -- there
is no separate backend that could drift from the real one:

    GET  /ui/                    the single-page UI (static files in swiftserve/ui/)
    GET  /api/dashboard          one JSON snapshot: replicas, router state, aggregates
    GET  /api/events/stream      Server-Sent Events: the live structured event stream
    GET  /api/events             recent events as JSON (same data, for reconnect/tests)
    GET  /api/requests           recent per-request records
    GET  /api/requests/{id}      one request's full record + its events

Inference itself still goes through the existing ``POST /v1/chat/completions``.

Honesty rules baked into the snapshot (each is a deliberate choice, not an
oversight):

- A metric the router cannot measure is reported as unavailable *with the
  reason*, never as 0. In particular GPU utilization and VRAM are not exposed
  by vLLM's `/metrics`, so they are listed as unavailable; the KV-cache usage
  gauge vLLM does expose is labelled as KV-cache occupancy, not GPU load.
- GPU identity is only shown when the operator supplied it
  (``SWIFTSERVE_REPLICA_LABELS``). It is never inferred from a replica number.
- ``simulated`` is True only when whoever launched the router said so
  (``SWIFTSERVE_SIMULATED``); the router cannot detect fake replicas itself.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from swiftserve.events import EventBus
from swiftserve.policy import POLICIES
from swiftserve.state import ReplicaState
from swiftserve.telemetry import Telemetry

UI_DIR = Path(__file__).resolve().parent / "ui"

_GPU_UTIL_REASON = (
    "vLLM's /metrics does not expose GPU compute utilization or VRAM, and the router talks to replicas over HTTP "
    "only. GPU facts come from each node's replica sidecar (GET /sidecar/gpu, which runs nvidia-smi on that node). "
    "A replica without a sidecar, or with one older than GPU reporting, therefore shows no GPU data."
)


def _gpu_view(raw: dict | None) -> dict:
    """Normalise the sidecar's /sidecar/gpu answer for the UI. A GPU the node
    did not report is "unavailable" with the reason, never a zero."""
    if raw is None:
        return {"available": False, "reason": "not polled yet"}
    if not raw.get("available"):
        return {"available": False, "reason": raw.get("error") or "unavailable"}
    gpus = raw.get("gpus") or []
    g = gpus[0] if gpus else {}
    return {
        "available": True,
        "source": "nvidia-smi on the replica's own node (via its sidecar)",
        "count": len(gpus),
        "name": g.get("name"),
        "utilization_pct": g.get("utilization_pct"),
        "memory_used_mib": g.get("memory_used_mib"),
        "memory_total_mib": g.get("memory_total_mib"),
        "temperature_c": g.get("temperature_c"),
        "all": gpus,
    }


def _round(v: float | None, nd: int = 1) -> float | None:
    return None if v is None else round(v, nd)


def candidate_view(r: ReplicaState, available: bool) -> dict:
    """What the router knew about one replica at decision time. Read-only."""
    return {
        "replica_id": r.replica_id,
        "available": available,
        "circuit": r.circuit.state.value,
        "healthy": _health(r),
        "in_flight": r.in_flight,
        "queue_depth": r.queue_depth(),
        "est_latency_ms": _round(r.estimate_latency_ms()),
        "batch_capacity": r.effective_batch_capacity(),
    }


def _health(r: ReplicaState) -> bool | None:
    """True/False from the router's own /health probe; None until it has probed."""
    return r.metrics.healthy if r.metrics.health_checked else None


def _replica_snapshot(r: ReplicaState, label: str | None, telem: dict) -> dict:
    now = time.monotonic()
    m = r.metrics
    scraped_age = (now - m.last_scraped_monotonic) if m.last_scraped_monotonic else None
    status = r.status()
    gpu = _gpu_view(m.gpu)
    detected = gpu.get("name") if gpu["available"] else None
    return {
        "replica_id": r.replica_id,
        "base_url": r.base_url,
        "label": label,
        "gpu": gpu,
        "gpu_mapping": {
            "known": label is not None or detected is not None,
            "label": label,
            "detected": detected,
            "source": "; ".join(
                x for x in (
                    "label: SWIFTSERVE_REPLICA_LABELS (operator-supplied)" if label is not None else "",
                    "detected: nvidia-smi on the replica's node" if detected else "",
                ) if x
            ) or None,
            "note": None if (label is not None or detected is not None)
            else "GPU mapping unavailable: the router only knows this replica's URL",
        },
        "healthy": _health(r),
        "circuit": status["circuit"],
        "in_flight": r.in_flight,
        "queue_depth": r.queue_depth(),
        "est_latency_ms": _round(r.estimate_latency_ms()),
        "ewma_latency_ms": _round(r.ewma_latency_ms),
        "batch_capacity": r.effective_batch_capacity(),
        "tracked_sessions": status["tracked_sessions"],
        "occupancy_bucket_latency_ms": status["occupancy_bucket_latency_ms"],
        "scraped": {
            "running": m.running if scraped_age is not None else None,
            "waiting": m.waiting if scraped_age is not None else None,
            "kv_cache_usage": _round(m.gpu_cache_usage_perc, 4) if m.gpu_cache_usage_seen else None,
            "age_s": _round(scraped_age, 1),
        },
        "true_prefix_hit_rate": _round(r.true_prefix_hit_rate, 4),
        "stats": telem,
    }


def build_snapshot(
    *,
    replicas: list[ReplicaState],
    policy: object,
    settings: object,
    admission: object,
    telemetry: Telemetry,
    bus: EventBus,
    started_wall: float,
) -> dict:
    s = settings
    labels: list[str] = list(getattr(s, "replica_labels", []) or [])
    telem = telemetry.snapshot()
    per = telem["per_replica"]
    empty_stats = {  # a replica nothing has been routed to yet: real zeros, not missing data
        "selected": 0, "dispatch_attempts": 0, "retries": 0, "completed_ok": 0, "completed_error": 0,
        "client_disconnected": 0, "in_progress": 0, "prompt_tokens": 0, "completion_tokens": 0,
        "cached_tokens": 0, "requests_with_usage": 0,
        "ttft_ms": {"n": 0, "mean": None, "p50": None, "p95": None},
        "itl_mean_ms": {"n": 0, "mean": None, "p50": None, "p95": None},
        "tpot_ms": {"n": 0, "mean": None, "p50": None, "p95": None},
        "total_ms": {"n": 0, "mean": None, "p50": None, "p95": None},
    }
    replica_rows = [
        _replica_snapshot(r, labels[i] if i < len(labels) else None, per.get(str(r.replica_id), empty_stats))
        for i, r in enumerate(replicas)
    ]

    policy_state: dict = {"name": getattr(policy, "name", type(policy).__name__)}
    index = getattr(policy, "index", None)
    if index is not None:
        policy_state["prefix_index"] = index.status()
        policy_state["tokenizer"] = getattr(policy, "tokenizer_name", None)
        policy_state["thresholds"] = {
            "min_ratio": getattr(policy, "cache_threshold", None),
            "min_tokens": getattr(policy, "min_match_tokens", None),
        }
        policy_state["namespace"] = getattr(policy, "namespace", None)
    trie = getattr(policy, "prefix_trie", None)
    if trie is not None:
        policy_state["prefix_trie"] = trie.status()
    policy_state["session_affinity_ttl_s"] = getattr(s, "cache_affinity_ttl_s", None)

    warnings: list[str] = []
    if policy_state["name"] in {"prefix_aware", "swiftserve"} and getattr(s, "assumed_max_batch_size", 1) == 1:
        warnings.append(
            "SWIFTSERVE_ASSUMED_MAX_BATCH_SIZE is 1 (the default): the latency model then treats a replica with a "
            "single request in flight as saturated, which can make the SLA check reject a cache-warm replica. Set it "
            "to your replicas' vLLM --max-num-seqs."
        )
    if policy_state["name"] == "prefix_aware" and not getattr(s, "tokenizer", ""):
        warnings.append(
            "prefix_aware is using the byte-chunk fallback tokenizer (SWIFTSERVE_TOKENIZER is empty): its block "
            "boundaries do not match a real engine's, so cache predictions are not meaningful against real vLLM."
        )
    if not labels:
        warnings.append("Replica-to-GPU mapping is not configured (SWIFTSERVE_REPLICA_LABELS); GPUs are shown as unmapped.")

    gpu_ok = [r for r in replicas if r.metrics.gpu and r.metrics.gpu.get("available")]
    gpu_reasons = sorted({_gpu_view(r.metrics.gpu).get("reason") or "" for r in replicas if r not in gpu_ok} - {""})
    gpu_availability = {
        "available": bool(gpu_ok),
        "reporting_replicas": [r.replica_id for r in gpu_ok],
        "reason": None if len(gpu_ok) == len(replicas) else (_GPU_UTIL_REASON + (" Reported reasons: " + " | ".join(gpu_reasons) if gpu_reasons else "")),
        "source": "nvidia-smi via each replica's sidecar (/sidecar/gpu)",
    }
    seen_kv = any(r.metrics.gpu_cache_usage_seen for r in replicas)
    any_hit_rate = any(r.true_prefix_hit_rate is not None for r in replicas)
    return {
        "server_time": time.time(),
        "uptime_s": round(time.time() - started_wall, 1),
        "mode": {
            "simulated": bool(getattr(s, "simulated", False)),
            "note": "replicas are fake (no model, no GPU); every latency and token number is simulated"
            if getattr(s, "simulated", False)
            else "replicas are whatever SWIFTSERVE_REPLICAS points at; the router does not verify they are real GPUs",
            "policy_switch": {"enabled": bool(getattr(s, "allow_policy_switch", False)), "policies": list(POLICIES)},
        },
        "router": {
            "policy": policy_state["name"],
            "model": getattr(s, "model_name", None),
            "default_sla_ms": getattr(s, "default_sla_ms", None),
            "block_size": getattr(s, "block_size", None),
            "admission": admission.status() if hasattr(admission, "status") else None,
            "events": bus.status(),
        },
        "policy_state": policy_state,
        "replicas": replica_rows,
        "totals": telem["counters"],
        "throughput": telem["throughput"],
        "metric_availability": {
            "gpu_utilization": gpu_availability,
            "gpu_memory": gpu_availability,
            "kv_cache_usage": {
                "available": seen_kv,
                "source": "vLLM /metrics gauge vllm:gpu_cache_usage_perc or vllm:kv_cache_usage_perc",
                "note": "fraction of the replica's KV-cache blocks in use; this is NOT GPU compute utilization",
            },
            "ttft": {"available": True, "scope": "streaming requests only; measured at the router from request receipt"},
            "itl": {"available": True, "scope": "streaming requests only; mean gap between content events"},
            "token_counts": {"available": True, "scope": "only for responses where the engine reports `usage`"},
            "engine_prefix_hit_rate": {
                "available": any_hit_rate,
                "source": "vLLM /metrics vllm:prefix_cache_hits / vllm:prefix_cache_queries",
            },
        },
        "warnings": warnings,
    }


def _sse(event: dict) -> bytes:
    return f"id: {event['seq']}\ndata: {json.dumps(event, separators=(',', ':'), default=str)}\n\n".encode()


def register_dashboard(
    app: FastAPI,
    *,
    replicas: list[ReplicaState],
    get_policy: Callable[[], object],
    settings: object,
    admission: object,
    telemetry: Telemetry,
    bus: EventBus,
    auth: Callable,
    stop_event: asyncio.Event | Callable[[], asyncio.Event],
    started_wall: float | None = None,
) -> None:
    started = started_wall if started_wall is not None else time.time()
    # Accept an Event or a callable returning the *current* one (the router's
    # lifespan creates a fresh Event on every startup).
    get_stop: Callable[[], asyncio.Event] = stop_event if callable(stop_event) else (lambda: stop_event)
    api = APIRouter(dependencies=[Depends(auth)])

    @api.get("/api/dashboard")
    async def dashboard():
        return build_snapshot(
            replicas=replicas, policy=get_policy(), settings=settings, admission=admission,
            telemetry=telemetry, bus=bus, started_wall=started,
        )

    @api.get("/api/events")
    async def events(since: int = 0, limit: int = 500):
        limit = max(1, min(limit, 5000))
        return {
            "events": [e.to_dict() for e in bus.since(since, limit)],
            "head_seq": bus.head_seq,
            "oldest_seq": bus.oldest_seq,
            "gap": bus.has_gap_after(since) if since else False,
        }

    @api.get("/api/events/stream")
    async def events_stream(
        request: Request, since: int | None = None, heartbeat_s: float = 15.0, max_duration_s: float = 30.0
    ):
        """SSE. With ``since`` (or a ``Last-Event-ID`` header) the buffered
        events after that sequence number are replayed first, then live events
        follow without a gap. With neither, it starts from "now".

        Each connection is closed after ``max_duration_s`` (default 30 s) and
        the client is expected to reconnect with ``Last-Event-ID`` (the bundled
        UI does, losing nothing). The cap exists because uvicorn will not run
        the app's shutdown hook until every open connection has closed, so an
        unbounded stream would otherwise stall a graceful shutdown."""
        last_id = since
        if last_id is None:
            header = request.headers.get("last-event-id")
            last_id = int(header) if header and header.isdigit() else bus.head_seq
        heartbeat_s = max(0.05, min(heartbeat_s, 60.0))
        deadline = time.monotonic() + max(0.2, min(max_duration_s, 300.0))

        async def gen() -> AsyncIterator[bytes]:
            # Subscribe *before* reading the backlog: both happen with no
            # await between them, so no event can fall in the crack.
            sub = bus.subscribe()
            try:
                sent = last_id
                if bus.has_gap_after(last_id):
                    yield (
                        "event: gap\ndata: "
                        + json.dumps({"oldest_seq": bus.oldest_seq, "head_seq": bus.head_seq})
                        + "\n\n"
                    ).encode()
                for e in bus.since(last_id):
                    sent = e.seq
                    yield _sse(e.to_dict())
                last_out = time.monotonic()
                while not get_stop().is_set() and time.monotonic() < deadline:
                    # Wake at least every 0.5 s so shutdown is noticed promptly;
                    # a keepalive comment is only written every `heartbeat_s`.
                    ev = await sub.get(timeout=min(heartbeat_s, 0.5))
                    now = time.monotonic()
                    if ev is None:
                        if sub.overflowed:
                            break
                        if now - last_out >= heartbeat_s:
                            last_out = now
                            yield b": keepalive\n\n"
                        continue
                    if ev.seq <= sent:
                        continue
                    sent = ev.seq
                    last_out = now
                    yield _sse(ev.to_dict())
                    if sub.overflowed and sub.empty():
                        break
                if sub.overflowed:
                    yield (
                        "event: gap\ndata: "
                        + json.dumps({"reason": "subscriber_overflow", "head_seq": bus.head_seq})
                        + "\n\n"
                    ).encode()
            finally:
                sub.close()

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @api.get("/api/requests")
    async def requests_list(limit: int = 100):
        return {"requests": [r.to_dict() for r in telemetry.recent(max(1, min(limit, 1000)))]}

    @api.get("/api/requests/{request_id}")
    async def request_detail(request_id: str):
        rec = telemetry.record(request_id)
        if rec is None:
            raise HTTPException(status_code=404, detail="unknown request id (never seen, or aged out of the buffer)")
        return {"request": rec.to_dict(), "events": [e.to_dict() for e in bus.for_request(request_id)]}

    app.include_router(api)

    @app.get("/", include_in_schema=False)
    async def root():
        return RedirectResponse("/ui/")

    @app.get("/ui", include_in_schema=False)
    async def ui_redirect():
        return RedirectResponse("/ui/")

    app.mount("/ui", StaticFiles(directory=str(UI_DIR), html=True), name="ui")


__all__ = ["UI_DIR", "build_snapshot", "candidate_view", "register_dashboard"]
