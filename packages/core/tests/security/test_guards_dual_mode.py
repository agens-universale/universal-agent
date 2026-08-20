"""Гварды поверх async-тулов (план async-переезда, Задача 4).

Проверяется драйвер, а не политика: политика у sync и async одна и та же по
построению — тело гварда одно.
"""
import inspect
import json

import pytest

from gc_core.security.dual_wrapper import dual_wrap
from gc_core.security.url_policy import enforce_url_policy


def _passthrough_flow(*args, **kwargs):
    """Тело, которое всегда зовёт тул и отдаёт его результат как есть."""
    return (yield)


def _blocking_flow(*args, **kwargs):
    """Тело, которое отказывает, не позвав тул."""
    return "blocked"
    yield  # noqa: unreachable — делает функцию генератором


def _postprocessing_flow(*args, **kwargs):
    """Тело, которое правит результат тула."""
    result = yield
    return f"[{result}]"


def test_sync_func_gets_sync_wrapper():
    def f(x):
        return f"ok:{x}"

    w = dual_wrap(f, _passthrough_flow)
    assert not inspect.iscoroutinefunction(w)
    assert w("1") == "ok:1"


async def test_async_func_gets_async_wrapper():
    async def f(x):
        return f"ok:{x}"

    w = dual_wrap(f, _passthrough_flow)
    assert inspect.iscoroutinefunction(w)
    assert await w("1") == "ok:1"


async def test_block_skips_the_async_tool_entirely():
    """Отказ означает, что тул НЕ вызывался: иначе гвард ничего не защищает."""
    called = {"n": 0}

    async def f(x):
        called["n"] += 1
        return "должно быть недостижимо"

    assert await dual_wrap(f, _blocking_flow)("1") == "blocked"
    assert called["n"] == 0


def test_block_skips_the_sync_tool_entirely():
    called = {"n": 0}

    def f(x):
        called["n"] += 1
        return "должно быть недостижимо"

    assert dual_wrap(f, _blocking_flow)("1") == "blocked"
    assert called["n"] == 0


async def test_postprocessing_sees_async_result():
    async def f(x):
        return "ядро"

    assert await dual_wrap(f, _postprocessing_flow)("1") == "[ядро]"


def test_postprocessing_sees_sync_result():
    def f(x):
        return "ядро"

    assert dual_wrap(f, _postprocessing_flow)("1") == "[ядро]"


def test_wrapper_preserves_signature_metadata():
    """Гварды достают имена аргументов из сигнатуры обёрнутой функции
    (_extract_named_value) — без переноса метаданных они брали бы не тот."""
    def f(path: str, depth: int = 1):
        return "ok"

    w = dual_wrap(f, _passthrough_flow)
    assert list(inspect.signature(w).parameters) == ["path", "depth"]
    assert w.__name__ == "f"


def test_tool_exception_propagates_unchanged():
    """Гвард не глотает падение тула: этим занимается диспетчер apply_tool."""
    def f(x):
        raise ValueError("тул упал")

    with pytest.raises(ValueError, match="тул упал"):
        dual_wrap(f, _passthrough_flow)("1")


async def test_async_tool_exception_propagates_unchanged():
    async def f(x):
        raise ValueError("тул упал")

    with pytest.raises(ValueError, match="тул упал"):
        await dual_wrap(f, _passthrough_flow)("1")


def test_double_yield_is_a_loud_error():
    """Тело с двумя yield — ошибка автора гварда, а не тихо съеденный результат."""
    def _bad_flow(*args, **kwargs):
        yield
        yield

    def f(x):
        return "ok"

    with pytest.raises(RuntimeError, match="дважды"):
        dual_wrap(f, _bad_flow)("1")


# --- настоящий гвард поверх настоящего async-тула ---------------------------
#
# Тесты выше проверяют драйвер на игрушечных телах. Эти два — то, ради чего он
# сделан: политика одна и та же по построению (тело гварда одно), и здесь это
# видно на живом enforce_url_policy.

_URL_CONFIG = {
    "url_policy": {
        "enabled": True,
        "allowed_schemes": ["https"],
        "url_blacklist": ["blocked.example"],
    }
}


async def test_real_url_guard_blocks_async_tool():
    called = {"n": 0}

    @enforce_url_policy(url_arg="url")
    async def fetch(url: str, config_dict: dict) -> str:
        called["n"] += 1
        return json.dumps({"ok": True, "url": url})

    result = json.loads(await fetch("https://blocked.example", config_dict=_URL_CONFIG))

    assert result["ok"] is False
    assert result["error"]["type"] == "url_blocked_by_policy"
    assert result["error"]["matched_rule"] == "blocked.example"
    assert called["n"] == 0, "заблокированный тул не должен был выполниться"


async def test_real_url_guard_lets_allowed_async_tool_through():
    @enforce_url_policy(url_arg="url")
    async def fetch(url: str, config_dict: dict) -> str:
        return json.dumps({"ok": True, "url": url})

    result = json.loads(await fetch("https://allowed.example", config_dict=_URL_CONFIG))

    assert result == {"ok": True, "url": "https://allowed.example"}


def test_real_url_guard_verdicts_match_across_sync_and_async(anyio_backend=None):
    """Вердикт синхронного тула — тот же, что у асинхронного.

    Это и есть смысл одного тела: разъехавшаяся политика между sync- и
    async-копией была бы дырой, которую никто не заметит.
    """
    import asyncio

    @enforce_url_policy(url_arg="url")
    def fetch_sync(url: str, config_dict: dict) -> str:
        return json.dumps({"ok": True, "url": url})

    @enforce_url_policy(url_arg="url")
    async def fetch_async(url: str, config_dict: dict) -> str:
        return json.dumps({"ok": True, "url": url})

    def _verdict(payload: str) -> dict:
        # Поле tool — имя самого тула, и оно ОБЯЗАНО различаться: гвард
        # отчитывается о том, кого проверял. Сравниваем всё остальное.
        data = json.loads(payload)
        data.pop("tool", None)
        return data

    for url in ("https://blocked.example", "https://allowed.example",
                "http://insecure.example"):
        sync_verdict = _verdict(fetch_sync(url, config_dict=_URL_CONFIG))
        async_verdict = _verdict(asyncio.run(fetch_async(url, config_dict=_URL_CONFIG)))
        assert sync_verdict == async_verdict, url
