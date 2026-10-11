"""GPU facts from the replica sidecar (GET /sidecar/gpu) and the router's
polling of them. nvidia-smi itself is MOCKED here (no GPU in CI): what is under
test is parsing, the cache, error reporting, and that an unknown reading is
never turned into 0."""

from __future__ import annotations

import asyncio

import httpx

import swiftserve.replica_sidecar as sidecar
from swiftserve.metrics_scraper import _GPU_POLL_INTERVAL_S, _parse_metrics_text, poll_gpu
from swiftserve.state import ReplicaState

T4_LINE = "0, Tesla T4, 37, 1234, 15360, 52"


# -- parsing -------------------------------------------------------------------------------------------


def test_parses_a_normal_nvidia_smi_line():
    assert sidecar.parse_nvidia_smi_csv(T4_LINE + "\n") == [{
        "index": 0, "name": "Tesla T4", "utilization_pct": 37.0, "memory_used_mib": 1234.0,
        "memory_total_mib": 15360.0, "temperature_c": 52.0}]


def test_multiple_gpus_and_blank_lines():
    gpus = sidecar.parse_nvidia_smi_csv("0, A100, 5, 100, 40000, 40\n\n1, A100, 90, 39000, 40000, 71\n")
    assert [g["index"] for g in gpus] == [0, 1] and gpus[1]["utilization_pct"] == 90.0


def test_not_available_fields_become_none_never_zero():
    g = sidecar.parse_nvidia_smi_csv("0, Tesla T4, [N/A], [N/A], 15360, [N/A]")[0]
    assert g["utilization_pct"] is None and g["memory_used_mib"] is None and g["temperature_c"] is None
    assert g["memory_total_mib"] == 15360.0


def test_malformed_lines_are_skipped():
    assert sidecar.parse_nvidia_smi_csv("garbage\nnot, enough") == []


# -- query_gpu (nvidia-smi mocked) ------------------------------------------------------------------------


class FakeProc:
    def __init__(self, out=b"", err=b"", code=0, hang=False):
        self._out, self._err, self.returncode, self._hang, self.killed = out, err, code, hang, False

    async def communicate(self):
        if self._hang:
            await asyncio.sleep(10)
        return self._out, self._err

    def kill(self):
        self.killed = True


def patch_exec(monkeypatch, proc=None, exc=None):
    calls = {"n": 0}

    async def fake_exec(*args, **kwargs):
        calls["n"] += 1
        if exc:
            raise exc
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    sidecar._gpu_cache.update(t=0.0, value=None)
    return calls


async def test_query_gpu_success(monkeypatch):
    patch_exec(monkeypatch, FakeProc(out=(T4_LINE + "\n").encode()))
    r = await sidecar.query_gpu()
    assert r["available"] is True and r["gpus"][0]["name"] == "Tesla T4" and r["source"] == "nvidia-smi"


async def test_query_gpu_without_nvidia_smi_says_so(monkeypatch):
    patch_exec(monkeypatch, exc=FileNotFoundError())
    r = await sidecar.query_gpu()
    assert r == {"available": False, "error": "nvidia-smi not found on this node", "source": "nvidia-smi"}


async def test_query_gpu_nonzero_exit_reports_stderr(monkeypatch):
    patch_exec(monkeypatch, FakeProc(err=b"NVIDIA-SMI has failed", code=9))
    r = await sidecar.query_gpu()
    assert r["available"] is False and "has failed" in r["error"]


async def test_query_gpu_timeout_kills_the_process(monkeypatch):
    proc = FakeProc(hang=True)
    patch_exec(monkeypatch, proc)
    r = await sidecar.query_gpu(timeout_s=0.05)
    assert r["available"] is False and "did not answer" in r["error"] and proc.killed


async def test_query_gpu_is_cached_so_polling_cannot_spawn_a_process_per_request(monkeypatch):
    calls = patch_exec(monkeypatch, FakeProc(out=(T4_LINE + "\n").encode()))
    await sidecar.query_gpu()
    await sidecar.query_gpu()
    await sidecar.query_gpu()
    assert calls["n"] == 1
    await sidecar.query_gpu(max_age_s=0.0)
    assert calls["n"] == 2


async def test_sidecar_endpoint_returns_what_the_node_reports(monkeypatch):
    async def fake_query(*a, **k):
        return {"available": True, "gpus": [{"name": "Tesla T4"}], "source": "nvidia-smi", "error": None}

    monkeypatch.setattr(sidecar, "query_gpu", fake_query)
    app = sidecar.create_app("http://unused", admin_token=None)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://s") as c:
        body = (await c.get("/sidecar/gpu")).json()
    assert body["gpus"][0]["name"] == "Tesla T4"


# -- router-side polling -----------------------------------------------------------------------------------------


def replica():
    return ReplicaState(0, "http://node", cache_ttl_s=60)


async def poll(handler, r=None):
    r = r or replica()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        await poll_gpu(c, r)
    return r


async def test_poll_stores_the_nodes_answer():
    r = await poll(lambda req: httpx.Response(200, json={"available": True, "gpus": [{"name": "Tesla T4"}]}))
    assert r.metrics.gpu["available"] is True and r.metrics.gpu["gpus"][0]["name"] == "Tesla T4"


async def test_poll_404_means_no_sidecar_or_an_old_one_not_zero_utilization():
    r = await poll(lambda req: httpx.Response(404))
    assert r.metrics.gpu["available"] is False and "does not expose /sidecar/gpu" in r.metrics.gpu["error"]


async def test_poll_reports_server_errors_and_unreachable_nodes():
    r = await poll(lambda req: httpx.Response(500))
    assert "HTTP 500" in r.metrics.gpu["error"]

    def boom(req):
        raise httpx.ConnectError("down")

    r = await poll(boom)
    assert r.metrics.gpu["available"] is False and "ConnectError" in r.metrics.gpu["error"]


async def test_poll_handles_non_json_and_wrong_shapes():
    assert "did not return JSON" in (await poll(lambda req: httpx.Response(200, content=b"<html>"))).metrics.gpu["error"]
    assert "unexpected response shape" in (await poll(lambda req: httpx.Response(200, json=[1, 2]))).metrics.gpu["error"]


async def test_poll_is_rate_limited():
    r = replica()
    seen = {"n": 0}

    def handler(req):
        seen["n"] += 1
        return httpx.Response(200, json={"available": True, "gpus": []})

    await poll(handler, r)
    await poll(handler, r)
    assert seen["n"] == 1
    r.metrics.gpu_polled_monotonic -= _GPU_POLL_INTERVAL_S + 1
    await poll(handler, r)
    assert seen["n"] == 2


# -- the vLLM V1 metric name ----------------------------------------------------------------------------------------


def test_scraper_understands_both_vllm_kv_cache_metric_names():
    assert _parse_metrics_text("vllm:gpu_cache_usage_perc{m=\"x\"} 0.42\n")["gpu_cache"] == 0.42
    assert _parse_metrics_text("vllm:kv_cache_usage_perc{m=\"x\"} 0.31\n")["gpu_cache"] == 0.31
    assert "gpu_cache" not in _parse_metrics_text("vllm:something_else 1\n")
