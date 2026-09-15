"""Вопрос `turn_ask_human` виден ДО того, как ждём ответ.

Асинхронный вход читал ответ первым, а печать вопроса, проверки, лог и событие
прогресса оставались в общем теле, которое получало ответ уже готовым. В CLI
человек видел вопрос только после того, как вслепую ответил; баннер субагента
в TUI (он рисуется по событию прогресса `ask_human`) не появлялся вовсе; пустой
и слишком длинный вопрос тоже сначала ждали ввода.
"""
import json

import pytest

from gc_core.tools.turn.ask_human import AskHuman
from gc_testkit.helpers.tool_ctx import local_ctx


@pytest.fixture
def events(monkeypatch):
    """Подменяем `emit_progress` на ТОМ объекте модуля, из которого его берёт тело.

    Тело инструмента импортирует его заново, из `sys.modules`, а conftest
    подменяет пакеты `gc_core.tools`: импорт на уровне этого файла мог дать
    другой объект. Первая редакция сторожа так и промахнулась — проверка
    «события не было» зеленела вхолостую.
    """
    import importlib

    seen: list = []
    mod = importlib.import_module("gc_core.tools.subagents.progress")
    monkeypatch.setattr(mod, "emit_progress", lambda ctx, state, kind, **kw: seen.append(kind))
    return seen


async def test_question_is_announced_before_the_answer_is_awaited(events):
    async def port(question):
        events.append("port")
        return "синий"

    ctx = local_ctx()
    ctx.ask_human = port
    out = json.loads(await AskHuman(ctx=ctx).acall(question="Любимый цвет?"))

    assert out["ok"] is True
    assert events.index("ask_human") < events.index("port") < events.index("ask_human_done"), events


@pytest.mark.parametrize("question", ["", "   ", "x" * 5000])
async def test_invalid_question_is_rejected_without_waiting(events, question):
    async def port(_question):
        raise AssertionError("ждали ввода ради вопроса, который всё равно отклонят")

    ctx = local_ctx()
    ctx.ask_human = port
    out = json.loads(await AskHuman(ctx=ctx).acall(question=question))

    assert out["ok"] is False
    assert out["error"]["type"] == "invalid_argument"
    assert "ask_human" not in events
