"""SIGTERM ``gc-cli`` гасит процессы MCP (спека §5.12).

Обработчик ставится сразу после создания сервиса прогона, до сборки
контекста, и снимается на выходе из области сервиса — после закрытия и
безусловного гашения. Гашение после закрытия идёт и тогда, когда закрытие
прервано. Пробуждение цикла по SIGTERM и по Ctrl+C снимаются в любом
порядке, не отнимая друг у друга wakeup-fd.
"""

import sys

# `import cli` разбирает sys.argv на импорте (-q обязателен).
sys.argv = ["gc-cli", "-q", "x"]

import signal
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from gc_cli import cli
from gc_cli.run_stop import install_run_sigint, restore_run_sigint
from gc_core.tools.connect_mcp.exit_signals import kill_mcp_on_sigterm
from test_cli_mcp_service import _arun_cli, fake, host


async def test_scope_installs_handler_before_build(fake, monkeypatch):
    handled: list = []

    def _install(service):
        fake.events.append("handler")
        handled.append(service)
        return lambda: fake.events.append("restore")

    async def _build(settings, **kwargs):
        fake.events.append("build")
        return SimpleNamespace(logger=MagicMock())

    monkeypatch.setattr(cli, "kill_mcp_on_sigterm", _install)
    monkeypatch.setattr(cli, "acreate_run_context", _build)

    assert await _arun_cli() == 0

    assert fake.events == [
        "handler",
        "open_consumer",
        "build",
        "turn",
        "consumer.aclose",
        "service.aclose",
        "service.kill",
        "restore",
        "aclose_clients",
    ]
    [service] = handled
    assert service._fake is fake  # сервис, созданный областью


async def test_interrupted_close_still_kills(fake, monkeypatch):
    """Второй Ctrl+C посреди закрытия: прерывание выходит наружу, а
    процессы всё равно гасятся."""

    async def _interrupted(service, consumer, budget):
        fake.events.append("close")
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "_aclose_mcp", _interrupted)

    with pytest.raises(KeyboardInterrupt):
        async with cli._mcp_service_scope():
            pass

    assert fake.events[-2:] == ["close", "service.kill"]


class _Service:
    def kill_processes_now(self) -> None:
        raise AssertionError("гашение без сигнала")


class _Executor:
    def stop_run(self) -> None:
        return None


def _wakeup_fd() -> int:
    """Текущий wakeup-fd процесса (``-1`` — не поставлен)."""
    fd = signal.set_wakeup_fd(-1)
    signal.set_wakeup_fd(fd)
    return fd


@pytest.mark.parametrize("sigterm_first", [True, False])
async def test_wakeups_restore_in_any_order(sigterm_first):
    """SIGTERM ставится областью сервиса, Ctrl+C — прогоном; снятие
    первого не снимает wakeup-fd, пока цикл будит второй."""
    before = _wakeup_fd()
    restore_sigterm = kill_mcp_on_sigterm(_Service())
    sigint = install_run_sigint(_Executor())
    assert sigint is not None
    loop_fd = _wakeup_fd()
    assert loop_fd != before

    if sigterm_first:
        restore_sigterm()
        assert _wakeup_fd() == loop_fd  # Ctrl+C ещё будит цикл
        restore_run_sigint(sigint)
    else:
        restore_run_sigint(sigint)
        assert _wakeup_fd() == loop_fd  # SIGTERM ещё будит цикл
        restore_sigterm()

    assert _wakeup_fd() == before
