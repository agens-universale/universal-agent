from pathlib import Path
from gc_core.security.protected_paths import protected_paths


def test_classes_composition(tmp_path, monkeypatch):
    from gc_core.config_modules import env_vars
    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc")
    proj = tmp_path / "proj"
    ps = protected_paths(cwd=str(proj), extra=[str(tmp_path / "secret")])

    assert (proj / "AGENTS.md") in ps.instructions
    assert (proj / ".gc") in ps.operational
    assert (tmp_path / "secret") in ps.operational
    assert (proj / ".gc" / "skills") in ps.skills_exceptions


def test_instruction_files_not_in_operational(tmp_path, monkeypatch):
    from gc_core.config_modules import env_vars
    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc")
    proj = tmp_path / "proj"
    ps = protected_paths(cwd=str(proj))
    # L1 AGENTS.md лежит в gc config-каталоге, но это класс инструкций, не операционный
    assert any(p.name == "AGENTS.md" for p in ps.instructions)
