"""Импорт сервера не меняет asyncio под встраивающим приложением.

До Задачи 5 gc_server на импорте подменял asyncio.run процессно-глобально —
включая код того, кто его импортировал. Дыру, ради которой это делалось,
закрывает gc_core.utils.aio_compat.run_coro_blocking, локально.
"""
import asyncio


def test_importing_server_does_not_patch_asyncio_run():
    original = asyncio.run
    import gc_server.server  # noqa: F401

    assert asyncio.run is original


def test_rest_api_utils_no_longer_exports_the_patch():
    from gc_server.rest_api import utils

    assert not hasattr(utils, "patch_asyncio_run_for_fastapi")
