"""app_version: версия пакета + источник установки из direct_url.json (PEP 610)."""
import app_version

GIT_PINNED = '{"url": "https://github.com/pypa/sampleproject", "vcs_info": {"vcs": "git", "commit_id": "621e497", "requested_revision": "main"}}'
GIT_DEFAULT = '{"url": "https://gitverse.ru/gigacode-ai/background-agent", "vcs_info": {"vcs": "git", "commit_id": "a1b2c3d"}}'
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
