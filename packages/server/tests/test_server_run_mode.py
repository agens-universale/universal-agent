"""Сервер объявляет свой режим запуска — от него зависит запись журнала правок.

Журнал правок пишется там, где его смотрят: в TUI — по умолчанию, на сервере —
только если включено `agent_edits.record.server`. Ядро узнаёт сервер по
`RuntimeEnv.run_mode`, который ставит сборщик настроек агента."""

from gc_server.rest_api import server_config
from gc_testkit.settings import make_settings


def test_agent_settings_for_server_declare_the_server_run_mode(monkeypatch):
    monkeypatch.setattr(
        server_config, "load_settings", lambda *a, **kw: make_settings()
    )

    loaded = server_config.build_agent_settings_for_server({})

    assert loaded.runtime.run_mode == "server"


def test_unknown_logging_key_in_server_config_is_dropped_not_fatal(
    monkeypatch, caplog
):
    """Лишний ключ в `logging:` config_server.yml (`logging_format: json`):
    на develop уходил в logging_params, сервер стартовал. Теперь — тоже
    старт, ключ отброшен с предупреждением (как в YAML агента)."""
    monkeypatch.setattr(
        server_config, "load_settings", lambda *a, **kw: make_settings()
    )
    with caplog.at_level("WARNING"):
        loaded = server_config.build_agent_settings_for_server(
            {"logging": {"logging_format": "json", "save_logs": False}}
        )
    assert loaded.settings.logging.save_logs is False
    assert "logging_format" in caplog.text
