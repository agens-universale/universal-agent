"""Вебхук объявляет режим запуска и канал — от них зависят журнал правок,
подтверждение и судья действий.

Журнал правок на сервере пишется, только если включено
`agent_edits.record.server`: ядро узнаёт сервер по `RuntimeEnv.run_mode`.
"""

from gc_server.config import ServerConfig
from gc_server.webhook.surface import webhook_settings
from server_harness import runtime


def test_webhook_settings_declare_the_server_run_mode():
    assert webhook_settings(runtime()).runtime.run_mode == "server"


def test_unknown_logging_key_in_server_config_is_dropped_not_fatal(caplog):
    """Лишний ключ в `server.logging` (`logging_format: json`) — старт, ключ
    отброшен с предупреждением (как в YAML агента)."""
    rt = runtime()
    rt.config = ServerConfig.model_validate(
        {"server": {"logging": {"logging_format": "json", "save_logs": False}}}
    )
    with caplog.at_level("WARNING"):
        loaded = webhook_settings(rt)
    assert loaded.settings.logging.save_logs is False
    assert "logging_format" in caplog.text


def test_webhook_settings_declare_the_webhook_channel():
    """За вебхуком нет человека: подтверждение спросить некого, а слова
    вебхука для судьи действий — запрос внешней системы."""
    loaded = webhook_settings(runtime())
    assert loaded.runtime.human_channel is False
    assert loaded.runtime.external_user_words is True
