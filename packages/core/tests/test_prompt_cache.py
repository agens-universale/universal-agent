"""Разметка prompt cache для claude-моделей: предикат по id и разметка payload
(спека docs/specs/2026-09-07-prompt-cache-design.md §4)."""
from copy import deepcopy

import pytest

from gc_core.utils.prompt_cache import (
    CACHE_CONTROL,
    apply_prompt_cache,
    is_prompt_cache_model,
    prompt_cache_enabled,
)


@pytest.mark.parametrize(
    "model_id",
    ["anthropic/claude-opus-5", "Claude-Sonnet-5@2026", "us.anthropic.claude-x", "claude"],
)
def test_predicate_true_for_claude_ids(model_id):
    assert is_prompt_cache_model(model_id) is True


@pytest.mark.parametrize(
    "model_id",
    ["google/gemini-2.5-flash", "z-ai/glm-5.3", "GigaCode", "anthropic/other", None, "", 42],
)
def test_predicate_false_otherwise(model_id):
    assert is_prompt_cache_model(model_id) is False


def _steady_payload() -> dict:
    """Установившийся режим: system, user, assistant(tool_calls), tool, tool."""
    return {
        "model": "anthropic/claude-opus-5",
        "messages": [
            {"role": "system", "content": "You are an agent.", "name": "sys"},
            {"role": "user", "content": "do it"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "type": "function",
                                                                 "function": {"name": "fs_read", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "out-1"},
            {"role": "tool", "tool_call_id": "c1", "content": "out-2"},
        ],
        "tools": [{"type": "function", "function": {"name": "fs_read", "parameters": {"type": "object"}}}],
        "tool_choice": "auto",
        "max_tokens": 100,
    }


def test_marks_first_system_and_last_tail_message():
    payload = _steady_payload()
    result = apply_prompt_cache(payload)
    assert "cache_control" not in result
    assert result["messages"][0] == {
        "role": "system",
        "name": "sys",
        "content": [{"type": "text", "text": "You are an agent.", "cache_control": {"type": "ephemeral"}}],
    }
    assert result["messages"][-1] == {
        "role": "tool",
        "tool_call_id": "c1",
        "content": [{"type": "text", "text": "out-2", "cache_control": {"type": "ephemeral"}}],
    }


def test_middle_messages_are_the_same_objects_and_strings():
    payload = _steady_payload()
    result = apply_prompt_cache(payload)
    for original, marked in zip(payload["messages"][1:-1], result["messages"][1:-1]):
        assert marked is original
        assert isinstance(marked["content"], str)


def test_first_call_marks_system_and_user():
    payload = {"messages": [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]}
    result = apply_prompt_cache(payload)
    assert isinstance(result["messages"][0]["content"], list)
    assert result["messages"][1]["content"] == [
        {"type": "text", "text": "hi", "cache_control": {"type": "ephemeral"}}
    ]


def test_tail_skips_assistant_and_blank_messages():
    payload = _steady_payload()
    payload["messages"].append({"role": "assistant", "content": "final text"})
    payload["messages"].append({"role": "tool", "tool_call_id": "c9", "content": "  "})
    result = apply_prompt_cache(payload)
    assert isinstance(result["messages"][-1]["content"], str)
    assert isinstance(result["messages"][-2]["content"], str)
    assert result["messages"][-3]["content"][0]["text"] == "out-2"
    assert "cache_control" in result["messages"][-3]["content"][0]


def test_input_payload_is_not_mutated():
    payload = _steady_payload()
    snapshot = deepcopy(payload)
    apply_prompt_cache(payload)
    assert payload == snapshot


def test_tools_and_other_keys_untouched():
    payload = _steady_payload()
    result = apply_prompt_cache(payload)
    assert result["tools"] is payload["tools"]
    assert result["tool_choice"] == "auto"
    assert result["max_tokens"] == 100
    assert result["model"] == "anthropic/claude-opus-5"


@pytest.mark.parametrize("system_content", ["", "   \n\t", None, [{"type": "text", "text": "x"}]])
def test_system_not_candidate_keeps_message_but_tail_is_marked(system_content):
    payload = _steady_payload()
    payload["messages"][0] = {"role": "system", "content": system_content}
    result = apply_prompt_cache(payload)
    assert result["messages"][0] is payload["messages"][0]
    assert isinstance(result["messages"][-1]["content"], list)


def test_only_first_system_is_marked():
    payload = _steady_payload()
    payload["messages"].insert(1, {"role": "system", "content": "second system"})
    result = apply_prompt_cache(payload)
    assert isinstance(result["messages"][0]["content"], list)
    assert result["messages"][1] is payload["messages"][1]


def test_no_candidates_returns_equal_payload():
    payload = {"model": "anthropic/claude-opus-5", "messages": [{"role": "assistant", "content": ""}]}
    assert apply_prompt_cache(payload) == payload
    assert apply_prompt_cache({"model": "anthropic/claude-opus-5"}) == {"model": "anthropic/claude-opus-5"}


def _strip_marks(marked: dict) -> dict:
    """Оракул: снять разметку — должен получиться исходный payload."""
    plain = dict(marked)
    messages = []
    for m in plain.get("messages", []):
        content = m.get("content")
        if isinstance(content, list) and len(content) == 1 and "cache_control" in content[0]:
            m = {**m, "content": content[0]["text"]}
        messages.append(m)
    if "messages" in plain:
        plain["messages"] = messages
    return plain


def test_stripping_marks_restores_original_payload():
    payload = _steady_payload()
    assert _strip_marks(apply_prompt_cache(payload)) == payload


def test_cache_control_constant_is_ephemeral_without_ttl():
    assert CACHE_CONTROL == {"type": "ephemeral"}


@pytest.mark.parametrize("setting,model_id,expected", [
    (None, "anthropic/claude-opus-5", True),        # не задано — авто по id
    (None, "google/gemini-2.5-flash", False),
    (True, "google/gemini-2.5-flash", True),
    (False, "anthropic/claude-opus-5", False),
    (0, "anthropic/claude-opus-5", False),          # CLI --claude_prompt_cache 0 → int мимо схемы
    (1, "google/gemini-2.5-flash", True),           # CLI --claude_prompt_cache 1 → int мимо схемы
    ("disabled", "anthropic/claude-opus-5", True),  # незнакомая строка — как не задано (авто)
    ("disabled", "google/gemini-2.5-flash", False), # …и никогда не включает принудительно
    ("auto", "google/gemini-2.5-flash", False),
])
def test_prompt_cache_enabled_setting_beats_autodetect(setting, model_id, expected):
    assert prompt_cache_enabled(setting, model_id) is expected
