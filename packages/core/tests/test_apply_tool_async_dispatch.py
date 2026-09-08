"""Диспетчер вызова тулов: async рядом с sync (план async-переезда, Задача 3).

Смысл — совместимость: у встраивающих SDK свои тулы с синхронным ``__call__``,
и переезд ядра на asyncio не имеет права требовать от них переписывания.
"""
import asyncio
import json
import threading

from gc_core.tools import aapply_tool, apply_tool


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

    def __call__(self, x: str) -> str:
        return f"sync:{x}"


class _AsyncTool(_SyncTool):
    @classmethod
    def tool_description(cls):
        d = _SyncTool.tool_description()
        d["name"] = "t_async"
        return d

    async def __call__(self, x: str) -> str:
        await asyncio.sleep(0)
        return f"async:{x}"


def _data(cls, name):
    return {name: {"tool_fabric": cls, "spec": cls.tool_description(), "description": ""}}


async def test_aapply_tool_awaits_async_tool():
    out = await aapply_tool("t_async", {"x": "1"}, _data(_AsyncTool, "t_async"),
                            ctx=None, config_dict={})
    assert out == "async:1"


async def test_aapply_tool_offloads_sync_tool_to_thread():
    """Синхронный сторонний тул не имеет права заблокировать loop."""
    seen = {}

    class _Probe(_SyncTool):
        def __call__(self, x: str) -> str:
            seen["thread"] = threading.get_ident()
            return "ok"

    out = await aapply_tool("t_sync", {"x": "1"}, _data(_Probe, "t_sync"),
                            ctx=None, config_dict={})
    assert out == "ok"
    assert seen["thread"] != threading.get_ident()


def test_apply_tool_sync_path_unchanged():
    """Синхронный путь остался прямым: ни портала, ни loop на нём нет."""
    out = apply_tool("t_sync", {"x": "1"}, _data(_SyncTool, "t_sync"),
                     ctx=None, config_dict={})
    assert out == "sync:1"


def test_apply_tool_rejects_async_tool_with_actionable_message():
    """Синхронный путь не умеет ждать корутину — говорит об этом прямо.

    Молчаливое ``str(coroutine)`` ушло бы агенту как результат тула, и понять,
    почему инструмент «вернул» ``<coroutine object ...>``, было бы неоткуда.
    """
    out = apply_tool("t_async", {"x": "1"}, _data(_AsyncTool, "t_async"),
                     ctx=None, config_dict={})
    assert "aapply_tool" in out


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
        async def __call__(self, x: str) -> str:
            raise ValueError("тул сломался")

    out = await aapply_tool("t_async", {"x": "1"}, _data(_Boom, "t_async"),
                            ctx=None, config_dict={})
    assert "тул сломался" in out
    payload = json.loads(out)
    assert payload["ok"] is False
    assert payload["exception_type"] == "ValueError"
    assert "Traceback (most recent call last)" not in out


async def test_sync_tool_exception_via_async_dispatch():
    """Исключение синхронного тула, поднятое в потоке, тоже становится текстом."""
    class _Boom(_SyncTool):
        def __call__(self, x: str) -> str:
            raise RuntimeError("и этот сломался")

    out = await aapply_tool("t_sync", {"x": "1"}, _data(_Boom, "t_sync"),
                            ctx=None, config_dict={})
    assert "и этот сломался" in out


async def test_unknown_tool_returns_same_text_on_both_paths():
    """Ранние выходы (неизвестный тул) одинаковы у обоих путей: код общий."""
    data = _data(_SyncTool, "t_sync")
    sync_out = apply_tool("нет_такого", {}, data, ctx=None, config_dict={})
    async_out = await aapply_tool("нет_такого", {}, data, ctx=None, config_dict={})
    assert sync_out == async_out
    assert "нет_такого" in sync_out


async def test_non_string_result_is_serialized_the_same_way():
    """dict/list от тула сериализуются одинаково на обоих путях."""
    class _Dict(_SyncTool):
        def __call__(self, x: str):
            return {"ok": True, "x": x}

    data = _data(_Dict, "t_sync")
    sync_out = apply_tool("t_sync", {"x": "1"}, data, ctx=None, config_dict={})
    async_out = await aapply_tool("t_sync", {"x": "1"}, data, ctx=None, config_dict={})
    assert sync_out == async_out == '{"ok": true, "x": "1"}'
