"""Дефолт-промпты модулей резолвятся через bundled_root() (а не через ../../..,
который промахивается в wheel). Каждый путь должен лежать под bundled_root()/prompts
и реально существовать — иначе в method-2 фича упадёт «prompt not found»."""

from pathlib import Path

import pytest
from gc_core.bundled_assets import bundled_root
from gc_testkit.settings import fake_ctx


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
    return fake_ctx()


def test_intent_guard_prompt_under_bundled_root():
    from gc_core.intent_guard.final_report_consistency import (
        DEFAULT_INTENT_GUARD_PROMPT_PATH,
    )

    assert Path(DEFAULT_INTENT_GUARD_PROMPT_PATH) == _bp(
        "prompts", "chat_completions_intent_guard_prompt.md"
    )
    assert Path(DEFAULT_INTENT_GUARD_PROMPT_PATH).is_file()


def test_routing_pair_judge_prompt_under_bundled_root():
    from gc_core.auto_routing.pair_judge import DEFAULT_PROMPT_PATH

    assert Path(DEFAULT_PROMPT_PATH) == _bp(
        "prompts", "chat_completions_routing_pair_judge_prompt.md"
    )
    assert Path(DEFAULT_PROMPT_PATH).is_file()


def _store(ctx):
    from gc_core.agent_store import agent_store_of

    return agent_store_of(ctx)


def _worker_read(kind: str, ctx) -> str:
    from gc_core.execution.resources import PromptResourceRequest
    from gc_core.resource_loading import aload_prompt_resource_here
    from gc_testkit import run_coro

    return run_coro(
        aload_prompt_resource_here(PromptResourceRequest(kind), ctx=ctx)
    )


def test_command_judge_prompts_under_bundled_root(
    foreign_prompt_workspace, tmp_path
):
    from gc_core.execution.guard_contracts import GuardProbeRequest
    from gc_core.security.command_moderation import aload_command_judge_prompt
    from gc_core.security.guard_probe import evaluate_guard_probe
    from gc_testkit import run_coro

    facts = evaluate_guard_probe(
        GuardProbeRequest(
            "command_prompts", "exec_command", "", cwd=str(tmp_path)
        ),
        settings=foreign_prompt_workspace,
    )
    store = _store(foreign_prompt_workspace)
    for kind in ("system", "user"):
        path = _bp("prompts", "security", f"command_judge_{kind}.md")
        assert path.is_file()
        assert Path(store.label("prompt", f"command_judge_{kind}")) == path
        expected = path.read_text(encoding="utf-8").strip()
        assert (
            run_coro(aload_command_judge_prompt(kind, store=store)) == expected
        )
        assert facts.data[kind] == expected


def test_intent_scope_prompt_under_bundled_root(foreign_prompt_workspace):
    from gc_core.intent_scope.summarizer import aload_intent_prompt
    from gc_testkit import run_coro

    path = _bp("prompts", "intent_scope_tool_summary_prompt.md")
    assert path.is_file()
    store = _store(foreign_prompt_workspace)
    assert Path(store.label("prompt", "intent_tool")) == path
    expected = path.read_text(encoding="utf-8").strip()
    assert run_coro(aload_intent_prompt(store, "intent_tool")) == expected
    assert _worker_read("intent_tool", foreign_prompt_workspace) == expected


def test_memory_patch_prompts_under_bundled_root(foreign_prompt_workspace):
    from gc_core.summarize.prompt_loading import aload_memory_patch_prompt
    from gc_testkit import run_coro

    path = _bp("prompts", "chat_completions_memory_patch_replace.md")
    assert path.is_file()
    store = _store(foreign_prompt_workspace)
    assert Path(store.label("prompt", "memory_patch")) == path
    # The production reader expands memory tags and subagent placeholders.
    expected = run_coro(aload_memory_patch_prompt(store))
    assert expected
    assert "Foreign workspace prompt" not in expected
    assert _worker_read("memory_patch", foreign_prompt_workspace) == expected


def test_summary_prompt_under_bundled_root():
    from gc_core.agent_store import FileAgentStore

    path = FileAgentStore().label("prompt", "summary")
    assert Path(path) == _bp("prompts", "chat_completions_summary_prompt.md")
    assert Path(path).is_file()


def test_project_memory_compaction_prompt_under_bundled_root(
    foreign_prompt_workspace,
):
    from gc_core.project_memory.compaction import aload_compaction_prompt
    from gc_testkit import run_coro

    path = _bp("prompts", "project_memory_compaction_prompt.md")
    assert path.is_file()
    expected = path.read_text(encoding="utf-8")
    store = _store(foreign_prompt_workspace)
    assert run_coro(aload_compaction_prompt(store)) == expected
    assert _worker_read("project_memory", foreign_prompt_workspace) == expected


def test_subagent_system_prompt_asset_present():
    # runner._aresolve_subagent_system_prompt() резолвит дефолт через bundled_root();
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
