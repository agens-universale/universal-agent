"""Прогон ``gc-cli`` под настоящим SIGINT — для ``test_cli_mcp_service.py``.

Запуск: ``python cli_sigint_driver.py <сценарий> <каталог>``. Сервис MCP —
двойник с глухим закрытием потребителя, сборка и ход подменены; сигналы
шлёт поток процесса самому себе по событиям прогона, а не по часам.
Каждая строка stdout — событие ``<секунды> <что>``; ответ хода печатается
прогоном как есть.

Сценарии:
* ``close_after_success`` — ход удался, одно нажатие во время закрытия;
* ``second_in_close`` — первое нажатие останавливает ход, второе — во
  время закрытия;
* ``second_in_turn`` — ход стоп не слушает, второе нажатие — во время хода.
"""

import asyncio
import os
import signal
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import yaml

SCENARIO, WORKDIR = sys.argv[1], Path(sys.argv[2])
os.chdir(WORKDIR)
CONFIG = WORKDIR / "config.yml"
CONFIG.write_text(
    yaml.safe_dump(
        {
            "backend": {
                "api_mode": "chat_completions",
                "completions_url": "http://x.invalid/v1/chat/completions",
            },
            "agent": {"model": "scripted"},
            "logging": {"save_logs": False, "console_trace": True},
            "paths": {"threads_data_path": str(WORKDIR / "threads")},
        }
    ),
    encoding="utf-8",
)
sys.argv = ["gc-cli", "-q", "x", "--config", str(CONFIG)]

import gc_core.utils.async_http as async_http
from gc_cli import cli
from gc_core import upgrade_gate

T0 = time.monotonic()
#: Закрытие потребителя (глухое) — сигнал потоку.
CLOSING = threading.Event()
#: Ход начался / стоп хода пришёл.
TURN = threading.Event()
STOPPED = threading.Event()


def log(what: str) -> None:
    print(f"{time.monotonic() - T0:.2f} {what}", flush=True)


class _Consumer:
    def servers(self) -> dict:
        return {}

    async def aclose(self) -> None:
        log("consumer.aclose")
        CLOSING.set()
        await asyncio.sleep(30)


class _Service:
    def __init__(self, **_kw) -> None:
        pass

    def open_consumer(self, label: str) -> _Consumer:
        return _Consumer()

    async def aclose(self, budget: float = 10.0) -> None:
        log(f"service.aclose {budget:.2f}")
        await asyncio.sleep(min(budget, 30))


class _Executor:
    last_terminal_outcome = None

    def __init__(self) -> None:
        self._stop = asyncio.Event()

    def get_state(self):
        return None

    def stop_run(self) -> None:
        log("stop_run")
        self._stop.set()
        STOPPED.set()

    async def arun(self, *_a, **_kw):
        TURN.set()
        if SCENARIO == "close_after_success":
            return "ответ хода"
        if SCENARIO == "second_in_close":
            await self._stop.wait()
            return "ход остановлен"
        await asyncio.sleep(60)
        return None


async def _build(_settings, **_kw):
    return SimpleNamespace(logger=MagicMock())


async def _aclose_clients() -> None:
    log("aclose_clients")


cli._ensure_project_gc_dir = lambda: None
cli._ensure_user_skills_dir = lambda: None
upgrade_gate.detect_upgrade = lambda path=None: None
upgrade_gate.mark_current_run = lambda path=None: None
cli.McpService = _Service
cli.acreate_run_context = _build
cli.AgentExecutor = lambda _ctx: _Executor()
async_http.aclose_clients = _aclose_clients
# Удачный прогон ждёт глухое закрытие весь бюджет — короткий; у прерванного
# бюджета нет, и длинный бюджет показал бы, что выход его всё же ждёт.
cli._MCP_CLOSE_BUDGET_SEC = 1.5 if SCENARIO == "close_after_success" else 20


def _press() -> None:
    log("SIGINT")
    os.kill(os.getpid(), signal.SIGINT)


def _presser() -> None:
    TURN.wait(30)
    if SCENARIO == "close_after_success":
        CLOSING.wait(30)
        _press()
        return
    _press()
    if SCENARIO == "second_in_close":
        CLOSING.wait(30)
    else:
        STOPPED.wait(30)
    _press()


threading.Thread(target=_presser, daemon=True).start()
try:
    code = cli.main()
    log(f"rc {code}")
except KeyboardInterrupt:
    log("KeyboardInterrupt")
