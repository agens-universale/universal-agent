import json

from gc_core.message_policy.todo_reminder import (
    build_todo_reminder_text,
    todo_reminder_text,
)
from gc_core.settings import LoadedSettings
from gc_core.states import SharedStateData, State
from gc_testkit.settings import make_settings

OPEN_ITEMS = [
    {"content": "Шаг 1", "status": "completed"},
    {"content": "Шаг 2", "status": "in_progress"},
    {"content": "Шаг 3", "status": "pending"},
]
CLOSED_ITEMS = [{"content": "Шаг 1", "status": "completed"}]


def _state(items) -> State:
    return State(
        verbose=False,
        model="m",
        tools_spec=[],
        tools_descr=[],
        skills_list=[],
        max_steps=100,
        system_prompt_id="v1",
        use_server_subagents=False,
        shared=SharedStateData(session_todo=list(items)),
    )


CFG = make_settings({"todo": {"reminder_steps": 3}})


def _tick(state, cfg=CFG, step_messages=None):
    return todo_reminder_text(
        state,
        cfg,
        step_messages=step_messages or [{"role": "assistant", "id": "a1"}],
    )


def test_fires_after_n_steps_with_open_items():
    state = _state(OPEN_ITEMS)
    assert _tick(state) is None  # 1
    assert _tick(state) is None  # 2
    out = _tick(state)  # 3 — порог
    assert out is not None
    assert "Шаг 2" in out
    assert state.todo_reminder_cnt == 0  # сброс после инжекта


def test_cycle_repeats_after_fire():
    state = _state(OPEN_ITEMS)
    for _ in range(3):
        last = _tick(state)
    assert last is not None
    assert _tick(state) is None  # цикл пошёл заново
    assert state.todo_reminder_cnt == 1


def test_empty_or_closed_list_gets_soft_nudge_once():
    """Живого плана нет (пусто ИЛИ всё закрыто) → мягкая подсказка, один раз.

    Раньше в этом состоянии молчали вовсе; см. канон: правило формулируется
    через «есть ли живой план», а не через «есть ли список»."""
    for items in ([], CLOSED_ITEMS):
        state = _state(items)
        fired = [msg for _ in range(5) if (msg := _tick(state))]
        assert len(fired) == 1
        assert "without an active task checklist" in fired[0]


def test_counter_resets_when_tool_used_in_batch():
    state = _state(OPEN_ITEMS)
    _tick(state)
    _tick(state)
    assert state.todo_reminder_cnt == 2
    out = todo_reminder_text(
        state,
        CFG,
        step_messages=[
            {"type": "function_call", "name": "session_todo", "id": "c1"}
        ],
    )
    assert out is None
    assert state.todo_reminder_cnt == 0


def test_counter_resets_on_function_call_output():
    """Flush перед моделью передаёт output батча, не function_call.

    У function_call_output нет поля name: имя тула лежит в JSON output.
    Иначе session_todo не сбрасывает счётчик, и следующее напоминание
    говорит «давно не обновлял» про список, записанный шагом раньше.
    """
    state = _state(OPEN_ITEMS)
    _tick(state)
    _tick(state)
    assert state.todo_reminder_cnt == 2
    out = todo_reminder_text(
        state,
        CFG,
        step_messages=[
            {
                "id": "fco_other",
                "type": "function_call_output",
                "call_id": "call_other",
                "output": json.dumps({"tool": "exec_command", "ok": True}),
            },
            {
                "id": "fco_todo",
                "type": "function_call_output",
                "call_id": "call_todo",
                "output": json.dumps(
                    {
                        "tool": "session_todo",
                        "ok": True,
                        "message": "Checklist replaced",
                        "technical_system_messages": ["unrelated"],
                    }
                ),
            },
        ],
    )
    assert out is None
    assert state.todo_reminder_cnt == 0


def test_zero_steps_disables():
    state = _state(OPEN_ITEMS)
    cfg = make_settings({"todo": {"reminder_steps": 0}})
    for _ in range(20):
        assert _tick(state, cfg=cfg) is None
    assert state.todo_reminder_cnt == 0


def test_stop_tools_disables():
    state = _state(OPEN_ITEMS)
    cfg = make_settings(
        {
            "todo": {"reminder_steps": 1},
            "agent.stop_tools": ["session_todo"],
        }
    )
    assert _tick(state, cfg=cfg) is None


def test_reminder_text_contains_snapshot_and_instruction():
    text = build_todo_reminder_text(OPEN_ITEMS)
    assert "[x] Шаг 1" in text
    assert "[>] Шаг 2" in text
    assert "[ ] Шаг 3" in text
    assert "session_todo" in text


# --- счётчик переживает границу хода ----------------------------------------


def test_reminder_counter_round_trips_through_meta():
    """Счётчик кладётся в meta и поднимается обратно — как соседи в той же паре.

    Без этого задача из серии коротких ходов (каждый короче reminder_steps)
    никогда не докатывается до напоминания: State строится заново на каждый run().
    """
    from types import SimpleNamespace

    from gc_core.message_policy.tool_usage_notice.state import (
        hydrate_tool_usage_notice_state,
        persist_tool_usage_notice_state,
    )

    state = SimpleNamespace(
        todo_reminder_cnt=7,
        tool_usage_notice_cnt={},
        project_memory_content_hash=None,
    )
    meta: dict = {}
    persist_tool_usage_notice_state(state, meta)

    restored = SimpleNamespace(
        todo_reminder_cnt=0,
        tool_usage_notice_cnt={},
        project_memory_content_hash=None,
    )
    hydrate_tool_usage_notice_state(restored, meta)
    assert restored.todo_reminder_cnt == 7


def test_reminder_counter_survives_into_session_state():
    """Счётчик попадает в состояние сессии при записи шага (иначе
    потеряется в хранилище)."""
    from types import SimpleNamespace

    from gc_core.persistence.session_state import session_from_state
    from gc_core.threads.models import SessionState

    state = SimpleNamespace(
        todo_reminder_cnt=4,
        tool_usage_notice_cnt={},
        project_memory_content_hash=None,
    )
    session = session_from_state(
        SessionState(),
        state,
        notice={},
        rendered_system_prompt=None,
        backend_api_mode="chat_completions",
        previous_response_id=None,
    )
    assert session.todo_reminder_count == 4


# --- мягкая подсказка «заведи чек-лист» -------------------------------------


def _cfg(steps: int = 1) -> LoadedSettings:
    return make_settings({"todo": {"reminder_steps": steps}})


def _soft_state(todos=None, cnt: int = 0):
    from gc_core.states import SharedStateData, State

    st = State(
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
    st.session_todo = list(todos or [])
    st.todo_reminder_cnt = cnt
    return st


def test_soft_nudge_when_no_checklist_at_all():
    """Живого плана нет → мягкая подсказка «если задача того стоит — заведи»."""
    out = todo_reminder_text(_soft_state([]), _cfg(), step_messages=[])
    assert out is not None
    assert "without an active task checklist" in out
    assert "consider writing the checklist" in out
    assert "Skip this if the task is simple" in out


def test_soft_nudge_when_all_items_completed():
    """Список весь закрыт = живого плана нет → та же мягкая подсказка."""
    done = [{"content": "Сделано", "status": "completed"}]
    out = todo_reminder_text(_soft_state(done), _cfg(), step_messages=[])
    assert out is not None
    assert "without an active task checklist" in out


def test_soft_nudge_sent_once_per_run():
    """Отклонённую мягкую подсказку не повторяем — это было бы спором с решением."""
    state = _soft_state([])
    first = todo_reminder_text(state, _cfg(), step_messages=[])
    assert first is not None
    second = todo_reminder_text(state, _cfg(), step_messages=[])
    assert second is None


def test_open_items_still_get_directive_reminder():
    """Незакрытые пункты → прежнее директивное напоминание, не мягкое."""
    items = [{"content": "Шаг", "status": "in_progress"}]
    out = todo_reminder_text(_soft_state(items), _cfg(), step_messages=[])
    assert out is not None
    assert "has not been updated for a while" in out


def test_counter_resets_even_when_nudge_suppressed():
    """После подавленной (повторной) мягкой подсказки счётчик тоже обнуляется.

    Иначе он растёт до конца хода, персистится раздутым, а флаг «подсказка была»
    per-run — и на следующем ходу напоминание стреляет на первом же шаге вместо
    N-го. Сброс обязателен во всех ветках, не только в двух отправляющих.
    """
    state = _soft_state([])
    assert todo_reminder_text(state, _cfg(), step_messages=[]) is not None
    for _ in range(5):
        assert todo_reminder_text(state, _cfg(), step_messages=[]) is None
        assert state.todo_reminder_cnt < 1, (
            f"счётчик разбух до {state.todo_reminder_cnt} — следующий ход получит напоминание немедленно"
        )
