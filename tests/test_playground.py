"""Tests for scripts/playground.py: the system-prompt presets and the API that
relays one request to a policy's router and reports what happened.

The routers themselves are replaced by an httpx MockTransport that answers
with a hand-built SSE stream and the same headers the real router sets, so
these check the playground's own logic (isolation marker, field arithmetic,
error mapping) without starting any process."""

import json

import httpx
import pytest

from scripts.playground import PlaygroundState, create_app, make_system_prompt

POLICIES = ["round_robin", "least_connections", "swiftserve", "prefix_aware"]


def sse(cached: int, prompt: int = 100) -> bytes:
    chunks = [
        {"choices": [{"delta": {"content": "Hel"}}]},
        {"choices": [{"delta": {"content": "lo"}}]},
        {"choices": [], "usage": {
            "prompt_tokens": prompt, "completion_tokens": 2, "prompt_tokens_details": {"cached_tokens": cached},
        }},
    ]
    return ("".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n").encode()


def make_state(handler) -> PlaygroundState:
    state = PlaygroundState(
        model="m", tokenizer="", policies=POLICIES, replica_urls=["http://r0", "http://r1"], simulated=True,
        router_urls={p: f"http://router-{p}" for p in POLICIES},
    )
    state.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return state


def router_ok(captured: list, cached: int = 64, predicted: int = 48, replica: str = "2"):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/status":
            return httpx.Response(200, json={"policy": "x", "replicas": []})
        captured.append(json.loads(request.content))
        return httpx.Response(
            200, content=sse(cached), headers={
                "content-type": "text/event-stream",
                "x-swiftserve-replica": replica,
                "x-swiftserve-cache-hit": "false",
                "x-swiftserve-predicted-cached-tokens": str(predicted),
            },
        )
    return handler


async def call(state: PlaygroundState, method: str, path: str, **kwargs) -> httpx.Response:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(state)), base_url="http://t") as c:
        return await c.request(method, path, **kwargs)


# -- presets -----------------------------------------------------------------


def test_system_prompt_is_deterministic_and_sized():
    a = make_system_prompt("acme-bank", 2000)
    assert a == make_system_prompt("acme-bank", 2000)
    assert 2000 <= len(a) // 4 <= 2000 + 40  # overshoots by at most one rule line
    assert a.startswith("You are a retail-banking support assistant for Acme Bank.")


def test_different_presets_share_no_prefix_beyond_the_template_words():
    a, b = make_system_prompt("acme-bank", 500), make_system_prompt("skytravel", 500)
    assert a != b
    assert a[:40] != b[:40]  # differ right at the start, so they cannot share cache blocks


def test_unknown_preset_raises():
    with pytest.raises(KeyError):
        make_system_prompt("nope")


# -- API ---------------------------------------------------------------------


async def test_config_lists_policies_and_mode():
    state = make_state(router_ok([]))
    body = (await call(state, "GET", "/api/config")).json()
    assert body["simulated"] is True and body["policies"] == POLICIES
    assert {p["id"] for p in body["presets"]} >= {"acme-bank", "skytravel"}


async def test_preset_endpoint_and_404():
    state = make_state(router_ok([]))
    ok = await call(state, "GET", "/api/preset", params={"id": "shopeasy", "tokens": 300})
    assert ok.status_code == 200 and ok.json()["system"] == make_system_prompt("shopeasy", 300)
    assert (await call(state, "GET", "/api/preset", params={"id": "zzz"})).status_code == 404


async def test_send_computes_fields_from_router_response():
    captured: list = []
    state = make_state(router_ok(captured, cached=64, predicted=48, replica="2"))
    resp = await call(state, "POST", "/api/send", json={"policy": "prefix_aware", "system": "S", "user": "hi"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["replica"] == "2" and body["policy"] == "prefix_aware"
    assert body["prompt_tokens"] == 100 and body["cached_tokens"] == 64
    assert body["predicted_cached_tokens"] == 48 and body["prediction_error_tokens"] == 16
    assert body["cache_ratio"] == pytest.approx(0.64)
    assert body["content"] == "Hello" and body["ttft_ms"] is not None
    assert [m["role"] for m in captured[0]["messages"]] == ["system", "user"]


async def test_send_isolation_marker_is_per_policy_and_optional():
    captured: list = []
    state = make_state(router_ok(captured))
    await call(state, "POST", "/api/send", json={"policy": "prefix_aware", "system": "S", "user": "q"})
    await call(state, "POST", "/api/send", json={"policy": "swiftserve", "system": "S", "user": "q"})
    await call(state, "POST", "/api/send", json={"policy": "swiftserve", "system": "S", "user": "q", "isolate": False})
    systems = [c["messages"][0]["content"] for c in captured]
    assert systems == ["[ns:prefix_aware] S", "[ns:swiftserve] S", "S"]


async def test_send_without_system_prompt_sends_only_the_user_message():
    captured: list = []
    state = make_state(router_ok(captured))
    await call(state, "POST", "/api/send", json={"policy": "round_robin", "user": "just a question"})
    assert [m["role"] for m in captured[0]["messages"]] == ["user"]


async def test_send_unknown_policy_is_400():
    state = make_state(router_ok([]))
    resp = await call(state, "POST", "/api/send", json={"policy": "bogus", "user": "x"})
    assert resp.status_code == 400 and "bogus" in resp.json()["error"]


async def test_send_maps_router_failure_to_502():
    state = make_state(lambda request: httpx.Response(503, json={"error": "busy"}))
    resp = await call(state, "POST", "/api/send", json={"policy": "round_robin", "user": "x"})
    assert resp.status_code == 502 and "503" in resp.json()["error"]


async def test_send_maps_unreachable_router_to_502():
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    state = make_state(boom)
    resp = await call(state, "POST", "/api/send", json={"policy": "round_robin", "user": "x"})
    assert resp.status_code == 502 and "unreachable" in resp.json()["error"]


async def test_send_validates_input():
    state = make_state(router_ok([]))
    assert (await call(state, "POST", "/api/send", json={"policy": "round_robin", "user": ""})).status_code == 422
    assert (await call(state, "POST", "/api/send",
                       json={"policy": "round_robin", "user": "x", "max_tokens": 0})).status_code == 422


async def test_status_collects_every_router_and_tolerates_one_being_down():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "router-swiftserve":
            raise httpx.ConnectError("down")
        return httpx.Response(200, json={"policy": request.url.host, "replicas": []})

    state = make_state(handler)
    body = (await call(state, "GET", "/api/status")).json()
    assert set(body) == set(POLICIES)
    assert "error" in body["swiftserve"] and body["prefix_aware"]["policy"] == "router-prefix_aware"


def test_ui_file_exists_and_is_served():
    from scripts.playground import UI_PATH

    assert UI_PATH.exists() and "SwiftServe Playground" in UI_PATH.read_text()
