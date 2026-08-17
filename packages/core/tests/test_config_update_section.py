def test_update_defaults():
    from gc_core.updater.check import auto_check_enabled, extra_args
    assert auto_check_enabled({}) is True                 # дефолт auto
    assert extra_args({}) == []


def test_appconfig_schema_accepts_update_section():
    """Схема AppConfig (extra='forbid') принимает ключ update — иначе config.yml
    с секцией update (demo/default) не проходит валидацию (регресс Task 5)."""
    from gc_core.config_modules.root import AppConfig
    AppConfig.model_validate({})                                   # без update — дефолт
    AppConfig.model_validate({"update": {}})                       # пустая секция
    AppConfig.model_validate({"update": {"check": "off",
                                         "extra_args": ["--system-certs"]}})


def test_load_config_carries_update_knobs(tmp_path):
    """load_config строит config_dict whitelist'ом — секция update должна попадать
    в него, иначе ручки off/extra_args в реальных прогонах не работают."""
    from gc_core.config import load_config
    from gc_core.updater.check import auto_check_enabled, extra_args
    cfg_file = tmp_path / "c.yml"
    cfg_file.write_text(
        "update:\n  check: off\n  extra_args: [\"--system-certs\"]\n",
        encoding="utf-8")
    cfg = load_config(str(cfg_file))
    assert "update" in cfg
    assert auto_check_enabled(cfg) is False
    assert extra_args(cfg) == ["--system-certs"]


def test_update_off_and_overrides():
    from gc_core.updater.check import auto_check_enabled, extra_args
    cfg = {"update": {"check": "off", "extra_args": ["--system-certs"]}}
    assert auto_check_enabled(cfg) is False
    assert extra_args(cfg) == ["--system-certs"]


def test_legacy_check_interval_days_is_dropped_with_notice(tmp_path):
    """Убранная ручка: старый config.yml с update.check_interval_days грузится
    (extra='forbid' не роняет старт) — ключ выбрасывается compat-слоем, факт уходит
    в стартовые уведомления."""
    from gc_core.config import load_config
    cfg_file = tmp_path / "c.yml"
    cfg_file.write_text("update:\n  check: auto\n  check_interval_days: 3\n", encoding="utf-8")
    cfg = load_config(str(cfg_file))
    assert "check_interval_days" not in cfg["update"]
    assert any("check_interval_days" in f for f in cfg["compat_notices"])
