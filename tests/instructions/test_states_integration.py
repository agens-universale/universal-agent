# tests/instructions/test_states_integration.py
from states import State


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
    from config_modules import env_vars
    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc")
    monkeypatch.chdir(tmp_path)
    l0 = tmp_path / "demo.md"
    _write(l0, "L0BODY")
    _write(tmp_path / "AGENTS.md", "PROJECTBODY")

    state = _make_state(agent_md_path=str(l0))
    meta = state.set_call_metadata()

    ui = meta["promptVariables"]["userInstructions"]
    assert "<user_defined_instructions>" in ui
    assert "L0BODY" in ui and "PROJECTBODY" in ui
    assert "## Project instructions (AGENTS.md)" in ui
