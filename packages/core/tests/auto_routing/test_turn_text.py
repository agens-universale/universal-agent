"""Текст задачи хода — общее правило судей автовыбора (спека pair-judge §3.1)."""

from types import SimpleNamespace

from gc_core.auto_routing.turn_text import (
    opening_user_text,
    task_text,
    turn_messages,
)
from gc_core.intent_log import append_user_ask


def _state(asks=(), messages=None, start=None):
    current = []
    for ask in asks:
        current = append_user_ask(current, ask, user_turn_seq=0)
    return SimpleNamespace(
        current_user_intent=current,
        turn_messages_start=start,
        messages=messages or [{"role": "system", "content": "SYS"}],
    )


def test_all_asks_of_the_open_turn_are_the_task():
    assert (
        task_text(_state(asks=["fix login", "and the logout too"]), limit=100)
        == "fix login\nand the logout too"
    )


def test_the_task_is_cut_from_the_head():
    assert task_text(_state(asks=["x" * 50]), limit=10) == "x" * 10


def test_an_empty_journal_falls_back_to_the_first_real_user_message_of_the_turn():
    messages = [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "old turn"},
        {"role": "user", "content": "rename the module"},
    ]
    state = _state(messages=messages, start=2)
    assert opening_user_text(state) == "rename the module"
    assert task_text(state, limit=100) == "rename the module"
    assert [m["content"] for m in turn_messages(state)] == [
        "rename the module"
    ]
