"""Tests for swiftserve.proxy's handling of upstream responses that don't
round-trip cleanly through JSON -- a broken replica, an intermediate proxy's
HTML error page, or an empty body should never crash the router with an
unhandled exception after the circuit breaker/metrics have already recorded
the outcome."""

from __future__ import annotations

import httpx
import pytest

from swiftserve.proxy import forward_chat_completion
from swiftserve.state import ReplicaState


def _make_replica() -> ReplicaState:
    return ReplicaState(replica_id=0, base_url="http://fake", cache_ttl_s=60.0)


@pytest.mark.asyncio
async def test_non_json_upstream_response_passes_through_raw_body():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not json at all", headers={"content-type": "text/plain"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    replica = _make_replica()
    completions: list[tuple[float, bool]] = []

    response = await forward_chat_completion(
        client=client,
        replica=replica,
        path="/v1/chat/completions",
        body=b'{"messages": []}',
        headers={},
        is_streaming=False,
        timeout_s=5.0,
        on_complete=lambda latency_ms, success: completions.append((latency_ms, success)),
    )

    assert response.status_code == 200
    assert response.body == b"not json at all"
    # A 2xx is still recorded as success even though the body wasn't JSON --
    # the circuit breaker cares about the HTTP status, not body shape.
    assert len(completions) == 1
    assert completions[0][1] is True
    await client.aclose()


@pytest.mark.asyncio
async def test_non_json_5xx_upstream_response_still_trips_failure_accounting():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"<html>internal error</html>", headers={"content-type": "text/html"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    replica = _make_replica()
    completions: list[tuple[float, bool]] = []

    response = await forward_chat_completion(
        client=client,
        replica=replica,
        path="/v1/chat/completions",
        body=b'{"messages": []}',
        headers={},
        is_streaming=False,
        timeout_s=5.0,
        on_complete=lambda latency_ms, success: completions.append((latency_ms, success)),
    )

    assert response.status_code == 500
    assert response.body == b"<html>internal error</html>"
    assert completions == [(completions[0][0], False)]
    assert replica.circuit.status()["consecutive_failures"] == 1


@pytest.mark.asyncio
async def test_valid_json_upstream_response_still_decoded_normally():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "hi"}}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    replica = _make_replica()

    response = await forward_chat_completion(
        client=client,
        replica=replica,
        path="/v1/chat/completions",
        body=b'{"messages": []}',
        headers={},
        is_streaming=False,
        timeout_s=5.0,
        on_complete=lambda latency_ms, success: None,
    )

    assert response.status_code == 200
    assert b"choices" in response.body
    await client.aclose()
