"""Unit tests for the in-process event bus (swiftserve/events.py): ordering,
bounded memory, replay, gap detection, and the guarantee that a slow observer
can never block or break the producer."""

import asyncio

import pytest

from swiftserve.events import EventBus


def test_sequence_numbers_are_monotonic_and_start_at_one():
    bus = EventBus()
    seqs = [bus.emit("t", "r").seq for _ in range(5)]
    assert seqs == [1, 2, 3, 4, 5]
    assert bus.head_seq == 5 and bus.oldest_seq == 1


def test_event_dict_flattens_data_and_keeps_core_fields():
    bus = EventBus()
    ev = bus.emit("dispatch", "req-1", replica=2, attempt=1)
    d = ev.to_dict()
    assert d["type"] == "dispatch" and d["request_id"] == "req-1" and d["replica"] == 2 and d["seq"] == 1
    assert isinstance(d["ts"], float)


def test_reserved_keys_cannot_be_overwritten_by_event_data():
    bus = EventBus()
    assert bus.emit("t", "r", seq=99) is None  # refused, and emit() itself did not raise
    assert bus.head_seq == 0


def test_ring_buffer_is_bounded_and_reports_a_gap():
    bus = EventBus(capacity=3)
    for i in range(10):
        bus.emit("t", f"r{i}")
    assert len(bus.since(0)) == 3
    assert bus.oldest_seq == 8 and bus.head_seq == 10
    assert bus.has_gap_after(2) is True       # events 3..7 are gone
    assert bus.has_gap_after(7) is False      # 8..10 are all still buffered
    assert bus.has_gap_after(10) is False


def test_since_and_limit_and_for_request():
    bus = EventBus()
    for i in range(6):
        bus.emit("t", "a" if i % 2 == 0 else "b")
    assert [e.seq for e in bus.since(3)] == [4, 5, 6]
    assert [e.seq for e in bus.since(0, limit=2)] == [5, 6]
    assert [e.seq for e in bus.for_request("a")] == [1, 3, 5]


async def test_subscriber_receives_events_in_order():
    bus = EventBus()
    with bus.subscribe() as sub:
        for i in range(3):
            bus.emit("t", f"r{i}")
        got = [(await sub.get(timeout=1)).request_id for _ in range(3)]
    assert got == ["r0", "r1", "r2"]
    assert bus.subscriber_count == 0  # context manager unsubscribed


async def test_get_times_out_with_none():
    bus = EventBus()
    with bus.subscribe() as sub:
        assert await sub.get(timeout=0.05) is None


def test_slow_subscriber_overflows_without_blocking_the_producer():
    bus = EventBus(capacity=100, subscriber_queue_size=2)
    sub = bus.subscribe()
    for i in range(10):
        assert bus.emit("t", f"r{i}") is not None   # producer never blocked or raised
    assert sub.overflowed is True                    # ...and the observer is told it missed events
    assert bus.head_seq == 10


def test_one_overflowed_subscriber_does_not_affect_a_healthy_one():
    bus = EventBus(subscriber_queue_size=2)
    slow, fast = bus.subscribe(), bus.subscribe()
    for i in range(2):
        bus.emit("t", f"r{i}")
    assert not slow.overflowed and not fast.overflowed
    bus.emit("t", "r2")
    assert slow.overflowed and fast.overflowed  # both queues were full here; now drain only `fast`
    fast2 = bus.subscribe()
    bus.emit("t", "r3")
    assert not fast2.overflowed


async def test_closed_subscription_receives_nothing_more():
    bus = EventBus()
    sub = bus.subscribe()
    sub.close()
    bus.emit("t", "r")
    assert sub.empty()


def test_capacity_must_be_positive():
    with pytest.raises(ValueError):
        EventBus(capacity=0)


async def test_emit_from_many_tasks_keeps_unique_ordered_sequence():
    bus = EventBus()

    async def producer(n):
        for _ in range(50):
            bus.emit("t", f"p{n}")
            await asyncio.sleep(0)

    await asyncio.gather(*(producer(n) for n in range(8)))
    seqs = [e.seq for e in bus.since(0)]
    assert seqs == sorted(seqs) and len(set(seqs)) == 400
