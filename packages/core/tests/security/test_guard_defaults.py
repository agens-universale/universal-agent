from gc_core.config import apply_cli_overrides, flat_config
from gc_core.config_modules.security import SecurityConfig
from gc_core.settings import settings_from_flat


def test_pydantic_defaults_on_ask():
    s = SecurityConfig()
    assert s.write_guard.enabled is True and s.write_guard.mode == "ask"
    assert s.read_guard.enabled is True and s.read_guard.mode == "ask"
    assert s.url_policy.enabled is True and s.url_policy.mode == "ask"
    assert (
        s.command_moderation.enabled is True
        and s.command_moderation.mode == "ask"
    )
    assert s.protected_paths.enabled is True


def test_cli_overrides_no_args_yield_on_ask():
    # CLI без флагов гвардов не ослабляет дефолты: всё включено, режим ask.
    cfg = flat_config(apply_cli_overrides(settings_from_flat({})))
    assert cfg["command_moderation"]["enabled"] is True
    assert cfg["command_moderation"]["mode"] == "ask"
    assert cfg["write_guard"]["enabled"] is True
    assert cfg["read_guard"]["enabled"] is True
    assert cfg["read_guard"]["mode"] == "ask"
