"""Политика загрузки конфига к полноте файла.

1. Лишний ключ — уведомление (файл, ключ, подсказка), а не отказ загрузки.
2. Нет ключа с умолчанием — действует умолчание.
3. Нет обязательного поля записи без умолчания (сервер MCP без transport и т.п.)
   — отказ с понятным текстом, как и раньше (решение: не ослаблять).
"""

import pytest
import yaml
from gc_core.config import load_settings
from gc_core.config_modules import env_vars
from gc_core.config_modules.root import AppConfig
from gc_core.config_modules.unknown_keys import drop_unknown_keys


@pytest.fixture
def stack(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        env_vars, "managed_config_path", lambda: tmp_path / "managed.yml"
    )
    monkeypatch.setattr(env_vars, "project_local_config_path", lambda: None)

    def write(name: str, body: dict):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(body), encoding="utf-8")
        return path

    return write


def test_unknown_keys_are_dropped_with_a_notice(stack):
    path = stack(
        "config.yml",
        {
            "agent": {"max_stps": 3, "max_steps": 9},
            "backend": {"strem": True},
            "brand_new_section": {"a": 1},
        },
    )
    loaded = load_settings(str(path))
    assert loaded.settings.agent.max_steps == 9
    notices = "\n".join(loaded.runtime.notices)
    assert "«agent.max_stps» проигнорирован — возможно, «max_steps»" in notices
    assert "«backend.strem» проигнорирован — возможно, «stream»" in notices
    assert "«brand_new_section» проигнорирован" in notices
    assert str(path) in notices  # человек видит, в каком файле чинить


def test_model_level_backend_field_is_explained(stack):
    path = stack(
        "config.yml",
        {
            "models": {
                "gpt-4.1": {
                    "model": "g",
                    "backend": {"stream": False, "use_retry": True},
                }
            }
        },
    )
    loaded = load_settings(str(path))
    backend = loaded.settings.models["gpt-4.1"].backend
    assert backend.use_retry is True
    assert not hasattr(backend, "stream")
    # Устаревший ключ снимает compat (REMOVED с `*`), а не общий фильтр
    # лишних ключей; алиас с точкой — не помеха.
    notices = "\n".join(loaded.runtime.notices)
    assert "models.gpt-4.1.backend.stream устарел и игнорируется" in notices
    assert "задайте в общей секции backend" in notices


def test_unknown_key_in_profile_does_not_break_loading(stack):
    path = stack("config.yml", {"profile": "work"})
    stack("profiles/work.yml", {"agent": {"max_steps": 4, "typo_key": 1}})
    loaded = load_settings(str(path))
    assert loaded.settings.agent.max_steps == 4
    assert any("agent.typo_key" in n for n in loaded.runtime.notices)


def test_missing_keys_take_defaults(stack):
    path = stack("config.yml", {})
    loaded = load_settings(str(path))
    assert loaded.settings.agent.max_steps == AppConfig().agent.max_steps
    partial = stack("partial.yml", {"agent": {"model": "m"}})
    assert load_settings(str(partial)).settings.agent.max_steps == (
        AppConfig().agent.max_steps
    )


def test_missing_required_field_still_fails_clearly(stack):
    path = stack(
        "config.yml", {"mcp": {"servers": {"x": {"command": "echo"}}}}
    )
    with pytest.raises(ValueError, match=r"mcp\.servers\.x\.transport"):
        load_settings(str(path))


def test_valid_templates_have_no_unknown_keys():
    from gc_testkit.paths import REPO

    for rel in ("demo_config.yml", "packages/core/assets/config.default.yml"):
        raw = yaml.safe_load((REPO / rel).read_text(encoding="utf-8"))
        _, dropped = drop_unknown_keys(raw, AppConfig)
        assert dropped == [], (rel, [d.dotted for d in dropped])


def test_unknown_security_key_notice_warns_that_protection_is_off():
    from gc_core.config_modules.unknown_keys import (
        UnknownKey,
        unknown_key_notice,
    )

    notice = unknown_key_notice(
        UnknownKey(("security", "read_guard", "denied_pathz"), "denied_paths")
    )
    assert notice.startswith("ВНИМАНИЕ: настройка защиты не применена")
    assert "security.read_guard.denied_pathz" in notice
