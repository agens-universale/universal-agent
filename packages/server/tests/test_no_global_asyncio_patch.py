"""Импорт сервера не меняет asyncio под встраивающим приложением.

До Задачи 5 gc_server на импорте подменял asyncio.run процессно-глобально —
включая код того, кто его импортировал. Дыру, ради которой это делалось,
закрывает gc_core.utils.aio_compat.run_coro_blocking, локально.
"""

import asyncio


def test_importing_server_does_not_patch_asyncio_run():
    original = asyncio.run
    import gc_server.app  # noqa: F401
    import gc_server.cli  # noqa: F401
    import gc_server.webhook.surface  # noqa: F401

    assert asyncio.run is original


def test_request_helpers_do_not_export_the_patch():
    from gc_server.webhook import request

    assert not hasattr(request, "patch_asyncio_run_for_fastapi")
