"""--structured-output-format: файл формы, поля turn_finish, один aux-запрос."""

import json
import sys

import pytest
from gc_cli.structured_output import (
    REQUEST_LABEL,
    StructuredOutputError,
    aemit_structured_result,
    arephrase_turn_outcome,
    read_format_description,
    turn_outcome_text,
)
from gc_core.config_modules.chat_completions import (
    DEFAULT_AUXILIARY_LLM_MAX_TOKENS,
)


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

    def info(self, msg, extra=None):
        self.infos.append((msg, extra))

    def warning(self, msg, *args):
        self.warnings.append(msg % args if args else msg)


class _Ctx:
    def __init__(self):
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
        return "  ## Итог\n4\n"

    monkeypatch.setattr(
        "gc_cli.structured_output.arequest_auxiliary_completion_text",
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
    assert captured["payload_overrides"] == {}
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
        return "ok"

    monkeypatch.setattr(
        "gc_cli.structured_output.arequest_auxiliary_completion_text",
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
        return "ok"

    monkeypatch.setattr(
        "gc_cli.structured_output.arequest_auxiliary_completion_text",
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
        return "  \n"

    monkeypatch.setattr(
        "gc_cli.structured_output.arequest_auxiliary_completion_text",
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
async def test_rephrase_transport_error_stays_structured(monkeypatch):
    async def _boom(**_kwargs):
        raise RuntimeError("backend down")

    monkeypatch.setattr(
        "gc_cli.structured_output.arequest_auxiliary_completion_text",
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
async def test_emit_prints_even_when_console_trace_off(monkeypatch, capsys):
    async def _ok(**_kwargs):
        return '{"n": 4}'

    monkeypatch.setattr(
        "gc_cli.structured_output.arequest_auxiliary_completion_text",
        _ok,
    )
    ctx = _Ctx()
    code = await aemit_structured_result(
        response=_finish(),
        config_dict={"console_trace": False, "logging_format": "text"},
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
async def test_emit_jsonl_does_not_print_a_line(monkeypatch, capsys):
    async def _ok(**_kwargs):
        return "only-in-event"

    monkeypatch.setattr(
        "gc_cli.structured_output.arequest_auxiliary_completion_text",
        _ok,
    )
    ctx = _Ctx()
    code = await aemit_structured_result(
        response=_finish(),
        config_dict={"logging_format": "jsonl", "console_trace": True},
        ctx=ctx,
        state=object(),
        terminal_outcome=None,
        format_text="json",
    )
    assert code == 0
    out, err = capsys.readouterr()
    assert out == ""
    assert err == ""
    assert ctx.logger.infos[0][1]["answer"] == "only-in-event"
    assert ctx.logger.infos[0][1]["event"] == "final_answer"


@pytest.mark.asyncio
async def test_emit_failure_leaves_stdout_empty(capsys):
    ctx = _Ctx()
    code = await aemit_structured_result(
        response="не итог",
        config_dict={"logging_format": "text"},
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
    assert ctx.logger.infos == []
    assert ctx.logger.warnings
