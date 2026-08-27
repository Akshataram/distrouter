"""A minimal, honestly-labeled stand-in for a real vLLM server.

This is NOT vLLM and does NOT run a real model -- it exists so the rest of
the distributed system (router, sidecars, circuit breaking, chaos
injection, admission control) can be exercised over real HTTP, real TCP
sockets, and real separate OS processes without requiring a GPU. Every
place this project actually needs real inference results (the benchmark
numbers in README.md, the cache-hit-rate finding) uses real vLLM on a real
GPU (Colab/Kaggle T4, see DEPLOYMENT.md) -- this stub is only ever used to
prove the *routing, resilience, and chaos-engineering* machinery works
correctly, which does not depend on what's actually generating tokens.

Exposes just enough of vLLM's OpenAI-compatible surface for SwiftServe to
route against: `/health` and `/v1/chat/completions` (non-streaming). Each
reply is tagged with this process's own `--tag` so a test can confirm
which real backend process actually handled a given request.
"""

from __future__ import annotations

import argparse
import os

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI(title="Fake vLLM stub (not a real model -- see module docstring)")


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/metrics")
async def metrics():
    return "vllm:num_requests_running 0\nvllm:num_requests_waiting 0\nvllm:gpu_cache_usage_perc 0.0\n"


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    tag = os.environ.get("FAKE_VLLM_TAG", "fake-vllm")
    last_user_content = body["messages"][-1]["content"]
    return JSONResponse(
        {
            "id": f"chatcmpl-{tag}",
            "object": "chat.completion",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": f"[{tag}] echo: {last_user_content}"},
                    "finish_reason": "stop",
                }
            ],
        }
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--tag", default="fake-vllm", help="included in each reply so tests can tell backends apart")
    args = parser.parse_args()
    os.environ["FAKE_VLLM_TAG"] = args.tag
    uvicorn.run(app, host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
