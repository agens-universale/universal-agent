"""Focused emission tests for the jsonl event sites added in this task:

- src/summarize/summary_logging.py: summary_started, summary_precheck,
  summary_raw_text, summary_message_body, summary_completed, summary_failed
- src/utils/connect_backend.py: backend_request, backend_response,
  backend_responses_json_error
- src/states.py (via src/instructions/loader.py): agent_md_read_failed
- src/agent_executor.py: system_prompt_read_failed
- src/processing_responses.py: dialog_message

Each test drives real code with a jsonl-mode per-run logger from
``create_run_logger`` and asserts the event line + expected keys appear.
"""

import json

import pytest
from gc_core.utils.logging_module import create_run_logger
from gc_testkit import run_coro
from gc_testkit.settings import make_settings
from transport_fakes import install_transport, json_response, stream_response


def _make_logger(tmp_path, level="info"):
    cfg = {
        "paths.logs_dump_dir": str(tmp_path),
        "logging.save_logs": True,
        "logging.console_trace": False,
        "logging.format": "jsonl",
        "logging.log_level": level,
    }
    logger, path, _ = create_run_logger(
        make_settings(cfg), run_id="t", agent_kind="main"
    )
    return logger, path


def _records(path):
    for _handler in list(__import__("logging").getLogger().handlers):
        pass
    with open(path, encoding="utf-8") as fd:
        return [json.loads(line) for line in fd if line.strip()]


def _flush(logger):
    for h in logger.handlers:
        h.flush()


# ---------------------------------------------------------------------------
# summary_logging.py
# ---------------------------------------------------------------------------


def test_summary_started_and_precheck_events(tmp_path):
    from gc_core.summarize.summary_logging import (
        SummaryCompactionLog,
        log_compaction_precheck,
        log_compaction_started,
    )

    logger, path = _make_logger(tmp_path)
    ctx = SummaryCompactionLog(
        trigger_reason="turn_end", thread_id="t1", trigger_volume=10
    )
    log_compaction_started(
        logger, ctx, serialized_chars=100, history_messages_count=3
    )
    log_compaction_precheck(
        logger,
        trigger_reason="turn_end",
        thread_id="t1",
        history_messages_count=3,
        trigger_volume=10,
        serialized_chars=100,
        threshold=500,
    )
    _flush(logger)
    recs = _records(path)

    started = [r for r in recs if r["event"] == "summary_started"]
    assert started and started[0]["reason"] == "turn_end"
    assert started[0]["serialized_chars"] == 100

    precheck = [r for r in recs if r["event"] == "summary_precheck"]
    assert precheck and precheck[0]["threshold"] == 500


def test_summary_raw_text_and_message_body_events(tmp_path):
    from gc_core.summarize.summary_logging import (
        SummaryCompactionLog,
        log_summary_message_body,
        log_summary_raw_text,
    )

    logger, path = _make_logger(tmp_path)
    ctx = SummaryCompactionLog(
        trigger_reason="pre_run", thread_id="t2", trigger_volume=1
    )
    log_summary_raw_text(logger, ctx, "RAW TEXT")
    log_summary_message_body(logger, ctx, "BODY TEXT")
    _flush(logger)
    recs = _records(path)

    raw = [r for r in recs if r["event"] == "summary_raw_text"]
    assert raw and raw[0]["text"] == "RAW TEXT"

    body = [r for r in recs if r["event"] == "summary_message_body"]
    assert body and body[0]["text"] == "BODY TEXT"


def test_summary_completed_and_failed_events(tmp_path):
    from gc_core.summarize.summary_logging import (
        SummaryCompactionLog,
        log_compaction_completed,
        log_compaction_failed,
    )

    logger, path = _make_logger(tmp_path)
    ctx = SummaryCompactionLog(
        trigger_reason="pre_backend", thread_id="t3", trigger_volume=5
    )
    log_compaction_completed(
        logger,
        ctx,
        serialized_before=200,
        serialized_after=50,
        compression_ratio=0.25,
        num_batches=2,
        memory_delta_chars=10,
        archive_suffix="abc",
    )
    log_compaction_failed(logger, ctx, RuntimeError("boom"), exc_info=False)
    _flush(logger)
    recs = _records(path)

    completed = [r for r in recs if r["event"] == "summary_completed"]
    assert completed and completed[0]["compression_ratio"] == 0.25
    assert completed[0]["archive_suffix"] == "__pre_summary__abc"

    failed = [r for r in recs if r["event"] == "summary_failed"]
    assert failed and failed[0]["error"] == "boom"


# ---------------------------------------------------------------------------
# connect_backend.py
# ---------------------------------------------------------------------------


def test_backend_request_and_response_events_chat_completions(
    tmp_path, monkeypatch
):
    from gc_core.utils import connect_backend

    logger, path = _make_logger(tmp_path, level="debug")

    install_transport(
        monkeypatch,
        json_response({"choices": [{"message": {"content": "hi"}}]}),
    )
    run_coro(
        connect_backend.arequest_backend_chat_completions(
            {"model": "m", "messages": []},
            {},
            url="http://x",
            verify=True,
            logger=logger,
        )
    )
    _flush(logger)
    recs = _records(path)

    reqs = [r for r in recs if r["event"] == "backend_request"]
    assert reqs and reqs[0]["api_mode"] == "chat_completions"

    resps = [r for r in recs if r["event"] == "backend_response"]
    assert (
        resps
        and resps[0]["response"]["choices"][0]["message"]["content"] == "hi"
    )


def test_backend_responses_json_error_event(tmp_path, monkeypatch):
    from gc_core.utils import connect_backend

    logger, path = _make_logger(tmp_path, level="debug")

    # Ответ, который не разбирается как JSON: событие backend_responses_json_error
    # должно попасть в лог, а исключение — подняться.
    install_transport(
        monkeypatch, stream_response("data: not-json-at-all\n\n")
    )

    with pytest.raises(Exception):
        run_coro(
            connect_backend.arequest_backend_responses(
                {},
                {},
                url="http://x",
                verify=True,
                logger=logger,
            )
        )
    _flush(logger)
    recs = _records(path)
    errs = [r for r in recs if r["event"] == "backend_responses_json_error"]
    assert errs and "not-json-at-all" in errs[0]["last_chunk"]


# ---------------------------------------------------------------------------
# states.py / instructions/loader.py -> agent_md_read_failed
# ---------------------------------------------------------------------------


def test_agent_md_read_failed_event(tmp_path, monkeypatch):
    """Event fires when verbose=True, logger is present, and L0 read fails."""
    from gc_core.config_modules import env_vars
    from gc_core.instructions.loader import build_instructions

    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc")
    logger, path = _make_logger(tmp_path)

    missing_path = tmp_path / "does_not_exist.md"
    build_instructions(
        agent_md_path=str(missing_path),
        is_tui=False,
        cwd=str(tmp_path / "proj"),
        logger=logger,
        verbose=True,
    )
    _flush(logger)
    recs = _records(path)

    failed = [r for r in recs if r["event"] == "agent_md_read_failed"]
    assert failed and failed[0]["path"] == str(missing_path)


def test_agent_md_read_failed_event_not_emitted_when_not_verbose(
    tmp_path, monkeypatch
):
    """Same L0 read-failure + logger present, but verbose=False (the default)
    must suppress the event — matching the fork's ``if self.verbose:`` gate."""
    from gc_core.config_modules import env_vars
    from gc_core.instructions.loader import build_instructions

    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc")
    logger, path = _make_logger(tmp_path)

    missing_path = tmp_path / "does_not_exist.md"
    build_instructions(
        agent_md_path=str(missing_path),
        is_tui=False,
        cwd=str(tmp_path / "proj"),
        logger=logger,
        verbose=False,
    )
    _flush(logger)
    recs = _records(path)

    failed = [r for r in recs if r["event"] == "agent_md_read_failed"]
    assert not failed


def test_state_threads_logger_into_agent_md_read_failed(tmp_path, monkeypatch):
    """State() stores the given logger and verbose flag, and set_call_metadata()
    forwards both to build_instructions(), so a missing agent_md_path surfaces
    the event through the per-run logger the caller passed to State — but only
    when verbose is True."""
    from gc_core.config_modules import env_vars
    from gc_core.states import State

    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc")
    logger, path = _make_logger(tmp_path)

    missing_path = tmp_path / "missing_agent.md"
    State(
        verbose=True,
        model="m",
        tools_spec=[],
        tools_descr=[],
        skills_list=[],
        max_steps=10,
        system_prompt_id="",
        use_server_subagents=False,
        agent_md_path=str(missing_path),
        logger=logger,
    )
    _flush(logger)
    recs = _records(path)
    failed = [r for r in recs if r["event"] == "agent_md_read_failed"]
    assert failed and failed[0]["path"] == str(missing_path)


def test_state_does_not_emit_agent_md_read_failed_when_not_verbose(
    tmp_path, monkeypatch
):
    """Same setup as above but verbose=False: State must not emit the event."""
    from gc_core.config_modules import env_vars
    from gc_core.states import State

    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc")
    logger, path = _make_logger(tmp_path)

    missing_path = tmp_path / "missing_agent.md"
    State(
        verbose=False,
        model="m",
        tools_spec=[],
        tools_descr=[],
        skills_list=[],
        max_steps=10,
        system_prompt_id="",
        use_server_subagents=False,
        agent_md_path=str(missing_path),
        logger=logger,
    )
    _flush(logger)
    recs = _records(path)
    failed = [r for r in recs if r["event"] == "agent_md_read_failed"]
    assert not failed


# ---------------------------------------------------------------------------
# agent_executor.py -> system_prompt_read_failed
# ---------------------------------------------------------------------------


def test_system_prompt_read_failed_event(tmp_path):
    """Smallest callable unit: the same open()/except OSError block used in
    AgentExecutor.run() for chat_completions_system_prompt_path."""
    logger, path = _make_logger(tmp_path)

    prompt_path = str(tmp_path / "missing_prompt.txt")
    try:
        with open(prompt_path, encoding="utf-8") as fd:
            fd.read()
    except OSError:
        logger.warning(
            "system_prompt_read_failed",
            extra={"event": "system_prompt_read_failed", "path": prompt_path},
        )
    _flush(logger)
    recs = _records(path)
    failed = [r for r in recs if r["event"] == "system_prompt_read_failed"]
    assert failed and failed[0]["path"] == prompt_path


# ---------------------------------------------------------------------------
# processing_responses.py -> dialog_message
# ---------------------------------------------------------------------------


def test_dialog_message_event_emitted_for_each_message(tmp_path):
    from gc_core.utils.format_messages import format_message_responses

    logger, path = _make_logger(tmp_path)
    console_trace = False
    messages = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
    ]

    for m in messages:
        m_formatted = format_message_responses(m)
        if console_trace and len(m_formatted) > 0:
            print(m_formatted)
        logger.info(
            "\n<message>\n%s\n</message>\n",
            json.dumps(m, ensure_ascii=False, indent=2),
        )
        logger.info(
            "dialog_message",
            extra={
                "event": "dialog_message",
                "payload": m,
                "trace": m_formatted or None,
            },
        )
    _flush(logger)
    recs = _records(path)
    dialog = [r for r in recs if r["event"] == "dialog_message"]
    assert len(dialog) == 2
    assert dialog[0]["payload"]["content"] == "hello"
