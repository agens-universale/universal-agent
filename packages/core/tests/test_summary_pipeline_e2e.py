import json
import os
import tempfile
import types

from gc_testkit.intent_fixtures import intent_user_ask_texts
from gc_core.intent_log import deserialize_list
from gc_core.summarize.compaction import maybe_run_summary_compaction
from gc_core.thread_store import ThreadSession, append_new_thread_messages, load_thread_meta
from gc_testkit.helpers.summary_sample import valid_summary_xml_for_tests


def _patch_chat_completion(monkeypatch, fake):
    monkeypatch.setattr("gc_core.summarize.thread_summarizer.arequest_summary_text", fake)
    monkeypatch.setattr("gc_core.summarize.summary_llm_steps.arequest_summary_text", fake)


def _ctx():
    return types.SimpleNamespace(
        config_dict={
            "model": "test-model",
            "completions_url": "http://example.test/v1/chat/completions",
            "summary_params": {
                "chat_completions_summary_trigger_chars": 1,
                "incremental_batch_chars": 2,
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
        user_intent_log=[],
        current_user_intent=[],
        user_turn_seq=0,
        messages=[],
    )


def _valid_summary(seed: str) -> str:
    return valid_summary_xml_for_tests(seed=seed)


def _read_current_data_rows(tmp_dir: str, thread_id: str):
    data_path = os.path.join(tmp_dir, f"data_cli_{thread_id}.jsonl")
    with open(data_path, "r", encoding="utf-8") as fd:
        return [json.loads(line) for line in fd if line.strip()]


def test_e2e_two_sequential_summaries_keep_user_log_order_and_no_original_block(monkeypatch):
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
        session = ThreadSession.from_config(
            config_dict={"threads_data_path": tmp_dir},
            system_id=system_id,
            thread_id=thread_id,
        )
        session.meta["backend_api_mode"] = "chat_completions"
        session.backend_api_mode = "chat_completions"
        state = _state()
        state.thread_id = thread_id
        state.messages = session.get_full_history_messages()
        state.user_intent_log = deserialize_list(["first request", "second request"])
        state.user_turn_seq = 1

        monkeypatch.setattr("gc_core.summarize.prompt_loading.load_prompt_template", lambda _p: "prompt")

        async def _fake_summary(**_kwargs):
            return _valid_summary("s")

        _patch_chat_completion(monkeypatch, _fake_summary)
        empty_patch = types.SimpleNamespace(request_messages=[], raw_response="")

        async def _fake_patch_replace(**_k):
            return empty_patch

        monkeypatch.setattr(
            "gc_core.summarize.memory_suggestions.arequest_patch_replace",
            _fake_patch_replace,
        )

        assert maybe_run_summary_compaction(
            ctx=_ctx(),
            state=state,
            summary_trigger_chars=1,
            trigger_reason="post_step",
            is_cli_mode=False,
            on_summary_applied=lambda event: session.archive_and_compact_summary_payload(
                event.get("summary_payload", {})
            ),
        )

        rows_after_first = _read_current_data_rows(tmp_dir, thread_id)
        assert len(rows_after_first) == 1
        first_compact_content = rows_after_first[0]["payload"]["content"]
        assert "<original_user_messages>" not in first_compact_content
        assert "Summarized context from previous dialogue." in first_compact_content

        state.user_intent_log.extend(
            deserialize_list(["third request", "fourth request"])
        )
        session.persist_batch(
            [
                {"role": "user", "content": "third request"},
                {"role": "assistant", "content": "third reply"},
                {"role": "user", "content": "fourth request"},
                {"role": "assistant", "content": "fourth reply"},
            ],
            previous_response_id=None,
        )
        prev_offset = int(state.dialog_steps_offset)
        state.messages = session.get_full_history_messages()
        assert maybe_run_summary_compaction(
            ctx=_ctx(),
            state=state,
            summary_trigger_chars=1,
            trigger_reason="post_step",
            is_cli_mode=False,
            on_summary_applied=lambda event: session.archive_and_compact_summary_payload(
                event.get("summary_payload", {})
            ),
        )
        assert int(state.dialog_steps_offset) > prev_offset

        assert intent_user_ask_texts(state.user_intent_log) == [
            "first request",
            "second request",
            "third request",
            "fourth request",
        ]
        assert state.previous_response_id is None
        meta_after_second = load_thread_meta(tmp_dir, system_id, thread_id)
        assert meta_after_second.get("backend_api_mode") == "chat_completions"
        wm = meta_after_second.get("shared", {}).get("working_memory", {})
        assert wm == {} or all(not str(v).strip() for v in wm.values())
        assert isinstance(meta_after_second.get("dialog_steps_offset"), int)
        assert meta_after_second.get("messages_count") == 1
        assert meta_after_second.get("previous_response_id") is None
        assert intent_user_ask_texts(
            deserialize_list(meta_after_second.get("user_intent_log"))
        ) == [
            "first request",
            "second request",
            "third request",
            "fourth request",
        ]
        rows_after_second = _read_current_data_rows(tmp_dir, thread_id)
        assert len(rows_after_second) == 1
        second_compact_content = rows_after_second[0]["payload"]["content"]
        assert "<original_user_messages>" not in second_compact_content

        archives = [x for x in os.listdir(tmp_dir) if "__pre_summary__" in x]
        assert len(archives) >= 2


def test_e2e_user_log_skips_technical_and_tool_messages(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp_dir:
        thread_id = "demo-thread-tech"
        system_id = "cli"
        known = set()
        append_new_thread_messages(
            tmp_dir,
            system_id,
            thread_id,
            [
                {
                    "role": "user",
                    "content": '<current_context>client has non-empty thread notes; call `thread_notes` with action="read" to load them.</current_context>',
                },
                {"type": "function_call", "call_id": "c1", "name": "thread_notes", "arguments": "{}"},
                {"type": "function_call_output", "call_id": "c1", "output": "ok"},
                {"role": "user", "content": "real request"},
            ],
            known,
            start_seq=0,
        )
        session = ThreadSession.from_config(
            config_dict={"threads_data_path": tmp_dir},
            system_id=system_id,
            thread_id=thread_id,
        )
        session.meta["backend_api_mode"] = "chat_completions"
        session.backend_api_mode = "chat_completions"
        state = _state()
        state.thread_id = thread_id
        state.messages = session.get_full_history_messages()
        state.user_intent_log = deserialize_list(
            [
                '<current_context>client has non-empty thread notes; call `thread_notes` with action="read" to load them.</current_context>',
                "real request",
            ]
        )
        state.user_turn_seq = 1

        monkeypatch.setattr("gc_core.summarize.prompt_loading.load_prompt_template", lambda _p: "prompt")

        async def _fake_summary(**_kwargs):
            return _valid_summary("s-tech")

        _patch_chat_completion(monkeypatch, _fake_summary)
        empty_patch = types.SimpleNamespace(request_messages=[], raw_response="")

        async def _fake_patch_replace(**_k):
            return empty_patch

        monkeypatch.setattr(
            "gc_core.summarize.memory_suggestions.arequest_patch_replace",
            _fake_patch_replace,
        )

        assert maybe_run_summary_compaction(
            ctx=_ctx(),
            state=state,
            summary_trigger_chars=1,
            trigger_reason="post_step",
            is_cli_mode=False,
            on_summary_applied=lambda event: session.archive_and_compact_summary_payload(
                event.get("summary_payload", {})
            ),
        )
        # После compaction user log в state не перезаписывается: source of truth
        # для него формируется инкрементально в runtime и здесь сохраняется как есть.
        assert intent_user_ask_texts(state.user_intent_log) == [
            '<current_context>client has non-empty thread notes; call `thread_notes` with action="read" to load them.</current_context>',
            "real request",
        ]
        meta_after = load_thread_meta(tmp_dir, system_id, thread_id)
        assert meta_after.get("backend_api_mode") == "chat_completions"
        assert meta_after.get("messages_count") == 1
