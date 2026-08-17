from gc_core.config_modules.paths import PathsConfig


def test_paths_has_special_instructions_field_default_none():
    p = PathsConfig()
    assert p.chat_completions_special_instructions_path is None


def test_paths_accepts_special_instructions_path():
    p = PathsConfig(chat_completions_special_instructions_path="/x/y.md")
    assert p.chat_completions_special_instructions_path == "/x/y.md"
