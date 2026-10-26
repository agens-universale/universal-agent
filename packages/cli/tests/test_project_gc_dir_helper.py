import sys
from pathlib import Path

from gc_core.config_modules.env_vars import project_gc_dir


def test_project_gc_dir_default_uses_cwd(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    assert project_gc_dir() == tmp_path / ".gc"


def test_project_gc_dir_explicit_cwd_argument(tmp_path):
    assert project_gc_dir(str(tmp_path)) == tmp_path / ".gc"


def test_project_gc_dir_returns_path_object():
    assert isinstance(project_gc_dir("/tmp"), Path)


def test_project_gc_dir_lazy_cwd_resolution(monkeypatch, tmp_path):
    """Default form must call os.getcwd() at invocation time, not at import time."""
    sub = tmp_path / "nested"
    sub.mkdir()
    monkeypatch.chdir(sub)
    assert project_gc_dir() == sub / ".gc"
    monkeypatch.chdir(tmp_path)
    assert project_gc_dir() == tmp_path / ".gc"


def test_cli_ensure_project_gc_dir_uses_helper(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    # `gc_cli.cli` разбирает sys.argv при импорте (`-q` обязателен): тот же
    # приём, что в test_cli_skill_flag.py, — иначе файл падает при запуске
    # отдельно, на аргументах самого pytest.
    monkeypatch.setattr(sys, "argv", ["gc-cli", "-q", "x"])
    from gc_cli.cli import _ensure_project_gc_dir

    _ensure_project_gc_dir()
    assert (tmp_path / ".gc").is_dir()


def test_local_project_overlay_uses_helper(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    from gc_core.config_modules.env_vars import project_local_config_path

    # No file → returns None
    assert project_local_config_path() is None
    (tmp_path / ".gc").mkdir()
    (tmp_path / ".gc" / "config.yml").write_text(
        "foo: bar\n", encoding="utf-8"
    )
    overlay = project_local_config_path()
    assert overlay == str((tmp_path / ".gc" / "config.yml").resolve())


def test_project_skills_dir_uses_helper(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    from gc_core.tools.skills.registry import _project_skills_root

    assert _project_skills_root() == str(tmp_path / ".gc" / "skills")
