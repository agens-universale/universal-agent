import types

from summarize.dialog_history import (
    build_history_text,
    group_into_turns,
    message_text_limited,
    split_turns_into_char_batches,
    trim_history_to_max_dialogue_chars,
    trim_original_user_messages_to_max_chars,
)
from summarize.compaction import apply_summary_state_patch
from summarize.thread_summarizer import summarize_dialog_via_chat_completions
from user_message_policy import TECHNICAL_TYPE_DIALOG_SUMMARY, wrap_technical_user_content


def _ctx(trigger_chars: int = 120000):
    incremental_batch_chars = int(trigger_chars * 1.4) if trigger_chars > 0 else 0
    return types.SimpleNamespace(
        config_dict={
            "model": "test-model",
            "completions_url": "http://example.test/v1/chat/completions",
            "summary_params": {
                "chat_completions_summary_trigger_chars": trigger_chars,
                "incremental_batch_chars": incremental_batch_chars,
                "max_accumulated_summary_chars": 20000,
                "original_user_messages_max_chars": 20000,
                "last_dialog_steps_count": 5,
                "last_dialog_step_message_max_chars": 5000,
            },
        },
        logger=None,
    )


def _state():
    return types.SimpleNamespace(default_model="test-model")


def _make_turn(user_content: str, assistant_content: str):
    return [
        {"type": "message", "role": "user", "content": user_content},
        {"type": "message", "role": "assistant", "content": assistant_content},
    ]


def _flatten_turns(turns):
    return [e for t in turns for e in t]


def _valid_summary(seed: str) -> str:
    return (
        f"TASK_AND_CONTEXT:\n- {seed} context details and scope.\n\n"
        "GOALS:\n- goal one\n- goal two\n\n"
        "PLAN_AND_PROGRESS:\n- [DONE] baseline done\n- [TODO] next step\n\n"
        "NEXT_ACTIONS:\n- action one\n- action two\n\n"
        "SPEC_AND_CONSTRAINTS:\n- keep API compatibility\n\n"
        "RISKS_AND_OPEN_QUESTIONS:\n- edge cases remain\n\n"
        "IMPORTANT_ARTIFACTS:\n- src/summarize/thread_summarizer.py\n"
    )


def test_summarize_incremental_empty_history_no_model_call(monkeypatch):
    calls = []

    def _never_call(**kwargs):
        calls.append(kwargs)
        return "should_not_happen"

    monkeypatch.setattr("summarize.prompt_loading.load_prompt_template", lambda _p: "prompt")
    monkeypatch.setattr("summarize.llm_chat.request_chat_completion_text", _never_call)
    monkeypatch.setattr("summarize.memory_suggestions.suggest_memory_additions", lambda **_k: "")

    result = summarize_dialog_via_chat_completions(
        ctx=_ctx(),
        state=_state(),
        full_history_messages=[],
        current_memory="",
    )
    assert result["raw_summary"] == ""
    assert result["last_dialog_steps"] == ""
    assert result["num_batches"] == 0
    assert calls == []


def test_summarize_incremental_single_batch_one_summary_call(monkeypatch):
    history = _flatten_turns([_make_turn("first user", "first assistant")])
    calls = []

    def _fake_request(**kwargs):
        calls.append(kwargs)
        return _valid_summary("single-batch")

    monkeypatch.setattr("summarize.prompt_loading.load_prompt_template", lambda _p: "prompt")
    monkeypatch.setattr("summarize.llm_chat.request_chat_completion_text", _fake_request)
    monkeypatch.setattr("summarize.memory_suggestions.suggest_memory_additions", lambda **_k: "")

    result = summarize_dialog_via_chat_completions(
        ctx=_ctx(trigger_chars=999999),
        state=_state(),
        full_history_messages=history,
        current_memory="",
    )
    assert "single-batch" in result["raw_summary"]
    assert result["num_batches"] == 1
    assert len(calls) == 1


def test_memory_additions_go_to_memory_files_journal_not_inside_summary(monkeypatch):
    history = _flatten_turns([_make_turn("hi", "hello")])

    def _fake_request(**kwargs):
        return _valid_summary("with-memory")

    def _fake_memory(**kwargs):
        return "- bullet one\n- bullet two"

    monkeypatch.setattr("summarize.prompt_loading.load_prompt_template", lambda _p: "prompt")
    monkeypatch.setattr("summarize.llm_chat.request_chat_completion_text", _fake_request)
    monkeypatch.setattr("summarize.memory_suggestions.suggest_memory_additions", _fake_memory)

    result = summarize_dialog_via_chat_completions(
        ctx=_ctx(trigger_chars=999999),
        state=_state(),
        full_history_messages=history,
        current_memory="existing note",
    )
    state = types.SimpleNamespace(
        memory_files={"journal": "existing note"},
        messages=[],
        dialog_steps_offset=0,
        previous_response_id=None,
    )
    content = apply_summary_state_patch(
        state=state,
        raw_summary=result["raw_summary"],
        last_dialog_steps=result["last_dialog_steps"],
        memory_additions=result["memory_additions"],
        summarized_messages_count=len(history),
    )
    assert "<add_to_memory>" not in content
    assert "Suggested memory" not in content
    inner = content.split("<summary>")[1].split("</summary>")[0]
    assert "bullet one" not in inner
    journal_block = content.split('<file name="journal">')[1].split("</file>")[0]
    assert "existing note" in journal_block
    assert "bullet one" in journal_block


def test_summarize_incremental_two_batches_previous_summary_passed(monkeypatch):
    history = _flatten_turns(
        [
            _make_turn("user1" + "a" * 3500, "assistant1"),
            _make_turn("user2" + "b" * 3500, "assistant2"),
        ]
    )
    seen_user_prompts = []
    responses = iter([_valid_summary("batch-1"), _valid_summary("batch-2")])

    def _fake_request(**kwargs):
        messages = kwargs["messages"]
        user_content = next((m["content"] for m in messages if m["role"] == "user"), "")
        seen_user_prompts.append(user_content)
        return next(responses)

    memory_calls = []

    def _fake_memory(**kwargs):
        memory_calls.append(kwargs)
        return ""

    monkeypatch.setattr("summarize.prompt_loading.load_prompt_template", lambda _p: "prompt")
    monkeypatch.setattr("summarize.llm_chat.request_chat_completion_text", _fake_request)
    monkeypatch.setattr("summarize.memory_suggestions.suggest_memory_additions", _fake_memory)

    result = summarize_dialog_via_chat_completions(
        ctx=_ctx(trigger_chars=4500),
        state=_state(),
        full_history_messages=history,
        current_memory="notes",
    )
    assert result["num_batches"] == 2
    assert "batch-2" in result["raw_summary"]
    assert len(seen_user_prompts) == 2
    assert "batch-1" in seen_user_prompts[1]
    # Для multi-batch второй такт memory updates не вызываем.
    assert memory_calls == []


def test_batches_respect_turn_boundaries():
    history = [
        {"type": "message", "role": "user", "content": "u"},
        {"type": "function_call", "role": "assistant", "call_id": "id1", "name": "tool_a", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "id1", "output": "ok1"},
        {"type": "message", "role": "assistant", "content": "done"},
    ]
    turns = group_into_turns(history)
    # Ожидаем: [user], [function_call+function_call_output], [assistant]
    assert len(turns) == 3
    assert len(turns[1]) == 2
    batches = split_turns_into_char_batches(turns, batch_chars_limit=20)
    # Даже при маленьком лимите второй turn не режется на части.
    assert any(len(turn) == 2 for batch in batches for turn in batch)


def _turns_reconstruct_history(turns):
    return [msg for turn in turns for msg in turn]


def _assert_turns_preserve_all_messages(turns, history):
    flat = _turns_reconstruct_history(turns)
    assert len(flat) == len(history)
    for msg in history:
        assert msg in flat


def test_group_into_turns_user_tool_tool_result_assistant():
    history = [
        {"type": "message", "role": "user", "content": "сделай X"},
        {"type": "function_call", "role": "assistant", "call_id": "id1", "name": "tool_a", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "id1", "output": "ok1"},
        {"type": "function_call", "role": "assistant", "call_id": "id2", "name": "tool_b", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "id2", "output": "ok2"},
        {"type": "message", "role": "assistant", "content": "Готово, сделал X."},
    ]
    turns = group_into_turns(history)
    assert _turns_reconstruct_history(turns) == history
    assert len(turns) == 4
    assert len(turns[0]) == 1 and turns[0][0].get("role") == "user"
    assert len(turns[1]) == 2 and turns[1][0].get("type") == "function_call"
    assert len(turns[2]) == 2 and turns[2][0].get("type") == "function_call"
    assert len(turns[3]) == 1 and turns[3][0].get("role") == "assistant"


def test_group_into_turns_tool_call_user_tool_result():
    history = [
        {"type": "function_call", "call_id": "cid1", "name": "t", "arguments": "{}"},
        {"type": "message", "role": "user", "content": "подожди"},
        {"type": "function_call_output", "call_id": "cid1", "output": "done"},
    ]
    turns = group_into_turns(history)
    _assert_turns_preserve_all_messages(turns, history)
    assert len(turns) == 2
    assert len(turns[0]) == 2
    assert turns[0][0].get("call_id") == turns[0][1].get("call_id") == "cid1"
    assert len(turns[1]) == 1 and turns[1][0].get("role") == "user"


def test_group_into_turns_tool1_tool2_result1_result2():
    history = [
        {"type": "function_call", "call_id": "id1", "name": "tool_a", "arguments": "{}"},
        {"type": "function_call", "call_id": "id2", "name": "tool_b", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "id1", "output": "res1"},
        {"type": "function_call_output", "call_id": "id2", "output": "res2"},
    ]
    turns = group_into_turns(history)
    _assert_turns_preserve_all_messages(turns, history)
    assert len(turns) == 2
    assert turns[0][0].get("call_id") == turns[0][1].get("call_id") == "id1"
    assert turns[1][0].get("call_id") == turns[1][1].get("call_id") == "id2"


def test_group_into_turns_chain_user_assistant_user_assistant():
    history = [
        {"type": "message", "role": "user", "content": "q1"},
        {"type": "message", "role": "assistant", "content": "a1"},
        {"type": "message", "role": "user", "content": "q2"},
        {"type": "message", "role": "assistant", "content": "a2"},
    ]
    turns = group_into_turns(history)
    assert _turns_reconstruct_history(turns) == history
    assert len(turns) == 2
    assert len(turns[0]) == 2 and len(turns[1]) == 2


def test_group_into_turns_chain_fc1_user_fc2_fco1_fco2():
    history = [
        {"type": "function_call", "call_id": "id1", "name": "t1", "arguments": "{}"},
        {"type": "message", "role": "user", "content": "стоп"},
        {"type": "function_call", "call_id": "id2", "name": "t2", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "id1", "output": "r1"},
        {"type": "function_call_output", "call_id": "id2", "output": "r2"},
    ]
    turns = group_into_turns(history)
    _assert_turns_preserve_all_messages(turns, history)
    assert len(turns) == 3
    assert turns[0][0].get("call_id") == turns[0][1].get("call_id") == "id1"
    assert len(turns[1]) == 1 and turns[1][0].get("role") == "user"
    assert turns[2][0].get("call_id") == turns[2][1].get("call_id") == "id2"


def test_group_into_turns_empty_and_orphans():
    assert group_into_turns([]) == []

    orphan_fc = [{"type": "function_call", "call_id": "orphan", "name": "tool", "arguments": "{}"}]
    turns_fc = group_into_turns(orphan_fc)
    assert len(turns_fc) == 1 and len(turns_fc[0]) == 1

    orphan_fco = [{"type": "function_call_output", "call_id": "x", "output": "y"}]
    turns_fco = group_into_turns(orphan_fco)
    assert len(turns_fco) == 1 and len(turns_fco[0]) == 1


def test_message_text_limited_truncates_to_max_message_chars():
    msg = {"role": "user", "content": "x" * 60000}
    text = message_text_limited(msg, 50000)
    assert len(text) <= 50000 + len(" ... [TEXT CUTTED]")
    assert text.endswith(" ... [TEXT CUTTED]")


def test_build_history_text_includes_full_dialog_summary_under_per_message_cap():
    payload = "z" * 40000
    content = wrap_technical_user_content(payload, technical_type=TECHNICAL_TYPE_DIALOG_SUMMARY)
    messages = [{"role": "user", "content": content}]
    out = build_history_text(messages, max_chars=0, max_message_chars=500)
    assert payload in out
    assert " ... [TEXT CUTTED]" not in out


def test_trim_history_to_max_dialogue_chars_takes_tail():
    messages = [
        {"role": "user", "content": "a" * 30000},
        {"role": "assistant", "content": "b" * 30000},
        {"role": "user", "content": "c" * 30000},
    ]
    trimmed = trim_history_to_max_dialogue_chars(
        messages,
        max_dialogue_chars=50000,
        max_message_chars=50000,
    )
    assert len(trimmed) == 1
    assert trimmed[0]["content"].startswith("c")


def test_accumulated_summary_is_compressed_when_limit_exceeded(monkeypatch):
    history = _flatten_turns(
        [
            _make_turn("u1" + "a" * 3500, "a1"),
            _make_turn("u2" + "b" * 3500, "a2"),
        ]
    )
    seen_user_prompts = []
    calls = {"n": 0}

    def _fake_request(**kwargs):
        calls["n"] += 1
        messages = kwargs["messages"]
        user_content = next((m["content"] for m in messages if m["role"] == "user"), "")
        seen_user_prompts.append(user_content)
        if calls["n"] == 1:
            return _valid_summary("too-long-before-compress") + ("X" * 200)
        if calls["n"] == 2:
            return _valid_summary("compressed-summary")
        return _valid_summary("final-summary")

    monkeypatch.setattr("summarize.prompt_loading.load_prompt_template", lambda _p: "prompt")
    monkeypatch.setattr("summarize.llm_chat.request_chat_completion_text", _fake_request)
    monkeypatch.setattr("summarize.memory_suggestions.suggest_memory_additions", lambda **_k: "")

    ctx = _ctx(trigger_chars=4500)
    ctx.config_dict["summary_params"]["max_accumulated_summary_chars"] = 50
    result = summarize_dialog_via_chat_completions(
        ctx=ctx,
        state=_state(),
        full_history_messages=history,
        current_memory="",
    )
    assert "final-summary" in result["raw_summary"]
    assert calls["n"] >= 3
    assert any("compressed-summary" in prompt for prompt in seen_user_prompts)


def test_trim_original_user_messages_to_max_chars_takes_tail_whole_messages():
    msgs = ["a" * 10000, "b" * 9000, "c" * 9000]
    trimmed = trim_original_user_messages_to_max_chars(msgs, 20000)
    assert trimmed == ["b" * 9000, "c" * 9000]


def test_build_history_text_strips_original_user_messages_block():
    inner = "body after block"
    raw = (
        "before\n"
        "<original_user_messages>\nLEGACY_DROP_ME\n</original_user_messages>\n\n"
        f"{inner}"
    )
    messages = [{"role": "user", "content": raw}]
    out = build_history_text(messages, max_message_chars=0)
    assert "LEGACY_DROP_ME" not in out
    assert inner in out


def test_build_history_text_keeps_all_rows_and_order():
    messages = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "second"},
        {"role": "user", "content": "third"},
    ]
    out = build_history_text(messages, max_message_chars=0)
    assert "third" in out
    assert "first" in out
    assert out.index("first") < out.index("second") < out.index("third")


def test_build_history_text_max_chars_takes_tail_of_formatted_rows():
    messages = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "second"},
        {"role": "user", "content": "third"},
    ]
    out = build_history_text(messages, max_chars=20, max_message_chars=0)
    assert "third" in out
    assert "first" not in out
    assert out.strip().startswith('<message n="3">')


def test_summarize_pipeline_calls_build_history_text_with_max_chars_disabled(monkeypatch):
    """Батчи уже ограничены split_turns_into_char_batches; второй хвост по строкам не применяем."""
    captured: list[dict] = []

    def wrapped(*args, **kwargs):
        captured.append(dict(kwargs))
        return build_history_text(*args, **kwargs)

    monkeypatch.setattr(
        "summarize.thread_summarizer.dialog_history.build_history_text",
        wrapped,
    )

    def _fake_request(**_kwargs):
        return (
            "TASK_AND_CONTEXT:\n- x\n\n"
            "GOALS:\n- x\n\n"
            "PLAN_AND_PROGRESS:\n- x\n\n"
            "NEXT_ACTIONS:\n- x\n\n"
            "SPEC_AND_CONSTRAINTS:\n- x\n\n"
            "RISKS_AND_OPEN_QUESTIONS:\n- x\n\n"
            "IMPORTANT_ARTIFACTS:\n- x\n"
        )

    monkeypatch.setattr("summarize.prompt_loading.load_prompt_template", lambda _p: "prompt")
    monkeypatch.setattr("summarize.llm_chat.request_chat_completion_text", _fake_request)
    monkeypatch.setattr("summarize.memory_suggestions.suggest_memory_additions", lambda **_k: "")

    history = [
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
    ]
    summarize_dialog_via_chat_completions(
        ctx=_ctx(trigger_chars=999999),
        state=types.SimpleNamespace(default_model="m"),
        full_history_messages=history,
        current_memory="",
    )
    assert captured
    assert all(k.get("max_chars") == 0 for k in captured)


def test_summarize_merges_existing_user_messages_log(monkeypatch):
    history = _flatten_turns([_make_turn("new user request", "assistant reply")])

    def _fake_request(**_kwargs):
        return _valid_summary("single-batch")

    monkeypatch.setattr("summarize.prompt_loading.load_prompt_template", lambda _p: "prompt")
    monkeypatch.setattr("summarize.llm_chat.request_chat_completion_text", _fake_request)
    monkeypatch.setattr("summarize.memory_suggestions.suggest_memory_additions", lambda **_k: "")

    result = summarize_dialog_via_chat_completions(
        ctx=_ctx(trigger_chars=999999),
        state=_state(),
        full_history_messages=history,
        current_memory="",
        user_messages_log=["older user request", "new user request"],
    )
    assert result["user_messages_log"] == [
        "older user request",
        "new user request",
    ]
    state = types.SimpleNamespace(
        memory_files={},
        messages=[],
        dialog_steps_offset=0,
        previous_response_id=None,
    )
    built = apply_summary_state_patch(
        state=state,
        raw_summary=result["raw_summary"],
        last_dialog_steps=result["last_dialog_steps"],
        memory_additions=result["memory_additions"],
        summarized_messages_count=len(history),
    )
    assert "<original_user_messages>" not in built


def test_summarize_preserves_user_messages_log_chronological_order(monkeypatch):
    history = _flatten_turns(
        [
            _make_turn("third user request", "assistant 1"),
            _make_turn("fourth user request", "assistant 2"),
        ]
    )

    def _fake_request(**_kwargs):
        return _valid_summary("single-batch")

    monkeypatch.setattr("summarize.prompt_loading.load_prompt_template", lambda _p: "prompt")
    monkeypatch.setattr("summarize.llm_chat.request_chat_completion_text", _fake_request)
    monkeypatch.setattr("summarize.memory_suggestions.suggest_memory_additions", lambda **_k: "")

    result = summarize_dialog_via_chat_completions(
        ctx=_ctx(trigger_chars=999999),
        state=_state(),
        full_history_messages=history,
        current_memory="",
        user_messages_log=[
            "first user request",
            "second user request",
            "third user request",
            "fourth user request",
        ],
    )

    assert result["user_messages_log"] == [
        "first user request",
        "second user request",
        "third user request",
        "fourth user request",
    ]


def test_summarize_filters_technical_messages_from_user_messages_log(monkeypatch):
    history = _flatten_turns([_make_turn("real request", "assistant")])

    def _fake_request(**_kwargs):
        return _valid_summary("single-batch")

    monkeypatch.setattr("summarize.prompt_loading.load_prompt_template", lambda _p: "prompt")
    monkeypatch.setattr("summarize.llm_chat.request_chat_completion_text", _fake_request)
    monkeypatch.setattr("summarize.memory_suggestions.suggest_memory_additions", lambda **_k: "")

    result = summarize_dialog_via_chat_completions(
        ctx=_ctx(trigger_chars=999999),
        state=_state(),
        full_history_messages=history,
        current_memory="",
        user_messages_log=[
            '<current_context>client has non-empty memory files; call `memory_files` with action="list" to see available files, then read what you need.</current_context>',
            "real request",
        ],
    )
    assert result["user_messages_log"] == ["real request"]


def test_invalid_summary_triggers_one_repair_retry(monkeypatch):
    history = _flatten_turns([_make_turn("u1", "a1")])
    calls = {"n": 0}

    def _fake_request(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return "[assistant] raw transcript"
        return _valid_summary("repaired")

    monkeypatch.setattr("summarize.prompt_loading.load_prompt_template", lambda _p: "prompt")
    monkeypatch.setattr("summarize.llm_chat.request_chat_completion_text", _fake_request)
    monkeypatch.setattr("summarize.memory_suggestions.suggest_memory_additions", lambda **_k: "")

    result = summarize_dialog_via_chat_completions(
        ctx=_ctx(trigger_chars=999999),
        state=_state(),
        full_history_messages=history,
        current_memory="",
    )
    assert "repaired" in result["raw_summary"]
    assert calls["n"] == 2

