"""Chaos scenario runner: orchestrates a real fault-injection test against
a live SwiftServe router and its replica sidecars, and reports whether the
system actually detected the fault, rerouted traffic around it, and
recovered once the fault cleared.

This is what exercises the circuit breaker and admission control against
real HTTP failures end-to-end, rather than only unit-testing the state
machine in isolation (see tests/test_resilience.py for that) -- it is the
difference between "the breaker class works" and "the deployed system
survives a replica dying".

Usage (against a real 2+-node deployment, router on one machine, each
replica fronted by its own chaos-capable sidecar -- see
swiftserve/replica_sidecar.py and DEPLOYMENT.md):

    python scripts/chaos_runner.py \\
        --router-url http://localhost:8000 \\
        --sidecar-urls http://gpu-node-0:9000,http://gpu-node-1:9000,http://gpu-node-2:9000 \\
        --target-replica 0 --scenario kill --admin-token "$SIDECAR_ADMIN_TOKEN" \\
        --output chaos_report.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field

import httpx


@dataclass
class TimelineEvent:
    t_s: float
    event: str
    detail: dict = field(default_factory=dict)


@dataclass
class ScenarioResult:
    scenario: str
    target_replica: int
    timeline: list[TimelineEvent]
    requests_ok: int
    requests_failed: int
    fault_detected: bool
    rerouted_successfully: bool
    recovered: bool
    mttr_s: float | None

    def summary(self) -> str:
        lines = [
            f"Scenario: {self.scenario} on replica {self.target_replica}",
            f"Requests: {self.requests_ok} ok, {self.requests_failed} failed (fault + recovery window)",
            f"Fault detected (circuit opened): {'yes' if self.fault_detected else 'no'}",
            f"Traffic rerouted to healthy replicas during fault: {'yes' if self.rerouted_successfully else 'no'}",
            "Recovered after fault cleared: "
            + ("yes" + (f" (MTTR {self.mttr_s:.1f}s)" if self.mttr_s is not None else "") if self.recovered else "no"),
        ]
        return "\n".join(lines)

    def to_json(self) -> dict:
        return {
            "scenario": self.scenario,
            "target_replica": self.target_replica,
            "requests_ok": self.requests_ok,
            "requests_failed": self.requests_failed,
            "fault_detected": self.fault_detected,
            "rerouted_successfully": self.rerouted_successfully,
            "recovered": self.recovered,
            "mttr_s": self.mttr_s,
            "timeline": [{"t_s": round(e.t_s, 2), "event": e.event, **e.detail} for e in self.timeline],
        }


def _admin_headers(admin_token: str | None) -> dict:
    return {"X-Chaos-Token": admin_token} if admin_token else {}


async def _inject_fault(client: httpx.AsyncClient, sidecar_url: str, scenario: str, admin_token: str | None) -> None:
    headers = _admin_headers(admin_token)
    if scenario == "partition":
        await client.post(f"{sidecar_url}/chaos/partition", params={"enabled": "true"}, headers=headers)
    elif scenario == "kill":
        await client.post(f"{sidecar_url}/chaos/kill", headers=headers)
    elif scenario == "latency":
        await client.post(f"{sidecar_url}/chaos/latency", params={"extra_ms": "5000"}, headers=headers)
    elif scenario == "error-rate":
        await client.post(f"{sidecar_url}/chaos/error-rate", params={"rate": "1.0"}, headers=headers)
    else:
        raise ValueError(f"unknown scenario: {scenario!r}")


async def _clear_fault(client: httpx.AsyncClient, sidecar_url: str, scenario: str, admin_token: str | None) -> None:
    headers = _admin_headers(admin_token)
    if scenario == "kill":
        await client.post(f"{sidecar_url}/chaos/restart", headers=headers)
    else:
        await client.post(f"{sidecar_url}/chaos/reset", headers=headers)


async def _one_request(client: httpx.AsyncClient, router_url: str, session_id: str) -> tuple[bool, int | None]:
    try:
        resp = await client.post(
            f"{router_url}/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "chaos probe"}]},
            headers={"X-Session-Id": session_id},
            timeout=10.0,
        )
        replica_header = resp.headers.get("x-swiftserve-replica")
        return resp.status_code < 500, (int(replica_header) if replica_header is not None else None)
    except httpx.HTTPError:
        return False, None


async def run_scenario(
    router_url: str,
    sidecar_urls: list[str],
    target_replica: int,
    scenario: str,
    admin_token: str | None = None,
    baseline_duration_s: float = 2.0,
    fault_duration_s: float = 10.0,
    recovery_timeout_s: float = 30.0,
    request_interval_s: float = 0.2,
    client: httpx.AsyncClient | None = None,
) -> ScenarioResult:
    """``client``, when passed, is used as-is (tests inject one whose
    `mounts` route straight into in-process ASGI apps instead of real
    sockets); otherwise a real network client is created and closed here."""
    if client is not None:
        return await _run_scenario(
            client, router_url, sidecar_urls, target_replica, scenario, admin_token,
            baseline_duration_s, fault_duration_s, recovery_timeout_s, request_interval_s,
        )
    async with httpx.AsyncClient() as owned_client:
        return await _run_scenario(
            owned_client, router_url, sidecar_urls, target_replica, scenario, admin_token,
            baseline_duration_s, fault_duration_s, recovery_timeout_s, request_interval_s,
        )


async def _run_scenario(
    client: httpx.AsyncClient,
    router_url: str,
    sidecar_urls: list[str],
    target_replica: int,
    scenario: str,
    admin_token: str | None,
    baseline_duration_s: float,
    fault_duration_s: float,
    recovery_timeout_s: float,
    request_interval_s: float,
) -> ScenarioResult:
    timeline: list[TimelineEvent] = []
    requests_ok = 0
    requests_failed = 0
    saw_other_replica_during_fault = False

    t0 = time.monotonic()

    async def probe_loop(duration_s: float, during_fault: bool) -> None:
        nonlocal requests_ok, requests_failed, saw_other_replica_during_fault
        deadline = time.monotonic() + duration_s
        while time.monotonic() < deadline:
            ok, replica_id = await _one_request(client, router_url, f"chaos-{uuid.uuid4().hex[:6]}")
            if ok:
                requests_ok += 1
                if during_fault and replica_id is not None and replica_id != target_replica:
                    saw_other_replica_during_fault = True
            else:
                requests_failed += 1
            await asyncio.sleep(request_interval_s)

    async def replica_circuit_state() -> str:
        status = (await client.get(f"{router_url}/status")).json()
        replica_status = next(r for r in status["replicas"] if r["replica_id"] == target_replica)
        return replica_status["circuit"]["state"]

    timeline.append(TimelineEvent(time.monotonic() - t0, "baseline_start"))
    await probe_loop(baseline_duration_s, during_fault=False)

    timeline.append(TimelineEvent(time.monotonic() - t0, "fault_injected", {"scenario": scenario, "replica": target_replica}))
    await _inject_fault(client, sidecar_urls[target_replica], scenario, admin_token)

    await probe_loop(fault_duration_s, during_fault=True)

    circuit_state_at_fault_end = await replica_circuit_state()
    fault_detected = circuit_state_at_fault_end in ("open", "half_open")
    timeline.append(
        TimelineEvent(time.monotonic() - t0, "fault_window_end", {"circuit_state": circuit_state_at_fault_end})
    )

    timeline.append(TimelineEvent(time.monotonic() - t0, "fault_cleared", {"scenario": scenario}))
    fault_cleared_at = time.monotonic()
    await _clear_fault(client, sidecar_urls[target_replica], scenario, admin_token)

    recovered = False
    mttr_s = None
    recovery_deadline = time.monotonic() + recovery_timeout_s
    while time.monotonic() < recovery_deadline:
        ok, _ = await _one_request(client, router_url, f"chaos-recover-{uuid.uuid4().hex[:6]}")
        if ok:
            requests_ok += 1
        else:
            requests_failed += 1
        if await replica_circuit_state() == "closed":
            recovered = True
            mttr_s = time.monotonic() - fault_cleared_at
            break
        await asyncio.sleep(request_interval_s)

    timeline.append(
        TimelineEvent(time.monotonic() - t0, "recovery_check_end", {"recovered": recovered, "mttr_s": mttr_s})
    )

    return ScenarioResult(
        scenario=scenario,
        target_replica=target_replica,
        timeline=timeline,
        requests_ok=requests_ok,
        requests_failed=requests_failed,
        fault_detected=fault_detected,
        rerouted_successfully=saw_other_replica_during_fault,
        recovered=recovered,
        mttr_s=mttr_s,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--router-url", default="http://localhost:8000")
    parser.add_argument(
        "--sidecar-urls", required=True,
        help="comma-separated sidecar URLs, one per replica, same order as SWIFTSERVE_REPLICAS",
    )
    parser.add_argument("--target-replica", type=int, default=0)
    parser.add_argument("--scenario", choices=["partition", "kill", "latency", "error-rate"], default="partition")
    parser.add_argument("--admin-token", default=None)
    parser.add_argument("--baseline-duration-s", type=float, default=2.0)
    parser.add_argument("--fault-duration-s", type=float, default=10.0)
    parser.add_argument("--recovery-timeout-s", type=float, default=30.0)
    parser.add_argument("--request-interval-s", type=float, default=0.2)
    parser.add_argument("--output", default=None, help="write the full JSON timeline report to this path")
    args = parser.parse_args()

    sidecar_urls = [u.strip().rstrip("/") for u in args.sidecar_urls.split(",")]
    result = asyncio.run(
        run_scenario(
            router_url=args.router_url.rstrip("/"),
            sidecar_urls=sidecar_urls,
            target_replica=args.target_replica,
            scenario=args.scenario,
            admin_token=args.admin_token,
            baseline_duration_s=args.baseline_duration_s,
            fault_duration_s=args.fault_duration_s,
            recovery_timeout_s=args.recovery_timeout_s,
            request_interval_s=args.request_interval_s,
        )
    )
    print(result.summary())
    if args.output:
        with open(args.output, "w") as f:
            json.dump(result.to_json(), f, indent=2)
        print(f"Full timeline written to {args.output}")


if __name__ == "__main__":
    main()
