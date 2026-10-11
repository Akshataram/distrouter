"""Tests for swiftserve/telemetry.py: the SSE stream timer (TTFT / ITL / TPOT
arithmetic with an injected clock), request records, per-replica aggregates,
and the rule that observability can never break a request."""

import json

import pytest

from swiftserve.events import EventBus
from swiftserve.telemetry import NULL_TRACE, StreamTimer, Telemetry, usage_fields


def sse(obj) -> bytes:
    return f"data: {json.dumps(obj)}\n\n".encode()


def content(text):
    return sse({"choices": [{"delta": {"content": text}}]})


ROLE_ONLY = sse({"choices": [{"delta": {"role": "assistant", "content": ""}}]})
DONE = b"data: [DONE]\n\n"


# -- StreamTimer ---------------------------------------------------------------


def test_ttft_is_first_non_empty_content_not_first_byte():
    t = StreamTimer()
    assert t.feed(ROLE_ONLY, now=10.0) is False                    # first byte, but no text yet
    assert t.feed(content("Hel"), now=10.5) is True                # first content event
    assert t.feed(content("lo"), now=10.6) is False                # not the first any more
    assert t.ttfb_ms(start=9.0) == pytest.approx(1000.0)
    assert t.ttft_ms(start=9.0) == pytest.approx(1500.0)


def test_itl_mean_is_span_over_gaps_and_tpot_uses_engine_token_count():
    # Hypothetical timestamps (seconds): content events at 1.00, 1.05, 1.10, 1.25
    t = StreamTimer()
    for now in (1.00, 1.05, 1.10, 1.25):
        t.feed(content("x"), now=now)
    t.feed(sse({"choices": [], "usage": {"prompt_tokens": 100, "completion_tokens": 4, "prompt_tokens_details": {"cached_tokens": 64}}}), now=1.3)
    t.feed(DONE, now=1.31)
    assert t.content_events == 4
    assert t.itl_mean_ms() == pytest.approx((1.25 - 1.00) / 3 * 1000)   # 83.33 ms
    assert t.tpot_ms() == pytest.approx((1.25 - 1.00) / (4 - 1) * 1000)  # same here: 1 event == 1 token
    assert t.done_seen is True
    assert usage_fields(t.usage) == {"prompt_tokens": 100, "completion_tokens": 4, "cached_tokens": 64}


def test_itl_mean_is_unaffected_by_events_coalesced_into_one_network_read():
    # Same four events as above but delivered as two reads of two events each.
    a, b = StreamTimer(), StreamTimer()
    for now in (1.0, 1.1, 1.2, 1.3):
        a.feed(content("x"), now=now)
    b.feed(content("x") + content("x"), now=1.0)   # events 1+2 arrive together at t=1.0 ...
    b.feed(content("x") + content("x"), now=1.3)   # ... events 3+4 together at t=1.3
    assert a.itl_mean_ms() == pytest.approx(b.itl_mean_ms())


def test_events_split_across_network_reads_are_reassembled():
    t = StreamTimer()
    raw = content("Hello")
    assert t.feed(raw[:10], now=1.0) is False      # half an event: nothing yet
    assert t.feed(raw[10:], now=1.2) is True       # completed -> counted at the time the last byte arrived
    assert t.first_content_t == 1.2


def test_itl_needs_two_events_and_tpot_needs_usage():
    t = StreamTimer()
    t.feed(content("x"), now=1.0)
    assert t.itl_mean_ms() is None and t.tpot_ms() is None


def test_malformed_and_foreign_lines_are_ignored_not_fatal():
    t = StreamTimer()
    t.feed(b": keepalive\n\n", now=1.0)
    t.feed(b"data: {not json\n\n", now=1.1)
    t.feed(b"event: ping\n\n", now=1.2)
    t.feed(b"data: [1,2,3]\n\n", now=1.3)
    t.feed(content(""), now=1.4)                    # empty content never counts as a token
    assert t.content_events == 0 and t.malformed_events == 1 and t.first_content_t is None


def test_oversized_unterminated_line_is_discarded():
    t = StreamTimer()
    t.feed(b"data: " + b"x" * 2_000_000, now=1.0)
    assert t.malformed_events == 1 and t._buf == b""


def test_usage_fields_distinguishes_unknown_from_zero():
    assert usage_fields(None) == {"prompt_tokens": None, "completion_tokens": None, "cached_tokens": None}
    assert usage_fields({"prompt_tokens": 5, "completion_tokens": 2})["cached_tokens"] is None   # not reported != 0
    assert usage_fields({"prompt_tokens_details": {"cached_tokens": 0}})["cached_tokens"] == 0


# -- records, aggregates ----------------------------------------------------------------


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def make_hub(clock=None, **kw):
    bus = EventBus()
    hub = Telemetry(bus, clock=clock or Clock(), wall=lambda: 1_700_000_000.0, **kw)
    return hub, bus


def begin(hub, rid="r1", stream=True):
    return hub.begin(rid, session_id="s", stream=stream, sla_ms=3000.0, policy="p", messages=2, prompt_chars=40, max_tokens=8)


def test_full_lifecycle_records_and_emits_in_order():
    clock = Clock()
    hub, bus = make_hub(clock)
    trace = begin(hub)
    clock.t += 0.002
    trace.routed(1, path="prefix_match", explain={"path": "prefix_match"}, candidates=[{"replica_id": 1}], predicted_cached_tokens=64, half_open_override=False)
    trace.dispatch(1, 1)
    trace.upstream_headers(1, 200)
    timer = StreamTimer(clock=clock)
    clock.t += 0.050
    timer.feed(content("a"), now=clock.t)
    trace.first_token(1, timer)
    clock.t += 0.030
    timer.feed(content("b"), now=clock.t)
    timer.feed(sse({"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 2, "prompt_tokens_details": {"cached_tokens": 4}}}), now=clock.t)
    timer.feed(DONE, now=clock.t)
    trace.completed(1, http_status=200, ok=True, upstream_ms=82.0, timer=timer)

    rec = hub.record("r1")
    assert rec.state == "completed" and rec.selected_replica == 1 and rec.served_replica == 1
    assert rec.ttft_ms == pytest.approx(52.0, abs=0.01)         # 2 ms routing + 50 ms to first content
    assert rec.itl_mean_ms == pytest.approx(30.0, abs=0.01)
    assert rec.total_ms == pytest.approx(82.0, abs=0.01)
    assert (rec.prompt_tokens, rec.completion_tokens, rec.cached_tokens) == (10, 2, 4)
    assert [e.type for e in bus.since(0)] == [
        "request.received", "routing.decision", "dispatch", "upstream.headers", "first_token", "request.completed"]
    assert hub.counters["completed"] == 1
    assert hub.stats_for(1).to_dict()["ttft_ms"]["n"] == 1


def test_non_streaming_has_no_ttft_or_itl_but_has_token_counts():
    hub, _ = make_hub()
    trace = begin(hub, stream=False)
    trace.routed(0, path="x", explain=None, candidates=[], predicted_cached_tokens=None, half_open_override=False)
    trace.dispatch(0, 1)
    trace.completed(0, http_status=200, ok=True, upstream_ms=10.0, usage={"prompt_tokens": 7, "completion_tokens": 3})
    rec = hub.record("r1")
    assert rec.ttft_ms is None and rec.itl_mean_ms is None and rec.tpot_ms is None
    assert (rec.prompt_tokens, rec.completion_tokens, rec.cached_tokens) == (7, 3, None)


def test_selected_dispatched_and_served_are_tracked_separately():
    """Model-level check: the record keeps the three notions apart, so a future
    failover (selected R0, retried on R1, served by R1) would be reported
    truthfully. (Today's proxy only retries the same replica.)"""
    hub, bus = make_hub()
    trace = begin(hub)
    trace.routed(0, path="x", explain=None, candidates=[], predicted_cached_tokens=None, half_open_override=False)
    trace.dispatch(0, 1)
    trace.dispatch(1, 2)
    trace.completed(1, http_status=200, ok=True, upstream_ms=5.0)
    rec = hub.record("r1")
    assert rec.selected_replica == 0 and [d["replica"] for d in rec.dispatches] == [0, 1] and rec.served_replica == 1
    flags = [e.to_dict()["same_as_selected"] for e in bus.since(0) if e.type == "dispatch"]
    assert flags == [True, False]


def test_failure_has_no_served_replica_and_clears_in_progress():
    hub, bus = make_hub()
    trace = begin(hub)
    trace.routed(2, path="x", explain=None, candidates=[], predicted_cached_tokens=None, half_open_override=False)
    trace.dispatch(2, 1)
    assert hub.stats_for(2).to_dict()["in_progress"] == 1
    trace.failed(2, stage="connect", error="ConnectError: boom", upstream_ms=3.0)
    rec = hub.record("r1")
    assert rec.state == "failed" and rec.served_replica is None and rec.error_stage == "connect"
    assert hub.stats_for(2).to_dict()["in_progress"] == 0 and hub.stats_for(2).completed_error == 1
    assert bus.since(0)[-1].type == "request.failed"


def test_terminal_state_is_set_exactly_once():
    hub, bus = make_hub()
    trace = begin(hub)
    trace.dispatch(0, 1)
    trace.client_disconnected(0, upstream_ms=1.0)
    trace.completed(0, http_status=200, ok=True, upstream_ms=2.0)     # late, must be ignored
    trace.failed(0, stage="stream", error="x", upstream_ms=2.0)
    assert hub.record("r1").state == "client_disconnected"
    assert [e.type for e in bus.since(0)].count("request.client_disconnected") == 1
    assert hub.counters["completed"] == 0 and hub.stats_for(0).client_disconnected == 1


def test_upstream_5xx_is_a_failed_request_with_status():
    hub, _ = make_hub()
    trace = begin(hub)
    trace.dispatch(0, 1)
    trace.completed(0, http_status=500, ok=False, upstream_ms=4.0)
    rec = hub.record("r1")
    assert rec.state == "failed" and rec.http_status == 500 and rec.error_stage == "upstream_status" and rec.served_replica is None


def test_rejections_are_recorded_and_counted():
    hub, bus = make_hub()
    begin(hub, "a").rejected("admission", http_status=503, detail="busy")
    hub.reject_invalid("b", "invalid JSON body")
    assert hub.record("a").state == "rejected" and hub.record("b").reject_reason == "invalid_request"
    assert hub.counters["rejected_admission"] == 1 and hub.counters["rejected_invalid_request"] == 1
    assert [e.type for e in bus.since(0)] == ["request.received", "request.rejected", "request.rejected"]


def test_record_buffer_is_bounded():
    hub, _ = make_hub(record_capacity=5)
    for i in range(20):
        begin(hub, f"r{i}")
    assert hub.record("r0") is None and hub.record("r19") is not None
    assert len(hub.recent(100)) == 5 and hub.recent(1)[0].request_id == "r19"   # newest first


def test_throughput_window_counts_only_recent_completions():
    clock = Clock()
    hub, _ = make_hub(clock, throughput_window_s=10.0)
    for i in range(4):
        trace = begin(hub, f"r{i}")
        trace.dispatch(0, 1)
        trace.completed(0, http_status=200, ok=True, upstream_ms=1.0, usage={"completion_tokens": 5})
    clock.t += 5.0
    tp = hub.throughput()
    assert tp["completed_requests"] == 4 and tp["window_s"] == pytest.approx(5.0)
    assert tp["requests_per_s"] == pytest.approx(0.8) and tp["completion_tokens_per_s"] == pytest.approx(4.0)
    clock.t += 20.0
    assert hub.throughput()["completed_requests"] == 0   # aged out of the window


def test_interleaved_requests_do_not_contaminate_each_other():
    hub, _ = make_hub()
    a, b = begin(hub, "A"), begin(hub, "B")
    a.routed(0, path="pa", explain={"who": "A"}, candidates=[], predicted_cached_tokens=1, half_open_override=False)
    b.routed(1, path="pb", explain={"who": "B"}, candidates=[], predicted_cached_tokens=2, half_open_override=False)
    b.dispatch(1, 1)
    a.dispatch(0, 1)
    a.completed(0, http_status=200, ok=True, upstream_ms=1.0, usage={"completion_tokens": 1})
    b.failed(1, stage="connect", error="x", upstream_ms=1.0)
    ra, rb = hub.record("A"), hub.record("B")
    assert (ra.selected_replica, ra.served_replica, ra.explain["who"], ra.state) == (0, 0, "A", "completed")
    assert (rb.selected_replica, rb.served_replica, rb.explain["who"], rb.state) == (1, None, "B", "failed")


def test_a_failure_inside_telemetry_never_reaches_the_caller():
    hub, _ = make_hub()
    trace = begin(hub)
    hub.stats_for = None  # type: ignore[assignment]   # sabotage: any use will raise TypeError
    trace.routed(0, path="x", explain=None, candidates=[], predicted_cached_tokens=None, half_open_override=False)  # must not raise
    trace.dispatch(0, 1)
    trace.completed(0, http_status=200, ok=True, upstream_ms=1.0)


def test_null_trace_accepts_every_call():
    NULL_TRACE.dispatch(0, 1)
    NULL_TRACE.completed(0, http_status=200, ok=True, upstream_ms=1.0, timer=None)
    NULL_TRACE.anything_at_all()
