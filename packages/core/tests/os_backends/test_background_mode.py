"""Долгоживущая команда запускается ИНАЧЕ, чем быстрая, — и переживает нас.

Быстрая команда читается через трубы: её вывод нужен сейчас, обратное давление
трубы заставляет читателя уступать loop, а пережить вызов она не должна. У
долгоживущей (`npm run dev`, `uvicorn`, вотчер) всё наоборот: ждать её нельзя,
вывод идёт часами, и читателя у неё нет вовсе — все её потоки смотрят в нулевое
устройство. Запись туда не кончается ошибкой и не встаёт никогда, и ничто не
связывает потомка с нами.

Прежние схемы давали это свойство в лучшем случае наполовину. Синхронная держала
daemon-поток, переживавший конец хода, — но только в TUI, потому что в gc-cli
завершается весь процесс и держать читателя некому в принципе. Схема с
анонимными файлами захвата давала выживание всегда, но платила за это на КАЖДОМ
вызове: голодание loop, рост файла без предела, поломка на Windows. Лог в файле
фоновой команды упирался в гарды: путь от модели — гард записи, свой лог в `.gc`
— закрыт на чтение, и модель его не видела.
"""
import asyncio
import os
import pathlib
import shlex
import subprocess
import sys
import time

import pytest

from gc_core.backends import local as local_backend
from gc_core.backends.local import LocalFsBackend

#: Корень исходников ядра: `.../packages/core/src`. Его и кладём на путь
#: подпроцессу, иначе он не найдёт `gc_core`.
_SRC_ROOT = pathlib.Path(local_backend.__file__).parents[2]

CHILD_TICKS = 20

#: Потомок пишет 20 тиков по 0,1 с — дольше, чем длится сам запуск. Иначе тест
#: зеленел бы и без выживания: быстрый потомок успевает всё до нашего выхода.
#:
#: Каждый тик идёт в stdout и stderr — окажись там труба без читателя, потомок
#: погиб бы на следующей записи, — и в СВОЙ файл (путь — первым аргументом):
#: stdout смотрит в нулевое устройство, и увидеть жизнь потомка иначе нельзя.
_CHILD = (
    "import sys, time\n"
    "with open(sys.argv[1], 'a', encoding='utf-8') as ticks:\n"
    f"    for i in range({CHILD_TICKS}):\n"
    "        print('tick', i, flush=True)\n"
    "        print('tick', i, file=sys.stderr, flush=True)\n"
    "        print('tick', i, file=ticks, flush=True)\n"
    "        time.sleep(0.1)\n"
)


def _child_command(ticks) -> str:
    return (
        f"{shlex.quote(sys.executable)} -c {shlex.quote(_CHILD)} "
        f"{shlex.quote(str(ticks))}"
    )


async def _read_when_written(path, timeout: float = 15.0) -> str:
    """Дождаться, пока потомок допишет метку, и вернуть её текст.

    Ждём непустого файла, а не его появления: `open` создаёт файл раньше, чем
    потомок успевает в него написать.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            text = path.read_text(encoding="utf-8")
            if text:
                return text
        await asyncio.sleep(0.05)
    raise AssertionError(f"потомок не написал {path.name} за {timeout:.0f} с")


async def test_background_launch_returns_at_once(tmp_path):
    """Запуск не ждёт команду: возврат заметно раньше, чем она отработает."""
    backend = LocalFsBackend()

    started = time.monotonic()
    res = await backend.aspawn_background(_child_command(tmp_path / "ticks.log"))
    elapsed = time.monotonic() - started

    assert isinstance(res.get("pid"), int) and res["pid"] > 0, res
    assert elapsed < 0.5, (
        f"фоновый запуск ждал {elapsed:.2f} с — он не должен ждать вовсе "
        f"(потомок живёт {CHILD_TICKS * 0.1:.0f} с)"
    )


@pytest.mark.skipif(os.name == "nt", reason="NUL на Windows через samestat не сверить")
async def test_child_streams_are_the_null_device(tmp_path):
    """stdin, stdout и stderr потомка — нулевое устройство, а не труба и не файл.

    Ломается, если вернуть потоку трубу (потомок погибнет вместе с читателем)
    или файл лога (вернётся лог, упирающийся в гарды). Потомок сам сверяет свои
    дескрипторы с `os.devnull` и пишет ответ в метку.
    """
    marker = tmp_path / "streams"
    child = (
        "import os, sys\n"
        "null = os.stat(os.devnull)\n"
        "same = [os.path.samestat(os.fstat(fd), null) for fd in (0, 1, 2)]\n"
        "open(sys.argv[1], 'w').write(repr(same))\n"
    )
    command = (
        f"{shlex.quote(sys.executable)} -c {shlex.quote(child)} "
        f"{shlex.quote(str(marker))}"
    )

    await LocalFsBackend().aspawn_background(command)

    assert await _read_when_written(marker) == "[True, True, True]", (
        "потоки потомка (stdin, stdout, stderr) смотрят не в нулевое устройство"
    )


async def test_chatty_child_is_not_blocked(tmp_path):
    """Потомок, пишущий много в оба потока, доходит до конца, а не встаёт.

    Проверяется именно НЕблокировка: труба на месте нулевого устройства
    остановила бы разговорчивого потомка на заполненном буфере, и отметка не
    появилась бы. Потомок печатает заведомо больше буфера трубы — и в stdout, и
    в stderr, — а в конце ставит отметку.
    """
    marker = tmp_path / "done"
    child = (
        "import sys\n"
        "for _ in range(200):\n"
        "    sys.stdout.write('x' * 8192)\n"
        "    sys.stderr.write('y' * 8192)\n"
        "sys.stdout.flush()\n"
        "sys.stderr.flush()\n"
        f"open({str(marker)!r}, 'w').write('ok')\n"
    )
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(child)}"

    await LocalFsBackend().aspawn_background(command)

    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and not marker.exists():
        await asyncio.sleep(0.1)

    assert marker.exists(), (
        "потомок не дошёл до конца: 3,2 МБ вывода некуда девать, и он встал"
    )


@pytest.mark.skipif(os.name == "nt", reason="сессии есть только на POSIX")
async def test_posix_child_leaves_our_session(tmp_path):
    """Потомок уходит в свою сессию: сигналы, адресованные нам, до него не доходят.

    Закрытие терминала (SIGHUP) и Ctrl+C (SIGINT) достаются сессии и группе
    агента. Останься потомок в них — погиб бы вместе с окном агента. Ломается,
    если потерять `start_new_session`.
    """
    marker = tmp_path / "sid"
    child = "import os, sys\nopen(sys.argv[1], 'w').write(str(os.getsid(0)))\n"
    command = (
        f"{shlex.quote(sys.executable)} -c {shlex.quote(child)} "
        f"{shlex.quote(str(marker))}"
    )

    await LocalFsBackend().aspawn_background(command)

    assert int(await _read_when_written(marker)) != os.getsid(0), (
        "потомок остался в сессии агента"
    )


def test_windows_child_gets_its_own_hidden_console():
    """На Windows потомок получает СВОЮ консоль без окна — и ничего сверх.

    Сессий там нет, а события консоли — Ctrl+C, Ctrl+Break, закрытие окна —
    система рассылает ВСЕМ процессам, подключённым к консоли. Каждый флаг ниже —
    отдельная поломка, видимая только на Windows. Здесь они закреплены, чтобы
    правка на macOS или Linux не сняла их молча; поведение проверяет следующий
    тест, и идёт он только на Windows.
    """
    from gc_core.backends.local import _background_detach_kwargs

    flags = _background_detach_kwargs("nt")["creationflags"]

    assert flags & 0x08000000, "нет CREATE_NO_WINDOW: потомок умрёт вместе с окном агента"
    assert not flags & 0x00000008, "DETACHED_PROCESS: консольные внуки cmd откроют окна"
    assert not flags & 0x00000010, "CREATE_NEW_CONSOLE: у каждой фоновой команды своё окно"
    assert not flags & 0x01000000, (
        "CREATE_BREAKAWAY_FROM_JOB: в job без права выхода запуск упадёт целиком"
    )


@pytest.mark.skipif(sys.platform != "win32", reason="консоль Windows")
async def test_windows_child_does_not_share_our_console(tmp_path):
    """Потомок живёт в своей консоли: окно агента и его Ctrl+C до него не дойдут.

    Потомок спрашивает `GetConsoleProcessList`, кто делит с ним консоль, и пишет
    ответ в метку. Нашего PID там быть не должно — иначе закрытие окна агента
    убьёт и его. А консоль у него быть обязана: без неё (`DETACHED_PROCESS`)
    каждый консольный внук `cmd` откроет своё видимое окно.
    """
    import ctypes

    ours = (ctypes.c_uint32 * 64)()
    if ctypes.windll.kernel32.GetConsoleProcessList(ours, 64) == 0:
        pytest.skip("у тестового процесса нет консоли — сравнивать не с чем")

    marker = tmp_path / "console"
    probe = tmp_path / "probe.py"
    probe.write_text(
        "import ctypes, sys\n"
        "ids = (ctypes.c_uint32 * 64)()\n"
        "n = ctypes.windll.kernel32.GetConsoleProcessList(ids, 64)\n"
        "pids = [ids[i] for i in range(min(n, 64))]\n"
        "open(sys.argv[1], 'w').write(f'{n}:' + ','.join(map(str, pids)))\n",
        encoding="utf-8",
    )
    command = subprocess.list2cmdline([sys.executable, str(probe), str(marker)])

    await LocalFsBackend().aspawn_background(command)

    count, _, listed = (await _read_when_written(marker)).partition(":")
    pids = {int(p) for p in listed.split(",") if p}
    assert int(count) >= 1, "у потомка нет консоли вовсе — внуки cmd откроют окна"
    assert os.getpid() not in pids, f"потомок делит консоль с агентом: {sorted(pids)}"


async def test_background_spawns_keep_the_loop_breathing():
    """Пачка фоновых запусков не держит loop одним куском.

    `subprocess.Popen` не отдаёт управление сам, и на успешном пути
    `aspawn_background` больше нет ни одного `await`. Без явной уступки двадцать
    запусков подряд идут неразрывно. Дефект дожил до замера именно потому, что
    разрыв loop на этом пути никто не мерил — тесты проверяли, что процесс
    запустился и выжил, но не чем за это платит приложение.

    **Критерий структурный, а не в миллисекундах, и это принципиально.**
    Абсолютного порога здесь НЕ СУЩЕСТВУЕТ: замерено 18.09.2026, что здоровый
    режим даёт худший разрыв до 19 мс в изоляции и до 39 мс внутри полного
    набора (куча от тысяч тестов удорожает каждый `Popen`), а сломанный —
    от 39,8 мс в изоляции. Диапазоны СМЫКАЮТСЯ: любое фиксированное число либо
    валит здоровый прогон в наборе, либо пропускает сломанный в изоляции. На
    этом я подряд ошибся дважды, выставив сначала 26, потом 50 мс.

    Что режимы разделяет надёжно — сколько раз цикл получил управление за время
    пачки. Замерено, 8 прогонов на режим, без перекрытия:

        уступка на месте : 5 тиков, худший разрыв 14,8–20,0 мс
        уступка снята    : 1 тик,  худший разрыв 29,5–48,7 мс

    Один тик означает, что вся пачка прошла ОДНИМ куском — это и есть «loop
    заблокирован целиком». Число тиков от скорости машины и тяжести процесса не
    зависит, в отличие от миллисекунд.
    """
    backend = LocalFsBackend()
    spawns, tick = 20, 0.005

    stop = asyncio.Event()
    stamped: list = []

    async def pulse() -> None:
        previous = time.monotonic()
        while not stop.is_set():
            await asyncio.sleep(tick)
            now = time.monotonic()
            stamped.append((now, now - previous))
            previous = now

    beat = asyncio.ensure_future(pulse())
    await asyncio.sleep(0.25)                  # дать пульсу устояться
    # Разделяем базу и замер: тики прогрева судить нельзя — там ничего не
    # происходит, и они только разбавили бы статистику пачки.
    split = started = time.monotonic()
    try:
        for _ in range(spawns):
            await backend.aspawn_background("true")
    finally:
        batch = time.monotonic() - started
        stop.set()
        await beat

    import statistics

    during = [g for ts, g in stamped if ts > split]
    base = [g for ts, g in stamped if ts <= split]
    detail = (
        f"{spawns} фоновых запусков за {batch * 1000:.0f} мс: "
        f"тиков во время пачки {len(during)}, "
        f"худший разрыв {max(during) * 1000:.1f} мс"
        if during else
        f"{spawns} фоновых запусков за {batch * 1000:.0f} мс: НИ ОДНОГО тика"
    )

    assert base, "пульс не тикнул даже на прогреве — замер не состоялся"
    # Главная проверка: цикл отдавался НЕ ОДИН раз. Здоровый режим даёт 5,
    # сломанный ровно 1; порог 3 оставляет запас в обе стороны.
    assert len(during) >= 3, f"loop заблокирован пачкой целиком: {detail}"
    # Страховка, не зависящая от числа тиков: ни один разрыв не съел пачку
    # целиком. В сломанном режиме единственный разрыв ≈ вся пачка (отношение
    # около 1,0), в здоровом — 0,3–0,45.
    assert max(during) < batch * 0.6, f"один разрыв съел почти всю пачку: {detail}"
    # Медиана по всей выборке: ловит случай «loop занят ПОСТОЯННО», когда тики
    # есть, но редкие.
    assert statistics.median(base) < tick * 1.5, (
        f"loop занят ПОСТОЯННО ещё до пачки: медиана прогрева "
        f"{statistics.median(base) * 1000:.1f} мс при шаге {tick * 1000:.0f} мс"
    )


async def test_cancelled_launch_does_not_lose_a_started_process():
    """Отмена фонового запуска не оставляет процесс, о котором вызывающий не узнал.

    Уступка loop стояла ПОСЛЕ `Popen`: отмена ровно на ней бросала
    `CancelledError`, когда процесс уже жил, — pid не доходил до модели, и она не
    могла ни найти процесс, ни остановить его. Уступка ДО запуска делает отмену
    на этой точке честной: процесса ещё нет.
    """
    from gc_core.backends.local import _BACKGROUND_PROCS

    before = set(_BACKGROUND_PROCS)
    task = asyncio.ensure_future(
        LocalFsBackend().aspawn_background(f"{shlex.quote(sys.executable)} -c pass")
    )
    await asyncio.sleep(0)          # задача дошла до своей уступки и стоит на ней
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert not (set(_BACKGROUND_PROCS) - before), (
        "отмена пришла, когда процесс уже был запущен: его pid потерян"
    )


async def test_finished_background_children_leave_the_registry(tmp_path):
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

    await backend.aspawn_background(_child_command(tmp_path / "ticks.log"))
    assert not (short_lived & _BACKGROUND_PROCS), (
        "вышедшие процессы остались в учёте — уборка не работает"
    )


def test_background_child_survives_the_whole_agent_process(tmp_path):
    """Главное свойство: внук переживает ВЫХОД процесса агента.

    Это случай gc-cli и трайла Harbor — агент ответил и завершился целиком. Ни
    один поток-читатель тут не помог бы: его убивает вместе с процессом. У
    потомка же ни одной трубы к нам — его потоки смотрят в нулевое устройство, и
    ему безразлично, что нас больше нет.
    """
    ticks = tmp_path / "ticks.log"
    agent = (
        "import asyncio, shlex, sys\n"
        f"sys.path.insert(0, {str(_SRC_ROOT)!r})\n"
        "from gc_core.backends.local import LocalFsBackend\n"
        f"child = {_CHILD!r}\n"
        f"ticks = {str(ticks)!r}\n"
        "async def turn():\n"
        "    await LocalFsBackend().aspawn_background(\n"
        "        f'{shlex.quote(sys.executable)} -c {shlex.quote(child)} {shlex.quote(ticks)}',\n"
        "    )\n"
        "asyncio.run(turn())\n"
    )

    done = subprocess.run([sys.executable, "-c", agent], capture_output=True, timeout=60)
    assert done.returncode == 0, done.stderr.decode()[-400:]

    # На момент выхода агента потомок заведомо НЕ успел отработать.
    early = ticks.read_text(encoding="utf-8").count("tick") if ticks.exists() else 0
    assert early < CHILD_TICKS, (
        f"потомок успел всё ({early}) до выхода агента — тест ничего не проверяет"
    )

    deadline = time.monotonic() + 15
    text = ""
    while time.monotonic() < deadline:
        text = ticks.read_text(encoding="utf-8") if ticks.exists() else ""
        if text.count("tick") >= CHILD_TICKS:
            break
        time.sleep(0.1)

    assert text.count("tick") == CHILD_TICKS, (
        f"дописано {text.count('tick')} из {CHILD_TICKS}: фоновый процесс не "
        f"пережил выход агента"
    )
