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
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import random
import statistics
import time
import uuid
from collections.abc import Callable

import httpx

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
                    # A non-200 (e.g. a 503 admission rejection) is never a
                    # met SLA just because it came back fast -- SLA
                    # attainment means the request actually succeeded AND
                    # was fast enough, not merely "some response arrived".
                    "sla_violated": resp.status_code != 200 or elapsed_ms > sla_ms,
                }
            )
            if resp.status_code == 200:
                reply = resp.json()["choices"][0]["message"]["content"]
                messages.append({"role": "assistant", "content": reply})
        except httpx.HTTPError as exc:
            results.append({"session_id": session_id, "turn": turn, "error": str(exc)})


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


async def run_one_trial(
    router_url: str, model: str, num_sessions: int, turns: int, concurrency: int,
    sla_ms: float, max_tokens: int, seed: int,
) -> dict:
    random.seed(seed)
    results: list[dict] = []
    semaphore = asyncio.Semaphore(concurrency)

    async def bounded_session(session_id: str):
        async with semaphore, httpx.AsyncClient() as client:
            await run_session(client, router_url, model, session_id, turns, sla_ms, max_tokens, results)

    session_ids = [f"bench-{seed}-{uuid.uuid4().hex[:8]}" for _ in range(num_sessions)]
    await asyncio.gather(*(bounded_session(sid) for sid in session_ids))

    ok = [r for r in results if "latency_ms" in r]
    if not ok:
        return {"seed": seed, "ok": 0, "errors": len(results), "latencies": [], "cache_hit_rate": None, "sla_violation_rate": None}

    latencies = [r["latency_ms"] for r in ok]
    return {
        "seed": seed,
        "ok": len(ok),
        "errors": len(results) - len(ok),
        "latencies": latencies,
        "cache_hit_rate": sum(1 for r in ok if r.get("cache_hit") == "true") / len(ok),
        "sla_violation_rate": sum(1 for r in ok if r["sla_violated"]) / len(ok),
    }


async def run_policy_trials(
    router_url: str, policy_label: str, model: str, num_sessions: int, turns: int,
    concurrency: int, sla_ms: float, max_tokens: int, seeds: list[int],
) -> list[dict]:
    trials = []
    for seed in seeds:
        trial = await run_one_trial(router_url, model, num_sessions, turns, concurrency, sla_ms, max_tokens, seed)
        if trial["latencies"]:
            print(
                f"  [{policy_label}] seed={seed}: {trial['ok']} ok, {trial['errors']} errors, "
                f"mean={statistics.fmean(trial['latencies']):.1f}ms, cache_hit={trial['cache_hit_rate']:.1%}"
            )
        else:
            print(f"  [{policy_label}] seed={seed}: no successful requests ({trial['errors']} errors)")
        trials.append(trial)
    return trials


# -- Goodput: open-loop load sweep (DistServe-style) -------------------------
#
# run_one_trial/run_policy_trials above are *closed-loop*: a fixed number of
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
    max_drain_s: float = 60.0,
) -> list[dict]:
    """New sessions arrive as a Poisson process at target_rps for
    duration_s seconds (each running `turns` sequential turns, same
    run_session as the closed-loop path), then waits up to max_drain_s for
    whatever's still in flight before giving up on stragglers. One shared,
    connection-pooled client: at realistic RPS many sessions overlap, and
    a fresh TCP/TLS handshake per session (as the closed-loop trials use)
    would itself skew the very latencies this is trying to measure."""
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
) -> dict:
    """Runs run_open_loop_arrivals once per RPS in rps_levels (in the
    order given -- ascending is the natural choice but not enforced) and
    reports per-level offered/completed counts, SLA attainment, and
    latency. Does not itself decide "the" goodput number -- see
    find_goodput, kept separate so a saved report can be re-evaluated
    against a different --sla-target without re-running live traffic."""
    levels = []
    for rps in rps_levels:
        results = await run_open_loop_arrivals(router_url, model, rps, duration_s, turns, sla_ms, max_tokens, seed)
        attainment = sla_attainment(results, sla_ms)
        ok = [r for r in results if "latency_ms" in r]
        latencies = sorted(r["latency_ms"] for r in ok)
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

    def ci_or_nan(values):
        return bootstrap_ci(values) if values else (float("nan"), float("nan"), float("nan"))

    mean_point, mean_lo, mean_hi = ci_or_nan(per_trial_means)
    cache_point, cache_lo, cache_hi = ci_or_nan(cache_hit_rates)
    sla_point, sla_lo, sla_hi = ci_or_nan(sla_violation_rates)

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
        "cache_hit_rate": {"point": cache_point, "ci95_low": cache_lo, "ci95_high": cache_hi},
        "sla_violation_rate": {"point": sla_point, "ci95_low": sla_lo, "ci95_high": sla_hi},
        "per_trial_means_ms": per_trial_means,
        "all_latencies_ms": all_latencies,
    }


def _fmt_percentile_cell(pct: dict) -> str:
    if pct["point"] is None:
        return "n/a"
    flag = "" if pct["reliable"] else "*"
    return f"{pct['point']:.0f}{flag}"


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

    print("\nPairwise significance (permutation test on per-trial mean latency, two-sided, alpha=0.05):")
    for i in range(len(reports)):
        for j in range(i + 1, len(reports)):
            a, b = reports[i], reports[j]
            if len(a["per_trial_means_ms"]) < 2 or len(b["per_trial_means_ms"]) < 2:
                print(f"  {a['policy']} vs {b['policy']}: need >=2 trials each for a significance test, skipping")
                continue
            diff, p = permutation_test_diff_means(a["per_trial_means_ms"], b["per_trial_means_ms"])
            verdict = "significant" if p < 0.05 else "NOT significant"
            print(f"  {a['policy']} vs {b['policy']}: diff={diff:+.1f}ms, p={p:.4f} -> {verdict}")


def _cmd_run(args: argparse.Namespace) -> None:
    seeds = [int(s) for s in args.seeds.split(",")]
    trials = asyncio.run(
        run_policy_trials(
            router_url=args.router_url.rstrip("/"),
            policy_label=args.policy_label,
            model=args.model,
            num_sessions=args.num_sessions,
            turns=args.turns,
            concurrency=args.concurrency,
            sla_ms=args.sla_ms,
            max_tokens=args.max_tokens,
            seeds=seeds,
        )
    )
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
    run_parser.add_argument("--max-tokens", type=int, default=128)
    run_parser.add_argument("--seeds", default="1,2,3,4,5", help="comma-separated seeds, one independent trial each")
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
    goodput_parser.add_argument("--output", required=True, help="path to write the JSON report to")
    goodput_parser.set_defaults(func=_cmd_goodput)

    goodput_compare_parser = subparsers.add_parser("goodput-compare", help="compare previously-saved goodput reports")
    goodput_compare_parser.add_argument("reports", nargs="+", help="paths to JSON reports produced by `goodput`")
    goodput_compare_parser.set_defaults(func=_cmd_goodput_compare)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
