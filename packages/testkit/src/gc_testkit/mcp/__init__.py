"""Помощники тестов жизненного цикла MCP: настоящие серверы и их журналы.

Серверы — исполняемые файлы рядом (``stdio_server.py``, ``stream_server.py``).
Поведение stdio-сервера задаёт JSON-файл «управления» (``set_behavior``);
оба сервера дописывают журнал ``<control>.mcp-journal`` — по нему тест видит
запуски, потомков, выходы и запросы (``events``, ``launches``, ``children``,
``exits``). Фикстуры — в
:mod:`gc_testkit.mcp.fixtures`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import socket
import subprocess
import sys
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

from gc_core.tools.connect_mcp.client_port import AgentRuntimeClient

_HERE = Path(__file__).parent
_STDIO_SERVER = _HERE / "stdio_server.py"
_STREAM_SERVER = _HERE / "stream_server.py"
_CONTROL_ENV = "GC_TEST_CONTROL"
#: Суффикс журнала к пути управления. Свой, а не ``.log``: уборка ищет журналы
#: под ``tmp_path`` по маске, и логи агента туда попадать не должны. Серверы
#: держат ту же строку у себя — они запускаются без тест-кита.
JOURNAL_SUFFIX = ".mcp-journal"
#: Транспорт ядра → имя транспорта FastMCP в argv сервера.
_STREAM_TRANSPORTS = {"stream_http": "streamable-http", "stream_sse": "sse"}
_POLL_SEC = 0.02
_STOP_SEC = 5.0


def stdio_cfg(control: Path) -> dict:
    """Определение stdio-сервера с поведением из файла ``control``."""
    return {
        "transport": "stdio",
        "command": sys.executable,
        "args": [str(_STDIO_SERVER)],
        "env": {_CONTROL_ENV: str(control)},
    }


def set_behavior(control: Path, **keys: Any) -> None:
    """Заменить поведение сервера целиком; запись атомарная.

    Сервер перечитывает файл перед каждым ответом и никогда не видит его
    недописанным: пишем рядом и переименовываем.
    """
    control = Path(control)
    staged = control.with_name(control.name + ".tmp")
    staged.write_text(json.dumps(keys), encoding="utf-8")
    os.replace(staged, control)


def _journal(control: Path) -> Path:
    """Путь журнала сервера с этим управлением."""
    return Path(f"{control}{JOURNAL_SUFFIX}")


def events(control: Path) -> list[str]:
    """Строки журнала по порядку; нет журнала — пусто."""
    try:
        text = _journal(control).read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    return text.splitlines()


def _pids(control: Path, kind: str) -> list[int]:
    """pid из строк ``<kind> <pid>``; единственный разбор этих строк."""
    return [
        int(parts[1])
        for parts in (line.split() for line in events(control))
        if len(parts) == 2 and parts[0] == kind and parts[1].isdigit()
    ]


def launches(control: Path) -> list[int]:
    """pid каждого запуска сервера по порядку."""
    return _pids(control, "start")


def children(control: Path) -> list[int]:
    """pid каждого порождённого сервером потомка по порядку."""
    return _pids(control, "child")


def exits(control: Path) -> list[int]:
    """pid каждого штатного выхода по порядку."""
    return _pids(control, "exit")


def _process_state(pid: int) -> str:
    """Буква состояния процесса (``R``, ``S``, ``Z`` …); пусто — нет такого."""
    if Path("/proc/self/stat").exists():
        try:
            stat = Path(f"/proc/{pid}/stat").read_text()
        except OSError:
            return ""
        return stat.rpartition(")")[2].split()[0]
    shown = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)],
        capture_output=True,
        text=True,
        check=False,
    )
    return shown.stdout.strip()[:1]


def alive(pid: int) -> bool:
    """Процесс существует и не зомби."""
    state = _process_state(pid)
    return bool(state) and state != "Z"


async def wait_for(
    predicate: Callable[[], bool], timeout: float = 10.0
) -> None:
    """Дождаться истинности ``predicate``, иначе ``AssertionError``."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() >= deadline:
            raise AssertionError(
                f"условие не выполнилось за {timeout} с: {predicate!r}"
            )
        await asyncio.sleep(_POLL_SEC)


def _tool_payload(response: Any) -> dict:
    """Ответ тула из ответа транспорта.

    stdio отдаёт конверт JSON-RPC (``result`` или ``error``), транспорты SDK —
    сам результат вызова; ответ тула — JSON в первом текстовом блоке.
    """
    body = response.get("result", response) if response else {}
    content = body.get("content") if isinstance(body, dict) else None
    if not content or body.get("isError"):
        raise RuntimeError(f"вызов тула не удался: {response!r}")
    return json.loads(content[0]["text"])


async def call_tool(
    server_name: str,
    server_cfg: dict,
    tool: str,
    arguments: dict,
    *,
    timeout: float = 20.0,
) -> dict:
    """Вызвать тул через ``AgentRuntimeClient`` под текущим владельцем.

    Возвращает разобранный ответ тула; ошибка сервера или пустой ответ —
    ``RuntimeError``.
    """
    client = AgentRuntimeClient(server_name, server_cfg)
    response = await client.acall_tool(tool, arguments, timeout=timeout)
    return _tool_payload(response)


def free_port() -> int:
    """Свободный сейчас порт TCP на loopback."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _accepts(port: int) -> bool:
    with socket.socket() as sock:
        sock.settimeout(_POLL_SEC)
        return sock.connect_ex(("127.0.0.1", port)) == 0


async def _stop(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.terminate()
    try:
        await asyncio.to_thread(proc.wait, _STOP_SEC)
    except subprocess.TimeoutExpired:
        proc.kill()
        await asyncio.to_thread(proc.wait, _STOP_SEC)


@contextlib.asynccontextmanager
async def stream_server(
    tmp_path: Path, transport: str, *, port: int | None = None
) -> AsyncIterator[tuple[dict, Path]]:
    """Поднять ``stream_server.py``; отдаёт ``(server_cfg, control)``.

    ``transport`` — транспорт ядра (``stream_http`` | ``stream_sse``);
    порт — заданный или свободный. Выход из блока гасит сервер.
    """
    port = free_port() if port is None else port
    control = Path(tmp_path) / f"stream-{port}.json"
    proc = subprocess.Popen(
        [
            sys.executable,
            str(_STREAM_SERVER),
            _STREAM_TRANSPORTS[transport],
            str(port),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env={**os.environ, _CONTROL_ENV: str(control)},
        start_new_session=True,
    )
    try:
        await wait_for(lambda: proc.poll() is not None or _accepts(port))
        if proc.poll() is not None:
            raise AssertionError(
                f"тестовый сервер вышел с кодом {proc.returncode}"
            )
        cfg = {"transport": transport, "url": f"http://127.0.0.1:{port}"}
        yield cfg, control
    finally:
        await _stop(proc)


class FakeClock:
    """Подставные часы: время идёт только по ``sleep`` и ``advance``."""

    def __init__(self) -> None:
        self._now = 0.0

    def now(self) -> float:
        return self._now

    async def sleep(self, sec: float) -> None:
        """Сдвинуть время и уступить циклу."""
        self._now += sec
        await asyncio.sleep(0)

    def advance(self, sec: float) -> None:
        self._now += sec


__all__ = [
    "JOURNAL_SUFFIX",
    "FakeClock",
    "alive",
    "call_tool",
    "children",
    "events",
    "exits",
    "free_port",
    "launches",
    "set_behavior",
    "stdio_cfg",
    "stream_server",
    "wait_for",
]
