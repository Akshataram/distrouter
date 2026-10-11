from swiftserve.metrics_scraper import _parse_metrics_text

SAMPLE = """
# HELP vllm:num_requests_running Number of requests currently running.
# TYPE vllm:num_requests_running gauge
vllm:num_requests_running{model_name="Qwen/Qwen2.5-7B-Instruct"} 3.0
# HELP vllm:num_requests_waiting Number of requests waiting in queue.
# TYPE vllm:num_requests_waiting gauge
vllm:num_requests_waiting{model_name="Qwen/Qwen2.5-7B-Instruct"} 1.0
# HELP vllm:gpu_cache_usage_perc GPU KV-cache usage.
# TYPE vllm:gpu_cache_usage_perc gauge
vllm:gpu_cache_usage_perc{model_name="Qwen/Qwen2.5-7B-Instruct"} 0.42
# HELP vllm:prefix_cache_hits Prefix cache hits.
# TYPE vllm:prefix_cache_hits counter
vllm:prefix_cache_hits{model_name="Qwen/Qwen2.5-7B-Instruct"} 120.0
# HELP vllm:prefix_cache_queries Prefix cache queries.
# TYPE vllm:prefix_cache_queries counter
vllm:prefix_cache_queries{model_name="Qwen/Qwen2.5-7B-Instruct"} 200.0
"""

# Some vLLM versions ship these two as plain counters, others append the
# standard Prometheus `_total` suffix -- the parser must handle both.
SAMPLE_TOTAL_SUFFIX = """
vllm:prefix_cache_hits_total{model_name="Qwen/Qwen2.5-7B-Instruct"} 55.0
vllm:prefix_cache_queries_total{model_name="Qwen/Qwen2.5-7B-Instruct"} 110.0
"""


def test_parses_vllm_prometheus_gauges():
    parsed = _parse_metrics_text(SAMPLE)
    assert parsed["running"] == 3.0
    assert parsed["waiting"] == 1.0
    assert parsed["gpu_cache"] == 0.42


def test_missing_gauges_are_simply_absent():
    parsed = _parse_metrics_text("# no relevant metrics here\n")
    assert parsed == {}


def test_parses_prefix_cache_hits_and_queries_plain_spelling():
    parsed = _parse_metrics_text(SAMPLE)
    assert parsed["prefix_cache_hits"] == 120.0
    assert parsed["prefix_cache_queries"] == 200.0


def test_parses_prefix_cache_hits_and_queries_total_suffix_spelling():
    parsed = _parse_metrics_text(SAMPLE_TOTAL_SUFFIX)
    assert parsed["prefix_cache_hits"] == 55.0
    assert parsed["prefix_cache_queries"] == 110.0
