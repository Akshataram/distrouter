"""Tests for the tokenizer layer.

`transformers` is deliberately NOT a test dependency, so these tests cover
the download-free fallback plus the contract that the real tokenizer fails
loudly (and informatively) rather than silently degrading when it isn't
installed."""

from __future__ import annotations

import pytest

from swiftserve.tokenization import (
    ByteChunkTokenizer,
    HFChatTokenizer,
    build_tokenizer,
    render_chat_fallback,
)

MESSAGES = [
    {"role": "system", "content": "You are a helpful support agent."},
    {"role": "user", "content": "reset my password"},
]


def test_fallback_is_deterministic():
    tok = ByteChunkTokenizer()
    assert tok.encode_chat(MESSAGES) == tok.encode_chat(MESSAGES)


def test_fallback_token_ids_depend_on_content_not_just_position():
    """The property that must not be faked: two prompts of identical length
    but different text must tokenize differently, or the block hasher sees
    every equal-length prompt as the same prompt."""
    tok = ByteChunkTokenizer()
    a = tok.encode_chat([{"role": "user", "content": "aaaabbbbccccdddd"}])
    b = tok.encode_chat([{"role": "user", "content": "aaaabbbbccccdddX"}])
    assert len(a) == len(b)
    assert a != b


def test_fallback_identical_prefix_yields_identical_leading_tokens():
    tok = ByteChunkTokenizer()
    shared = "You are a helpful support agent. " * 10
    a = tok.encode_chat([{"role": "system", "content": shared}, {"role": "user", "content": "one"}])
    b = tok.encode_chat([{"role": "system", "content": shared}, {"role": "user", "content": "two"}])
    # The rendered system message is byte-identical, so a long run of
    # leading pseudo-tokens must agree -- this is what makes a block-level
    # prefix match possible at all.
    common = sum(1 for x, y in zip(a, b, strict=False) if x == y)
    assert common > 50


def test_fallback_longer_prompt_yields_more_tokens():
    tok = ByteChunkTokenizer()
    short = tok.encode_chat([{"role": "user", "content": "hi"}])
    long = tok.encode_chat([{"role": "user", "content": "hi " * 500}])
    assert len(long) > len(short)


def test_render_includes_role_markers_and_generation_prompt():
    rendered = render_chat_fallback(MESSAGES)
    assert "<|im_start|>system" in rendered
    assert "<|im_start|>user" in rendered
    assert rendered.endswith("<|im_start|>assistant\n")


def test_render_flattens_messages_into_one_sequence():
    # The property that invalidates message-level matching: message
    # boundaries do not survive rendering as anything a comparison can key on.
    rendered = render_chat_fallback(MESSAGES)
    assert "You are a helpful support agent." in rendered
    assert "reset my password" in rendered
    assert rendered.count("<|im_start|>") == 3  # system, user, assistant prompt


def test_multimodal_list_content_does_not_crash():
    tok = ByteChunkTokenizer()
    messages = [{"role": "user", "content": [{"type": "text", "text": "what is this"}]}]
    assert len(tok.encode_chat(messages)) > 0


def test_non_dict_message_entries_are_skipped():
    tok = ByteChunkTokenizer()
    assert len(tok.encode_chat(["not a dict", {"role": "user", "content": "hi"}])) > 0


def test_empty_messages_still_render_the_generation_prompt():
    tok = ByteChunkTokenizer()
    assert len(tok.encode_chat([])) > 0


def test_build_tokenizer_returns_fallback_for_empty_spec():
    tok = build_tokenizer("Qwen/Qwen2.5-3B-Instruct", "")
    assert isinstance(tok, ByteChunkTokenizer)
    assert tok.name == "byte-chunk-fallback"


def test_build_tokenizer_returns_hf_for_a_named_model():
    tok = build_tokenizer("Qwen/Qwen2.5-3B-Instruct", "Qwen/Qwen2.5-3B-Instruct")
    assert isinstance(tok, HFChatTokenizer)
    assert tok.name == "hf:Qwen/Qwen2.5-3B-Instruct"


def test_hf_tokenizer_constructs_without_transformers_installed():
    """Construction must be lazy -- the router should start even on a host
    with no `transformers`, and only fail when it actually needs to encode."""
    HFChatTokenizer("Qwen/Qwen2.5-3B-Instruct")


def test_hf_tokenizer_raises_an_actionable_error_when_transformers_is_missing():
    try:
        import transformers  # noqa: F401
    except ImportError:
        pass
    else:
        pytest.skip("transformers is installed, so the missing-dependency path can't be exercised")

    with pytest.raises(RuntimeError, match="transformers"):
        HFChatTokenizer("Qwen/Qwen2.5-3B-Instruct").encode_chat(MESSAGES)


# -- the return-shape bug that cost a 4-GPU experiment run -----------------


class _FakeHF:
    """Stands in for transformers.AutoTokenizer with a configurable return
    shape, so every version's behaviour is covered without the dependency."""

    def __init__(self, result):
        self._result = result

    def apply_chat_template(self, messages, tokenize, add_generation_prompt, return_dict=False):
        return self._result


def _hf_with(result):
    tok = HFChatTokenizer("fake/model")
    tok._tokenizer = _FakeHF(result)
    return tok


def test_flat_list_of_ids_passes_through():
    assert _hf_with(list(range(40))).encode_chat(MESSAGES) == list(range(40))


def test_batch_of_one_nested_list_is_unwrapped():
    assert _hf_with([list(range(40))]).encode_chat(MESSAGES) == list(range(40))


def test_batch_encoding_mapping_uses_input_ids():
    """The real failure: newer transformers returns a BatchEncoding, and
    list() on it yields its KEYS -- two strings, i.e. zero full blocks, so
    the prefix index matches nothing and the router silently becomes a load
    balancer."""
    mapping = {"input_ids": list(range(40)), "attention_mask": [1] * 40}
    assert _hf_with(mapping).encode_chat(MESSAGES) == list(range(40))


def test_mapping_without_input_ids_raises():
    with pytest.raises(RuntimeError, match="without 'input_ids'"):
        _hf_with({"attention_mask": [1, 2, 3]}).encode_chat(MESSAGES)


def test_string_keys_are_rejected_rather_than_hashed_as_tokens():
    with pytest.raises(RuntimeError, match="integer token ids"):
        _hf_with(["input_ids", "attention_mask"]).encode_chat(MESSAGES)


def test_empty_token_sequence_raises():
    with pytest.raises(RuntimeError, match="empty token sequence"):
        _hf_with([]).encode_chat(MESSAGES)
