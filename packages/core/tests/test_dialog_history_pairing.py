"""Фундамент: техническое user-сообщение не отрывается от своего реального хода."""

from gc_core.message_format.dialog_history import (
    group_into_turns,
    split_turns_into_char_batches,
)


def _user(text):
    return {"role": "user", "content": text}


def _tech(text):
    return {
        "role": "user",
        "content": f'<technical type="technical">{text}</technical>',
    }


def _assistant(text):
    return {"role": "assistant", "type": "message", "content": text}


def test_real_plus_technical_form_one_turn():
    msgs = [_user("почини баг"), _tech("attached: a.py"), _assistant("готово")]
    turns = group_into_turns(msgs)
    assert len(turns) == 1
    assert turns[0] == msgs


def test_two_pairs_form_two_turns():
    msgs = [
        _user("первый"),
        _tech("attached: a.py"),
        _user("второй"),
        _tech("attached: b.py"),
    ]
    turns = group_into_turns(msgs)
    assert len(turns) == 2
    assert turns[0] == [msgs[0], msgs[1]]
    assert turns[1] == [msgs[2], msgs[3]]


def test_char_batch_never_splits_a_pair():
    msgs = [_user("a" * 100), _tech("b" * 100)]
    turns = group_into_turns(msgs)
    batches = split_turns_into_char_batches(turns, batch_chars_limit=120)
    flat = [m for batch in batches for turn in batch for m in turn]
    assert flat == msgs
    for batch in batches:
        flat_batch = [m for turn in batch for m in turn]
        assert (msgs[0] in flat_batch) == (msgs[1] in flat_batch)


def test_standalone_technical_forms_own_turn():
    """Техническое сообщение без предшествующего реального хода — отдельный ход (pre-existing)."""
    msgs = [_tech("orphan"), _assistant("ответ")]
    turns = group_into_turns(msgs)
    assert len(turns) == 1
    assert turns[0] == msgs


def test_current_context_technical_also_binds():
    """Другой технический маркер (<current_context>) тоже примыкает к предыдущему ходу."""
    real = {"role": "user", "content": "сделай X"}
    ctx = {
        "role": "user",
        "content": "<current_context>some ctx</current_context>",
    }
    asst = {"role": "assistant", "type": "message", "content": "ок"}
    turns = group_into_turns([real, ctx, asst])
    assert len(turns) == 1
    assert turns[0] == [real, ctx, asst]


from gc_core.run_summary_coordinator import build_preserved_batch


def test_preserved_batch_keeps_both_real_and_technical():
    real = {"role": "user", "content": "почини баг"}
    tech = {
        "role": "user",
        "content": '<technical type="technical">attached: a.py</technical>',
    }
    current = [real, tech]
    out = build_preserved_batch(
        current_messages=current,
        mode="user_ask",
        state_messages=current,
        message_keys_already_persisted=None,
    )
    assert real in out
    assert tech in out
