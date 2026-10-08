"""Interactive playground: send your own requests through each routing policy
and watch what the router and the engine did with them.

`scripts/demo.py` fires a fixed 24-request workload and prints one table.
This is the hands-on version: a local web page where you pick a company's
system prompt, type a question, choose a policy, press Send, and see -- per
request -- which GPU got it, the TTFT, how many prompt tokens the engine
actually reused (`cached_tokens`), and how many the router *predicted* it
would. Send the same system prompt twice in a row and the second request
should be fast if the router sent it back to the GPU that already read it.

    # GPU-free rehearsal (fake replicas, labelled SIMULATED in the page)
    python scripts/playground.py

    # Real cluster: router(s) run here, replicas are your tunnelled vLLM nodes
    python scripts/playground.py \\
        --replicas https://a.trycloudflare.com,https://b.trycloudflare.com \\
        --model Qwen/Qwen2.5-3B-Instruct --tokenizer Qwen/Qwen2.5-3B-Instruct

then open http://127.0.0.1:8080.

## How it is wired

One router process per policy is started (the same `spawn_router` the demo
uses), all pointing at the same replicas. A router runs exactly one policy,
so "compare policies" means "talk to a different router". This page's own
server just relays a request to the chosen router, streams the reply to
measure TTFT, and reads the same headers and usage fields `benchmark.py`
reads. It adds no routing logic of its own.

## Honesty notes

- **Isolation.** By default each policy's system prompt gets a `[ns:<policy>]`
  marker at the very front. That changes block 0 and therefore every chained
  hash after it, so one policy cannot reuse blocks another policy warmed.
  Untick it only if you want policies to share (and contaminate) one cache.
- **Fake replicas** (no `--replicas`) are the in-repo stub: real block-level
  prefix caching and real SSE, but no model and a linear timing model. The
  page says SIMULATED and those numbers must be presented that way.
- **Each router keeps its own index.** "Reset routers" restarts them (empty
  index) and clears replica caches where the replica supports it.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import random
import subprocess
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.benchmark import _stream_chat_turn  # noqa: E402
from scripts.demo import _wait_healthy, reset_replica_caches, spawn_fake_replicas, spawn_router  # noqa: E402
from swiftserve.policy import POLICIES  # noqa: E402

UI_PATH = Path(__file__).resolve().parent / "playground_ui.html"
DEFAULT_POLICIES = ["round_robin", "least_connections", "swiftserve", "prefix_aware"]

# -- system-prompt presets ---------------------------------------------------
#
# Readable stand-ins for "a company's long instructions". Deterministic: the
# same (preset, size) always yields byte-identical text, which is the whole
# point -- two requests only share cache blocks if their system prompts are
# identical token for token.

_PRESETS: dict[str, dict] = {
    "acme-bank": {
        "name": "Acme Bank", "role": "a retail-banking support assistant",
        "topics": ["card disputes", "wire transfers", "overdraft fees", "mortgage questions",
                   "fraud alerts", "account closure", "interest rates", "statement requests"],
    },
    "skytravel": {
        "name": "SkyTravel Airlines", "role": "an airline booking assistant",
        "topics": ["baggage allowance", "rebooking", "refunds", "seat selection",
                   "visa documents", "loyalty miles", "flight delays", "pet travel"],
    },
    "medicare-plus": {
        "name": "MediCare Plus", "role": "a patient-services assistant",
        "topics": ["appointment booking", "prescription refills", "insurance coverage", "lab results",
                   "referrals", "billing questions", "telehealth visits", "emergency guidance"],
    },
    "shopeasy": {
        "name": "ShopEasy", "role": "an online-shopping support assistant",
        "topics": ["order tracking", "returns", "gift cards", "promo codes",
                   "shipping times", "damaged items", "price matching", "warranty claims"],
    },
    "codehelp": {
        "name": "CodeHelp Cloud", "role": "a developer-tools support assistant",
        "topics": ["API keys", "rate limits", "webhooks", "SDK versions",
                   "billing plans", "service outages", "SSO setup", "data export"],
    },
    "legaldesk": {
        "name": "LegalDesk", "role": "a legal-information assistant",
        "topics": ["contract review", "NDA templates", "trademark questions", "data privacy",
                   "employment basics", "dispute resolution", "incorporation", "compliance deadlines"],
    },
}

_ACTIONS = [
    "ask for the account reference before sharing any details",
    "answer in at most three short sentences",
    "never speculate; say you will escalate to a human agent",
    "quote the relevant policy section by number",
    "offer one follow-up question to clarify the request",
    "mention the standard processing time",
    "decline politely if identity cannot be verified",
    "summarize the next step in one line",
]

SUGGESTED_QUESTIONS = [
    "How do I get started?",
    "What is your refund policy?",
    "Can you summarize my options?",
    "Who do I contact for urgent help?",
]


def make_system_prompt(preset_id: str, target_tokens: int = 2000) -> str:
    """Deterministic system prompt for `preset_id`, sized so the chars/4
    heuristic gives about `target_tokens` (a real tokenizer lands within
    roughly 10-25% of that; the page shows the router's own count after the
    first send via `prompt_tokens`)."""
    if preset_id not in _PRESETS:
        raise KeyError(preset_id)
    preset = _PRESETS[preset_id]
    rng = random.Random(preset_id)
    head = f"You are {preset['role']} for {preset['name']}. Follow every rule below exactly.\n"
    lines = [head]
    length = len(head)
    target_chars = max(0, target_tokens) * 4
    n = 1
    while length < target_chars:
        topic = preset["topics"][(n - 1) % len(preset["topics"])]
        action = rng.choice(_ACTIONS)
        line = f"Rule {n}: when a {preset['name']} customer asks about {topic}, {action}.\n"
        lines.append(line)
        length += len(line)
        n += 1
    return "".join(lines)


# -- shared state --------------------------------------------------------------


@dataclass
class PlaygroundState:
    model: str
    tokenizer: str
    policies: list[str]
    replica_urls: list[str]
    simulated: bool
    block_size: int = 16
    min_match_tokens: int = 256
    max_num_seqs: int = 32
    log_dir: Path = Path("demo_logs")
    # policy -> base URL of that policy's router
    router_urls: dict[str, str] = field(default_factory=dict)
    router_procs: dict[str, subprocess.Popen] = field(default_factory=dict)
    client: httpx.AsyncClient | None = None


async def start_routers(state: PlaygroundState) -> None:
    """One router per policy, all pointing at the same replicas."""
    for policy in state.policies:
        proc, url = spawn_router(
            policy, state.replica_urls, state.model, state.tokenizer,
            state.block_size, state.min_match_tokens, state.log_dir, state.max_num_seqs,
        )
        state.router_procs[policy] = proc
        state.router_urls[policy] = url
    for policy, url in state.router_urls.items():
        if not await _wait_healthy(url):
            raise RuntimeError(
                f"router for {policy} never became healthy; see {state.log_dir}/router-{policy}.log "
                f"(for prefix_aware this usually means the tokenizer probe failed -- is `transformers` installed "
                f"and does --tokenizer match the served model?)"
            )


async def stop_routers(state: PlaygroundState) -> None:
    for proc in state.router_procs.values():
        proc.terminate()
    for proc in state.router_procs.values():
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=10)
    state.router_procs.clear()
    state.router_urls.clear()


# -- API -------------------------------------------------------------------------


class SendRequest(BaseModel):
    policy: str
    system: str = ""
    user: str = Field(min_length=1)
    max_tokens: int = Field(default=16, ge=1, le=256)
    isolate: bool = True
    session_id: str | None = None


def create_app(state: PlaygroundState) -> FastAPI:
    owns_client = state.client is None

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if state.client is None:
            state.client = httpx.AsyncClient()
        yield
        if owns_client and state.client is not None:
            await state.client.aclose()

    app = FastAPI(title="SwiftServe playground", lifespan=lifespan)

    @app.get("/")
    async def index():
        return FileResponse(UI_PATH, media_type="text/html")

    @app.get("/api/config")
    async def config():
        return {
            "simulated": state.simulated,
            "model": state.model,
            "tokenizer": state.tokenizer or "(byte-chunk fallback)",
            "policies": state.policies,
            "replicas": state.replica_urls,
            "block_size": state.block_size,
            "presets": [{"id": pid, "name": p["name"]} for pid, p in _PRESETS.items()],
            "questions": SUGGESTED_QUESTIONS,
        }

    @app.get("/api/preset")
    async def preset(id: str, tokens: int = 2000):
        if id not in _PRESETS:
            raise HTTPException(status_code=404, detail=f"unknown preset {id!r}")
        tokens = max(0, min(tokens, 6000))
        return {"id": id, "tokens_target": tokens, "system": make_system_prompt(id, tokens)}

    @app.post("/api/send")
    async def send(req: SendRequest):
        router_url = state.router_urls.get(req.policy)
        if router_url is None:
            return JSONResponse(status_code=400, content={"error": f"unknown policy {req.policy!r}"})
        assert state.client is not None

        messages: list[dict] = []
        if req.system:
            system = f"[ns:{req.policy}] {req.system}" if req.isolate else req.system
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": req.user})
        session_id = req.session_id or f"pg-{uuid.uuid4().hex[:8]}"

        result = await _stream_chat_turn(
            state.client, router_url, state.model, session_id, messages, req.max_tokens
        )
        if result.get("error"):
            return JSONResponse(status_code=502, content={"error": f"router unreachable: {result['error']}"})
        if result["status"] != 200:
            return JSONResponse(
                status_code=502, content={"error": f"router returned HTTP {result['status']}", "policy": req.policy}
            )

        prompt = result["prompt_tokens"] or 0
        cached = result["cached_tokens"] or 0
        predicted = result["predicted_cached_tokens"]
        return {
            "policy": req.policy,
            "session_id": session_id,
            "replica": result["replica"],
            "router_session_hit": result["cache_hit"] == "true",
            "ttft_ms": result["ttft_ms"],
            "e2e_ms": result["e2e_ms"],
            "prompt_tokens": prompt,
            "completion_tokens": result["completion_tokens"],
            "cached_tokens": cached,
            "predicted_cached_tokens": predicted,
            "prediction_error_tokens": abs(predicted - cached),
            "cache_ratio": (cached / prompt) if prompt else None,
            "content": result["content"],
        }

    @app.get("/api/status")
    async def status():
        assert state.client is not None

        async def one(policy: str, url: str):
            try:
                resp = await state.client.get(f"{url}/status", timeout=3.0)  # type: ignore[union-attr]
                return policy, resp.json()
            except (httpx.HTTPError, ValueError) as exc:
                return policy, {"error": str(exc)}

        pairs = await asyncio.gather(*(one(p, u) for p, u in state.router_urls.items()))
        return dict(pairs)

    @app.post("/api/reset")
    async def reset():
        """Forget every router's index and clear replica caches."""
        await stop_routers(state)
        failed = await reset_replica_caches(state.replica_urls)
        await start_routers(state)
        return {"ok": True, "replica_cache_reset_failed": failed}

    return app


# -- entry point -------------------------------------------------------------------


async def amain(args: argparse.Namespace) -> int:
    log_dir = Path(args.log_dir)
    policies = [p.strip() for p in args.policies.split(",") if p.strip()]
    unknown = [p for p in policies if p not in POLICIES]
    if unknown:
        print(f"ERROR: unknown policies {unknown}; choose from {list(POLICIES)}", file=sys.stderr)
        return 2

    simulated = not args.replicas
    replica_procs: list[subprocess.Popen] = []
    if simulated:
        print(f"Starting {args.num_replicas} fake vLLM replicas (kv_blocks={args.kv_blocks}) ...")
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

    state = PlaygroundState(
        model=args.model, tokenizer=args.tokenizer, policies=policies, replica_urls=replica_urls,
        simulated=simulated, block_size=args.block_size, min_match_tokens=args.min_match_tokens,
        max_num_seqs=args.max_num_seqs, log_dir=log_dir,
    )
    try:
        print(f"Starting {len(policies)} routers ({', '.join(policies)}) ...")
        await start_routers(state)
        server = uvicorn.Server(uvicorn.Config(create_app(state), host=args.host, port=args.port, log_level="warning"))
        label = "SIMULATED (fake replicas)" if simulated else "REAL replicas"
        print(f"\nPlayground ready [{label}]  ->  http://{args.host}:{args.port}\n(Ctrl+C to stop)\n")
        await server.serve()
        return 0
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    finally:
        await stop_routers(state)
        for proc in replica_procs:
            proc.terminate()
        for proc in replica_procs:
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=10)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--replicas", default="", help="comma-separated replica base URLs. Omit for fake local replicas.")
    parser.add_argument("--policies", default=",".join(DEFAULT_POLICIES))
    parser.add_argument("--model", default="demo-model")
    parser.add_argument("--tokenizer", default="",
                        help="model id for real tokenization (needs `transformers`); must match the served model")
    parser.add_argument("--block-size", type=int, default=16, help="must match the replicas' vLLM --block-size")
    parser.add_argument("--min-match-tokens", type=int, default=256)
    parser.add_argument("--max-num-seqs", type=int, default=32, help="must equal the replicas' vLLM --max-num-seqs")
    parser.add_argument("--num-replicas", type=int, default=4, help="fake-replica mode only")
    parser.add_argument("--kv-blocks", type=int, default=375, help="fake-replica cache capacity in blocks")
    parser.add_argument("--prefill-ms-per-token", type=float, default=0.2, help="fake-replica mode only")
    parser.add_argument("--decode-ms-per-token", type=float, default=5.0, help="fake-replica mode only")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--log-dir", default="demo_logs")
    args = parser.parse_args()
    with contextlib.suppress(KeyboardInterrupt):
        raise SystemExit(asyncio.run(amain(args)))


if __name__ == "__main__":
    main()
