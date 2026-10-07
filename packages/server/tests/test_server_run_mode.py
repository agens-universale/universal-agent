"""Сервер объявляет свой режим запуска — от него зависит запись журнала правок.

Журнал правок пишется там, где его смотрят: в TUI — по умолчанию, на сервере —
только если включено `agent_edits.record.server`. Ядро узнаёт сервер по
служебному `run_mode`, который ставит сборщик конфига агента."""

from gc_server.rest_api import server_config


def test_agent_config_for_server_declares_the_server_run_mode(monkeypatch):
    monkeypatch.setattr(server_config, "load_config", lambda *a, **kw: {})

    cfg = server_config.build_agent_config_for_server({})

    assert cfg["run_mode"] == "server"
