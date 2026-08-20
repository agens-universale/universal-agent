"""HITL как порт вместо stdin (план async-переезда, Задача 21).

Ядро — библиотека: у встраивающего приложения свой способ спросить
пользователя, и stdin ему не принадлежит.
"""
from types import SimpleNamespace

from gc_core.tools.turn.ask_human import _read_human_response


def _ctx(**kw):
    return SimpleNamespace(config_dict={}, logger=None, **kw)


def test_sync_port_is_used():
    seen = {}

    def port(question: str) -> str:
        seen["question"] = question
        return "ответ приложения"

    assert _read_human_response(_ctx(ask_human=port), "как дела?") == "ответ приложения"
    assert seen["question"] == "как дела?"


def test_async_port_is_awaited():
    """Порт асинхронного приложения принимается наравне с обычным."""
    async def port(question: str) -> str:
        return f"async: {question}"

    assert _read_human_response(_ctx(ask_human=port), "?") == "async: ?"


def test_port_result_is_stripped():
    assert _read_human_response(_ctx(ask_human=lambda q: "  с полями  "), "?") == "с полями"


def test_port_returning_none_gives_empty():
    assert _read_human_response(_ctx(ask_human=lambda q: None), "?") == ""


def test_without_port_reads_stdin_as_before(monkeypatch):
    """Контракт CLI не меняется: построчно до пустой строки."""
    answers = iter(["первая", "вторая", ""])
    monkeypatch.setattr("builtins.input", lambda: next(answers))

    assert _read_human_response(_ctx(ask_human=None), "?") == "первая\nвторая"


def test_without_port_handles_eof(monkeypatch):
    def _eof():
        raise EOFError

    monkeypatch.setattr("builtins.input", _eof)
    assert _read_human_response(_ctx(ask_human=None), "?") == ""
