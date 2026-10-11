"""Per-request telemetry: what the router actually did with each request.

Before this module the router measured exactly one latency, end to end, and
fed it into the replica's EWMA (`ReplicaState.record_completion_latency`) and
a Prometheus histogram. Time-to-first-token (TTFT) and inter-token latency
(ITL) existed only in `scripts/benchmark.py`, measured by a *client*. This
module adds them to the router itself, plus the per-request attribution a
live routing view needs:

- ``StreamTimer`` parses the upstream SSE stream as it passes through the
  proxy and timestamps content events, so TTFT/ITL are measured *at the
  router* (the vantage point that also made the routing decision).
- ``RequestRecord`` is one request's full story: what the policy selected,
  every dispatch attempt, which replica actually served it, timings, token
  counts, and the error if it failed.
- ``Telemetry`` owns the bounded set of recent records, the per-replica
  aggregates (counts, latency windows, throughput), and turns state changes
  into events on the shared `EventBus`.

Three replica notions are kept distinct on purpose:

1. **selected**  -- the replica the routing policy (or a half-open probe
   override) chose.
2. **dispatched** -- every replica an HTTP attempt was actually sent to, in
   order. Today the proxy only retries the *same* replica, so this equals the
   selected replica repeated; the field is a list so that a future failover
   would show up truthfully instead of being papered over.
3. **served**    -- the replica that produced the response. ``None`` unless
   the request really succeeded.

Everything here runs on the router's single asyncio loop and never awaits
inside an update, so it needs no locks. Every public method on
``RequestTrace`` swallows its own exceptions: observability must never turn
into a request failure. Each request has its *own* ``RequestRecord`` that is
passed explicitly down the call chain (not stored in a shared "current
request" variable), which is what prevents one request's telemetry from
bleeding into another's under concurrency.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import logging
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from swiftserve.events import EventBus

logger = logging.getLogger("swiftserve.telemetry")

_MAX_SSE_LINE_BYTES = 1_048_576  # a single un-terminated line larger than this is treated as garbage


# --------------------------------------------------------------------------
# SSE stream timing
# --------------------------------------------------------------------------


def usage_fields(usage: dict | None) -> dict:
    """Normalise an OpenAI/vLLM ``usage`` object. A field the engine did not
    report is ``None`` (unknown), never 0 (which would claim "none cached")."""
    if not isinstance(usage, dict):
        return {"prompt_tokens": None, "completion_tokens": None, "cached_tokens": None}
    details = usage.get("prompt_tokens_details")
    cached = details.get("cached_tokens") if isinstance(details, dict) else None
    return {
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "cached_tokens": cached,
    }


class StreamTimer:
    """Incremental parser for an OpenAI-style SSE chat-completion stream that
    timestamps *content* events.

    Definitions (these are the definitions the UI shows):

    - **TTFB** -- first byte of the upstream body (may be a role-only opening
      event with no text; real vLLM sends one).
    - **TTFT** -- first SSE event whose ``choices[].delta.content`` is a
      non-empty string. This is the "first token" a user sees.
    - **ITL (mean)** -- ``(t_last_content - t_first_content) / (n_events - 1)``
      over content events. The *mean* is unaffected by several events
      arriving in one network read; the per-gap distribution is, which is why
      only the mean is reported.
    - **TPOT** -- the same span divided by ``completion_tokens - 1`` using the
      engine's own token count, when ``usage`` is present. Equal to ITL when
      every event carries exactly one token.

    Timestamps are taken once per network read, so events coalesced into one
    read share a timestamp. ``clock`` is injectable for exact arithmetic in
    tests.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._buf = b""
        self.first_byte_t: float | None = None
        self.first_content_t: float | None = None
        self.last_content_t: float | None = None
        self.content_events = 0
        self.usage: dict | None = None
        self.done_seen = False
        self.malformed_events = 0
        self.bytes_total = 0

    def feed(self, chunk: bytes, now: float | None = None) -> bool:
        """Consume one network read. Returns True iff this call saw the first
        content event (so the caller can emit ``first_token`` exactly once)."""
        t = self._clock() if now is None else now
        self.bytes_total += len(chunk)
        if self.first_byte_t is None and chunk:
            self.first_byte_t = t
        self._buf += chunk
        saw_first = False
        while True:
            nl = self._buf.find(b"\n")
            if nl < 0:
                if len(self._buf) > _MAX_SSE_LINE_BYTES:
                    self.malformed_events += 1
                    self._buf = b""
                break
            line, self._buf = self._buf[:nl], self._buf[nl + 1 :]
            if self._consume_line(line.strip(), t):
                saw_first = True
        return saw_first

    def _consume_line(self, line: bytes, t: float) -> bool:
        if not line.startswith(b"data:"):
            return False
        payload = line[5:].strip()
        if not payload:
            return False
        if payload == b"[DONE]":
            self.done_seen = True
            return False
        try:
            obj = json.loads(payload)
        except ValueError:
            self.malformed_events += 1
            return False
        if not isinstance(obj, dict):
            return False
        first = False
        for choice in obj.get("choices") or []:
            delta = choice.get("delta") if isinstance(choice, dict) else None
            content = delta.get("content") if isinstance(delta, dict) else None
            if isinstance(content, str) and content:
                self.content_events += 1
                if self.first_content_t is None:
                    self.first_content_t = t
                    first = True
                self.last_content_t = t
        usage = obj.get("usage")
        if isinstance(usage, dict) and usage:
            self.usage = usage
        return first

    # -- derived ---------------------------------------------------------------

    def ttfb_ms(self, start: float) -> float | None:
        return None if self.first_byte_t is None else (self.first_byte_t - start) * 1000.0

    def ttft_ms(self, start: float) -> float | None:
        return None if self.first_content_t is None else (self.first_content_t - start) * 1000.0

    def itl_mean_ms(self) -> float | None:
        if self.content_events < 2 or self.first_content_t is None or self.last_content_t is None:
            return None
        return (self.last_content_t - self.first_content_t) * 1000.0 / (self.content_events - 1)

    def tpot_ms(self) -> float | None:
        n = (self.usage or {}).get("completion_tokens")
        if not isinstance(n, int) or n < 2 or self.first_content_t is None or self.last_content_t is None:
            return None
        return (self.last_content_t - self.first_content_t) * 1000.0 / (n - 1)


# --------------------------------------------------------------------------
# Records
# --------------------------------------------------------------------------

TERMINAL_STATES = {"completed", "failed", "rejected", "client_disconnected"}


@dataclass
class RequestRecord:
    request_id: str
    received_wall: float
    received_mono: float
    session_id: str | None = None
    stream: bool = False
    sla_ms: float | None = None
    policy: str | None = None
    messages: int = 0
    prompt_chars: int = 0
    max_tokens: int | None = None

    state: str = "received"  # received -> routed -> dispatched -> (streaming) -> terminal
    selected_replica: int | None = None
    route_path: str | None = None
    explain: dict | None = None
    candidates: list[dict] = field(default_factory=list)
    predicted_cached_tokens: int | None = None
    dispatches: list[dict] = field(default_factory=list)  # [{attempt, replica, at_ms}]
    retries: list[dict] = field(default_factory=list)
    served_replica: int | None = None

    http_status: int | None = None
    ok: bool | None = None
    error: str | None = None
    error_stage: str | None = None
    reject_reason: str | None = None

    routed_at_ms: float | None = None
    first_dispatch_at_ms: float | None = None
    first_token_at_ms: float | None = None
    finished_at_ms: float | None = None
    total_ms: float | None = None  # received -> finished, as the router saw it
    upstream_ms: float | None = None  # first dispatch -> finished (what the EWMA is fed)
    ttfb_ms: float | None = None
    ttft_ms: float | None = None
    itl_mean_ms: float | None = None
    tpot_ms: float | None = None
    content_events: int | None = None
    stream_complete: bool | None = None

    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cached_tokens: int | None = None

    def finished(self) -> bool:
        return self.state in TERMINAL_STATES

    def to_dict(self) -> dict:
        return {
            "request_id": self.request_id,
            "received_at": self.received_wall,
            "state": self.state,
            "session_id": self.session_id,
            "stream": self.stream,
            "sla_ms": self.sla_ms,
            "policy": self.policy,
            "messages": self.messages,
            "prompt_chars": self.prompt_chars,
            "max_tokens": self.max_tokens,
            "selected_replica": self.selected_replica,
            "route_path": self.route_path,
            "explain": self.explain,
            "candidates": self.candidates,
            "predicted_cached_tokens": self.predicted_cached_tokens,
            "dispatches": self.dispatches,
            "retries": self.retries,
            "served_replica": self.served_replica,
            "http_status": self.http_status,
            "ok": self.ok,
            "error": self.error,
            "error_stage": self.error_stage,
            "reject_reason": self.reject_reason,
            "timeline_ms": {
                "routed": self.routed_at_ms,
                "first_dispatch": self.first_dispatch_at_ms,
                "first_token": self.first_token_at_ms,
                "finished": self.finished_at_ms,
            },
            "total_ms": self.total_ms,
            "upstream_ms": self.upstream_ms,
            "ttfb_ms": self.ttfb_ms,
            "ttft_ms": self.ttft_ms,
            "itl_mean_ms": self.itl_mean_ms,
            "tpot_ms": self.tpot_ms,
            "content_events": self.content_events,
            "stream_complete": self.stream_complete,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cached_tokens": self.cached_tokens,
        }


def _percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * p / 100.0
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def _summary(values: list[float]) -> dict:
    return {
        "n": len(values),
        "mean": (sum(values) / len(values)) if values else None,
        "p50": _percentile(values, 50),
        "p95": _percentile(values, 95),
    }


class ReplicaStats:
    """Router-side counters and latency windows for one replica."""

    def __init__(self, window: int = 500):
        self.selected = 0
        self.dispatch_attempts = 0
        self.retries = 0
        self.completed_ok = 0
        self.completed_error = 0
        self.client_disconnected = 0
        self.in_progress: set[str] = set()
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.cached_tokens = 0
        self.requests_with_usage = 0
        self.ttft: deque[float] = deque(maxlen=window)
        self.itl: deque[float] = deque(maxlen=window)
        self.tpot: deque[float] = deque(maxlen=window)
        self.total: deque[float] = deque(maxlen=window)

    def to_dict(self) -> dict:
        return {
            "selected": self.selected,
            "dispatch_attempts": self.dispatch_attempts,
            "retries": self.retries,
            "completed_ok": self.completed_ok,
            "completed_error": self.completed_error,
            "client_disconnected": self.client_disconnected,
            "in_progress": len(self.in_progress),
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cached_tokens": self.cached_tokens,
            "requests_with_usage": self.requests_with_usage,
            "ttft_ms": _summary(list(self.ttft)),
            "itl_mean_ms": _summary(list(self.itl)),
            "tpot_ms": _summary(list(self.tpot)),
            "total_ms": _summary(list(self.total)),
        }


# --------------------------------------------------------------------------
# Telemetry hub + per-request trace
# --------------------------------------------------------------------------

_F = Callable[..., Any]


def _safe(fn: _F) -> _F:
    """Observability must never break the request path."""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except Exception:
            logger.exception("telemetry failure in %s (ignored)", fn.__name__)
            return None

    return wrapper


class NullTrace:
    """Drop-in used when no telemetry is wired (unit tests of the proxy)."""

    def __getattr__(self, name: str) -> Callable[..., None]:
        return _noop


def _noop(*args: Any, **kwargs: Any) -> None:
    return None


NULL_TRACE = NullTrace()


class RequestTrace:
    """The router's handle on one request. Created by ``Telemetry.begin`` and
    passed explicitly through the request path."""

    def __init__(self, hub: Telemetry, record: RequestRecord):
        self._hub = hub
        self.record = record

    # -- helpers ------------------------------------------------------------

    def _ms(self) -> float:
        return (self._hub.clock() - self.record.received_mono) * 1000.0

    def _emit(self, type: str, **data: object) -> None:
        self._hub.bus.emit(type, self.record.request_id, **data)

    def _stats(self, replica_id: int) -> ReplicaStats:
        return self._hub.stats_for(replica_id)

    def _terminal(self, state: str) -> bool:
        """Mark terminal exactly once; False if it already was."""
        r = self.record
        if r.finished():
            return False
        r.state = state
        r.finished_at_ms = self._ms()
        r.total_ms = r.finished_at_ms
        return True

    # -- lifecycle ------------------------------------------------------------

    @_safe
    def routed(
        self,
        selected_replica: int,
        *,
        path: str,
        explain: dict | None,
        candidates: list[dict],
        predicted_cached_tokens: int | None,
        half_open_override: bool,
    ) -> None:
        r = self.record
        r.state = "routed"
        r.selected_replica = selected_replica
        r.route_path = path
        r.explain = explain
        r.candidates = candidates
        r.predicted_cached_tokens = predicted_cached_tokens
        r.routed_at_ms = self._ms()
        self._stats(selected_replica).selected += 1
        self._emit(
            "routing.decision",
            policy=r.policy,
            selected_replica=selected_replica,
            path=path,
            explain=explain,
            candidates=candidates,
            predicted_cached_tokens=predicted_cached_tokens,
            half_open_override=half_open_override,
            at_ms=r.routed_at_ms,
        )

    @_safe
    def dispatch(self, replica_id: int, attempt: int) -> None:
        r = self.record
        at = self._ms()
        r.state = "dispatched"
        r.dispatches.append({"attempt": attempt, "replica": replica_id, "at_ms": at})
        stats = self._stats(replica_id)
        stats.dispatch_attempts += 1
        if r.first_dispatch_at_ms is None:
            r.first_dispatch_at_ms = at
            stats.in_progress.add(r.request_id)
        self._emit(
            "dispatch",
            replica=replica_id,
            attempt=attempt,
            selected_replica=r.selected_replica,
            same_as_selected=(replica_id == r.selected_replica),
            at_ms=at,
        )

    @_safe
    def retry(self, replica_id: int, attempt: int, error: str, delay_s: float) -> None:
        r = self.record
        r.retries.append({"replica": replica_id, "failed_attempt": attempt, "error": error, "delay_s": delay_s})
        self._stats(replica_id).retries += 1
        self._emit("dispatch.retry", replica=replica_id, failed_attempt=attempt, error=error, delay_s=round(delay_s, 3))

    @_safe
    def upstream_headers(self, replica_id: int, status: int) -> None:
        self.record.http_status = status
        self._emit("upstream.headers", replica=replica_id, http_status=status, at_ms=self._ms())

    @_safe
    def first_token(self, replica_id: int, timer: StreamTimer) -> None:
        """Called once, when the stream's first content event has just been seen.
        TTFT/TTFB are measured from the moment the router received the request."""
        r = self.record
        r.state = "streaming"
        r.first_token_at_ms = self._ms()
        r.ttft_ms = timer.ttft_ms(r.received_mono)
        r.ttfb_ms = timer.ttfb_ms(r.received_mono)
        self._emit("first_token", replica=replica_id, ttft_ms=r.ttft_ms, ttfb_ms=r.ttfb_ms, at_ms=r.first_token_at_ms)

    @_safe
    def completed(
        self,
        replica_id: int,
        *,
        http_status: int,
        ok: bool,
        upstream_ms: float,
        timer: StreamTimer | None = None,
        usage: dict | None = None,
    ) -> None:
        """Terminal event for a request whose upstream response finished.
        ``timer`` is the stream timer for streaming requests (absent for
        non-streaming ones, which have no observable first token); ``usage``
        is the JSON ``usage`` object for non-streaming responses."""
        if not self._terminal("completed" if ok else "failed"):
            return
        r = self.record
        itl_mean_ms = tpot_ms = content_events = stream_complete = None
        if timer is not None:
            r.ttft_ms = timer.ttft_ms(r.received_mono)
            r.ttfb_ms = timer.ttfb_ms(r.received_mono)
            itl_mean_ms, tpot_ms = timer.itl_mean_ms(), timer.tpot_ms()
            content_events, stream_complete = timer.content_events, timer.done_seen
            usage = timer.usage
        u = usage_fields(usage)
        r.http_status = http_status
        r.ok = ok
        r.upstream_ms = upstream_ms
        r.itl_mean_ms = itl_mean_ms
        r.tpot_ms = tpot_ms
        r.content_events = content_events
        r.stream_complete = stream_complete
        r.prompt_tokens, r.completion_tokens, r.cached_tokens = u["prompt_tokens"], u["completion_tokens"], u["cached_tokens"]
        if ok:
            r.served_replica = replica_id
        else:
            r.error = f"upstream HTTP {http_status}"
            r.error_stage = "upstream_status"
        self._hub.on_finished(r, replica_id)
        self._emit(
            "request.completed" if ok else "request.failed",
            replica=replica_id,
            served_replica=r.served_replica,
            selected_replica=r.selected_replica,
            http_status=http_status,
            ok=ok,
            total_ms=r.total_ms,
            upstream_ms=upstream_ms,
            ttfb_ms=r.ttfb_ms,
            ttft_ms=r.ttft_ms,
            itl_mean_ms=itl_mean_ms,
            tpot_ms=tpot_ms,
            content_events=content_events,
            stream_complete=stream_complete,
            prompt_tokens=r.prompt_tokens,
            completion_tokens=r.completion_tokens,
            cached_tokens=r.cached_tokens,
            predicted_cached_tokens=r.predicted_cached_tokens,
            error=r.error,
            error_stage=r.error_stage,
            attempts=len(r.dispatches),
            at_ms=r.finished_at_ms,
        )

    @_safe
    def failed(
        self, replica_id: int | None, *, stage: str, error: str, upstream_ms: float | None, http_status: int | None = None
    ) -> None:
        if not self._terminal("failed"):
            return
        r = self.record
        r.ok = False
        r.error = error
        r.error_stage = stage
        r.http_status = http_status if http_status is not None else r.http_status
        r.upstream_ms = upstream_ms
        self._hub.on_finished(r, replica_id)
        self._emit(
            "request.failed",
            replica=replica_id,
            served_replica=None,
            selected_replica=r.selected_replica,
            http_status=r.http_status,
            ok=False,
            error=error,
            error_stage=stage,
            total_ms=r.total_ms,
            upstream_ms=upstream_ms,
            attempts=len(r.dispatches),
            at_ms=r.finished_at_ms,
        )

    @_safe
    def client_disconnected(self, replica_id: int, *, upstream_ms: float) -> None:
        if not self._terminal("client_disconnected"):
            return
        r = self.record
        r.upstream_ms = upstream_ms
        self._hub.on_finished(r, replica_id)
        self._emit(
            "request.client_disconnected",
            replica=replica_id,
            selected_replica=r.selected_replica,
            upstream_ms=upstream_ms,
            ttft_ms=r.ttft_ms,
            at_ms=r.finished_at_ms,
        )

    @_safe
    def rejected(self, reason: str, *, http_status: int, detail: str | None = None) -> None:
        if not self._terminal("rejected"):
            return
        r = self.record
        r.reject_reason = reason
        r.http_status = http_status
        r.ok = False
        r.error = detail or reason
        self._hub.counters["rejected_" + reason] = self._hub.counters.get("rejected_" + reason, 0) + 1
        self._emit("request.rejected", reason=reason, http_status=http_status, detail=detail, at_ms=r.finished_at_ms)


class Telemetry:
    def __init__(
        self,
        bus: EventBus,
        *,
        record_capacity: int = 2000,
        throughput_window_s: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
    ):
        self.bus = bus
        self.clock = clock
        self.wall = wall
        self.record_capacity = record_capacity
        self.throughput_window_s = throughput_window_s
        self.started_mono = clock()
        self._records: OrderedDict[str, RequestRecord] = OrderedDict()
        self._stats: dict[int, ReplicaStats] = {}
        self._completions: deque[tuple[float, int]] = deque()  # (mono time, completion_tokens)
        self.counters: dict[str, int] = {"received": 0, "completed": 0, "failed": 0, "client_disconnected": 0}

    # -- creation -------------------------------------------------------------

    def stats_for(self, replica_id: int) -> ReplicaStats:
        s = self._stats.get(replica_id)
        if s is None:
            s = self._stats[replica_id] = ReplicaStats()
        return s

    def _store(self, record: RequestRecord) -> None:
        self._records[record.request_id] = record
        self._records.move_to_end(record.request_id)
        while len(self._records) > self.record_capacity:
            self._records.popitem(last=False)

    def begin(
        self,
        request_id: str,
        *,
        session_id: str | None,
        stream: bool,
        sla_ms: float,
        policy: str,
        messages: int,
        prompt_chars: int,
        max_tokens: int | None,
    ) -> RequestTrace | NullTrace:
        try:
            record = RequestRecord(
                request_id=request_id,
                received_wall=self.wall(),
                received_mono=self.clock(),
                session_id=session_id,
                stream=stream,
                sla_ms=sla_ms,
                policy=policy,
                messages=messages,
                prompt_chars=prompt_chars,
                max_tokens=max_tokens,
            )
            self._store(record)
            self.counters["received"] += 1
            self.bus.emit(
                "request.received",
                request_id,
                session_id=session_id,
                stream=stream,
                sla_ms=sla_ms,
                policy=policy,
                messages=messages,
                prompt_chars=prompt_chars,
                max_tokens=max_tokens,
            )
            return RequestTrace(self, record)
        except Exception:
            logger.exception("telemetry failure in begin (ignored)")
            return NULL_TRACE

    def reject_invalid(self, request_id: str, detail: str, http_status: int = 400) -> None:
        """A request that failed validation before it could be routed."""
        try:
            record = RequestRecord(request_id=request_id, received_wall=self.wall(), received_mono=self.clock())
            record.state = "rejected"
            record.reject_reason = "invalid_request"
            record.http_status = http_status
            record.ok = False
            record.error = detail
            record.finished_at_ms = record.total_ms = 0.0
            self._store(record)
            self.counters["rejected_invalid_request"] = self.counters.get("rejected_invalid_request", 0) + 1
            self.bus.emit("request.rejected", request_id, reason="invalid_request", http_status=http_status, detail=detail)
        except Exception:
            logger.exception("telemetry failure in reject_invalid (ignored)")

    # -- completion bookkeeping -------------------------------------------------

    def on_finished(self, r: RequestRecord, replica_id: int | None) -> None:
        """Fold one finished request into the aggregates."""
        if r.state == "completed":
            self.counters["completed"] += 1
        elif r.state == "client_disconnected":
            self.counters["client_disconnected"] += 1
        elif r.state == "failed":
            self.counters["failed"] += 1
        if replica_id is None:
            return
        s = self.stats_for(replica_id)
        s.in_progress.discard(r.request_id)
        if r.state == "completed":
            s.completed_ok += 1
            if r.ttft_ms is not None:
                s.ttft.append(r.ttft_ms)
            if r.itl_mean_ms is not None:
                s.itl.append(r.itl_mean_ms)
            if r.tpot_ms is not None:
                s.tpot.append(r.tpot_ms)
            if r.total_ms is not None:
                s.total.append(r.total_ms)
            if r.prompt_tokens is not None or r.completion_tokens is not None:
                s.requests_with_usage += 1
                s.prompt_tokens += r.prompt_tokens or 0
                s.completion_tokens += r.completion_tokens or 0
                s.cached_tokens += r.cached_tokens or 0
            self._completions.append((self.clock(), r.completion_tokens or 0))
        elif r.state == "failed":
            s.completed_error += 1
        elif r.state == "client_disconnected":
            s.client_disconnected += 1

    # -- reading ------------------------------------------------------------------

    def record(self, request_id: str) -> RequestRecord | None:
        return self._records.get(request_id)

    def recent(self, limit: int = 100) -> list[RequestRecord]:
        return list(self._records.values())[-limit:][::-1]

    def throughput(self) -> dict:
        now = self.clock()
        while self._completions and now - self._completions[0][0] > self.throughput_window_s:
            self._completions.popleft()
        window = max(min(self.throughput_window_s, now - self.started_mono), 1e-9)
        n = len(self._completions)
        tokens = sum(t for _, t in self._completions)
        return {
            "window_s": round(window, 3),
            "completed_requests": n,
            "requests_per_s": n / window,
            "completion_tokens_per_s": tokens / window,
            "note": "completion tokens come from the engine's usage report; requests without usage count as 0 tokens",
        }

    def snapshot(self) -> dict:
        return {
            "counters": dict(self.counters),
            "per_replica": {str(rid): s.to_dict() for rid, s in sorted(self._stats.items())},
            "throughput": self.throughput(),
            "records_buffered": len(self._records),
        }


# --------------------------------------------------------------------------
# Replica state-change events
# --------------------------------------------------------------------------


async def watch_replica_state(replicas: list, bus: EventBus, interval_s: float, stop_event: asyncio.Event) -> None:
    """Emit ``replica.state`` when a replica's circuit state or scraped health
    changes, so the live view reflects availability changes as they happen
    (not only when a request happens to hit them)."""
    last: dict[int, tuple[str, bool]] = {}
    while not stop_event.is_set():
        for r in list(replicas):
            cur = (r.circuit.state.value, bool(r.metrics.healthy))
            prev = last.get(r.replica_id)
            if prev is not None and prev != cur:
                bus.emit(
                    "replica.state",
                    None,
                    replica=r.replica_id,
                    circuit={"from": prev[0], "to": cur[0]},
                    healthy={"from": prev[1], "to": cur[1]},
                )
            last[r.replica_id] = cur
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop_event.wait(), timeout=interval_s)
