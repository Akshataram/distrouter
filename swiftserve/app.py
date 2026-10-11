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
import time
import uuid
from contextlib import asynccontextmanager

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, ValidationError

from swiftserve import metrics
from swiftserve.config import settings
from swiftserve.dashboard_api import candidate_view, register_dashboard
from swiftserve.events import EventBus
from swiftserve.metrics_scraper import scrape_loop
from swiftserve.policy import POLICIES, Policy, PrefixAwarePolicy, SwiftServePolicy
from swiftserve.prefix_index import PrefixIndex
from swiftserve.prefix_trie import PrefixCacheTrie
from swiftserve.proxy import forward_chat_completion
from swiftserve.request_context import configure_logging, get_request_id, reset_request_id, set_request_id
from swiftserve.resilience import AdmissionController, CircuitState
from swiftserve.state import ReplicaState
from swiftserve.telemetry import Telemetry, watch_replica_state
from swiftserve.tokenization import build_tokenizer

configure_logging()
logger = logging.getLogger("swiftserve.app")

replicas: list[ReplicaState] = [
    ReplicaState(
        replica_id=i,
        base_url=url,
        cache_ttl_s=settings.cache_affinity_ttl_s,
        circuit_failure_threshold=settings.circuit_failure_threshold,
        circuit_reset_timeout_s=settings.circuit_reset_timeout_s,
        circuit_max_reset_timeout_s=settings.circuit_max_reset_timeout_s,
        assumed_max_batch_size=settings.assumed_max_batch_size,
        cold_start_ms_per_token=settings.cold_start_ms_per_token,
    )
    for i, url in enumerate(settings.replica_urls)
]

if settings.policy not in POLICIES:
    raise ValueError(f"Unknown SWIFTSERVE_POLICY={settings.policy!r}; choose from {list(POLICIES)}")
policy: Policy
if settings.policy == "swiftserve":
    # Special-cased (rather than POLICIES[settings.policy]()) so the
    # cross-session prefix trie picks up the same cache TTL and depth cap
    # as the rest of the router's config, instead of SwiftServePolicy's
    # zero-arg default.
    policy = SwiftServePolicy(PrefixCacheTrie(ttl_s=settings.cache_affinity_ttl_s, max_depth=settings.prefix_trie_max_depth))
elif settings.policy == "prefix_aware":
    # Also special-cased: it needs a tokenizer and a block index sized from
    # config, which a zero-arg constructor cannot supply.
    _tokenizer = build_tokenizer(settings.model_name, settings.tokenizer)
    policy = PrefixAwarePolicy(
        tokenizer=_tokenizer,
        index=PrefixIndex(block_size=settings.block_size, max_blocks_per_replica=settings.index_max_blocks),
        cache_threshold=settings.cache_threshold,
        min_match_tokens=settings.min_match_tokens,
        # Namespacing by model keeps hashes from one model's cache being
        # credited to another's if the router is ever repointed.
        namespace=settings.model_name,
    )
    # Fail fast rather than silently behaving like a load balancer: a
    # tokenizer that returns too few ids yields no full blocks, so the index
    # matches nothing and every cache prediction is 0. That is invisible in
    # the output -- it cost a whole 4-GPU experiment run to notice.
    _probe = [{"role": "system", "content": "probe " * 400}, {"role": "user", "content": "hi"}]
    _probe_tokens = _tokenizer.encode_chat(_probe)
    _probe_blocks = len(_probe_tokens) // settings.block_size
    if _probe_blocks < 2:
        raise RuntimeError(
            f"tokenizer {_tokenizer.name!r} produced only {len(_probe_tokens)} tokens for a "
            f"~400-word probe prompt ({_probe_blocks} blocks of {settings.block_size}). The prefix "
            f"index cannot work with this, and prefix_aware would silently degrade to least-loaded. "
            f"Check SWIFTSERVE_TOKENIZER and the installed transformers version."
        )
    logger.info(
        "prefix index live: tokenizer=%s, probe produced %d tokens = %d blocks of %d",
        _tokenizer.name, len(_probe_tokens), _probe_blocks, settings.block_size,
    )
    if not settings.tokenizer:
        logger.warning(
            "policy=prefix_aware is using the ByteChunkTokenizer fallback (SWIFTSERVE_TOKENIZER unset). "
            "Its block boundaries do NOT match the engine's, so cache predictions will be "
            "systematically wrong. Set SWIFTSERVE_TOKENIZER=%s for real predictions.",
            settings.model_name,
        )
else:
    policy = POLICIES[settings.policy]()
admission = AdmissionController(max_in_flight=settings.admission_max_in_flight)

# Observability (see swiftserve/events.py and telemetry.py): a bounded in-process
# event stream plus per-request records. Purely additive -- routing never reads them.
bus = EventBus(capacity=settings.event_buffer_size)
telemetry = Telemetry(bus, record_capacity=settings.record_buffer_size)
_started_wall = time.time()

_http_client: httpx.AsyncClient | None = None
_scrape_stop_event = asyncio.Event()
_scrape_task: asyncio.Task | None = None
_watch_task: asyncio.Task | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _http_client, _scrape_task, _watch_task, _scrape_stop_event
    # A fresh event per startup: an asyncio.Event binds to the event loop that first
    # waits on it, and a stopped one stays stopped. The production server starts once,
    # but anything that starts the app twice in one process (tests, embedding) needs this.
    _scrape_stop_event = asyncio.Event()
    _http_client = httpx.AsyncClient()
    _scrape_task = asyncio.create_task(scrape_loop(replicas, settings.metrics_scrape_interval_s, _scrape_stop_event))
    _watch_task = asyncio.create_task(watch_replica_state(replicas, bus, 1.0, _scrape_stop_event))
    logger.info(
        "SwiftServe up | model=%s | policy=%s | replicas=%s",
        settings.model_name, policy.name, [r.base_url for r in replicas],
    )
    if not settings.api_token:
        logger.warning(
            "SWIFTSERVE_API_TOKEN not set: /v1/chat/completions, /status, and /metrics are "
            "unauthenticated. Fine for a private demo, not for anything exposed beyond a trusted network."
        )
    yield
    _scrape_stop_event.set()  # also ends open SSE streams and the replica-state watcher
    if _scrape_task:
        await _scrape_task
    if _watch_task:
        await _watch_task
    await _http_client.aclose()


app = FastAPI(title="SwiftServe", lifespan=lifespan)


@app.middleware("http")
async def _request_id_middleware(request: Request, call_next):
    """Accept an inbound X-Request-Id (e.g. from a caller correlating its
    own logs, or a replica echoing back a router-issued ID on an unrelated
    request) or mint a new one, make it available to every log line in this
    request's call chain via the request_context contextvar, and echo it
    back so the caller can correlate this response with router/proxy logs."""
    request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
    request.state.request_id = request_id
    token = set_request_id(request_id)
    try:
        response = await call_next(request)
    finally:
        reset_request_id(token)
    response.headers["X-Request-Id"] = request_id
    return response


async def _require_api_token(authorization: str | None = Header(default=None)) -> None:
    """No-op when SWIFTSERVE_API_TOKEN is unset, preserving today's
    unauthenticated behavior by default. When set, requires an exact
    ``Authorization: Bearer <token>`` match -- same bearer-token shape as
    replica_sidecar.py's X-Chaos-Token admin API, applied here to the
    endpoints that either spend real inference capacity
    (/v1/chat/completions) or leak operational detail (/status, /metrics)."""
    if not settings.api_token:
        return
    if authorization != f"Bearer {settings.api_token}":
        raise HTTPException(status_code=401, detail="missing or invalid Authorization bearer token")


register_dashboard(
    app,
    replicas=replicas,
    get_policy=lambda: policy,
    settings=settings,
    admission=admission,
    telemetry=telemetry,
    bus=bus,
    auth=_require_api_token,
    stop_event=lambda: _scrape_stop_event,  # looked up at use time: lifespan replaces it on each startup
    started_wall=_started_wall,
)


def _take_decision(p: object) -> dict | None:
    """The policy's own account of its last choice (`last_decision`), copied
    immediately after `select` returns. There is no `await` between the two,
    so under asyncio concurrency another request cannot overwrite it first.
    Policies that publish nothing yield None, and nothing is invented."""
    d = getattr(p, "last_decision", None)
    return dict(d) if isinstance(d, dict) else None


def _prompt_chars(messages: object) -> int:
    if not isinstance(messages, list):
        return 0
    return sum(len(str(m.get("content", ""))) for m in messages if isinstance(m, dict))


def _pick_half_open_probe(candidates: list[ReplicaState]) -> ReplicaState:
    """Which currently-half-open replica gets this request's probe slot.

    Picking whichever replica happens to sort first (e.g. always the
    lowest replica_id) would let it monopolize every probe request until
    its own half_open_max_probes is exhausted before any other
    simultaneously-recovering replica gets a single probe. Preferring the
    replica with the fewest probes already outstanding spreads probe
    traffic evenly across all of them instead, so several replicas
    recovering from a correlated failure all get a chance to prove
    themselves at roughly the same rate."""
    return min(candidates, key=lambda r: (r.circuit.half_open_in_flight, r.replica_id))


class ChatCompletionRequest(BaseModel):
    """Shape validation only, never reserialized: the original raw bytes
    (not this model) are what get forwarded upstream, so an extra field
    vLLM accepts but this model doesn't know about is never dropped."""

    model_config = ConfigDict(extra="allow")

    messages: list[dict] | None = None
    stream: bool = False
    user: str | None = None


@app.get("/healthz")
async def healthz():
    return {"status": "ok", "policy": policy.name, "model": settings.model_name}


@app.get("/metrics", dependencies=[Depends(_require_api_token)])
async def router_metrics():
    metrics.refresh_replica_gauges(replicas)
    return Response(content=metrics.render_latest(), media_type=metrics.CONTENT_TYPE_LATEST)


@app.get("/status", dependencies=[Depends(_require_api_token)])
async def status():
    payload: dict[str, object] = {
        "model": settings.model_name,
        "policy": policy.name,
        "admission": admission.status(),
        "replicas": [r.status() for r in replicas],
    }
    index = getattr(policy, "index", None)
    if index is not None:
        payload["prefix_index"] = index.status()
        # Which tokenizer is live matters for interpreting every cache
        # prediction below it, so it is reported rather than implied.
        payload["tokenizer"] = getattr(policy, "tokenizer_name", None)
    return payload


@app.post("/v1/chat/completions", dependencies=[Depends(_require_api_token)])
async def chat_completions(request: Request):
    assert _http_client is not None  # set by lifespan(), which runs before any request is served
    request_id = get_request_id()
    raw_body = await request.body()
    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError:
        telemetry.reject_invalid(request_id, "invalid JSON body")
        return JSONResponse(status_code=400, content={"error": "invalid JSON body"})

    if not isinstance(payload, dict):
        telemetry.reject_invalid(request_id, "request body must be a JSON object")
        return JSONResponse(status_code=400, content={"error": "request body must be a JSON object"})

    try:
        ChatCompletionRequest.model_validate(payload)
    except ValidationError as exc:
        telemetry.reject_invalid(request_id, f"invalid request body: {exc.errors()[0]['msg']}")
        return JSONResponse(status_code=400, content={"error": f"invalid request body: {exc.errors()[0]['msg']}"})

    sla_header = request.headers.get("x-sla-ms")
    if sla_header is None:
        sla_ms = settings.default_sla_ms
    else:
        try:
            sla_ms = float(sla_header)
        except ValueError:
            telemetry.reject_invalid(request_id, "X-SLA-Ms header must be a number")
            return JSONResponse(status_code=400, content={"error": "X-SLA-Ms header must be a number"})

    session_id = request.headers.get("x-session-id") or payload.get("user") or str(uuid.uuid4())
    is_streaming = bool(payload.get("stream", False))
    messages = payload.get("messages") or []
    max_tokens = payload.get("max_tokens")
    trace = telemetry.begin(
        request_id,
        session_id=session_id,
        stream=is_streaming,
        sla_ms=sla_ms,
        policy=policy.name,
        messages=len(messages) if isinstance(messages, list) else 0,
        prompt_chars=_prompt_chars(messages),
        max_tokens=max_tokens if isinstance(max_tokens, int) else None,
    )

    if not admission.try_acquire():
        metrics.record_admission_rejected()
        trace.rejected("admission", http_status=503, detail="at max in-flight capacity")
        return JSONResponse(
            status_code=503,
            content={"error": "server busy: at max in-flight capacity, retry shortly"},
            headers={"Retry-After": "1"},
        )

    available = [r for r in replicas if not r.circuit.is_open() and r.circuit.has_probe_capacity()]
    if not available:
        admission.release()
        metrics.record_all_circuits_open()
        trace.rejected("all_circuits_open", http_status=503, detail="every replica is circuit-open")
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
    # How many prompt tokens the router *predicts* are already warm on the
    # replica it picked. 0 when the policy has no block-level opinion (the
    # baselines, or a half-open probe that overrode the policy). Published
    # as a response header so the benchmark can score this prediction
    # against the engine's own reported `cached_tokens` -- the whole point
    # being that it is a prediction and its error is measurable.
    predicted_cached_tokens = 0
    # What the router knew about every replica at the instant of the decision
    # (read-only; taken before the policy runs so it is what the policy saw).
    candidates = [candidate_view(r, r in available) for r in replicas]
    explain: dict | None
    route_path: str
    if half_open_probes:
        chosen = _pick_half_open_probe(half_open_probes)
        route_path = "half_open_probe"
        explain = {"note": "a recovering (half-open) replica gets the next request; the policy was not consulted"}
    elif hasattr(policy, "select_with_prediction"):
        chosen, predicted_cached_tokens = policy.select_with_prediction(
            session_id, sla_ms, available, messages
        )
        explain = _take_decision(policy)
        route_path = (explain or {}).get("path", policy.name)
    else:
        chosen = policy.select(session_id, sla_ms, available, messages)
        explain = _take_decision(policy)
        route_path = (explain or {}).get("path", policy.name)
    trace.routed(
        chosen.replica_id,
        path=route_path,
        explain=explain,
        candidates=candidates,
        predicted_cached_tokens=predicted_cached_tokens if hasattr(policy, "select_with_prediction") and not half_open_probes else None,
        half_open_override=bool(half_open_probes),
    )
    was_cache_hit = chosen.has_warm_cache(session_id)
    chosen.touch_session(session_id)
    chosen.circuit.mark_dispatched()
    occupancy_at_dispatch = chosen.queue_depth()
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
            # A fast failure (upstream 5xx, connect error) never "met SLA"
            # just because it came back quickly -- meeting SLA requires
            # actually succeeding, not merely responding fast.
            sla_violated=(not success) or (latency_ms > sla_ms),
        )

    # `x-request-id` is excluded here (case-insensitively) because it is set
    # explicitly just below; copying the caller's header AND setting ours used
    # to send the header twice, which a server sees as "id, id".
    forward_headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in {"host", "content-length", "x-request-id"}
    }
    # Always present, even if the caller didn't send one: the middleware
    # already generated an ID for this request, and the replica/sidecar
    # should log against the same one rather than minting its own.
    forward_headers["X-Request-Id"] = get_request_id()
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
            occupancy_at_dispatch=occupancy_at_dispatch,
            trace=trace,
        )
    except httpx.HTTPError as exc:
        logger.error("upstream request to replica %s failed: %s", chosen.replica_id, exc)
        return JSONResponse(status_code=502, content={"error": f"upstream replica unreachable: {exc}"})
    except Exception as exc:
        # Not an upstream failure: a bug on the router side. Record it so the
        # request does not sit in "dispatched" forever, then let it propagate unchanged.
        trace.failed(chosen.replica_id, stage="router_error", error=f"{type(exc).__name__}: {exc}", upstream_ms=None)
        raise

    response.headers["X-SwiftServe-Cache-Hit"] = "true" if was_cache_hit else "false"
    response.headers["X-SwiftServe-Predicted-Cached-Tokens"] = str(predicted_cached_tokens)
    return response
