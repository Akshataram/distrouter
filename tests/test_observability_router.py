"""The real router request path, observed. Upstream replicas are MOCKED
(httpx.MockTransport), so nothing here proves anything about real GPUs; it
proves what the router *records* about requests it routes:

- the caller's X-Request-Id threads through the router's events, the upstream
  request, the response header and the stored record;
- selected / dispatched / served replicas are reported accurately, including
  when connect errors are retried or a request fails;
- TTFT / ITL are measured at the router for streaming and absent otherwise;
- many concurrent requests never get each other's attribution;
- rejected, invalid and failed requests are all visible, never silently lost.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

import swiftserve.app as app_module
from swiftserve.policy import PrefixAwarePolicy
from swiftserve.prefix_index import PrefixIndex
from swiftserve.resilience import AdmissionController
from swiftserve.tokenization import ByteChunkTokenizer

BODY = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}


def sse(obj) -> bytes:
    return f"data: {json.dumps(obj)}\n\n".encode()


def stream_body(n=3, usage=True) -> bytes:
    parts = [sse({"choices": [{"delta": {"role": "assistant", "content": ""}}]})]
    parts += [sse({"choices": [{"delta": {"content": f"t{i} "}}]}) for i in range(n)]
    if usage:
        parts.append(sse({"choices": [], "usage": {"prompt_tokens": 12, "completion_tokens": n, "prompt_tokens_details": {"cached_tokens": 8}}}))
    parts.append(b"data: [DONE]\n\n")
    return b"".join(parts)


def ok_handler(seen=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            # get_list: the id must arrive as exactly ONE header line, not duplicated
            seen.append((request.headers.get_list("x-request-id"), request.url.host))
        body = json.loads(request.content)
        if body.get("stream"):
            return httpx.Response(200, content=stream_body(), headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}],
                                         "usage": {"prompt_tokens": 12, "completion_tokens": 1, "prompt_tokens_details": {"cached_tokens": 8}}})
    return handler


def types(env, rid):
    return [e.type for e in env.bus.for_request(rid)]


# -- request id propagation ------------------------------------------------------------


async def test_caller_request_id_flows_to_events_upstream_response_and_record(router_env):
    seen: list = []
    router_env.set_upstream(ok_handler(seen))
    async with router_env.client() as c:
        resp = await c.post("/v1/chat/completions", json=BODY, headers={"X-Request-Id": "ui-abc-123"})
    assert resp.status_code == 200 and resp.headers["x-request-id"] == "ui-abc-123"
    assert seen == [(["ui-abc-123"], "r0")]                                     # forwarded upstream, exactly once
    assert types(router_env, "ui-abc-123") == ["request.received", "routing.decision", "dispatch", "upstream.headers", "request.completed"]
    rec = router_env.telemetry.record("ui-abc-123")
    assert rec.state == "completed" and rec.served_replica == 0


async def test_generated_request_id_is_a_uuid_and_matches_the_record(router_env):
    router_env.set_upstream(ok_handler())
    async with router_env.client() as c:
        resp = await c.post("/v1/chat/completions", json=BODY)
    rid = resp.headers["x-request-id"]
    assert len(rid) == 36 and router_env.telemetry.record(rid) is not None


# -- selected / dispatched / served -------------------------------------------------------


async def test_selected_dispatched_and_served_agree_on_a_normal_request(router_env):
    router_env.set_upstream(ok_handler())
    async with router_env.client() as c:
        await c.post("/v1/chat/completions", json=BODY, headers={"X-Request-Id": "a"})   # round robin -> r0
        resp = await c.post("/v1/chat/completions", json=BODY, headers={"X-Request-Id": "b"})  # -> r1
    rec = router_env.telemetry.record("b")
    assert resp.headers["x-swiftserve-replica"] == "1"
    assert rec.selected_replica == 1 and [d["replica"] for d in rec.dispatches] == [1] and rec.served_replica == 1
    decision = next(e for e in router_env.bus.for_request("b") if e.type == "routing.decision").to_dict()
    assert decision["path"] == "round_robin" and decision["explain"]["slot"] == 1
    assert [c["replica_id"] for c in decision["candidates"]] == [0, 1, 2] and all(c["available"] for c in decision["candidates"])


async def test_connect_errors_are_retried_on_the_same_replica_and_reported(router_env):
    attempts = {"n": 0}

    def flaky(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] <= 2:
            raise httpx.ConnectError("tunnel flapped")
        return ok_handler()(request)

    router_env.set_upstream(flaky)
    async with router_env.client() as c:
        resp = await c.post("/v1/chat/completions", json=BODY, headers={"X-Request-Id": "retry-1"})
    assert resp.status_code == 200
    rec = router_env.telemetry.record("retry-1")
    assert [(d["attempt"], d["replica"]) for d in rec.dispatches] == [(1, 0), (2, 0), (3, 0)]
    assert len(rec.retries) == 2 and "ConnectError" in rec.retries[0]["error"]
    assert rec.state == "completed" and rec.served_replica == 0
    assert types(router_env, "retry-1").count("dispatch.retry") == 2


async def test_exhausted_retries_fail_with_no_served_replica(router_env):
    def dead(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    router_env.set_upstream(dead)
    async with router_env.client() as c:
        resp = await c.post("/v1/chat/completions", json=BODY, headers={"X-Request-Id": "dead-1"})
    assert resp.status_code == 502
    rec = router_env.telemetry.record("dead-1")
    assert rec.state == "failed" and rec.error_stage == "connect" and rec.served_replica is None
    assert rec.selected_replica == 0 and len(rec.dispatches) == 3          # 1 try + 2 retries (settings default)
    assert router_env.telemetry.stats_for(0).completed_error == 1
    assert router_env.replicas[0].in_flight == 0 and app_module.admission.status()["in_flight"] == 0   # nothing leaked


async def test_upstream_5xx_is_failed_and_attributed(router_env):
    router_env.set_upstream(lambda r: httpx.Response(500, json={"error": "boom"}))
    async with router_env.client() as c:
        resp = await c.post("/v1/chat/completions", json=BODY, headers={"X-Request-Id": "five"})
    assert resp.status_code == 500
    rec = router_env.telemetry.record("five")
    assert rec.state == "failed" and rec.http_status == 500 and rec.served_replica is None and rec.dispatches[0]["replica"] == 0


# -- latency measurement -------------------------------------------------------------------------


class SlowSSE(httpx.AsyncByteStream):
    """Upstream stream that really delivers chunks `gap_s` apart (MOCK of a replica)."""

    def __init__(self, chunks, gap_s):
        self.chunks, self.gap_s = chunks, gap_s

    async def __aiter__(self):
        for ch in self.chunks:
            await asyncio.sleep(self.gap_s)
            yield ch


async def test_streaming_ttft_and_itl_are_measured_at_the_router(router_env):
    gap = 0.04
    chunks = [sse({"choices": [{"delta": {"role": "assistant", "content": ""}}]})]
    chunks += [sse({"choices": [{"delta": {"content": f"t{i}"}}]}) for i in range(5)]
    chunks += [sse({"choices": [], "usage": {"prompt_tokens": 9, "completion_tokens": 5, "prompt_tokens_details": {"cached_tokens": 0}}}), b"data: [DONE]\n\n"]
    router_env.set_upstream(lambda r: httpx.Response(200, stream=SlowSSE(chunks, gap), headers={"content-type": "text/event-stream"}))
    async with router_env.client() as c:
        await c.post("/v1/chat/completions", json={**BODY, "stream": True}, headers={"X-Request-Id": "slow"})
    rec = router_env.telemetry.record("slow")
    # role-only chunk at ~40 ms, first content at ~80 ms: TTFB < TTFT (generous bounds: CI timers jitter)
    assert rec.ttfb_ms is not None and rec.ttft_ms is not None and rec.ttfb_ms < rec.ttft_ms
    assert 60 <= rec.ttft_ms <= 400
    assert 25 <= rec.itl_mean_ms <= 120 and rec.tpot_ms == pytest.approx(rec.itl_mean_ms, rel=0.05)
    assert rec.content_events == 5 and rec.completion_tokens == 5 and rec.stream_complete is True
    assert rec.total_ms >= rec.ttft_ms and "first_token" in types(router_env, "slow")


async def test_non_streaming_reports_tokens_but_no_ttft_or_itl(router_env):
    router_env.set_upstream(ok_handler())
    async with router_env.client() as c:
        await c.post("/v1/chat/completions", json=BODY, headers={"X-Request-Id": "ns"})
    rec = router_env.telemetry.record("ns")
    assert rec.ttft_ms is None and rec.itl_mean_ms is None and "first_token" not in types(router_env, "ns")
    assert (rec.prompt_tokens, rec.completion_tokens, rec.cached_tokens) == (12, 1, 8) and rec.total_ms is not None


async def test_streaming_usage_gives_cached_tokens(router_env):
    router_env.set_upstream(ok_handler())
    async with router_env.client() as c:
        await c.post("/v1/chat/completions", json={**BODY, "stream": True}, headers={"X-Request-Id": "s1"})
    rec = router_env.telemetry.record("s1")
    assert rec.cached_tokens == 8 and rec.prompt_tokens == 12 and rec.completion_tokens == 3


# -- concurrency -----------------------------------------------------------------------------------


async def test_concurrent_requests_keep_their_own_attribution(router_env):
    """30 overlapping requests across 3 mocked replicas. Each replica reports who
    it saw; the router's per-request records must match that exactly."""
    saw: dict[str, str] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.005 * (hash(request.headers["x-request-id"]) % 7))   # scramble completion order
        saw[request.headers["x-request-id"]] = request.url.host   # would KeyError if the header were duplicated
        return ok_handler()(request)

    router_env.set_upstream(handler)
    async with router_env.client() as c:
        resps = await asyncio.gather(*(c.post("/v1/chat/completions", json={**BODY, "stream": i % 2 == 0},
                                              headers={"X-Request-Id": f"c{i}"}) for i in range(30)))
    assert all(r.status_code == 200 for r in resps)
    for i, r in enumerate(resps):
        rid = f"c{i}"
        rec = router_env.telemetry.record(rid)
        expected = int(saw[rid][1:])                                   # host "r2" -> replica 2
        assert rec.selected_replica == rec.served_replica == expected == int(r.headers["x-swiftserve-replica"])
        assert {e.to_dict().get("replica") for e in router_env.bus.for_request(rid) if e.type in ("dispatch", "request.completed")} == {expected}
    assert sum(router_env.telemetry.stats_for(i).completed_ok for i in range(3)) == 30
    assert all(r.in_flight == 0 for r in router_env.replicas) and app_module.admission.status()["in_flight"] == 0
    assert all(len(router_env.telemetry.stats_for(i).in_progress) == 0 for i in range(3))


# -- rejection / validation ----------------------------------------------------------------------------


async def test_admission_rejection_is_visible_and_never_routed(router_env, monkeypatch):
    monkeypatch.setattr(app_module, "admission", AdmissionController(max_in_flight=0))
    router_env.set_upstream(ok_handler())
    async with router_env.client() as c:
        resp = await c.post("/v1/chat/completions", json=BODY, headers={"X-Request-Id": "busy"})
    assert resp.status_code == 503
    assert types(router_env, "busy") == ["request.received", "request.rejected"]
    rec = router_env.telemetry.record("busy")
    assert rec.state == "rejected" and rec.reject_reason == "admission" and rec.selected_replica is None and rec.dispatches == []


async def test_all_circuits_open_is_visible(router_env):
    for r in router_env.replicas:
        for _ in range(r.circuit.failure_threshold):
            r.circuit.record_failure()
    router_env.set_upstream(ok_handler())
    async with router_env.client() as c:
        resp = await c.post("/v1/chat/completions", json=BODY, headers={"X-Request-Id": "open"})
    assert resp.status_code == 503 and router_env.telemetry.record("open").reject_reason == "all_circuits_open"
    assert app_module.admission.status()["in_flight"] == 0


@pytest.mark.parametrize("kwargs,detail", [
    ({"content": b"{nope"}, "invalid JSON"),
    ({"json": ["x"]}, "JSON object"),
    ({"json": {"messages": "x"}}, "invalid request body"),
])
async def test_invalid_requests_are_recorded_not_silently_dropped(router_env, kwargs, detail):
    router_env.set_upstream(ok_handler())
    async with router_env.client() as c:
        resp = await c.post("/v1/chat/completions", headers={"X-Request-Id": "bad"}, **kwargs)
    assert resp.status_code == 400
    rec = router_env.telemetry.record("bad")
    assert rec.state == "rejected" and rec.reject_reason == "invalid_request" and detail in rec.error


# -- routing explanations ------------------------------------------------------------------------------------


async def test_prefix_aware_explanation_is_the_policys_own_record(router_env, monkeypatch):
    monkeypatch.setattr(app_module, "policy", PrefixAwarePolicy(ByteChunkTokenizer(), PrefixIndex()))
    router_env.set_upstream(ok_handler())
    sysmsg = "You are a careful support agent. " * 80
    body = lambda q: {"model": "m", "messages": [{"role": "system", "content": sysmsg}, {"role": "user", "content": q}]}  # noqa: E731
    async with router_env.client() as c:
        await c.post("/v1/chat/completions", json=body("one"), headers={"X-Request-Id": "p1"})
        await c.post("/v1/chat/completions", json=body("two"), headers={"X-Request-Id": "p2"})
    first, second = router_env.telemetry.record("p1"), router_env.telemetry.record("p2")
    assert first.route_path == "no_eligible_match" and first.explain["matched_blocks"] == {} and first.predicted_cached_tokens == 0
    assert second.route_path == "prefix_match" and second.selected_replica == first.selected_replica
    assert second.explain["best"]["matched_blocks"] == second.explain["matched_blocks"][str(second.selected_replica)]
    assert second.predicted_cached_tokens == second.explain["predicted_cached_tokens"] > 0


async def test_baseline_policies_report_no_prediction_not_zero(router_env):
    router_env.set_upstream(ok_handler())
    async with router_env.client() as c:
        await c.post("/v1/chat/completions", json=BODY, headers={"X-Request-Id": "rr"})
    assert router_env.telemetry.record("rr").predicted_cached_tokens is None


async def test_policy_that_publishes_nothing_gets_no_invented_reason(router_env, monkeypatch):
    class Silent:
        name = "silent"

        def select(self, session_id, sla_ms, replicas, messages=None):
            return replicas[1]

    monkeypatch.setattr(app_module, "policy", Silent())
    router_env.set_upstream(ok_handler())
    async with router_env.client() as c:
        await c.post("/v1/chat/completions", json=BODY, headers={"X-Request-Id": "silent"})
    rec = router_env.telemetry.record("silent")
    assert rec.explain is None and rec.route_path == "silent" and rec.selected_replica == 1


async def test_half_open_override_is_reported_as_such(router_env):
    r0 = router_env.replicas[0]
    object.__setattr__(r0.circuit, "failure_threshold", 1)
    r0.circuit.record_failure()
    r0.circuit._current_timeout_s = 0.0   # cooldown already elapsed -> reads as half-open
    assert r0.circuit.state.value == "half_open"
    router_env.set_upstream(ok_handler())
    async with router_env.client() as c:
        await c.post("/v1/chat/completions", json=BODY, headers={"X-Request-Id": "probe"})
    rec = router_env.telemetry.record("probe")
    assert rec.route_path == "half_open_probe" and rec.selected_replica == 0
    decision = next(e for e in router_env.bus.for_request("probe") if e.type == "routing.decision").to_dict()
    assert decision["half_open_override"] is True


# -- non-interference --------------------------------------------------------------------------------------------


async def test_telemetry_failure_does_not_break_routing(router_env, monkeypatch):
    def explode(*a, **k):
        raise RuntimeError("telemetry bug")

    monkeypatch.setattr(type(router_env.telemetry), "stats_for", explode)
    router_env.set_upstream(ok_handler())
    async with router_env.client() as c:
        resp = await c.post("/v1/chat/completions", json=BODY, headers={"X-Request-Id": "still-ok"})
    assert resp.status_code == 200 and resp.headers["x-swiftserve-replica"] == "0"
    assert router_env.replicas[0].in_flight == 0
