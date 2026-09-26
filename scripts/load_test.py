"""Real, live load test against a running SwiftServe router (or, for an A/B
comparison, directly against a naive round-robin over raw replica URLs).

This hits actual HTTP endpoints -- either your SwiftServe router in front of
real vLLM/Qwen replicas, or the replicas directly -- and reports real
observed latency, cache-hit rate (from the X-SwiftServe-Cache-Hit response
header, when present), and per-replica distribution.

Usage:
    python scripts/load_test.py --router-url http://localhost:8000 \\
        --num-sessions 50 --turns 4 --concurrency 10

Thin wrapper: the actual session-running and workload-generation logic now
lives in scripts/workloads.py (deterministic workload generators) and
scripts/benchmark.py (_stream_chat_turn / run_session -- streaming SSE,
same as `benchmark.py run`), so this script and benchmark.py no longer
carry two copies of the same PROMPTS list and turn-runner. This stays the
simple, single-shot tool; scripts/benchmark.py is the statistically-
rigorous one (bootstrap CIs, permutation tests, multiple workload shapes).
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
import time
import uuid
from pathlib import Path

import httpx

# Running this file directly (`python scripts/load_test.py`, as documented
# above) only puts scripts/ itself on sys.path, not the repo root -- so
# `from scripts.benchmark import ...` below would otherwise only work
# under pytest (which already puts the repo root on sys.path). Inserting
# the repo root explicitly makes both invocations work the same way.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.benchmark import (  # noqa: E402
    format_percentile,
    run_session,
)


async def main_async(args: argparse.Namespace) -> None:
    results: list[dict] = []
    semaphore = asyncio.Semaphore(args.concurrency)

    async def bounded_session(session_id: str) -> None:
        async with semaphore, httpx.AsyncClient() as client:
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

    print(f"Requests: {len(ok)} ok, {len(errors)} errored | wall clock: {wall_s:.1f}s")
    print(f"Cache-hit rate (of requests that report it): {len(cache_hits)}/{len(ok)} = {len(cache_hits) / len(ok):.1%}")
    print(f"SLA violation rate: {len(sla_violations)}/{len(ok)} = {len(sla_violations) / len(ok):.1%}")
    print(
        f"Latency: mean={statistics.fmean(latencies):.1f}ms "
        f"p50={format_percentile(latencies, 50)} p90={format_percentile(latencies, 90)} "
        f"p95={format_percentile(latencies, 95)} p99={format_percentile(latencies, 99)}"
    )
    print(f"Requests per replica: {replica_counts}")
    print(
        "(this is scripts/load_test.py -- a single-shot number that scheduling jitter can swing; "
        "for anything you'd defend, use scripts/benchmark.py's multi-seed bootstrap CIs instead)"
    )


def main() -> None:
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
