"""Нет дублей в state.messages при продолжении треда (-t) и корректный trigger_volume."""

from __future__ import annotations

from types import SimpleNamespace

from gc_core.run_summary_coordinator import filter_messages_not_in_state
from gc_core.summarize.compaction import history_exceeds_summary_trigger


def _history_message(i: int, *, chars: int) -> dict:
    return {
        "role": "user" if i % 2 == 0 else "assistant",
        "content": f"msg-{i}-" + ("x" * chars),
        "id": f"m_{i}",
    }


def test_thread_continuation_does_not_double_count_for_summary_trigger():
    """Симуляция -t: history в state + batch с повтором history + new user."""
    history = [_history_message(i, chars=1000) for i in range(36)]
    new_user = {"role": "user", "content": "follow-up", "id": "m_new"}
    system = {"role": "system", "content": "sys", "id": "m_sys"}

    state = SimpleNamespace(
        backend_api_mode="chat_completions",
        messages=[system, *history],
    )

    # Раньше agent_executor передавал [*history, new_user] в run_agent.
    batch = [*history, new_user]
    to_append = filter_messages_not_in_state(state.messages, batch)
    state.messages += to_append

    assert to_append == [new_user]
    assert len(state.messages) == 1 + len(history) + 1

    assert history_exceeds_summary_trigger(
        state=state,
        summary_trigger_chars=200_000,
    ) is False
