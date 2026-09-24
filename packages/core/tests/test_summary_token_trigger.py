"""Токенный порог суммаризации рядом с символьным."""

import json
import os
import tempfile
from types import SimpleNamespace

from gc_core import run_summary_coordinator as rsc
from gc_core.persistence.thread_meta_mapper import ThreadMetaMapper
from gc_core.processing_responses import remember_main_prompt_tokens
from gc_core.run_summary_coordinator import RunSummaryCoordinator
from gc_core.states import State
from gc_core.summarize.compaction import (
    apply_summary_state_patch,
    exceeded_summary_triggers,
    history_exceeds_summary_trigger,
)
from gc_core.summarize.summary_logging import SummaryCompactionLog, log_compaction_started
from gc_core.thread_store import (
    ThreadSession,
    append_new_thread_messages,
    load_thread_meta,
    save_thread_meta,
)
from gc_core.usage.account import UsageAccount
from gc_testkit import run_coro


def _user(text: str, msg_id: str) -> dict:
    return {"role": "user", "content": text, "id": msg_id}


def _state_with(*messages, tokens=None, count=None):
    return SimpleNamespace(
        messages=list(messages),
        last_main_prompt_tokens=tokens,
        last_main_prompt_message_count=count,
        backend_api_mode="chat_completions",
    )


def _hits(state, *, chars=0, tokens=0):
    return exceeded_summary_triggers(
        state=state,
        summary_trigger_chars=chars,
        summary_trigger_tokens=tokens,
    )


def test_zero_thresholds_are_ignored():
    state = _state_with(_user("x" * 5000, "1"), tokens=10_000, count=1)
    hits = _hits(state, chars=0, tokens=0)
    assert hits.chars is False
    assert hits.tokens is False
    assert not hits


def test_char_comparison_unchanged_with_token_threshold():
    state = _state_with(_user("abcd", "1"), tokens=1, count=0)
    assert history_exceeds_summary_trigger(
        state=state, summary_trigger_chars=3
    ) is True
    assert _hits(state, chars=3, tokens=0).chars is True
    assert _hits(state, chars=3, tokens=10_000).chars is True
    assert _hits(state, chars=4, tokens=10_000).chars is False
    assert _hits(state, chars=0, tokens=10_000).chars is False


def test_token_lever_silent_without_number():
    state = _state_with(_user("x" * 100, "1"))
    assert _hits(state, tokens=10).tokens is False


def test_token_estimate_is_number_plus_tail_half_and_excludes_counted_prefix():
    counted = _user("a" * 100, "counted")
    tail = _user("b" * 10, "tail")
    system = {"role": "system", "content": "S" * 1000, "id": "sys"}
    state = _state_with(system, counted, tail, tokens=50, count=2)
    # хвост — 10 символов, 10 // 2 = 5; system и counted в число уже вошли
    assert _hits(state, tokens=54).tokens is True
    assert _hits(state, tokens=55).tokens is False


def test_both_exceeded_is_one_compaction_and_log_names_both(monkeypatch, tmp_path):
    counted = _user("a" * 20, "1")
    tail = _user("b" * 20, "2")
    state = _state_with(counted, tail, tokens=100, count=1)
    hits = _hits(state, chars=10, tokens=10)
    assert hits.chars is True and hits.tokens is True

    calls = []

    async def _capture(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(rsc, "arun_summary_compaction", _capture)
    coord = RunSummaryCoordinator(
        ctx=SimpleNamespace(config_dict={}),
        state=state,
        thread_session=None,
        summary_trigger_chars=10,
        summary_trigger_tokens=10,
        is_cli_mode=False,
        pre_backend_trigger_reason="pre_backend",
        logger=None,
    )
    run_coro(coord.apre_backend_compaction_hook([counted, tail], "user_ask"))
    assert len(calls) == 1
    assert calls[0]["exceeded_chars"] is True
    assert calls[0]["exceeded_tokens"] is True

    from gc_core.utils.logging_module import create_run_logger

    logger, path, _ = create_run_logger(
        {
            "logs_dump_dir": str(tmp_path),
            "save_logs": True,
            "console_trace": False,
            "logging_format": "jsonl",
            "log_level": "info",
        },
        run_id="t",
        agent_kind="main",
    )
    log_compaction_started(
        logger,
        SummaryCompactionLog(
            trigger_reason="pre_backend",
            exceeded_chars=True,
            exceeded_tokens=True,
        ),
        serialized_chars=1,
        history_messages_count=2,
    )
    for handler in logger.handlers:
        handler.flush()
    recs = [
        json.loads(line)
        for line in open(path, encoding="utf-8")
        if line.strip()
    ]
    started = [r for r in recs if r.get("event") == "summary_started"]
    assert started and started[0]["exceeded_chars"] is True
    assert started[0]["exceeded_tokens"] is True


def test_pre_run_tokens_only_stops_after_one_pass(monkeypatch):
    state = _state_with(_user("x" * 40, "1"), tokens=100, count=1)
    calls = []

    async def _keep_number(**kwargs):
        calls.append(1)

    monkeypatch.setattr(rsc, "arun_summary_compaction", _keep_number)
    coord = RunSummaryCoordinator(
        ctx=SimpleNamespace(config_dict={}),
        state=state,
        thread_session=None,
        summary_trigger_chars=0,
        summary_trigger_tokens=10,
        is_cli_mode=False,
        pre_backend_trigger_reason="pre_backend",
        logger=None,
    )
    run_coro(coord.apre_run_compact_if_needed())
    assert calls == [1]


def test_pre_run_chars_keep_going_after_token_number_is_forgotten(monkeypatch):
    state = _state_with(_user("x" * 40, "1"), tokens=100, count=1)
    calls = []

    async def _clear_tokens(**kwargs):
        calls.append(1)
        kwargs["state"].last_main_prompt_tokens = None
        kwargs["state"].last_main_prompt_message_count = None

    monkeypatch.setattr(rsc, "arun_summary_compaction", _clear_tokens)
    coord = RunSummaryCoordinator(
        ctx=SimpleNamespace(config_dict={}),
        state=state,
        thread_session=None,
        summary_trigger_chars=1,
        summary_trigger_tokens=10,
        is_cli_mode=False,
        pre_backend_trigger_reason="pre_backend",
        logger=None,
    )
    run_coro(coord.apre_run_compact_if_needed())
    assert len(calls) == 20


def test_new_state_prompt_fields_start_as_none():
    state = State(
        verbose=False,
        model="m",
        tools_spec=[],
        tools_descr=[],
        skills_list=[],
        max_steps=1,
        system_prompt_id="",
        use_server_subagents=False,
    )
    assert state.last_main_prompt_tokens is None
    assert state.last_main_prompt_message_count is None


def test_remember_main_prompt_only_on_chat_completions_with_input_tokens():
    state = SimpleNamespace(
        last_main_prompt_tokens=7,
        last_main_prompt_message_count=1,
        usage=UsageAccount(),
    )
    remember_main_prompt_tokens(
        state, {"prompt_tokens": 40}, 3, backend_mode="responses"
    )
    assert state.last_main_prompt_tokens == 7
    assert state.last_main_prompt_message_count == 1

    remember_main_prompt_tokens(
        state, {"completion_tokens": 9}, 3, backend_mode="chat_completions"
    )
    assert state.last_main_prompt_tokens == 7

    state.usage.add("summary", 999, 1, 0)
    assert state.last_main_prompt_tokens == 7

    remember_main_prompt_tokens(
        state, {"input_tokens": 15}, 4, backend_mode="chat_completions"
    )
    assert state.last_main_prompt_tokens == 15
    assert state.last_main_prompt_message_count == 4


def test_compaction_clears_prompt_fields_in_state_and_meta():
    state = SimpleNamespace(
        messages=[_user("hi", "1")],
        last_main_prompt_tokens=80,
        last_main_prompt_message_count=1,
        dialog_steps_offset=0,
        previous_response_id="r",
        user_intent_log=[],
        current_user_intent=[],
        shared=None,
    )
    apply_summary_state_patch(
        state=state,
        raw_summary="short",
        last_dialog_steps="",
        summarized_messages_count=1,
    )
    assert state.last_main_prompt_tokens is None
    assert state.last_main_prompt_message_count is None
    meta = ThreadMetaMapper.merge_state_into_meta({}, state)
    assert "last_main_prompt_tokens" in meta
    assert meta["last_main_prompt_tokens"] is None
    assert meta["last_main_prompt_message_count"] is None


def test_resume_reads_null_and_missing_as_none_and_keeps_zero():
    assert ThreadMetaMapper.read_prompt_token_fields({}) == (None, None)
    assert ThreadMetaMapper.read_prompt_token_fields(
        {"last_main_prompt_tokens": None, "last_main_prompt_message_count": None}
    ) == (None, None)
    assert ThreadMetaMapper.read_prompt_token_fields(
        {"last_main_prompt_tokens": 0, "last_main_prompt_message_count": 0}
    ) == (0, 0)
    assert ThreadMetaMapper.read_prompt_token_fields(
        {"last_main_prompt_tokens": 9, "last_main_prompt_message_count": 2}
    ) == (9, 2)


async def test_compact_writes_null_prompt_fields_to_meta():
    with tempfile.TemporaryDirectory() as tmp_dir:
        thread_id = "tok-thread"
        system_id = "cli"
        known: set = set()
        append_new_thread_messages(
            tmp_dir,
            system_id,
            thread_id,
            [_user("old", "1")],
            known,
            start_seq=0,
        )
        save_thread_meta(
            tmp_dir,
            system_id,
            thread_id,
            {
                "created_at": "2026-01-01T00:00:00+00:00",
                "backend_api_mode": "chat_completions",
                "last_main_prompt_tokens": 900,
                "last_main_prompt_message_count": 4,
            },
        )
        session = await ThreadSession.afrom_config(
            config_dict={"threads_data_path": tmp_dir},
            system_id=system_id,
            thread_id=thread_id,
        )
        await session.aarchive_and_compact_summary_payload(
            {
                "summary_message_content": "compressed",
                "dialog_steps_offset": 1,
                "last_main_prompt_tokens": None,
                "last_main_prompt_message_count": None,
            }
        )
        meta = load_thread_meta(tmp_dir, system_id, thread_id)
        assert "last_main_prompt_tokens" in meta
        assert meta["last_main_prompt_tokens"] is None
        assert meta["last_main_prompt_message_count"] is None
        raw = open(
            os.path.join(tmp_dir, f"meta_cli_{thread_id}.json"),
            encoding="utf-8",
        ).read()
        assert '"last_main_prompt_tokens": null' in raw
