"""app_version: версия пакета + источник установки из direct_url.json (PEP 610)."""
from gc_core import app_version

GIT_PINNED = '{"url": "https://github.com/pypa/sampleproject", "vcs_info": {"vcs": "git", "commit_id": "621e497", "requested_revision": "main"}}'
GIT_DEFAULT = '{"url": "https://gitverse.ru/gigacode-ai/universal-agent", "vcs_info": {"vcs": "git", "commit_id": "a1b2c3d"}}'
EDITABLE = '{"url": "file:///D:/dev/sber/background-agent", "dir_info": {"editable": true}}'


def test_install_source_git_pinned():
    src = app_version.install_source(_read=lambda: GIT_PINNED)
    assert src == app_version.InstallSource(
        url="https://github.com/pypa/sampleproject",
        ref="main", commit="621e497", editable=False)


def test_install_source_git_default_branch_has_no_ref():
    src = app_version.install_source(_read=lambda: GIT_DEFAULT)
    assert src.ref is None
    assert src.commit == "a1b2c3d"
    assert not src.editable


def test_install_source_editable():
    src = app_version.install_source(_read=lambda: EDITABLE)
    assert src.editable is True
    assert src.commit is None


def test_install_source_absent_and_broken():
    assert app_version.install_source(_read=lambda: None) is None
    assert app_version.install_source(_read=lambda: "не json{") is None


def test_agent_version_is_nonempty_string():
    # editable-установка этого репо даёт версию из pyproject; фолбэк — "dev"
    assert isinstance(app_version.agent_version(), str)
    assert app_version.agent_version()


# --- подсказки установки ------------------------------------------------------
#
# Переехали 27.08.2026 из packages/core/tests/test_missing_profile_guards.py: тот
# файл сторожил шимы `tui_entry`/`cli_entry`/`serve_entry`, а их в ядре больше нет —
# после разведения имён команд «команда есть, а пакета нет» в здоровой установке не
# случается. Сами подсказки остались: `install_hint` зовёт gc_tui, когда не находит
# textual. Дыра, ради которой проверки заведены, — расхождение текста подсказки с
# реальным подкаталогом манифеста: пользователя отправляли в несуществующий
# `packaging/`.


def test_install_hint_uses_real_subdirs():
    """Подсказка собирается из PROFILE_SUBDIRS, а не из строки рядом с текстом."""
    for dist, subdir in app_version.PROFILE_SUBDIRS.items():
        hint = app_version.install_hint(dist)
        assert f"#subdirectory={subdir}" in hint
        assert hint.startswith("uv tool install")
        assert "\n" not in hint


def test_no_stale_packaging_dir_in_hints():
    """Каталога packaging/ не существует — ни одна подсказка не должна его называть."""
    for dist in app_version.PROFILE_SUBDIRS:
        assert "packaging/" not in app_version.install_hint(dist)
