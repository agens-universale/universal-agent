from gc_core.security.protected_paths import protected_decision, protection_enabled


def test_decisions(tmp_path, monkeypatch):
    from gc_core.config_modules import env_vars

    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc")
    proj = tmp_path / "proj"

    assert protected_decision(str(proj / ".gc" / "config.yml"), cwd=str(proj)) == "deny"
    assert protected_decision(str(proj / "AGENTS.md"), cwd=str(proj)) == "ask"
    assert protected_decision(str(proj / ".gc" / "skills" / "foo" / "SKILL.md"), cwd=str(proj)) == "allow"
    assert protected_decision(str(proj / "src" / "main.py"), cwd=str(proj)) == "allow"


def test_protection_enabled_default_true():
    assert protection_enabled({}) is True
    assert protection_enabled({"protected_paths": {"enabled": False}}) is False
