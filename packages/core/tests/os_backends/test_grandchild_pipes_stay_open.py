"""Фоновый внук команды переднего плана продолжает писать в свои потоки.

`exec_command` запускает `/bin/sh -c …` (на Windows — `cmd.exe /c …`), и его
stdout/stderr — трубы к нам. Команда с `&` (в cmd.exe — `start /b`, а чаще —
чужой скрипт, который сам уводит сервис в фон через `nohup`) оставляет внука,
унаследовавшего эти трубы. Шелл вышел, но EOF не придёт: трубу закрывает
ПОСЛЕДНИЙ писатель, а внук держит её часами.

Ждать его мы не можем — тул обязан вернуться. Но и закрывать трубу нельзя:
первая же запись внука даёт EPIPE, то есть SIGPIPE и смерть для обычной
программы, `BrokenPipeError` на каждой строке лога для Python. На develop
трубы дочитывали daemon-потоки до EOF, и внук спокойно жил весь сеанс.

Здесь внук просыпается заведомо позже потолка дренажа и пишет в оба потока.
"""

import asyncio
import os
import shlex
import sys
import threading
import time

from gc_core.backends.local import LocalFsBackend

#: Заведомо больше `_DRAIN_JOIN_TIMEOUT_SEC` (2 с): внук должен проснуться
#: после того, как прежний код уже закрыл бы трубы.
_WAKE_AFTER_SEC = 3

_CHILD = (
    "import sys, time\n"
    f"time.sleep({_WAKE_AFTER_SEC})\n"
    "try:\n"
    "    print('tick', flush=True)\n"
    "    sys.stderr.write('tick\\n')\n"
    "    sys.stderr.flush()\n"
    "    status = 'ok'\n"
    "except BaseException as exc:\n"
    "    status = repr(exc)\n"
    "open(sys.argv[1], 'w', encoding='utf-8').write(status)\n"
)


async def _status_when_written(path, timeout: float = 20.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            text = path.read_text(encoding="utf-8")
            if text:
                return text
        await asyncio.sleep(0.05)
    raise AssertionError(f"внук не отчитался за {timeout:.0f} с")


def _background_command(tmp_path, marker) -> str:
    """Команда, уводящая внука в фон средствами шелла: `&` в sh, `start /b` в cmd.exe.

    Внук — файлом: многострочный `-c` через cmd.exe не передать.
    """
    child = tmp_path / "child.py"
    child.write_text(_CHILD, encoding="utf-8")
    if os.name == "nt":
        return f'start /b "" "{sys.executable}" "{child}" "{marker}"'
    return f"{shlex.quote(sys.executable)} {shlex.quote(str(child))} {shlex.quote(str(marker))} &"


async def _new_drain_threads_gone(before: set, timeout: float = 10.0) -> bool:
    """Потоки дренажа, появившиеся после снимка `before`, дошли до EOF и вышли."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not [t for t in threading.enumerate() if t.name == "exec-pipe-drain" and t.ident not in before]:
            return True
        await asyncio.sleep(0.05)
    return False


async def test_a_grandchild_keeps_writing_after_the_tool_returned(tmp_path):
    """Команда вернулась, внук остался — и его записи не обрываются."""
    marker = tmp_path / "status"
    command = _background_command(tmp_path, marker)

    started = time.monotonic()
    res = await LocalFsBackend().aexec(command, timeout=30)
    elapsed = time.monotonic() - started

    assert res.return_code == 0, res
    assert elapsed < _WAKE_AFTER_SEC, f"тул ждал внука {elapsed:.1f} с — он не должен ждать вовсе"
    assert await _status_when_written(marker) == "ok", (
        "внук получил ошибку на записи: трубы закрыли под ним, и сервис, "
        "поднятый чужим скриптом, начал сыпаться через пару секунд"
    )


async def test_a_grandchild_survives_the_end_of_a_turn_loop(tmp_path):
    """Ход TUI идёт своим `asyncio.run` — внук обязан пережить и его конец.

    `AgentRunner` крутит каждый ход в рабочем потоке через `asyncio.run`. На
    выходе тот отменяет всё оставшееся, включая фоновый дренаж: трубы
    закрываются, и внук ловит EPIPE уже после того, как модель проверила порт
    внутри хода и отчиталась «сервер поднят». Проверка в одном loop (выше)
    этого не видит.
    """
    marker = tmp_path / "status"
    command = _background_command(tmp_path, marker)
    before = {t.ident for t in threading.enumerate()}

    def _turn():
        """Ровно то, что делает воркер TUI: ход в своём loop, в своём потоке."""

        async def _body():
            return await LocalFsBackend().aexec(command, timeout=30)

        return asyncio.run(_body())

    res = await asyncio.to_thread(_turn)

    assert res.return_code == 0, res
    assert await _status_when_written(marker) == "ok", (
        "внук получил ошибку на записи после конца хода: loop ушёл и забрал с "
        "собой дренаж, а трубы закрылись под живым процессом"
    )
    assert await _new_drain_threads_gone(before), (
        "внук вышел, а потоки дренажа живы: EOF не распознан, и каждая такая команда оставляла бы поток до конца сеанса"
    )
