"""Большой stdin не должен подвешивать запуск процесса навсегда.

Дефект переезда на asyncio: `_arun_bounded_process` писал в stdin и ждал
`drain()` ДО того, как заводились читатели stdout/stderr, а таймаут стартовал
ещё позже — в `_await_process_exit`. Труба конечна с обеих сторон, поэтому
когда stdin больше буфера с high-water (около 128 КиБ), а ребёнок до чтения
stdin успевает выдать больше ~192 КиБ (asyncio перестаёт читать на 2×limit),
родитель встаёт в `drain()`, ребёнок — в `write()`. Взаимная блокировка, и
таймаут тула до неё не доходит: шаг агента висит бесконечно.

Достижимо не в теории: `skills_run_script` передаёт аргументы JSON'ом через
stdin, `arun_python` — код. Таймаут тула здесь единственный предохранитель
агента, и именно он не срабатывал.

Числа в тесте подобраны с запасом за оба порога. При 200 КБ вывода дефект
скрыт предбуфером asyncio — тест на таких числах был бы зелёным всегда.
"""

import asyncio
import sys
import time

import pytest
from gc_core.backends.local import LocalFsBackend

#: Ребёнок сначала ЗАЛИВАЕТ stdout, и только потом читает stdin — так и
#: возникает встречная блокировка.
_FLOOD_THEN_READ = (
    "import sys\n"
    "sys.stdout.write('x' * 2_000_000)\n"
    "sys.stdout.flush()\n"
    "data = sys.stdin.read()\n"
    "sys.stderr.write(str(len(data)))\n"
)

_STDIN_BYTES = 300_000
_TOOL_TIMEOUT = 5
#: Потолок ожидания самого теста: заметно больше таймаута тула, чтобы отличить
#: «таймаут сработал» от «висим навсегда».
_OUTER_BUDGET = 20


@pytest.fixture
def backend():
    return LocalFsBackend()


async def test_large_stdin_with_flooding_child_does_not_hang(
    backend, tmp_path
):
    """Шаг обязан завершиться — сам или по таймауту, но не висеть."""
    script = tmp_path / "flood.py"
    script.write_text(_FLOOD_THEN_READ, encoding="utf-8")

    started = time.perf_counter()
    try:
        result = await asyncio.wait_for(
            backend.aexec_argv(
                [sys.executable, str(script)],
                timeout=_TOOL_TIMEOUT,
                stdin="a" * _STDIN_BYTES,
            ),
            timeout=_OUTER_BUDGET,
        )
    except TimeoutError:
        spent = time.perf_counter() - started
        pytest.fail(
            f"aexec_argv висел больше {spent:.1f}с при таймауте тула "
            f"{_TOOL_TIMEOUT}с — предохранителя нет: запись в stdin идёт "
            f"до старта читателей и вне дедлайна"
        )

    spent = time.perf_counter() - started
    assert spent < _OUTER_BUDGET, spent
    assert result is not None


async def test_child_actually_receives_the_whole_stdin(backend, tmp_path):
    """Читатели вперёд — но stdin обязан доехать целиком, а не потеряться."""
    script = tmp_path / "echo_len.py"
    script.write_text(
        "import sys\ndata = sys.stdin.read()\nsys.stdout.write(str(len(data)))\n",
        encoding="utf-8",
    )

    result = await backend.aexec_argv(
        [sys.executable, str(script)], timeout=30, stdin="b" * _STDIN_BYTES
    )

    assert str(_STDIN_BYTES) in str(result.stdout), result


async def test_run_python_with_large_code_still_works(backend):
    """`arun_python` гонит код через тот же stdin-путь."""
    code = "x = 0\n" + "x += 1\n" * 20_000 + "print(x)\n"

    result = await backend.arun_python(code, timeout=60)

    assert "20000" in str(result.stdout), result
