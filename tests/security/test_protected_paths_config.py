from config_modules.security import SecurityConfig


def test_defaults_enabled_true():
    cfg = SecurityConfig()
    assert cfg.protected_paths.enabled is True
    assert cfg.protected_paths.extra == []


def test_parse_disabled():
    cfg = SecurityConfig(protected_paths={"enabled": False, "extra": ["/x"]})
    assert cfg.protected_paths.enabled is False
    assert cfg.protected_paths.extra == ["/x"]
