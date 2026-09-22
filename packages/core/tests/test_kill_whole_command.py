"""Таймаут и отмена снимают команду ЦЕЛИКОМ, а не только её шелл.

``proc.kill()`` бьёт по одному pid. Для ``sh -c "a; b"`` это шелл, а его
потомки переживали и таймаут, и отмену — с унаследованными pipe'ами, то есть
ещё и удерживая дескрипторы. Агент возвращал «команда снята», а работа
продолжалась.
"""
import asyncio
import os
from pathlib import Path

import pytest
from gc_core.backends.local import LocalFsBackend

pytestmark = pytest.mark.skipif(os.name == "nt", reason="групп процессов нет на Windows")


def _marker_script(marker: Path) -> str:
    """Потомок, который переживёт шелл и оставит след, если его не убили."""
    return (
        f"python3 -c \"import time,pathlib; time.sleep(2); "
        f"pathlib.Path({str(marker)!r}).write_text('жив')\" & "
        "sleep 30"
    )


async def test_timeout_kills_the_child_too(tmp_path):
    marker = tmp_path / "survived.txt"

    await LocalFsBackend().aexec(_marker_script(marker), timeout=1)

    # Потомок спит 2 с: если группу сняли, до записи он не доживёт.
    await asyncio.sleep(3.0)
    assert not marker.exists(), (
        "потомок пережил таймаут — убили только шелл"
    )


async def test_cancellation_kills_the_child_too(tmp_path):
    marker = tmp_path / "survived.txt"

    task = asyncio.create_task(
        LocalFsBackend().aexec(_marker_script(marker), timeout=30)
    )
    await asyncio.sleep(0.5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    await asyncio.sleep(3.0)
    assert not marker.exists(), "потомок пережил отмену — убили только шелл"


async def test_a_normal_command_still_returns_its_output():
    """Группа процессов не должна менять обычный путь."""
    res = await LocalFsBackend().aexec("echo привет", timeout=10)

    assert "привет" in (res.stdout or ""), res
    assert res.return_code == 0
