"""Real, live load test against a running SwiftServe router (or, for an A/B
comparison, directly against a naive round-robin over raw replica URLs).

This hits actual HTTP endpoints -- either your SwiftServe router in front of
real vLLM/Qwen replicas, or the replicas directly -- and reports real
observed latency, cache-hit rate (from the X-SwiftServe-Cache-Hit response
header, when present), and per-replica distribution.

Usage:
    python scripts/load_test.py --router-url http://localhost:8000 \\
        --num-sessions 50 --turns 4 --concurrency 10
"""

from __future__ import annotations

import argparse
import asyncio
import random
import statistics
import time
import uuid

import httpx

PROMPTS = [
    "Summarize the plot of a story about a lighthouse keeper.",
    "What are three ways to improve a Python function's performance?",
    "Explain the difference between TCP and UDP in one paragraph.",
    "Give me a recipe idea using chickpeas and spinach.",
    "Write a short haiku about autumn rain.",
]


async def run_session(
    client: httpx.AsyncClient,
    router_url: str,
    model: str,
    session_id: str,
    num_turns: int,
    sla_ms: float,
    max_tokens: int,
    results: list[dict],
):
    messages = []
    for turn in range(num_turns):
        messages.append({"role": "user", "content": random.choice(PROMPTS)})
        start = time.monotonic()
        try:
            resp = await client.post(
                f"{router_url}/v1/chat/completions",
                json={"model": model, "messages": messages, "max_tokens": max_tokens},
                headers={"X-Session-Id": session_id, "X-SLA-Ms": str(sla_ms)},
                timeout=120.0,
            )
            elapsed_ms = (time.monotonic() - start) * 1000.0
            results.append(
                {
                    "session_id": session_id,
                    "turn": turn,
                    "latency_ms": elapsed_ms,
                    "status": resp.status_code,
                    "replica": resp.headers.get("x-swiftserve-replica"),
                    "cache_hit": resp.headers.get("x-swiftserve-cache-hit"),
                    "sla_ms": sla_ms,
                    "sla_violated": elapsed_ms > sla_ms,
                }
            )
            if resp.status_code == 200:
                reply = resp.json()["choices"][0]["message"]["content"]
                messages.append({"role": "assistant", "content": reply})
        except httpx.HTTPError as exc:
            results.append({"session_id": session_id, "turn": turn, "error": str(exc)})


async def main_async(args):
    results: list[dict] = []
    semaphore = asyncio.Semaphore(args.concurrency)

    async def bounded_session(session_id: str):
        async with semaphore:
            async with httpx.AsyncClient() as client:
                await run_session(
                    client, args.router_url, args.model, session_id,
                    args.turns, args.sla_ms, args.max_tokens, results,
                )

    session_ids = [f"loadtest-{uuid.uuid4().hex[:8]}" for _ in range(args.num_sessions)]
    start = time.monotonic()
    await asyncio.gather(*(bounded_session(sid) for sid in session_ids))
    wall_s = time.monotonic() - start

    ok = [r for r in results if "latency_ms" in r]
    errors = [r for r in results if "error" in r]
    if not ok:
        print(f"No successful requests. {len(errors)} errors, first: {errors[:1]}")
        return

    latencies = sorted(r["latency_ms"] for r in ok)
    cache_hits = [r for r in ok if r.get("cache_hit") == "true"]
    sla_violations = [r for r in ok if r["sla_violated"]]
    replica_counts: dict[str, int] = {}
    for r in ok:
        replica_counts[r["replica"]] = replica_counts.get(r["replica"], 0) + 1

    quantiles = statistics.quantiles(latencies, n=100, method="inclusive")
    print(f"Requests: {len(ok)} ok, {len(errors)} errored | wall clock: {wall_s:.1f}s")
    print(f"Cache-hit rate (of requests that report it): {len(cache_hits)}/{len(ok)} = {len(cache_hits) / len(ok):.1%}")
    print(f"SLA violation rate: {len(sla_violations)}/{len(ok)} = {len(sla_violations) / len(ok):.1%}")
    print(f"Latency ms: mean={statistics.fmean(latencies):.1f} p50={quantiles[49]:.1f} p95={quantiles[94]:.1f} p99={quantiles[98]:.1f}")
    print(f"Requests per replica: {replica_counts}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--router-url", default="http://localhost:8000")
    parser.add_argument("--model", default="Qwen/Qwen2.5-7B-Instruct")
    parser.add_argument("--num-sessions", type=int, default=20)
    parser.add_argument("--turns", type=int, default=3)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--sla-ms", type=float, default=3000.0)
    parser.add_argument("--max-tokens", type=int, default=128)
    args = parser.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
