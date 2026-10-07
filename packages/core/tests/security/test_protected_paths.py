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


def test_workspace_inside_gc_state_is_not_operational(tmp_path, monkeypatch):
    """sandbox workdir=auto: рабочая область рана лежит в <state>/sandboxes/<id>.

    Сама область не служебная — иначе каждая команда в ней ловила бы
    protected_path_denied. Служебными остаются её `.gc`, остальной state и extra.
    """
    from gc_core.config_modules import env_vars
    from gc_core.security.protected_paths import classify_path

    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc")
    ws = tmp_path / "gc" / "sandboxes" / "r1"
    ps = protected_paths(cwd=str(ws), extra=[str(ws / "secret")])

    assert classify_path(ws / "in.txt", ps) == "none"
    assert classify_path(ws / "sub" / "x.py", ps) == "none"
    assert classify_path(ws / ".gc" / "config.yml", ps) == "operational"
    assert classify_path(ws / "secret" / "k", ps) == "operational"
    assert (
        classify_path(tmp_path / "gc" / "sandboxes" / "r2" / "a", ps)
        == "operational"
    )
    assert classify_path(tmp_path / "gc" / "config.yml", ps) == "operational"


def test_carve_out_only_under_state_sandboxes(tmp_path, monkeypatch):
    """Исключение — только для <state>/sandboxes/<run_id>; cwd=~/.config/gc не снимает защиту с config.yml."""
    from gc_core.config_modules import env_vars
    from gc_core.security.protected_paths import classify_path

    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc")
    ps = protected_paths(cwd=str(tmp_path / "gc"))
    assert classify_path(tmp_path / "gc" / "config.yml", ps) == "operational"
    assert ps.workspace_exception is None


def test_skills_runtime_copy_is_classified_as_skills(tmp_path, monkeypatch):
    # Копия скиллов в среде исполнения (<launch_root>/.gc/skills_runtime)
    # читается моделью как обычные скиллы, а не как служебный `.gc`.
    from gc_core.security.protected_paths import classify_path, protected_paths

    proj = tmp_path / "work"
    proj.mkdir()
    ps = protected_paths(cwd=str(proj))
    script = (
        proj / ".gc" / "skills_runtime" / "system" / "x" / "scripts" / "a.py"
    )
    assert classify_path(script, ps) == "skills"
    assert classify_path(proj / ".gc" / "config.yml", ps) == "operational"
