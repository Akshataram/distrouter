import time

import pytest

from swiftserve.resilience import AdmissionController, CircuitBreaker, CircuitState


def make_breaker(**kwargs):
    defaults = dict(failure_threshold=3, reset_timeout_s=0.05, max_reset_timeout_s=0.2)
    defaults.update(kwargs)
    return CircuitBreaker(**defaults)


def test_stays_closed_below_failure_threshold():
    cb = make_breaker()
    cb.record_failure()
    cb.record_failure()
    assert cb.state is CircuitState.CLOSED
    assert not cb.is_open()


def test_trips_open_at_failure_threshold():
    cb = make_breaker()
    for _ in range(3):
        cb.record_failure()
    assert cb.state is CircuitState.OPEN
    assert cb.is_open()


def test_success_resets_consecutive_failure_count():
    cb = make_breaker()
    cb.record_failure()
    cb.record_failure()
    cb.record_success()
    cb.record_failure()
    cb.record_failure()
    assert cb.state is CircuitState.CLOSED  # would have tripped without the reset


def test_transitions_to_half_open_after_timeout():
    cb = make_breaker()
    for _ in range(3):
        cb.record_failure()
    assert cb.is_open()
    time.sleep(0.06)
    assert cb.state is CircuitState.HALF_OPEN


def test_half_open_success_closes_circuit():
    cb = make_breaker()
    for _ in range(3):
        cb.record_failure()
    time.sleep(0.06)
    assert cb.state is CircuitState.HALF_OPEN
    cb.record_success()
    assert cb.state is CircuitState.CLOSED


def test_half_open_failure_reopens_with_longer_backoff():
    cb = make_breaker()
    for _ in range(3):
        cb.record_failure()
    time.sleep(0.06)
    assert cb.state is CircuitState.HALF_OPEN
    cb.record_failure()
    assert cb.state is CircuitState.OPEN
    # backoff doubled (0.05 -> 0.1s): not yet half-open at the original timeout
    time.sleep(0.06)
    assert cb.state is CircuitState.OPEN
    time.sleep(0.06)
    assert cb.state is CircuitState.HALF_OPEN


def test_half_open_probe_capacity_is_capped():
    cb = make_breaker(half_open_max_probes=1)
    for _ in range(3):
        cb.record_failure()
    time.sleep(0.06)
    assert cb.has_probe_capacity()
    cb.mark_dispatched()
    assert not cb.has_probe_capacity()


def test_closed_circuit_always_has_probe_capacity():
    cb = make_breaker()
    assert cb.has_probe_capacity()
    cb.mark_dispatched()  # no-op outside half-open
    assert cb.has_probe_capacity()


def test_admission_controller_rejects_past_capacity():
    admission = AdmissionController(max_in_flight=2)
    assert admission.try_acquire()
    assert admission.try_acquire()
    assert not admission.try_acquire()


def test_admission_controller_release_frees_capacity():
    admission = AdmissionController(max_in_flight=1)
    assert admission.try_acquire()
    assert not admission.try_acquire()
    admission.release()
    assert admission.try_acquire()


def test_admission_controller_release_below_zero_is_safe():
    admission = AdmissionController(max_in_flight=1)
    admission.release()
    admission.release()
    assert admission.in_flight == 0
