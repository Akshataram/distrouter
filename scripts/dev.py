"""One command to run the router together with its web console.

    # Your real cluster: four Colab T4 nodes, each reached through a Cloudflare
    # tunnel URL printed by the notebook. The router and the console run here.
    python scripts/dev.py \\
        --replicas https://a.trycloudflare.com,https://b.trycloudflare.com,https://c.trycloudflare.com,https://d.trycloudflare.com \\
        --policy prefix_aware --model Qwen/Qwen2.5-3B-Instruct --tokenizer Qwen/Qwen2.5-3B-Instruct \\
        --max-num-seqs 4 --labels "colab-a T4,colab-b T4,colab-c T4,colab-d T4"

    # Only for developing the console without GPUs: explicit opt-in, labelled SIMULATED.
    python scripts/dev.py --simulate --policy prefix_aware

then open http://127.0.0.1:8000/ui/.

What this script does, and does not do
--------------------------------------
- It validates the setup first and says plainly what is wrong (a dead
  Cloudflare tunnel is reported differently from a live tunnel with nothing
  behind it, and a model name the replicas do not serve is caught here instead
  of as a 404 on your first request).
- It starts ONE router process (`uvicorn swiftserve.app:app`) with the same
  environment variables the router already understands. It does not change
  routing behaviour: unset options keep the router's own defaults.
- REAL replicas are the default and `--replicas` is required. Only with the
  explicit `--simulate` flag does it start fake replicas (`scripts/fake_vllm_stub.py`)
  and set SWIFTSERVE_SIMULATED=1 so the console labels all numbers as simulated.
- It NEVER starts vLLM, downloads a model, or touches a GPU. Real replicas are
  whatever you already have running (see notebooks/gpu_node.ipynb).
- Ctrl+C / SIGTERM stops the router gracefully (in-flight requests get
  `--graceful-timeout` seconds to finish), then any fake replicas.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import importlib.util
import os
import signal
import socket
import subprocess
import sys
import time
import webbrowser
from dataclasses import dataclass
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

REQUIRED_MODULES = {"fastapi": "fastapi", "uvicorn": "uvicorn", "httpx": "httpx", "prometheus_client": "prometheus-client", "pydantic": "pydantic"}


# --------------------------------------------------------------------------
# argument handling and pure helpers (unit-tested)
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--replicas", default=os.environ.get("SWIFTSERVE_REPLICAS", ""),
                   help="comma-separated replica base URLs (e.g. your Cloudflare tunnel URLs). "
                        "Default: $SWIFTSERVE_REPLICAS. Required unless --simulate is given.")
    p.add_argument("--simulate", action="store_true", help="development only: start FAKE replicas (no model, no GPU) and label everything SIMULATED")
    p.add_argument("--policy", default=os.environ.get("SWIFTSERVE_POLICY", "swiftserve"),
                   help="routing policy: round_robin, least_connections, swiftserve, prefix_aware "
                        "(default: $SWIFTSERVE_POLICY, else the router's own default 'swiftserve')")
    p.add_argument("--model", default=os.environ.get("SWIFTSERVE_MODEL", ""),
                   help="model name the replicas serve. If omitted it is read from the first reachable replica's /v1/models.")
    p.add_argument("--tokenizer", default=os.environ.get("SWIFTSERVE_TOKENIZER", ""),
                   help="HF model id for real tokenization (needs `pip install transformers`); required for meaningful "
                        "prefix_aware predictions against real vLLM")
    p.add_argument("--block-size", type=int, default=int(os.environ.get("SWIFTSERVE_BLOCK_SIZE", "16")),
                   help="must equal the replicas' vLLM --block-size")
    p.add_argument("--max-num-seqs", type=int, default=None,
                   help="the replicas' vLLM --max-num-seqs; sets SWIFTSERVE_ASSUMED_MAX_BATCH_SIZE (router default is 1)")
    p.add_argument("--index-max-blocks", type=int, default=None, help="sets SWIFTSERVE_INDEX_MAX_BLOCKS (prefix_aware index cap per replica)")
    p.add_argument("--min-match-tokens", type=int, default=None, help="sets SWIFTSERVE_MIN_MATCH_TOKENS")
    p.add_argument("--labels", default=os.environ.get("SWIFTSERVE_REPLICA_LABELS", ""),
                   help="comma-separated display labels, same order as --replicas (e.g. 'colab-a T4,colab-b T4,...'). "
                        "Labels are what YOU say each replica is; the router does not guess GPUs.")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--graceful-timeout", type=int, default=8, help="seconds in-flight requests get on shutdown")
    p.add_argument("--allow-unreachable", action="store_true", help="start even if no replica answers /health")
    p.add_argument("--check-only", action="store_true", help="run the startup validation and exit")
    p.add_argument("--open", action="store_true", help="open the console in a browser once the router is up")
    p.add_argument("--log-dir", default="dev_logs")
    sim = p.add_argument_group("simulated mode only (--simulate)")
    sim.add_argument("--num-replicas", type=int, default=4)
    sim.add_argument("--kv-blocks", type=int, default=375)
    sim.add_argument("--prefill-ms-per-token", type=float, default=0.2)
    sim.add_argument("--decode-ms-per-token", type=float, default=25.0)
    return p


def parse_replica_urls(raw: str) -> list[str]:
    urls = [u.strip().rstrip("/") for u in raw.split(",") if u.strip()]
    for u in urls:
        if not u.startswith(("http://", "https://")):
            raise ValueError(f"replica URL must start with http:// or https://: {u!r}")
    if len(set(urls)) != len(urls):
        raise ValueError("duplicate replica URL in --replicas")
    return urls


def missing_modules(required: dict[str, str] = REQUIRED_MODULES) -> list[str]:
    return [pip for mod, pip in required.items() if importlib.util.find_spec(mod) is None]


def port_in_use(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def build_router_env(args: argparse.Namespace, replica_urls: list[str], model: str, simulated: bool, base: dict | None = None) -> dict:
    """Environment for the router process. Only options the operator actually
    set are exported, so everything else keeps the router's own default."""
    env = dict(os.environ if base is None else base)
    env["SWIFTSERVE_REPLICAS"] = ",".join(replica_urls)
    env["SWIFTSERVE_POLICY"] = args.policy
    if model:
        env["SWIFTSERVE_MODEL"] = model
    if args.tokenizer:
        env["SWIFTSERVE_TOKENIZER"] = args.tokenizer
    env["SWIFTSERVE_BLOCK_SIZE"] = str(args.block_size)
    if args.max_num_seqs is not None:
        env["SWIFTSERVE_ASSUMED_MAX_BATCH_SIZE"] = str(args.max_num_seqs)
    if args.index_max_blocks is not None:
        env["SWIFTSERVE_INDEX_MAX_BLOCKS"] = str(args.index_max_blocks)
    if args.min_match_tokens is not None:
        env["SWIFTSERVE_MIN_MATCH_TOKENS"] = str(args.min_match_tokens)
    if args.labels:
        env["SWIFTSERVE_REPLICA_LABELS"] = args.labels
    if getattr(args, "host", "127.0.0.1") in ("127.0.0.1", "localhost", "::1"):
        # Only on a loopback bind: lets the console's Compare tab switch policies. Anyone who can
        # reach a router that is exposed on a network should not be able to change its routing.
        env["SWIFTSERVE_ALLOW_POLICY_SWITCH"] = "1"
    if simulated:
        env["SWIFTSERVE_SIMULATED"] = "1"
        env.setdefault("SWIFTSERVE_SCRAPE_INTERVAL_S", "1")
    else:
        env.pop("SWIFTSERVE_SIMULATED", None)
    return env


# --------------------------------------------------------------------------
# replica validation
# --------------------------------------------------------------------------


@dataclass
class ProbeResult:
    url: str
    ok: bool
    detail: str
    hint: str = ""


def classify_probe(url: str, status: int | None, headers: dict | None, error: Exception | None) -> ProbeResult:
    """Turn what a /health probe returned into a plain-language diagnosis.
    Cloudflare quick tunnels fail in recognisable ways, and telling them apart
    saves a lot of guessing."""
    if error is not None:
        name = type(error).__name__
        if isinstance(error, (httpx.ConnectTimeout, httpx.ReadTimeout)):
            return ProbeResult(url, False, f"timed out ({name})", "the host did not answer in time; check the URL and your network")
        if isinstance(error, httpx.ConnectError):
            return ProbeResult(url, False, "could not connect (DNS or network failure)",
                               "check the URL for typos; a trycloudflare.com host that no longer exists fails DNS")
        return ProbeResult(url, False, f"{name}: {error}", "")
    assert status is not None
    via_cloudflare = bool(headers and ("cf-ray" in headers or "cloudflare" in headers.get("server", "").lower()))
    if status == 200:
        return ProbeResult(url, True, "healthy (HTTP 200)")
    if status == 530 or (via_cloudflare and status in (1033, 1034)):
        return ProbeResult(url, False, f"HTTP {status} from Cloudflare", "the tunnel no longer exists: the Colab tunnel cell stopped or "
                           "the session ended. Re-run the notebook's tunnel cell and use the NEW URL it prints.")
    if status in (502, 503, 504):
        who = "Cloudflare reached the tunnel" if via_cloudflare else "the server answered"
        return ProbeResult(url, False, f"HTTP {status}", f"{who} but nothing healthy is behind it: the sidecar or vLLM is not up "
                           "(vLLM may still be loading weights, or the notebook's launch cell has crashed).")
    if status == 404:
        return ProbeResult(url, False, "HTTP 404 on /health", "reachable, but this does not look like a vLLM/sidecar URL")
    return ProbeResult(url, False, f"HTTP {status}", "unexpected status from /health")


async def probe_replica(client: httpx.AsyncClient, url: str) -> ProbeResult:
    try:
        resp = await client.get(f"{url}/health", timeout=6.0)
    except httpx.HTTPError as exc:
        return classify_probe(url, None, None, exc)
    return classify_probe(url, resp.status_code, dict(resp.headers), None)


async def detect_models(client: httpx.AsyncClient, url: str) -> list[str]:
    """Model ids a replica reports at /v1/models (empty if it does not say)."""
    try:
        resp = await client.get(f"{url}/v1/models", timeout=6.0)
        if resp.status_code == 200:
            return [m.get("id") for m in resp.json().get("data", []) if isinstance(m, dict) and m.get("id")]
    except (httpx.HTTPError, ValueError, AttributeError):
        pass
    return []


async def validate_replicas(urls: list[str], model: str) -> tuple[list[ProbeResult], str, list[str]]:
    """Probe every replica, resolve the model name, return (results, model, warnings)."""
    warnings: list[str] = []
    async with httpx.AsyncClient() as client:
        results = list(await asyncio.gather(*(probe_replica(client, u) for u in urls)))
        reachable = [r.url for r in results if r.ok]
        served: list[str] = []
        for u in reachable[:1]:
            served = await detect_models(client, u)
    if served:
        if not model:
            model = served[0]
            warnings.append(f"model not given; using {model!r}, the first model replica 0 reports at /v1/models")
        elif model not in served:
            warnings.append(f"model {model!r} is not in the replica's /v1/models list {served}: vLLM rejects unknown model "
                            f"names with HTTP 404, so requests would fail. Use --model {served[0]}.")
    elif reachable and not model:
        warnings.append("model not given and the replica did not report one at /v1/models; the router will send its built-in "
                        "default model name, which a real vLLM will probably reject. Pass --model.")
    return results, model, warnings


def policy_warnings(args: argparse.Namespace, simulated: bool) -> list[str]:
    w: list[str] = []
    if args.policy not in {"round_robin", "least_connections", "swiftserve", "prefix_aware"}:
        w.append(f"unknown policy {args.policy!r}; the router will refuse to start")
    if not simulated and args.policy == "prefix_aware":
        if not args.tokenizer:
            w.append("prefix_aware without --tokenizer uses a fake byte tokenizer: its block boundaries do not match a real vLLM, "
                     "so its cache predictions will be wrong. Pass --tokenizer <the served model id> and `pip install transformers`.")
        elif importlib.util.find_spec("transformers") is None:
            w.append("--tokenizer is set but `transformers` is not installed: the router will fail its startup tokenizer check. "
                     "Run `pip install transformers`.")
    if not simulated and args.policy in {"prefix_aware", "swiftserve"} and args.max_num_seqs is None and "SWIFTSERVE_ASSUMED_MAX_BATCH_SIZE" not in os.environ:
        w.append("--max-num-seqs not given: the router's latency model assumes each replica runs ONE request at a time, which can make "
                 "the SLA check veto a cache-warm replica. Pass your replicas' vLLM --max-num-seqs (your notebook uses 32).")
    return w


# --------------------------------------------------------------------------
# process management
# --------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def spawn_fake_replicas(args: argparse.Namespace, log_dir: Path) -> tuple[list[subprocess.Popen], list[str]]:
    procs, urls = [], []
    for i in range(args.num_replicas):
        port = _free_port()
        log = open(log_dir / f"replica-{i}.log", "w")  # noqa: SIM115 - lives as long as the subprocess
        procs.append(subprocess.Popen(
            [sys.executable, str(REPO_ROOT / "scripts" / "fake_vllm_stub.py"), "--port", str(port), "--tag", f"sim{i}",
             "--block-size", str(args.block_size), "--kv-blocks", str(args.kv_blocks),
             "--prefill-ms-per-token", str(args.prefill_ms_per_token), "--decode-ms-per-token", str(args.decode_ms_per_token),
             "--tokenizer", args.tokenizer],
            stdout=log, stderr=subprocess.STDOUT))
        urls.append(f"http://127.0.0.1:{port}")
    return procs, urls


async def wait_healthy(url: str, proc: subprocess.Popen | None, timeout_s: float, path: str = "/healthz") -> bool:
    deadline = time.monotonic() + timeout_s
    async with httpx.AsyncClient() as client:
        while time.monotonic() < deadline:
            if proc is not None and proc.poll() is not None:
                return False
            with contextlib.suppress(httpx.HTTPError):
                if (await client.get(f"{url}{path}", timeout=2.0)).status_code == 200:
                    return True
            await asyncio.sleep(0.4)
    return False


def tail(path: Path, n: int = 25) -> str:
    try:
        return "".join(path.read_text(errors="replace").splitlines(keepends=True)[-n:])
    except OSError:
        return "(no log)"


def stop(proc: subprocess.Popen, grace_s: float) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=grace_s)
    except subprocess.TimeoutExpired:
        proc.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=5)


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    log_dir = Path(args.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    print("SwiftServe dev launcher\n")
    missing = missing_modules()
    if missing:
        print(f"ERROR: missing Python packages: {', '.join(missing)}\n  pip install -r requirements.txt", file=sys.stderr)
        return 2
    try:
        replica_urls = parse_replica_urls(args.replicas)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    simulated = bool(args.simulate)
    if simulated and replica_urls:
        print("ERROR: --simulate starts its own fake replicas; do not combine it with --replicas.", file=sys.stderr)
        return 2
    if not simulated and not replica_urls:
        print("ERROR: no replicas given. Pass --replicas with your Cloudflare tunnel URLs (or set SWIFTSERVE_REPLICAS).\n"
              "  (For console development without GPUs only: --simulate.)", file=sys.stderr)
        return 2

    if not args.check_only and port_in_use(args.host, args.port):
        print(f"ERROR: {args.host}:{args.port} is already in use. Stop whatever is listening there or pass --port.", file=sys.stderr)
        return 2
    labels = [x.strip() for x in args.labels.split(",")] if args.labels else []
    if labels and not simulated and len(labels) != len(replica_urls):
        print(f"ERROR: --labels has {len(labels)} entries but there are {len(replica_urls)} replicas; they are matched by position.", file=sys.stderr)
        return 2

    replica_procs: list[subprocess.Popen] = []
    router: subprocess.Popen | None = None

    def shutdown(*_: object) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, shutdown)
    try:
        model = args.model
        warnings: list[str] = []
        if simulated:
            print(f"--simulate: starting {args.num_replicas} FAKE replicas (no model, no GPU). Everything will be labelled SIMULATED.")
            if args.check_only:
                print("check-only: nothing started.")
                return 0
            replica_procs, replica_urls = spawn_fake_replicas(args, log_dir)
            for i, u in enumerate(replica_urls):
                if not asyncio.run(wait_healthy(u, replica_procs[i], 30, "/health")):
                    print(f"ERROR: fake replica {i} did not start; see {log_dir}/replica-{i}.log\n{tail(log_dir / f'replica-{i}.log')}", file=sys.stderr)
                    return 1
            model = model or "demo-model"
            if not labels:
                args.labels = ",".join(f"simulated-{i}" for i in range(len(replica_urls)))
            if args.max_num_seqs is None:
                args.max_num_seqs = 32  # the fake replicas batch without limit; this only stops the latency model vetoing
        else:
            print(f"Checking {len(replica_urls)} replica(s)...")
            results, model, warnings = asyncio.run(validate_replicas(replica_urls, model))
            for i, r in enumerate(results):
                print(f"  [{'ok' if r.ok else 'DOWN':>4}] replica {i}  {r.url}\n         {r.detail}" + (f"\n         -> {r.hint}" if r.hint else ""))
            if not any(r.ok for r in results):
                msg = "no replica answered /health"
                if not args.allow_unreachable:
                    print(f"\nERROR: {msg}. Fix the URLs above, or pass --allow-unreachable to start anyway "
                          f"(the router will then show every replica as down).", file=sys.stderr)
                    return 1
                print(f"\nWARNING: {msg}; starting anyway because of --allow-unreachable.")
            elif not all(r.ok for r in results):
                print("\nWARNING: some replicas are down. The router starts, marks them unhealthy and routes around them once its circuit breaker trips.")
        warnings += policy_warnings(args, simulated)
        for w in warnings:
            print(f"\nWARNING: {w}")
        if args.check_only:
            print("\ncheck-only: validation finished, nothing started.")
            return 0

        env = build_router_env(args, replica_urls, model, simulated)
        log_path = log_dir / "router.log"
        base_url = f"http://{args.host}:{args.port}"
        print(f"\nStarting the router ({args.policy}) ... log: {log_path}")
        router = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "swiftserve.app:app", "--host", args.host, "--port", str(args.port),
             "--timeout-graceful-shutdown", str(args.graceful_timeout), "--log-level", "info"],
            env=env, cwd=str(REPO_ROOT), stdout=open(log_path, "w"), stderr=subprocess.STDOUT)  # noqa: SIM115
        if not asyncio.run(wait_healthy(base_url, router, 60)):
            print(f"\nERROR: the router did not become healthy.\n--- last lines of {log_path} ---\n{tail(log_path)}", file=sys.stderr)
            return 1
        print(f"\nRouter is up.  Console:  {base_url}/ui/\n"
              f"  mode: {'SIMULATED (fake replicas)' if simulated else 'live replicas'} | policy: {args.policy} | model: {model or '(router default)'}\n"
              f"  Ctrl+C to stop (in-flight requests get up to {args.graceful_timeout}s).")
        if args.open:
            webbrowser.open(f"{base_url}/ui/")
        while router.poll() is None:
            time.sleep(0.5)
        print(f"\nThe router exited with code {router.returncode}; see {log_path}\n{tail(log_path)}", file=sys.stderr)
        return router.returncode or 1
    except KeyboardInterrupt:
        print("\nStopping ...")
        return 0
    finally:
        if router is not None:
            stop(router, args.graceful_timeout + 5)
        for p in replica_procs:
            stop(p, 5)


if __name__ == "__main__":
    raise SystemExit(main())
