import os
from config_modules.root import AppConfig, resolve_paths
from bundled_assets import bundled_root


def test_system_prompt_falls_back_to_bundled(tmp_path):
    # config_dir без prompts/ рядом → дефолт должен указать в bundled_root().
    cfg = AppConfig.model_validate({})
    resolve_paths(cfg, str(tmp_path))
    expected = str(bundled_root() / "prompts" / "chat_completions_system_prompt.md")
    assert cfg.paths.chat_completions_system_prompt_path == expected


def test_system_prompt_prefers_local_prompts(tmp_path):
    # Если рядом с конфигом есть prompts/<file> — берём его.
    pdir = tmp_path / "prompts"
    pdir.mkdir()
    local = pdir / "chat_completions_system_prompt.md"
    local.write_text("x", encoding="utf-8")
    cfg = AppConfig.model_validate({})
    resolve_paths(cfg, str(tmp_path))
    assert cfg.paths.chat_completions_system_prompt_path == str(local)


def test_all_prompt_defaults_resolve_to_bundled(tmp_path):
    """Пустой config_dir (нет prompts/ рядом) → все 5 дефолтных путей промптов
    указывают в bundled_root()/prompts/<name>. Ловит опечатку в имени файла
    в любом из 5 call-site _prompt_default."""
    cfg = AppConfig.model_validate({})
    resolve_paths(cfg, str(tmp_path))
    pr = bundled_root() / "prompts"
    assert cfg.paths.chat_completions_system_prompt_path == str(pr / "chat_completions_system_prompt.md")
    assert cfg.summary.chat_completions_summary_prompt_path == str(pr / "chat_completions_summary_prompt.md")
    assert cfg.summary.chat_completions_memory_patch_append_prompt_path == str(pr / "chat_completions_memory_patch_append.md")
    assert cfg.summary.chat_completions_memory_patch_replace_prompt_path == str(pr / "chat_completions_memory_patch_replace.md")
    assert cfg.intent_scope.intent_scope_tool_summary_prompt_path == str(pr / "intent_scope_tool_summary_prompt.md")
