"""--structured-output-format: файл формы, поля turn_finish, один aux-запрос."""

import json
import sys
from types import SimpleNamespace

import pytest
from gc_cli.structured_output import (
    REQUEST_LABEL,
    StructuredOutputError,
    aemit_structured_result,
    arephrase_turn_outcome,
    read_format_description,
    turn_outcome_text,
)
from gc_core.agent_executor import AgentExecutor
from gc_core.config_modules.chat_completions import (
    DEFAULT_AUXILIARY_LLM_MAX_TOKENS,
)
from gc_core.usage.account import CATEGORY_MAIN, UsageAccount
from gc_core.usage.context import record_usage
from gc_testkit.settings import fake_ctx, make_settings


def _parse(argv, monkeypatch):
    monkeypatch.setattr(sys, "argv", argv)
    import importlib

    from gc_cli import arg_parser as ap_mod

    importlib.reload(ap_mod)
    return ap_mod


def _finish(**overrides):
    payload = {
        "solution_type": "full_solution",
        "task": "посчитать",
        "task_report": "Ответ 4.",
        "comment": "без оговорок",
        "handoff": "{}",
    }
    payload.update(overrides)
    return json.dumps(payload, ensure_ascii=False)


class _Aux:
    def __init__(self, max_tokens=128, completion_kwargs=None):
        self.max_tokens = max_tokens
        self.completion_kwargs = (
            {} if completion_kwargs is None else completion_kwargs
        )


class _Logger:
    def __init__(self):
        self.infos = []
        self.warnings = []

    def info(self, msg, *args, extra=None):
        self.infos.append((msg % args if args else msg, extra))

    def warning(self, msg, *args, extra=None):
        self.warnings.append((msg % args if args else msg, extra))


class _Ctx:
    def __init__(self):
        loaded = make_settings()
        self.settings = loaded.settings
        self.runtime = loaded.runtime
        self.auxiliary_llm = _Aux()
        self.logger = _Logger()


def test_flag_is_literal_path(monkeypatch):
    """Объявленный флаг не прогоняется через yaml.safe_load."""
    mod = _parse(
        [
            "gc-cli",
            "-q",
            "x",
            "--structured-output-format",
            "formats/00123.md",
        ],
        monkeypatch,
    )
    assert mod.run_args.structured_output_format == "formats/00123.md"
    assert "structured_output_format" not in mod.extra_config_overrides
    assert "structured-output-format" not in mod.extra_config_overrides


def test_flag_absent(monkeypatch):
    mod = _parse(["gc-cli", "-q", "x"], monkeypatch)
    assert mod.run_args.structured_output_format is None


def test_read_format_description_returns_body(tmp_path):
    path = tmp_path / "fmt.md"
    path.write_text("# Report\nполе: итог\n", encoding="utf-8")
    assert read_format_description(str(path)) == "# Report\nполе: итог\n"


def test_read_format_description_missing(tmp_path):
    with pytest.raises(StructuredOutputError, match="не читается"):
        read_format_description(str(tmp_path / "nope.md"))


def test_read_format_description_empty(tmp_path):
    path = tmp_path / "empty.md"
    path.write_text("  \n", encoding="utf-8")
    with pytest.raises(StructuredOutputError, match="пустой"):
        read_format_description(str(path))


def test_read_format_description_blank_path():
    with pytest.raises(StructuredOutputError, match="пустой"):
        read_format_description("   ")


def test_read_format_description_resolves_relative_path(tmp_path, monkeypatch):
    path = tmp_path / "fmt.md"
    path.write_text("форма\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert read_format_description("fmt.md") == "форма\n"
    assert read_format_description("./fmt.md") == "форма\n"


def test_turn_outcome_text_keeps_prose_and_raw_json():
    assert turn_outcome_text("  готово  ") == "готово"
    raw = _finish()
    assert turn_outcome_text(raw) == raw
    assert '"task_report"' in turn_outcome_text(json.loads(raw))
    assert turn_outcome_text("") is None
    assert turn_outcome_text(None) is None
    assert turn_outcome_text("   ") is None


@pytest.mark.asyncio
async def test_rephrase_sends_format_and_fields(monkeypatch):
    captured = {}

    async def _fake(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(text="  ## Итог\n4\n", finish_reason="stop")

    monkeypatch.setattr(
        "gc_core.structured_output.arequest_chat_completion_outcome",
        _fake,
    )
    text = await arephrase_turn_outcome(
        format_text="markdown, language: Russian",
        response=_finish(),
        ctx=_Ctx(),
        state=object(),
        terminal_outcome=None,
    )
    assert text == "## Итог\n4"
    assert captured["consumer"] == "summary"
    assert captured["request_label"] == REQUEST_LABEL
    assert captured["max_tokens"] == 128
    assert captured.get("payload_overrides") is None
    system, user = captured["messages"]
    assert system["role"] == "system"
    assert "Language:" in system["content"]
    assert "turn outcome" in system["content"]
    assert "format description names a language" in system["content"]
    assert user["content"].startswith(
        "Format description:\nmarkdown, language: Russian\n"
    )
    assert '"task_report": "Ответ 4."' in user["content"]


@pytest.mark.asyncio
async def test_rephrase_refuses_aborted_run_and_accepts_prose(monkeypatch):
    with pytest.raises(StructuredOutputError, match="step_limit"):
        await arephrase_turn_outcome(
            format_text="json",
            response=_finish(),
            ctx=_Ctx(),
            state=object(),
            terminal_outcome="step_limit",
        )

    captured = {}

    async def _fake(**kwargs):
        captured.update(kwargs)
        return "ok", "stop"

    monkeypatch.setattr(
        "gc_core.structured_output._arequest_rephrase",
        _fake,
    )
    text = await arephrase_turn_outcome(
        format_text="json",
        response="просто текст",
        ctx=_Ctx(),
        state=object(),
        terminal_outcome=None,
    )
    assert text == "ok"
    assert "просто текст" in captured["messages"][1]["content"]


@pytest.mark.asyncio
async def test_rephrase_uses_default_max_tokens_when_unset(monkeypatch):
    captured = {}

    async def _fake(**kwargs):
        captured.update(kwargs)
        return "ok", None

    monkeypatch.setattr(
        "gc_core.structured_output._arequest_rephrase",
        _fake,
    )
    ctx = _Ctx()
    ctx.auxiliary_llm = _Aux(max_tokens=None)
    text = await arephrase_turn_outcome(
        format_text="json",
        response="готово",
        ctx=ctx,
        state=object(),
        terminal_outcome=None,
    )
    assert text == "ok"
    assert captured["max_tokens"] == DEFAULT_AUXILIARY_LLM_MAX_TOKENS


@pytest.mark.asyncio
async def test_rephrase_empty_answer_is_an_error(monkeypatch):
    async def _empty(**_kwargs):
        return "  \n", "stop"

    monkeypatch.setattr(
        "gc_core.structured_output._arequest_rephrase",
        _empty,
    )
    with pytest.raises(StructuredOutputError, match="пустой ответ"):
        await arephrase_turn_outcome(
            format_text="json",
            response=_finish(),
            ctx=_Ctx(),
            state=object(),
            terminal_outcome=None,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("finish_reason", "match"),
    [
        ("length", "оборван"),
        ("content_filter", "отклонён"),
        ("refusal", "отклонён"),
        ("safety", "отклонён"),
    ],
)
async def test_rephrase_unfinished_reason_is_an_error(
    monkeypatch, finish_reason, match
):
    async def _cut(**_kwargs):
        return '{"result": "answer', finish_reason

    monkeypatch.setattr(
        "gc_core.structured_output._arequest_rephrase",
        _cut,
    )
    with pytest.raises(StructuredOutputError, match=match):
        await arephrase_turn_outcome(
            format_text="json",
            response=_finish(),
            ctx=_Ctx(),
            state=object(),
            terminal_outcome=None,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "finish_reason", ["stop", "tool_calls", None, "weird"]
)
async def test_rephrase_keeps_text_when_reason_does_not_block(
    monkeypatch, finish_reason
):
    async def _done(**_kwargs):
        return "форма", finish_reason

    monkeypatch.setattr(
        "gc_core.structured_output._arequest_rephrase",
        _done,
    )
    text = await arephrase_turn_outcome(
        format_text="json",
        response=_finish(),
        ctx=_Ctx(),
        state=object(),
        terminal_outcome=None,
    )
    assert text == "форма"


@pytest.mark.asyncio
async def test_rephrase_transport_error_stays_structured(monkeypatch):
    async def _boom(**_kwargs):
        raise RuntimeError("backend down")

    monkeypatch.setattr(
        "gc_core.structured_output._arequest_rephrase",
        _boom,
    )
    with pytest.raises(StructuredOutputError, match="backend down"):
        await arephrase_turn_outcome(
            format_text="json",
            response=_finish(),
            ctx=_Ctx(),
            state=object(),
            terminal_outcome=None,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "config",
    [
        {"logging.format": "text", "logging.console_trace": True},
        {"logging.format": "jsonl", "logging.console_trace": False},
    ],
)
async def test_emit_prints_text_when_stdout_is_not_jsonl(
    monkeypatch, capsys, config
):
    async def _ok(**_kwargs):
        return '{"n": 4}', "stop"

    monkeypatch.setattr(
        "gc_core.structured_output._arequest_rephrase",
        _ok,
    )
    ctx = _Ctx()
    code = await aemit_structured_result(
        response=_finish(),
        settings=make_settings(config),
        ctx=ctx,
        state=object(),
        terminal_outcome=None,
        format_text="json",
    )
    assert code == 0
    out, err = capsys.readouterr()
    assert out == '{"n": 4}\n'
    assert err == ""
    assert ctx.logger.infos[0][1]["answer"] == '{"n": 4}'


@pytest.mark.asyncio
async def test_emit_jsonl_stream_keeps_stdout_free_of_raw_text(
    monkeypatch, capsys
):
    """Сырая строка в потоке событий перестаёт быть jsonl."""

    async def _ok(**_kwargs):
        return '{"n": 4}', "stop"

    monkeypatch.setattr(
        "gc_core.structured_output._arequest_rephrase",
        _ok,
    )
    ctx = _Ctx()
    code = await aemit_structured_result(
        response=_finish(),
        settings=make_settings(
            {"logging.format": "jsonl", "logging.console_trace": True}
        ),
        ctx=ctx,
        state=object(),
        terminal_outcome=None,
        format_text="json",
    )
    assert code == 0
    out, err = capsys.readouterr()
    assert out == ""
    assert err == ""
    assert ctx.logger.infos[0][1]["event"] == "final_answer"
    assert ctx.logger.infos[0][1]["answer"] == '{"n": 4}'


@pytest.mark.asyncio
async def test_emit_failure_leaves_stdout_empty(capsys):
    ctx = _Ctx()
    code = await aemit_structured_result(
        response="не итог",
        settings=make_settings({"logging.format": "text"}),
        ctx=ctx,
        state=object(),
        terminal_outcome="tool_errors",
        format_text="json",
    )
    assert code == 1
    out, err = capsys.readouterr()
    assert out == ""
    assert "без итога" in err
    assert "tool_errors" in err
    assert err.startswith("[gc] ")
    assert ctx.logger.infos[0] == (
        "final_answer",
        {"event": "final_answer", "answer": "не итог"},
    )
    message, extra = ctx.logger.warnings[0]
    assert extra["event"] == "structured_output_failed"
    assert message == extra["error"]
    assert "tool_errors" in extra["error"]


@pytest.mark.asyncio
async def test_emit_failure_logs_raw_answer_then_rephrase_error(
    monkeypatch, capsys
):
    """Сбой запроса не гасит итог хода: сначала final_answer, потом отказ."""

    async def _boom(**_kwargs):
        raise RuntimeError("backend down")

    monkeypatch.setattr(
        "gc_core.structured_output._arequest_rephrase",
        _boom,
    )
    ctx = _Ctx()
    raw = _finish()
    code = await aemit_structured_result(
        response=raw,
        settings=make_settings({"logging.format": "text"}),
        ctx=ctx,
        state=object(),
        terminal_outcome=None,
        format_text="json",
    )
    assert code == 1
    out, err = capsys.readouterr()
    assert out == ""
    assert err.startswith("[gc] ")
    assert "backend down" in err
    answer = ctx.logger.infos[0][1]["answer"]
    assert ctx.logger.infos[0][1]["event"] == "final_answer"
    assert '"task_report": "Ответ 4."' in answer
    message, extra = ctx.logger.warnings[0]
    assert extra["event"] == "structured_output_failed"
    assert "backend down" in message


class _RunLogger:
    def __init__(self):
        self.events = []

    def info(self, msg, *args, extra=None):
        if extra:
            self.events.append(extra)

    def warning(self, msg, *args, **kwargs):
        return None


class _Session:
    def __init__(self):
        self.meta = {}
        self.backend_api_mode = "chat_completions"
        self.batches = []

    async def apersist_batch(self, messages, *, previous_response_id):
        self.batches.append((list(messages), previous_response_id))


def _executor_with_open_accounting():
    """Учёт ещё не закрыт: токены хода уже в счёте, перефраз впереди."""
    start = UsageAccount()
    usage = UsageAccount()
    usage.add(CATEGORY_MAIN, 100, 10)
    state = SimpleNamespace(
        usage=usage,
        run_start_usage_meta=start.to_meta(),
        last_run_usage=None,
        user_intent_log=[],
        current_user_intent=[],
        user_turn_seq=0,
        dialog_steps_offset=0,
        last_main_prompt_tokens=None,
        last_main_prompt_message_count=None,
        shared=None,
        previous_response_id="resp-1",
        backend_api_mode="chat_completions",
        discard_writes_after_stop=False,
        stop_requested=False,
    )
    logger = _RunLogger()
    ctx = fake_ctx(
        {
            "logging.console_trace": True,
            "logging.format": "text",
            "agent.model": "m",
            "backend.completions_url": "http://llm/v1/chat/completions",
            "backend.request_timeout_sec": 30,
        },
        logger=logger,
        auxiliary_llm=_Aux(),
        model_router=None,
    )
    executor = AgentExecutor(ctx)
    executor._state = state
    executor._thread_session = _Session()
    return executor


@pytest.mark.asyncio
async def test_rephrase_tokens_are_in_the_snapshot_taken_after_it(
    monkeypatch, capsys
):
    """Перефраз сам пишет категорию, снимок учёта снимается уже после вызова."""

    async def _headers(*_args, **_kwargs):
        return {}

    async def _policy(**_kwargs):
        record_usage(
            {"prompt_tokens": 17, "completion_tokens": 9},
            "http://llm/v1/chat/completions",
        )
        return {"choices": [{"message": {"content": "форма"}}]}, {}

    monkeypatch.setattr(
        "gc_core.utils.llm_chat_client.acreate_call_headers",
        _headers,
    )
    monkeypatch.setattr(
        "gc_core.utils.llm_chat_client.arequest_chat_completions_with_policy",
        _policy,
    )
    executor = _executor_with_open_accounting()
    code = await aemit_structured_result(
        response=_finish(),
        settings=executor._ctx,
        ctx=executor._ctx,
        state=executor.get_state(),
        terminal_outcome=None,
        format_text="json",
    )
    assert code == 0
    await executor.aclose_run_accounting(console=False)

    stored = executor._thread_session.meta["token_usage"]["by_category"]
    assert stored[REQUEST_LABEL]["input"] == 17
    assert stored[REQUEST_LABEL]["output"] == 9
    assert stored[CATEGORY_MAIN]["input"] == 100
    run = executor.get_state().last_run_usage
    assert run.by_category[REQUEST_LABEL]["input"] == 17
    assert run.by_category[REQUEST_LABEL]["output"] == 9
    events = [
        extra
        for extra in executor._ctx.logger.events
        if extra.get("event") == "run_usage"
    ]
    assert len(events) == 1
    categories = events[0]["usage"]["categories"]
    assert categories[REQUEST_LABEL]["input"] == 17
    assert categories[REQUEST_LABEL]["output"] == 9
    out, err = capsys.readouterr()
    assert out == "форма\n"
    assert err == ""


@pytest.mark.asyncio
async def test_truncated_rephrase_is_not_printed(monkeypatch, capsys):
    """finish_reason=length читается из choice, фрагмент в stdout не попадает."""

    async def _headers(*_args, **_kwargs):
        return {}

    async def _policy(**_kwargs):
        return {
            "choices": [
                {
                    "message": {"content": '{"result": "answer'},
                    "finish_reason": "length",
                }
            ]
        }, {}

    monkeypatch.setattr(
        "gc_core.utils.llm_chat_client.acreate_call_headers",
        _headers,
    )
    monkeypatch.setattr(
        "gc_core.utils.llm_chat_client.arequest_chat_completions_with_policy",
        _policy,
    )
    executor = _executor_with_open_accounting()
    raw = _finish()
    code = await aemit_structured_result(
        response=raw,
        settings=executor._ctx,
        ctx=executor._ctx,
        state=executor.get_state(),
        terminal_outcome=None,
        format_text="json",
    )
    assert code == 1
    out, err = capsys.readouterr()
    assert out == ""
    assert "оборван" in err
    assert "finish_reason=length" in err
    finals = [
        extra
        for extra in executor._ctx.logger.events
        if extra.get("event") == "final_answer"
    ]
    assert len(finals) == 1
    assert '"task_report"' in finals[0]["answer"]
    assert '"result": "answer' not in finals[0]["answer"]


@pytest.mark.asyncio
async def test_meta_write_error_still_shuts_down_mcp(monkeypatch, tmp_path):
    """Ошибка записи меты не пропускает закрытие клиентов и MCP-демонов."""
    import sys
    from unittest.mock import AsyncMock

    # Импорт cli разбирает sys.argv. Флаг pytest `-q` иначе становится запросом.
    monkeypatch.setattr(sys, "argv", ["gc-cli", "-q", "x"])
    from gc_cli import cli

    fmt = tmp_path / "format.md"
    fmt.write_text("верни JSON\n", encoding="utf-8")
    calls: list[str] = []

    async def _arun(*_args, **_kwargs):
        calls.append("arun")
        return "ok"

    async def _close_accounting():
        calls.append("accounting")
        raise OSError("meta write failed")

    async def _aclose_clients():
        calls.append("clients")

    def _shutdown():
        calls.append("daemons")

    monkeypatch.setattr(cli, "_ensure_project_gc_dir", lambda: None)
    monkeypatch.setattr(cli, "_ensure_user_skills_dir", lambda: None)
    monkeypatch.setattr(
        cli.arg_parser,
        "run_args",
        SimpleNamespace(
            init_config=False,
            sync_config=False,
            reset_config=False,
            check_update=False,
            update=False,
            init=False,
            config="cfg.yml",
            profile=None,
            write_guard=None,
            no_write_guard=False,
            command_moderation=None,
            read_guard=None,
            no_read_guard=False,
            read_guard_mode=None,
            agentsmd_override=False,
            trace_id=None,
            skill=None,
            query="hi",
            wrap_technical=False,
            verbose=False,
            thread_id=None,
            new=False,
            auto_mcp=True,
            structured_output_format=str(fmt),
        ),
    )
    monkeypatch.setattr(
        cli, "load_settings", lambda *_a, **_k: make_settings()
    )
    monkeypatch.setattr(cli, "apply_cli_overrides", lambda d, **_kw: d)
    monkeypatch.setattr(
        "gc_core.upgrade_gate.detect_upgrade", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        "gc_core.upgrade_gate.mark_current_run", lambda *_a, **_k: None
    )
    monkeypatch.setattr(cli, "build_auth_connector", lambda *_a, **_k: None)
    monkeypatch.setattr(cli, "ensure_mcp_daemons", lambda *_a, **_k: None)
    monkeypatch.setattr(cli, "shutdown_mcp_daemons", _shutdown)
    monkeypatch.setattr(
        cli, "acreate_run_context", AsyncMock(return_value=SimpleNamespace())
    )
    monkeypatch.setattr(
        "gc_core.startup_notices.collect_startup_notices",
        lambda *_a, **_k: [],
    )
    monkeypatch.setattr(
        "gc_core.utils.async_http.aclose_clients", _aclose_clients
    )
    monkeypatch.setattr(
        cli, "aemit_structured_result", AsyncMock(return_value=0)
    )
    monkeypatch.setattr(
        cli,
        "AgentExecutor",
        lambda _ctx: SimpleNamespace(
            arun=_arun,
            aclose_run_accounting=_close_accounting,
            get_state=lambda: None,
            last_terminal_outcome=None,
        ),
    )

    with pytest.raises(OSError, match="meta write failed"):
        await cli.arun_cli()

    assert calls == ["arun", "accounting", "clients", "daemons"]
