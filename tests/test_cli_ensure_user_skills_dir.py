import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# arg_parser.py парсит sys.argv на import — подменяем argv до импорта cli,
# чтобы required=True для -q не падал под pytest.
_ORIG_ARGV = sys.argv
sys.argv = ["pytest-cli-import", "-q", "dummy"]
try:
    from cli import _ensure_user_skills_dir
finally:
    sys.argv = _ORIG_ARGV

from config_modules import env_vars
from config_modules.env_vars import EnvVars


@pytest.fixture
def isolated_env(monkeypatch, tmp_path):
    """Изолированный env_vars singleton с gc_home в tmp_path → data → tmp_path."""
    fake = EnvVars(gc_home=tmp_path)
    monkeypatch.setattr(env_vars, "env", fake)
    return fake


def test_ensure_creates_user_skills_dir(isolated_env, tmp_path):
    """Создаёт <gc_data_dir>/skills/ — discoverability для user-слоя."""
    _ensure_user_skills_dir()
    assert (tmp_path / "skills").is_dir()


def test_ensure_idempotent(isolated_env, tmp_path):
    """Повторный вызов не падает."""
    _ensure_user_skills_dir()
    _ensure_user_skills_dir()
    assert (tmp_path / "skills").is_dir()


def test_ensure_silent_on_permission_error(isolated_env):
    """При PermissionError не raise."""
    with patch("os.makedirs", side_effect=PermissionError("no permission")):
        # Не должно бросить исключение
        _ensure_user_skills_dir()


def test_ensure_silent_on_os_error(isolated_env):
    """При OSError (например, диск переполнен) не raise."""
    with patch("os.makedirs", side_effect=OSError("disk full")):
        _ensure_user_skills_dir()
