"""Причина ошибки хода — одно правило ядра (`gc_core.turn_error`).

Ядро пишет им `outcome.error.message` записи итога хода, TUI — живую
ошибку вне прогона: класс и первая строка, секреты под маской,
длинное — с `…`."""

from __future__ import annotations

from gc_core.security.log_redaction import MASK
from gc_core.turn_error import TURN_ERROR_LIMIT, describe_turn_error


class _ReadError(Exception):
    pass


def test_reason_is_class_and_first_line():
    exc = _ReadError("connection reset by peer\ntraceback tail")
    assert describe_turn_error(exc) == "_ReadError: connection reset by peer"


def test_reason_without_message_is_class():
    assert describe_turn_error(_ReadError()) == "_ReadError"


def test_long_reason_is_cut():
    reason = describe_turn_error(_ReadError("слово " * 200))
    assert len(reason) <= TURN_ERROR_LIMIT
    assert reason.endswith("…")


def test_reason_masks_secrets():
    key = "sk-" + "a1B2c3D4e5F6g7H8i9J0"
    exc = _ReadError(f"401 for https://api.example/v1?key={key}")
    reason = describe_turn_error(exc)
    assert key not in reason
    assert reason == f"_ReadError: 401 for https://api.example/v1?key={MASK}"
