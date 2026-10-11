"""Run a real uvicorn server inside the test's own event loop, on an ephemeral
port. Real sockets matter for the things ASGITransport cannot show: genuinely
incremental streaming (SSE), and what happens when a client disconnects."""

from __future__ import annotations

import asyncio

import uvicorn


class LiveServer:
    def __init__(self, app, lifespan: str = "off"):
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan=lifespan))
        self.task: asyncio.Task | None = None
        self.port = 0

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def __aenter__(self) -> LiveServer:
        self.task = asyncio.create_task(self.server.serve())
        for _ in range(500):
            if self.server.started:
                break
            await asyncio.sleep(0.01)
        assert self.server.started, "uvicorn did not start"
        self.port = self.server.servers[0].sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc) -> None:
        self.server.should_exit = True
        assert self.task is not None
        await asyncio.wait_for(self.task, timeout=10)
