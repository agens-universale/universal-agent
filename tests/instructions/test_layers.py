from pathlib import Path
from instructions.layers import instruction_layer_specs, instruction_file_paths


def test_specs_ordered_L0_to_L3(tmp_path, monkeypatch):
    from config_modules import env_vars
    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc")
    specs = instruction_layer_specs(agent_md_path="/bundled/demo.md", cwd=str(tmp_path / "proj"))
    assert [s.id for s in specs] == ["L0", "L1", "L2", "L3"]
    assert [s.scope for s in specs] == ["shipped", "global", "project", "project"]


def test_L2_is_root_AGENTS_md(tmp_path, monkeypatch):
    from config_modules import env_vars
    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc")
    specs = instruction_layer_specs(agent_md_path=None, cwd=str(tmp_path / "proj"))
    l2 = next(s for s in specs if s.id == "L2")
    assert l2.path == (tmp_path / "proj" / "AGENTS.md")


def test_file_paths_excludes_L0(tmp_path, monkeypatch):
    from config_modules import env_vars
    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc")
    paths = instruction_file_paths(cwd=str(tmp_path / "proj"))
    assert set(paths) == {"L1", "L2", "L3"}
    assert paths["L3"] == (tmp_path / "proj" / ".gc" / "GIGACODE.md")
