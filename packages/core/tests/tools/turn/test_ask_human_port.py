"""HITL как порт вместо stdin (план async-переезда, Задача 21).

Ядро — библиотека: у встраивающего приложения свой способ спросить
пользователя, и stdin ему не принадлежит.
"""

from types import SimpleNamespace

from gc_core.tools.turn.ask_human import _aread_human_response


def _ctx(**kw):
    return SimpleNamespace(config_dict={}, logger=None, **kw)


async def test_sync_port_is_used():
    seen = {}

    def port(question: str) -> str:
        seen["question"] = question
        return "ответ приложения"

    assert (
        await _aread_human_response(_ctx(ask_human=port), "как дела?")
        == "ответ приложения"
    )
    assert seen["question"] == "как дела?"


async def test_async_port_is_awaited():
    """Порт асинхронного приложения принимается наравне с обычным."""

    async def port(question: str) -> str:
        return f"async: {question}"

    assert await _aread_human_response(_ctx(ask_human=port), "?") == "async: ?"


async def test_port_result_is_stripped():
    assert (
        await _aread_human_response(
            _ctx(ask_human=lambda q: "  с полями  "), "?"
        )
        == "с полями"
    )


async def test_port_returning_none_gives_empty():
    assert (
        await _aread_human_response(_ctx(ask_human=lambda q: None), "?") == ""
    )


async def test_without_port_reads_stdin_as_before(monkeypatch):
    """Контракт CLI не меняется: построчно до пустой строки."""
    answers = iter(["первая", "вторая", ""])
    monkeypatch.setattr("builtins.input", lambda: next(answers))

    assert (
        await _aread_human_response(_ctx(ask_human=None), "?")
        == "первая\nвторая"
    )


async def test_without_port_handles_eof(monkeypatch):
    def _eof():
        raise EOFError

    monkeypatch.setattr("builtins.input", _eof)
    assert await _aread_human_response(_ctx(ask_human=None), "?") == ""
