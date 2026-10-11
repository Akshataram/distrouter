"""The read-only observability API (swiftserve/dashboard_api.py): the snapshot's
honesty rules, the events/requests endpoints, auth, the static UI, and the live
SSE stream over a real socket.

Honesty rules under test: a metric nobody measured is "unavailable" with a
reason (never 0), GPU identity appears only when the operator configured it or
the node itself reported it, and `simulated` is only true when asked for."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, Header, HTTPException

from swiftserve.dashboard_api import build_snapshot, register_dashboard
from swiftserve.events import EventBus
from swiftserve.policy import PrefixAwarePolicy, RoundRobinPolicy, SwiftServePolicy
from swiftserve.prefix_index import PrefixIndex
from swiftserve.prefix_trie import PrefixCacheTrie
from swiftserve.resilience import AdmissionController
from swiftserve.state import ReplicaState
from swiftserve.telemetry import Telemetry
from swiftserve.tokenization import ByteChunkTokenizer
from tests.live_server import LiveServer


def settings(**over):
    base = dict(replica_labels=[], simulated=False, model_name="m", default_sla_ms=3000.0, block_size=16,
                assumed_max_batch_size=32, tokenizer="Qwen/x", cache_affinity_ttl_s=600.0)
    base.update(over)
    return SimpleNamespace(**base)


def make(n=2, policy=None, cfg=None, auth=None, **bus_kw):
    replicas = [ReplicaState(i, f"https://node{i}.trycloudflare.com", cache_ttl_s=600) for i in range(n)]
    bus = EventBus(**bus_kw)
    telemetry = Telemetry(bus)
    stop = asyncio.Event()
    app = FastAPI()

    async def no_auth():
        return None

    register_dashboard(
        app, replicas=replicas, get_policy=lambda: policy or RoundRobinPolicy(), settings=cfg or settings(),
        admission=AdmissionController(max_in_flight=10), telemetry=telemetry, bus=bus, auth=auth or no_auth, stop_event=stop,
    )
    return SimpleNamespace(app=app, replicas=replicas, bus=bus, telemetry=telemetry, stop=stop)


def snap(env, policy=None, cfg=None):
    return build_snapshot(replicas=env.replicas, policy=policy or RoundRobinPolicy(), settings=cfg or settings(),
                          admission=AdmissionController(max_in_flight=10), telemetry=env.telemetry, bus=env.bus, started_wall=0.0)


# -- honesty rules ---------------------------------------------------------------------------


def test_nothing_measured_means_unavailable_with_a_reason_not_zero():
    s = snap(make())
    r = s["replicas"][0]
    assert r["healthy"] is None                                 # not probed yet != unhealthy
    assert r["gpu"] == {"available": False, "reason": "not polled yet"}
    assert r["scraped"]["kv_cache_usage"] is None and r["scraped"]["running"] is None
    assert r["true_prefix_hit_rate"] is None
    assert r["gpu_mapping"]["known"] is False and "unavailable" in r["gpu_mapping"]["note"]
    assert s["metric_availability"]["gpu_utilization"]["available"] is False
    assert "nvidia-smi" in s["metric_availability"]["gpu_utilization"]["reason"]


def test_gpu_identity_comes_only_from_the_operator_or_the_node():
    env = make()
    env.replicas[0].metrics.gpu = {"available": True, "source": "nvidia-smi", "gpus": [
        {"index": 0, "name": "Tesla T4", "utilization_pct": 37.0, "memory_used_mib": 1200.0, "memory_total_mib": 15360.0, "temperature_c": 51.0}]}
    s = snap(env, cfg=settings(replica_labels=["colab-a", "colab-b"]))
    a, b = s["replicas"]
    assert a["gpu"]["available"] and a["gpu"]["name"] == "Tesla T4" and a["gpu"]["utilization_pct"] == 37.0
    assert a["gpu_mapping"] == {**a["gpu_mapping"], "known": True, "label": "colab-a", "detected": "Tesla T4"}
    assert b["gpu_mapping"]["label"] == "colab-b" and b["gpu_mapping"]["detected"] is None   # label only: nothing detected
    av = s["metric_availability"]["gpu_utilization"]
    assert av["available"] is True and av["reporting_replicas"] == [0] and av["reason"]      # partial: replica 1 is silent


def test_replica_number_alone_never_implies_a_gpu():
    s = snap(make(n=4))
    assert all(r["gpu_mapping"]["known"] is False and r["gpu_mapping"]["label"] is None for r in s["replicas"])


def test_sidecar_404_reason_is_passed_through():
    env = make()
    env.replicas[0].metrics.gpu = {"available": False, "error": "this replica does not expose /sidecar/gpu (...)"}
    assert "does not expose /sidecar/gpu" in snap(env)["replicas"][0]["gpu"]["reason"]


def test_kv_cache_usage_zero_is_distinguished_from_not_exposed():
    env = make()
    env.replicas[0].record_scrape(running=0, waiting=0, gpu_cache_usage_perc=0.0)     # engine says 0%
    env.replicas[1].record_scrape(running=0, waiting=0, gpu_cache_usage_perc=None)    # engine exposes nothing
    a, b = snap(env)["replicas"]
    assert a["scraped"]["kv_cache_usage"] == 0.0 and b["scraped"]["kv_cache_usage"] is None


def test_simulated_flag_is_only_what_the_launcher_said():
    assert snap(make())["mode"]["simulated"] is False
    s = snap(make(), cfg=settings(simulated=True))
    assert s["mode"]["simulated"] is True and "fake" in s["mode"]["note"]


def test_configuration_warnings_are_derived_from_real_settings():
    risky = snap(make(), policy=PrefixAwarePolicy(ByteChunkTokenizer(), PrefixIndex()), cfg=settings(assumed_max_batch_size=1, tokenizer=""))
    text = " ".join(risky["warnings"])
    assert "ASSUMED_MAX_BATCH_SIZE" in text and "byte-chunk" in text and "REPLICA_LABELS" in text
    clean = snap(make(), policy=PrefixAwarePolicy(ByteChunkTokenizer(), PrefixIndex()), cfg=settings(replica_labels=["a", "b"]))
    assert clean["warnings"] == []


def test_policy_state_exposes_the_real_index_or_trie():
    pa = PrefixAwarePolicy(ByteChunkTokenizer(), PrefixIndex())
    ps = snap(make(), policy=pa)["policy_state"]
    assert ps["prefix_index"]["block_size"] == 16 and ps["thresholds"] == {"min_ratio": 0.5, "min_tokens": 256}
    sw = snap(make(), policy=SwiftServePolicy(PrefixCacheTrie(ttl_s=60)))["policy_state"]
    assert "prefix_trie" in sw and "prefix_index" not in sw
    assert "prefix_index" not in snap(make(), policy=RoundRobinPolicy())["policy_state"]


def test_idle_replicas_report_real_zeros_and_traffic_shows_up():
    env = make()
    tr = env.telemetry.begin("r", session_id=None, stream=True, sla_ms=1, policy="p", messages=1, prompt_chars=1, max_tokens=1)
    tr.routed(1, path="x", explain=None, candidates=[], predicted_cached_tokens=None, half_open_override=False)
    tr.dispatch(1, 1)
    tr.completed(1, http_status=200, ok=True, upstream_ms=5.0, usage={"prompt_tokens": 4, "completion_tokens": 2})
    a, b = snap(env)["replicas"]
    assert a["stats"]["selected"] == 0 and a["stats"]["completed_ok"] == 0
    assert b["stats"]["selected"] == 1 and b["stats"]["completed_ok"] == 1 and b["stats"]["completion_tokens"] == 2


# -- HTTP endpoints ------------------------------------------------------------------------------


async def get(env, path, **kw):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=env.app), base_url="http://t") as c:
        return await c.get(path, **kw)


async def test_dashboard_endpoint_returns_the_snapshot():
    r = await get(make(), "/api/dashboard")
    assert r.status_code == 200 and r.json()["replicas"][0]["base_url"] == "https://node0.trycloudflare.com"


async def test_events_endpoint_since_limit_and_gap():
    env = make(capacity=4)
    for i in range(10):
        env.bus.emit("t", f"r{i}")
    body = (await get(env, "/api/events", params={"since": 0})).json()
    assert [e["seq"] for e in body["events"]] == [7, 8, 9, 10] and body["gap"] is False   # since=0 means "everything": no gap flag
    body = (await get(env, "/api/events", params={"since": 2})).json()
    assert body["gap"] is True and body["oldest_seq"] == 7
    body = (await get(env, "/api/events", params={"since": 8, "limit": 1})).json()
    assert [e["seq"] for e in body["events"]] == [10] and body["gap"] is False


async def test_requests_endpoints():
    env = make()
    for rid in ("a", "b"):
        env.telemetry.begin(rid, session_id=None, stream=False, sla_ms=1, policy="p", messages=1, prompt_chars=1, max_tokens=None)
    listing = (await get(env, "/api/requests")).json()["requests"]
    assert [r["request_id"] for r in listing] == ["b", "a"]                       # newest first
    detail = (await get(env, "/api/requests/a")).json()
    assert detail["request"]["request_id"] == "a" and [e["type"] for e in detail["events"]] == ["request.received"]
    assert (await get(env, "/api/requests/missing")).status_code == 404


async def test_api_requires_the_token_when_configured_but_the_ui_shell_does_not():
    async def auth(authorization: str | None = Header(default=None)):
        if authorization != "Bearer s3cret":
            raise HTTPException(status_code=401, detail="no")

    env = make(auth=auth)
    assert (await get(env, "/api/dashboard")).status_code == 401
    assert (await get(env, "/api/events/stream")).status_code == 401
    assert (await get(env, "/api/dashboard", headers={"Authorization": "Bearer s3cret"})).status_code == 200
    ui = await get(env, "/ui/")
    assert ui.status_code == 200 and "SwiftServe Console" in ui.text           # static shell carries no secrets


async def test_static_ui_files_and_root_redirect():
    env = make()
    js = await get(env, "/ui/js/store.js")
    assert js.status_code == 200 and "javascript" in js.headers["content-type"] and "class Store" in js.text
    css = await get(env, "/ui/styles.css")
    assert css.status_code == 200 and "text/css" in css.headers["content-type"]
    root = await get(env, "/", follow_redirects=False)
    assert root.status_code in (302, 307) and root.headers["location"] == "/ui/"


# -- live SSE over a real socket ------------------------------------------------------------------


async def collect(resp, want, timeout=3.0):
    """Read SSE blocks until `want(blocks)` is true; returns parsed blocks."""
    blocks: list[dict] = []

    async def reader():
        buf = ""
        async for chunk in resp.aiter_text():
            buf += chunk
            while "\n\n" in buf:
                raw, buf = buf.split("\n\n", 1)
                blk = {"comment": raw.startswith(":"), "event": "message", "id": None, "data": None}
                for line in raw.splitlines():
                    if line.startswith("event:"):
                        blk["event"] = line[6:].strip()
                    elif line.startswith("id:"):
                        blk["id"] = int(line[3:])
                    elif line.startswith("data:"):
                        blk["data"] = json.loads(line[5:])
                blocks.append(blk)
                if want(blocks):
                    return

    await asyncio.wait_for(reader(), timeout)
    return blocks


async def test_sse_delivers_new_events_live_and_cleans_up_on_disconnect():
    env = make()
    async with (
        LiveServer(env.app) as srv,
        httpx.AsyncClient(timeout=None) as c,
        c.stream("GET", f"{srv.url}/api/events/stream") as resp,
    ):
        assert resp.headers["content-type"].startswith("text/event-stream")
        for _ in range(100):                       # wait until the server-side subscription exists
            if env.bus.subscriber_count == 1:
                break
            await asyncio.sleep(0.01)
        env.bus.emit("request.received", "live-1", policy="p")
        env.bus.emit("dispatch", "live-1", replica=2, attempt=1)
        blocks = await collect(resp, lambda b: len([x for x in b if x["data"]]) >= 2)
        events = [b["data"] for b in blocks if b["data"]]
        assert [e["type"] for e in events] == ["request.received", "dispatch"] and events[1]["replica"] == 2
        assert [b["id"] for b in blocks if b["data"]] == [1, 2]
        for _ in range(100):
            if env.bus.subscriber_count == 0:
                break
            await asyncio.sleep(0.02)
        assert env.bus.subscriber_count == 0           # an abandoned stream does not leak a subscriber


async def test_sse_replays_from_since_then_continues_live_without_a_gap():
    env = make()
    for i in range(5):
        env.bus.emit("t", f"old{i}")
    async with (
        LiveServer(env.app) as srv,
        httpx.AsyncClient(timeout=None) as c,
        c.stream("GET", f"{srv.url}/api/events/stream", params={"since": 3}) as resp,
    ):
        env.bus.emit("t", "new")
        blocks = await collect(resp, lambda b: len([x for x in b if x["data"]]) >= 3)
    assert [b["data"]["request_id"] for b in blocks if b["data"]] == ["old3", "old4", "new"]


async def test_sse_honours_last_event_id_header_and_default_is_from_now():
    env = make()
    for i in range(3):
        env.bus.emit("t", f"old{i}")
    async with (
        LiveServer(env.app) as srv,
        httpx.AsyncClient(timeout=None) as c,
        c.stream("GET", f"{srv.url}/api/events/stream", headers={"Last-Event-ID": "2"}) as resp,
    ):
        blocks = await collect(resp, lambda b: any(x["data"] for x in b))
        assert blocks[0]["data"]["request_id"] == "old2"                       # resumed after id 2
        async with c.stream("GET", f"{srv.url}/api/events/stream", params={"heartbeat_s": 0.1}) as resp:
            blocks = await collect(resp, lambda b: any(x["comment"] for x in b))
        assert all(x["data"] is None for x in blocks)                          # no replay: started from "now"


async def test_sse_sends_keepalives_and_closes_at_max_duration():
    env = make()
    async with (
        LiveServer(env.app) as srv,
        httpx.AsyncClient(timeout=None) as c,
        c.stream("GET", f"{srv.url}/api/events/stream", params={"heartbeat_s": 0.1, "max_duration_s": 0.6}) as resp,
    ):
        async def read_all() -> str:
            return "".join([t async for t in resp.aiter_text()])     # returns only when the SERVER ends the stream

        text = await asyncio.wait_for(read_all(), timeout=3)
    assert text.count(": keepalive") >= 2                                   # idle connection stays alive...
    assert "data:" not in text                                              # ...without inventing events


async def test_sse_reports_a_gap_when_asked_for_events_that_were_evicted():
    env = make(capacity=3)
    for i in range(10):
        env.bus.emit("t", f"r{i}")
    async with (
        LiveServer(env.app) as srv,
        httpx.AsyncClient(timeout=None) as c,
        c.stream("GET", f"{srv.url}/api/events/stream", params={"since": 1}) as resp,
    ):
        blocks = await collect(resp, lambda b: len(b) >= 4)
    assert blocks[0]["event"] == "gap" and blocks[0]["data"]["oldest_seq"] == 8
    assert [b["data"]["request_id"] for b in blocks[1:] if b["data"]] == ["r7", "r8", "r9"]


async def test_sse_tells_a_too_slow_client_it_missed_events():
    env = make(subscriber_queue_size=1)
    async with (
        LiveServer(env.app) as srv,
        httpx.AsyncClient(timeout=None) as c,
        c.stream("GET", f"{srv.url}/api/events/stream") as resp,
    ):
        for _ in range(100):
            if env.bus.subscriber_count == 1:
                break
            await asyncio.sleep(0.01)
        for i in range(6):                       # burst with no await: the 1-slot queue overflows
            env.bus.emit("t", f"b{i}")
        blocks = await collect(resp, lambda b: any(x["event"] == "gap" for x in b))
    gap = next(b for b in blocks if b["event"] == "gap")
    assert gap["data"]["reason"] == "subscriber_overflow"


async def test_open_streams_end_when_the_app_is_shutting_down():
    env = make()
    async with (
        LiveServer(env.app) as srv,
        httpx.AsyncClient(timeout=None) as c,
        c.stream("GET", f"{srv.url}/api/events/stream") as resp,
    ):
        for _ in range(100):
            if env.bus.subscriber_count == 1:
                break
            await asyncio.sleep(0.01)
        env.stop.set()                                                   # what the router's lifespan does at shutdown
        await asyncio.wait_for(resp.aread(), timeout=3)                  # the stream closes promptly
    assert env.bus.subscriber_count == 0


@pytest.mark.parametrize("path", ["/api/dashboard", "/api/events", "/api/requests"])
async def test_endpoints_are_read_only(path):
    env = make()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=env.app), base_url="http://t") as c:
        assert (await c.post(path)).status_code == 405
