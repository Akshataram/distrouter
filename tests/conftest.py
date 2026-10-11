"""Shared fixtures.

`router_env` rebuilds the router module's globals (replicas, policy, event bus,
telemetry, admission, upstream HTTP client) for one test and restores them
afterwards, so tests that exercise the real `/v1/chat/completions` path do not
leak state into each other. The upstream "replicas" are httpx MockTransport
handlers -- MOCKED, not real inference servers -- which is exactly what lets the
tests assert on attribution and timing without a GPU.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx
import pytest

import swiftserve.app as app_module
from swiftserve.events import EventBus
from swiftserve.policy import RoundRobinPolicy
from swiftserve.resilience import AdmissionController
from swiftserve.state import ReplicaState
from swiftserve.telemetry import Telemetry


@dataclass
class RouterEnv:
    replicas: list
    bus: EventBus
    telemetry: Telemetry

    def set_upstream(self, handler) -> None:
        """Install a MOCK upstream: `handler(request)` stands in for every replica."""
        app_module._http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app_module.app), base_url="http://router")


@pytest.fixture
def router_env(monkeypatch):
    replicas = [ReplicaState(i, f"http://r{i}", cache_ttl_s=600, assumed_max_batch_size=32) for i in range(3)]
    bus = EventBus()
    telemetry = Telemetry(bus)
    monkeypatch.setattr(app_module, "replicas", replicas)
    monkeypatch.setattr(app_module, "policy", RoundRobinPolicy())
    monkeypatch.setattr(app_module, "bus", bus)
    monkeypatch.setattr(app_module, "telemetry", telemetry)
    monkeypatch.setattr(app_module, "admission", AdmissionController(max_in_flight=256))
    monkeypatch.setattr(app_module, "_http_client", None)
    # fast retries so failure tests do not sleep for real
    object.__setattr__(app_module.settings, "proxy_retry_base_delay_s", 0.001)
    yield RouterEnv(replicas, bus, telemetry)
    object.__setattr__(app_module.settings, "proxy_retry_base_delay_s", 0.1)
