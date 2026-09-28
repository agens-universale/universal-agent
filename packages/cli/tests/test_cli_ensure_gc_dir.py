import sys
from unittest.mock import patch

# arg_parser.py парсит sys.argv на import — подменяем argv до импорта cli,
# чтобы required=True для -q не падал под pytest.
_ORIG_ARGV = sys.argv
sys.argv = ["pytest-cli-import", "-q", "dummy"]
try:
    from gc_cli.cli import _ensure_project_gc_dir
finally:
    sys.argv = _ORIG_ARGV


def test_ensure_creates_gc_dir(monkeypatch, tmp_path):
    """В пустой cwd создаёт <cwd>/.gc/."""
    monkeypatch.chdir(tmp_path)
    _ensure_project_gc_dir()
    assert (tmp_path / ".gc").is_dir()


def test_ensure_idempotent(monkeypatch, tmp_path):
    """Повторный вызов не падает."""
    monkeypatch.chdir(tmp_path)
    _ensure_project_gc_dir()
    _ensure_project_gc_dir()
    assert (tmp_path / ".gc").is_dir()


def test_ensure_does_not_create_skills_subdir(monkeypatch, tmp_path):
    """Создаётся только корневая .gc/, не .gc/skills/."""
    monkeypatch.chdir(tmp_path)
    _ensure_project_gc_dir()
    assert not (tmp_path / ".gc" / "skills").exists()


def test_ensure_silent_on_permission_error(monkeypatch, tmp_path):
    """При PermissionError не raise."""
    monkeypatch.chdir(tmp_path)
    with patch("os.makedirs", side_effect=PermissionError("no permission")):
        # Не должно бросить исключение
        _ensure_project_gc_dir()


def test_ensure_silent_on_os_error(monkeypatch, tmp_path):
    """При OSError (например, диск переполнен) не raise."""
    monkeypatch.chdir(tmp_path)
    with patch("os.makedirs", side_effect=OSError("disk full")):
        _ensure_project_gc_dir()
