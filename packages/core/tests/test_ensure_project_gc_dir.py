"""Общий стартовый хелпер env_vars.ensure_project_gc_dir (зовут gc-cli и gc-ua)."""
from unittest.mock import patch

from gc_core.config_modules import env_vars


def test_creates_gc_dir(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    env_vars.ensure_project_gc_dir()
    assert (tmp_path / ".gc").is_dir()


def test_idempotent(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    env_vars.ensure_project_gc_dir()
    env_vars.ensure_project_gc_dir()
    assert (tmp_path / ".gc").is_dir()


def test_does_not_create_skills_subdir(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    env_vars.ensure_project_gc_dir()
    assert not (tmp_path / ".gc" / "skills").exists()


def test_silent_on_permission_error(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    with patch("os.makedirs", side_effect=PermissionError("no permission")):
        env_vars.ensure_project_gc_dir()  # не должно бросить


def test_silent_on_os_error(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    with patch("os.makedirs", side_effect=OSError("disk full")):
        env_vars.ensure_project_gc_dir()
