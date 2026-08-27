"""Replica sidecar: runs next to a real vLLM process on a GPU node (a
Colab or Kaggle notebook, in this project's real deployment) and proxies
ordinary traffic straight through, while exposing authenticated `/chaos/*`
endpoints so a chaos scenario can inject real faults into *this specific
node* over the same public tunnel already carrying real traffic -- no SSH
or notebook access needed mid-demo.

Two fault classes:
  - Soft faults (partition / latency / error-rate): pure request-handling
    behavior changes here in the sidecar; the real vLLM process underneath
    is untouched and keeps serving other traffic normally.
  - Hard fault (kill / restart): only available when the sidecar was
    started with --supervise "<vllm launch command>", in which case it
    owns the vLLM process directly and can SIGKILL + relaunch it -- the
    closest thing to a real crash a chaos test can safely trigger on
    borrowed free-tier GPU time.

SwiftServe's own router talks to this sidecar's port exactly as if it were
vLLM itself; only the chaos scenario runner needs to know the sidecar is
there at all.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import random
import shlex
import signal
import subprocess
from contextlib import asynccontextmanager
from dataclasses import dataclass

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import StreamingResponse

logger = logging.getLogger("swiftserve.replica_sidecar")

_HOP_BY_HOP_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "content-length", "content-encoding",
}


@dataclass
class ChaosState:
    partitioned: bool = False
    extra_latency_ms: float = 0.0
    error_rate: float = 0.0

    def status(self) -> dict:
        return {
            "partitioned": self.partitioned,
            "extra_latency_ms": self.extra_latency_ms,
            "error_rate": self.error_rate,
        }


class ProcessSupervisor:
    """Owns the real vLLM subprocess only when launched with --supervise,
    so /chaos/kill and /chaos/restart have something to act on. A sidecar
    started without --supervise still proxies fine -- there is just no
    process here to hard-kill (the earlier torchaudio-mismatch bug found
    while first building this project is exactly the kind of real crash
    this exists to reproduce on demand instead of by accident)."""

    def __init__(self, launch_cmd: list[str] | None):
        self.launch_cmd = launch_cmd
        self._proc: subprocess.Popen | None = None

    @property
    def supervised(self) -> bool:
        return self.launch_cmd is not None

    def start(self) -> None:
        if not self.supervised:
            return
        self._proc = subprocess.Popen(self.launch_cmd)
        logger.info("supervised process started: pid=%s cmd=%s", self._proc.pid, self.launch_cmd)

    def is_alive(self) -> bool | None:
        if not self.supervised:
            return None  # sidecar has no process to report on; check upstream /health instead
        return self._proc is not None and self._proc.poll() is None

    def kill(self) -> dict:
        if not self.supervised:
            raise HTTPException(status_code=400, detail="sidecar was not started with --supervise; nothing to kill")
        if self._proc is None or self._proc.poll() is not None:
            return {"already_dead": True}
        pid = self._proc.pid
        self._proc.send_signal(signal.SIGKILL)
        self._proc.wait(timeout=10)
        return {"killed": True, "pid": pid}

    def restart(self) -> dict:
        if not self.supervised:
            raise HTTPException(status_code=400, detail="sidecar was not started with --supervise; nothing to restart")
        if self._proc is not None and self._proc.poll() is None:
            self._proc.send_signal(signal.SIGKILL)
            self._proc.wait(timeout=10)
        self.start()
        return {"restarted": True, "pid": self._proc.pid}


def create_app(
    upstream_url: str,
    admin_token: str | None,
    supervisor: ProcessSupervisor | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> FastAPI:
    """``http_client``, when passed, is used as-is instead of the sidecar
    creating (and later closing) its own -- tests use this to point the
    sidecar at an in-process fake upstream via ASGITransport, exactly like
    swiftserve.app's own smoke tests do for the router itself."""
    chaos = ChaosState()
    supervisor = supervisor if supervisor is not None else ProcessSupervisor(None)
    upstream_url = upstream_url.rstrip("/")
    owns_client = http_client is None
    state: dict[str, httpx.AsyncClient] = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        supervisor.start()
        state["client"] = http_client if http_client is not None else httpx.AsyncClient()
        yield
        if owns_client:
            await state["client"].aclose()

    app = FastAPI(title="SwiftServe Replica Sidecar", lifespan=lifespan)

    def _require_admin(x_chaos_token: str | None) -> None:
        if admin_token and x_chaos_token != admin_token:
            raise HTTPException(status_code=403, detail="invalid or missing X-Chaos-Token")

    @app.get("/chaos/status")
    async def chaos_status():
        return {"chaos": chaos.status(), "supervised": supervisor.supervised, "process_alive": supervisor.is_alive()}

    @app.post("/chaos/partition")
    async def chaos_partition(enabled: bool = True, x_chaos_token: str | None = Header(default=None)):
        _require_admin(x_chaos_token)
        chaos.partitioned = enabled
        return chaos.status()

    @app.post("/chaos/latency")
    async def chaos_latency(extra_ms: float = 0.0, x_chaos_token: str | None = Header(default=None)):
        _require_admin(x_chaos_token)
        chaos.extra_latency_ms = max(0.0, extra_ms)
        return chaos.status()

    @app.post("/chaos/error-rate")
    async def chaos_error_rate(rate: float = 0.0, x_chaos_token: str | None = Header(default=None)):
        _require_admin(x_chaos_token)
        chaos.error_rate = min(1.0, max(0.0, rate))
        return chaos.status()

    @app.post("/chaos/reset")
    async def chaos_reset(x_chaos_token: str | None = Header(default=None)):
        _require_admin(x_chaos_token)
        chaos.partitioned = False
        chaos.extra_latency_ms = 0.0
        chaos.error_rate = 0.0
        return chaos.status()

    @app.post("/chaos/kill")
    async def chaos_kill(x_chaos_token: str | None = Header(default=None)):
        _require_admin(x_chaos_token)
        return supervisor.kill()

    @app.post("/chaos/restart")
    async def chaos_restart(x_chaos_token: str | None = Header(default=None)):
        _require_admin(x_chaos_token)
        return supervisor.restart()

    @app.api_route("/{full_path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
    async def proxy_passthrough(full_path: str, request: Request):
        if chaos.partitioned:
            raise HTTPException(status_code=503, detail="chaos: node partitioned")
        if chaos.error_rate and random.random() < chaos.error_rate:
            raise HTTPException(status_code=500, detail="chaos: injected error")
        if chaos.extra_latency_ms:
            await asyncio.sleep(chaos.extra_latency_ms / 1000.0)

        body = await request.body()
        forward_headers = {
            k: v for k, v in request.headers.items() if k.lower() not in {"host", "content-length"}
        }
        client = state["client"]
        upstream_req = client.build_request(
            request.method,
            f"{upstream_url}/{full_path}",
            content=body,
            headers=forward_headers,
            params=list(request.query_params.multi_items()),
        )
        try:
            upstream_resp = await client.send(upstream_req, stream=True)
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"upstream unreachable: {exc}") from exc

        response_headers = {
            k: v for k, v in upstream_resp.headers.items() if k.lower() not in _HOP_BY_HOP_HEADERS
        }

        async def body_iter():
            try:
                async for chunk in upstream_resp.aiter_bytes():
                    yield chunk
            finally:
                await upstream_resp.aclose()

        return StreamingResponse(
            body_iter(),
            status_code=upstream_resp.status_code,
            headers=response_headers,
            media_type=upstream_resp.headers.get("content-type"),
        )

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--upstream-url", default=os.environ.get("SIDECAR_UPSTREAM_URL", "http://localhost:8000"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("SIDECAR_PORT", "9000")))
    parser.add_argument("--admin-token", default=os.environ.get("SIDECAR_ADMIN_TOKEN"))
    parser.add_argument(
        "--supervise",
        default=os.environ.get("SIDECAR_SUPERVISE_CMD"),
        help="vLLM launch command to own and hard-kill/restart, e.g. "
        "'python -m vllm.entrypoints.openai.api_server --model Qwen/Qwen2.5-0.5B-Instruct --port 8000'",
    )
    args = parser.parse_args()

    if not args.admin_token:
        logger.warning(
            "no --admin-token / SIDECAR_ADMIN_TOKEN set: /chaos/* endpoints are unauthenticated. "
            "Fine for a private demo, not for anything exposed beyond your own tunnel URL."
        )

    import uvicorn

    supervisor = ProcessSupervisor(shlex.split(args.supervise) if args.supervise else None)
    app = create_app(args.upstream_url, args.admin_token, supervisor)
    uvicorn.run(app, host="0.0.0.0", port=args.port)


if __name__ == "__main__":
    main()
