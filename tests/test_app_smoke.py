"""Smoke tests for the FastAPI app wiring, using a fake in-process vLLM
stand-in (httpx ASGITransport) instead of real GPU replicas -- this checks
the request path (extract session/SLA -> pick replica -> forward -> return),
not real inference behavior."""

import json

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

import swiftserve.app as app_module


def make_fake_vllm(reply_text: str) -> FastAPI:
    fake = FastAPI()

    @fake.get("/health")
    async def health():
        return {"status": "ok"}

    @fake.get("/metrics")
    async def metrics():
        return "vllm:num_requests_running 0\nvllm:num_requests_waiting 0\nvllm:gpu_cache_usage_perc 0.1\n"

    @fake.post("/v1/chat/completions")
    async def chat_completions(request: Request):
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
