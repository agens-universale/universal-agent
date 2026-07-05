from config_modules.paths import PathsConfig


def test_agent_md_path_default_is_empty():
    assert PathsConfig().agent_md_path == ""
