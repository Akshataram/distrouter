"""Statistically-honest benchmark: repeated live runs (multiple seeds) per
policy against a real running SwiftServe router, reporting bootstrap
confidence intervals instead of a single point estimate, plus a
permutation test for whether an observed difference between two policies
is distinguishable from noise.

This replaces scripts/load_test.py's single-shot numbers for anything
going in front of a panel: one run of load_test.py can make round-robin
look better or worse than SwiftServe purely from scheduling jitter --
this harness runs several independent trials per policy and reports
whether the *difference* survives repetition.

A single router process serves one policy at a time (SWIFTSERVE_POLICY),
so this is a two-step workflow: run each policy's router separately, then
compare the saved reports.

    # once per policy, against a router already configured with that policy:
    python scripts/benchmark.py run --router-url http://localhost:8000 \\
        --policy-label swiftserve --num-sessions 30 --turns 4 \\
        --concurrency 6 --sla-ms 3000 --seeds 1,2,3,4,5 \\
        --output reports/swiftserve.json

    python scripts/benchmark.py run --router-url http://localhost:8001 \\
        --policy-label round_robin ... --output reports/round_robin.json

    # then, once all reports exist:
    python scripts/benchmark.py compare reports/swiftserve.json reports/round_robin.json

Also implements Goodput@N (DistServe, arXiv:2401.09670): the highest
open-loop request rate a policy sustains while at least N% of requests
both succeed and meet their SLA -- an orthogonal question to "how fast is
a typical request at a fixed load" (what `run`/`compare` answer). See the
`goodput` / `goodput-compare` subcommands below.

Workloads (scripts/workloads.py): by default `run`/`goodput` still send
the original 5 canned prompts (--workload tiny, unchanged behavior). The
other --workload choices (shared_system, long_doc, sharegpt) generate
sessions with real shared-prefix structure across sessions -- multi-tenant
system prompts, long-document Q&A, real multi-turn traces -- so a
cache-affinity-aware policy actually has shared prefixes to exploit, and
this harness can report whether it does (true_cache_ratio) rather than
only end-to-end latency.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import random
import statistics
import sys
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import httpx

# Running this file directly (`python scripts/benchmark.py ...`, as
# documented above) only puts scripts/ itself on sys.path, not the repo
# root -- so `from scripts.workloads import ...` below would otherwise
# only work under pytest (which already puts the repo root on sys.path).
# Inserting the repo root explicitly makes both invocations work the same
# way; this is the one import benchmark.py takes outside its own file
# (workloads.py exists precisely to be shared, unlike PROMPTS below).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.workloads import (  # noqa: E402
    SessionRequests,
    Workload,
    WorkloadArgs,
    build_workload,
    poisson_arrivals,
)

# A percentile estimate is only as good as how many samples actually land
# in its tail: p99 asks "what's typical of the worst 1%", which is
# unanswerable from a few dozen requests -- you'd just be reading off the
# single largest observation and calling it a percentile. Requiring at
# least ~10 samples past the tail (min 20 overall) is a standard rule of
# thumb for a percentile to reflect real distribution shape rather than
# noise from the top 1-2 points. Deliberately duplicated from
# scripts/load_test.py (not imported) for the same standalone-script reason
# as PROMPTS below.
_MIN_TAIL_SAMPLES = 10


def percentile(sorted_values: list[float], p: float) -> float:
    """Linear-interpolation percentile (numpy's default 'linear' method),
    0 <= p <= 100, over an already-sorted list."""
    if not sorted_values:
        raise ValueError("percentile of empty data")
    if len(sorted_values) == 1:
        return sorted_values[0]
    rank = (p / 100) * (len(sorted_values) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(sorted_values) - 1)
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (rank - lo)


def min_samples_for_percentile(p: float) -> int:
    tail_fraction = (100 - p) / 100
    return max(20, math.ceil(_MIN_TAIL_SAMPLES / tail_fraction))


def format_percentile(sorted_values: list[float], p: float) -> str:
    """Human-readable single-run percentile display for scripts/load_test.py
    (a single-shot tool with no bootstrap CI): flags a percentile as
    unreliable inline rather than printing a falsely-precise number when
    there aren't enough samples in its tail (see min_samples_for_percentile)."""
    value = percentile(sorted_values, p)
    min_n = min_samples_for_percentile(p)
    n = len(sorted_values)
    if n < min_n:
        return f"{value:.1f}ms (unreliable: n={n}, want >={min_n})"
    return f"{value:.1f}ms"


def sla_attainment(results: list[dict], sla_ms: float) -> float:
    """Fraction of *offered* requests -- successes and failures alike --
    that both got a 200 and finished within sla_ms. This is the
    SLO-attainment definition DistServe's Goodput is built on: a dropped
    connection or a non-200 response counts as not meeting the SLA (same
    as a slow one), but still belongs in the denominator, since goodput
    describes the system's overall offered load, not just the subset that
    happened to come back successfully."""
    if not results:
        return 0.0
    met = sum(
        1 for r in results
        if "latency_ms" in r and r.get("status") == 200 and r["latency_ms"] <= sla_ms
    )
    return met / len(results)


def dual_slo_attainment(results: list[dict]) -> float:
    """Same spirit as sla_attainment(), but for the two SLOs a real
    streaming client actually cares about separately: TTFT (how long
    until anything starts coming back) and TPOT (how fast tokens keep
    arriving once they start) -- DistServe's own Goodput definition. A
    request only counts as "met" if it succeeded AND both measurements
    exist AND both are within their own SLO. Requests that don't carry
    ttft_slo_ms/tpot_slo_ms at all (e.g. results from a workload that
    never set them) never count as met -- there is no SLO to have met.
    Still divides by *all* offered requests, errors included, for the
    same reason as sla_attainment."""
    if not results:
        return 0.0
    met = sum(
        1 for r in results
        if r.get("status") == 200
        and r.get("ttft_ms") is not None and r.get("tpot_ms") is not None
        and r.get("ttft_slo_ms") is not None and r.get("tpot_slo_ms") is not None
        and r["ttft_ms"] <= r["ttft_slo_ms"] and r["tpot_ms"] <= r["tpot_slo_ms"]
    )
    return met / len(results)


# Deliberately duplicated from scripts/load_test.py (not imported) so this
# stays a self-contained script runnable directly (`python
# scripts/benchmark.py ...`, as documented above): running a .py file
# directly only puts its own directory on sys.path, not the repo root, so
# a `from scripts.load_test import ...` here would break that invocation
# even though it works fine under pytest (which does put the repo root on
# sys.path). load_test.py stays the simple single-shot tool; this is the
# statistically-rigorous one -- small duplication, no fragile import.
PROMPTS = [
    "Summarize the plot of a story about a lighthouse keeper.",
    "What are three ways to improve a Python function's performance?",
    "Explain the difference between TCP and UDP in one paragraph.",
    "Give me a recipe idea using chickpeas and spinach.",
    "Write a short haiku about autumn rain.",
]


def parse_sse_stream(lines: list[tuple[float, str]], request_start_t: float) -> dict:
    """Parses one chat-completion SSE stream into timing + usage metrics.

    ``lines`` is a list of (wall-clock monotonic time the line was
    received, raw SSE line text) pairs -- kept as plain data (not an
    async generator) so this stays a pure, synchronously-testable
    function over a hand-built fake sequence, with the actual network
    read happening in the caller.

    ttft_ms is measured from ``request_start_t`` to the first chunk whose
    delta carries actual content (the "time to first token" a real
    caller experiences, not merely "time to first SSE event" -- some
    servers send an empty role-only opening delta first). tpot_ms is the
    average time between successive content tokens (last content chunk
    time minus first, divided by completion_tokens-1) rather than a
    per-chunk average, since each chunk can carry more or fewer than one
    token. usage fields come from the final ``usage``-bearing chunk (sent
    when the request asked for ``stream_options: {"include_usage": true}``);
    a missing or null ``cached_tokens`` (a server that doesn't report it)
    becomes 0, never a crash."""
    content_parts: list[str] = []
    first_content_t: float | None = None
    last_content_t: float | None = None
    usage: dict = {}

    for t, raw_line in lines:
        line = raw_line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[len("data:"):].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            chunk = json.loads(payload)
        except json.JSONDecodeError:
            continue
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            content = delta.get("content")
            if content:
                content_parts.append(content)
                if first_content_t is None:
                    first_content_t = t
                last_content_t = t
        chunk_usage = chunk.get("usage")
        if chunk_usage:
            usage = chunk_usage

    prompt_tokens = usage.get("prompt_tokens") or 0
    completion_tokens = usage.get("completion_tokens") or 0
    cached_tokens = (usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0

    ttft_ms = (first_content_t - request_start_t) * 1000.0 if first_content_t is not None else None
    tpot_ms = None
    if first_content_t is not None and last_content_t is not None and completion_tokens:
        tpot_ms = (last_content_t - first_content_t) * 1000.0 / max(completion_tokens - 1, 1)

    return {
        "content": "".join(content_parts),
        "ttft_ms": ttft_ms,
        "tpot_ms": tpot_ms,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "cached_tokens": cached_tokens,
    }


async def _stream_chat_turn(
    client: httpx.AsyncClient, router_url: str, model: str, session_id: str,
    transcript: list[dict], max_tokens: int,
) -> dict:
    """Sends one turn's full ``transcript`` as a streaming chat-completion
    request (``stream: true`` + ``stream_options.include_usage``) and
    returns the parsed low-level result: status/replica/cache-hit
    headers, parse_sse_stream's timing+usage fields, and the assistant's
    full replied content (for the caller to append to its own transcript
    before the next turn). On a transport-level failure, returns just
    ``{"error": str(exc)}`` -- never partially-filled timing fields that
    could be mistaken for a real (if degenerate) measurement."""
    request_start = time.monotonic()
    lines: list[tuple[float, str]] = []
    try:
        async with client.stream(
            "POST",
            f"{router_url}/v1/chat/completions",
            json={
                "model": model,
                "messages": transcript,
                "max_tokens": max_tokens,
                "stream": True,
                "stream_options": {"include_usage": True},
            },
            headers={"X-Session-Id": session_id},
            timeout=120.0,
        ) as response:
            status_code = response.status_code
            replica = response.headers.get("x-swiftserve-replica")
            cache_hit = response.headers.get("x-swiftserve-cache-hit")
            predicted_cached_tokens = int(response.headers.get("x-swiftserve-predicted-cached-tokens") or 0)
            async for raw_line in response.aiter_lines():
                lines.append((time.monotonic(), raw_line))
    except httpx.HTTPError as exc:
        return {"error": str(exc)}

    e2e_ms = (time.monotonic() - request_start) * 1000.0
    parsed = parse_sse_stream(lines, request_start)
    return {
        "error": None,
        "status": status_code,
        "replica": replica,
        "cache_hit": cache_hit,
        "predicted_cached_tokens": predicted_cached_tokens,
        "e2e_ms": e2e_ms,
        "ttft_ms": parsed["ttft_ms"],
        "tpot_ms": parsed["tpot_ms"],
        "prompt_tokens": parsed["prompt_tokens"],
        "completion_tokens": parsed["completion_tokens"],
        "cached_tokens": parsed["cached_tokens"],
        "content": parsed["content"],
    }


async def run_session(
    client: httpx.AsyncClient,
    router_url: str,
    model: str,
    session_id: str,
    num_turns: int,
    sla_ms: float,
    max_tokens: int,
    results: list[dict],
) -> None:
    """Legacy canned-prompt session runner (one of PROMPTS chosen at
    random per turn, uniform SLA), preserved for run_open_loop_arrivals's
    default (--workload-less) path and for run_goodput_sweep's default
    behavior -- streams over SSE now (see _stream_chat_turn) but keeps
    exactly the result fields this module's own tests
    (test_benchmark_goodput.py) already assert on: latency_ms, status,
    sla_violated, plus the new streaming fields riding alongside them."""
    transcript: list[dict] = []
    for turn in range(num_turns):
        transcript.append({"role": "user", "content": random.choice(PROMPTS)})
        turn_result = await _stream_chat_turn(client, router_url, model, session_id, transcript, max_tokens)
        if turn_result.get("error") is not None:
            results.append({"session_id": session_id, "turn": turn, "error": turn_result["error"]})
            continue

        elapsed_ms = turn_result["e2e_ms"]
        results.append(
            {
                "session_id": session_id,
                "turn": turn,
                "latency_ms": elapsed_ms,
                "status": turn_result["status"],
                "replica": turn_result["replica"],
                "cache_hit": turn_result["cache_hit"],
                "sla_ms": sla_ms,
                # A non-200 (e.g. a 503 admission rejection) is never a
                # met SLA just because it came back fast -- SLA
                # attainment means the request actually succeeded AND
                # was fast enough, not merely "some response arrived".
                "sla_violated": turn_result["status"] != 200 or elapsed_ms > sla_ms,
                "ttft_ms": turn_result["ttft_ms"],
                "tpot_ms": turn_result["tpot_ms"],
                "predicted_cached_tokens": turn_result["predicted_cached_tokens"],
                "prompt_tokens": turn_result["prompt_tokens"],
                "completion_tokens": turn_result["completion_tokens"],
                "cached_tokens": turn_result["cached_tokens"],
            }
        )
        if turn_result["status"] == 200 and turn_result["content"]:
            transcript.append({"role": "assistant", "content": turn_result["content"]})


async def run_session_from_requests(
    client: httpx.AsyncClient,
    router_url: str,
    model: str,
    session_requests: SessionRequests,
    results: list[dict],
) -> None:
    """Workload-driven session runner: sends session_requests.turns in
    the message-append semantics documented in scripts/workloads.py (turn
    0 is the full seed messages, turn>0 is a single new user message),
    appending each turn's real streamed assistant reply before sending
    the next turn -- never a scripted one -- so the bytes actually sent
    to the router match what vLLM's own prefix cache would have stored."""
    transcript: list[dict] = []
    for turn_idx, turn_messages in enumerate(session_requests.turns):
        transcript = list(turn_messages) if turn_idx == 0 else transcript + [turn_messages[0]]

        turn_result = await _stream_chat_turn(
            client, router_url, model, session_requests.session_id, transcript, session_requests.max_tokens
        )
        if turn_result.get("error") is not None:
            results.append({"session_id": session_requests.session_id, "turn": turn_idx, "error": turn_result["error"]})
            continue

        results.append(
            {
                "session_id": session_requests.session_id,
                "turn": turn_idx,
                "ttft_ms": turn_result["ttft_ms"],
                "tpot_ms": turn_result["tpot_ms"],
                "e2e_ms": turn_result["e2e_ms"],
                "status": turn_result["status"],
                "replica": turn_result["replica"],
                "cache_hit": turn_result["cache_hit"],
                "predicted_cached_tokens": turn_result["predicted_cached_tokens"],
                "prompt_tokens": turn_result["prompt_tokens"],
                "completion_tokens": turn_result["completion_tokens"],
                "cached_tokens": turn_result["cached_tokens"],
                "ttft_slo_ms": session_requests.ttft_slo_ms,
                "tpot_slo_ms": session_requests.tpot_slo_ms,
            }
        )
        if turn_result["status"] == 200 and turn_result["content"]:
            transcript.append({"role": "assistant", "content": turn_result["content"]})


def bootstrap_ci(
    values: list[float], stat_fn: Callable[[list[float]], float] = statistics.fmean,
    n_resamples: int = 2000, ci: float = 0.95, seed: int = 0,
) -> tuple[float, float, float]:
    """Basic percentile bootstrap. Returns (point_estimate, ci_low, ci_high)."""
    point = stat_fn(values)
    if len(values) < 2:
        return point, point, point
    rng = random.Random(seed)
    n = len(values)
    resample_stats = [stat_fn([values[rng.randrange(n)] for _ in range(n)]) for _ in range(n_resamples)]
    resample_stats.sort()
    alpha = (1 - ci) / 2
    lo_idx = int(alpha * n_resamples)
    hi_idx = min(int((1 - alpha) * n_resamples), n_resamples - 1)
    return point, resample_stats[lo_idx], resample_stats[hi_idx]


def permutation_test_diff_means(
    a: list[float], b: list[float], n_permutations: int = 5000, seed: int = 0
) -> tuple[float, float]:
    """Two-sided permutation test for a difference in means between two
    independent samples. Returns (observed_diff, p_value). Avoids needing
    scipy: shuffle the pooled values, re-split into groups of the original
    sizes, and see how often a random split produces a difference at least
    as extreme as the one actually observed."""
    observed = statistics.fmean(a) - statistics.fmean(b)
    if len(a) < 2 or len(b) < 2:
        return observed, float("nan")
    rng = random.Random(seed)
    pooled = a + b
    n_a = len(a)
    count_extreme = 0
    for _ in range(n_permutations):
        rng.shuffle(pooled)
        diff = statistics.fmean(pooled[:n_a]) - statistics.fmean(pooled[n_a:])
        if abs(diff) >= abs(observed):
            count_extreme += 1
    # +1 / +1 smoothing: a p-value of exactly 0 is never justified by a
    # finite number of permutations.
    p_value = (count_extreme + 1) / (n_permutations + 1)
    return observed, p_value


def _build_trial(seed: int, results: list[dict], sla_ms: float) -> dict:
    """Aggregates one trial's raw per-request results (from run_session or
    run_session_from_requests) into the per-trial summary dict
    summarize_policy pools across trials. ``dual_slo_attainment``,
    ``true_cache_ratio`` and ``prediction_error`` are None (not 0.0) when
    this trial's results never carried the fields they need -- e.g. the
    legacy canned-prompt path doesn't set ttft_slo_ms/tpot_slo_ms, so
    there's no SLO here to report attainment against, and that's a
    different fact than "0% attainment"."""
    ok = [r for r in results if "e2e_ms" in r or "latency_ms" in r]
    if not ok:
        return {
            "seed": seed, "ok": 0, "errors": len(results), "latencies": [],
            "cache_hit_rate": None, "sla_violation_rate": None,
            "ttft_ms": [], "tpot_ms": [], "dual_slo_attainment": None,
            "true_cache_ratio": None, "prediction_error": None, "replica_counts": {},
        }

    latencies = [r.get("e2e_ms", r.get("latency_ms")) for r in ok]
    cache_hits = [r for r in ok if r.get("cache_hit") == "true"]
    sla_violations = [
        r for r in ok if r.get("status") != 200 or r.get("e2e_ms", r.get("latency_ms", 0.0)) > sla_ms
    ]
    ttft_values = [r["ttft_ms"] for r in ok if r.get("ttft_ms") is not None]
    tpot_values = [r["tpot_ms"] for r in ok if r.get("tpot_ms") is not None]

    replica_counts: dict[str, int] = {}
    for r in ok:
        replica = r.get("replica")
        if replica:
            replica_counts[replica] = replica_counts.get(replica, 0) + 1

    total_prompt_tokens = sum(r.get("prompt_tokens") or 0 for r in ok)
    total_cached_tokens = sum(r.get("cached_tokens") or 0 for r in ok)
    true_cache_ratio = (total_cached_tokens / total_prompt_tokens) if total_prompt_tokens > 0 else None

    prediction_errors = [
        abs((r.get("predicted_cached_tokens") or 0) - (r.get("cached_tokens") or 0)) / r["prompt_tokens"]
        for r in ok if (r.get("prompt_tokens") or 0) > 0
    ]
    prediction_error = statistics.fmean(prediction_errors) if prediction_errors else None

    has_dual_slo = any(r.get("ttft_slo_ms") is not None for r in results)
    dual_slo_rate = dual_slo_attainment(results) if has_dual_slo else None

    return {
        "seed": seed,
        "ok": len(ok),
        "errors": len(results) - len(ok),
        "latencies": latencies,
        "cache_hit_rate": len(cache_hits) / len(ok),
        "sla_violation_rate": len(sla_violations) / len(ok),
        "ttft_ms": ttft_values,
        "tpot_ms": tpot_values,
        "dual_slo_attainment": dual_slo_rate,
        "true_cache_ratio": true_cache_ratio,
        "prediction_error": prediction_error,
        "replica_counts": replica_counts,
    }


async def _run_closed_loop_workload(
    router_url: str, model: str, workload: Workload, concurrency: int,
) -> list[dict]:
    """Closed-loop: every session in workload.sessions is one concurrent
    worker, bounded by ``concurrency`` -- the direct workload-driven
    replacement for the old PROMPTS-based run_one_trial/run_policy_trials
    (which are no longer needed now that every --workload choice,
    including the default "tiny", goes through this same path)."""
    results: list[dict] = []
    semaphore = asyncio.Semaphore(concurrency)

    async def bounded_session(session_requests: SessionRequests) -> None:
        async with semaphore, httpx.AsyncClient() as client:
            await run_session_from_requests(client, router_url, model, session_requests, results)

    await asyncio.gather(*(bounded_session(sr) for sr in workload.sessions))
    return results


async def run_open_loop_workload_window(
    router_url: str, model: str, workload: Workload, target_rps: float, seed: int, max_drain_s: float = 60.0,
) -> list[dict]:
    """Open-loop counterpart to _run_closed_loop_workload for `benchmark.py
    run --rate`: every session in workload.sessions arrives exactly once,
    Poisson-spaced at target_rps (poisson_arrivals) rather than at a fixed
    concurrency -- one workload IS one arrival window here, distinct from
    run_open_loop_arrivals's goodput-sweep use below, which spawns new
    sessions continuously for a fixed duration instead of a fixed count."""
    if target_rps <= 0:
        raise ValueError("target_rps must be > 0")
    results: list[dict] = []
    arrivals = poisson_arrivals(target_rps, len(workload.sessions), seed)
    limits = httpx.Limits(max_connections=max(50, int(target_rps * 4)), max_keepalive_connections=50)
    async with httpx.AsyncClient(limits=limits) as client:
        start = time.monotonic()
        tasks: list[asyncio.Task] = []
        for session_requests, arrival_t in zip(workload.sessions, arrivals, strict=True):
            wait_s = arrival_t - (time.monotonic() - start)
            if wait_s > 0:
                await asyncio.sleep(wait_s)
            tasks.append(
                asyncio.create_task(run_session_from_requests(client, router_url, model, session_requests, results))
            )
        if tasks:
            _done, pending = await asyncio.wait(tasks, timeout=max_drain_s)
            for t in pending:
                t.cancel()
    return results


def _print_trial_line(policy_label: str, trial: dict) -> None:
    if not trial["latencies"]:
        print(f"  [{policy_label}] seed={trial['seed']}: no successful requests ({trial['errors']} errors)")
        return
    extra = ""
    if trial.get("dual_slo_attainment") is not None:
        extra = f", goodput={trial['dual_slo_attainment']:.1%}"
    print(
        f"  [{policy_label}] seed={trial['seed']}: {trial['ok']} ok, {trial['errors']} errors, "
        f"mean={statistics.fmean(trial['latencies']):.1f}ms, cache_hit={trial['cache_hit_rate']:.1%}{extra}"
    )


# -- Goodput: open-loop load sweep (DistServe-style) -------------------------
#
# _run_closed_loop_workload above is *closed-loop*: a fixed number of
# concurrent session workers, each starting its next turn only once the
# previous one returns. Under overload, that quietly self-throttles the
# offered rate to match whatever the system can keep up with -- exactly
# the wrong tool for asking "how much load can this system sustain", since
# the answer would just be "however much you configured --concurrency to
# allow". An open-loop generator issues new session arrivals on a clock of
# their own (a Poisson process at a target rate), independent of how fast
# earlier ones finish, so a system that can't keep up actually shows it
# (queuing, timeouts, SLA violations) instead of hiding it.


async def run_open_loop_arrivals(
    router_url: str, model: str, target_rps: float, duration_s: float,
    turns: int, sla_ms: float, max_tokens: int, seed: int,
    max_drain_s: float = 60.0, workload: Workload | None = None,
) -> list[dict]:
    """New sessions arrive as a Poisson process at target_rps for
    duration_s seconds, then waits up to max_drain_s for whatever's still
    in flight before giving up on stragglers. One shared, connection-
    pooled client: at realistic RPS many sessions overlap, and a fresh
    TCP/TLS handshake per session would itself skew the very latencies
    this is trying to measure.

    With ``workload=None`` (the default -- unchanged behavior), each
    arrival runs the legacy canned-prompt run_session with its own fresh
    session id. With a workload given, each arrival instead cycles through
    workload.sessions (via run_session_from_requests, in order, wrapping
    around) so a goodput sweep over shared_system/long_doc/sharegpt
    actually exercises those workloads' shared-prefix structure -- each
    cycle through the list gets a distinct session id suffix so repeated
    passes over the same finite list don't collide on cache-affinity
    state keyed by session id."""
    if target_rps <= 0:
        raise ValueError("target_rps must be > 0")
    rng = random.Random(seed)
    results: list[dict] = []
    tasks: list[asyncio.Task] = []
    limits = httpx.Limits(max_connections=max(50, int(target_rps * 4)), max_keepalive_connections=50)
    async with httpx.AsyncClient(limits=limits) as client:
        deadline = time.monotonic() + duration_s
        session_count = 0
        while time.monotonic() < deadline:
            session_count += 1
            if workload is not None and workload.sessions:
                base = workload.sessions[(session_count - 1) % len(workload.sessions)]
                session_requests = replace(base, session_id=f"{base.session_id}-arrival{session_count}")
                tasks.append(
                    asyncio.create_task(
                        run_session_from_requests(client, router_url, model, session_requests, results)
                    )
                )
            else:
                session_id = f"goodput-{seed}-{session_count}"
                tasks.append(
                    asyncio.create_task(
                        run_session(client, router_url, model, session_id, turns, sla_ms, max_tokens, results)
                    )
                )
            await asyncio.sleep(rng.expovariate(target_rps))
        if tasks:
            _done, pending = await asyncio.wait(tasks, timeout=max_drain_s)
            for t in pending:
                t.cancel()
    return results


async def run_goodput_sweep(
    router_url: str, policy_label: str, model: str, rps_levels: list[float],
    duration_s: float, turns: int, sla_ms: float, max_tokens: int, seed: int,
    workload: Workload | None = None,
) -> dict:
    """Runs run_open_loop_arrivals once per RPS in rps_levels (in the
    order given -- ascending is the natural choice but not enforced) and
    reports per-level offered/completed counts, SLA attainment, and
    latency. Does not itself decide "the" goodput number -- see
    find_goodput, kept separate so a saved report can be re-evaluated
    against a different --sla-target without re-running live traffic."""
    levels = []
    for rps in rps_levels:
        results = await run_open_loop_arrivals(
            router_url, model, rps, duration_s, turns, sla_ms, max_tokens, seed, workload=workload
        )
        attainment = sla_attainment(results, sla_ms)
        ok = [r for r in results if "latency_ms" in r or "e2e_ms" in r]
        latencies = sorted(r.get("latency_ms", r.get("e2e_ms")) for r in ok)
        mean_ms = statistics.fmean(latencies) if latencies else None
        p95_ms = percentile(latencies, 95) if len(latencies) >= 2 else None
        print(
            f"  [{policy_label}] rps={rps:g}: {len(ok)}/{len(results)} completed, attainment={attainment:.1%}"
            + (f", mean={mean_ms:.0f}ms p95={p95_ms:.0f}ms" if latencies else ", no completions")
        )
        levels.append(
            {
                "target_rps": rps,
                "offered_requests": len(results),
                "completed_requests": len(ok),
                "attainment": attainment,
                "mean_latency_ms": mean_ms,
                "p95_latency_ms": p95_ms,
            }
        )
    return {"policy": policy_label, "sla_ms": sla_ms, "levels": levels}


def find_goodput(levels: list[dict], sla_target: float = 0.9) -> dict:
    """The highest *tested* RPS whose measured attainment >= sla_target --
    never an interpolated or extrapolated guess between tested points.
    goodput_rps is None if no tested level cleared the target, including
    when even the lowest one didn't (report that honestly rather than
    picking the "least-bad" level and calling it goodput)."""
    passing = [lvl["target_rps"] for lvl in levels if lvl["attainment"] >= sla_target]
    return {"sla_target": sla_target, "goodput_rps": max(passing) if passing else None}


def print_goodput_report(report: dict) -> None:
    print(f"\n{report['policy']}: SLA={report['sla_ms']:.0f}ms, target attainment >= {report['sla_target']:.0%}")
    print(f"{'target_rps':>12}{'offered':>10}{'completed':>11}{'attainment':>12}{'mean_ms':>10}{'p95_ms':>10}")
    for lvl in report["levels"]:
        mean_str = f"{lvl['mean_latency_ms']:.0f}" if lvl["mean_latency_ms"] is not None else "n/a"
        p95_str = f"{lvl['p95_latency_ms']:.0f}" if lvl["p95_latency_ms"] is not None else "n/a"
        print(
            f"{lvl['target_rps']:>12g}{lvl['offered_requests']:>10}{lvl['completed_requests']:>11}"
            f"{lvl['attainment']:>12.1%}{mean_str:>10}{p95_str:>10}"
        )
    if report["goodput_rps"] is not None:
        print(f"-> Goodput@{report['sla_target']:.0%} = {report['goodput_rps']:g} req/s")
    else:
        print(
            f"-> Goodput@{report['sla_target']:.0%}: none of the tested RPS levels met the target "
            "-- try lower --rps-levels, or the system is already over capacity at your lowest one"
        )


def print_goodput_comparison(reports: list[dict]) -> None:
    print(f"{'policy':<18}{'sla_ms':>8}{'target':>8}{'goodput_rps':>14}")
    for r in reports:
        g = f"{r['goodput_rps']:g}" if r["goodput_rps"] is not None else "none"
        print(f"{r['policy']:<18}{r['sla_ms']:>8.0f}{r['sla_target']:>8.0%}{g:>14}")


def summarize_percentile(all_latencies: list[float], p: float) -> dict:
    """Point estimate plus a bootstrap 95% CI, computed over the pooled
    per-request latencies (not per-trial means -- "p95 latency" is a
    statement about individual requests). ``reliable`` is False when there
    simply aren't enough samples in this percentile's tail for the number
    to mean anything yet -- callers should show that, not hide it behind a
    falsely-precise decimal."""
    min_n = min_samples_for_percentile(p)
    if len(all_latencies) < 2:
        return {"point": None, "ci95_low": None, "ci95_high": None, "n": len(all_latencies), "min_recommended_n": min_n, "reliable": False}
    point, lo, hi = bootstrap_ci(all_latencies, stat_fn=lambda xs: percentile(sorted(xs), p))
    return {
        "point": point,
        "ci95_low": lo,
        "ci95_high": hi,
        "n": len(all_latencies),
        "min_recommended_n": min_n,
        "reliable": len(all_latencies) >= min_n,
    }


def summarize_policy(policy_label: str, trials: list[dict]) -> dict:
    all_latencies = [latency for t in trials for latency in t["latencies"]]
    per_trial_means = [statistics.fmean(t["latencies"]) for t in trials if t["latencies"]]
    cache_hit_rates = [t["cache_hit_rate"] for t in trials if t["latencies"]]
    sla_violation_rates = [t["sla_violation_rate"] for t in trials if t["latencies"]]

    # New (additive) fields below all use .get(...) with an empty/None
    # fallback: trials built by _build_trial always carry these keys, but
    # a hand-built trial dict from an older report or a test (see
    # tests/test_benchmark_stats.py) may not, and summarize_policy must
    # keep working over those exactly as before rather than KeyError.
    all_ttft = [v for t in trials for v in t.get("ttft_ms", [])]
    all_tpot = [v for t in trials for v in t.get("tpot_ms", [])]
    per_trial_ttft_means = [statistics.fmean(t["ttft_ms"]) for t in trials if t.get("ttft_ms")]
    dual_slo_rates = [t["dual_slo_attainment"] for t in trials if t.get("dual_slo_attainment") is not None]
    true_cache_ratios = [t["true_cache_ratio"] for t in trials if t.get("true_cache_ratio") is not None]
    prediction_errors = [t["prediction_error"] for t in trials if t.get("prediction_error") is not None]

    pooled_replica_counts: dict[str, int] = {}
    for t in trials:
        for replica, count in t.get("replica_counts", {}).items():
            pooled_replica_counts[replica] = pooled_replica_counts.get(replica, 0) + count
    load_imbalance = None
    if pooled_replica_counts:
        counts = list(pooled_replica_counts.values())
        mean_count = statistics.fmean(counts)
        load_imbalance = (max(counts) / mean_count) if mean_count > 0 else None

    def ci_or_nan(values):
        return bootstrap_ci(values) if values else (float("nan"), float("nan"), float("nan"))

    mean_point, mean_lo, mean_hi = ci_or_nan(per_trial_means)
    cache_point, cache_lo, cache_hi = ci_or_nan(cache_hit_rates)
    sla_point, sla_lo, sla_hi = ci_or_nan(sla_violation_rates)
    goodput_point, goodput_lo, goodput_hi = ci_or_nan(dual_slo_rates)
    true_cache_point, true_cache_lo, true_cache_hi = ci_or_nan(true_cache_ratios)
    pred_err_point, pred_err_lo, pred_err_hi = ci_or_nan(prediction_errors)

    return {
        "policy": policy_label,
        "num_trials": len(trials),
        "total_requests": sum(t["ok"] for t in trials),
        "total_errors": sum(t["errors"] for t in trials),
        "mean_latency_ms": {"point": mean_point, "ci95_low": mean_lo, "ci95_high": mean_hi},
        # String keys, not int: this dict round-trips through JSON (saved by
        # `run`, reloaded by `compare`), and json.dump silently stringifies
        # int dict keys on the way out -- using strings from the start keeps
        # a freshly-computed report and one reloaded from disk identical.
        "latency_percentiles_ms": {f"p{p}": summarize_percentile(all_latencies, p) for p in (50, 90, 95, 99)},
        "ttft_percentiles_ms": {f"p{p}": summarize_percentile(all_ttft, p) for p in (50, 90, 99)},
        "tpot_percentiles_ms": {f"p{p}": summarize_percentile(all_tpot, p) for p in (50, 99)},
        "cache_hit_rate": {"point": cache_point, "ci95_low": cache_lo, "ci95_high": cache_hi},
        "sla_violation_rate": {"point": sla_point, "ci95_low": sla_lo, "ci95_high": sla_hi},
        "goodput_rate": {"point": goodput_point, "ci95_low": goodput_lo, "ci95_high": goodput_hi},
        "true_cache_ratio": {"point": true_cache_point, "ci95_low": true_cache_lo, "ci95_high": true_cache_hi},
        "prediction_error": {"point": pred_err_point, "ci95_low": pred_err_lo, "ci95_high": pred_err_hi},
        "load_imbalance": load_imbalance,
        "per_trial_means_ms": per_trial_means,
        "per_trial_ttft_means_ms": per_trial_ttft_means,
        "per_trial_goodput_rates": dual_slo_rates,
        "all_latencies_ms": all_latencies,
    }


def _fmt_percentile_cell(pct: dict) -> str:
    if pct["point"] is None:
        return "n/a"
    flag = "" if pct["reliable"] else "*"
    return f"{pct['point']:.0f}{flag}"


def _fmt_ci_metric(metric: dict, fmt: str = "{:.1%}") -> str:
    if metric["point"] != metric["point"]:  # NaN check without importing math again here
        return "n/a"
    return fmt.format(metric["point"])


def _print_pairwise_permutation(reports: list[dict], key: str, label: str) -> None:
    """Shared pairwise permutation-test printer, generalized from what was
    originally a single copy-pasted block for e2e latency only -- used for
    e2e latency, TTFT, and goodput/dual-SLO attainment rate alike."""
    print(f"\nPairwise significance ({label}, permutation test, two-sided, alpha=0.05):")
    for i in range(len(reports)):
        for j in range(i + 1, len(reports)):
            a, b = reports[i], reports[j]
            va, vb = a.get(key, []), b.get(key, [])
            if len(va) < 2 or len(vb) < 2:
                print(f"  {a['policy']} vs {b['policy']}: need >=2 trials each with this metric, skipping")
                continue
            diff, p = permutation_test_diff_means(va, vb)
            verdict = "significant" if p < 0.05 else "NOT significant"
            print(f"  {a['policy']} vs {b['policy']}: diff={diff:+.4f}, p={p:.4f} -> {verdict}")


def print_comparison_table(reports: list[dict]) -> None:
    print(
        f"{'policy':<18}{'n':>6}{'mean_ms':>10}{'95% CI (ms)':>20}"
        f"{'p50':>7}{'p90':>7}{'p95':>7}{'p99':>7}{'cache_hit':>11}{'sla_viol':>10}"
    )
    any_unreliable = False
    for r in reports:
        ci = r["mean_latency_ms"]
        ci_str = f"[{ci['ci95_low']:.0f}, {ci['ci95_high']:.0f}]"
        pcts = r["latency_percentiles_ms"]
        any_unreliable = any_unreliable or any(not pcts[k]["reliable"] for k in ("p50", "p90", "p95", "p99"))
        print(
            f"{r['policy']:<18}{r['total_requests']:>6}{ci['point']:>10.1f}{ci_str:>20}"
            f"{_fmt_percentile_cell(pcts['p50']):>7}{_fmt_percentile_cell(pcts['p90']):>7}"
            f"{_fmt_percentile_cell(pcts['p95']):>7}{_fmt_percentile_cell(pcts['p99']):>7}"
            f"{r['cache_hit_rate']['point']:>10.1%}{r['sla_violation_rate']['point']:>10.1%}"
        )
    if any_unreliable:
        print(
            "* = fewer samples than recommended for a reliable estimate at that percentile "
            "(see the report JSON's latency_percentiles_ms.<p>.n vs .min_recommended_n -- "
            "run more trials/sessions, or trust the mean's 95% CI instead)"
        )

    # Prefix-aware routing metrics -- only meaningful for workloads with
    # real shared-prefix structure (--workload shared_system/long_doc),
    # but always printed (as n/a where absent) since a report's JSON
    # always carries these keys once produced by this version.
    if any(r.get("ttft_percentiles_ms", {}).get("p50", {}).get("point") is not None for r in reports):
        print(f"\n{'policy':<18}{'ttft_p50':>10}{'ttft_p90':>10}{'ttft_p99':>10}{'tpot_p50':>10}{'tpot_p99':>10}"
              f"{'goodput':>10}{'true_cache':>12}{'pred_err':>10}{'load_imb':>10}")
        for r in reports:
            ttft = r.get("ttft_percentiles_ms", {})
            tpot = r.get("tpot_percentiles_ms", {})
            goodput = r.get("goodput_rate", {"point": float("nan")})
            true_cache = r.get("true_cache_ratio", {"point": float("nan")})
            pred_err = r.get("prediction_error", {"point": float("nan")})
            load_imb = r.get("load_imbalance")
            load_imb_str = f"{load_imb:.2f}" if load_imb is not None else "n/a"
            print(
                f"{r['policy']:<18}"
                f"{_fmt_percentile_cell(ttft.get('p50', {'point': None, 'reliable': True})):>10}"
                f"{_fmt_percentile_cell(ttft.get('p90', {'point': None, 'reliable': True})):>10}"
                f"{_fmt_percentile_cell(ttft.get('p99', {'point': None, 'reliable': True})):>10}"
                f"{_fmt_percentile_cell(tpot.get('p50', {'point': None, 'reliable': True})):>10}"
                f"{_fmt_percentile_cell(tpot.get('p99', {'point': None, 'reliable': True})):>10}"
                f"{_fmt_ci_metric(goodput):>10}"
                f"{_fmt_ci_metric(true_cache):>12}"
                f"{_fmt_ci_metric(pred_err):>10}"
                f"{load_imb_str:>10}"
            )

    _print_pairwise_permutation(reports, "per_trial_means_ms", "mean e2e latency (ms)")
    _print_pairwise_permutation(reports, "per_trial_ttft_means_ms", "mean TTFT (ms)")
    _print_pairwise_permutation(reports, "per_trial_goodput_rates", "goodput / dual-SLO attainment rate")


def _cmd_run(args: argparse.Namespace) -> None:
    seeds = [int(s) for s in args.seeds.split(",")]
    base_workload_args = WorkloadArgs(
        workload=args.workload,
        num_apps=args.workload_num_apps,
        zipf_s=args.workload_zipf_s,
        system_prompt_tokens=args.workload_system_prompt_tokens,
        num_docs=args.workload_num_docs,
        doc_tokens=args.workload_doc_tokens,
        questions_per_doc=args.workload_questions_per_doc,
        sharegpt_path=args.workload_sharegpt_path,
        sessions=args.num_sessions,
        turns=args.turns,
        max_tokens=args.max_tokens,
        ttft_slo_ms=args.ttft_slo_ms,
        tpot_slo_ms=args.tpot_slo_ms,
        sla_ms=args.sla_ms,
    )

    router_url = args.router_url.rstrip("/")
    trials = []
    for seed in seeds:
        workload = build_workload(replace(base_workload_args, seed=seed))
        if args.rate:
            results = asyncio.run(run_open_loop_workload_window(router_url, args.model, workload, args.rate, seed))
        else:
            results = asyncio.run(_run_closed_loop_workload(router_url, args.model, workload, args.concurrency))
        trial = _build_trial(seed, results, args.sla_ms)
        _print_trial_line(args.policy_label, trial)
        trials.append(trial)

    report = summarize_policy(args.policy_label, trials)
    print()
    print_comparison_table([report])
    with open(args.output, "w") as f:
        json.dump(report, f, indent=2)
    print(f"\nReport written to {args.output}")


def _cmd_compare(args: argparse.Namespace) -> None:
    reports = []
    for path in args.reports:
        with open(path) as f:
            reports.append(json.load(f))
    print_comparison_table(reports)


def _cmd_goodput(args: argparse.Namespace) -> None:
    rps_levels = [float(x) for x in args.rps_levels.split(",")]
    if any(rps <= 0 for rps in rps_levels):
        raise SystemExit("--rps-levels must all be positive")

    workload = None
    if args.workload != "tiny":
        workload = build_workload(
            WorkloadArgs(
                workload=args.workload,
                num_apps=args.workload_num_apps,
                zipf_s=args.workload_zipf_s,
                system_prompt_tokens=args.workload_system_prompt_tokens,
                num_docs=args.workload_num_docs,
                doc_tokens=args.workload_doc_tokens,
                questions_per_doc=args.workload_questions_per_doc,
                sharegpt_path=args.workload_sharegpt_path,
                turns=args.turns,
                seed=args.seed,
                max_tokens=args.max_tokens,
                sla_ms=args.sla_ms,
            )
        )

    report = asyncio.run(
        run_goodput_sweep(
            router_url=args.router_url.rstrip("/"),
            policy_label=args.policy_label,
            model=args.model,
            rps_levels=rps_levels,
            duration_s=args.duration_s,
            turns=args.turns,
            sla_ms=args.sla_ms,
            max_tokens=args.max_tokens,
            seed=args.seed,
            workload=workload,
        )
    )
    report.update(find_goodput(report["levels"], args.sla_target))
    print_goodput_report(report)
    with open(args.output, "w") as f:
        json.dump(report, f, indent=2)
    print(f"\nReport written to {args.output}")


def _cmd_goodput_compare(args: argparse.Namespace) -> None:
    reports = []
    for path in args.reports:
        with open(path) as f:
            reports.append(json.load(f))
    print_goodput_comparison(reports)


def _add_workload_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--workload", choices=["shared_system", "long_doc", "sharegpt", "tiny"], default="tiny",
        help="workload shape to generate (default 'tiny' = original 5-canned-prompt behavior, unchanged)",
    )
    parser.add_argument("--workload-num-apps", type=int, default=6, help="shared_system: number of distinct system prompts")
    parser.add_argument("--workload-zipf-s", type=float, default=1.1, help="shared_system: Zipf skew exponent for app assignment")
    parser.add_argument("--workload-system-prompt-tokens", type=int, default=2000, help="shared_system: tokens per system prompt")
    parser.add_argument("--workload-num-docs", type=int, default=5, help="long_doc: number of distinct documents")
    parser.add_argument("--workload-doc-tokens", type=int, default=3000, help="long_doc: tokens per document")
    parser.add_argument("--workload-questions-per-doc", type=int, default=6, help="long_doc: questions asked per document")
    parser.add_argument("--workload-sharegpt-path", default="", help="sharegpt: path to a local ShareGPT-format JSON trace file")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="run multi-seed trials against one policy's router")
    run_parser.add_argument("--router-url", default="http://localhost:8000")
    run_parser.add_argument("--policy-label", required=True, help="label for this run, e.g. swiftserve/round_robin")
    run_parser.add_argument("--model", default="Qwen/Qwen2.5-7B-Instruct")
    run_parser.add_argument("--num-sessions", type=int, default=30)
    run_parser.add_argument("--turns", type=int, default=4)
    run_parser.add_argument("--concurrency", type=int, default=6)
    run_parser.add_argument("--sla-ms", type=float, default=3000.0)
    run_parser.add_argument("--ttft-slo-ms", type=float, default=500.0, help="per-request TTFT SLO for goodput_rate")
    run_parser.add_argument("--tpot-slo-ms", type=float, default=50.0, help="per-request TPOT SLO for goodput_rate")
    run_parser.add_argument("--max-tokens", type=int, default=128)
    run_parser.add_argument("--seeds", default="1,2,3,4,5", help="comma-separated seeds, one independent trial each")
    run_parser.add_argument(
        "--rate", type=float, default=None,
        help="if given, each seed is one open-loop Poisson-arrival window at this target RPS instead of closed-loop",
    )
    _add_workload_args(run_parser)
    run_parser.add_argument("--output", required=True, help="path to write the JSON report to")
    run_parser.set_defaults(func=_cmd_run)

    compare_parser = subparsers.add_parser("compare", help="compare previously-saved reports")
    compare_parser.add_argument("reports", nargs="+", help="paths to JSON reports produced by `run`")
    compare_parser.set_defaults(func=_cmd_compare)

    goodput_parser = subparsers.add_parser(
        "goodput",
        help="open-loop load sweep to find the max RPS meeting an SLA-attainment target (DistServe-style Goodput)",
    )
    goodput_parser.add_argument("--router-url", default="http://localhost:8000")
    goodput_parser.add_argument("--policy-label", required=True, help="label for this run, e.g. swiftserve/round_robin")
    goodput_parser.add_argument("--model", default="Qwen/Qwen2.5-7B-Instruct")
    goodput_parser.add_argument(
        "--rps-levels", required=True, help="comma-separated target arrival rates to test, e.g. 5,10,15,20,25,30"
    )
    goodput_parser.add_argument(
        "--duration-s", type=float, default=20.0, help="how long to generate open-loop arrivals at each RPS level"
    )
    goodput_parser.add_argument("--turns", type=int, default=4)
    goodput_parser.add_argument("--sla-ms", type=float, default=3000.0)
    goodput_parser.add_argument(
        "--sla-target", type=float, default=0.9, help="minimum SLA-attainment fraction to count toward goodput (0.9 = Goodput@90)"
    )
    goodput_parser.add_argument("--max-tokens", type=int, default=128)
    goodput_parser.add_argument("--seed", type=int, default=1)
    _add_workload_args(goodput_parser)
    goodput_parser.add_argument("--output", required=True, help="path to write the JSON report to")
    goodput_parser.set_defaults(func=_cmd_goodput)

    goodput_compare_parser = subparsers.add_parser("goodput-compare", help="compare previously-saved goodput reports")
    goodput_compare_parser.add_argument("reports", nargs="+", help="paths to JSON reports produced by `goodput`")
    goodput_compare_parser.set_defaults(func=_cmd_goodput_compare)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
