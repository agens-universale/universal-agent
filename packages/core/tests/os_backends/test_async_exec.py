"""Неблокирующий запуск команд (план async-переезда, Задача 18).

Прежняя реализация держала по два дренажных потока на каждый вызов: pipe
нельзя не читать — заполненный буфер блокирует запись в дочернем процессе.
"""
import asyncio
import sys
import threading
import time

import pytest
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


async def test_native_and_adapted_paths_agree():
    """Паритет: чужой синхронный бэкенд через адаптер даёт тот же результат.

    Сравнивать асинхронный путь с синхронным больше нечего: синхронной формы
    у НАШИХ бэкендов нет — она была обёрткой ``run_coro_blocking``, то есть
    заводила второй loop. Прежняя версия этого теста сравнивала
    ``backend.exec`` с ``backend.aexec``; после снятия двойника она сравнивала
    бы результат с самим собой и зеленела бы при любой поломке.

    Настоящая двойственность осталась на уровне БЭКЕНДА: свой ядро видит
    напрямую, чужой синхронный — через ``SyncExecAdapter``. Оба обязаны дать
    один результат.
    """
    cmd = f'{sys.executable} -c "print(\'паритет\')"'
    native = LocalFsBackend()
    assert aexec_port(native) is native
    native_res = await native.aexec(cmd)

    class _Legacy:
        """Чужой бэкенд: только синхронная форма, как у встраивающего."""

        def __init__(self) -> None:
            self._inner = LocalFsBackend()

        def exec(self, command, *, cwd=None, timeout=None, env=None,
                 capture_max_chars=None):
            # Адаптер зовёт это в БРОШЕННОМ потоке, где своего loop нет.
            return asyncio.run(self._inner.aexec(
                command, cwd=cwd, timeout=timeout, env=env,
                capture_max_chars=capture_max_chars,
            ))

    port = aexec_port(_Legacy())
    assert isinstance(port, SyncExecAdapter)
    adapted_res = await port.aexec(cmd)

    assert adapted_res.stdout == native_res.stdout
    assert adapted_res.return_code == native_res.return_code
    assert adapted_res.timed_out == native_res.timed_out


# --- регрессии, найденные ревизией переезда --------------------------------
#
# Три свойства, которые синхронная реализация держала, а асинхронная потеряла.
# Каждое было незакрыто тестом: мутация `continue`→`break` в чтении захвата
# оставляла все 205 тестов exec зелёными.


async def test_captured_survives_drain_timeout():
    """Захваченное отдаётся, даже если дренаж не успел дойти до EOF.

    Фоновый потомок держит pipe открытым, поэтому чтение до конца не
    завершится и дренаж будет отпущен по таймауту. Снимок при этом обязан
    остаться у вызывающего.

    Утверждение о ФОРМЕ снимка обновлено под голова+хвост, пришедший с
    develop: раньше отдавался только префикс (`"A" * 1000`), теперь — начало
    и настоящий конец потока. Это и есть смысл правки: по одному началу
    нельзя было понять, чем команда кончилась.
    """
    backend = LocalFsBackend(exec_output_capture_max_chars=1000)
    res = await backend.aexec(
        f"{sys.executable} -c \"print('A'*200000)\"; (sleep 20 &)", timeout=10
    )

    assert res.stdout_hit_capture_cap is True
    assert len(res.stdout) == 1000, "захваченное потеряно вместе с отменой дренажа"
    assert set(res.stdout) <= {"A", "\n"}, f"в снимок попало лишнее: {res.stdout[:80]!r}"
    stream = res.stdout_stream
    assert stream.truncated is True
    assert stream.head == "A" * 500, "начало потока потеряно"
    assert stream.tail.endswith("\n"), (
        "хвост не настоящий: у `print` в конце перевод строки, и он обязан "
        "доехать — ради этого голова+хвост и делались"
    )
    assert stream.total_chars > 200000, (
        f"счётчик считает только снимок, а не весь поток: {stream.total_chars}"
    )


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


# --- судьба фонового потомка -----------------------------------------------
#
# Здесь стоял тест `test_background_child_can_keep_writing_after_the_tool_returned`:
# он запускал `npm run dev &` обычным `aexec` и требовал, чтобы потомок дописал
# 300 КиБ уже после возврата тула. Это обещание СНЯТО сознательно.
#
# Команда переднего плана читается через трубу, и труба требует живого читателя:
# после возврата вызова читать её некому, и разговорчивый внук встанет на
# заполненном буфере. Платить за редкий случай на каждом вызове мы больше не
# хотим — именно эта плата дала голодание loop, безразмерный файл захвата и
# поломку запуска на Windows.
#
# Свойство «фоновый процесс живёт дальше» никуда не делось: оно целиком переехало
# в ОТДЕЛЬНЫЙ фоновый режим и проверяется в `test_background_mode.py` — причём
# строже, чем здесь, вплоть до выхода всего процесса агента, чего эта редакция
# не проверяла никогда.
