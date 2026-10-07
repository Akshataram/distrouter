"""Turning a chat-completion request into the exact token sequence vLLM sees.

This exists because of one fact that invalidates character-level prefix
matching entirely: vLLM does not cache *messages*, it caches **blocks of
tokens of the rendered chat template**. By the time vLLM looks for a cache
hit, `[m1, m2, m3]` has been flattened into one flat token sequence --

    <|im_start|>system\\n{m1}<|im_end|>\\n<|im_start|>user\\n{m2}<|im_end|>\\n...

-- chopped into fixed-size blocks, and hashed. Message boundaries do not
survive, and nothing about a message's *text* tells you how many *blocks*
two requests actually share. (See the worked cases in ARCHITECTURE.md: a
system prompt differing by one character at the end shares ~99% of its
blocks, while one differing by a character at the start shares none --
indistinguishable to any message-level comparison.)

So the router must tokenize the same way vLLM does, or its cache
predictions are guesses about the wrong quantity.

Two implementations:

- ``HFChatTokenizer`` -- the real thing: `transformers.AutoTokenizer` with
  `apply_chat_template(..., add_generation_prompt=True)`, which is exactly
  what vLLM's OpenAI server calls internally. **Required for a deployment
  whose cache predictions you intend to trust**, and the only option that
  makes the router's block hashes line up with the engine's real cache.
  `transformers` is an optional dependency, imported lazily, so the router
  and its tests never require it.

- ``ByteChunkTokenizer`` -- the fallback, and what the test suite uses:
  renders messages with the same template *shape*, UTF-8 encodes, and
  treats every 4 bytes as one pseudo-token. No model download, no network,
  deterministic. It reproduces the structural properties that matter for
  testing the index (same text -> same tokens, a change at position k
  invalidates every block after k, block boundaries fall where they fall)
  **without** claiming to match any real tokenizer's output. A deployment
  using this fallback has a systematically wrong view of block boundaries
  and will mispredict; it exists so the routing logic is testable and so
  the router degrades to something honest rather than crashing when
  `transformers` isn't installed.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

# vLLM's own default; must match the replica's `--block-size` flag, since a
# router hashing 16-token blocks against an engine caching 32-token blocks
# shares no hashes at all.
DEFAULT_BLOCK_SIZE = 16

_BYTES_PER_PSEUDO_TOKEN = 4


@runtime_checkable
class Tokenizer(Protocol):
    """Anything that can turn a message list into the token ids vLLM would
    see for the same request."""

    name: str

    def encode_chat(self, messages: list[dict]) -> list[int]: ...


def render_chat_fallback(messages: list[dict]) -> str:
    """ChatML-shaped rendering used by ByteChunkTokenizer.

    Deliberately mimics the *shape* of Qwen/ChatML templates (role markers,
    one flattened string, a trailing generation prompt) rather than any
    specific model's exact template -- the point is that the structural
    consequences are the same: messages are concatenated into one sequence
    and the generation prompt lands at the end."""
    parts = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = message.get("role", "user")
        content = message.get("content", "")
        if isinstance(content, (list, dict)):
            # Multimodal content parts: stringify canonically. The real
            # tokenizer expands these into image placeholder tokens, so
            # the fallback's view of a multimodal request is especially
            # approximate -- flagged rather than hidden.
            import json

            content = json.dumps(content, sort_keys=True, default=str)
        parts.append(f"<|im_start|>{role}\n{content}<|im_end|>\n")
    return "".join(parts) + "<|im_start|>assistant\n"


class ByteChunkTokenizer:
    """Deterministic, download-free stand-in. Every 4 UTF-8 bytes of the
    rendered chat string become one pseudo-token id.

    Pseudo-token ids are content-derived (a stable hash of the 4-byte
    chunk), not positional -- a positional scheme would make any two
    equal-length prompts look identical to the block hasher, which is the
    one property that must not be faked."""

    name = "byte-chunk-fallback"

    def encode_chat(self, messages: list[dict]) -> list[int]:
        import hashlib

        raw = render_chat_fallback(messages).encode("utf-8")
        return [
            int.from_bytes(
                hashlib.blake2b(raw[i : i + _BYTES_PER_PSEUDO_TOKEN], digest_size=4).digest(), "big"
            )
            for i in range(0, len(raw), _BYTES_PER_PSEUDO_TOKEN)
        ]


class HFChatTokenizer:
    """Real tokenization via `transformers.AutoTokenizer`, applying the
    model's own chat template -- the same call path vLLM's OpenAI server
    uses, so the token sequence (and therefore the block hashes) matches
    what the engine actually caches.

    `transformers` is imported lazily on first use so that importing this
    module, and the entire test suite, never needs it."""

    def __init__(self, model_name_or_path: str):
        self.model_name_or_path = model_name_or_path
        self.name = f"hf:{model_name_or_path}"
        self._tokenizer = None

    def _load(self):
        if self._tokenizer is None:
            try:
                from transformers import AutoTokenizer
            except ImportError as exc:  # pragma: no cover - depends on optional dep
                raise RuntimeError(
                    "HFChatTokenizer needs `transformers` installed (pip install transformers). "
                    "Without it the router cannot reproduce the token sequence vLLM caches on; "
                    "set SWIFTSERVE_TOKENIZER='' to fall back to ByteChunkTokenizer, accepting "
                    "that its cache predictions will be systematically wrong."
                ) from exc
            self._tokenizer = AutoTokenizer.from_pretrained(self.model_name_or_path)
        return self._tokenizer

    def encode_chat(self, messages: list[dict]) -> list[int]:
        tokenizer = self._load()
        tokens = tokenizer.apply_chat_template(
            messages, tokenize=True, add_generation_prompt=True
        )
        # Some versions return a BatchEncoding / nested list depending on
        # the template; normalize to a flat list of ints.
        if tokens and isinstance(tokens[0], list):
            tokens = tokens[0]
        return list(tokens)


def build_tokenizer(model_name: str, tokenizer_spec: str | None) -> Tokenizer:
    """`tokenizer_spec` is SWIFTSERVE_TOKENIZER: a model id/path for real
    tokenization, or empty to accept the byte fallback. Empty is the
    default so the router starts anywhere, but it is the *degraded* mode --
    callers should surface which one is live (the router logs it at
    startup and reports it in /status)."""
    if tokenizer_spec:
        return HFChatTokenizer(tokenizer_spec)
    return ByteChunkTokenizer()
