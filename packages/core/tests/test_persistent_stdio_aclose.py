"""Закрытие долгоживущих stdio MCP-сессий не занимает поток из пула.

``AgentExecutor.arun`` в своём ``finally`` гасит сессии, и это небыстро:
terminate → ожидание до 5 с → killpg → ещё до 2 с. Прямой вызов из корутины
заморозил бы loop приложения на всё это время, поэтому закрытие уходило в
``asyncio.to_thread``.

Поток здесь не обязателен. Блокирует ровно одно — ожидание смерти процесса
(``Popen.wait(timeout)``) и ожидание ответа на close-тул (очередь читателя).
Оба заменяются опросом с ``await asyncio.sleep``: ``Popen.poll()`` и
``queue.get_nowait()`` не блокируют, а сигналы (``killpg``, ``terminate``) —
мгновенные системные вызовы.

Сам ``Popen`` при этом остаётся ``Popen``: переводить его на
``asyncio.create_subprocess_exec`` нельзя, процесс делят разные event loop
(разбор — в докстроке ``persistent_stdio``). Меняется только СПОСОБ ЖДАТЬ.

Форма повторяет уже принятую в ``stream_client._PersistentSessionManager``:
синхронный ``close_all`` рядом с ``aclose_all``.
"""
import subprocess
import sys
import time

import pytest
from gc_core.tools.connect_mcp import persistent_stdio as ps

#: Скрипт-двойник MCP-сервера, который ИГНОРИРУЕТ SIGTERM. Так проверяется
#: дорогой путь: graceful-таймаут вышел, дожимаем SIGKILL.
_STUBBORN = (
    "import signal, sys, time\n"
    "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "sys.stderr.close()\n"
    "time.sleep(60)\n"
)

#: Послушный: умирает по SIGTERM сразу.
_OBEDIENT = "import time\ntime.sleep(60)\n"


def _spawn(code: str) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", code],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


@pytest.fixture
def obedient():
    proc = _spawn(_OBEDIENT)
    yield proc
    if proc.poll() is None:
        proc.kill()
        proc.wait(timeout=5)


@pytest.fixture
def stubborn():
    proc = _spawn(_STUBBORN)
    # Даём ему поставить обработчик, иначе SIGTERM прилетит до SIG_IGN.
    time.sleep(0.3)
    yield proc
    if proc.poll() is None:
        proc.kill()
        proc.wait(timeout=5)


async def test_aterminate_kills_an_obedient_process(obedient):
    """Обычный путь: процесс уходит по SIGTERM, до SIGKILL не доходит."""
    await ps._aterminate_process_tree(obedient, graceful_timeout=5.0)

    assert obedient.poll() is not None, "процесс пережил закрытие"


async def test_aterminate_escalates_to_kill_for_a_stubborn_process(stubborn):
    """Дорогой путь: SIGTERM проигнорирован — дожимаем SIGKILL.

    Эскалация и есть причина, по которой закрытие «небыстрое». Если её
    потерять, игнорирующий SIGTERM сервер (например Chromium под
    @playwright/mcp) переживёт агента — ровно то, ради чего всё затевалось.
    """
    await ps._aterminate_process_tree(stubborn, graceful_timeout=0.4)

    assert stubborn.poll() is not None, "упрямый процесс пережил закрытие"


async def test_loop_stays_free_while_waiting_for_a_stubborn_process(stubborn):
    """Главная проверка: ждём опросом, а не потоком и не блокировкой loop."""
    from gc_testkit import LoopPulse, quiet_gc_for_loop_timing

    # Сборщик глушится на окне замера: пауза поколения 2 на куче полного набора
    # (180–220 мс) больше порога, и тест судил бы её, а не ожидание процесса.
    #
    # Пульс прогретый и общий. Собственный ставился в очередь прямо перед
    # `await _aterminate_process_tree` и не видел ничего, что снос делает до
    # первой уступки, — сигнал группе и первую проверку: 0,2 с сна в начале
    # сноса тест пропускал — проверено подменой.
    with quiet_gc_for_loop_timing():
        async with LoopPulse() as pulse:
            await ps._aterminate_process_tree(stubborn, graceful_timeout=0.5)

    assert len(pulse.gaps) > 20, f"пульс почти не тикал: {len(pulse.gaps)}"
    assert pulse.worst() < 0.15, (
        f"loop залипал на {pulse.worst():.3f}с — ожидание процесса всё ещё "
        f"блокирующее ({pulse.report()})"
    )


async def test_registry_aclose_all_is_awaitable():
    """У реестра есть асинхронный вход — его и зовёт цикл агента."""
    registry = ps.PersistentStdioRegistry()

    await registry.aclose_all()  # пустой реестр: просто не должно падать

    assert hasattr(registry, "aclose_all")


def test_sync_close_all_still_exists():
    """Синхронный вход сохранён: его зовут завершение процесса и тесты."""
    registry = ps.PersistentStdioRegistry()

    registry.close_all()

    assert hasattr(registry, "close_all")
