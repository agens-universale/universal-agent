"""Дефолт-промпты модулей резолвятся через bundled_root() (а не через ../../..,
который промахивается в wheel). Каждый путь должен лежать под bundled_root()/prompts
и реально существовать — иначе в method-2 фича упадёт «prompt not found»."""

from pathlib import Path

from gc_core.bundled_assets import bundled_root


def _bp(*parts: str) -> Path:
    return bundled_root().joinpath(*parts)


def test_intent_guard_prompt_under_bundled_root():
    from gc_core.intent_guard.final_report_consistency import DEFAULT_INTENT_GUARD_PROMPT_PATH

    assert Path(DEFAULT_INTENT_GUARD_PROMPT_PATH) == _bp("prompts", "chat_completions_intent_guard_prompt.md")
    assert Path(DEFAULT_INTENT_GUARD_PROMPT_PATH).is_file()


def test_command_judge_prompts_under_bundled_root():
    from gc_core.security import command_moderation as cm

    for p in (cm._COMMAND_JUDGE_SYSTEM_PROMPT_PATH, cm._COMMAND_JUDGE_USER_PROMPT_PATH):
        assert Path(p).parent == _bp("prompts", "security")
        assert Path(p).is_file()


def test_intent_scope_prompt_under_bundled_root():
    from gc_core.intent_scope.summarizer import _DEFAULT_PROMPT_PATH

    assert Path(_DEFAULT_PROMPT_PATH) == _bp("prompts", "intent_scope_tool_summary_prompt.md")
    assert Path(_DEFAULT_PROMPT_PATH).is_file()


def test_memory_patch_prompts_under_bundled_root():
    from gc_core.summarize.memory_suggestions import DEFAULT_MEMORY_PATCH_REPLACE_PROMPT_PATH

    assert Path(DEFAULT_MEMORY_PATCH_REPLACE_PROMPT_PATH) == _bp("prompts", "chat_completions_memory_patch_replace.md")
    assert Path(DEFAULT_MEMORY_PATCH_REPLACE_PROMPT_PATH).is_file()


def test_summary_prompt_under_bundled_root():
    from gc_core.summarize.prompt_loading import default_prompt_path

    assert Path(default_prompt_path()) == _bp("prompts", "chat_completions_summary_prompt.md")
    assert Path(default_prompt_path()).is_file()


def test_project_memory_compaction_prompt_under_bundled_root():
    from gc_core.project_memory.compaction import _COMPACTION_PROMPT_PATH

    assert Path(_COMPACTION_PROMPT_PATH) == _bp("prompts", "project_memory_compaction_prompt.md")
    assert Path(_COMPACTION_PROMPT_PATH).is_file()


def test_subagent_system_prompt_asset_present():
    # runner._resolve_subagent_system_prompt() резолвит дефолт через bundled_root();
    # минимум — ассет на месте под bundled_root (иначе субагенты упадут в method-2).
    assert _bp("prompts", "chat_completions_subagent_system_prompt.md").is_file()


def test_server_agent_prompt_under_bundled_root():
    # server_agentmd_path по умолчанию "./prompts/server_agent.md" резолвится
    # относительно PROJECT_ROOT → должен указывать на bundled-промпт.
    import os

    from gc_server.rest_api import server_config

    resolved = os.path.join(server_config.PROJECT_ROOT, "prompts", "server_agent.md")
    assert Path(resolved) == _bp("prompts", "server_agent.md")
    assert Path(resolved).is_file()
