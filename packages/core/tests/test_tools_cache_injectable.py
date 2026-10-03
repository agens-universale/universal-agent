"""Гейт: ``ToolsCache`` прокидывается вызывающим и реально экономит дискавери.

Дискавери — 99.7% времени сборки контекста: замер на реальном конфиге дал
866–1405 мс при 2.4–6.9 мс на всё остальное. Кэш в ``ToolsCache`` был, но не
работал никогда: данные лежат в полях ЭКЗЕМПЛЯРА, а экземпляр создавался заново
на каждый ``abuild_tools_data``. Обещанный ``tools-cache-ttl: 3600`` не наступал.

Больнее всего это било по двум местам:

* сервер собирает контекст НА КАЖДЫЙ POST, причём до дедупликации прогонов;
* субагент — НА КАЖДЫЙ СПАВН, и делает это прямо на event loop (993 мс заморозки).

Кэш сделан ПРОКИДЫВАЕМЫМ, а не процессным синглтоном. Ровно эту болезнь лечили
в ``109548e1``: ``PersistentStdioRegistry`` был общим на процесс, и первый
доигравший агент закрывал сессии остальных. У глобального кэша те же беды —
скрытое время жизни, протечка между арендаторами сервера с разными наборами
MCP-серверов, переживание между тестами.
"""

from typing import Any, Dict

from gc_core.tools.connect_mcp.registry import ToolsCache
from gc_testkit.settings import make_settings


def _cfg(servers: Dict[str, Any]) -> Dict[str, Any]:
    return {"mcp-servers": servers, "mcp-meta": {"tools-cache-ttl": 3600}}


def test_rebind_keeps_the_payload():
    """``rebind`` переключает config-словарь, но не выбрасывает кэш.

    Каждая сборка контекста пересобирает ``cfg`` заново, и без ``rebind`` чужой
    кэш считал бы TTL по словарю, с которым его когда-то создали. Данные при
    этом сбрасывать нельзя: попадание определяется ключом от СОДЕРЖИМОГО
    ``mcp-servers``, а не тождеством словаря.
    """
    import time

    servers = {"srv": {"transport": "stdio", "command": "x"}}
    cache = ToolsCache(_cfg(servers))
    cache._data = {"mcp_srv_tool": {"tool": {}, "metadata": {}}}
    cache._cache_key = cache._make_cache_key(servers)
    cache._fetched_at = time.time()  # «только что», иначе TTL честно истёк

    cache.rebind(_cfg(dict(servers)))  # новый словарь, тот же набор

    assert cache._data is not None, "rebind выбросил содержимое"
    assert cache._is_valid(cache._make_cache_key(servers), ttl=3600) is True


def test_owner_cache_needs_no_config():
    """Кэш приложения создаётся без внутреннего словаря MCP: сборка контекста
    сама перепривязывает его к своим серверам (``rebind``)."""
    import time

    cache = ToolsCache(os_backend=None)
    servers = {"srv": {"transport": "stdio", "command": "x"}}
    cache.rebind(_cfg(servers))
    cache._data = {"mcp_srv_tool": {"tool": {}, "metadata": {}}}
    cache._cache_key = cache._make_cache_key(servers)
    cache._fetched_at = time.time()
    assert cache._is_valid(cache._make_cache_key(servers), ttl=3600) is True


def test_changed_servers_invalidate_even_with_a_shared_cache():
    """Сменился набор серверов — кэш обязан сходить в дискавери заново."""
    import time

    cache = ToolsCache(_cfg({"a": {"transport": "stdio", "command": "x"}}))
    cache._data = {}
    cache._cache_key = cache._make_cache_key(
        {"a": {"transport": "stdio", "command": "x"}}
    )
    cache._fetched_at = time.time()

    other = {"b": {"transport": "stdio", "command": "y"}}
    assert cache._is_valid(cache._make_cache_key(other), ttl=3600) is False


def test_build_tools_data_accepts_a_cache():
    """Контракт: параметр есть и у сборки тулов, и у сборки контекста."""
    import inspect

    from gc_core.run_context import acreate_run_context
    from gc_core.tools import abuild_tools_data

    assert "tools_cache" in inspect.signature(abuild_tools_data).parameters
    assert "tools_cache" in inspect.signature(acreate_run_context).parameters


def test_run_context_carries_the_cache():
    """Кэш должен лежать НА КОНТЕКСТЕ — иначе субагенты его не достанут."""
    from dataclasses import fields

    from gc_core.run_context import RunContext

    assert "tools_cache" in {f.name for f in fields(RunContext)}


def test_subagents_take_the_cache_from_parent_ctx():
    """Оба спавна субагента обязаны брать кэш родителя, а не собирать свой.

    Проверка структурная и намеренно: поведенческая потребовала бы поднять
    настоящего субагента с настоящим MCP-сервером. А регрессия здесь тихая —
    забыли аргумент, всё работает, просто каждый спавн опять стоит секунду.
    """
    import ast

    from gc_testkit.paths import REPO

    src = (
        REPO / "packages/core/src/gc_core/tools/subagents/runner.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(src)

    # Имя асинхронное: сборки детей идут из уже работающего loop, поэтому
    # зовут abuild_tools_data (синхронная там подняла бы ещё один loop в потоке).
    builds = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "abuild_tools_data"
    ]
    assert len(builds) == 2, (
        f"ожидалось 2 вызова abuild_tools_data, нашлось {len(builds)}"
    )

    for call in builds:
        assert any(kw.arg == "tools_cache" for kw in call.keywords), (
            "спавн субагента собирает тулы без кэша родителя — снова полная "
            "дискавери на каждый спавн, прямо на event loop"
        )


async def test_default_is_unchanged_behaviour():
    """Без ``tools_cache`` всё как было: свой одноразовый кэш, полная дискавери."""
    from gc_core.tools.connect_mcp.agent_tools import (
        aget_mcp_tools_data_for_config,
    )

    tools, status = await aget_mcp_tools_data_for_config(
        make_settings({"mcp-servers": {}})
    )
    assert tools == {} and status == {}
