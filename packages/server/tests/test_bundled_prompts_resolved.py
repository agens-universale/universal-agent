"""Дефолт-промпты модулей резолвятся через bundled_root() (а не через ../../..,
который промахивается в wheel). Каждый путь должен лежать под bundled_root()/prompts
и реально существовать — иначе в method-2 фича упадёт «prompt not found»."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from gc_core.bundled_assets import bundled_root


def _bp(*parts: str) -> Path:
    return bundled_root().joinpath(*parts)


@pytest.fixture
def foreign_prompt_workspace(tmp_path, monkeypatch):
    for relative in (
        "security/command_judge_system.md",
        "security/command_judge_user.md",
        "intent_scope_tool_summary_prompt.md",
        "chat_completions_memory_patch_replace.md",
        "project_memory_compaction_prompt.md",
    ):
        shadow = tmp_path / "prompts" / relative
        shadow.parent.mkdir(parents=True, exist_ok=True)
        shadow.write_text(
            "Foreign workspace prompt must not be loaded", encoding="utf-8"
        )
    monkeypatch.chdir(tmp_path)
    return SimpleNamespace(config_dict={})


def test_intent_guard_prompt_under_bundled_root():
    from gc_core.intent_guard.final_report_consistency import (
        DEFAULT_INTENT_GUARD_PROMPT_PATH,
    )

    assert Path(DEFAULT_INTENT_GUARD_PROMPT_PATH) == _bp(
        "prompts", "chat_completions_intent_guard_prompt.md"
    )
    assert Path(DEFAULT_INTENT_GUARD_PROMPT_PATH).is_file()


def test_command_judge_prompts_under_bundled_root(
    foreign_prompt_workspace, tmp_path
):
    from gc_core.execution.guard_contracts import GuardProbeRequest
    from gc_core.security.command_moderation import load_command_judge_prompt
    from gc_core.security.guard_probe import evaluate_guard_probe

    facts = evaluate_guard_probe(
        GuardProbeRequest(
            "command_prompts", "exec_command", "", cwd=str(tmp_path)
        ),
        config_dict=foreign_prompt_workspace.config_dict,
    )
    for kind in ("system", "user"):
        path = _bp("prompts", "security", f"command_judge_{kind}.md")
        assert path.is_file()
        expected = path.read_text(encoding="utf-8").strip()
        assert load_command_judge_prompt(kind) == expected
        assert facts.data[kind] == expected


def test_intent_scope_prompt_under_bundled_root(foreign_prompt_workspace):
    from gc_core.execution.resources import PromptResourceRequest
    from gc_core.intent_scope.summarizer import (
        _load_system_prompt,
        _resolve_prompt_path,
    )
    from gc_core.resource_loading import load_prompt_resource

    path = _bp("prompts", "intent_scope_tool_summary_prompt.md")
    assert path.is_file()
    assert Path(_resolve_prompt_path(foreign_prompt_workspace)) == path
    expected = path.read_text(encoding="utf-8").strip()
    assert _load_system_prompt(foreign_prompt_workspace) == expected
    assert (
        load_prompt_resource(
            PromptResourceRequest("intent_tool"), ctx=foreign_prompt_workspace
        )
        == expected
    )


def test_memory_patch_prompts_under_bundled_root(foreign_prompt_workspace):
    from gc_core.execution.resources import PromptResourceRequest
    from gc_core.resource_loading import load_prompt_resource
    from gc_core.summarize.memory_suggestions import (
        _resolve_prompt_path,
        default_memory_patch_path,
    )
    from gc_core.summarize.prompt_loading import (
        load_memory_patch_replace_prompt,
    )

    path = _bp("prompts", "chat_completions_memory_patch_replace.md")
    assert path.is_file()
    assert Path(default_memory_patch_path()) == path
    resolved = _resolve_prompt_path(
        foreign_prompt_workspace,
        "chat_completions_memory_patch_replace_prompt_path",
        default_memory_patch_path(),
    )
    assert Path(resolved) == path
    # The production reader expands memory tags and subagent placeholders.
    expected = load_memory_patch_replace_prompt(str(path))
    assert expected
    assert "Foreign workspace prompt" not in expected
    assert (
        load_prompt_resource(
            PromptResourceRequest("memory_patch"), ctx=foreign_prompt_workspace
        )
        == expected
    )


def test_summary_prompt_under_bundled_root():
    from gc_core.summarize.prompt_loading import default_prompt_path

    assert Path(default_prompt_path()) == _bp(
        "prompts", "chat_completions_summary_prompt.md"
    )
    assert Path(default_prompt_path()).is_file()


def test_project_memory_compaction_prompt_under_bundled_root(
    foreign_prompt_workspace,
):
    from gc_core.execution.resources import PromptResourceRequest
    from gc_core.project_memory.compaction import _load_prompt
    from gc_core.resource_loading import load_prompt_resource

    path = _bp("prompts", "project_memory_compaction_prompt.md")
    assert path.is_file()
    expected = path.read_text(encoding="utf-8")
    assert _load_prompt() == expected
    assert (
        load_prompt_resource(
            PromptResourceRequest("project_memory"),
            ctx=foreign_prompt_workspace,
        )
        == expected
    )


def test_subagent_system_prompt_asset_present():
    # runner._resolve_subagent_system_prompt() резолвит дефолт через bundled_root();
    # минимум — ассет на месте под bundled_root (иначе субагенты упадут в method-2).
    assert _bp(
        "prompts", "chat_completions_subagent_system_prompt.md"
    ).is_file()


def test_server_agent_prompt_under_bundled_root():
    # server_agentmd_path по умолчанию "./prompts/server_agent.md" резолвится
    # относительно PROJECT_ROOT → должен указывать на bundled-промпт.
    import os

    from gc_server.rest_api import server_config

    resolved = os.path.join(
        server_config.PROJECT_ROOT, "prompts", "server_agent.md"
    )
    assert Path(resolved) == _bp("prompts", "server_agent.md")
    assert Path(resolved).is_file()
