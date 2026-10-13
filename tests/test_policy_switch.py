"""POST /api/policy: swapping the routing policy at runtime (used by the console's Compare tab).

Disabled by default, refused while requests are in flight, and it only ever builds one of the
existing policies. Upstream replicas here are MOCKED (see conftest.py)."""

from __future__ import annotations

import httpx
import pytest

import swiftserve.app as app_module
from swiftserve.policy import LeastConnectionsPolicy, PrefixAwarePolicy, RoundRobinPolicy, SwiftServePolicy


@pytest.fixture
def switch_enabled():
    object.__setattr__(app_module.settings, "allow_policy_switch", True)
    yield
    object.__setattr__(app_module.settings, "allow_policy_switch", False)


async def post(env, name):
    async with env.client() as c:
        return await c.post("/api/policy", json={"name": name})


async def test_disabled_by_default(router_env):
    r = await post(router_env, "least_connections")
    assert r.status_code == 403 and "disabled" in r.json()["detail"]
    assert isinstance(app_module.policy, RoundRobinPolicy)


@pytest.mark.parametrize("name,cls", [("least_connections", LeastConnectionsPolicy), ("swiftserve", SwiftServePolicy),
                                      ("prefix_aware", PrefixAwarePolicy), ("round_robin", RoundRobinPolicy)])
async def test_switches_to_each_policy_with_fresh_state(router_env, switch_enabled, name, cls):
    r = await post(router_env, name)
    assert r.status_code == 200 and r.json()["policy"] == name
    assert isinstance(app_module.policy, cls)
    async with router_env.client() as c:
        snap = (await c.get("/api/dashboard")).json()
    assert snap["router"]["policy"] == name
    assert snap["mode"]["policy_switch"]["enabled"] is True
    assert set(snap["mode"]["policy_switch"]["policies"]) >= {"round_robin", "least_connections", "swiftserve", "prefix_aware"}


async def test_prefix_index_starts_empty_after_a_switch(router_env, switch_enabled):
    await post(router_env, "prefix_aware")
    first = app_module.policy
    first.index.insert(0, [b"a" * 8, b"b" * 8])
    assert first.index.status()["distinct_blocks"] == 2
    await post(router_env, "prefix_aware")
    assert app_module.policy is not first          # a new instance, so nothing carries over
    assert app_module.policy.index.status()["distinct_blocks"] == 0


async def test_unknown_policy_is_rejected(router_env, switch_enabled):
    r = await post(router_env, "nope")
    assert r.status_code == 400 and "unknown policy" in r.json()["detail"]
    assert isinstance(app_module.policy, RoundRobinPolicy)


async def test_refused_while_requests_are_in_flight(router_env, switch_enabled):
    assert app_module.admission.try_acquire()
    try:
        r = await post(router_env, "least_connections")
        assert r.status_code == 409 and "in flight" in r.json()["detail"]
        assert isinstance(app_module.policy, RoundRobinPolicy)
    finally:
        app_module.admission.release()


async def test_switch_is_published_as_an_event(router_env, switch_enabled):
    await post(router_env, "least_connections")
    ev = [e for e in router_env.bus.since(0) if e.type == "policy.changed"]
    assert len(ev) == 1 and ev[0].data == {"from": "round_robin", "to": "least_connections"}


async def test_requests_use_the_new_policy(router_env, switch_enabled):
    def handler(request: httpx.Request):
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}], "usage": {"prompt_tokens": 3, "completion_tokens": 1}})

    router_env.set_upstream(handler)
    await post(router_env, "least_connections")
    async with router_env.client() as c:
        r = await c.post("/v1/chat/completions", json={"model": "m", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200
    recs = router_env.telemetry.recent(1)
    assert recs and recs[0].policy == "least_connections"


async def test_switch_clears_what_earlier_traffic_taught_the_router(router_env, switch_enabled):
    r = router_env.replicas[0]
    seed = r.ewma_latency_ms
    r.ewma_latency_ms = 4000.0
    r._observed_max_concurrency = 17
    r._session_last_used["s1"] = 1.0
    r.in_flight = 0
    await post(router_env, "prefix_aware")
    assert r.ewma_latency_ms == seed
    assert r._observed_max_concurrency == 0
    assert not r._session_last_used
    assert r._bucket_observed == [False] * len(r._bucket_observed)
    assert r.circuit.state == "closed"           # health is a fact about the replica, not learned routing state
