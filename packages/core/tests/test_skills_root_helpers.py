from pathlib import Path

import pytest

from gc_core.config_modules import env_vars
from gc_core.config_modules.env_vars import EnvVars
from gc_core.tools.skills.registry import _user_skills_root


@pytest.fixture
def isolated_env(monkeypatch, tmp_path):
    """Изолированный env_vars singleton с gc_home в tmp_path."""
    fake = EnvVars(gc_home=tmp_path)
    monkeypatch.setattr(env_vars, "env", fake)
    return fake


def test_user_root_is_under_gc_paths_data(isolated_env, tmp_path):
    """User-слой = gc_paths()["data"] / "skills"."""
    root = _user_skills_root()
    expected = tmp_path / "skills"  # gc_home → data === gc_home
    assert Path(root) == expected


def test_user_root_uses_gc_data_dir_override(monkeypatch, tmp_path):
    """GC_DATA_DIR override → user_root = override / "skills"."""
    custom_data = tmp_path / "custom_data"
    fake = EnvVars(gc_home=tmp_path / "home", gc_data_dir=custom_data)
    monkeypatch.setattr(env_vars, "env", fake)
    root = _user_skills_root()
    assert Path(root) == custom_data / "skills"


from gc_core.tools.skills.registry import _project_skills_root


def test_project_root_is_cwd_dot_gc_skills(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    root = _project_skills_root()
    assert Path(root) == tmp_path / ".gc" / "skills"


def test_project_root_recomputes_each_call(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    root1 = _project_skills_root()
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.chdir(other)
    root2 = _project_skills_root()
    assert Path(root1) != Path(root2)
    assert Path(root2) == other / ".gc" / "skills"
