"""Integration test for the open-loop Poisson-arrival load generator, over
a real local HTTP server (no GPU, no vLLM -- a fake chat-completions
endpoint standing in for a router). The pure-math logic (sla_attainment,
find_goodput) is tested separately in test_benchmark_stats.py without any
network at all; this file exists to prove the arrival-scheduling mechanism
itself (asyncio.create_task per Poisson-spaced arrival, shared pooled
client, drain-on-deadline) actually produces well-formed results over real
sockets, not just that the math is right in isolation.

The fake router speaks real SSE now (content-delta chunks, a final
usage-bearing chunk, then [DONE]) since scripts/benchmark.py always
streams (`"stream": true, "stream_options": {"include_usage": true}`) --
a plain single JSON blob response is no longer representative of what a
real router/vLLM sends back."""

from __future__ import annotations

import asyncio
import json

import pytest
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

from scripts.benchmark import run_goodput_sweep, run_open_loop_arrivals


def make_fake_router() -> FastAPI:
    app = FastAPI()

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request):
        async def event_stream():
            for word in ("hello", " there", " friend"):
                chunk = {"id": "chatcmpl-fake", "choices": [{"index": 0, "delta": {"content": word}}]}
                yield f"data: {json.dumps(chunk)}\n\n".encode()
            usage_chunk = {
                "id": "chatcmpl-fake",
                "choices": [],
                "usage": {
                    "prompt_tokens": 20,
                    "completion_tokens": 3,
                    "prompt_tokens_details": {"cached_tokens": 5},
                },
            }
            yield f"data: {json.dumps(usage_chunk)}\n\n".encode()
            yield b"data: [DONE]\n\n"

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={"X-SwiftServe-Replica": "0", "X-SwiftServe-Cache-Hit": "true"},
        )

    return app


@pytest.fixture
async def fake_router_url():
    config = uvicorn.Config(make_fake_router(), host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(config)
    serve_task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await serve_task


@pytest.mark.asyncio
async def test_open_loop_arrivals_produces_well_formed_results(fake_router_url):
    results = await run_open_loop_arrivals(
        router_url=fake_router_url, model="fake-model", target_rps=15.0, duration_s=0.4,
        turns=1, sla_ms=5000.0, max_tokens=16, seed=1,
    )
    assert len(results) > 0
    for r in results:
        assert "latency_ms" in r
        assert r["status"] == 200
        assert r["sla_violated"] is False  # generous SLA, fast local fake server
        # Real SSE parsing: ttft/tpot measured, usage fields pulled from
        # the final usage chunk, cache-hit header read through.
        assert r["ttft_ms"] is not None
        assert r["prompt_tokens"] == 20
        assert r["completion_tokens"] == 3
        assert r["cached_tokens"] == 5
        assert r["cache_hit"] == "true"
        assert r["replica"] == "0"


@pytest.mark.asyncio
async def test_open_loop_arrivals_rejects_non_positive_rps():
    with pytest.raises(ValueError):
        await run_open_loop_arrivals(
            router_url="http://127.0.0.1:1", model="m", target_rps=0.0,
            duration_s=0.1, turns=1, sla_ms=1000.0, max_tokens=16, seed=1,
        )


@pytest.mark.asyncio
async def test_goodput_sweep_produces_well_formed_report(fake_router_url):
    report = await run_goodput_sweep(
        router_url=fake_router_url, policy_label="fake", model="fake-model",
        rps_levels=[5.0, 15.0], duration_s=0.3, turns=1, sla_ms=5000.0, max_tokens=16, seed=1,
    )
    assert report["policy"] == "fake"
    assert len(report["levels"]) == 2
    for level in report["levels"]:
        assert level["offered_requests"] > 0
        assert level["completed_requests"] == level["offered_requests"]
        assert level["attainment"] == 1.0  # fast local fake server, generous SLA


@pytest.mark.asyncio
async def test_open_loop_arrivals_with_workload_cycles_through_sessions(fake_router_url):
    from scripts.workloads import shared_system_prompt

    workload = shared_system_prompt(num_apps=2, sessions=3, turns=1, seed=1)
    results = await run_open_loop_arrivals(
        router_url=fake_router_url, model="fake-model", target_rps=15.0, duration_s=0.3,
        turns=1, sla_ms=5000.0, max_tokens=16, seed=1, workload=workload,
    )
    assert len(results) > 0
    for r in results:
        assert r["status"] == 200
        assert r["ttft_slo_ms"] == workload.ttft_slo_ms
