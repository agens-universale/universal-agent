"""`server.log_level` доезжает до uvicorn — но только тем, что uvicorn понимает.

До этой ветки поле читалось и НЕ передавалось: `uvicorn.run` звался без
`log_level`, и любое значение в конфиге было безвредным. Ветка стала его
передавать, а uvicorn берёт уровень прямым `LOG_LEVELS[level]` и падает
`KeyError` на написаниях, которые `logging` считает законными синонимами
(`warn`, `fatal`). Установка, годами жившая с `log_level: warn`, после
обновления не поднялась бы вовсе — молча, до первого взгляда в systemd.
"""

import logging

import pytest
from gc_server import server as server_mod


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("info", "info"),
        ("DEBUG", "debug"),
        ("  Warning  ", "warning"),
        # Синонимы logging, которых нет у uvicorn.
        ("warn", "warning"),
        ("fatal", "critical"),
        # Поле не задано вовсе.
        (None, "info"),
    ],
)
def test_known_levels_are_normalized(raw, expected):
    assert server_mod._normalize_log_level(raw) == expected


def test_unknown_level_falls_back_to_info_loudly(caplog):
    """Мусор в поле не роняет старт: поднятый сервер лучше упавшего, но с криком."""
    with caplog.at_level(logging.WARNING, logger=server_mod.__name__):
        assert server_mod._normalize_log_level("verbose") == "info"
    assert "log_level" in caplog.text


def test_every_normalized_level_is_accepted_by_uvicorn():
    """Список сверяется с настоящим uvicorn, а не с нашим представлением о нём."""
    from uvicorn.config import LOG_LEVELS

    assert set(LOG_LEVELS) >= server_mod._UVICORN_LOG_LEVELS, set(
        server_mod._UVICORN_LOG_LEVELS
    ) - set(LOG_LEVELS)
    # И обратно: молча растерять уровни, которые uvicorn умеет, тоже не хочется.
    assert set(LOG_LEVELS) <= server_mod._UVICORN_LOG_LEVELS, set(
        LOG_LEVELS
    ) - set(server_mod._UVICORN_LOG_LEVELS)
