"""Неблокирующий запуск команд (план async-переезда, Задача 18).

Прежняя реализация держала по два дренажных потока на каждый вызов: pipe
нельзя не читать — заполненный буфер блокирует запись в дочернем процессе.
"""
import asyncio
import pytest
import sys
import threading
import time

from gc_core.backends.async_protocol import SyncExecAdapter, aexec_port
from gc_core.backends.local import LocalFsBackend
from gc_core.backends.protocol import ExecResult


async def test_exec_runs_and_captures_output():
    port = aexec_port(LocalFsBackend())
    res = await port.aexec(f'{sys.executable} -c "print(42)"')
    assert res.stdout.strip() == "42"
    assert res.return_code == 0
    assert res.timed_out is False


async def test_two_commands_run_concurrently():
    """Две команды по 0.3с параллельно занимают заметно меньше 0.6с."""
    port = aexec_port(LocalFsBackend())
    started = time.monotonic()
    a, b = await asyncio.gather(
        port.aexec(f'{sys.executable} -c "import time; time.sleep(0.3); print(\'a\')"'),
        port.aexec(f'{sys.executable} -c "import time; time.sleep(0.3); print(\'b\')"'),
    )
    elapsed = time.monotonic() - started

    assert a.stdout.strip() == "a" and b.stdout.strip() == "b"
    assert elapsed < 0.55, f"похоже, команды шли последовательно: {elapsed:.2f}с"


async def test_no_helper_threads_per_call():
    """Ради этого всё и делалось: дренажных потоков на вызов больше нет."""
    port = aexec_port(LocalFsBackend())
    before = threading.active_count()
    await port.aexec(f'{sys.executable} -c "print(\'x\' * 100000)"')
    assert threading.active_count() == before


async def test_timeout_kills_process():
    port = aexec_port(LocalFsBackend())
    res = await port.aexec(f'{sys.executable} -c "import time; time.sleep(5)"', timeout=1)
    assert res.timed_out is True
    assert res.return_code is None


async def test_stderr_captured_separately():
    port = aexec_port(LocalFsBackend())
    res = await port.aexec(f'{sys.executable} -c "import sys; sys.stderr.write(\'беда\')"')
    assert "беда" in res.stderr
    assert res.stdout == ""


async def test_output_is_bounded_and_flagged():
    """Буфер ограничен, но процесс не виснет: хвост дочитывается в никуда."""
    backend = LocalFsBackend(exec_output_capture_max_chars=100)
    port = aexec_port(backend)
    res = await port.aexec(f'{sys.executable} -c "print(\'y\' * 50000)"')

    assert len(res.stdout) <= 100
    assert res.stdout_hit_capture_cap is True
    assert res.return_code == 0


async def test_run_python_receives_code_via_stdin():
    port = aexec_port(LocalFsBackend())
    res = await port.arun_python("print('из stdin')")
    assert "из stdin" in res.stdout


async def test_argv_form_needs_no_shell():
    port = aexec_port(LocalFsBackend())
    res = await port.aexec_argv([sys.executable, "-c", "print('argv')"])
    assert res.stdout.strip() == "argv"


async def test_sync_backend_gets_adapter():
    """Чужой FsBackend работает без единой правки у его автора."""
    class _Legacy:
        def exec(self, command, *, cwd=None, timeout=None, env=None):
            return ExecResult(stdout="legacy", stderr="", return_code=0)

    port = aexec_port(_Legacy())
    assert isinstance(port, SyncExecAdapter)
    res = await port.aexec("что угодно")
    assert res.stdout == "legacy"


async def test_native_backend_is_used_directly():
    backend = LocalFsBackend()
    assert aexec_port(backend) is backend


async def test_async_and_sync_results_agree():
    """Паритет: тот же результат, что у синхронного пути."""
    backend = LocalFsBackend()
    cmd = f'{sys.executable} -c "print(\'паритет\')"'
    sync_res = await asyncio.to_thread(lambda: backend.exec(cmd))
    async_res = await backend.aexec(cmd)

    assert async_res.stdout == sync_res.stdout
    assert async_res.return_code == sync_res.return_code
    assert async_res.timed_out == sync_res.timed_out


# --- регрессии, найденные ревизией переезда --------------------------------
#
# Три свойства, которые синхронная реализация держала, а асинхронная потеряла.
# Каждое было незакрыто тестом: мутация `continue`→`break` в _adrain_stream
# оставляла все 205 тестов exec зелёными.


async def test_captured_survives_drain_timeout():
    """Захваченное отдаётся, даже если хвост дочитать не успели.

    Свойство удалённого при переезде test_drain_pipe_publishes_captured_
    before_discard_finishes: по достижении лимита буфер публикуется сразу, и
    вызывающий не ждёт конца отбрасывания. Фоновый потомок держит pipe
    открытым, поэтому дренаж до EOF не дойдёт и будет снят по таймауту.
    """
    backend = LocalFsBackend(exec_output_capture_max_chars=1000)
    res = await backend.aexec(
        f"{sys.executable} -c \"print('A'*200000)\"; (sleep 20 &)", timeout=10
    )

    assert res.stdout_hit_capture_cap is True
    assert len(res.stdout) == 1000, "захваченное потеряно вместе с отменой дренажа"
    assert res.stdout == "A" * 1000


async def test_background_child_is_not_a_timeout():
    """Фоновый потомок, унаследовавший stdout, — не повод объявлять таймаут.

    ``Process.wait()`` резолвится только по EOF всех pipe'ов, поэтому наивная
    реализация выжигала весь таймаут и теряла уже напечатанный вывод.
    Синхронный ``Popen.wait()`` возвращался по выходу процесса.
    """
    backend = LocalFsBackend()
    res = await backend.aexec("echo раз; (sleep 20 &); echo два", timeout=8)

    assert res.timed_out is False, "ложный таймаут из-за фонового потомка"
    assert res.return_code == 0
    assert res.stdout.split() == ["раз", "два"]


async def test_cancel_kills_the_child():
    """Отмена прогона не оставляет процесс-сироту."""
    import os
    import subprocess

    mark = f"gcexecprobe{os.getpid()}"
    code = f"import time; _={mark!r}; time.sleep(30)"

    def alive() -> list:
        out = subprocess.run(["pgrep", "-f", mark], capture_output=True, text=True).stdout
        return [p for p in out.split() if p and int(p) != os.getpid()]

    backend = LocalFsBackend()
    task = asyncio.create_task(backend.aexec_argv([sys.executable, "-c", code]))
    await asyncio.sleep(0.8)
    assert len(alive()) == 1, "процесс не стартовал — тест ничего не проверяет"

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.5)

    survivors = alive()
    for pid in survivors:
        subprocess.run(["kill", "-9", pid])
    assert survivors == [], "после отмены остался процесс-сирота"
