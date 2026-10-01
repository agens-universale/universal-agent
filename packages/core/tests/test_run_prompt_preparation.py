"""Prepared prompts preserve local semantics without workflow-side file reads."""

from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from gc_core.agent_executor import AgentExecutor
from gc_core.run_context import acreate_run_context
from gc_core.run_preparation import (
    PreparedPrompts,
    PromptPreparationRequest,
    load_run_prompts,
)


def test_local_prompt_files_and_steering_keep_existing_order(tmp_path):
    system = tmp_path / "system.md"
    special = tmp_path / "special.md"
    system.write_text("  system rules\n", encoding="utf-8")
    special.write_text("  client rules\n", encoding="utf-8")
    request = PromptPreparationRequest.from_config(
        {
            "chat_completions_system_prompt_path": str(system),
            "chat_completions_special_instructions_path": f" {special} ",
            "model_steering": "  be concise  ",
        },
        verbose=False,
    )

    result = load_run_prompts(request)

    assert result.system_prompt == "system rules"
    assert result.special_instructions == (
        "client rules\n\n### Model steering\nbe concise"
    )


@pytest.mark.parametrize("unreadable", ["missing", "invalid_utf8"])
def test_required_prompt_failure_keeps_diagnostic_and_exception(
    tmp_path, unreadable, capsys
):
    path = tmp_path / "system.md"
    error = FileNotFoundError
    if unreadable == "invalid_utf8":
        path.write_bytes(b"\xff")
        error = UnicodeDecodeError
    logger = Mock()

    with pytest.raises(error):
        load_run_prompts(
            PromptPreparationRequest(
                system_prompt_path=str(path), verbose=True
            ),
            logger=logger,
        )

    assert str(path) in capsys.readouterr().out
    logger.warning.assert_called_once_with(
        "system_prompt_read_failed",
        extra={"event": "system_prompt_read_failed", "path": str(path)},
    )


@pytest.mark.parametrize("unreadable", ["missing", "invalid_utf8"])
def test_optional_prompt_failure_retains_model_steering(tmp_path, unreadable):
    path = tmp_path / "special.md"
    if unreadable == "invalid_utf8":
        path.write_bytes(b"\xff")

    result = load_run_prompts(
        PromptPreparationRequest(
            special_instructions_path=str(path), model_steering="be concise"
        )
    )

    assert result == PreparedPrompts(
        special_instructions="### Model steering\nbe concise"
    )


async def test_executor_uses_prepared_prompts_without_local_io(monkeypatch):
    requests = []

    async def loader(request):
        requests.append(asdict(request))
        return PreparedPrompts("remote system", "remote special")

    config = {
        "chat_completions_system_prompt_path": "/worker/system.md",
        "model_steering": "concise",
        "api_key": "credential-not-to-be-sent",
    }
    executor = AgentExecutor(
        SimpleNamespace(config_dict=config, prompt_loader=loader)
    )

    def forbidden(*_args, **_kwargs):
        raise AssertionError(
            "prepared workflow path must not read local files"
        )

    monkeypatch.setattr("builtins.open", forbidden)
    result = await executor._aload_chat_completions_prompts(
        effective_verbose=False, logger=None
    )

    assert result == ("remote system", "remote special")
    assert requests == [
        {
            "system_prompt_path": "/worker/system.md",
            "special_instructions_path": None,
            "model_steering": "concise",
            "verbose": False,
        }
    ]


async def test_injected_loader_error_never_falls_back_to_local_files():
    failure = RuntimeError("resource unavailable")

    async def loader(_request):
        raise failure

    executor = AgentExecutor(
        SimpleNamespace(
            config_dict={"chat_completions_system_prompt_path": "/missing"},
            prompt_loader=loader,
        )
    )

    with pytest.raises(RuntimeError) as caught:
        await executor._aload_chat_completions_prompts(
            effective_verbose=False, logger=None
        )
    assert caught.value is failure


async def test_sdk_context_accepts_prompt_loader(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    async def loader(_request):
        return PreparedPrompts("provided system")

    ctx = await acreate_run_context(
        {"save_logs": False, "backend_api_mode": "chat_completions"},
        prompt_loader=loader,
    )
    executor = AgentExecutor(ctx)

    assert await executor._aload_chat_completions_prompts(
        effective_verbose=False, logger=ctx.logger
    ) == ("provided system", None)


@pytest.mark.parametrize("steering", ["", "  be concise  "])
async def test_model_steering_reader_matches_local_and_prepared_port(steering):
    """Exercise the current config reader through both executor boundaries."""
    config = {"model_steering": steering}

    async def loader(request):
        return load_run_prompts(request)

    local = AgentExecutor(
        SimpleNamespace(config_dict=config, prompt_loader=None)
    )
    prepared = AgentExecutor(
        SimpleNamespace(config_dict=config, prompt_loader=loader)
    )
    expected = (
        None,
        "### Model steering\nbe concise" if steering.strip() else None,
    )
    assert (
        await local._aload_chat_completions_prompts(
            effective_verbose=False, logger=None
        )
        == expected
    )
    assert (
        await prepared._aload_chat_completions_prompts(
            effective_verbose=False, logger=None
        )
        == expected
    )
