"""Неблокирующий запуск команд (план async-переезда, Задача 18).

Прежняя реализация держала по два дренажных потока на каждый вызов: pipe
нельзя не читать — заполненный буфер блокирует запись в дочернем процессе.
"""
import asyncio
import threading
import time

from gc_core.backends.async_protocol import SyncExecAdapter, aexec_port
from gc_core.backends.local import LocalFsBackend
from gc_core.backends.protocol import ExecResult


async def test_exec_runs_and_captures_output():
    port = aexec_port(LocalFsBackend())
    res = await port.aexec('python -c "print(42)"')
    assert res.stdout.strip() == "42"
    assert res.return_code == 0
    assert res.timed_out is False


async def test_two_commands_run_concurrently():
    """Две команды по 0.3с параллельно занимают заметно меньше 0.6с."""
    port = aexec_port(LocalFsBackend())
    started = time.monotonic()
    a, b = await asyncio.gather(
        port.aexec('python -c "import time; time.sleep(0.3); print(\'a\')"'),
        port.aexec('python -c "import time; time.sleep(0.3); print(\'b\')"'),
    )
    elapsed = time.monotonic() - started

    assert a.stdout.strip() == "a" and b.stdout.strip() == "b"
    assert elapsed < 0.55, f"похоже, команды шли последовательно: {elapsed:.2f}с"


async def test_no_helper_threads_per_call():
    """Ради этого всё и делалось: дренажных потоков на вызов больше нет."""
    port = aexec_port(LocalFsBackend())
    before = threading.active_count()
    await port.aexec('python -c "print(\'x\' * 100000)"')
    assert threading.active_count() == before


async def test_timeout_kills_process():
    port = aexec_port(LocalFsBackend())
    res = await port.aexec('python -c "import time; time.sleep(5)"', timeout=1)
    assert res.timed_out is True
    assert res.return_code is None


async def test_stderr_captured_separately():
    port = aexec_port(LocalFsBackend())
    res = await port.aexec('python -c "import sys; sys.stderr.write(\'беда\')"')
    assert "беда" in res.stderr
    assert res.stdout == ""


async def test_output_is_bounded_and_flagged():
    """Буфер ограничен, но процесс не виснет: хвост дочитывается в никуда."""
    backend = LocalFsBackend(exec_output_capture_max_chars=100)
    port = aexec_port(backend)
    res = await port.aexec('python -c "print(\'y\' * 50000)"')

    assert len(res.stdout) <= 100
    assert res.stdout_hit_capture_cap is True
    assert res.return_code == 0


async def test_run_python_receives_code_via_stdin():
    port = aexec_port(LocalFsBackend())
    res = await port.arun_python("print('из stdin')")
    assert "из stdin" in res.stdout


async def test_argv_form_needs_no_shell():
    port = aexec_port(LocalFsBackend())
    res = await port.aexec_argv(["python", "-c", "print('argv')"])
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
    cmd = 'python -c "print(\'паритет\')"'
    sync_res = await asyncio.to_thread(lambda: backend.exec(cmd))
    async_res = await backend.aexec(cmd)

    assert async_res.stdout == sync_res.stdout
    assert async_res.return_code == sync_res.return_code
    assert async_res.timed_out == sync_res.timed_out
