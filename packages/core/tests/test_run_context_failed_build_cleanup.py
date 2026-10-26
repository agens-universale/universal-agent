"""Несостоявшаяся сборка контекста не оставляет ни логгера, ни MCP-сервера.

Страховка ``try/except BaseException`` в ``acreate_run_context`` накрывала
только прогрев корня. Дискавери MCP (``abuild_tools_data`` под токеном
владельца контекста) и досборка (``_finish_run_context``) шли уже вне её.
Обычный отказ дискавери исключения не бросает — сервер просто помечается
недоступным, — но отмена посреди дискавери и исключение сборки после неё
пролетали мимо: логгер прогона с открытым файлом доживал до конца процесса,
а stdio-сервер, поднятый дискавери под владельцем, — до ``atexit``. Закрыть
его раньше некому: снос фильтрует по владельцу, а контекста, которому он
принадлежит, так и не появилось.

Сервер здесь настоящий процесс, дискавери — настоящая: свойство «сервер
закрыт» проверяется по его смерти, а не по вызову подменённого закрытия.
"""

import asyncio
import logging
import os
import signal
import sys
import time
import uuid

import pytest
from gc_core import run_context
from gc_testkit.settings import make_settings

pytestmark = pytest.mark.skipif(
    os.name != "posix",
    reason="жизнь процесса проверяется через kill(pid, 0) — POSIX",
)

_SERVER_NAME = "leaky"

#: MCP-сервер-двойник. Сообщает свой pid и то, что у него спросили список
#: тулов; с ``GC_TEST_HANG_ON_LIST=1`` на этот вопрос не отвечает — дискавери
#: висит, пока её не снимут.
_SERVER = r"""
import json, os, sys

with open(os.environ["GC_TEST_PID_FILE"], "w") as f:
    f.write(str(os.getpid()))
hang = os.environ.get("GC_TEST_HANG_ON_LIST") == "1"

for line in sys.stdin:
    try:
        req = json.loads(line)
    except ValueError:
        continue
    rid = req.get("id")
    if rid is None:
        continue
    if req.get("method") == "tools/list":
        open(os.environ["GC_TEST_ASKED"], "w").close()
        if hang:
            continue
        result = {"tools": [{"name": "ping", "description": "ping",
                             "inputSchema": {"type": "object", "properties": {}}}]}
    else:
        result = {}
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}) + "\n")
    sys.stdout.flush()
"""


def _config(tmp_path, *, hang: bool):
    script = tmp_path / "mcp_server.py"
    script.write_text(_SERVER, encoding="utf-8")
    return make_settings(
        {
            "agent.stop_tools": [],
            "backend.api_mode": "chat_completions",
            "logging.save_logs": True,
            "logging.console_trace": False,
            "paths.logs_dump_dir": str(tmp_path / "logs"),
            "mcp.servers": {
                _SERVER_NAME: {
                    "transport": "stdio",
                    "command": sys.executable,
                    "args": [str(script)],
                    "persistent_session": True,
                    "env": {
                        "GC_TEST_PID_FILE": str(tmp_path / "server.pid"),
                        "GC_TEST_ASKED": str(tmp_path / "asked"),
                        "GC_TEST_HANG_ON_LIST": "1" if hang else "0",
                    },
                },
            },
        }
    )


def _run_id() -> str:
    return f"failed-build-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def run_loggers(monkeypatch):
    """Логгеры, созданные сборкой, с хендлерами на момент создания.

    Хендлеры запоминаем сразу: освобождение их с логгера снимает, и потом
    проверить, закрыт ли файл, было бы уже не на чем.
    """
    made: list = []
    real = run_context.create_run_logger

    def _spy(*args, **kwargs):
        built = real(*args, **kwargs)
        made.append((built[0], list(built[0].handlers)))
        return built

    monkeypatch.setattr(run_context, "create_run_logger", _spy)
    return made


def _registry():
    """Реестр stdio-сессий — тот, что видела дискавери.

    Импорт здесь, а не при сборе тестов: часть набора выкидывает
    ``gc_core.tools.*`` из ``sys.modules``, и копия, взятая при импорте
    модуля, могла бы оказаться не той, в которую дискавери положила сессию.
    """
    from gc_core.tools.connect_mcp.persistent_stdio import (
        PersistentStdioRegistry,
    )

    return PersistentStdioRegistry.instance()


def _sessions_left() -> list:
    return [key for key in _registry()._sessions if key[1] == _SERVER_NAME]


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _gone_within(pid: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while _alive(pid):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.02)
    return True


async def _until(path, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists():
        if time.monotonic() >= deadline:
            raise AssertionError(
                f"не дождались {path.name}: дискавери не дошла до сервера"
            )
        await asyncio.sleep(0.02)


async def _cleanup(tmp_path) -> None:
    """Добить всё, что сборка могла оставить, в любом исходе теста."""
    registry = _registry()
    with registry._lock:
        leftovers = [
            registry._sessions.pop(key)
            for key in list(registry._sessions)
            if key[1] == _SERVER_NAME
        ]
    for session in leftovers:
        try:
            await session.aclose()
        except Exception:
            pass
    pid_file = tmp_path / "server.pid"
    if pid_file.exists():
        pid = int(pid_file.read_text())
        if _alive(pid):
            try:
                os.killpg(pid, signal.SIGKILL)
            except OSError:
                pass


def _assert_logger_released(made: list) -> None:
    assert len(made) == 1, (
        f"проверка пустая: сборка создала логгеров — {len(made)}"
    )
    logger, handlers = made[0]
    files = [h for h in handlers if isinstance(h, logging.FileHandler)]
    assert files, "проверка пустая: у логгера прогона нет файла"
    assert logger.name not in logging.Logger.manager.loggerDict, (
        f"логгер {logger.name} остался в реестре logging до конца процесса"
    )
    assert all(h.stream is None or h.stream.closed for h in files), (
        "файл лога прогона остался открытым — дескриптор течёт"
    )


async def test_a_cancelled_discovery_leaves_neither_logger_nor_server(
    tmp_path, run_loggers
):
    """Отмена посреди дискавери: логгер отдан, сервер владельца закрыт.

    Самый жизненный путь утечки: сборку снимают (стоп пользователя, дедлайн
    вызывающего), пока сервер думает над ``tools/list``. Он к этому моменту
    уже поднят под токеном владельца контекста, которого не будет.
    """
    building = asyncio.create_task(
        run_context.acreate_run_context(
            _config(tmp_path, hang=True), run_id=_run_id()
        )
    )
    try:
        await _until(tmp_path / "asked")  # дискавери ждёт ответа сервера
        pid = int((tmp_path / "server.pid").read_text())
        building.cancel()
        with pytest.raises(asyncio.CancelledError):
            await building

        assert _gone_within(pid, 2.0), (
            "сервер, поднятый дискавери, пережил отменённую сборку контекста"
        )
        assert _sessions_left() == [], (
            "сессия несостоявшегося контекста осталась в реестре"
        )
        _assert_logger_released(run_loggers)
    finally:
        if not building.done():
            building.cancel()
            await asyncio.gather(building, return_exceptions=True)
        await _cleanup(tmp_path)


@pytest.mark.parametrize("where", ["discovery", "finish"])
async def test_a_failed_build_leaves_neither_logger_nor_server(
    tmp_path, run_loggers, monkeypatch, where
):
    """Исключение сборки после дискавери: логгер отдан, сервер владельца закрыт.

    ``discovery`` — дискавери прошла (сервер поднят под владельцем) и
    бросила; ``finish`` — упала досборка контекста. Оба места были вне
    страховки: она накрывала только прогрев корня.
    """
    if where == "discovery":
        # Контекст собирает базовый реестр (`abuild_tools_data`) и декорирует
        # его сам (автовыбор §5.2 п.4) — дискавери MCP идёт в этой сборке.
        real = run_context.abuild_tools_data

        async def _discover_then_fail(*args, **kwargs):
            await real(*args, **kwargs)
            raise RuntimeError("сборка тулов упала после дискавери")

        monkeypatch.setattr(
            run_context, "abuild_tools_data", _discover_then_fail
        )
    else:

        def _finish_fails(*args, **kwargs):
            raise RuntimeError("досборка контекста упала")

        monkeypatch.setattr(run_context, "_finish_run_context", _finish_fails)

    try:
        with pytest.raises(RuntimeError, match="упала"):
            await run_context.acreate_run_context(
                _config(tmp_path, hang=False), run_id=_run_id()
            )

        assert (tmp_path / "asked").exists(), (
            "проверка пустая: дискавери до сервера не дошла"
        )
        pid = int((tmp_path / "server.pid").read_text())
        assert _gone_within(pid, 2.0), (
            "сервер, поднятый дискавери, пережил упавшую сборку контекста"
        )
        assert _sessions_left() == [], (
            "сессия несостоявшегося контекста осталась в реестре"
        )
        _assert_logger_released(run_loggers)
    finally:
        await _cleanup(tmp_path)
