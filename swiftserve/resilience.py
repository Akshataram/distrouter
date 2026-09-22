"""Resilience layer: per-replica circuit breaking and global admission
control -- the difference between "one bad replica adds latency" and "one
bad replica takes down the whole cluster".

Neither mechanism talks to vLLM or HTTP directly; both are pure state
machines that swiftserve.app drives from the request lifecycle (dispatch,
success, failure) so they're trivially unit-testable without a real
replica.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class CircuitBreaker:
    """Trips after `failure_threshold` consecutive failures and stays OPEN
    for a cooldown that doubles on each repeat trip (capped at
    `max_reset_timeout_s`), then allows up to `half_open_max_probes`
    concurrent trial requests through before deciding CLOSED or OPEN again.

    Deliberately simple: a real breaker (e.g. one guarding a payments API)
    would rate-limit failures over a sliding window rather than counting
    consecutive ones, and would use a token-bucket for probes rather than a
    flat cap. Consecutive-failure counting is the right tradeoff here
    because vLLM replica failures are almost always binary (process is up
    and healthy, or it just died) rather than a background error rate.
    """

    failure_threshold: int = 5
    reset_timeout_s: float = 10.0
    max_reset_timeout_s: float = 120.0
    half_open_max_probes: int = 1

    _state: CircuitState = field(default=CircuitState.CLOSED, init=False)
    _consecutive_failures: int = field(default=0, init=False)
    _opened_at: float = field(default=0.0, init=False)
    _current_timeout_s: float = field(default=0.0, init=False)
    _half_open_in_flight: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        self._current_timeout_s = self.reset_timeout_s

    @property
    def state(self) -> CircuitState:
        """Read-only except for the time-based OPEN -> HALF_OPEN transition,
        which is bookkeeping (the clock ticking), not an action being taken."""
        if self._state is CircuitState.OPEN and time.monotonic() - self._opened_at >= self._current_timeout_s:
            self._state = CircuitState.HALF_OPEN
        return self._state

    def is_open(self) -> bool:
        return self.state is CircuitState.OPEN

    @property
    def half_open_in_flight(self) -> int:
        """How many half-open probe requests are currently outstanding for
        this replica. Used to spread probe traffic fairly across several
        simultaneously-recovering replicas (see app.py's probe selection)
        instead of always favoring whichever replica happens to sort
        first -- irrelevant (always 0) outside HALF_OPEN."""
        return self._half_open_in_flight

    def has_probe_capacity(self) -> bool:
        """Whether another half-open trial request may be dispatched right
        now. Irrelevant (always True) outside HALF_OPEN."""
        return self.state is not CircuitState.HALF_OPEN or self._half_open_in_flight < self.half_open_max_probes

    def mark_dispatched(self) -> None:
        """Call once, right after choosing this replica, before the
        upstream call is made."""
        if self.state is CircuitState.HALF_OPEN:
            self._half_open_in_flight += 1

    def record_success(self) -> None:
        if self._state is CircuitState.HALF_OPEN:
            self._half_open_in_flight = max(0, self._half_open_in_flight - 1)
        self._consecutive_failures = 0
        self._current_timeout_s = self.reset_timeout_s
        self._state = CircuitState.CLOSED

    def record_failure(self) -> None:
        if self._state is CircuitState.HALF_OPEN:
            self._half_open_in_flight = max(0, self._half_open_in_flight - 1)
            self._trip()
            return
        self._consecutive_failures += 1
        if self._consecutive_failures >= self.failure_threshold:
            self._trip()

    def _trip(self) -> None:
        # Only back off further on a *repeat* trip (failing again after a
        # half-open probe) -- the very first trip always uses the base
        # reset_timeout_s, not a pre-doubled one.
        if self._state is CircuitState.HALF_OPEN:
            self._current_timeout_s = min(self._current_timeout_s * 2, self.max_reset_timeout_s)
        self._state = CircuitState.OPEN
        self._opened_at = time.monotonic()

    def status(self) -> dict:
        return {
            "state": self.state.value,
            "consecutive_failures": self._consecutive_failures,
            "current_reset_timeout_s": round(self._current_timeout_s, 1),
        }


class AllReplicasUnavailableError(Exception):
    """Every replica is circuit-open or out of half-open probe capacity."""


class AdmissionRejectedError(Exception):
    """The cluster is at its global concurrency ceiling; fail fast instead
    of queueing an unbounded number of requests behind it."""


@dataclass
class AdmissionController:
    """Global backpressure valve. Without this, a client-side traffic spike
    just makes every in-flight request wait longer and longer -- the
    connection queue grows unbounded until the process runs out of memory
    or every request times out together. Rejecting the request that would
    exceed capacity, immediately and cheaply, keeps the requests already
    admitted fast and lets the caller retry or shed load intelligently.
    """

    max_in_flight: int

    _in_flight: int = field(default=0, init=False)

    def try_acquire(self) -> bool:
        if self._in_flight >= self.max_in_flight:
            return False
        self._in_flight += 1
        return True

    def release(self) -> None:
        self._in_flight = max(0, self._in_flight - 1)

    @property
    def in_flight(self) -> int:
        return self._in_flight

    def status(self) -> dict:
        return {"in_flight": self._in_flight, "max_in_flight": self.max_in_flight}
