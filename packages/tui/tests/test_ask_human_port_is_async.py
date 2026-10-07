"""Порт «спросить человека» у TUI не должен морозить loop агента.

Два свойства, и оба были нарушены.

**Форма порта.** Ядро ждёт ``ctx.ask_human`` нативно, только если он
awaitable; обычную функцию оно зовёт ИНЛАЙН (``tools/turn/ask_human.py``).
TUI ставил синхронный ``queue.get()``, то есть весь loop агента стоял, пока
человек думает: вместе с ним стояли субагенты, keep-alive MCP-сессий и хуки
компакции. Ожидание человека ничем не ограничено — «стоял» здесь измеряется
минутами.

Прежний комментарий в коде оправдывал синхронную форму тем, что диспетчер
всё равно уводит тул в ``to_thread``. Эта ступень снята вместе с синхронным
входом тулов, и оправдание вместе с ней.

**Разблокировка на стопе.** ``stop()`` дёргал ``cancel_confirm()``, но не
``cancel_ask_human()``. Поток оставался в ``queue.get()`` навсегда, ``arun``
не доходил до своего ``finally`` — а там закрытие MCP-сессий и пула httpx.
То есть Chromium переживал агента, а сокеты жили до конца процесса.
"""

import asyncio
import inspect
import threading
import time

from gc_tui.services.agent_runner import AgentRunner


class _Ex:
    """Минимальный executor: runner ставит порт на его ``_ctx``."""

    def __init__(self) -> None:
        self._ctx = type("Ctx", (), {"ask_human": None})()
        self.stopped = False

    def stop_run(self) -> None:
        self.stopped = True


def test_port_is_a_coroutine_function():
    """Синхронный порт ядро зовёт инлайн — значит порт обязан быть корутинным."""
    runner = AgentRunner(executor=_Ex())

    assert inspect.iscoroutinefunction(runner._read_answer), (
        "порт снова синхронный: ядро позовёт его инлайн и заморозит loop "
        "на всё время, пока человек думает"
    )


def test_wired_port_on_ctx_is_the_coroutine_one():
    """Мало объявить корутину — на ctx должна попасть именно она.

    Через фабрику, а не через ``executor=``: порт ставится только на пути
    сборки, и это продовый путь (прямая подстановка — тестовая лазейка).
    """
    ex = _Ex()
    runner = AgentRunner(executor_factory=lambda: ex)

    runner._ensure_executor()

    assert inspect.iscoroutinefunction(ex._ctx.ask_human), ex._ctx.ask_human


async def test_loop_keeps_ticking_while_the_human_thinks():
    """Свойство, а не форма: соседняя корутина тикает во время ожидания."""
    from gc_testkit import LoopPulse, quiet_gc_for_loop_timing

    runner = AgentRunner(executor=_Ex())

    # Сборщик глушится на окне замера: пауза поколения 2 на куче полного набора
    # (180–220 мс) больше порога, и тест судил бы её, а не порт.
    #
    # Пульс прогретый и общий. Собственный видел начало порта только потому,
    # что его задачу создавали РАНЬШЕ задачи порта и loop запускал их по
    # очереди: 0,2 с сна в начале `_read_answer` тест замечал, но
    # держалось это на порядке двух строк, который ничто не сторожит. Прогрев
    # делает это свойством измерителя.
    with quiet_gc_for_loop_timing():
        async with LoopPulse() as pulse:
            asker = asyncio.create_task(runner._read_answer("как дела?"))

            await asyncio.sleep(0.3)  # человек «думает»
            runner.submit_ask_human_reply("хорошо")

            answer = await asyncio.wait_for(asker, 5)

    assert answer == "хорошо", answer
    assert len(pulse.gaps) > 10, f"пульс почти не тикал: {len(pulse.gaps)}"
    assert pulse.worst() < 0.1, (
        f"loop залипал на {pulse.worst() * 1000:.0f} мс — порт снова блокирующий ({pulse.report()})"
    )


def test_stop_unblocks_a_pending_ask_human():
    """`stop()` обязан разблокировать ожидание, иначе `finally` у `arun` не наступит."""
    ex = _Ex()
    runner = AgentRunner(executor=ex)

    done = threading.Event()
    result: list = []

    def _wait() -> None:
        result.append(runner._read_answer_blocking("?"))
        done.set()

    waiter = threading.Thread(target=_wait, daemon=True)
    waiter.start()
    time.sleep(0.05)  # дать потоку встать в очередь

    runner.stop()

    assert done.wait(3), (
        "поток остался в queue.get() после stop() — `arun` никогда не дойдёт до закрытия MCP-сессий и пула httpx"
    )
    assert ex.stopped is True
