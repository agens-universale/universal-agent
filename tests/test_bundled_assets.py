from pathlib import Path


def test_bundled_root_contains_prompts():
    from bundled_assets import bundled_root
    root = bundled_root()
    assert (root / "prompts").is_dir()
    assert (root / "prompts" / "chat_completions_system_prompt.md").is_file()


def test_bundled_root_has_config_default(tmp_path):
    # config.default.yml лежит рядом с prompts/ (создаётся в Task 2;
    # тест проверяет инвариант расположения).
    from bundled_assets import bundled_root
    assert (bundled_root() / "config.default.yml").is_file()
