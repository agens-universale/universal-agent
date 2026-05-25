from pathlib import Path

import pytest

from config_modules import env_vars
from config_modules.env_vars import EnvVars, project_slug
from swarm.config import build_swarm_config


@pytest.fixture
def isolated_env(monkeypatch, tmp_path):
    monkeypatch.setattr("sys.platform", "linux")
    fake = EnvVars(gc_home=tmp_path)
    monkeypatch.setattr(env_vars, "env", fake)
    monkeypatch.chdir(tmp_path)
    return fake


def test_swarm_threads_default_under_state_projects_slug(isolated_env, tmp_path):
    cfg = build_swarm_config({})
    threads_dir = Path(cfg["threads_dir"])
    assert threads_dir == tmp_path / "projects" / project_slug() / "swarm" / "threads"


def test_swarm_sandbox_default_under_state_projects_slug(isolated_env, tmp_path):
    cfg = build_swarm_config({})
    sandbox_dir = Path(cfg["sandbox_root_dir"])
    assert sandbox_dir == tmp_path / "projects" / project_slug() / "swarm" / "sandboxes"


def test_swarm_explicit_threads_dir_used_as_is(isolated_env, tmp_path):
    explicit = tmp_path / "custom_threads"
    cfg = build_swarm_config({"swarm": {"threads_dir": str(explicit)}})
    assert Path(cfg["threads_dir"]) == explicit


def test_swarm_wrapper_and_agentmd_paths_unchanged(isolated_env, tmp_path):
    """Пакетные ресурсы остаются с PROJECT_ROOT-дефолтами."""
    cfg = build_swarm_config({})
    assert tmp_path not in Path(cfg["wrapper_path"]).parents
    assert tmp_path not in Path(cfg["agentmd_path"]).parents
    assert tmp_path not in Path(cfg["skills_dir"]).parents
