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
