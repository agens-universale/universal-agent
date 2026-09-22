
from gc_testkit.paths import ASSETS


def test_bundled_root_contains_prompts():
    from gc_core.bundled_assets import bundled_root
    root = bundled_root()
    assert (ASSETS / "prompts").is_dir()
    assert (ASSETS / "prompts" / "chat_completions_system_prompt.md").is_file()


def test_bundled_root_has_config_default():
    # config.default.yml лежит рядом с prompts/ (создаётся в Task 2;
    # тест проверяет инвариант расположения).
    from gc_core.bundled_assets import bundled_root
    assert (bundled_root() / "config.default.yml").is_file()


def test_tui_special_instructions_path_resolves():
    from gc_core.bundled_assets import bundled_root
    from gc_tui.services.prompt_assets import tui_special_instructions_path
    p = tui_special_instructions_path()
    assert p == str(bundled_root() / "prompts" / "chat_completions_tui_special_instructions.md")
    import os
    assert os.path.isfile(p)
