"""Sidecar tests run entirely in-process (ASGI transport for both the
sidecar and its fake upstream -- no real network, no GPU), plus a real
(GPU-free) subprocess for the ProcessSupervisor tests, since process
management needs an actual OS process to be meaningful."""

import time

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from swiftserve.replica_sidecar import ProcessSupervisor, create_app


def make_fake_upstream(reply_text: str = "ok"):
    fake = FastAPI()

    @fake.get("/health")
    async def health():
        return {"status": reply_text}

    @fake.post("/v1/chat/completions")
    async def chat(request: Request):
        body = await request.json()
        return JSONResponse({"echo": body["messages"][-1]["content"], "tag": reply_text})

    return fake


def make_sidecar(admin_token=None, upstream_reply="ok", supervisor=None):
    fake_upstream = make_fake_upstream(upstream_reply)
    fake_client = httpx.AsyncClient(transport=httpx.ASGITransport(app=fake_upstream), base_url="http://fake-upstream")
    app = create_app(
        upstream_url="http://fake-upstream", admin_token=admin_token, supervisor=supervisor, http_client=fake_client
    )
    return app


@pytest.mark.asyncio
async def test_passthrough_forwards_to_upstream():
    app = make_sidecar(upstream_reply="hello-from-vllm")
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://sidecar") as client:
            resp = await client.get("/health")
            assert resp.status_code == 200
            assert resp.json() == {"status": "hello-from-vllm"}

            resp = await client.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "hi"}]})
            assert resp.status_code == 200
            assert resp.json() == {"echo": "hi", "tag": "hello-from-vllm"}


@pytest.mark.asyncio
async def test_partition_blocks_requests_without_reaching_upstream():
    app = make_sidecar()
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://sidecar") as client:
            await client.post("/chaos/partition", params={"enabled": "true"})
            resp = await client.get("/health")
            assert resp.status_code == 503
            status = (await client.get("/chaos/status")).json()
            assert status["chaos"]["partitioned"] is True


@pytest.mark.asyncio
async def test_error_rate_one_always_injects_500():
    app = make_sidecar()
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://sidecar") as client:
            await client.post("/chaos/error-rate", params={"rate": "1.0"})
            resp = await client.get("/health")
            assert resp.status_code == 500


@pytest.mark.asyncio
async def test_error_rate_zero_never_injects():
    app = make_sidecar()
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://sidecar") as client:
            await client.post("/chaos/error-rate", params={"rate": "0.0"})
            for _ in range(10):
                resp = await client.get("/health")
                assert resp.status_code == 200


@pytest.mark.asyncio
async def test_extra_latency_delays_response():
    app = make_sidecar()
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://sidecar") as client:
            await client.post("/chaos/latency", params={"extra_ms": "80"})
            start = time.monotonic()
            resp = await client.get("/health")
            elapsed = time.monotonic() - start
            assert resp.status_code == 200
            assert elapsed >= 0.07


@pytest.mark.asyncio
async def test_chaos_reset_clears_all_faults():
    app = make_sidecar()
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://sidecar") as client:
            await client.post("/chaos/partition", params={"enabled": "true"})
            await client.post("/chaos/latency", params={"extra_ms": "50"})
            await client.post("/chaos/error-rate", params={"rate": "0.5"})
            await client.post("/chaos/reset")
            status = (await client.get("/chaos/status")).json()
            assert status["chaos"] == {"partitioned": False, "extra_latency_ms": 0.0, "error_rate": 0.0}


@pytest.mark.asyncio
async def test_admin_token_required_for_chaos_endpoints():
    app = make_sidecar(admin_token="secret123")
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://sidecar") as client:
            resp = await client.post("/chaos/partition", params={"enabled": "true"})
            assert resp.status_code == 403

            resp = await client.post(
                "/chaos/partition", params={"enabled": "true"}, headers={"X-Chaos-Token": "secret123"}
            )
            assert resp.status_code == 200


@pytest.mark.asyncio
async def test_kill_and_restart_without_supervise_are_rejected():
    app = make_sidecar()
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://sidecar") as client:
            resp = await client.post("/chaos/kill")
            assert resp.status_code == 400
            resp = await client.post("/chaos/restart")
            assert resp.status_code == 400


def test_process_supervisor_kill_and_restart_real_process():
    supervisor = ProcessSupervisor(["sleep", "30"])
    supervisor.start()
    try:
        assert supervisor.is_alive() is True

        result = supervisor.kill()
        assert result["killed"] is True
        assert supervisor.is_alive() is False

        result = supervisor.restart()
        assert result["restarted"] is True
        assert supervisor.is_alive() is True
    finally:
        supervisor.kill()


def test_unsupervised_is_alive_reports_none():
    supervisor = ProcessSupervisor(None)
    assert supervisor.is_alive() is None
