"""Pure tests for scripts/benchmark.py's parse_sse_stream and
dual_slo_attainment -- no network, hand-built fake SSE line sequences with
explicit (timestamp, line) pairs so ttft_ms/tpot_ms math is checked exactly."""

from __future__ import annotations

import json

import pytest

from scripts.benchmark import dual_slo_attainment, parse_sse_stream


def _chunk_line(content: str | None = None, usage: dict | None = None) -> str:
    choices = [{"index": 0, "delta": {"content": content}}] if content is not None else []
    body: dict = {"id": "chatcmpl-test", "choices": choices}
    if usage is not None:
        body["usage"] = usage
    return f"data: {json.dumps(body)}"


def test_parse_sse_stream_computes_ttft_and_tpot():
    # request starts at t=0.0; first content chunk at t=0.1 (ttft=100ms);
    # 3 content tokens total, last one at t=0.3 -> tpot = (0.3-0.1)*1000/(3-1) = 100ms
    lines = [
        (0.1, _chunk_line(content="Hello")),
        (0.2, _chunk_line(content=" there")),
        (0.3, _chunk_line(content=" friend")),
        (0.31, _chunk_line(usage={"prompt_tokens": 50, "completion_tokens": 3, "prompt_tokens_details": {"cached_tokens": 10}})),
        (0.32, "data: [DONE]"),
    ]
    result = parse_sse_stream(lines, request_start_t=0.0)
    assert result["content"] == "Hello there friend"
    assert result["ttft_ms"] == pytest.approx(100.0)
    assert result["tpot_ms"] == pytest.approx(100.0)
    assert result["prompt_tokens"] == 50
    assert result["completion_tokens"] == 3
    assert result["cached_tokens"] == 10


def test_parse_sse_stream_null_cached_tokens_becomes_zero():
    lines = [
        (0.05, _chunk_line(content="hi")),
        (0.06, _chunk_line(usage={"prompt_tokens": 10, "completion_tokens": 1, "prompt_tokens_details": {"cached_tokens": None}})),
    ]
    result = parse_sse_stream(lines, request_start_t=0.0)
    assert result["cached_tokens"] == 0


def test_parse_sse_stream_missing_usage_details_becomes_zero():
    lines = [(0.05, _chunk_line(content="hi")), (0.06, _chunk_line(usage={"prompt_tokens": 10, "completion_tokens": 1}))]
    result = parse_sse_stream(lines, request_start_t=0.0)
    assert result["cached_tokens"] == 0


def test_parse_sse_stream_ignores_non_data_lines_and_done_sentinel():
    lines = [
        (0.01, ": keep-alive comment"),
        (0.1, _chunk_line(content="ok")),
        (0.2, "data: [DONE]"),
    ]
    result = parse_sse_stream(lines, request_start_t=0.0)
    assert result["content"] == "ok"
    assert result["ttft_ms"] == pytest.approx(100.0)


def test_parse_sse_stream_no_content_chunks_has_none_timings():
    lines = [(0.1, "data: [DONE]")]
    result = parse_sse_stream(lines, request_start_t=0.0)
    assert result["content"] == ""
    assert result["ttft_ms"] is None
    assert result["tpot_ms"] is None


def test_parse_sse_stream_single_completion_token_avoids_division_by_zero():
    lines = [
        (0.05, _chunk_line(content="one")),
        (0.06, _chunk_line(usage={"prompt_tokens": 5, "completion_tokens": 1})),
    ]
    result = parse_sse_stream(lines, request_start_t=0.0)
    assert result["tpot_ms"] is not None  # max(completion_tokens - 1, 1) guards this


def test_dual_slo_attainment_requires_both_ttft_and_tpot_within_slo():
    results = [
        {"status": 200, "ttft_ms": 100, "tpot_ms": 20, "ttft_slo_ms": 500, "tpot_slo_ms": 50},  # meets both
        {"status": 200, "ttft_ms": 600, "tpot_ms": 20, "ttft_slo_ms": 500, "tpot_slo_ms": 50},  # ttft too slow
        {"status": 200, "ttft_ms": 100, "tpot_ms": 60, "ttft_slo_ms": 500, "tpot_slo_ms": 50},  # tpot too slow
        {"status": 503, "ttft_ms": 100, "tpot_ms": 20, "ttft_slo_ms": 500, "tpot_slo_ms": 50},  # failed
    ]
    assert dual_slo_attainment(results) == 0.25


def test_dual_slo_attainment_missing_slo_fields_never_counts_as_met():
    results = [{"status": 200, "ttft_ms": 10, "tpot_ms": 10}]
    assert dual_slo_attainment(results) == 0.0


def test_dual_slo_attainment_empty_results_is_zero():
    assert dual_slo_attainment([]) == 0.0
