"""На Windows стоп и таймаут снимают дерево команды, а не только ``cmd``.

Групп процессов, как на POSIX, там нет: ``proc.terminate()`` и ``proc.kill()``
гасят сам ``cmd``, а запущенная им команда доживала до конца. Ход в чате
останавливался, ``ping`` продолжал работать.
"""

import asyncio
import os
import sys

import pytest
from gc_core.backends import local
from gc_core.backends.local import LocalFsBackend, _astop_process


class _Proc:
    pid = 4242

    def __init__(self, returncode=None) -> None:
        self.returncode = returncode
        self.terminated = False

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 1

    def kill(self) -> None:
        self.returncode = 1


class _Killer:
    def __init__(self, code: int) -> None:
        self._code = code

    async def wait(self) -> int:
        return self._code


def _fake_taskkill(monkeypatch, proc: _Proc, code: int = 0) -> list:
    calls = []

    async def spawn(*argv, **_kwargs):
        calls.append(list(argv))
        if code == 0:
            proc.returncode = 1
        return _Killer(code)

    monkeypatch.setattr(local.asyncio, "create_subprocess_exec", spawn)
    return calls


async def test_windows_stop_kills_the_tree(monkeypatch):
    proc = _Proc()
    calls = _fake_taskkill(monkeypatch, proc)

    await _astop_process(proc, os_name="nt")

    assert len(calls) == 1
    assert (
        calls[0][0].lower().endswith(os.path.join("system32", "taskkill.exe"))
    )
    assert calls[0][1:] == ["/T", "/F", "/PID", "4242"]
    assert proc.terminated is False


async def test_failed_taskkill_falls_back_to_the_process_itself(monkeypatch):
    proc = _Proc()
    _fake_taskkill(monkeypatch, proc, code=128)

    await _astop_process(proc, os_name="nt")

    assert proc.terminated is True


async def test_missing_taskkill_falls_back_to_the_process_itself(monkeypatch):
    async def spawn(*argv, **_kwargs):
        raise FileNotFoundError(argv[0])

    monkeypatch.setattr(local.asyncio, "create_subprocess_exec", spawn)
    proc = _Proc()

    await _astop_process(proc, os_name="nt")

    assert proc.terminated is True


async def test_exited_process_is_not_hit_by_pid(monkeypatch):
    """Pid вышедшего процесса Windows может отдать чужому."""
    proc = _Proc(returncode=0)
    calls = _fake_taskkill(monkeypatch, proc)

    await _astop_process(proc, os_name="nt")

    assert calls == []


async def test_posix_never_calls_taskkill(monkeypatch):
    """Настоящих сигналов нет: группа подменена, выдуманный pid не задет."""
    proc = _Proc()
    calls = _fake_taskkill(monkeypatch, proc)
    signals = []

    def killpg(_pgid, sig):
        signals.append(sig)
        proc.returncode = 1

    monkeypatch.setattr(local.os, "getpgid", lambda pid: pid, raising=False)
    monkeypatch.setattr(local.os, "killpg", killpg, raising=False)

    await _astop_process(proc, os_name="posix")

    assert calls == []
    assert signals


@pytest.mark.skipif(sys.platform != "win32", reason="дерево cmd — Windows")
async def test_timeout_kills_the_child_of_cmd(tmp_path):
    started = tmp_path / "started.txt"
    survived = tmp_path / "survived.txt"
    child = (
        f'"{sys.executable}" -c "import time,pathlib; '
        f"pathlib.Path(r'{started}').write_text('x'); time.sleep(3); "
        f"pathlib.Path(r'{survived}').write_text('x')\""
    )

    await LocalFsBackend().aexec(child, timeout=1)

    await asyncio.sleep(4.0)
    assert started.exists(), "потомок не запустился — тест ничего не проверил"
    assert not survived.exists(), "потомок cmd пережил таймаут"
