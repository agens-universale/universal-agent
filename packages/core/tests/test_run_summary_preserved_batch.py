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


# --- технический хвост батча (спека изображений §7.3, §21.1) ------------------------

_IMAGE_REF = {
    "id": "3f9a1c27b0e4d5a6",
    "kind": "image",
    "mime": "image/png",
    "width": 1456,
    "height": 819,
    "size": 214880,
    "path": "/threads/images/cli_t1_3f9a1c27b0e4d5a6.png",
    "origin": "tool",
    "label": "Image from fs_view_image",
    "source": "/work/shot.png",
    "orig_width": 2912,
    "orig_height": 1638,
}


def _call(call_id: str) -> dict:
    return {
        "type": "function_call",
        "call_id": call_id,
        "name": "fs_read_file",
        "arguments": '{"path": "a.py"}',
        "id": f"fc_{call_id}",
    }


def _output(call_id: str) -> dict:
    return {
        "type": "function_call_output",
        "call_id": call_id,
        "output": '{"ok": true}',
        "id": f"fco_{call_id}",
    }


def _technical(text: str, msg_id: str, **extra) -> dict:
    return {
        "role": "user",
        "content": wrap_technical_user_content(text),
        "id": msg_id,
        **extra,
    }


def test_function_call_output_keeps_trailing_technical_tail_in_order():
    """Пары «вызов → ответ» и за ними хвост батча: напоминание и картинка тула."""
    c1, c2 = _call("c1"), _call("c2")
    o1, o2 = _output("c1"), _output("c2")
    notice = _technical(
        "Loop detector: you called fs_read_file with the same arguments again.",
        "m_n",
    )
    image = _technical(
        "Image from fs_view_image: /work/shot.png · png 1456×819",
        "m_i",
        attachments=[_IMAGE_REF],
    )
    batch = build_preserved_batch(
        current_messages=[o1, o2, notice, image],
        mode="function_call_output",
        state_messages=[c1, c2, o1, o2, notice, image],
    )
    assert batch == [c1, o1, c2, o2, notice, image]


def test_function_call_output_keeps_only_the_tail_after_last_output():
    """Техническое сообщение внутри батча вызовов (фидбэк битого вызова из снимка
    session_compact) хвостом не считается: хвост — только после последнего ответа."""
    c1 = _call("c1")
    o1 = _output("c1")
    feedback = _technical(
        "Tool call cannot be sent: invalid JSON in tool arguments.", "m_f"
    )
    batch = build_preserved_batch(
        current_messages=[feedback, c1, o1],
        mode="function_call_output",
        state_messages=[],
    )
    assert batch == [c1, o1]


def test_user_ask_keeps_technical_message_with_attachments():
    """[ответы, картинка тула, напоминание, реплика]: картинка переживает компакцию,
    напоминание без вложений — нет (как любое техническое сообщение)."""
    o1 = _output("c1")
    image = _technical(
        "Image from fs_view_image: /work/shot.png · png 1456×819",
        "m_i",
        attachments=[_IMAGE_REF],
    )
    notice = _technical(
        "Loop detector: you called fs_read_file with the same arguments again.",
        "m_n",
    )
    human = {"role": "user", "content": "а что на скриншоте?", "id": "m_h"}
    batch = build_preserved_batch(
        current_messages=[o1, image, notice, human],
        mode="user_ask",
        state_messages=[_call("c1"), o1, image, notice, human],
    )
    assert batch == [image, human]


def test_user_ask_scoped_to_incoming_keeps_technical_message_with_attachments():
    o1 = _output("c1")
    image = _technical(
        "Image from fs_view_image: /work/shot.png · png 1456×819",
        "m_i",
        attachments=[_IMAGE_REF],
    )
    human = {"role": "user", "content": "а что на скриншоте?", "id": "m_h"}
    batch = build_preserved_batch(
        current_messages=[o1, image, human],
        mode="user_ask",
        state_messages=[],
        message_keys_already_persisted=frozenset({make_message_key(o1)}),
    )
    assert batch == [image, human]
