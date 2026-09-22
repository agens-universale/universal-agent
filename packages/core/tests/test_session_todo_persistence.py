from gc_core.persistence.schema import normalize_session_todo, serialize_shared_block
from gc_core.persistence.thread_meta_mapper import ThreadMetaMapper
from gc_core.states import SharedStateData
from gc_testkit import run_coro

ITEMS = [
    {"content": "Изучить код", "status": "completed"},
    {"content": "Написать тест", "status": "in_progress"},
    {"content": "Прогнать pytest", "status": "pending"},
]


def test_normalize_keeps_valid_items():
    assert normalize_session_todo(ITEMS) == ITEMS


def test_normalize_drops_invalid_items():
    raw = [
        {"content": "ok", "status": "pending"},
        {"content": "", "status": "pending"},  # пустой content
        {"content": "bad status", "status": "done"},  # статус вне enum
        "not a dict",
        {"status": "pending"},  # нет content
    ]
    assert normalize_session_todo(raw) == [{"content": "ok", "status": "pending"}]


def test_normalize_non_list_is_empty():
    assert normalize_session_todo(None) == []
    assert normalize_session_todo({"content": "x"}) == []


def test_serialize_shared_block_includes_session_todo():
    shared = SharedStateData(session_todo=list(ITEMS))
    block = serialize_shared_block(shared)
    assert block["session_todo"] == ITEMS


def test_shared_from_meta_round_trip():
    shared = SharedStateData(session_todo=list(ITEMS))
    meta = ThreadMetaMapper.merge_shared_into_meta({}, shared)
    restored = ThreadMetaMapper.shared_from_meta(meta)
    assert restored is not None
    assert restored.session_todo == ITEMS


def test_shared_from_meta_without_session_todo_is_empty_list():
    shared = SharedStateData(working_memory={"state": "note"})
    meta = ThreadMetaMapper.merge_shared_into_meta({}, shared)
    restored = ThreadMetaMapper.shared_from_meta(meta)
    assert restored is not None
    assert restored.session_todo == []


def test_session_todo_survives_compaction_via_post_run_heal():
    """A2 (нормальный путь): суммаризация урезает meta['shared'] до
    working_memory+notes_md (compaction.py); post-run heal (merge_state_into_meta)
    восстанавливает полный shared, включая session_todo. Пиннит громкое обещание
    фичи «переживает суммаризацию» на нормальном пути.

    Известный pre-existing разрыв (обрыв рана до heal) — см. память
    compaction-drops-shared-fields; тут пиннится именно нормальный путь."""
    from gc_core.persistence.thread_meta_mapper import ThreadMetaMapper
    from gc_core.states import SharedStateData, State

    state = State(
        verbose=False,
        model="m",
        tools_spec=[],
        tools_descr=[],
        skills_list=[],
        max_steps=100,
        system_prompt_id="v1",
        use_server_subagents=False,
        shared=SharedStateData(session_todo=list(ITEMS), working_memory={"state": "прогресс"}),
    )
    # Как compaction оставляет meta на диске: shared урезан до 2 полей.
    truncated_meta = {"shared": {"working_memory": {"state": "прогресс"}, "notes_md": ""}}

    healed = ThreadMetaMapper.merge_state_into_meta(truncated_meta, state)

    assert healed["shared"]["session_todo"] == ITEMS


def test_session_todo_survives_compaction_payload():
    """Чеклист доезжает в summary_payload компакции (а не только через post-run heal).

    Живой прогон 2026-07-30 показал: при ручном /compact чеклист исчезал из meta,
    т.к. compaction собирал shared из двух полей. Общий фикс — в
    tests/test_compaction_preserves_shared.py; здесь пиннится именно session_todo
    (поле есть только в этой ветке, поэтому тест живёт отдельно от фикса)."""
    import types
    from unittest.mock import patch

    from gc_core.summarize.compaction import arun_summary_compaction
    from gc_core.summarize.working_memory_gateway import WorkingMemoryDeltas

    state = types.SimpleNamespace(
        thread_id="t-todo",
        messages=[{"role": "user", "content": "x" * 500}],
        user_intent_log=[],
        current_user_intent=[],
        user_turn_seq=0,
        dialog_steps_offset=0,
        previous_response_id=None,
        shared=types.SimpleNamespace(
            working_memory={},
            notes_md="",
            fetch_url_cache={},
            confirm_rules={},
            session_todo=list(ITEMS),
        ),
    )
    ctx = types.SimpleNamespace(config_dict={"summary_params": {}}, logger=None)
    captured: dict = {}

    async def _fake_summarize(**_kw):
        return {
            "raw_summary": "<summary>ok</summary>",
            "num_batches": 1,
            "last_dialog_steps": "",
            "memory_append_deltas": WorkingMemoryDeltas(),
            "memory_replace_deltas": WorkingMemoryDeltas(),
            "memory_append_touched": [],
            "memory_replace_touched": [],
            "trace": {"memory_patch": {"executed": False}, "final_output": {}},
        }

    with (
        patch("gc_core.summarize.compaction.log_compaction_started"),
        patch("gc_core.summarize.compaction.log_summary_raw_text"),
        patch("gc_core.summarize.compaction.log_summary_message_body"),
        patch("gc_core.summarize.compaction.log_compaction_completed"),
        patch("gc_core.summarize.compaction.asummarize_dialog_via_chat_completions", _fake_summarize),
        patch("gc_core.summarize.compaction.apply_summary_state_patch", lambda **_kw: "body"),
    ):
        run_coro(
            arun_summary_compaction(
                ctx=ctx,
                state=state,
                trigger_reason="test",
                is_cli_mode=False,
                on_summary_applied=lambda e: captured.update(e),
            )
        )

    assert captured["summary_payload"]["shared"]["session_todo"] == ITEMS
