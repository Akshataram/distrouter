"""Forwards a chat-completion request to the chosen vLLM replica, streaming
the response back to the client untouched.
"""

from __future__ import annotations

import time
from typing import Callable

import httpx
from fastapi import Response
from fastapi.responses import JSONResponse, StreamingResponse

from swiftserve.state import ReplicaState

_HOP_BY_HOP_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "content-length", "content-encoding",
}


async def forward_chat_completion(
    client: httpx.AsyncClient,
    replica: ReplicaState,
    path: str,
    body: bytes,
    headers: dict,
    is_streaming: bool,
    timeout_s: float,
    on_complete: Callable[[], None],
) -> Response:
    """``on_complete`` fires exactly once, when the upstream call is fully
    done -- immediately for a non-streaming response, or only once the
    stream has been fully drained for a streaming one. The caller uses it
    to release the in-flight slot and record latency at the right time."""
    upstream_url = f"{replica.base_url}{path}"
    start = time.monotonic()

    if is_streaming:
        req = client.build_request("POST", upstream_url, content=body, headers=headers, timeout=timeout_s)
        upstream_response = await client.send(req, stream=True)

        async def event_stream():
            try:
                async for chunk in upstream_response.aiter_bytes():
                    yield chunk
            finally:
                await upstream_response.aclose()
                replica.record_completion_latency((time.monotonic() - start) * 1000.0)
                on_complete()

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
        upstream_response = await client.post(upstream_url, content=body, headers=headers, timeout=timeout_s)
        replica.record_completion_latency((time.monotonic() - start) * 1000.0)
    finally:
        on_complete()

    response_headers = {k: v for k, v in upstream_response.headers.items() if k.lower() not in _HOP_BY_HOP_HEADERS}
    response_headers["X-SwiftServe-Replica"] = str(replica.replica_id)
    return JSONResponse(
        content=upstream_response.json(),
        status_code=upstream_response.status_code,
        headers=response_headers,
    )
