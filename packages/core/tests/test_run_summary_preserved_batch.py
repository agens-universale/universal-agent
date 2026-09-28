"""Юнит-тесты ``build_preserved_batch`` и суффикса истории (шаги 2–3 спеки)."""

from __future__ import annotations

from gc_core.message_policy.chat_completions import repair_tool_call_pairing
from gc_core.message_policy.user_message_policy import (
    TECHNICAL_TYPE_CURRENT_CONTEXT,
    wrap_technical_user_content,
)
from gc_core.run_summary_coordinator import (
    build_preserved_batch,
    filter_messages_not_in_state,
    remove_exact_suffix_if_matches,
)
from gc_core.thread_store import make_message_key


def test_user_ask_keeps_prior_non_technical_users_skips_technical():
    tech = wrap_technical_user_content(
        "ctx...", technical_type=TECHNICAL_TYPE_CURRENT_CONTEXT
    )
    current_messages = [
        {"role": "user", "content": "старый смысловой запрос"},
        {"role": "user", "content": tech},
        {"role": "user", "content": "текущий запрос"},
    ]
    batch = build_preserved_batch(
        current_messages=current_messages,
        mode="user_ask",
        state_messages=[],
    )
    assert batch == [current_messages[0], current_messages[-1]]

    batch_incoming_only = build_preserved_batch(
        current_messages=current_messages,
        mode="user_ask",
        state_messages=[],
        message_keys_already_persisted=frozenset(
            {make_message_key(current_messages[0])}
        ),
    )
    assert batch_incoming_only == [current_messages[-1]]


def test_user_ask_multiple_plain_users():
    current_messages = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "hi"},
        {"role": "user", "content": "second"},
        {"role": "user", "content": "third"},
    ]
    batch = build_preserved_batch(
        current_messages=current_messages,
        mode="user_ask",
        state_messages=[],
    )
    assert batch == [
        current_messages[0],
        current_messages[2],
        current_messages[3],
    ]

    batch_incoming = build_preserved_batch(
        current_messages=current_messages,
        mode="user_ask",
        state_messages=[],
        message_keys_already_persisted=frozenset(
            {
                make_message_key(current_messages[0]),
                make_message_key(current_messages[1]),
                make_message_key(current_messages[2]),
            }
        ),
    )
    assert batch_incoming == [current_messages[3]]


def test_user_ask_last_message_technical_still_preserved():
    tech = wrap_technical_user_content(
        "x", technical_type=TECHNICAL_TYPE_CURRENT_CONTEXT
    )
    current_messages = [
        {"role": "user", "content": "norm"},
        {"role": "user", "content": tech},
    ]
    batch = build_preserved_batch(
        current_messages=current_messages,
        mode="user_ask",
        state_messages=[],
    )
    assert batch == [current_messages[0], current_messages[-1]]


def test_function_call_output_pair_in_current_batch():
    call = {
        "type": "function_call",
        "call_id": "c1",
        "name": "fs_read_file",
        "arguments": "{}",
        "id": "fc_1",
    }
    out = {
        "type": "function_call_output",
        "call_id": "c1",
        "output": "{}",
        "id": "fco_1",
    }
    batch = build_preserved_batch(
        current_messages=[call, out],
        mode="function_call_output",
        state_messages=[],
    )
    assert batch == [call, out]


def test_function_call_output_finds_call_earlier_in_state():
    call = {
        "type": "function_call",
        "call_id": "c_old",
        "name": "memory_files",
        "arguments": "{}",
        "id": "fc_old",
    }
    out = {
        "type": "function_call_output",
        "call_id": "c_old",
        "output": "{}",
        "id": "fco",
    }
    state_messages = [{"role": "system", "content": "s"}, call]
    batch = build_preserved_batch(
        current_messages=[out],
        mode="function_call_output",
        state_messages=state_messages,
    )
    assert len(batch) == 2
    assert batch[0] == call
    assert batch[1] == out


def test_function_call_output_skips_orphan():
    out = {
        "type": "function_call_output",
        "call_id": "missing",
        "output": "{}",
        "id": "fco",
    }
    batch = build_preserved_batch(
        current_messages=[out],
        mode="function_call_output",
        state_messages=[],
    )
    assert batch == []


def test_function_call_output_mixed_multiple_outputs_dedupes():
    c1 = {
        "type": "function_call",
        "call_id": "a",
        "name": "t1",
        "arguments": "{}",
        "id": "1",
    }
    o1 = {
        "type": "function_call_output",
        "call_id": "a",
        "output": "1",
        "id": "o1",
    }
    c2 = {
        "type": "function_call",
        "call_id": "b",
        "name": "t2",
        "arguments": "{}",
        "id": "2",
    }
    o2 = {
        "type": "function_call_output",
        "call_id": "b",
        "output": "2",
        "id": "o2",
    }
    o1_dup = {
        "type": "function_call_output",
        "call_id": "a",
        "output": "repeat",
        "id": "o3",
    }
    batch = build_preserved_batch(
        current_messages=[c1, o1, c2, o2, o1_dup],
        mode="function_call_output",
        state_messages=[],
    )
    assert batch == [c1, o1, c2, o2]


def test_function_call_output_pairing_passes_repair():
    msg_list = []
    batch = []
    for i in ("x", "y"):
        call = {
            "type": "function_call",
            "call_id": i,
            "name": "n",
            "arguments": "{}",
            "id": f"f{i}",
        }
        out = {
            "type": "function_call_output",
            "call_id": i,
            "output": "{}",
            "id": f"o{i}",
        }
        msg_list.extend((call, out))
        batch.extend((call, out))
    repaired_full = repair_tool_call_pairing(
        [*msg_list, {"role": "assistant", "content": "later"}],
        keep_latest_unanswered=False,
    )
    assert any(m.get("type") == "function_call_output" for m in repaired_full)
    assert len(
        repair_tool_call_pairing(batch, keep_latest_unanswered=False)
    ) == len(batch)


def test_tool_pair_variants_accepted():
    call = {
        "type": "tool_call",
        "call_id": "t1",
        "name": "n",
        "arguments": "{}",
        "id": "tc",
    }
    out = {
        "type": "tool_call_output",
        "call_id": "t1",
        "output": "{}",
        "id": "to",
    }
    batch = build_preserved_batch(
        current_messages=[call, out],
        mode="function_call_output",
        state_messages=[],
    )
    assert batch == [call, out]


def test_remove_exact_suffix_truncates_matching_tail_only():
    a = {"role": "system", "content": "s"}
    b = {"role": "user", "content": "u1"}
    c = {"role": "user", "content": "u2"}
    full = [a, b, c]
    assert remove_exact_suffix_if_matches(full, [b, c]) is True
    assert full == [a]


def test_remove_exact_suffix_requires_full_tail_match_no_middle_change():
    u1 = {"role": "user", "content": "x"}
    u2 = {"role": "user", "content": "y"}
    full = [u1, u2]
    assert remove_exact_suffix_if_matches(full, [u1]) is False
    assert full == [u1, u2]


def test_remove_exact_suffix_false_when_suffix_longer_than_full():
    assert remove_exact_suffix_if_matches([], [{"a": 1}]) is False


def test_filter_messages_not_in_state_skips_known_thread_history():
    history = [
        {"role": "user", "content": "old", "id": "m_old"},
        {"role": "assistant", "content": "ok", "id": "m_asst"},
    ]
    new_user = {"role": "user", "content": "new ask", "id": "m_new"}
    state = [{"role": "system", "content": "sys", "id": "m_sys"}, *history]
    batch = [*history, new_user]
    assert filter_messages_not_in_state(state, batch) == [new_user]


def test_filter_messages_not_in_state_empty_when_batch_already_in_state():
    msg = {"role": "user", "content": "x", "id": "m1"}
    state = [msg]
    assert filter_messages_not_in_state(state, [msg]) == []
