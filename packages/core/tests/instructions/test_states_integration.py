# tests/instructions/test_states_integration.py
from gc_core.states import State


def _write(p, t):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(t, encoding="utf-8")


def _make_state(**kwargs) -> State:
    defaults = dict(
        verbose=False,
        model="CodeChat",
        tools_spec=[],
        tools_descr=[],
        skills_list=[],
        max_steps=2,
        system_prompt_id="v1",
        use_server_subagents=False,
    )
    defaults.update(kwargs)
    return State(**defaults)


def test_userInstructions_contains_merged_layers(tmp_path, monkeypatch):
    from gc_core.config_modules import env_vars
    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc")
    monkeypatch.chdir(tmp_path)
    l0 = tmp_path / "demo.md"
    _write(l0, "L0BODY")
    _write(tmp_path / "AGENTS.md", "PROJECTBODY")

    state = _make_state(agent_md_path=str(l0))
    meta = state.set_call_metadata()

    ui = meta["promptVariables"]["userInstructions"]
    assert "<user_defined_instructions>" in ui
    assert "L0BODY" in ui and "PROJECTBODY" not in ui


def test_state_cli_no_layers(tmp_path, monkeypatch):
    """State(is_tui=False) без agent_md_path → userInstructions отсутствует (CLI не загружает слои)."""
    from gc_core.config_modules import env_vars
    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc")
    monkeypatch.chdir(tmp_path)
    _write(tmp_path / "AGENTS.md", "ROOT RULES")

    state = _make_state(is_tui=False, agent_md_path="")
    meta = state.set_call_metadata()

    assert "ROOT RULES" not in meta["promptVariables"].get("userInstructions", "")


def test_state_tui_loads_agentsmd(tmp_path, monkeypatch):
    """State(is_tui=True) без agent_md_path → AGENTS.md попадает в userInstructions."""
    from gc_core.config_modules import env_vars
    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc")
    monkeypatch.chdir(tmp_path)
    _write(tmp_path / "AGENTS.md", "ROOT RULES")

    state = _make_state(is_tui=True, agent_md_path="")
    meta = state.set_call_metadata()

    ui = meta["promptVariables"].get("userInstructions", "")
    assert "ROOT RULES" in ui


def test_state_agentsmd_override_project_only(tmp_path, monkeypatch):
    """State(is_tui=True, agentsmd_override=True) без agent_md_path → только L3 (AGENTS.md)."""
    from gc_core.config_modules import env_vars
    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc")
    monkeypatch.chdir(tmp_path)
    gc_global = tmp_path / "gc"
    _write(gc_global / "AGENTS.md", "GLOBAL RULES")
    _write(tmp_path / "AGENTS.md", "PROJECT RULES")

    state = _make_state(is_tui=True, agentsmd_override=True, agent_md_path="")
    meta = state.set_call_metadata()

    ui = meta["promptVariables"].get("userInstructions", "")
    assert "PROJECT RULES" in ui
    assert "GLOBAL RULES" not in ui


def test_state_agentsmd_override_false_includes_global(tmp_path, monkeypatch):
    """State(is_tui=True, agentsmd_override=False) → оба слоя (глобальный + проектный)."""
    from gc_core.config_modules import env_vars
    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc")
    monkeypatch.chdir(tmp_path)
    gc_global = tmp_path / "gc"
    _write(gc_global / "AGENTS.md", "GLOBAL RULES")
    _write(tmp_path / "AGENTS.md", "PROJECT RULES")

    state = _make_state(is_tui=True, agentsmd_override=False, agent_md_path="")
    meta = state.set_call_metadata()

    ui = meta["promptVariables"].get("userInstructions", "")
    assert "PROJECT RULES" in ui
    assert "GLOBAL RULES" in ui


def test_state_exposes_override_conflict_warning(tmp_path, monkeypatch):
    """State with is_tui=True, agent_md_path, agentsmd_override=True → warning in state.instruction_warnings."""
    from gc_core.config_modules import env_vars
    from gc_core.instructions.loader import AGENTSMD_OVERRIDE_IGNORED_WARNING
    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc")
    monkeypatch.chdir(tmp_path)
    role = tmp_path / "role.md"
    _write(role, "ROLE PROMPT")

    state = _make_state(is_tui=True, agent_md_path=str(role), agentsmd_override=True)

    assert AGENTSMD_OVERRIDE_IGNORED_WARNING in state.instruction_warnings


def test_state_no_warning_without_agent_md_path(tmp_path, monkeypatch):
    """State(is_tui=True, agentsmd_override=True) без agent_md_path → no warnings."""
    from gc_core.config_modules import env_vars
    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc")
    monkeypatch.chdir(tmp_path)
    _write(tmp_path / "AGENTS.md", "PROJECT RULES")

    state = _make_state(is_tui=True, agent_md_path="", agentsmd_override=True)

    assert state.instruction_warnings == []
