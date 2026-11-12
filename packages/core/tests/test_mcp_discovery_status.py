import logging
import time

import pytest
from gc_core.tools.connect_mcp import registry as reg


async def test_build_registry_parallel_and_status(monkeypatch):
    async def fake_discover_one(name, cfg, *, timeout, os_backend=None):
        if name == "ok":
            return (
                [{"name": "do_x", "description": "d"}],
                {
                    "status": "connected",
                    "reason": None,
                    "tool_count": 1,
                    "elapsed_ms": 5,
                },
            )
        # «Зависание» дольше дедлайна — ждать его мы не обязаны.
        #
        # Ждём АСИНХРОННО, потому что моделируем медленный СЕРВЕР, а не
        # блокирующего клиента. У порта `alist_tools` корутинный: клиент,
        # который морозит loop, нарушает контракт, и раньше это скрывал поток
        # на уровне диспетчера. Теперь решение «нужен ли поток» принимает сам
        # клиент — долгоживущий stdio, например, уводит блокирующий обмен в
        # брошенный поток у себя внутри.
        import asyncio

        await asyncio.sleep(timeout + 2)
        return (
            [],
            {
                "status": "connected",
                "reason": None,
                "tool_count": 0,
                "elapsed_ms": 0,
            },
        )

    monkeypatch.setattr(reg, "_adiscover_one", fake_discover_one)
    servers = {
        "ok": {"transport": "stdio", "command": "x"},
        "hang": {"transport": "stdio", "command": "y"},
    }

    t0 = time.monotonic()
    tools, status = await reg._abuild_tools_registry(
        servers, discovery_timeout_sec=1
    )
    elapsed = time.monotonic() - t0

    assert elapsed < 1.5
    assert status["ok"]["status"] == "connected"
    assert status["hang"]["status"] == "failed"
    assert "таймаут" in (status["hang"]["reason"] or "")
    assert any(k.startswith("mcp_ok_") for k in tools)


async def test_discover_one_bad_command_failed_fast():
    t0 = time.monotonic()
    tools, status = await reg._adiscover_one(
        "office",
        {
            "transport": "stdio",
            "command": "definitely-not-a-real-cmd-xyz",
            "args": ["--x"],
        },
        timeout=8,
    )
    assert time.monotonic() - t0 < 5
    assert tools == []
    assert status["status"] == "failed"
    assert "не найдена" in (status["reason"] or "")


def test_humanize_unwraps_exception_group():
    # stream_http (anyio) оборачивает реальную ошибку в ExceptionGroup/TaskGroup —
    # причина должна разворачиваться до внутренней (человекочитаемой).
    class ConnectError(Exception):
        pass

    inner = ConnectError("Connection refused")
    eg = ExceptionGroup(
        "unhandled errors in a TaskGroup (1 sub-exception)", [inner]
    )
    reason = reg._humanize_discovery_error(
        eg, {"url": "http://localhost:8931/mcp"}
    )
    assert "нет соединения" in reason
    assert "localhost:8931" in reason
    assert "TaskGroup" not in reason


_HEADER_SECRET = "Bearer hdr-s3cret-value"
_URL_SECRET_URL = "http://127.0.0.1:9/mcp?apikey=qry-s3cret-value"


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError(f"отказ: {_HEADER_SECRET} для {_URL_SECRET_URL}"),
        ConnectionRefusedError("refused"),
        ExceptionGroup(
            "unhandled errors in a TaskGroup",
            [RuntimeError(f"отказ: {_HEADER_SECRET} для {_URL_SECRET_URL}")],
        ),
    ],
    ids=["текст-исключения", "шаблон-с-адресом", "группа-задач"],
)
async def test_failure_reason_has_no_secrets(
    monkeypatch, capsys, caplog, error
):
    """Отказ прежнего обнаружения: значения секретов определения
    (заголовок, строка запроса адреса) не попадают ни в строку ``[MCP]``,
    ни в лог, ни в статус."""

    class _Client:
        async def alist_tools(self, *, timeout):
            raise error

    monkeypatch.setattr(reg, "resolve_mcp_client", lambda *a, **k: _Client())
    cfg = {
        "transport": "stream_http",
        "url": _URL_SECRET_URL,
        "headers": {"Authorization": _HEADER_SECRET},
    }

    with caplog.at_level(logging.WARNING, logger=reg.logger.name):
        _, status = await reg._adiscover_one("srv", cfg, timeout=5)

    printed = capsys.readouterr().out
    assert status["status"] == "failed"
    assert "[MCP] srv: недоступен" in printed
    assert "MCP discovery failed for srv" in caplog.text
    for text in (printed, caplog.text, status["reason"]):
        assert "s3cret" not in text
    # В журнал — развёрнутая причина, а не обёртка группы задач.
    assert "TaskGroup" not in caplog.text
