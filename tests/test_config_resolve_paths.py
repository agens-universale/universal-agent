import sys
from pathlib import Path

import pytest

from config_modules import env_vars
from config_modules.env_vars import EnvVars, project_slug
from config_modules.root import AppConfig, resolve_paths


@pytest.fixture
def isolated_env(monkeypatch, tmp_path):
    """Изолированный env_vars singleton с gc_home в tmp_path + linux platform."""
    monkeypatch.setattr("sys.platform", "linux")
    fake = EnvVars(gc_home=tmp_path)
    monkeypatch.setattr(env_vars, "env", fake)
    monkeypatch.chdir(tmp_path)
    return fake


def test_default_config_path_is_normalized():
    """Дефолтный bundled-конфиг без '..' — путь показывается пользователю в /config."""
    from config import _default_config_path
    p = _default_config_path()
    assert ".." not in p
    assert p.endswith(".yml")


def test_resolve_paths_threads_default_under_state_projects_slug(
    isolated_env, tmp_path
):
    cfg = AppConfig()
    resolve_paths(cfg, str(tmp_path))
    threads_path = Path(cfg.paths.threads_data_path)
    # GC_HOME=tmp_path → state == tmp_path → projects/<slug>/threads
    assert threads_path == tmp_path / "projects" / project_slug() / "threads"


def test_resolve_paths_logs_default_under_state_projects_slug(isolated_env, tmp_path):
    cfg = AppConfig()
    resolve_paths(cfg, str(tmp_path))
    logs_path = Path(cfg.paths.logs_dump_dir)
    assert logs_path == tmp_path / "projects" / project_slug() / "logs"


def test_explicit_threads_absolute_used_as_is(isolated_env, tmp_path):
    explicit = tmp_path / "explicit_threads"
    cfg = AppConfig(paths={"threads_data_path": str(explicit)})
    resolve_paths(cfg, str(tmp_path))
    assert Path(cfg.paths.threads_data_path) == explicit


def test_explicit_logs_absolute_used_as_is(isolated_env, tmp_path):
    explicit = tmp_path / "explicit_logs"
    cfg = AppConfig(paths={"logs_dump_dir": str(explicit)})
    resolve_paths(cfg, str(tmp_path))
    assert Path(cfg.paths.logs_dump_dir) == explicit


def test_explicit_threads_relative_resolves_from_config_dir(isolated_env, tmp_path):
    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    cfg = AppConfig(paths={"threads_data_path": "rel/threads"})
    resolve_paths(cfg, str(config_dir))
    assert Path(cfg.paths.threads_data_path) == config_dir / "rel" / "threads"


def test_explicit_empty_string_falls_back_to_default(isolated_env, tmp_path):
    cfg = AppConfig(paths={"threads_data_path": ""})
    resolve_paths(cfg, str(tmp_path))
    threads_path = Path(cfg.paths.threads_data_path)
    assert threads_path == tmp_path / "projects" / project_slug() / "threads"
