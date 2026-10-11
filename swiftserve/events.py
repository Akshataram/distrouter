"""In-process structured event bus: the router's live observability channel.

The router already exposes Prometheus counters (`metrics.py`), which answer
"how many, on average". They cannot answer "what exactly happened to *this*
request", which is what a live routing view needs. This module is the small
piece of plumbing that makes that possible: the request path emits one
structured event per step (received, routing decision, dispatch, first
token, completion/failure), and any number of observers -- the UI's SSE
stream, a test -- see them in order.

Design constraints, all driven by "observability must not interfere with
routing":

- **`emit()` never blocks and never raises.** It runs on the request path.
  A slow or stalled observer must not slow a request down, so each
  subscriber has a *bounded* queue and a full queue means that subscriber is
  marked overflowed (and told so) rather than back-pressuring the router.
- **Bounded memory.** Events live in a fixed-size ring buffer. A late
  subscriber can replay what is still in the buffer; anything older is gone
  and the subscriber is told there is a gap instead of being shown a
  silently incomplete history.
- **Single event loop.** Everything here is called from the router's one
  asyncio loop with no `await` inside a critical section, so plain Python
  structures are consistent without locks. Run the router with
  ``--workers N > 1`` and each worker process has its own bus; that is the
  same per-process scope the router's replica/policy state already has.

Events are plain dicts at the API boundary (`Event.to_dict`). Event field
names are part of the UI contract; see docs/COMPLETE_PROJECT_WALKTHROUGH.md
for the full catalogue.
"""

from __future__ import annotations

import asyncio
import itertools
import time
from collections import deque
from dataclasses import dataclass, field

_RESERVED = {"seq", "ts", "type", "request_id"}


@dataclass(frozen=True)
class Event:
    seq: int
    ts: float  # wall-clock epoch seconds (for display and cross-process correlation)
    type: str
    request_id: str | None
    data: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"seq": self.seq, "ts": self.ts, "type": self.type, "request_id": self.request_id, **self.data}


class Subscription:
    """One observer's bounded queue of events. Iterate with ``await get()``."""

    def __init__(self, bus: EventBus, maxsize: int):
        self._bus = bus
        self._queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=maxsize)
        self.overflowed = False
        self.closed = False

    def _offer(self, event: Event) -> None:
        if self.closed or self.overflowed:
            return
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            # Do not block the router and do not silently drop: remember that
            # this observer missed events so it can resynchronise.
            self.overflowed = True

    def empty(self) -> bool:
        return self._queue.empty()

    async def get(self, timeout: float | None = None) -> Event | None:
        """Next event, or ``None`` on timeout."""
        try:
            if timeout is None:
                return await self._queue.get()
            return await asyncio.wait_for(self._queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return None

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self._bus._unsubscribe(self)

    def __enter__(self) -> Subscription:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class EventBus:
    def __init__(self, capacity: int = 5000, subscriber_queue_size: int = 2000):
        if capacity < 1:
            raise ValueError("capacity must be >= 1")
        self.capacity = capacity
        self.subscriber_queue_size = subscriber_queue_size
        self._buffer: deque[Event] = deque(maxlen=capacity)
        self._seq = itertools.count(1)
        self._head = 0
        self._subscribers: list[Subscription] = []
        self.emitted_total = 0

    # -- producing -----------------------------------------------------------

    def emit(self, type: str, request_id: str | None = None, **data: object) -> Event | None:
        """Record an event and fan it out. Never raises: an observability
        failure must not become a request failure."""
        try:
            clash = _RESERVED.intersection(data)
            if clash:
                raise ValueError(f"event data may not use reserved keys: {sorted(clash)}")
            self._head = next(self._seq)
            event = Event(self._head, time.time(), type, request_id, dict(data))
            self._buffer.append(event)
            self.emitted_total += 1
            for sub in tuple(self._subscribers):
                sub._offer(event)
            return event
        except Exception:  # pragma: no cover - defensive, see docstring
            return None

    # -- consuming -----------------------------------------------------------

    @property
    def head_seq(self) -> int:
        """Sequence number of the most recent event (0 if none yet)."""
        return self._head

    @property
    def oldest_seq(self) -> int:
        """Sequence number of the oldest event still buffered (0 if none)."""
        return self._buffer[0].seq if self._buffer else 0

    def since(self, seq: int = 0, limit: int | None = None) -> list[Event]:
        """Buffered events with ``seq`` strictly greater than the given one."""
        out = [e for e in self._buffer if e.seq > seq]
        return out[-limit:] if limit else out

    def has_gap_after(self, seq: int) -> bool:
        """True if events newer than ``seq`` existed but have been evicted
        from the ring buffer, i.e. a replay from ``seq`` would be incomplete."""
        return self._head > seq and self.oldest_seq > seq + 1

    def for_request(self, request_id: str) -> list[Event]:
        return [e for e in self._buffer if e.request_id == request_id]

    def subscribe(self) -> Subscription:
        sub = Subscription(self, self.subscriber_queue_size)
        self._subscribers.append(sub)
        return sub

    def _unsubscribe(self, sub: Subscription) -> None:
        if sub in self._subscribers:
            self._subscribers.remove(sub)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def status(self) -> dict:
        return {
            "capacity": self.capacity,
            "buffered": len(self._buffer),
            "head_seq": self._head,
            "oldest_seq": self.oldest_seq,
            "emitted_total": self.emitted_total,
            "subscribers": len(self._subscribers),
        }
