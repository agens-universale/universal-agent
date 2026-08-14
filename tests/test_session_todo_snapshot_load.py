"""Test that session_todo round-trips through the snapshot load path (M4 regression)."""

import json

from persistence.schema import STATE_SCHEMA_VERSION
from persistence.state_serializer import StateSerializer
from states import State


def _minimal_snapshot(session_todo: list) -> dict:
    """Build a minimal v2 snapshot with ``shared.session_todo`` populated."""
    state = State(
        verbose=False,
        model="m",
        tools_spec=[],
        tools_descr=[],
        skills_list=[],
        max_steps=10,
        system_prompt_id="v1",
        use_server_subagents=False,
    )
    payload = StateSerializer.serialize(state)
    # Inject session_todo into the shared block, matching serialize_shared_block output shape.
    payload["shared"]["session_todo"] = session_todo
    return payload


TODO_ITEMS = [
    {"content": "Изучить код", "status": "completed"},
    {"content": "Написать тест", "status": "in_progress"},
    {"content": "Прогнать pytest", "status": "pending"},
]


def test_snapshot_load_hydrates_session_todo():
    """session_todo must survive snapshot → deserialize → state.shared.session_todo."""
    payload = _minimal_snapshot(TODO_ITEMS)
    # Simulate the JSON round-trip that happens in production (file → load).
    restored = StateSerializer.deserialize(json.loads(json.dumps(payload)))
    assert restored.shared.session_todo == TODO_ITEMS


def test_snapshot_load_hydrates_empty_session_todo():
    """Empty list must come back as empty list (not fall through to default)."""
    payload = _minimal_snapshot([])
    restored = StateSerializer.deserialize(json.loads(json.dumps(payload)))
    assert restored.shared.session_todo == []


def test_snapshot_load_session_todo_absent_defaults_to_empty():
    """Missing key must not crash; defaults to []."""
    state = State(
        verbose=False,
        model="m",
        tools_spec=[],
        tools_descr=[],
        skills_list=[],
        max_steps=10,
        system_prompt_id="v1",
        use_server_subagents=False,
    )
    payload = StateSerializer.serialize(state)
    # Remove session_todo entirely (legacy snapshot without the field).
    payload["shared"].pop("session_todo", None)
    restored = StateSerializer.deserialize(json.loads(json.dumps(payload)))
    assert restored.shared.session_todo == []
