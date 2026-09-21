"""Smoke tests for the FastAPI app wiring, using a fake in-process vLLM
stand-in (httpx ASGITransport) instead of real GPU replicas -- this checks
the request path (extract session/SLA -> pick replica -> forward -> return),
not real inference behavior."""

import json
import time

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

import swiftserve.app as app_module
from swiftserve.state import ReplicaState


def make_fake_vllm(reply_text: str, capture_headers: dict | None = None) -> FastAPI:
    fake = FastAPI()

    @fake.get("/health")
    async def health():
        return {"status": "ok"}

    @fake.get("/metrics")
    async def metrics():
        return "vllm:num_requests_running 0\nvllm:num_requests_waiting 0\nvllm:gpu_cache_usage_perc 0.1\n"

    @fake.post("/v1/chat/completions")
    async def chat_completions(request: Request):
        if capture_headers is not None:
            capture_headers.update(request.headers)
        body = await request.json()
        return JSONResponse(
            {
                "id": "chatcmpl-fake",
                "choices": [{"message": {"role": "assistant", "content": f"{reply_text}:{body['messages'][-1]['content']}"}}],
            }
        )

    return fake


@pytest.mark.asyncio
async def test_chat_completions_routes_and_forwards():
    fake_replica = make_fake_vllm("hello")
    transport = httpx.ASGITransport(app=fake_replica)

    app_module.replicas[:] = app_module.replicas[:1]  # keep it simple: 1 fake replica
    app_module._http_client = httpx.AsyncClient(transport=transport, base_url="http://fake")
    app_module.replicas[0].base_url = "http://fake"

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_module.app), base_url="http://router") as client:
        resp = await client.post(
            "/v1/chat/completions",
            headers={"X-Session-Id": "sess-1"},
            content=json.dumps({"model": "Qwen/Qwen2.5-7B-Instruct", "messages": [{"role": "user", "content": "hi"}]}),
        )

    assert resp.status_code == 200
    assert resp.headers["x-swiftserve-replica"] == "0"
    assert resp.headers["x-swiftserve-cache-hit"] == "false"
    assert "hello:hi" in resp.json()["choices"][0]["message"]["content"]

    await app_module._http_client.aclose()


@pytest.mark.asyncio
async def test_second_turn_same_session_is_a_cache_hit():
    fake_replica = make_fake_vllm("hello")
    transport = httpx.ASGITransport(app=fake_replica)

    app_module.replicas[:] = app_module.replicas[:1]
    app_module._http_client = httpx.AsyncClient(transport=transport, base_url="http://fake")
    app_module.replicas[0].base_url = "http://fake"

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_module.app), base_url="http://router") as client:
        await client.post(
            "/v1/chat/completions",
            headers={"X-Session-Id": "sess-2"},
            content=json.dumps({"messages": [{"role": "user", "content": "turn1"}]}),
        )
        resp = await client.post(
            "/v1/chat/completions",
            headers={"X-Session-Id": "sess-2"},
            content=json.dumps({"messages": [{"role": "user", "content": "turn2"}]}),
        )

    assert resp.headers["x-swiftserve-cache-hit"] == "true"
    await app_module._http_client.aclose()


@pytest.mark.asyncio
async def test_malformed_json_body_is_400():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_module.app), base_url="http://router") as client:
        resp = await client.post("/v1/chat/completions", content=b"{not json")

    assert resp.status_code == 400
    assert "invalid JSON" in resp.json()["error"]


@pytest.mark.asyncio
async def test_non_object_json_body_is_400():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_module.app), base_url="http://router") as client:
        resp = await client.post("/v1/chat/completions", content=json.dumps(["not", "an", "object"]))

    assert resp.status_code == 400
    assert "JSON object" in resp.json()["error"]


@pytest.mark.asyncio
async def test_wrong_typed_messages_field_is_400():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_module.app), base_url="http://router") as client:
        resp = await client.post(
            "/v1/chat/completions",
            content=json.dumps({"messages": "not-a-list"}),
        )

    assert resp.status_code == 400
    assert "invalid request body" in resp.json()["error"]


@pytest.mark.asyncio
async def test_malformed_sla_header_is_400():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_module.app), base_url="http://router") as client:
        resp = await client.post(
            "/v1/chat/completions",
            headers={"X-SLA-Ms": "not-a-number"},
            content=json.dumps({"messages": [{"role": "user", "content": "hi"}]}),
        )

    assert resp.status_code == 400
    assert "X-SLA-Ms" in resp.json()["error"]


def _make_half_open_replica(replica_id: int) -> ReplicaState:
    r = ReplicaState(
        replica_id=replica_id,
        base_url=f"http://replica-{replica_id}",
        cache_ttl_s=600,
        circuit_failure_threshold=1,
        circuit_reset_timeout_s=0.01,
    )
    r.circuit.record_failure()
    time.sleep(0.02)
    assert r.circuit.state.value == "half_open"
    return r


def test_pick_half_open_probe_breaks_ties_by_replica_id():
    r0, r1 = _make_half_open_replica(0), _make_half_open_replica(1)
    assert app_module._pick_half_open_probe([r1, r0]) is r0  # order-independent


def test_pick_half_open_probe_spreads_across_simultaneously_recovering_replicas():
    r0, r1 = _make_half_open_replica(0), _make_half_open_replica(1)

    first = app_module._pick_half_open_probe([r0, r1])
    assert first is r0
    first.circuit.mark_dispatched()

    # replica 0 now has a probe outstanding: replica 1 gets the next one,
    # instead of replica 0 monopolizing every probe until it's resolved.
    second = app_module._pick_half_open_probe([r0, r1])
    assert second is r1


@pytest.mark.asyncio
async def test_status_unauthenticated_when_no_api_token_configured():
    assert app_module.settings.api_token is None  # default: no SWIFTSERVE_API_TOKEN set
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_module.app), base_url="http://router") as client:
        resp = await client.get("/status")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_auth_required_once_api_token_is_configured():
    # Settings is a frozen dataclass by design (immutable runtime config);
    # object.__setattr__ is the standard way to flip one field for a test.
    object.__setattr__(app_module.settings, "api_token", "secret-token")
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_module.app), base_url="http://router") as client:
            resp = await client.get("/status")
            assert resp.status_code == 401

            resp = await client.get("/status", headers={"Authorization": "Bearer wrong-token"})
            assert resp.status_code == 401

            resp = await client.get("/status", headers={"Authorization": "Bearer secret-token"})
            assert resp.status_code == 200
    finally:
        object.__setattr__(app_module.settings, "api_token", None)


@pytest.mark.asyncio
async def test_request_id_generated_and_echoed_when_absent():
    fake_replica = make_fake_vllm("hello")
    transport = httpx.ASGITransport(app=fake_replica)

    app_module.replicas[:] = app_module.replicas[:1]
    app_module._http_client = httpx.AsyncClient(transport=transport, base_url="http://fake")
    app_module.replicas[0].base_url = "http://fake"

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_module.app), base_url="http://router") as client:
        resp = await client.post(
            "/v1/chat/completions",
            content=json.dumps({"messages": [{"role": "user", "content": "hi"}]}),
        )

    request_id = resp.headers.get("x-request-id")
    assert request_id  # generated even though the caller didn't send one
    # a real UUID4, not a placeholder
    assert len(request_id) == 36 and request_id.count("-") == 4
    await app_module._http_client.aclose()


@pytest.mark.asyncio
async def test_request_id_echoed_back_and_forwarded_upstream_when_provided():
    captured: dict = {}
    fake_replica = make_fake_vllm("hello", capture_headers=captured)
    transport = httpx.ASGITransport(app=fake_replica)

    app_module.replicas[:] = app_module.replicas[:1]
    app_module._http_client = httpx.AsyncClient(transport=transport, base_url="http://fake")
    app_module.replicas[0].base_url = "http://fake"

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_module.app), base_url="http://router") as client:
        resp = await client.post(
            "/v1/chat/completions",
            headers={"X-Request-Id": "caller-supplied-id-123"},
            content=json.dumps({"messages": [{"role": "user", "content": "hi"}]}),
        )

    assert resp.headers["x-request-id"] == "caller-supplied-id-123"
    assert captured.get("x-request-id") == "caller-supplied-id-123"
    await app_module._http_client.aclose()
