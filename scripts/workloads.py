"""Shared, deterministic workload generators for scripts/benchmark.py and
scripts/load_test.py.

Both scripts used to carry their own copy-pasted ``PROMPTS`` list and
``run_session`` (5 canned prompts, no notion of a "session sharing a system
prompt with other sessions" or "many questions against one long document").
That's fine for a smoke test, but it can't reproduce the workload shapes
DistServe/Preble/MoonCake actually benchmark against -- shared-system-prompt
multi-tenant serving, long-document Q&A, and real multi-turn traces -- so
comparing SwiftServe's prefix-aware routing against a plain policy on the
old canned prompts was never going to show a difference: there was no
shared prefix structure across sessions for it to exploit.

Every generator here is a pure function of a seed: no I/O, no wall-clock
dependence, so a --workload run today can be reproduced byte-for-byte
tomorrow (a permutation test is only meaningful when the plumbing feeding
it is deterministic).

Message semantics -- the one subtlety, get this wrong and every workload
here silently stops testing prefix caching:

    for turn == 0, ``messages`` is the full seed, e.g.
        [system_prompt_message, first_user_message]
    for turn > 0, ``messages`` is ONLY the new user message for that turn,
        a 1-item list -- never a fabricated multi-turn history.

The runner (run_session_from_requests in scripts/benchmark.py) is
responsible for appending each turn's real, live streamed assistant reply
to the growing transcript before sending the next turn's request. A
workload generator that instead pre-scripted a fake assistant turn here
would make the real model's actual completion diverge from what's sent
next turn -- silently breaking vLLM's real prefix-cache match on every
turn after the first, since the bytes vLLM sees would no longer be a
prefix of what it actually generated and cached.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from pathlib import Path

# ~150-word vocabulary for _generate_text: enough variety that generated
# filler text doesn't itself become a degenerate shared prefix across
# unrelated sessions, small enough to keep this module dependency-free
# (no nltk/corpus download).
_VOCAB = ["system", "prompt", "context", "window", "latency", "throughput", "replica", "cluster", "router", "policy", "cache", "session", "token", "completion", "prompt", "inference", "batch", "queue", "depth", "circuit", "breaker", "admission", "control", "retry", "backoff", "fallback", "gpu", "memory", "bandwidth", "compute", "forward", "pass", "attention", "layer", "transformer", "model", "weight", "quantization", "decode", "prefill", "schedule", "arrival", "rate", "poisson", "distribution", "percentile", "median", "tail", "slo", "sla", "goodput", "workload", "trace", "document", "paragraph", "section", "summary", "question", "answer", "context", "history", "conversation", "turn", "assistant", "user", "role", "message", "array", "json", "payload", "header", "response", "status", "error", "timeout", "circuit", "half", "open", "closed", "probe", "capacity", "concurrency", "occupancy", "bucket", "ewma", "moving", "average", "estimate", "prediction", "warm", "cold", "miss", "hit", "ratio", "affinity", "heatmap", "trie", "prefix", "shared", "tenant", "multi", "node", "deploy", "scale", "horizontal", "vertical", "replica", "load", "balance", "round", "robin", "least", "connections", "session", "sticky", "routing", "vllm", "openai", "chat", "completions", "stream", "chunk", "delta", "usage", "tokens", "cached", "cost", "benefit", "tradeoff", "design", "architecture", "diagram", "sequence", "class", "entity", "relationship", "data", "flow", "interview", "study", "guide", "reference", "deep", "dive", "analysis", "honest", "real", "fake", "mock", "stub", "simulate", "benchmark", "load", "test", "harness", "fixture", "assertion", "pytest", "ruff", "mypy", "lint", "type", "check", "ci", "continuous", "integration", "pipeline", "workflow", "action", "branch", "commit", "push", "pull", "request", "review", "merge", "conflict", "rebase", "stash", "reset", "restore", "clean"]


def _generate_text(num_tokens: int, rng: random.Random) -> str:
    """Fixed-vocabulary filler text sized so ``len(text) // 4`` (the same
    chars/4 heuristic policy.py's ``_estimate_prefix_tokens`` uses) comes
    out to approximately ``num_tokens``. Words average ~6 chars incl. the
    trailing space, i.e. ~1.5 tokens/word under that heuristic, so we draw
    roughly ``num_tokens // 1.5`` words and trim/pad on the resulting
    length rather than the word count, so the estimate stays accurate
    regardless of which words happened to get drawn."""
    if num_tokens <= 0:
        return ""
    target_chars = num_tokens * 4
    words: list[str] = []
    length = 0
    while length < target_chars:
        word = rng.choice(_VOCAB)
        words.append(word)
        length += len(word) + 1
    return " ".join(words)[:target_chars] or " "


def zipf_weights(n: int, s: float) -> list[float]:
    """Zipf(s) weights over n ranks (1-indexed), normalized to sum to 1:
    w_i ∝ 1 / rank_i^s. Higher s concentrates more mass on the low-rank
    (rank 1 = most popular) items -- the standard model for skewed
    multi-tenant traffic where a handful of "apps"/system-prompts account
    for most sessions."""
    if n <= 0:
        raise ValueError("n must be > 0")
    raw = [1.0 / (rank**s) for rank in range(1, n + 1)]
    total = sum(raw)
    return [w / total for w in raw]


def poisson_arrivals(rate_rps: float, n: int, seed: int) -> list[float]:
    """n cumulative inter-arrival draws from Exponential(rate_rps), i.e. a
    Poisson arrival process's event times starting from 0. Returns
    strictly increasing arrival timestamps in seconds."""
    if rate_rps <= 0:
        raise ValueError("rate_rps must be > 0")
    rng = random.Random(seed)
    times = []
    t = 0.0
    for _ in range(n):
        t += rng.expovariate(rate_rps)
        times.append(t)
    return times


@dataclass
class SessionRequests:
    """One session's worth of turns, in the message-append semantics
    described in the module docstring. ``turns`` is a list of ``messages``
    lists, one per turn -- turn 0 is the full seed, turn>0 is a single new
    user message."""

    session_id: str
    turns: list[list[dict]]
    max_tokens: int
    ttft_slo_ms: float
    tpot_slo_ms: float
    # Which shared "app"/document this session belongs to, for reporting
    # (e.g. true_cache_ratio grouped by app) -- not used for routing.
    app_id: str = ""


@dataclass
class Workload:
    """Everything one workload generator produces: the per-session request
    plans plus the SLO thresholds sessions were generated with (so
    scripts/benchmark.py doesn't need a separate --workload-specific SLO
    flag on top of the general --sla-ms)."""

    sessions: list[SessionRequests]
    ttft_slo_ms: float
    tpot_slo_ms: float


def _assign_apps_by_zipf(num_sessions: int, num_apps: int, zipf_s: float, rng: random.Random) -> list[int]:
    weights = zipf_weights(num_apps, zipf_s)
    return rng.choices(range(num_apps), weights=weights, k=num_sessions)


def shared_system_prompt(
    num_apps: int = 6,
    zipf_s: float = 1.1,
    system_prompt_tokens: int = 2000,
    sessions: int = 30,
    turns: int = 4,
    seed: int = 1,
    max_tokens: int = 128,
    ttft_slo_ms: float = 500.0,
    tpot_slo_ms: float = 50.0,
    user_turn_tokens: int = 40,
) -> Workload:
    """Multi-tenant "shared system prompt" workload (DistServe/Preble-style):
    ``num_apps`` distinct long system prompts (e.g. distinct customers each
    with their own instructions/few-shot examples baked into a system
    message), and each of ``sessions`` conversations is Zipf-assigned to
    one app -- so a handful of apps get most of the sessions, and every
    session sharing an app shares that app's exact system-prompt prefix.
    A router that recognizes and exploits that shared prefix (routing
    same-app sessions to the same replica) should show a materially higher
    true cache-hit ratio than one that doesn't."""
    rng = random.Random(seed)
    system_prompts = [_generate_text(system_prompt_tokens, rng) for _ in range(num_apps)]
    app_assignment = _assign_apps_by_zipf(sessions, num_apps, zipf_s, rng)

    session_requests = []
    for i, app_id in enumerate(app_assignment):
        session_id = f"shared-{seed}-{i}"
        turn_messages = [
            [
                {"role": "system", "content": system_prompts[app_id]},
                {"role": "user", "content": _generate_text(user_turn_tokens, rng)},
            ]
        ]
        for _ in range(1, turns):
            turn_messages.append([{"role": "user", "content": _generate_text(user_turn_tokens, rng)}])
        session_requests.append(
            SessionRequests(
                session_id=session_id,
                turns=turn_messages,
                max_tokens=max_tokens,
                ttft_slo_ms=ttft_slo_ms,
                tpot_slo_ms=tpot_slo_ms,
                app_id=f"app-{app_id}",
            )
        )
    return Workload(sessions=session_requests, ttft_slo_ms=ttft_slo_ms, tpot_slo_ms=tpot_slo_ms)


def long_document_qa(
    num_docs: int = 5,
    doc_tokens: int = 3000,
    questions_per_doc: int = 6,
    seed: int = 1,
    max_tokens: int = 128,
    ttft_slo_ms: float = 500.0,
    tpot_slo_ms: float = 50.0,
    question_tokens: int = 30,
) -> Workload:
    """Long-document Q&A workload: ``num_docs`` distinct long documents,
    each attached as the system message of ``questions_per_doc`` separate
    single-turn sessions asking different questions about it -- the other
    classic prefix-sharing shape (DistServe's own benchmark suite), distinct
    from shared_system_prompt in that here the shared prefix is one big
    document rather than a short reusable instruction set, and each
    "app" (document) always gets exactly the same number of sessions
    rather than a Zipf-skewed count."""
    rng = random.Random(seed)
    documents = [_generate_text(doc_tokens, rng) for _ in range(num_docs)]

    session_requests = []
    for doc_idx, document in enumerate(documents):
        for q in range(questions_per_doc):
            session_id = f"longdoc-{seed}-{doc_idx}-{q}"
            turn_messages = [
                [
                    {"role": "system", "content": document},
                    {"role": "user", "content": _generate_text(question_tokens, rng)},
                ]
            ]
            session_requests.append(
                SessionRequests(
                    session_id=session_id,
                    turns=turn_messages,
                    max_tokens=max_tokens,
                    ttft_slo_ms=ttft_slo_ms,
                    tpot_slo_ms=tpot_slo_ms,
                    app_id=f"doc-{doc_idx}",
                )
            )
    return Workload(sessions=session_requests, ttft_slo_ms=ttft_slo_ms, tpot_slo_ms=tpot_slo_ms)


def sharegpt_multiturn(
    path: str,
    sessions: int = 30,
    max_turns: int = 4,
    seed: int = 1,
    max_tokens: int = 128,
    ttft_slo_ms: float = 500.0,
    tpot_slo_ms: float = 50.0,
) -> Workload:
    """Real multi-turn conversation traces loaded from a local ShareGPT-format
    JSON file (a list of ``{"conversations": [{"from": "human"|"gpt",
    "value": "..."}]}`` records, the format ShareGPT dumps and most
    "real multi-turn trace" LLM-serving benchmarks use as-is). Requires an
    actual file on disk -- there's no synthetic stand-in for "real human
    conversation shape", so this raises clearly rather than silently
    falling back to generated text if no path is given."""
    if not path:
        raise ValueError(
            "sharegpt_multiturn requires --workload-sharegpt-path pointing at a local "
            "ShareGPT-format JSON file; there is no synthetic substitute for real "
            "multi-turn conversation traces"
        )
    file_path = Path(path)
    if not file_path.is_file():
        raise FileNotFoundError(f"ShareGPT trace file not found: {path}")
    with file_path.open() as f:
        records = json.load(f)
    if not isinstance(records, list) or not records:
        raise ValueError(f"{path} does not look like a ShareGPT trace (expected a non-empty JSON list)")

    rng = random.Random(seed)
    role_map = {"human": "user", "gpt": "assistant", "system": "system"}

    session_requests = []
    for i in range(sessions):
        record = records[i % len(records)]
        conv = record.get("conversations", [])
        # Only human turns become new user messages this workload sends;
        # any "gpt" turns in the source trace are discarded rather than
        # used as scripted assistant replies, for the same reason given
        # in the module docstring -- the runner supplies the real,
        # live-streamed reply instead.
        human_turns = [turn["value"] for turn in conv if role_map.get(turn.get("from")) == "user"]
        human_turns = human_turns[:max_turns] or [_generate_text(30, rng)]

        session_id = f"sharegpt-{seed}-{i}"
        turn_messages = [[{"role": "user", "content": human_turns[0]}]]
        for value in human_turns[1:]:
            turn_messages.append([{"role": "user", "content": value}])
        session_requests.append(
            SessionRequests(
                session_id=session_id,
                turns=turn_messages,
                max_tokens=max_tokens,
                ttft_slo_ms=ttft_slo_ms,
                tpot_slo_ms=tpot_slo_ms,
                app_id="sharegpt",
            )
        )
    return Workload(sessions=session_requests, ttft_slo_ms=ttft_slo_ms, tpot_slo_ms=tpot_slo_ms)


# The original 5-canned-prompts workload from both scripts, kept verbatim
# (same prompt list, same random.choice-per-turn behavior) as a regression
# baseline: anything that changed benchmark numbers on this workload would
# mean the harness itself broke, not that a workload got harder/easier.
_TINY_PROMPTS = [
    "Summarize the plot of a story about a lighthouse keeper.",
    "What are three ways to improve a Python function's performance?",
    "Explain the difference between TCP and UDP in one paragraph.",
    "Give me a recipe idea using chickpeas and spinach.",
    "Write a short haiku about autumn rain.",
]


def tiny_prompts(
    sessions: int = 30,
    turns: int = 4,
    seed: int = 1,
    max_tokens: int = 128,
    ttft_slo_ms: float = 500.0,
    tpot_slo_ms: float = 50.0,
) -> Workload:
    """The original canned-prompts workload (5 fixed prompts, uniformly
    random per turn), kept as-is for regression comparison against the
    new workloads above."""
    rng = random.Random(seed)
    session_requests = []
    for i in range(sessions):
        session_id = f"tiny-{seed}-{i}"
        turn_messages = [[{"role": "user", "content": rng.choice(_TINY_PROMPTS)}] for _ in range(turns)]
        session_requests.append(
            SessionRequests(
                session_id=session_id,
                turns=turn_messages,
                max_tokens=max_tokens,
                ttft_slo_ms=ttft_slo_ms,
                tpot_slo_ms=tpot_slo_ms,
                app_id="tiny",
            )
        )
    return Workload(sessions=session_requests, ttft_slo_ms=ttft_slo_ms, tpot_slo_ms=tpot_slo_ms)


_WORKLOAD_BUILDERS: dict[str, object] = {
    "shared_system": shared_system_prompt,
    "long_doc": long_document_qa,
    "sharegpt": sharegpt_multiturn,
    "tiny": tiny_prompts,
}


@dataclass
class WorkloadArgs:
    """CLI-facing knobs, one field per --workload-* flag across all
    generators; each builder only reads the subset it needs. Kept as a
    single dataclass (rather than one argparse subparser tree per
    workload) so scripts/benchmark.py's `run`/`goodput` subcommands can
    expose all workloads under one flat --workload-* flag surface."""

    workload: str = "tiny"
    num_apps: int = 6
    zipf_s: float = 1.1
    system_prompt_tokens: int = 2000
    num_docs: int = 5
    doc_tokens: int = 3000
    questions_per_doc: int = 6
    sharegpt_path: str = ""
    sessions: int = 30
    turns: int = 4
    seed: int = 1
    max_tokens: int = 128
    ttft_slo_ms: float = 500.0
    tpot_slo_ms: float = 50.0
    sla_ms: float = 3000.0
    _: None = field(default=None, repr=False, compare=False)


def build_workload(args: WorkloadArgs) -> Workload:
    """Dispatches to the right generator by name, translating the shared
    WorkloadArgs into each builder's own keyword arguments."""
    if args.workload == "shared_system":
        return shared_system_prompt(
            num_apps=args.num_apps, zipf_s=args.zipf_s, system_prompt_tokens=args.system_prompt_tokens,
            sessions=args.sessions, turns=args.turns, seed=args.seed, max_tokens=args.max_tokens,
            ttft_slo_ms=args.ttft_slo_ms, tpot_slo_ms=args.tpot_slo_ms,
        )
    if args.workload == "long_doc":
        return long_document_qa(
            num_docs=args.num_docs, doc_tokens=args.doc_tokens, questions_per_doc=args.questions_per_doc,
            seed=args.seed, max_tokens=args.max_tokens, ttft_slo_ms=args.ttft_slo_ms, tpot_slo_ms=args.tpot_slo_ms,
        )
    if args.workload == "sharegpt":
        return sharegpt_multiturn(
            path=args.sharegpt_path, sessions=args.sessions, max_turns=args.turns, seed=args.seed,
            max_tokens=args.max_tokens, ttft_slo_ms=args.ttft_slo_ms, tpot_slo_ms=args.tpot_slo_ms,
        )
    if args.workload == "tiny":
        return tiny_prompts(
            sessions=args.sessions, turns=args.turns, seed=args.seed, max_tokens=args.max_tokens,
            ttft_slo_ms=args.ttft_slo_ms, tpot_slo_ms=args.tpot_slo_ms,
        )
    raise ValueError(f"unknown workload {args.workload!r}; choose one of {sorted(_WORKLOAD_BUILDERS)}")
