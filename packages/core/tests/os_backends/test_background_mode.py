"""Долгоживущая команда запускается ИНАЧЕ, чем быстрая, — и переживает нас.

Быстрая команда читается через трубы: её вывод нужен сейчас, обратное давление
трубы заставляет читателя уступать loop, а пережить вызов она не должна. У
долгоживущей (`npm run dev`, `uvicorn`, вотчер) всё наоборот: ждать её нельзя,
вывод идёт часами, и читателя у неё нет вовсе — он и не нужен, потому что вывод
пишется в ФАЙЛ. Файлу безразлично, кто его открыл и жив ли он ещё.

Прежние схемы давали это свойство в лучшем случае наполовину. Синхронная держала
daemon-поток, переживавший конец хода, — но только в TUI, потому что в gc-cli
завершается весь процесс и держать читателя некому в принципе. Схема с
анонимными файлами захвата давала выживание всегда, но платила за это на КАЖДОМ
вызове: голодание loop, рост файла без предела, поломка на Windows.
"""
import asyncio
import os
import pathlib
import shlex
import subprocess
import sys
import time

from gc_core.backends import local as local_backend
from gc_core.backends.local import LocalFsBackend

#: Корень исходников ядра: `.../packages/core/src`. Его и кладём на путь
#: подпроцессу, иначе он не найдёт `gc_core`.
_SRC_ROOT = pathlib.Path(local_backend.__file__).parents[2]

CHILD_TICKS = 20

#: Потомок пишет 20 тиков по 0,1 с — дольше, чем длится сам запуск. Иначе тест
#: зеленел бы и без выживания: быстрый потомок успевает всё до нашего выхода.
_CHILD = (
    "import sys, time\n"
    f"for i in range({CHILD_TICKS}):\n"
    "    print('tick', i, flush=True)\n"
    "    time.sleep(0.1)\n"
)


def _child_command() -> str:
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(_CHILD)}"


async def test_background_launch_returns_at_once():
    """Запуск не ждёт команду: возврат заметно раньше, чем она отработает."""
    backend = LocalFsBackend()

    started = time.monotonic()
    res = await backend.aspawn_background(_child_command())
    elapsed = time.monotonic() - started

    assert isinstance(res.get("pid"), int) and res["pid"] > 0, res
    assert elapsed < 0.5, (
        f"фоновый запуск ждал {elapsed:.2f} с — он не должен ждать вовсе "
        f"(потомок живёт {CHILD_TICKS * 0.1:.0f} с)"
    )


async def test_output_goes_to_the_log_we_opened(tmp_path):
    """Вывод фоновой команды попадает в переданный дескриптор."""
    log = tmp_path / "bg.log"
    fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        await LocalFsBackend().aspawn_background(_child_command(), log_fd=fd)
    finally:
        os.close(fd)

    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if log.exists() and log.read_text(encoding="utf-8").count("tick") >= CHILD_TICKS:
            break
        await asyncio.sleep(0.1)

    assert log.read_text(encoding="utf-8").count("tick") == CHILD_TICKS


async def test_without_a_log_the_child_is_not_blocked(tmp_path):
    """Без лога вывод отбрасывается, но команда работает, а не встаёт.

    Проверяется именно НЕблокировка: труба на её месте остановила бы
    разговорчивого потомка на заполненном буфере, и отметка не появилась бы.
    Потомок печатает заведомо больше буфера трубы, а в конце ставит отметку.
    """
    marker = tmp_path / "done"
    child = (
        "import sys\n"
        "for _ in range(200):\n"
        "    sys.stdout.write('x' * 8192)\n"
        "sys.stdout.flush()\n"
        f"open({str(marker)!r}, 'w').write('ok')\n"
    )
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(child)}"

    await LocalFsBackend().aspawn_background(command)          # лога нет вовсе

    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and not marker.exists():
        await asyncio.sleep(0.1)

    assert marker.exists(), (
        "потомок не дошёл до конца: 1,6 МБ вывода некуда девать, и он встал"
    )


async def test_background_spawns_keep_the_loop_breathing():
    """Пачка фоновых запусков не держит loop одним куском.

    `subprocess.Popen` не отдаёт управление сам, и на успешном пути
    `aspawn_background` больше нет ни одного `await`. Без явной уступки двадцать
    запусков подряд шли неразрывно: замерено 26–32 мс худшего разрыва против
    3 мс у прежнего asyncio-пути, который уступал внутри себя. Дефект дожил до
    замера именно потому, что разрыв loop на этом пути никто не мерил — тесты
    проверяли, что процесс запустился и выжил, но не чем за это платит
    приложение.

    Порог 20 мс выбран между наблюдаемыми режимами (5–7 мс с уступкой против
    26–32 без неё), чтобы тест не мигал на нагруженной машине.
    """
    backend = LocalFsBackend()
    spawns, tick, ceiling = 20, 0.005, 0.020

    stop = asyncio.Event()
    worst = 0.0

    async def pulse() -> None:
        nonlocal worst
        previous = time.monotonic()
        while not stop.is_set():
            await asyncio.sleep(tick)
            now = time.monotonic()
            worst = max(worst, now - previous - tick)
            previous = now

    beat = asyncio.ensure_future(pulse())
    await asyncio.sleep(0.25)                  # дать пульсу устояться
    try:
        for _ in range(spawns):
            await backend.aspawn_background("true")
    finally:
        stop.set()
        await beat

    assert worst < ceiling, (
        f"{spawns} фоновых запусков держали loop {worst * 1000:.1f} мс при "
        f"потолке {ceiling * 1000:.0f} мс — уступка потеряна"
    )


async def test_finished_background_children_leave_the_registry():
    """Вышедший фоновый потомок снимается с учёта на следующем запуске.

    Учёт нужен, чтобы дожать потомка: без ссылки он остаётся зомби до конца
    жизни агента. Прежняя уборка смотрела на `returncode` asyncio-процесса и
    была МЁРТВЫМ кодом: это поле выставляет наблюдатель того loop, на котором
    процесс создан, а loop хода к тому времени закрыт — записи не удалялись
    никогда, и за долгую сессию их копилось столько же, сколько запусков.

    Заодно это проверяет, что фон больше не привязан к loop: состояние
    процесса спрашивается синхронно, без всякого наблюдателя.
    """
    from gc_core.backends.local import _BACKGROUND_PROCS

    backend = LocalFsBackend()
    await backend.aspawn_background(f"{shlex.quote(sys.executable)} -c pass")
    short_lived = set(_BACKGROUND_PROCS)
    assert short_lived, "запущенный процесс не попал в учёт"

    # Ждём выхода команды, НЕ трогая учтённые процессы: `poll()` сам выставляет
    # `returncode`, и опроси мы их здесь — уборка, читающая только это поле,
    # выглядела бы рабочей. Тест перестал бы ловить ту самую регрессию.
    await asyncio.sleep(2.0)

    await backend.aspawn_background(_child_command())
    assert not (short_lived & _BACKGROUND_PROCS), (
        "вышедшие процессы остались в учёте — уборка не работает"
    )


def test_background_child_survives_the_whole_agent_process(tmp_path):
    """Главное свойство: внук переживает ВЫХОД процесса агента.

    Это случай gc-cli и трайла Harbor — агент ответил и завершился целиком. Ни
    один поток-читатель тут не помог бы: его убивает вместе с процессом. Файл
    же остаётся открытым у потомка, и ему безразлично, что нас больше нет.
    """
    log = tmp_path / "bg.log"
    agent = (
        "import asyncio, os, shlex, sys\n"
        f"sys.path.insert(0, {str(_SRC_ROOT)!r})\n"
        "from gc_core.backends.local import LocalFsBackend\n"
        f"child = {_CHILD!r}\n"
        f"log = {str(log)!r}\n"
        "async def turn():\n"
        "    fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)\n"
        "    try:\n"
        "        await LocalFsBackend().aspawn_background(\n"
        "            f'{shlex.quote(sys.executable)} -c {shlex.quote(child)}', log_fd=fd,\n"
        "        )\n"
        "    finally:\n"
        "        os.close(fd)\n"
        "asyncio.run(turn())\n"
    )

    done = subprocess.run([sys.executable, "-c", agent], capture_output=True, timeout=60)
    assert done.returncode == 0, done.stderr.decode()[-400:]

    # На момент выхода агента потомок заведомо НЕ успел отработать.
    early = log.read_text(encoding="utf-8").count("tick") if log.exists() else 0
    assert early < CHILD_TICKS, (
        f"потомок успел всё ({early}) до выхода агента — тест ничего не проверяет"
    )

    deadline = time.monotonic() + 15
    text = ""
    while time.monotonic() < deadline:
        text = log.read_text(encoding="utf-8") if log.exists() else ""
        if text.count("tick") >= CHILD_TICKS:
            break
        time.sleep(0.1)

    assert text.count("tick") == CHILD_TICKS, (
        f"дописано {text.count('tick')} из {CHILD_TICKS}: фоновый процесс не "
        f"пережил выход агента"
    )
