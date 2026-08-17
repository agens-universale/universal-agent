import tempfile
import types

from intent_fixtures import intent_user_ask_texts, meta_intent_log, user_ask_records
from gc_core.intent_log import append_user_ask, serialize_for_meta
from gc_core.summarize.compaction import maybe_run_summary_compaction
from gc_core.thread_store import ThreadSession, append_new_thread_messages, save_thread_meta
from tests.helpers.summary_sample import valid_summary_xml_for_tests

TS = "2026-05-30T11:35:08Z"


def _patch_chat_completion(monkeypatch, fake):
    monkeypatch.setattr("gc_core.summarize.thread_summarizer.request_summary_text", fake)
    monkeypatch.setattr("gc_core.summarize.summary_llm_steps.request_summary_text", fake)


def _ctx(trigger_chars: int = 1):
    return types.SimpleNamespace(
        config_dict={
            "model": "test-model",
            "completions_url": "http://example.test/v1/chat/completions",
            "summary_params": {
                "chat_completions_summary_trigger_chars": trigger_chars,
                "incremental_batch_chars": int(trigger_chars * 1.4) if trigger_chars > 0 else 0,
                "max_accumulated_summary_chars": 20000,
                "last_dialog_steps_count": 5,
                "last_dialog_step_message_max_chars": 5000,
            },
            "chat_completions_params": {
                "llm": {
                    "max_tokens": 8192,
                    "completion_kwargs": {},
                },
            },
        },
        logger=None,
    )


def _state():
    return types.SimpleNamespace(
        default_model="test-model",
        shared=types.SimpleNamespace(working_memory={}),
        dialog_steps_offset=0,
        previous_response_id=None,
        messages=[],
        initial_project_data={"project": "demo"},
        call_metadata={"k": "v"},
    )


def test_state_user_intent_log_preserves_order_and_dedup():
    state = _state()
    state.user_intent_log = user_ask_records("first", "second", turn=1)
    seen = {str(record.content) for record in state.user_intent_log}
    for msg in [
        {"role": "user", "content": "third"},
        {"role": "user", "content": "second"},
        {"role": "assistant", "content": "ok"},
    ]:
        if msg.get("role") == "user":
            item = str(msg.get("content") or "").strip()
            if item and item not in seen:
                state.user_intent_log = append_user_ask(
                    state.user_intent_log,
                    item,
                    user_turn_seq=1,
                    ts=TS,
                )
                seen.add(item)
    out = intent_user_ask_texts(state.user_intent_log)
    assert out == [
        "first",
        "second",
        "third",
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
                "state_schema_version": "v2",
                "user_intent_log": meta_intent_log("first request", "second request"),
            },
        )
        session = ThreadSession.from_config(
            config_dict={"threads_data_path": tmp_dir},
            system_id=system_id,
            thread_id=thread_id,
        )

        monkeypatch.setattr("gc_core.summarize.prompt_loading.load_prompt_template", lambda _p: "prompt")
        _patch_chat_completion(
            monkeypatch,
            lambda **_kwargs: valid_summary_xml_for_tests(seed="context"),
        )
        empty_patch = types.SimpleNamespace(request_messages=[], raw_response="")
        monkeypatch.setattr(
            "gc_core.summarize.memory_suggestions.request_patch_replace",
            lambda **_k: empty_patch,
        )

        state = _state()
        state.thread_id = thread_id
        state.messages = session.get_full_history_messages()
        state.user_intent_log = user_ask_records("first request", "second request", turn=1)
        state.user_turn_seq = 1
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

        state.user_intent_log = append_user_ask(
            state.user_intent_log,
            "third request",
            user_turn_seq=state.user_turn_seq,
            ts=TS,
        )
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
        assert intent_user_ask_texts(state.user_intent_log) == [
            "first request",
            "second request",
            "third request",
        ]


def test_persist_batch_writes_user_intent_log_to_meta():
    with tempfile.TemporaryDirectory() as tmp_dir:
        thread_id = "demo-thread"
        system_id = "cli"
        session = ThreadSession.from_config(
            config_dict={"threads_data_path": tmp_dir},
            system_id=system_id,
            thread_id=thread_id,
        )
        session.meta["user_intent_log"] = meta_intent_log("first request", "second request")
        session.meta["state_schema_version"] = "v2"
        session.persist_batch(
            [
                {"role": "user", "content": "first request"},
                {"role": "assistant", "content": "first reply"},
            ],
            previous_response_id=None,
        )

        reloaded = ThreadSession.from_config(
            config_dict={"threads_data_path": tmp_dir},
            system_id=system_id,
            thread_id=thread_id,
        )
        assert reloaded.meta.get("user_intent_log") == meta_intent_log(
            "first request",
            "second request",
        )


def test_maybe_run_summary_compaction_works_without_thread(monkeypatch):
    monkeypatch.setattr("gc_core.summarize.prompt_loading.load_prompt_template", lambda _p: "prompt")
    _patch_chat_completion(
        monkeypatch,
        lambda **_kwargs: valid_summary_xml_for_tests(seed="context"),
    )
    empty_patch = types.SimpleNamespace(request_messages=[], raw_response="")
    monkeypatch.setattr(
        "gc_core.summarize.memory_suggestions.request_patch_replace",
        lambda **_k: empty_patch,
    )

    state = _state()
    state.messages = [
        {"role": "user", "content": "first request"},
        {"role": "assistant", "content": "first reply"},
        {"role": "user", "content": "second request"},
        {"role": "assistant", "content": "second reply"},
    ]
    state.user_intent_log = user_ask_records("first request", "second request", turn=1)
    state.user_turn_seq = 1
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
    assert intent_user_ask_texts(state.user_intent_log) == ["first request", "second request"]


def test_maybe_run_summary_compaction_preserves_system_and_state_metadata(monkeypatch):
    monkeypatch.setattr("gc_core.summarize.prompt_loading.load_prompt_template", lambda _p: "prompt")
    _patch_chat_completion(
        monkeypatch,
        lambda **_kwargs: valid_summary_xml_for_tests(seed="context"),
    )
    empty_patch = types.SimpleNamespace(request_messages=[], raw_response="")
    monkeypatch.setattr(
        "gc_core.summarize.memory_suggestions.request_patch_replace",
        lambda **_k: empty_patch,
    )

    state = _state()
    system_msg = {"role": "system", "content": "SYSTEM", "id": "sys_1"}
    state.messages = [
        system_msg,
        {"role": "user", "content": "first request", "id": "m1"},
        {"role": "assistant", "content": "first reply", "id": "m2"},
    ]
    state.shared = types.SimpleNamespace(working_memory={"goal": "keep me"})
    state.user_intent_log = user_ask_records("first request", turn=1)
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
    assert "keep me" in state.shared.working_memory.get("goal", "")
    assert intent_user_ask_texts(state.user_intent_log) == ["first request"]
    assert state.dialog_steps_offset > 7
    assert state.previous_response_id is None
    # Контракт этапа 1: compaction не должен менять project metadata/call metadata.
    assert state.initial_project_data == initial_project_data_before
    assert state.call_metadata == call_metadata_before

