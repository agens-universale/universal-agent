"""Фоновый внук команды переднего плана продолжает писать в свои потоки.

`exec_command` запускает `/bin/sh -c …`, и его stdout/stderr — трубы к нам.
Команда с `&` (а чаще — чужой скрипт, который сам уводит сервис в фон через
`nohup`) оставляет внука, унаследовавшего эти трубы. Шелл вышел, но EOF не
придёт: трубу закрывает ПОСЛЕДНИЙ писатель, а внук держит её часами.

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
import time

import pytest

from gc_core.backends.local import LocalFsBackend

pytestmark = pytest.mark.skipif(
    os.name == "nt", reason="`&` в cmd.exe — разделитель команд, а не фон"
)

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


async def test_a_grandchild_keeps_writing_after_the_tool_returned(tmp_path):
    """Команда вернулась, внук остался — и его записи не обрываются."""
    marker = tmp_path / "status"
    command = (
        f"{shlex.quote(sys.executable)} -c {shlex.quote(_CHILD)} "
        f"{shlex.quote(str(marker))} &"
    )

    started = time.monotonic()
    res = await LocalFsBackend().aexec(command, timeout=30)
    elapsed = time.monotonic() - started

    assert res.return_code == 0, res
    assert elapsed < _WAKE_AFTER_SEC, (
        f"тул ждал внука {elapsed:.1f} с — он не должен ждать вовсе"
    )
    assert await _status_when_written(marker) == "ok", (
        "внук получил ошибку на записи: трубы закрыли под ним, и сервис, "
        "поднятый чужим скриптом, начал сыпаться через пару секунд"
    )
