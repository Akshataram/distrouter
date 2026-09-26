"""Background polling of each vLLM replica's health and Prometheus metrics.

vLLM's OpenAI-compatible server exposes `/health` and a `/metrics` endpoint
in Prometheus text format including (name may vary slightly by vLLM
version):

    vllm:num_requests_running{...}       <float>
    vllm:num_requests_waiting{...}       <float>
    vllm:gpu_cache_usage_perc{...}       <float>
    vllm:prefix_cache_hits{...}          <float>  (counter; some vLLM
    vllm:prefix_cache_queries{...}       <float>   versions name these
                                                    with a `_total` suffix)

We do a minimal text-format parse (no `prometheus_client` dependency) since
we only need a handful of gauge/counter families.

prefix_cache_hits/queries are vLLM's own ground truth for whether its
prefix cache actually served a request from cache -- unlike SwiftServe's
own has_warm_cache()/X-SwiftServe-Cache-Hit, which is only ever the
router's *prediction* (it can be wrong: vLLM may have evicted the blocks
under memory pressure since the router last saw this session). Scraping
these two counters and taking their ratio (ReplicaState.true_prefix_hit_rate)
gives a real signal to check the prediction against, per replica.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re

import httpx

from swiftserve.state import ReplicaState

logger = logging.getLogger("swiftserve.metrics_scraper")

_GAUGE_PATTERNS = {
    "running": re.compile(r"^vllm:num_requests_running(?:\{[^}]*\})?\s+([0-9.eE+-]+)", re.MULTILINE),
    "waiting": re.compile(r"^vllm:num_requests_waiting(?:\{[^}]*\})?\s+([0-9.eE+-]+)", re.MULTILINE),
    "gpu_cache": re.compile(r"^vllm:gpu_cache_usage_perc(?:\{[^}]*\})?\s+([0-9.eE+-]+)", re.MULTILINE),
    # `(?:_total)?` handles both spellings across vLLM versions: some ship
    # these as plain gauges (`vllm:prefix_cache_hits`), others as counters
    # with the standard Prometheus `_total` suffix.
    "prefix_cache_hits": re.compile(r"^vllm:prefix_cache_hits(?:_total)?(?:\{[^}]*\})?\s+([0-9.eE+-]+)", re.MULTILINE),
    "prefix_cache_queries": re.compile(r"^vllm:prefix_cache_queries(?:_total)?(?:\{[^}]*\})?\s+([0-9.eE+-]+)", re.MULTILINE),
}


def _parse_metrics_text(text: str) -> dict[str, float]:
    parsed = {}
    for key, pattern in _GAUGE_PATTERNS.items():
        match = pattern.search(text)
        if match:
            parsed[key] = float(match.group(1))
    return parsed


async def scrape_once(client: httpx.AsyncClient, replica: ReplicaState) -> None:
    try:
        health_resp = await client.get(f"{replica.base_url}/health", timeout=3.0)
        replica.metrics.healthy = health_resp.status_code == 200
    except httpx.HTTPError:
        replica.metrics.healthy = False
        return

    try:
        metrics_resp = await client.get(f"{replica.base_url}/metrics", timeout=3.0)
        if metrics_resp.status_code == 200:
            parsed = _parse_metrics_text(metrics_resp.text)
            replica.record_scrape(
                running=int(parsed.get("running", replica.metrics.running)),
                waiting=int(parsed.get("waiting", replica.metrics.waiting)),
                gpu_cache_usage_perc=parsed.get("gpu_cache", replica.metrics.gpu_cache_usage_perc),
                prefix_cache_hits=parsed.get("prefix_cache_hits", replica.metrics.prefix_cache_hits),
                prefix_cache_queries=parsed.get("prefix_cache_queries", replica.metrics.prefix_cache_queries),
            )
    except httpx.HTTPError as exc:
        logger.warning("metrics scrape failed for replica %s: %s", replica.replica_id, exc)


async def scrape_loop(replicas: list[ReplicaState], interval_s: float, stop_event: asyncio.Event) -> None:
    async with httpx.AsyncClient() as client:
        while not stop_event.is_set():
            await asyncio.gather(*(scrape_once(client, r) for r in replicas))
            # asyncio.TimeoutError specifically (not bare TimeoutError): they're
            # only the same class from Python 3.11 on, and this project targets 3.10+.
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop_event.wait(), timeout=interval_s)
