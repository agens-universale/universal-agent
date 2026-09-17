"""Подача чеклиста session_todo модели: baseline после сжатия + persistent_state.

Пробел, найденный живым прогоном 2026-07-30: чеклист переживал компакцию на диске,
но НИКУДА не попадал в контекст модели, и после сжатия она заводила список заново.

Канал доставки — тело summary-сообщения (дословно, как session_memory). Статичного
снимка в системном промпте намеренно нет: он рендерился один раз на старте рана и
после сжатия ПОСРЕДИ хода показывал устаревшее состояние (ревью 2026-08-01).
"""

from gc_core.message_policy.todo_context import render_todo_for_persistent_state

ITEMS = [
    {"content": "Спроектировать CLI", "status": "completed"},
    {"content": "Реализовать флаг --json", "status": "in_progress"},
    {"content": "Обновить README", "status": "pending"},
]


def test_no_static_checklist_snapshot_in_system_prompt():
    """Промпт-канал упразднён: снимок на старте рана устаревал к середине хода."""
    import inspect

    from gc_core import agent_executor
    from gc_core.message_policy import todo_context

    assert not hasattr(todo_context, "build_todo_prompt_block")
    assert not hasattr(todo_context, "merge_special_instructions_with_todo")
    # Сборка системного промпта живёт в run() — чек-листа там быть не должно.
    # Раньше срез по ПЕРВОМУ вхождению «render_system_prompt» попадал в строку
    # импорта, и проверка не смотрела на реальный вызов вовсе.
    run_src = inspect.getsource(agent_executor.AgentExecutor.run)
    assert "render_system_prompt(" in run_src, "сборка промпта уехала — обновить тест"
    assert "todo" not in run_src.lower()


def test_persistent_state_block_empty_when_no_checklist():
    assert render_todo_for_persistent_state([]) == ""
    assert render_todo_for_persistent_state(None) == ""


def test_persistent_state_block_is_tagged_and_carries_items():
    block = render_todo_for_persistent_state(ITEMS)
    assert block.startswith("<session_todo>")
    assert block.rstrip().endswith("</session_todo>")
    assert "[>] Реализовать флаг --json" in block


def test_persistent_state_prompt_includes_checklist():
    """B: блок <persistent_state> суммаризации несёт чеклист, чтобы сводка
    передала план дальше через сжатие."""
    from gc_core.summarize.working_memory_gateway import WorkingMemorySnapshot, render_persistent_state_for_prompt

    snapshot = WorkingMemorySnapshot(goal="цель", constraints="", state="", knowledge="")
    block = render_persistent_state_for_prompt(snapshot, session_todo=ITEMS)
    assert "<persistent_state>" in block
    assert "<session_memory>" in block
    assert "<session_todo>" in block
    assert "[>] Реализовать флаг --json" in block


def test_persistent_state_prompt_without_checklist_has_no_todo_tag():
    from gc_core.summarize.working_memory_gateway import WorkingMemorySnapshot, render_persistent_state_for_prompt

    snapshot = WorkingMemorySnapshot(goal="цель", constraints="", state="", knowledge="")
    block = render_persistent_state_for_prompt(snapshot, session_todo=[])
    assert "<session_todo>" not in block
    assert "<session_memory>" in block


# --- канал доставки чек-листа после сжатия ----------------------------------

ITEMS_MIX = [
    {"content": "Спроектировать", "status": "completed"},
    {"content": "Реализовать", "status": "in_progress"},
    {"content": "Прогнать тесты", "status": "pending"},
]


def test_summary_message_carries_checklist_verbatim():
    """Чек-лист едет в теле summary-сообщения дословно, как session_memory.

    Это единственный канал после сжатия: система-промпт больше снимок не несёт,
    а вызовы session_todo компакция вырезает."""
    from gc_core.summarize.summary_message_body import build_dialog_summary_content
    from gc_core.message_policy.todo_context import render_todo_for_persistent_state

    body = build_dialog_summary_content(
        summary_text="пересказ диалога",
        last_dialog_steps="",
        session_memory_block="<session_memory>\n<goal>\nцель\n</goal>\n</session_memory>",
        session_todo_block=render_todo_for_persistent_state(ITEMS_MIX),
    )
    assert "<session_todo>" in body
    assert "[x] Спроектировать" in body
    assert "[>] Реализовать" in body
    assert "[ ] Прогнать тесты" in body


def test_summary_message_without_checklist_has_no_block():
    from gc_core.summarize.summary_message_body import build_dialog_summary_content
    from gc_core.message_policy.todo_context import render_todo_for_persistent_state

    body = build_dialog_summary_content(
        summary_text="пересказ",
        last_dialog_steps="",
        session_todo_block=render_todo_for_persistent_state([]),
    )
    assert "session_todo" not in body


def test_compaction_puts_checklist_into_summary_message():
    """Сквозная проверка: после сжатия сообщение-baseline несёт актуальный список."""
    import inspect
    from gc_core.summarize import compaction
    src = inspect.getsource(compaction)
    assert "session_todo_block=" in src, (
        "compaction обязана передавать чек-лист в build_dialog_summary_content"
    )
