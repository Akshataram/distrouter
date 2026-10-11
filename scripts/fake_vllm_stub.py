"""A minimal, honestly-labeled stand-in for a real vLLM server.

This is NOT vLLM and does NOT run a real model -- it exists so the rest of
the distributed system (router, sidecars, circuit breaking, chaos
injection, admission control, and now prefix-cache-aware routing) can be
exercised over real HTTP, real TCP sockets, and real separate OS processes
without requiring a GPU. Every place this project needs real inference
results (the benchmark numbers, the cache-hit findings) uses real vLLM on a
real GPU (see DEPLOYMENT.md) -- this stub proves the *routing, resilience,
and chaos-engineering* machinery works, which does not depend on what is
actually generating tokens.

## What is faithfully simulated, and what is not

**Faithful** (the mechanics the router's correctness depends on):

- **Block-level prefix caching.** Messages are rendered through a chat
  template, tokenized, chopped into fixed-size blocks, and chain-hashed --
  the same construction `swiftserve/prefix_index.py` uses, which is the
  same construction vLLM uses. A request's `cached_tokens` is the length of
  the leading run of blocks this process already holds. Prefix-only, full
  blocks only, LRU eviction at a configurable capacity. A cache hit here
  depends on the same things it depends on in vLLM: identical tokens from
  position zero, and the blocks not having been evicted since.
- **The `usage` report**, including `prompt_tokens_details.cached_tokens`,
  so the benchmark's `true_cache_ratio` measures something real.
- **`vllm:prefix_cache_hits` / `vllm:prefix_cache_queries`** on /metrics,
  so the router's scraper has real counters to read.
- **SSE streaming shape**, so TTFT and TPOT are measured from a real
  token-by-token stream rather than one blob.

**Not faithful** (and must not be presented as such):

- There is no model. Replies are canned text; their *content* is
  meaningless.
- Timing is a linear cost model (`--prefill-ms-per-token`,
  `--decode-ms-per-token`), not a GPU. It reproduces the *shape* of the
  cost -- uncached prefill is proportional to uncached tokens, decode is
  proportional to output length -- but a real T4 has batching effects,
  memory-bandwidth limits and kernel launch overheads this cannot show.
  Both default to 0.0 (instant), so timing simulation is opt-in and never
  silently fabricates latency numbers.
- Tokenization defaults to the byte-chunk fallback, whose block boundaries
  do not match any real tokenizer's. Pass `--tokenizer <model-id>` to use
  the real one and have the stub's blocks line up with a router configured
  the same way.

The honest use for the timing simulation: **rehearsing and smoke-testing
the demo end to end with no GPU**, and verifying the router's predicted
cached tokens against a ground truth that is actually computed rather than
assumed. Numbers produced this way are labeled as simulated everywhere
they appear; the real result comes from real vLLM.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import OrderedDict
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse

# Running this file directly only puts scripts/ on sys.path, not the repo
# root, so the swiftserve imports below would fail. Same fix as benchmark.py.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from swiftserve.prefix_index import block_hashes  # noqa: E402
from swiftserve.tokenization import build_tokenizer  # noqa: E402

app = FastAPI(title="Fake vLLM stub (not a real model -- see module docstring)")


class SimulatedKVCache:
    """LRU set of block hashes, standing in for a paged KV cache.

    Only block *identity* is tracked, never any tensor -- which is all the
    router's prefix matching depends on. `capacity_blocks=0` means
    unlimited (nothing ever evicts), which is the configuration that makes
    every policy tie; see RESEARCH_PLAN_V2.md section 4.3 on why a demo
    needs real cache pressure to show anything."""

    def __init__(self, capacity_blocks: int = 0):
        self.capacity_blocks = capacity_blocks
        self._blocks: OrderedDict[bytes, None] = OrderedDict()
        self.hits = 0
        self.queries = 0

    def lookup_and_store(self, hashes: list[bytes]) -> int:
        """Returns the number of leading blocks already cached, then stores
        all of them (refreshing LRU order), evicting past capacity.

        The walk stops at the first miss because a prefix-cache hit is
        prefix-only: holding block 7 without blocks 0-6 is worth nothing."""
        matched = 0
        for h in hashes:
            if h in self._blocks:
                matched += 1
            else:
                break

        for h in hashes:
            self._blocks[h] = None
            self._blocks.move_to_end(h)
        while self.capacity_blocks and len(self._blocks) > self.capacity_blocks:
            self._blocks.popitem(last=False)

        # vLLM counts these per block queried, not per request.
        self.queries += len(hashes)
        self.hits += matched
        return matched

    @property
    def size(self) -> int:
        return len(self._blocks)

    def clear(self) -> None:
        self._blocks.clear()

    def status(self) -> dict:
        return {
            "cached_blocks": len(self._blocks),
            "capacity_blocks": self.capacity_blocks or None,
            "prefix_cache_hits": self.hits,
            "prefix_cache_queries": self.queries,
        }


class StubConfig:
    def __init__(self) -> None:
        self.tag = "fake-vllm"
        self.block_size = 16
        self.prefill_ms_per_token = 0.0
        self.decode_ms_per_token = 0.0
        self.tokenizer = build_tokenizer("fake-model", "")
        self.cache = SimulatedKVCache()
        self.running = 0


CONFIG = StubConfig()


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/metrics")
async def metrics():
    """Prometheus text format, with the gauge/counter families
    swiftserve/metrics_scraper.py actually parses."""
    status = CONFIG.cache.status()
    return PlainTextResponse(
        f"vllm:num_requests_running {CONFIG.running}\n"
        f"vllm:num_requests_waiting 0\n"
        f"vllm:gpu_cache_usage_perc {_cache_usage():.4f}\n"
        f"vllm:prefix_cache_hits_total {status['prefix_cache_hits']}\n"
        f"vllm:prefix_cache_queries_total {status['prefix_cache_queries']}\n"
    )


def _cache_usage() -> float:
    cap = CONFIG.cache.capacity_blocks
    if not cap:
        return 0.0
    return min(CONFIG.cache.size / cap, 1.0)


@app.get("/cache_status")
async def cache_status():
    """Not a vLLM endpoint -- a stub-only window into the simulated cache,
    so a demo can show the ground truth the router is trying to predict."""
    return {"tag": CONFIG.tag, "block_size": CONFIG.block_size, **CONFIG.cache.status()}


@app.post("/cache_reset")
async def cache_reset():
    """Stub-only: drop the simulated cache, standing in for a replica
    restart (whose real cache would also be empty)."""
    CONFIG.cache.clear()
    return {"status": "cleared"}


def _measure(payload: dict) -> tuple[int, int, int]:
    """Returns (prompt_tokens, cached_tokens, completion_tokens)."""
    messages = payload.get("messages") or []
    tokens = CONFIG.tokenizer.encode_chat(messages)
    hashes = block_hashes(tokens, CONFIG.block_size, namespace=payload.get("model", ""))
    matched_blocks = CONFIG.cache.lookup_and_store(hashes)
    completion_tokens = int(payload.get("max_tokens") or 16)
    return len(tokens), matched_blocks * CONFIG.block_size, completion_tokens


def _usage(prompt_tokens: int, cached_tokens: int, completion_tokens: int) -> dict:
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
        "prompt_tokens_details": {"cached_tokens": cached_tokens},
    }


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    payload = await request.json()
    prompt_tokens, cached_tokens, completion_tokens = _measure(payload)

    # The one number that makes prefix caching visible: only the *uncached*
    # part of the prompt is prefilled.
    prefill_ms = (prompt_tokens - cached_tokens) * CONFIG.prefill_ms_per_token
    decode_ms_each = CONFIG.decode_ms_per_token

    CONFIG.running += 1
    try:
        if payload.get("stream"):
            return StreamingResponse(
                _event_stream(payload, prompt_tokens, cached_tokens, completion_tokens, prefill_ms, decode_ms_each),
                media_type="text/event-stream",
                headers={"X-Fake-Vllm-Cached-Tokens": str(cached_tokens)},
            )

        if prefill_ms:
            await asyncio.sleep(prefill_ms / 1000.0)
        if decode_ms_each:
            await asyncio.sleep(decode_ms_each * completion_tokens / 1000.0)
        return JSONResponse(
            {
                "id": f"chatcmpl-{CONFIG.tag}",
                "object": "chat.completion",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": f"[{CONFIG.tag}] simulated reply"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": _usage(prompt_tokens, cached_tokens, completion_tokens),
            },
            headers={"X-Fake-Vllm-Cached-Tokens": str(cached_tokens)},
        )
    finally:
        CONFIG.running -= 1


async def _event_stream(
    payload: dict, prompt_tokens: int, cached_tokens: int, completion_tokens: int,
    prefill_ms: float, decode_ms_each: float,
):
    """OpenAI-compatible SSE. The prefill sleep happens *before* the first
    content chunk, so a client's measured TTFT reflects the cache hit --
    which is the entire quantity this project is about."""
    if prefill_ms:
        await asyncio.sleep(prefill_ms / 1000.0)

    for i in range(completion_tokens):
        if decode_ms_each:
            await asyncio.sleep(decode_ms_each / 1000.0)
        chunk = {
            "id": f"chatcmpl-{CONFIG.tag}",
            "object": "chat.completion.chunk",
            "choices": [{"index": 0, "delta": {"content": f"tok{i} "}}],
        }
        yield f"data: {json.dumps(chunk)}\n\n".encode()

    # Final usage chunk, as vLLM sends when stream_options.include_usage is set.
    if payload.get("stream_options", {}).get("include_usage", True):
        yield (
            "data: "
            + json.dumps(
                {
                    "id": f"chatcmpl-{CONFIG.tag}",
                    "object": "chat.completion.chunk",
                    "choices": [],
                    "usage": _usage(prompt_tokens, cached_tokens, completion_tokens),
                }
            )
            + "\n\n"
        ).encode()
    yield b"data: [DONE]\n\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--tag", default="fake-vllm", help="included in each reply so tests can tell backends apart")
    parser.add_argument("--block-size", type=int, default=16, help="must match the router's SWIFTSERVE_BLOCK_SIZE")
    parser.add_argument(
        "--kv-blocks", type=int, default=0,
        help="simulated KV cache capacity in blocks (0 = unlimited). Set this to create real cache "
             "pressure -- with an unlimited cache nothing ever evicts and every routing policy ties.",
    )
    parser.add_argument(
        "--prefill-ms-per-token", type=float, default=0.0,
        help="simulated prefill cost per UNCACHED prompt token (0 = instant). ~0.2 is T4-ish for a 3B model, "
             "but measure your own rather than trusting this number.",
    )
    parser.add_argument(
        "--decode-ms-per-token", type=float, default=0.0,
        help="simulated per-output-token decode cost (0 = instant). ~30 is T4-ish for a 3B model.",
    )
    parser.add_argument(
        "--tokenizer", default="",
        help="model id for real tokenization (needs `transformers`); empty uses the byte-chunk fallback. "
             "Must match the router's SWIFTSERVE_TOKENIZER or their block hashes will never agree.",
    )
    args = parser.parse_args()

    os.environ["FAKE_VLLM_TAG"] = args.tag
    CONFIG.tag = args.tag
    CONFIG.block_size = args.block_size
    CONFIG.prefill_ms_per_token = args.prefill_ms_per_token
    CONFIG.decode_ms_per_token = args.decode_ms_per_token
    CONFIG.tokenizer = build_tokenizer("fake-model", args.tokenizer)
    CONFIG.cache = SimulatedKVCache(capacity_blocks=args.kv_blocks)

    print(
        f"[fake-vllm {args.tag}] NOT a real model. block_size={args.block_size} "
        f"kv_blocks={args.kv_blocks or 'unlimited'} "
        f"prefill={args.prefill_ms_per_token}ms/tok decode={args.decode_ms_per_token}ms/tok "
        f"tokenizer={CONFIG.tokenizer.name}",
        flush=True,
    )
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
