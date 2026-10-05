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
