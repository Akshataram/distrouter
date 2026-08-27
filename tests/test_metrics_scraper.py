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
"""


def test_parses_vllm_prometheus_gauges():
    parsed = _parse_metrics_text(SAMPLE)
    assert parsed["running"] == 3.0
    assert parsed["waiting"] == 1.0
    assert parsed["gpu_cache"] == 0.42


def test_missing_gauges_are_simply_absent():
    parsed = _parse_metrics_text("# no relevant metrics here\n")
    assert parsed == {}
