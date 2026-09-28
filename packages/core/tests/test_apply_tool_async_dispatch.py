"""Диспетчер вызова тулов: async рядом с sync (план async-переезда, Задача 3).

Смысл — совместимость: у встраивающих SDK свои тулы с синхронным ``__call__``,
и переезд ядра на asyncio не имеет права требовать от них переписывания.
"""

import asyncio
import json

import pytest
from gc_core.tools import aapply_tool


class _SyncTool:
    def __init__(self, **kw):
        pass

    @classmethod
    def tool_description(cls):
        return {
            "type": "function",
            "name": "t_sync",
            "parameters": {
                "type": "object",
                "properties": {"x": {"type": "string"}},
                "required": ["x"],
                "additionalProperties": False,
            },
        }

    async def acall(self, x: str) -> str:
        # «Синхронный» здесь про ТЕЛО: оно ничего не ждёт и идёт инлайн.
        # Форма входа у тула одна — `acall`.
        return f"sync:{x}"


class _AsyncTool(_SyncTool):
    @classmethod
    def tool_description(cls):
        d = _SyncTool.tool_description()
        d["name"] = "t_async"
        return d

    async def acall(self, x: str) -> str:
        await asyncio.sleep(0)
        return f"async:{x}"


def _data(cls, name):
    return {
        name: {
            "tool_fabric": cls,
            "spec": cls.tool_description(),
            "description": "",
        }
    }


async def test_aapply_tool_awaits_async_tool():
    out = await aapply_tool(
        "t_async",
        {"x": "1"},
        _data(_AsyncTool, "t_async"),
        ctx=None,
        config_dict={},
    )
    assert out == "async:1"


async def test_aapply_tool_rejects_a_tool_without_acall():
    """Тул без `acall` — жёсткая ошибка проводки, а не тихий уход в поток.

    Раньше здесь стоял запасной путь: у тула нет `acall` — диспетчер уводил
    его в `asyncio.to_thread`. Ступень существовала «ради встраивающих», но
    зарегистрировать свой Python-класс-тул извне НЕЛЬЗЯ: `abuild_tools_data`
    собирает набор из `config_dict`, параметра у `create_run_context` нет,
    механизма плагинов нет. То есть ступень сторожила несуществующую
    возможность, а платой были две формы у каждого тула и слот общего пула.

    Точка расширения для тулов — MCP, и она асинхронна насквозь.
    """

    class _NoAcall(_SyncTool):
        """Вся обвязка тула на месте, а `acall` нет — ошибка проводки."""

        acall = None

    with pytest.raises(TypeError, match="has no acall"):
        await aapply_tool(
            "t_sync",
            {"x": "1"},
            _data(_NoAcall, "t_sync"),
            ctx=None,
            config_dict={},
        )


async def test_async_tool_exception_becomes_agent_visible_text():
    """Падение тула — текст агенту, а не всплывшее исключение в цикле.

    Тот же контракт, что у синхронного пути: цикл продолжается, агент видит,
    что именно сломалось. Форма — слим-конверт
    (docs_md/actual/2026-08-14-apply-tool-internal-error-CONTRACT.ru.md), а не
    прежние `str(exc)` + traceback: стек остаётся в логах и в контекст модели
    не уезжает. Проверка «тот же контракт» — в
    `test_internal_tool_error.py::test_async_entry_gives_the_same_slim_envelope`.
    """

    class _Boom(_SyncTool):
        async def acall(self, x: str) -> str:
            raise ValueError("тул сломался")

    out = await aapply_tool(
        "t_async",
        {"x": "1"},
        _data(_Boom, "t_async"),
        ctx=None,
        config_dict={},
    )
    assert "тул сломался" in out
    payload = json.loads(out)
    assert payload["ok"] is False
    assert payload["exception_type"] == "ValueError"
    assert "Traceback (most recent call last)" not in out


async def test_sync_tool_exception_via_async_dispatch():
    """Исключение синхронного тула, поднятое в потоке, тоже становится текстом."""

    class _Boom(_SyncTool):
        async def acall(self, x: str) -> str:
            raise RuntimeError("и этот сломался")

    out = await aapply_tool(
        "t_sync", {"x": "1"}, _data(_Boom, "t_sync"), ctx=None, config_dict={}
    )
    assert "и этот сломался" in out


async def test_unknown_tool_returns_an_actionable_envelope():
    """Ранний выход: неизвестный тул называет себя, а не падает.

    Раньше тест сверял два пути между собой. Путь теперь один — сверять не с
    чем, поэтому проверяется сам конверт.
    """
    data = _data(_SyncTool, "t_sync")
    out = await aapply_tool("нет_такого", {}, data, ctx=None, config_dict={})
    assert "нет_такого" in out


async def test_non_string_result_is_serialized_the_same_way():
    """dict/list от тула сериализуются в JSON, а не в repr."""

    class _Dict(_SyncTool):
        async def acall(self, x: str):
            return {"ok": True, "x": x}

    data = _data(_Dict, "t_sync")
    out = await aapply_tool(
        "t_sync", {"x": "1"}, data, ctx=None, config_dict={}
    )
    assert out == '{"ok": true, "x": "1"}'
