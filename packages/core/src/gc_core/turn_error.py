"""Причина ошибки хода: класс исключения и первая строка его сообщения.

Одно правило ядра: им пишется `outcome.error.message` записи итога хода
(`gc_core.persistence.turn_outcome`), его же зовёт фронтенд для живой
ошибки вне прогона. Длинное — с `…` (экран переносит причину до трёх
строк). Секрет-подобные значения (`sk-…`, Bearer, JWT — паттерны
редактора журнала ядра) маскируются: текст лежит в файле треда и виден
на экране."""

from __future__ import annotations

from gc_core.security.log_redaction import Redactor
from gc_core.utils.backend_errors import BackendBadResponseError

#: Предел причины в символах: три строки текста на обычной ширине
#: (~120 колонок минус отступ `└`) с запасом; дальше полезного мало.
TURN_ERROR_LIMIT = 400


def describe_turn_error(exc: BaseException) -> str:
    """`Класс: первая строка сообщения`, секреты под маской; сообщения
    нет — только класс."""
    if isinstance(exc, BackendBadResponseError):
        return _bad_response_text(exc)
    name = exc.__class__.__name__
    lines = str(exc).strip().splitlines()
    reason = f"{name}: {lines[0].strip()}" if lines else name
    reason = Redactor().redact(reason)
    if len(reason) <= TURN_ERROR_LIMIT:
        return reason
    return reason[: TURN_ERROR_LIMIT - 1].rstrip() + "…"


def _bad_response_text(exc: BackendBadResponseError) -> str:
    """Сбой ответа модели — с подсказкой по API, в истории и живом TUI."""
    if exc.api_mode == "responses":
        hint = "прокси оборвал или испортил поток ответа"
    else:
        hint = "прокси не поддерживает stream:false"
    return (
        "Сервер модели вернул пустой или не-JSON ответ "
        f"(HTTP {exc.status_code}, {exc.length} байт) — возможно, "
        f"{hint}; повторите запрос"
    )
