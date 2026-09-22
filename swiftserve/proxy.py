"""Forwards a chat-completion request to the chosen vLLM replica, streaming
the response back to the client untouched.

A connect-level failure (the replica process is down, or a tunnel/network
hop between the router and a remote GPU node dropped) is retried a few
times with jittered backoff -- but only before anything has reached the
client: once the first response byte is on its way out, retrying would mean
replaying a partial reply, so streaming failures propagate as-is instead.
Every outcome (success, 5xx, or connect failure) reports into the
replica's circuit breaker so a consistently failing replica gets pulled out
of rotation instead of eating a retry budget on every single request.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Callable

import httpx
from fastapi import Response
from fastapi.responses import JSONResponse, StreamingResponse

from swiftserve.state import ReplicaState

logger = logging.getLogger("swiftserve.proxy")

_HOP_BY_HOP_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "content-length", "content-encoding",
}

# Only connect-level failures are retried here: nothing has been sent to
# vLLM yet, so retrying is always safe regardless of request idempotency.
_RETRYABLE_EXCEPTIONS = (httpx.ConnectError, httpx.ConnectTimeout)


async def _send_with_retry(
    build_request: Callable[[], httpx.Request],
    client: httpx.AsyncClient,
    stream: bool,
    max_attempts: int,
    base_delay_s: float,
) -> httpx.Response:
    last_exc: Exception = RuntimeError("max_attempts must be >= 1")
    for attempt in range(max_attempts):
        try:
            return await client.send(build_request(), stream=stream)
        except _RETRYABLE_EXCEPTIONS as exc:
            last_exc = exc
            if attempt == max_attempts - 1:
                break
            delay = base_delay_s * (2**attempt) * (0.5 + random.random())
            logger.warning(
                "connect attempt %d/%d failed (%s), retrying in %.2fs",
                attempt + 1, max_attempts, exc, delay,
            )
            await asyncio.sleep(delay)
    raise last_exc


async def forward_chat_completion(
    client: httpx.AsyncClient,
    replica: ReplicaState,
    path: str,
    body: bytes,
    headers: dict,
    is_streaming: bool,
    timeout_s: float,
    on_complete: Callable[[float, bool], None],
    max_retries: int = 2,
    retry_base_delay_s: float = 0.1,
    occupancy_at_dispatch: int = 0,
) -> Response:
    """``on_complete(latency_ms, success)`` fires exactly once, when the
    upstream call is fully done -- immediately for a non-streaming response
    (success or failure), or only once the stream has been fully drained
    for a streaming one, so ``latency_ms`` is always true end-to-end
    latency rather than time-to-first-byte. The caller uses it to release
    the in-flight slot / admission-control permit and record metrics at the
    right time; callers must not release those themselves.

    ``occupancy_at_dispatch`` is the replica's queue_depth() at the moment
    this request was chosen (before this request's own count was added) --
    it's passed straight through to ``record_completion_latency`` so the
    observed latency calibrates the occupancy bucket this request actually
    ran under, not the occupancy at completion time (which includes
    whatever else was dispatched in the meantime)."""
    upstream_url = f"{replica.base_url}{path}"
    start = time.monotonic()
    max_attempts = max_retries + 1

    def build_request() -> httpx.Request:
        return client.build_request("POST", upstream_url, content=body, headers=headers, timeout=timeout_s)

    if is_streaming:
        try:
            upstream_response = await _send_with_retry(
                build_request, client, stream=True, max_attempts=max_attempts, base_delay_s=retry_base_delay_s
            )
        except httpx.HTTPError:
            replica.circuit.record_failure()
            on_complete((time.monotonic() - start) * 1000.0, False)
            raise

        async def event_stream():
            stream_failed = False
            try:
                async for chunk in upstream_response.aiter_bytes():
                    yield chunk
            except httpx.HTTPError:
                stream_failed = True
                raise
            finally:
                elapsed_ms = (time.monotonic() - start) * 1000.0
                await upstream_response.aclose()
                replica.record_completion_latency(elapsed_ms, occupancy_at_dispatch=occupancy_at_dispatch)
                success = not stream_failed and upstream_response.status_code < 500
                if success:
                    replica.circuit.record_success()
                else:
                    replica.circuit.record_failure()
                on_complete(elapsed_ms, success)

        response_headers = {
            k: v for k, v in upstream_response.headers.items() if k.lower() not in _HOP_BY_HOP_HEADERS
        }
        response_headers["X-SwiftServe-Replica"] = str(replica.replica_id)
        return StreamingResponse(
            event_stream(),
            status_code=upstream_response.status_code,
            headers=response_headers,
            media_type=upstream_response.headers.get("content-type", "text/event-stream"),
        )

    try:
        upstream_response = await _send_with_retry(
            build_request, client, stream=False, max_attempts=max_attempts, base_delay_s=retry_base_delay_s
        )
    except httpx.HTTPError:
        replica.circuit.record_failure()
        on_complete((time.monotonic() - start) * 1000.0, False)
        raise

    elapsed_ms = (time.monotonic() - start) * 1000.0
    replica.record_completion_latency(elapsed_ms, occupancy_at_dispatch=occupancy_at_dispatch)
    success = upstream_response.status_code < 500
    if success:
        replica.circuit.record_success()
    else:
        replica.circuit.record_failure()
    on_complete(elapsed_ms, success)

    response_headers = {k: v for k, v in upstream_response.headers.items() if k.lower() not in _HOP_BY_HOP_HEADERS}
    response_headers["X-SwiftServe-Replica"] = str(replica.replica_id)
    try:
        content = upstream_response.json()
    except ValueError:
        # Upstream didn't actually return JSON (a broken replica, an error
        # page from an intermediate proxy, an empty body): pass the raw
        # bytes through with the upstream's real content-type instead of
        # crashing here, now that success/failure has already been recorded
        # correctly above based on the HTTP status code alone.
        return Response(
            content=upstream_response.content,
            status_code=upstream_response.status_code,
            headers=response_headers,
            media_type=upstream_response.headers.get("content-type"),
        )
    return JSONResponse(
        content=content,
        status_code=upstream_response.status_code,
        headers=response_headers,
    )
