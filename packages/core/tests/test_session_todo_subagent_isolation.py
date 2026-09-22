"""Чек-лист субагента изолирован от чек-листа оркестратора.

Дефект: дочерний State получает ``shared=parent.shared`` по ссылке, а у тула
replace-семантика — субагент затирал план оркестратора целиком. Хранилище
маршрутизируется по ``parent_state_id``: у оркестратора — общий ``shared``
(персистится), у субагента — собственное поле (живёт только его прогон).
"""
from gc_testkit.helpers.tool_ctx import CallableTool
import json

from gc_core.message_policy.todo_reminder import apply_todo_reminder
from gc_core.persistence.state_serializer import StateSerializer
from gc_core.states import SharedStateData, State
from gc_core.summarize.working_memory_gateway import read_session_todo_from_state
from gc_core.tools.session.todo import SessionTodo

PARENT_ITEMS = [
    {"content": "Спроектировать", "status": "completed"},
    {"content": "Реализовать", "status": "in_progress"},
    {"content": "Прогнать тесты", "status": "pending"},
]
CHILD_ITEMS = [
    {"content": "Прочитать файлы", "status": "in_progress"},
    {"content": "Написать ревью", "status": "pending"},
]


def _state(**overrides) -> State:
    kwargs = dict(
        verbose=False,
        model="m",
        tools_spec=[],
        tools_descr=[],
        skills_list=[],
        max_steps=100,
        system_prompt_id="v1",
        use_server_subagents=False,
        shared=SharedStateData(),
    )
    kwargs.update(overrides)
    return State(**kwargs)


def _child_of(parent: State) -> State:
    """Дочерний State как его собирает runner: shared родителя по ссылке."""
    return _state(parent_state_id=parent.state_id, shared=parent.shared)


# --- маршрутизация хранилища -------------------------------------------------

def test_orchestrator_checklist_lives_in_shared():
    state = _state()
    state.session_todo = list(PARENT_ITEMS)
    assert state.shared.session_todo == PARENT_ITEMS
    assert state.session_todo == PARENT_ITEMS


def test_subagent_checklist_does_not_touch_shared():
    parent = _state()
    parent.session_todo = list(PARENT_ITEMS)
    child = _child_of(parent)

    child.session_todo = list(CHILD_ITEMS)

    assert child.session_todo == CHILD_ITEMS
    assert parent.session_todo == PARENT_ITEMS
    assert parent.shared.session_todo == PARENT_ITEMS


def test_subagent_starts_with_empty_checklist():
    parent = _state()
    parent.session_todo = list(PARENT_ITEMS)
    assert _child_of(parent).session_todo == []


# --- тул ---------------------------------------------------------------------

def test_tool_call_from_subagent_preserves_parent_checklist():
    parent = _state()
    CallableTool(SessionTodo(state=parent))(todos=list(PARENT_ITEMS))
    child = _child_of(parent)

    out = json.loads(CallableTool(SessionTodo(state=child))(todos=list(CHILD_ITEMS)))

    assert out["ok"] is True
    assert out["count"] == 2
    assert parent.shared.session_todo == PARENT_ITEMS
    assert child.session_todo == CHILD_ITEMS


def test_tool_error_from_subagent_reports_own_counts():
    parent = _state()
    CallableTool(SessionTodo(state=parent))(todos=list(PARENT_ITEMS))
    child = _child_of(parent)
    CallableTool(SessionTodo(state=child))(todos=list(CHILD_ITEMS))

    out = json.loads(CallableTool(SessionTodo(state=child))(todos="не список"))

    assert out["ok"] is False
    assert out["count"] == 2, "в ошибке отдаём текущий чек-лист субагента, не родителя"


# --- читатели чек-листа ------------------------------------------------------

def test_persistent_state_reads_own_checklist():
    parent = _state()
    parent.session_todo = list(PARENT_ITEMS)
    child = _child_of(parent)
    child.session_todo = list(CHILD_ITEMS)

    assert read_session_todo_from_state(child) == CHILD_ITEMS
    assert read_session_todo_from_state(parent) == PARENT_ITEMS


def test_legacy_snapshot_without_subagent_todo_restores():
    """Снапшот пишется до __init__-поля: восстановление не должно падать."""
    parent = _state()
    parent.session_todo = list(PARENT_ITEMS)
    child = _child_of(parent)
    snapshot = StateSerializer.serialize(child)
    snapshot.pop("subagent_todo", None)

    restored = StateSerializer.deserialize(snapshot)

    assert restored.session_todo == []
    restored.session_todo = list(CHILD_ITEMS)
    assert restored.shared.session_todo != CHILD_ITEMS


def test_reminder_uses_own_checklist():
    """Напоминание судит по СВОЕМУ списку субагента, а не по родительскому.

    У субагента список закрыт → мягкое «заведи»; у родителя пункты открыты, и
    если бы reminder читал его, пришло бы директивное «обнови»."""
    parent = _state()
    parent.session_todo = list(PARENT_ITEMS)
    child = _child_of(parent)
    child.session_todo = [{"content": "Готово", "status": "completed"}]
    config = {"todo_params": {"reminder_steps": 1}}

    out = apply_todo_reminder([], child, config, step_messages=[])

    assert len(out) == 1
    assert "without an active task checklist" in out[0]["content"]
    assert "has not been updated" not in out[0]["content"]
