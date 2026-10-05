from gc_core.message_policy.todo_reminder import (
    apply_todo_reminder,
    build_todo_reminder_text,
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
    return apply_todo_reminder(
        [],
        state,
        cfg,
        step_messages=step_messages or [{"role": "assistant", "id": "a1"}],
    )


def test_fires_after_n_steps_with_open_items():
    state = _state(OPEN_ITEMS)
    assert _tick(state) == []  # 1
    assert _tick(state) == []  # 2
    out = _tick(state)  # 3 — порог
    assert len(out) == 1
    assert out[0]["role"] == "user"
    assert "Шаг 2" in out[0]["content"]
    assert state.todo_reminder_cnt == 0  # сброс после инжекта


def test_cycle_repeats_after_fire():
    state = _state(OPEN_ITEMS)
    for _ in range(3):
        last = _tick(state)
    assert len(last) == 1
    assert _tick(state) == []  # цикл пошёл заново
    assert state.todo_reminder_cnt == 1


def test_empty_or_closed_list_gets_soft_nudge_once():
    """Живого плана нет (пусто ИЛИ всё закрыто) → мягкая подсказка, один раз.

    Раньше в этом состоянии молчали вовсе; см. канон: правило формулируется
    через «есть ли живой план», а не через «есть ли список»."""
    for items in ([], CLOSED_ITEMS):
        state = _state(items)
        fired = [msg for _ in range(5) for msg in _tick(state)]
        assert len(fired) == 1
        assert "without an active task checklist" in fired[0]["content"]


def test_counter_resets_when_tool_used_in_batch():
    state = _state(OPEN_ITEMS)
    _tick(state)
    _tick(state)
    assert state.todo_reminder_cnt == 2
    out = apply_todo_reminder(
        [],
        state,
        CFG,
        step_messages=[
            {"type": "function_call", "name": "session_todo", "id": "c1"}
        ],
    )
    assert out == []
    assert state.todo_reminder_cnt == 0


def test_zero_steps_disables():
    state = _state(OPEN_ITEMS)
    cfg = make_settings({"todo": {"reminder_steps": 0}})
    for _ in range(20):
        assert _tick(state, cfg=cfg) == []
    assert state.todo_reminder_cnt == 0


def test_stop_tools_disables():
    state = _state(OPEN_ITEMS)
    cfg = make_settings(
        {
            "todo": {"reminder_steps": 1},
            "agent.stop_tools": ["session_todo"],
        }
    )
    assert _tick(state, cfg=cfg) == []


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


def test_reminder_counter_survives_meta_merge_to_base():
    """Ключ попадает в base_meta перед сохранением треда (иначе потеряется на диске)."""
    from types import SimpleNamespace

    from gc_core.message_policy.tool_usage_notice.state import (
        merge_tool_usage_notice_meta_to_base,
        persist_tool_usage_notice_state,
    )

    session_meta: dict = {}
    persist_tool_usage_notice_state(
        SimpleNamespace(
            todo_reminder_cnt=4,
            tool_usage_notice_cnt={},
            project_memory_content_hash=None,
        ),
        session_meta,
    )
    base: dict = {}
    merge_tool_usage_notice_meta_to_base(base, session_meta)
    assert base.get("todo_reminder_cnt") == 4


# --- мягкая подсказка «заведи чек-лист» -------------------------------------


def _cfg(steps: int = 1) -> LoadedSettings:
    return make_settings({"todo": {"reminder_steps": steps}})


def _state(todos=None, cnt: int = 0):
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
    out = apply_todo_reminder([], _state([]), _cfg(), step_messages=[])
    assert len(out) == 1
    text = out[0]["content"]
    assert "without an active task checklist" in text
    assert "consider writing the checklist" in text
    assert "Skip this if the task is simple" in text


def test_soft_nudge_when_all_items_completed():
    """Список весь закрыт = живого плана нет → та же мягкая подсказка."""
    done = [{"content": "Сделано", "status": "completed"}]
    out = apply_todo_reminder([], _state(done), _cfg(), step_messages=[])
    assert len(out) == 1
    assert "without an active task checklist" in out[0]["content"]


def test_soft_nudge_sent_once_per_run():
    """Отклонённую мягкую подсказку не повторяем — это было бы спором с решением."""
    state = _state([])
    first = apply_todo_reminder([], state, _cfg(), step_messages=[])
    assert len(first) == 1
    second = apply_todo_reminder([], state, _cfg(), step_messages=[])
    assert second == []


def test_open_items_still_get_directive_reminder():
    """Незакрытые пункты → прежнее директивное напоминание, не мягкое."""
    items = [{"content": "Шаг", "status": "in_progress"}]
    out = apply_todo_reminder([], _state(items), _cfg(), step_messages=[])
    assert len(out) == 1
    assert "has not been updated for a while" in out[0]["content"]


def test_counter_resets_even_when_nudge_suppressed():
    """После подавленной (повторной) мягкой подсказки счётчик тоже обнуляется.

    Иначе он растёт до конца хода, персистится раздутым, а флаг «подсказка была»
    per-run — и на следующем ходу напоминание стреляет на первом же шаге вместо
    N-го. Сброс обязателен во всех ветках, не только в двух отправляющих.
    """
    state = _state([])
    assert len(apply_todo_reminder([], state, _cfg(), step_messages=[])) == 1
    for _ in range(5):
        assert apply_todo_reminder([], state, _cfg(), step_messages=[]) == []
        assert state.todo_reminder_cnt < 1, (
            f"счётчик разбух до {state.todo_reminder_cnt} — следующий ход получит напоминание немедленно"
        )
