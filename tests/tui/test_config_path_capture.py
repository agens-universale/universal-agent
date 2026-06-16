def test_resolve_config_path_explicit():
    from config import resolve_config_path
    assert resolve_config_path("D:/x/config.yml") == "D:/x/config.yml"


def test_resolve_config_path_default_is_str():
    from config import resolve_config_path
    p = resolve_config_path(None)
    assert isinstance(p, str) and p.endswith(".yml")
