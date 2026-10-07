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
    assumed_max_batch_size: int = field(default_factory=lambda: int(os.environ.get("SWIFTSERVE_ASSUMED_MAX_BATCH_SIZE", "1")))
    # Unset (default) means the router's own API is unauthenticated -- fine
    # for a private demo, not for anything exposed beyond a trusted network.
    # Mirrors replica_sidecar.py's SIDECAR_ADMIN_TOKEN / X-Chaos-Token pattern.
    api_token: str | None = field(default_factory=lambda: os.environ.get("SWIFTSERVE_API_TOKEN") or None)
    # 0.0 (default) = off: the routing fallback ignores prefix length until
    # a deployer sets this from their own replicas' measured prefill
    # throughput. Deliberately a configured constant, not self-calibrated
    # like assumed_max_batch_size -- isolating "extra time from a cold
    # prefix" from "extra time from current load" isn't observable from
    # data SwiftServe already collects. See ReplicaState.estimate_cold_start_penalty_ms.
    cold_start_ms_per_token: float = field(default_factory=lambda: float(os.environ.get("SWIFTSERVE_COLD_START_MS_PER_TOKEN", "0.0")))
    prefix_trie_max_depth: int = field(default_factory=lambda: int(os.environ.get("SWIFTSERVE_PREFIX_TRIE_MAX_DEPTH", "6")))

    # -- block-level prefix index (policy "prefix_aware") ----------------
    # Empty (default) = ByteChunkTokenizer, which needs no download but
    # does NOT reproduce real token/block boundaries, so its cache
    # predictions are systematically wrong. Set this to the served model id
    # (e.g. "Qwen/Qwen2.5-3B-Instruct", needs `pip install transformers`)
    # for predictions that line up with what the engine actually caches.
    tokenizer: str = field(default_factory=lambda: os.environ.get("SWIFTSERVE_TOKENIZER", ""))
    # MUST equal the replicas' vLLM `--block-size`. A router hashing
    # 16-token blocks against an engine caching 32-token blocks shares no
    # hashes at all, so every prediction silently becomes a miss.
    block_size: int = field(default_factory=lambda: int(os.environ.get("SWIFTSERVE_BLOCK_SIZE", "16")))
    # Per-replica cap on tracked blocks, imitating the engine's own LRU so
    # the index's belief decays roughly when the real cache does. vLLM logs
    # its real block count (`# GPU blocks:`) at startup -- set this to it.
    index_max_blocks: int = field(default_factory=lambda: int(os.environ.get("SWIFTSERVE_INDEX_MAX_BLOCKS", "20000")))
    # SGLang's cache_threshold: route on a match once this fraction of the
    # prompt's blocks are already warm there.
    cache_threshold: float = field(default_factory=lambda: float(os.environ.get("SWIFTSERVE_CACHE_THRESHOLD", "0.5")))
    # Absolute floor, OR'd with the ratio above: enough skipped prefill to
    # be worth distorting load-balancing for, regardless of prompt length.
    min_match_tokens: int = field(default_factory=lambda: int(os.environ.get("SWIFTSERVE_MIN_MATCH_TOKENS", "256")))


settings = Settings()
