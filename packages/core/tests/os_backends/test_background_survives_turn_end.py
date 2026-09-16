"""Фоновый процесс, поднятый агентом, переживает конец хода.

В TUI каждый ход — свой `asyncio.run`. Пока вывод шёл в трубы, их дочитывали
задачи на loop хода: на выходе loop отменял их, трубы закрывались, и потомок
(`npm run dev`, `uvicorn`, `python -m http.server`) умирал на следующей записи —
агент говорит «готово», а порт не отвечает.

Теперь долгоживущая команда запускается ОТДЕЛЬНЫМ режимом, и труб у неё нет
вовсе: вывод идёт в файл, открытый вызывающим. Поэтому конец хода ей ничем не
грозит — здесь это и проверяется, честным выходом из `asyncio.run` со сборкой
мусора после него.

Заметим, что прежняя редакция теста гоняла обычный `aexec` с `&` на конце. Это
поведение мы сознательно больше не обещаем: у команды переднего плана труба
закрывается вместе с вызовом. Свойство целиком переехало сюда, в фоновый режим.
"""
import asyncio
import gc
import shlex
import sys
import time

from gc_core.backends.local import LocalFsBackend


def test_background_writer_survives_the_end_of_the_turn(tmp_path):
    log = tmp_path / "child.log"
    child = (
        "import sys, time\n"
        "for i in range(20):\n"
        "    print('tick', i, flush=True)\n"
        "    time.sleep(0.1)\n"
    )
    cmd = f"{shlex.quote(sys.executable)} -c {shlex.quote(child)}"

    async def turn():
        import os

        fd = os.open(str(log), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            started = await LocalFsBackend().aspawn_background(cmd, log_fd=fd)
        finally:
            os.close(fd)
        assert started["pid"] > 0

    asyncio.run(turn())        # конец хода: loop закрыт, его задачи отменены
    gc.collect()               # транспорты хода собраны

    early = log.read_text(encoding="utf-8").count("tick") if log.exists() else 0
    assert early < 20, f"потомок успел всё ({early}) внутри хода — тест холостой"

    deadline = time.monotonic() + 15
    text = ""
    while time.monotonic() < deadline:
        text = log.read_text(encoding="utf-8") if log.exists() else ""
        if text.count("tick") >= 20:
            break
        time.sleep(0.1)
    assert text.count("tick") == 20, (
        f"дописано {text.count('tick')} из 20: процесс не пережил конец хода"
    )
