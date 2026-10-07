"""One-command side-by-side demo: does prefix-aware routing actually lower TTFT?

Runs the *same* shared-system-prompt workload through a router configured
with each policy in turn, and prints what changed. The quantity to watch is
TTFT and `true cache ratio`, not end-to-end latency -- prefix caching only
saves prompt processing, and e2e latency buries that under generation time.

Two modes, same code path:

    # GPU-free rehearsal: spawns fake vLLM replicas locally too.
    python scripts/demo.py

    # The real thing: router runs here, replicas are real vLLM over tunnels.
    python scripts/demo.py \\
        --replicas https://a.trycloudflare.com,https://b.trycloudflare.com \\
        --model Qwen/Qwen2.5-3B-Instruct \\
        --tokenizer Qwen/Qwen2.5-3B-Instruct

The router always runs locally (it is CPU-only); only the replicas differ.

## Honesty notes, because this is a demo and not a benchmark

- With `--replicas` omitted, the replicas are `scripts/fake_vllm_stub.py`:
  real block-level prefix caching and real SSE, but **no model and a linear
  timing model instead of a GPU**. Numbers from that mode are labeled
  SIMULATED in the output and must be presented that way.
- Policies are run **sequentially**, so a slow minute or a drifting node is
  confounded with the policy. That is fine for an effect this large and
  wrong for a publishable number -- use `scripts/benchmark.py` with
  multiple seeds, and the interleaved design in RESEARCH_PLAN_V2.md section
  6.2, for anything you would defend.
- Replica caches are reset between policies where the replica supports it
  (the stub always does; recent vLLM exposes `/reset_prefix_cache`). If a
  reset fails the demo says so loudly, because without it each policy
  inherits the previous one's warm caches and the comparison is meaningless.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import socket
import statistics
import subprocess
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.benchmark import (  # noqa: E402
    dual_slo_attainment,
    percentile,
    run_session_from_requests,
)
from scripts.workloads import shared_system_prompt  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _wait_healthy(url: str, timeout_s: float = 60.0, path: str = "/healthz") -> bool:
    deadline = time.monotonic() + timeout_s
    async with httpx.AsyncClient() as client:
        while time.monotonic() < deadline:
            try:
                if (await client.get(f"{url}{path}", timeout=3.0)).status_code == 200:
                    return True
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.2)
    return False


async def reset_replica_caches(replica_urls: list[str]) -> list[str]:
    """Best-effort cache clear between policies. Returns the URLs that could
    NOT be reset, so the caller can warn rather than silently compare a
    cold policy against a warm one.

    Tries the stub's own endpoint first, then vLLM's `/reset_prefix_cache`
    (present in recent versions)."""
    failed = []
    async with httpx.AsyncClient() as client:
        for url in replica_urls:
            for path in ("/cache_reset", "/reset_prefix_cache"):
                try:
                    resp = await client.post(f"{url}{path}", timeout=10.0)
                    if resp.status_code < 400:
                        break
                except httpx.HTTPError:
                    continue
            else:
                failed.append(url)
    return failed


def spawn_fake_replicas(count: int, kv_blocks: int, prefill_ms: float, decode_ms: float,
                        tokenizer: str, log_dir: Path) -> tuple[list[subprocess.Popen], list[str]]:
    procs, urls = [], []
    log_dir.mkdir(parents=True, exist_ok=True)
    for i in range(count):
        port = _free_port()
        # Deliberately not a context manager: the handle must stay open for
        # the subprocess's whole lifetime, which outlives this function.
        log = open(log_dir / f"replica-{i}.log", "w")  # noqa: SIM115
        procs.append(
            subprocess.Popen(
                [
                    sys.executable, str(REPO_ROOT / "scripts" / "fake_vllm_stub.py"),
                    "--port", str(port), "--tag", f"r{i}",
                    "--kv-blocks", str(kv_blocks),
                    "--prefill-ms-per-token", str(prefill_ms),
                    "--decode-ms-per-token", str(decode_ms),
                    "--tokenizer", tokenizer,
                ],
                stdout=log, stderr=subprocess.STDOUT,
            )
        )
        urls.append(f"http://127.0.0.1:{port}")
    return procs, urls


def spawn_router(policy: str, replica_urls: list[str], model: str, tokenizer: str,
                 block_size: int, min_match_tokens: int, log_dir: Path) -> tuple[subprocess.Popen, str]:
    port = _free_port()
    env = {
        **os.environ,
        "SWIFTSERVE_REPLICAS": ",".join(replica_urls),
        "SWIFTSERVE_MODEL": model,
        "SWIFTSERVE_POLICY": policy,
        "SWIFTSERVE_TOKENIZER": tokenizer,
        "SWIFTSERVE_BLOCK_SIZE": str(block_size),
        "SWIFTSERVE_MIN_MATCH_TOKENS": str(min_match_tokens),
        # Keep the scrape loop lively so load signals are fresh enough for a
        # short demo run.
        "SWIFTSERVE_SCRAPE_INTERVAL_S": "1",
    }
    log_dir.mkdir(parents=True, exist_ok=True)
    log = open(log_dir / f"router-{policy}.log", "w")  # noqa: SIM115 - outlives this function
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "swiftserve.app:app", "--port", str(port), "--log-level", "warning"],
        env=env, stdout=log, stderr=subprocess.STDOUT, cwd=str(REPO_ROOT),
    )
    return proc, f"http://127.0.0.1:{port}"


async def run_workload(router_url: str, model: str, sessions: list, concurrency: int) -> list[dict]:
    results: list[dict] = []
    semaphore = asyncio.Semaphore(concurrency)

    async def one(session_requests):
        async with semaphore, httpx.AsyncClient() as client:
            await run_session_from_requests(client, router_url, model, session_requests, results)

    await asyncio.gather(*(one(s) for s in sessions))
    return results


def summarize(results: list[dict]) -> dict:
    ok = [r for r in results if "e2e_ms" in r and r.get("status") == 200]
    ttfts = sorted(r["ttft_ms"] for r in ok if r.get("ttft_ms") is not None)
    e2es = sorted(r["e2e_ms"] for r in ok)
    prompt_tokens = sum(r.get("prompt_tokens") or 0 for r in ok)
    cached_tokens = sum(r.get("cached_tokens") or 0 for r in ok)
    predicted = sum(r.get("predicted_cached_tokens") or 0 for r in ok)

    per_replica: dict[str, int] = {}
    for r in ok:
        if r.get("replica"):
            per_replica[r["replica"]] = per_replica.get(r["replica"], 0) + 1

    # Mean absolute prediction error as a fraction of the prompt -- how wrong
    # the router's belief about the cache was, per request.
    errs = [
        abs((r.get("predicted_cached_tokens") or 0) - (r.get("cached_tokens") or 0)) / r["prompt_tokens"]
        for r in ok if (r.get("prompt_tokens") or 0) > 0
    ]

    return {
        "requests": len(ok),
        "errors": len(results) - len(ok),
        "ttft_p50": percentile(ttfts, 50) if ttfts else None,
        "ttft_p95": percentile(ttfts, 95) if len(ttfts) >= 2 else None,
        "e2e_p50": percentile(e2es, 50) if e2es else None,
        "true_cache_ratio": (cached_tokens / prompt_tokens) if prompt_tokens else None,
        "predicted_tokens": predicted,
        "cached_tokens": cached_tokens,
        "prediction_error": statistics.fmean(errs) if errs else None,
        "goodput": dual_slo_attainment(results),
        "per_replica": per_replica,
        "replica_spread": (max(per_replica.values()) / statistics.fmean(list(per_replica.values())))
        if per_replica else None,
    }


def _fmt(value, suffix="", pct=False, nd=0):
    if value is None:
        return "n/a"
    if pct:
        return f"{value:.1%}"
    return f"{value:.{nd}f}{suffix}"


def print_report(reports: dict[str, dict], simulated: bool, n_replicas: int) -> None:
    label = "SIMULATED (fake vLLM: real prefix caching, no model, linear timing)" if simulated \
        else "REAL vLLM on real GPUs"
    print()
    print("=" * 78)
    print(f"  SwiftServe routing demo -- {label}")
    print(f"  {n_replicas} replicas | workload: shared system prompts (Zipf-skewed tenants)")
    print("=" * 78)

    rows = [
        ("TTFT p50", lambda s: _fmt(s["ttft_p50"], "ms")),
        ("TTFT p95", lambda s: _fmt(s["ttft_p95"], "ms")),
        ("true cache ratio", lambda s: _fmt(s["true_cache_ratio"], pct=True)),
        ("prediction error", lambda s: _fmt(s["prediction_error"], pct=True)),
        ("goodput (TTFT+TPOT SLO)", lambda s: _fmt(s["goodput"], pct=True)),
        ("e2e p50", lambda s: _fmt(s["e2e_p50"], "ms")),
        ("requests / errors", lambda s: f"{s['requests']}/{s['errors']}"),
        ("max:mean per replica", lambda s: _fmt(s["replica_spread"], nd=2)),
    ]

    names = list(reports)
    width = max(24, max(len(n) for n in names) + 4)
    print(f"\n{'metric':<26}" + "".join(f"{n:>{width}}" for n in names))
    print("-" * (26 + width * len(names)))
    for metric, fn in rows:
        print(f"{metric:<26}" + "".join(f"{fn(reports[n]):>{width}}" for n in names))

    print(f"\n{'requests per replica':<26}" + "".join(f"{str(reports[n]['per_replica']):>{width}}" for n in names))

    best = min((n for n in names if reports[n]["ttft_p50"] is not None),
               key=lambda n: reports[n]["ttft_p50"], default=None)
    if best and len(names) > 1:
        others = [n for n in names if n != best and reports[n]["ttft_p50"]]
        if others:
            worst = max(others, key=lambda n: reports[n]["ttft_p50"])
            ratio = reports[worst]["ttft_p50"] / reports[best]["ttft_p50"]
            print(f"\n-> {best} has {ratio:.1f}x lower TTFT p50 than {worst}")

    print(
        "\nNote: policies ran sequentially, so this is a demo, not a defensible measurement."
        "\n      For numbers you would publish: scripts/benchmark.py with multiple seeds,"
        "\n      plus the interleaved design in RESEARCH_PLAN_V2.md section 6.2."
    )


async def main_async(args: argparse.Namespace) -> int:
    log_dir = Path(args.log_dir)
    simulated = not args.replicas
    replica_procs: list[subprocess.Popen] = []
    router_proc: subprocess.Popen | None = None

    try:
        if simulated:
            print(f"Starting {args.num_replicas} fake vLLM replicas "
                  f"(kv_blocks={args.kv_blocks}, prefill={args.prefill_ms_per_token}ms/tok)...")
            replica_procs, replica_urls = spawn_fake_replicas(
                args.num_replicas, args.kv_blocks, args.prefill_ms_per_token,
                args.decode_ms_per_token, args.tokenizer, log_dir,
            )
            for url in replica_urls:
                if not await _wait_healthy(url, timeout_s=30.0, path="/health"):
                    print(f"ERROR: fake replica at {url} never became healthy; see {log_dir}", file=sys.stderr)
                    return 1
        else:
            replica_urls = [u.strip().rstrip("/") for u in args.replicas.split(",") if u.strip()]
            print(f"Using {len(replica_urls)} provided replicas: {', '.join(replica_urls)}")

        workload = shared_system_prompt(
            num_apps=args.num_apps,
            sessions=args.sessions,
            turns=args.turns,
            system_prompt_tokens=args.system_prompt_tokens,
            max_tokens=args.max_tokens,
            seed=args.seed,
        )
        print(f"Workload: {len(workload.sessions)} sessions x {args.turns} turn(s), "
              f"{args.num_apps} distinct system prompts of ~{args.system_prompt_tokens} tokens\n")

        reports: dict[str, dict] = {}
        for policy in [p.strip() for p in args.policies.split(",") if p.strip()]:
            failed = await reset_replica_caches(replica_urls)
            if failed:
                print(f"  WARNING: could not reset the prefix cache on {failed}.")
                print("           This policy inherits the previous one's warm cache, so the")
                print("           comparison below understates the difference. Restart the")
                print("           replicas between policies for a clean run.")

            router_proc, router_url = spawn_router(
                policy, replica_urls, args.model, args.tokenizer,
                args.block_size, args.min_match_tokens, log_dir,
            )
            if not await _wait_healthy(router_url):
                print(f"ERROR: router for {policy} never became healthy; see {log_dir}/router-{policy}.log",
                      file=sys.stderr)
                return 1

            print(f"  [{policy}] running {len(workload.sessions)} sessions...", end="", flush=True)
            started = time.monotonic()
            results = await run_workload(router_url, args.model, workload.sessions, args.concurrency)
            reports[policy] = summarize(results)
            print(f" done in {time.monotonic() - started:.1f}s "
                  f"(TTFT p50 {_fmt(reports[policy]['ttft_p50'], 'ms')}, "
                  f"cache {_fmt(reports[policy]['true_cache_ratio'], pct=True)})")

            router_proc.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                router_proc.wait(timeout=10)
            router_proc = None

        print_report(reports, simulated, len(replica_urls))
        return 0

    finally:
        if router_proc is not None:
            router_proc.kill()
        for p in replica_procs:
            p.terminate()
        for p in replica_procs:
            with contextlib.suppress(subprocess.TimeoutExpired):
                p.wait(timeout=10)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--replicas", default="",
                        help="comma-separated replica base URLs (real vLLM). Omit to spawn fake ones locally.")
    parser.add_argument("--policies", default="least_connections,prefix_aware",
                        help="comma-separated policies to compare, in order")
    parser.add_argument("--model", default="demo-model")
    parser.add_argument("--tokenizer", default="",
                        help="model id for real tokenization (needs `transformers`). MUST match what the "
                             "replicas serve, or the router's block hashes never line up with the engine's.")
    parser.add_argument("--block-size", type=int, default=16, help="must match the replicas' vLLM --block-size")
    parser.add_argument("--min-match-tokens", type=int, default=256)

    parser.add_argument("--num-replicas", type=int, default=3, help="fake-replica mode only")
    parser.add_argument("--kv-blocks", type=int, default=400,
                        help="fake-replica cache capacity in blocks. Keep this SMALLER than the workload's "
                             "distinct-prefix working set or nothing ever evicts and all policies tie.")
    parser.add_argument("--prefill-ms-per-token", type=float, default=0.2, help="fake-replica mode only")
    parser.add_argument("--decode-ms-per-token", type=float, default=5.0, help="fake-replica mode only")

    parser.add_argument("--num-apps", type=int, default=6)
    parser.add_argument("--sessions", type=int, default=24)
    parser.add_argument("--turns", type=int, default=1)
    parser.add_argument("--system-prompt-tokens", type=int, default=2000)
    parser.add_argument("--max-tokens", type=int, default=8)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--log-dir", default="demo_logs")

    args = parser.parse_args()
    raise SystemExit(asyncio.run(main_async(args)))


if __name__ == "__main__":
    main()
