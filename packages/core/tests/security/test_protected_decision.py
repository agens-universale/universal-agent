from gc_core.security.protected_paths import (
    protected_decision,
    protection_enabled,
)
from gc_testkit.settings import make_settings


def test_decisions(tmp_path, monkeypatch):
    from gc_core.config_modules import env_vars

    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc")
    proj = tmp_path / "proj"

    assert (
        protected_decision(str(proj / ".gc" / "config.yml"), cwd=str(proj))
        == "deny"
    )
    assert protected_decision(str(proj / "AGENTS.md"), cwd=str(proj)) == "ask"
    assert (
        protected_decision(
            str(proj / ".gc" / "skills" / "foo" / "SKILL.md"), cwd=str(proj)
        )
        == "allow"
    )
    assert (
        protected_decision(str(proj / "src" / "main.py"), cwd=str(proj))
        == "allow"
    )


def test_protection_enabled_default_true():
    assert protection_enabled(make_settings()) is True
    assert protection_enabled(None) is True


def test_protection_enabled_flag_does_not_disable_floor():
    """``security.protected_paths.enabled: false`` floor-защиту не снимает.

    Так в проде всегда и было (docs/configuration.md): до гарда флаг не
    доходил — плоский вид настроек секцию не проецировал. Выключить её мог
    только плоский тест-словарь, а его больше нет.
    """
    settings = make_settings({"security.protected_paths": {"enabled": False}})
    assert settings.settings.security.protected_paths.enabled is False
    assert protection_enabled(settings) is True


def test_any_git_directory_is_protected_like_instructions(
    tmp_path, monkeypatch
):
    """`.git` хранит конфиг и хуки, которые git исполняет сам (`core.fsmonitor`
    при `git status`, хуки при `git commit`): молча писать туда нельзя ни в
    корне проекта, ни во вложенном репозитории. Как у эталона: `.git` —
    защищённый путь, запись спрашивается."""
    from gc_core.config_modules import env_vars

    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc")
    proj = tmp_path / "proj"
    for path in (
        proj / ".git" / "config",
        proj / "sub" / ".git" / "hooks" / "pre-commit",
        proj / ".git",
    ):
        assert protected_decision(str(path), cwd=str(proj)) == "ask", path
    assert (
        protected_decision(str(proj / ".gitignore"), cwd=str(proj)) == "allow"
    )
    assert (
        protected_decision(str(proj / "my.git" / "x"), cwd=str(proj))
        == "allow"
    )


def test_operational_roots_stay_denied_even_with_a_git_component(
    tmp_path, monkeypatch
):
    """`.git` под служебным каталогом или под `extra` — по-прежнему запрет:
    класс vcs проверяется после служебных, а не вместо них."""
    from gc_core.config_modules import env_vars

    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc")
    proj = tmp_path / "proj"
    assert (
        protected_decision(
            str(proj / ".gc" / ".git" / "config"), cwd=str(proj)
        )
        == "deny"
    )
    assert (
        protected_decision(
            str(proj / ".git" / "config"),
            cwd=str(proj),
            extra=[str(proj / ".git")],
        )
        == "deny"
    )


def test_git_directory_name_is_matched_regardless_of_case(
    tmp_path, monkeypatch
):
    """На Маке файловая система по умолчанию не различает регистр: `.GIT` — тот
    же каталог `.git`."""
    from gc_core.config_modules import env_vars

    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc")
    proj = tmp_path / "proj"
    assert (
        protected_decision(
            str(proj / ".GIT" / "hooks" / "pre-commit"), cwd=str(proj)
        )
        == "ask"
    )
