from pathlib import Path

from gc_core.instructions.layers import (
    instruction_file_paths,
    instruction_layer_specs,
)


def test_specs_ordered_L1_to_L3(tmp_path, monkeypatch):
    from gc_core.config_modules import env_vars

    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc")
    specs = instruction_layer_specs(cwd=str(tmp_path / "proj"))
    assert [s.id for s in specs] == ["L1", "L2", "L3"]
    assert [s.scope for s in specs] == ["global", "project", "project"]


def test_L2_is_gc_AGENTS_md(tmp_path, monkeypatch):
    from gc_core.config_modules import env_vars

    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc")
    specs = instruction_layer_specs(cwd=str(tmp_path / "proj"))
    l2 = next(s for s in specs if s.id == "L2")
    assert l2.path == (tmp_path / "proj" / ".gc" / "AGENTS.md")


def test_file_paths_excludes_L0(tmp_path, monkeypatch):
    from gc_core.config_modules import env_vars

    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc")
    paths = instruction_file_paths(cwd=str(tmp_path / "proj"))
    assert set(paths) == {"L1", "L2", "L3"}
    assert paths["L2"] == (tmp_path / "proj" / ".gc" / "AGENTS.md")
    assert paths["L3"] == (tmp_path / "proj" / "AGENTS.md")


def test_l2_is_gc_and_l3_is_root(tmp_path, monkeypatch):
    from gc_core.config_modules import env_vars

    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc")
    files = instruction_file_paths(cwd=str(tmp_path / "proj"))
    assert files["L2"] == Path(tmp_path / "proj") / ".gc" / "AGENTS.md"
    assert files["L3"] == Path(tmp_path / "proj") / "AGENTS.md"
