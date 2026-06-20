"""Tests for long-memory backup/restore via gc_state."""
from pathlib import Path

from project_memory import storage


def test_write_mirrors_to_backup(tmp_path, monkeypatch):
    from config_modules import env_vars
    monkeypatch.setattr(env_vars, "env", env_vars.EnvVars(gc_home=tmp_path / "gc"))
    monkeypatch.chdir(tmp_path)
    main = tmp_path / ".gc" / "GIGACODE_memory.md"
    storage.write(main, "FACTS", backup=storage.backup_path(cwd=str(tmp_path)))
    assert storage.backup_path(cwd=str(tmp_path)).read_text(encoding="utf-8") == "FACTS"


def test_read_restores_from_backup_when_main_deleted(tmp_path, monkeypatch):
    from config_modules import env_vars
    monkeypatch.setattr(env_vars, "env", env_vars.EnvVars(gc_home=tmp_path / "gc"))
    monkeypatch.chdir(tmp_path)
    main = tmp_path / ".gc" / "GIGACODE_memory.md"
    bkp = storage.backup_path(cwd=str(tmp_path))
    storage.write(main, "FACTS", backup=bkp)
    main.unlink()  # симулируем rm -rf .gc
    text = storage.read(main, backup=bkp)
    assert text == "FACTS"
    assert main.exists()  # восстановлен


def test_append_mirrors_to_backup(tmp_path, monkeypatch):
    from config_modules import env_vars
    monkeypatch.setattr(env_vars, "env", env_vars.EnvVars(gc_home=tmp_path / "gc"))
    monkeypatch.chdir(tmp_path)
    main = tmp_path / ".gc" / "GIGACODE_memory.md"
    main.parent.mkdir(parents=True, exist_ok=True)
    bkp = storage.backup_path(cwd=str(tmp_path))
    storage.append(main, "FIRST", backup=bkp)
    storage.append(main, "SECOND", backup=bkp)
    assert bkp.read_text(encoding="utf-8") == "FIRST\n\nSECOND"


def test_no_backup_arg_leaves_existing_callers_unaffected(tmp_path):
    """Existing callers omitting backup= still work unchanged."""
    main = tmp_path / ".gc" / "project_memory.md"
    storage.write(main, "CONTENT")
    assert storage.read(main) == "CONTENT"
    storage.append(main, "MORE")
    assert storage.read(main) == "CONTENT\n\nMORE"


def test_backup_path_shape(tmp_path, monkeypatch):
    from config_modules import env_vars
    monkeypatch.setattr(env_vars, "env", env_vars.EnvVars(gc_home=tmp_path / "gc"))
    monkeypatch.chdir(tmp_path)
    bkp = storage.backup_path(cwd=str(tmp_path))
    assert bkp.name == "GIGACODE_memory_backup.md"
    # must be under gc_state/projects/<slug>/
    assert bkp.parent.parent.name == "projects"
