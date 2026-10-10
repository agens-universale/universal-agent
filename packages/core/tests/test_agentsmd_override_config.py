from gc_core.config_modules.paths import PathsConfig
from gc_core.config_modules.tui import TuiConfig


def test_agentsmd_override_lives_in_tui_default_false():
    assert TuiConfig().agentsmd_override is False


def test_instructions_overwrite_removed_from_paths():
    assert not hasattr(PathsConfig(), "instructions_overwrite")


def test_agentsmd_override_e2e_config_to_state(tmp_path):
    """tui.agentsmd_override: true в конфиге → настройки → State.agentsmd_override is True."""
    from gc_core import config as cfgmod
    from gc_core.states import State

    conf = tmp_path / "config.yml"
    conf.write_text("tui:\n  agentsmd_override: true\n", encoding="utf-8")
    settings = cfgmod.load_settings(str(conf)).settings

    assert settings.tui.agentsmd_override is True

    state = State(
        verbose=False,
        model="CodeChat",
        tools_spec=[],
        tools_descr=[],
        skills_list=[],
        max_steps=2,
        system_prompt_id="v1",
        use_server_subagents=False,
        agentsmd_override=settings.tui.agentsmd_override,
    )

    assert state.agentsmd_override is True
