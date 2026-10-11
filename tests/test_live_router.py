"""The real router app over real sockets, talking to real (in-process) HTTP
upstreams that stream slowly. The upstreams are MOCK replicas -- they are not
vLLM and not GPUs -- but everything between the client and them is the real
code path: uvicorn, the proxy, genuinely incremental SSE, genuine disconnects.

What this proves that ASGITransport cannot: events reach a subscriber *while* a
request is still streaming, a client that hangs up mid-stream is recorded and
leaks nothing, and the router shuts down gracefully with an event stream open.
"""

from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, StreamingResponse

import swiftserve.app as app_module
from swiftserve.policy import RoundRobinPolicy
from swiftserve.state import ReplicaState
from tests.live_server import LiveServer

TOKEN_GAP_S = 0.03


def sse(obj) -> bytes:
    return f"data: {json.dumps(obj)}\n\n".encode()


def make_upstream(name: str, gpu: dict | None):
    """A MOCK replica: health, metrics, optional sidecar GPU endpoint, slow streaming chat."""
    app = FastAPI()

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/metrics")
    async def metrics():
        return PlainTextResponse("vllm:num_requests_running 0\nvllm:num_requests_waiting 0\nvllm:kv_cache_usage_perc 0.25\n")

    if gpu is not None:
        @app.get("/sidecar/gpu")
        async def sidecar_gpu():
            return gpu

    @app.post("/v1/chat/completions")
    async def chat(request: Request):
        body = await request.json()
        n = int(body.get("max_tokens", 5))
        if not body.get("stream"):
            return {"choices": [{"message": {"content": name}}], "usage": {"prompt_tokens": 5, "completion_tokens": n}}

        async def gen():
            yield sse({"choices": [{"delta": {"role": "assistant", "content": ""}}]})
            for i in range(n):
                await asyncio.sleep(TOKEN_GAP_S)
                yield sse({"choices": [{"delta": {"content": f"{name}{i} "}}]})
            yield sse({"choices": [], "usage": {"prompt_tokens": 20, "completion_tokens": n, "prompt_tokens_details": {"cached_tokens": 16}}})
            yield b"data: [DONE]\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream")

    return app


T4 = {"available": True, "source": "nvidia-smi", "gpus": [
    {"index": 0, "name": "Tesla T4", "utilization_pct": 42.0, "memory_used_mib": 900.0, "memory_total_mib": 15360.0, "temperature_c": 48.0}]}


@pytest.fixture
async def cluster(monkeypatch):
    saved = list(app_module.replicas)
    up0, up1 = LiveServer(make_upstream("a", T4)), LiveServer(make_upstream("b", None))
    await up0.__aenter__()
    await up1.__aenter__()
    app_module.replicas[:] = [
        ReplicaState(0, up0.url, cache_ttl_s=600, assumed_max_batch_size=32),
        ReplicaState(1, up1.url, cache_ttl_s=600, assumed_max_batch_size=32),
    ]
    monkeypatch.setattr(app_module, "policy", RoundRobinPolicy())
    object.__setattr__(app_module.settings, "metrics_scrape_interval_s", 0.2)
    router = LiveServer(app_module.app, lifespan="on")
    await router.__aenter__()
    yield type("Cluster", (), {"router": router, "up": [up0, up1]})
    await router.__aexit__(None, None, None)
    for up in (up0, up1):
        if up.server.started and not up.server.should_exit:
            await up.__aexit__(None, None, None)
    app_module.replicas[:] = saved
    object.__setattr__(app_module.settings, "metrics_scrape_interval_s", 2.0)


async def wait_for(predicate, timeout=5.0, step=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(step)
    return False


async def test_events_reach_a_subscriber_while_the_request_is_still_streaming(cluster):
    seen: list[dict] = []
    stop_reading = asyncio.Event()

    async def subscribe():
        async with httpx.AsyncClient(timeout=None) as c, c.stream("GET", f"{cluster.router.url}/api/events/stream") as r:
            buf = ""
            async for chunk in r.aiter_text():
                buf += chunk
                while "\n\n" in buf:
                    raw, buf = buf.split("\n\n", 1)
                    for line in raw.splitlines():                       # a block is "id: N" then "data: {...}"
                        if line.startswith("data:"):
                            seen.append({**json.loads(line[5:]), "_seen_at": time.monotonic()})
                if stop_reading.is_set():
                    return

    task = asyncio.create_task(subscribe())
    assert await wait_for(lambda: app_module.bus.subscriber_count >= 1)
    async with httpx.AsyncClient(timeout=30) as c:
        started = time.monotonic()
        resp = await c.post(f"{cluster.router.url}/v1/chat/completions", headers={"X-Request-Id": "live-e2e"},
                            json={"model": "m", "stream": True, "max_tokens": 10, "messages": [{"role": "user", "content": "hi"}]})
        finished = time.monotonic()
    assert resp.status_code == 200
    assert await wait_for(lambda: any(e["type"] == "request.completed" and e["request_id"] == "live-e2e" for e in seen))
    stop_reading.set()
    app_module.bus.emit("noop", None)             # wake the subscriber so it can exit
    await asyncio.wait_for(task, 5)

    mine = [e for e in seen if e.get("request_id") == "live-e2e"]
    assert [e["type"] for e in mine] == ["request.received", "routing.decision", "dispatch", "upstream.headers", "first_token", "request.completed"]
    first_token = next(e for e in mine if e["type"] == "first_token")
    assert first_token["_seen_at"] < finished - 0.1       # delivered mid-stream, long before the 300 ms response ended
    done = mine[-1]
    assert done["ok"] and done["served_replica"] == int(resp.headers["x-swiftserve-replica"]) == done["selected_replica"]
    assert done["ttft_ms"] >= TOKEN_GAP_S * 1000 * 0.8 and 20 <= done["itl_mean_ms"] <= 150
    assert done["completion_tokens"] == 10 and done["cached_tokens"] == 16 and done["stream_complete"] is True
    assert started < first_token["_seen_at"]


async def test_client_disconnect_is_recorded_and_leaks_nothing(cluster):
    rec_before = app_module.telemetry.counters["client_disconnected"]
    async with httpx.AsyncClient(timeout=30) as c:
        async with c.stream("POST", f"{cluster.router.url}/v1/chat/completions", headers={"X-Request-Id": "hangup"},
                            json={"model": "m", "stream": True, "max_tokens": 200, "messages": [{"role": "user", "content": "hi"}]}) as r:
            got_one_chunk = False
            async for _ in r.aiter_bytes():
                got_one_chunk = True
                break                                                    # hang up mid-stream
        assert got_one_chunk
    assert await wait_for(lambda: (app_module.telemetry.record("hangup") or object()).__dict__.get("state") == "client_disconnected", timeout=5)
    rec = app_module.telemetry.record("hangup")
    assert rec.state == "client_disconnected" and rec.served_replica is None
    assert app_module.telemetry.counters["client_disconnected"] == rec_before + 1
    assert any(e.type == "request.client_disconnected" for e in app_module.bus.for_request("hangup"))
    assert await wait_for(lambda: all(r.in_flight == 0 for r in app_module.replicas) and app_module.admission.status()["in_flight"] == 0)


async def test_gpu_facts_are_polled_from_the_node_and_absence_is_reported_honestly(cluster):
    async with httpx.AsyncClient() as c:
        async def snap():
            return (await c.get(f"{cluster.router.url}/api/dashboard")).json()

        for _ in range(60):
            s = await snap()
            if s["replicas"][0]["gpu"]["available"] and s["replicas"][1]["gpu"].get("reason", "").startswith("this replica does not expose"):
                break
            await asyncio.sleep(0.1)
    a, b = s["replicas"]
    assert a["gpu"]["name"] == "Tesla T4" and a["gpu"]["utilization_pct"] == 42.0 and a["gpu_mapping"]["detected"] == "Tesla T4"
    assert b["gpu"]["available"] is False and "does not expose /sidecar/gpu" in b["gpu"]["reason"]
    assert a["scraped"]["kv_cache_usage"] == 0.25                      # the V1 metric name is understood
    assert s["metric_availability"]["gpu_utilization"]["reporting_replicas"] == [0]


async def test_a_replica_that_goes_away_produces_a_state_change_event(cluster):
    async with httpx.AsyncClient() as c:
        assert await wait_for(lambda: all(r.metrics.health_checked and r.metrics.healthy for r in app_module.replicas))
        # replica.state events come from a 1 s sampler; let it observe both replicas as healthy first
        # (a state that flips and flips back inside one sample interval is not reported -- documented limitation)
        assert await wait_for(lambda: {e.to_dict()["replica"] for e in app_module.bus.since(0)
                                       if e.type == "replica.state" and e.to_dict()["healthy"]["to"] is True} == {0, 1}, timeout=6)
        head = app_module.bus.head_seq
        await cluster.up[1].__aexit__(None, None, None)                  # replica 1 dies
        found = await wait_for(lambda: any(e.type == "replica.state" and e.to_dict()["replica"] == 1 and e.to_dict()["healthy"]["to"] is False
                                           for e in app_module.bus.since(head)), timeout=6)
        assert found, (f"healthy={[r.metrics.healthy for r in app_module.replicas]} "
                       f"events={[(e.type, e.to_dict().get('replica')) for e in app_module.bus.since(head)]} head={head} "
                       f"watch_done={app_module._watch_task.done() if app_module._watch_task else None}")
        snap = (await c.get(f"{cluster.router.url}/api/dashboard")).json()
    assert snap["replicas"][1]["healthy"] is False and snap["replicas"][0]["healthy"] is True


async def test_router_shuts_down_gracefully_with_an_event_stream_open(monkeypatch):
    saved = list(app_module.replicas)
    up = LiveServer(make_upstream("a", None))
    await up.__aenter__()
    app_module.replicas[:] = [ReplicaState(0, up.url, cache_ttl_s=600)]
    router = LiveServer(app_module.app, lifespan="on")
    await router.__aenter__()
    try:
        client = httpx.AsyncClient(timeout=None)
        stream_cm = client.stream("GET", f"{router.url}/api/events/stream", params={"max_duration_s": 1})
        resp = await stream_cm.__aenter__()
        assert resp.status_code == 200
        t0 = time.monotonic()
        await router.__aexit__(None, None, None)                        # must NOT hang on the open stream
        elapsed = time.monotonic() - t0
        await stream_cm.__aexit__(None, None, None)
        await client.aclose()
        assert elapsed < 5, f"shutdown took {elapsed:.1f}s with an SSE stream open"
    finally:
        await up.__aexit__(None, None, None)
        app_module.replicas[:] = saved
