"""Runtime configuration for the SwiftServe control plane.

Everything here is environment-driven so the same code deploys against
whatever real vLLM replicas you stand up (bare metal, Docker Compose, or a
managed GPU cloud) -- SwiftServe never talks to a model directly, only to
the OpenAI-compatible HTTP endpoint each vLLM replica exposes.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _parse_replica_urls(raw: str) -> list[str]:
    urls = [u.strip().rstrip("/") for u in raw.split(",") if u.strip()]
    if not urls:
        raise ValueError(
            "SWIFTSERVE_REPLICAS must be a comma-separated list of vLLM base "
            "URLs, e.g. 'http://gpu1:8000,http://gpu2:8000,http://gpu3:8000'"
        )
    return urls


@dataclass(frozen=True)
class Settings:
    model_name: str = field(default_factory=lambda: os.environ.get("SWIFTSERVE_MODEL", "Qwen/Qwen2.5-7B-Instruct"))
    replica_urls: list[str] = field(
        default_factory=lambda: _parse_replica_urls(
            os.environ.get("SWIFTSERVE_REPLICAS", "http://localhost:8001,http://localhost:8002,http://localhost:8003")
        )
    )
    policy: str = field(default_factory=lambda: os.environ.get("SWIFTSERVE_POLICY", "swiftserve"))
    default_sla_ms: float = field(default_factory=lambda: float(os.environ.get("SWIFTSERVE_DEFAULT_SLA_MS", "3000")))
    cache_affinity_ttl_s: float = field(default_factory=lambda: float(os.environ.get("SWIFTSERVE_CACHE_TTL_S", "600")))
    metrics_scrape_interval_s: float = field(default_factory=lambda: float(os.environ.get("SWIFTSERVE_SCRAPE_INTERVAL_S", "2")))
    request_timeout_s: float = field(default_factory=lambda: float(os.environ.get("SWIFTSERVE_REQUEST_TIMEOUT_S", "120")))
    health_check_interval_s: float = field(default_factory=lambda: float(os.environ.get("SWIFTSERVE_HEALTH_INTERVAL_S", "5")))

    circuit_failure_threshold: int = field(default_factory=lambda: int(os.environ.get("SWIFTSERVE_CIRCUIT_FAILURE_THRESHOLD", "5")))
    circuit_reset_timeout_s: float = field(default_factory=lambda: float(os.environ.get("SWIFTSERVE_CIRCUIT_RESET_S", "10")))
    circuit_max_reset_timeout_s: float = field(default_factory=lambda: float(os.environ.get("SWIFTSERVE_CIRCUIT_MAX_RESET_S", "120")))
    admission_max_in_flight: int = field(default_factory=lambda: int(os.environ.get("SWIFTSERVE_MAX_IN_FLIGHT", "256")))
    proxy_max_retries: int = field(default_factory=lambda: int(os.environ.get("SWIFTSERVE_PROXY_MAX_RETRIES", "2")))
    proxy_retry_base_delay_s: float = field(default_factory=lambda: float(os.environ.get("SWIFTSERVE_PROXY_RETRY_BASE_DELAY_S", "0.1")))


settings = Settings()
