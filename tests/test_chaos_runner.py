"""End-to-end chaos test against REAL separate OS processes: a real
SwiftServe router (uvicorn), two real chaos-capable sidecars each
supervising their own child process, and those two real child processes
answering real HTTP over real sockets -- actual ports, actual TCP
connections, actual SIGKILL on actual PIDs for the "kill" scenario.

The one thing that is not real is the model itself: this sandbox has no
GPU, so scripts/fake_vllm_stub.py stands in for real vLLM (see its
docstring). Everything upstream of "what generates the reply text" --
routing, the circuit breaker, admission control, chaos fault injection,
and process supervision -- runs for real here, as separate processes
talking over localhost sockets, not simulated in-process function calls.
The same test would pass unchanged pointed at real vLLM replicas; only
the reply content would differ.
"""

from __future__ import annotations

import contextlib
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from scripts.chaos_runner import run_scenario

REPO_ROOT = Path(__file__).resolve().parents[1]
ADMIN_TOKEN = "test-chaos-token"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_until_up(url: str, timeout_s: float) -> None:
    deadline = time.monotonic() + timeout_s
    last_exc: Exception | None = None
    while time.monotonic() < deadline:
        try:
            resp = httpx.get(url, timeout=1.0)
            if resp.status_code < 500:
                return
        except httpx.HTTPError as exc:
            last_exc = exc
        time.sleep(0.2)
    raise RuntimeError(f"{url} never came up within {timeout_s}s: {last_exc}")


@pytest.fixture
def real_two_node_cluster():
    """Real router + 2 real sidecars (each supervising its own real
    fake_vllm_stub child process) as actual OS processes on actual
    localhost ports. Yields (router_url, [sidecar_url, ...])."""
    log_dir = REPO_ROOT / ".pytest_chaos_logs"
    log_dir.mkdir(exist_ok=True)
    procs: list[tuple[subprocess.Popen, object]] = []
    sidecar_ports: list[int] = []

    def spawn(cmd: list[str], name: str, env_overrides: dict | None = None) -> subprocess.Popen:
        # Deliberately not a `with` block: this file must stay open for the
        # subprocess's whole lifetime (it's Popen's stdout target), well
        # past this function returning -- it's closed in the fixture's own
        # teardown below, alongside the process itself.
        log_file = open(log_dir / f"{name}.log", "w")  # noqa: SIM115
        popen = subprocess.Popen(
            cmd,
            cwd=REPO_ROOT,
            env={**os.environ, **(env_overrides or {})},
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )
        procs.append((popen, log_file))
        return popen

    try:
        for i in range(2):
            internal_vllm_port = _free_port()
            sidecar_port = _free_port()
            sidecar_ports.append(sidecar_port)
            supervise_cmd = (
                f"{sys.executable} {REPO_ROOT / 'scripts' / 'fake_vllm_stub.py'} "
                f"--port {internal_vllm_port} --tag r{i}"
            )
            spawn(
                [
                    sys.executable, "-m", "swiftserve.replica_sidecar",
                    "--upstream-url", f"http://127.0.0.1:{internal_vllm_port}",
                    "--port", str(sidecar_port),
                    "--admin-token", ADMIN_TOKEN,
                    "--supervise", supervise_cmd,
                ],
                name=f"sidecar-{i}",
            )

        for port in sidecar_ports:
            # Proxied through to the supervised fake-vllm child, so this
            # confirms the whole chain (sidecar -> its own real subprocess)
            # is actually up, not just that the sidecar process exists.
            _wait_until_up(f"http://127.0.0.1:{port}/health", timeout_s=20.0)

        router_port = _free_port()
        replicas_env = ",".join(f"http://127.0.0.1:{p}" for p in sidecar_ports)
        spawn(
            [sys.executable, "-m", "uvicorn", "swiftserve.app:app", "--host", "127.0.0.1", "--port", str(router_port)],
            name="router",
            env_overrides={
                "SWIFTSERVE_REPLICAS": replicas_env,
                "SWIFTSERVE_POLICY": "round_robin",
                "SWIFTSERVE_CIRCUIT_FAILURE_THRESHOLD": "2",
                "SWIFTSERVE_CIRCUIT_RESET_S": "0.5",
                "SWIFTSERVE_CIRCUIT_MAX_RESET_S": "0.5",
            },
        )
        router_url = f"http://127.0.0.1:{router_port}"
        _wait_until_up(f"{router_url}/healthz", timeout_s=15.0)

        sidecar_urls = [f"http://127.0.0.1:{p}" for p in sidecar_ports]
        yield router_url, sidecar_urls
    finally:
        for port in sidecar_ports:
            with contextlib.suppress(Exception):
                httpx.post(
                    f"http://127.0.0.1:{port}/chaos/kill", headers={"X-Chaos-Token": ADMIN_TOKEN}, timeout=3.0
                )
        for popen, log_file in procs:
            with contextlib.suppress(ProcessLookupError):
                popen.terminate()
            try:
                popen.wait(timeout=5)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError):
                    popen.kill()
                popen.wait(timeout=5)
            log_file.close()


@pytest.mark.asyncio
async def test_real_partition_trips_circuit_reroutes_and_recovers(real_two_node_cluster):
    router_url, sidecar_urls = real_two_node_cluster
    result = await run_scenario(
        router_url=router_url,
        sidecar_urls=sidecar_urls,
        target_replica=0,
        scenario="partition",
        admin_token=ADMIN_TOKEN,
        baseline_duration_s=1.0,
        fault_duration_s=3.0,
        recovery_timeout_s=10.0,
        request_interval_s=0.1,
    )
    assert result.fault_detected, result.summary()
    assert result.rerouted_successfully, result.summary()
    assert result.recovered, result.summary()
    assert result.requests_ok > 0


@pytest.mark.asyncio
async def test_real_process_kill_trips_circuit_reroutes_and_recovers(real_two_node_cluster):
    router_url, sidecar_urls = real_two_node_cluster
    result = await run_scenario(
        router_url=router_url,
        sidecar_urls=sidecar_urls,
        target_replica=1,
        scenario="kill",
        admin_token=ADMIN_TOKEN,
        baseline_duration_s=1.0,
        fault_duration_s=3.0,
        recovery_timeout_s=15.0,
        request_interval_s=0.1,
    )
    assert result.fault_detected, result.summary()
    assert result.rerouted_successfully, result.summary()
    assert result.recovered, result.summary()
