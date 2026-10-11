"""scripts/dev.py: argument handling, environment building, and the startup
validation that turns "my tunnel is dead" into a readable diagnosis. Replica
HTTP is MOCKED; nothing here starts a process except the --check-only path."""

from __future__ import annotations

import argparse
import socket

import httpx
import pytest

from scripts import dev


def args(**over):
    ns = dev.build_parser().parse_args([])
    for k, v in over.items():
        setattr(ns, k, v)
    return ns


# -- pure helpers ----------------------------------------------------------------------------------


def test_parse_replica_urls_strips_whitespace_and_trailing_slashes():
    assert dev.parse_replica_urls(" https://a.trycloudflare.com/ , http://b:8000 ") == ["https://a.trycloudflare.com", "http://b:8000"]
    assert dev.parse_replica_urls("") == []


@pytest.mark.parametrize("raw", ["a.trycloudflare.com", "ftp://x", "https://a,https://a"])
def test_parse_replica_urls_rejects_bad_input(raw):
    with pytest.raises(ValueError):
        dev.parse_replica_urls(raw)


def test_router_env_only_exports_what_the_operator_set(monkeypatch):
    for k in ("SWIFTSERVE_ASSUMED_MAX_BATCH_SIZE", "SWIFTSERVE_INDEX_MAX_BLOCKS", "SWIFTSERVE_MIN_MATCH_TOKENS", "SWIFTSERVE_TOKENIZER"):
        monkeypatch.delenv(k, raising=False)
    env = dev.build_router_env(args(policy="prefix_aware"), ["https://a"], "Qwen/x", simulated=False, base={})
    assert env["SWIFTSERVE_REPLICAS"] == "https://a" and env["SWIFTSERVE_POLICY"] == "prefix_aware" and env["SWIFTSERVE_MODEL"] == "Qwen/x"
    assert env["SWIFTSERVE_BLOCK_SIZE"] == "16"
    for untouched in ("SWIFTSERVE_ASSUMED_MAX_BATCH_SIZE", "SWIFTSERVE_INDEX_MAX_BLOCKS", "SWIFTSERVE_MIN_MATCH_TOKENS",
                      "SWIFTSERVE_REPLICA_LABELS", "SWIFTSERVE_TOKENIZER", "SWIFTSERVE_SIMULATED"):
        assert untouched not in env                      # the router keeps its own defaults


def test_router_env_passes_configured_options_through():
    env = dev.build_router_env(
        args(policy="prefix_aware", tokenizer="Qwen/x", max_num_seqs=32, index_max_blocks=375, min_match_tokens=128, labels="a,b"),
        ["https://a", "https://b"], "Qwen/x", simulated=False, base={})
    assert (env["SWIFTSERVE_TOKENIZER"], env["SWIFTSERVE_ASSUMED_MAX_BATCH_SIZE"], env["SWIFTSERVE_INDEX_MAX_BLOCKS"],
            env["SWIFTSERVE_MIN_MATCH_TOKENS"], env["SWIFTSERVE_REPLICA_LABELS"]) == ("Qwen/x", "32", "375", "128", "a,b")


def test_simulated_flag_is_set_only_in_simulated_mode_and_cleared_otherwise():
    assert dev.build_router_env(args(), ["http://x"], "m", simulated=True, base={})["SWIFTSERVE_SIMULATED"] == "1"
    assert "SWIFTSERVE_SIMULATED" not in dev.build_router_env(args(), ["http://x"], "m", simulated=False, base={"SWIFTSERVE_SIMULATED": "1"})


def test_port_in_use_detects_a_listening_socket():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen()
        port = s.getsockname()[1]
        assert dev.port_in_use("127.0.0.1", port) is True
    assert dev.port_in_use("127.0.0.1", port) is False


def test_missing_modules_reports_pip_names():
    assert dev.missing_modules({"definitely_not_a_module_xyz": "some-pip-name", "json": "json"}) == ["some-pip-name"]


# -- Cloudflare-aware diagnosis --------------------------------------------------------------------------


CF = {"server": "cloudflare", "cf-ray": "abc"}


def test_healthy():
    r = dev.classify_probe("u", 200, CF, None)
    assert r.ok and "healthy" in r.detail


def test_dead_tunnel_is_diagnosed_differently_from_a_live_tunnel_with_nothing_behind_it():
    dead = dev.classify_probe("u", 530, CF, None)
    assert not dead.ok and "no longer exists" in dead.hint and "NEW URL" in dead.hint
    empty = dev.classify_probe("u", 502, CF, None)
    assert not empty.ok and "Cloudflare reached the tunnel" in empty.hint and "vLLM" in empty.hint
    assert "Cloudflare" not in dev.classify_probe("u", 502, {"server": "nginx"}, None).hint


def test_other_failures_have_plain_explanations():
    assert "does not look like a vLLM" in dev.classify_probe("u", 404, None, None).hint
    assert "DNS" in dev.classify_probe("u", None, None, httpx.ConnectError("x")).detail
    assert "timed out" in dev.classify_probe("u", None, None, httpx.ConnectTimeout("x")).detail
    assert dev.classify_probe("u", 418, None, None).ok is False


# -- validation against mocked replicas ------------------------------------------------------------------------


def patch_client(monkeypatch, handler):
    real = httpx.AsyncClient
    monkeypatch.setattr(dev.httpx, "AsyncClient", lambda *a, **k: real(transport=httpx.MockTransport(handler)))


def replica_handler(models=("Qwen/Qwen2.5-3B-Instruct",), down=()):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host in down:
            return httpx.Response(530, headers=CF)
        if request.url.path == "/health":
            return httpx.Response(200, json={})
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": m} for m in models]})
        return httpx.Response(404)
    return handler


async def test_validate_detects_the_model_from_the_replica_when_not_given(monkeypatch):
    patch_client(monkeypatch, replica_handler())
    results, model, warnings = await dev.validate_replicas(["https://a.example", "https://b.example"], "")
    assert all(r.ok for r in results) and model == "Qwen/Qwen2.5-3B-Instruct" and any("using 'Qwen/Qwen2.5-3B-Instruct'" in w for w in warnings)


async def test_validate_warns_when_the_given_model_is_not_served(monkeypatch):
    patch_client(monkeypatch, replica_handler())
    _, model, warnings = await dev.validate_replicas(["https://a.example"], "Qwen/Qwen2.5-7B-Instruct")
    assert model == "Qwen/Qwen2.5-7B-Instruct" and any("404" in w and "--model Qwen/Qwen2.5-3B-Instruct" in w for w in warnings)


async def test_validate_marks_dead_tunnels_and_still_checks_the_live_ones(monkeypatch):
    patch_client(monkeypatch, replica_handler(down=("dead.example",)))
    results, _, _ = await dev.validate_replicas(["https://dead.example", "https://live.example"], "Qwen/Qwen2.5-3B-Instruct")
    assert [r.ok for r in results] == [False, True] and "Re-run" in results[0].hint


def test_policy_warnings_for_a_real_prefix_aware_cluster(monkeypatch):
    monkeypatch.delenv("SWIFTSERVE_ASSUMED_MAX_BATCH_SIZE", raising=False)
    text = " ".join(dev.policy_warnings(args(policy="prefix_aware"), simulated=False))
    assert "--tokenizer" in text and "--max-num-seqs" in text
    clean = dev.policy_warnings(args(policy="prefix_aware", tokenizer="Qwen/x", max_num_seqs=32), simulated=False)
    assert not any("--tokenizer" in w and "fake byte" in w for w in clean) and not any("--max-num-seqs" in w for w in clean)
    assert dev.policy_warnings(args(policy="round_robin"), simulated=False) == []
    assert any("unknown policy" in w for w in dev.policy_warnings(args(policy="nope"), simulated=True))


# -- main() ---------------------------------------------------------------------------------------------------------------


def test_main_rejects_a_bad_url_and_a_labels_mismatch(capsys, tmp_path):
    assert dev.main(["--replicas", "ftp://x", "--log-dir", str(tmp_path)]) == 2
    assert dev.main(["--replicas", "https://a,https://b", "--labels", "only-one", "--log-dir", str(tmp_path), "--check-only"]) == 2
    assert "matched by position" in capsys.readouterr().err


def test_main_reports_missing_packages(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(dev, "missing_modules", lambda *a, **k: ["fastapi"])
    assert dev.main(["--replicas", "https://a.example", "--log-dir", str(tmp_path)]) == 2 and "pip install -r requirements.txt" in capsys.readouterr().err


def test_main_refuses_a_busy_port(capsys, tmp_path):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen()
        assert dev.main(["--replicas", "https://a.example", "--port", str(s.getsockname()[1]), "--log-dir", str(tmp_path)]) == 2
    assert "already in use" in capsys.readouterr().err


def test_main_check_only_in_simulated_mode_starts_nothing(capsys, tmp_path):
    assert dev.main(["--simulate", "--check-only", "--log-dir", str(tmp_path)]) == 0
    assert "SIMULATED" in capsys.readouterr().out


def test_main_exits_nonzero_when_no_replica_answers_unless_allowed(monkeypatch, capsys, tmp_path):
    patch_client(monkeypatch, replica_handler(down=("dead.example",)))
    assert dev.main(["--replicas", "https://dead.example", "--model", "m", "--check-only", "--log-dir", str(tmp_path)]) == 1
    assert "no replica answered /health" in capsys.readouterr().err
    assert dev.main(["--replicas", "https://dead.example", "--model", "m", "--check-only", "--allow-unreachable", "--log-dir", str(tmp_path)]) == 0


def test_namespace_type_is_what_build_parser_returns():
    assert isinstance(dev.build_parser().parse_args([]), argparse.Namespace)


def test_real_replicas_are_required_unless_simulate_is_explicit(capsys, tmp_path):
    assert dev.main(["--log-dir", str(tmp_path)]) == 2
    assert "no replicas given" in capsys.readouterr().err
    assert dev.main(["--simulate", "--replicas", "https://a.example", "--log-dir", str(tmp_path)]) == 2
