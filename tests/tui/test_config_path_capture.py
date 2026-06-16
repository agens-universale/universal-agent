def test_resolve_config_path_explicit():
    from config import resolve_config_path
    assert resolve_config_path("D:/x/config.yml") == "D:/x/config.yml"


def test_resolve_config_path_default_is_str(monkeypatch, tmp_path):
    from config import resolve_config_path
    from config_modules import env_vars

    # Изолируем: нет ~/.gc/config.yml → resolve_config_path(None) обязан вернуть bundled default.
    monkeypatch.setattr(env_vars, "gc_paths", lambda: {
        "config": tmp_path / "gc",
        "state": tmp_path / "state",
        "data": tmp_path / "data",
        "cache": tmp_path / "cache",
    })
    p = resolve_config_path(None)
    assert isinstance(p, str) and p.endswith("config.default.yml")
