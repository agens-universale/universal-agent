import tempfile
import types

from summary_pipeline import (
    maybe_run_summary_compaction,
)
from thread_store import ThreadSession, append_new_thread_messages, save_thread_meta


def _ctx(trigger_chars: int = 1):
    return types.SimpleNamespace(
        config_dict={
            "model": "test-model",
            "completions_url": "http://example.test/v1/chat/completions",
            "summary_params": {
                "chat_completions_summary_trigger_chars": trigger_chars,
                "incremental_batch_chars": int(trigger_chars * 1.4) if trigger_chars > 0 else 0,
                "max_accumulated_summary_chars": 20000,
                "original_user_messages_max_chars": 20000,
                "last_dialog_steps_count": 5,
                "last_dialog_step_message_max_chars": 5000,
            },
        },
        logger=None,
    )


def _state():
    return types.SimpleNamespace(
        default_model="test-model",
        notes_md="",
        dialog_steps_offset=0,
        previous_response_id=None,
        messages=[],
        initial_project_data={"project": "demo"},
        call_metadata={"k": "v"},
    )


def test_state_user_messages_log_preserves_order_and_dedup():
    state = _state()
    state.user_messages_log = ["[user] first", "[user] second"]
    for msg in [
        {"role": "user", "content": "third"},
        {"role": "user", "content": "second"},
        {"role": "assistant", "content": "ok"},
    ]:
        if msg.get("role") == "user":
            item = f"[user] {msg.get('content')}".strip()
            if item not in state.user_messages_log:
                state.user_messages_log.append(item)
    out = state.user_messages_log
    assert out == [
        "[user] first",
        "[user] second",
        "[user] third",
    ]


def test_maybe_run_summary_compaction_twice_uses_meta_user_log(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp_dir:
        thread_id = "demo-thread"
        system_id = "cli"
        known = set()
        append_new_thread_messages(
            tmp_dir,
            system_id,
            thread_id,
            [
                {"role": "user", "content": "first request"},
                {"role": "assistant", "content": "first reply"},
                {"role": "user", "content": "second request"},
                {"role": "assistant", "content": "second reply"},
            ],
            known,
            start_seq=0,
        )
        save_thread_meta(
            tmp_dir,
            system_id,
            thread_id,
            {
                "backend_api_mode": "chat_completions",
                "user_messages_log": ["[user] first request", "[user] second request"],
            },
        )
        session = ThreadSession.from_config(
            config_dict={"threads_data_path": tmp_dir},
            system_id=system_id,
            thread_id=thread_id,
        )

        monkeypatch.setattr("summarize.thread_summarizer._load_prompt_template", lambda _p: "prompt")
        monkeypatch.setattr(
            "summarize.thread_summarizer._request_chat_completion_text",
            lambda **_kwargs: (
                "TASK_AND_CONTEXT:\n- context\n\n"
                "GOALS:\n- goals\n\n"
                "PLAN_AND_PROGRESS:\n- done\n\n"
                "NEXT_ACTIONS:\n- next\n\n"
                "SPEC_AND_CONSTRAINTS:\n- keep\n\n"
                "RISKS_AND_OPEN_QUESTIONS:\n- none\n\n"
                "IMPORTANT_ARTIFACTS:\n- x\n"
            ),
        )
        monkeypatch.setattr("summarize.thread_summarizer.suggest_memory_additions", lambda **_k: "")

        state = _state()
        state.thread_id = thread_id
        state.messages = session.get_full_history_messages()
        state.user_messages_log = ["[user] first request", "[user] second request"]
        ran_1 = maybe_run_summary_compaction(
            ctx=_ctx(trigger_chars=1),
            state=state,
            summary_trigger_chars=1,
            trigger_reason="post_step",
            is_cli_mode=False,
            on_summary_applied=lambda event: session.archive_and_compact_summary_payload(
                event.get("summary_payload", {})
            ),
        )
        assert ran_1 is True
        assert "<original_user_messages>" not in session.history_messages[0]["content"]

        state.user_messages_log.append("[user] third request")
        session.persist_batch(
            [
                {"role": "user", "content": "third request"},
                {"role": "assistant", "content": "third reply"},
            ],
            previous_response_id=None,
        )
        state.messages = session.get_full_history_messages()
        ran_2 = maybe_run_summary_compaction(
            ctx=_ctx(trigger_chars=1),
            state=state,
            summary_trigger_chars=1,
            trigger_reason="post_step",
            is_cli_mode=False,
            on_summary_applied=lambda event: session.archive_and_compact_summary_payload(
                event.get("summary_payload", {})
            ),
        )
        assert ran_2 is True
        assert state.user_messages_log == [
            "[user] first request",
            "[user] second request",
            "[user] third request",
        ]


def test_maybe_run_summary_compaction_works_without_thread(monkeypatch):
    monkeypatch.setattr("summarize.thread_summarizer._load_prompt_template", lambda _p: "prompt")
    monkeypatch.setattr(
        "summarize.thread_summarizer._request_chat_completion_text",
        lambda **_kwargs: (
            "TASK_AND_CONTEXT:\n- context\n\n"
            "GOALS:\n- goals\n\n"
            "PLAN_AND_PROGRESS:\n- done\n\n"
            "NEXT_ACTIONS:\n- next\n\n"
            "SPEC_AND_CONSTRAINTS:\n- keep\n\n"
            "RISKS_AND_OPEN_QUESTIONS:\n- none\n\n"
            "IMPORTANT_ARTIFACTS:\n- x\n"
        ),
    )
    monkeypatch.setattr("summarize.thread_summarizer.suggest_memory_additions", lambda **_k: "")

    state = _state()
    state.messages = [
        {"role": "user", "content": "first request"},
        {"role": "assistant", "content": "first reply"},
        {"role": "user", "content": "second request"},
        {"role": "assistant", "content": "second reply"},
    ]
    state.user_messages_log = ["[user] first request", "[user] second request"]
    ran = maybe_run_summary_compaction(
        ctx=_ctx(trigger_chars=1),
        state=state,
        summary_trigger_chars=1,
        trigger_reason="post_step",
        is_cli_mode=False,
    )
    assert ran is True
    assert len(state.messages) == 1
    assert "Summarized context from previous dialogue." in state.messages[0]["content"]
    assert state.user_messages_log == ["[user] first request", "[user] second request"]


def test_maybe_run_summary_compaction_preserves_system_and_state_metadata(monkeypatch):
    monkeypatch.setattr("summarize.thread_summarizer._load_prompt_template", lambda _p: "prompt")
    monkeypatch.setattr(
        "summarize.thread_summarizer._request_chat_completion_text",
        lambda **_kwargs: (
            "TASK_AND_CONTEXT:\n- context\n\n"
            "GOALS:\n- goals\n\n"
            "PLAN_AND_PROGRESS:\n- done\n\n"
            "NEXT_ACTIONS:\n- next\n\n"
            "SPEC_AND_CONSTRAINTS:\n- keep\n\n"
            "RISKS_AND_OPEN_QUESTIONS:\n- none\n\n"
            "IMPORTANT_ARTIFACTS:\n- x\n"
        ),
    )
    monkeypatch.setattr("summarize.thread_summarizer.suggest_memory_additions", lambda **_k: "")

    state = _state()
    system_msg = {"role": "system", "content": "SYSTEM", "id": "sys_1"}
    state.messages = [
        system_msg,
        {"role": "user", "content": "first request", "id": "m1"},
        {"role": "assistant", "content": "first reply", "id": "m2"},
    ]
    state.notes_md = "keep me"
    state.user_messages_log = ["[user] first request"]
    state.dialog_steps_offset = 7
    initial_project_data_before = dict(state.initial_project_data)
    call_metadata_before = dict(state.call_metadata)

    ran = maybe_run_summary_compaction(
        ctx=_ctx(trigger_chars=1),
        state=state,
        summary_trigger_chars=1,
        trigger_reason="post_step",
        is_cli_mode=False,
    )

    assert ran is True
    assert len(state.messages) == 2
    assert state.messages[0]["role"] == "system"
    assert state.messages[0]["content"] == "SYSTEM"
    assert state.messages[1]["role"] == "user"
    assert "Summarized context from previous dialogue." in state.messages[1]["content"]
    assert "keep me" in state.notes_md
    assert state.user_messages_log == ["[user] first request"]
    assert state.dialog_steps_offset > 7
    assert state.previous_response_id is None
    # Контракт этапа 1: compaction не должен менять project metadata/call metadata.
    assert state.initial_project_data == initial_project_data_before
    assert state.call_metadata == call_metadata_before

