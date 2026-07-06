from pathlib import Path

from instructions.loader import build_instructions, AGENTSMD_OVERRIDE_IGNORED_WARNING


def _write(p: Path, text: str):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def test_agent_md_given_replaces_everything(tmp_path):
    # L0 задан → только он, L1-L3 игнорируются даже в TUI.
    l0 = tmp_path / "role.md"
    _write(l0, "ROLE PROMPT")
    _write(tmp_path / "AGENTS.md", "PROJECT RULES")
    r = build_instructions(agent_md_path=str(l0), is_tui=True, cwd=str(tmp_path))
    assert r.loaded_ids == ["L0"]
    assert "ROLE PROMPT" in r.text
    assert "PROJECT RULES" not in r.text


def test_cli_without_agent_md_is_empty(tmp_path):
    # не TUI, нет L0 → пусто (L1-L3 не грузятся).
    _write(tmp_path / "AGENTS.md", "PROJECT RULES")
    r = build_instructions(agent_md_path="", is_tui=False, cwd=str(tmp_path))
    assert r.text == ""
    assert r.loaded_ids == []


def test_tui_assembles_layers(tmp_path, monkeypatch):
    from config_modules import env_vars
    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "no_global")  # L1 absent
    _write(tmp_path / ".gc" / "AGENTS.md", "GC RULES")   # L2
    _write(tmp_path / "AGENTS.md", "ROOT RULES")          # L3
    r = build_instructions(agent_md_path="", is_tui=True, cwd=str(tmp_path))
    assert r.loaded_ids == ["L2", "L3"]
    assert "GC RULES" in r.text and "ROOT RULES" in r.text
    # L3 (корневой) идёт последним = сильнейший
    assert r.text.index("GC RULES") < r.text.index("ROOT RULES")


def test_tui_override_keeps_only_l3(tmp_path):
    _write(tmp_path / ".gc" / "AGENTS.md", "GC RULES")
    _write(tmp_path / "AGENTS.md", "ROOT RULES")
    r = build_instructions(agent_md_path="", is_tui=True, agentsmd_override=True, cwd=str(tmp_path))
    assert r.loaded_ids == ["L3"]
    assert "GC RULES" not in r.text and "ROOT RULES" in r.text


def test_path_beats_override_with_warning(tmp_path):
    l0 = tmp_path / "role.md"
    _write(l0, "ROLE")
    r = build_instructions(agent_md_path=str(l0), is_tui=True, agentsmd_override=True, cwd=str(tmp_path))
    assert r.loaded_ids == ["L0"]
    assert AGENTSMD_OVERRIDE_IGNORED_WARNING in r.warnings


def test_override_no_warning_when_no_path(tmp_path):
    _write(tmp_path / "AGENTS.md", "ROOT")
    r = build_instructions(agent_md_path="", is_tui=True, agentsmd_override=True, cwd=str(tmp_path))
    assert r.warnings == []
