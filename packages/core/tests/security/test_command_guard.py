from gc_core.security.command_guard import scan_command_for_protected


def test_rm_rf_gc_denied(tmp_path, monkeypatch):
    from gc_core.config_modules import env_vars

    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc")
    assert scan_command_for_protected("rm -rf .gc && git clone x .", cwd=str(tmp_path)) == "deny"


def test_skills_path_allowed(tmp_path, monkeypatch):
    from gc_core.config_modules import env_vars

    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc")
    assert scan_command_for_protected("python .gc/skills/foo/scripts/x.py", cwd=str(tmp_path)) == "allow"


def test_plain_command_allowed(tmp_path, monkeypatch):
    from gc_core.config_modules import env_vars

    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc")
    assert scan_command_for_protected("mkdir build && pytest", cwd=str(tmp_path)) == "allow"
