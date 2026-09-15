"""Фоновый процесс, поднятый агентом, переживает конец хода.

В TUI каждый ход — свой `asyncio.run`. Трубы фонового потомка дочитывали
задачи на loop хода: на выходе loop отменял их, трубы закрывались, и потомок
(`npm run dev`, `uvicorn`, `python -m http.server`) умирал на следующей записи
в лог — агент говорит «готово», а порт не отвечает. На develop трубы дочитывали
daemon-потоки, жившие весь процесс.

Здесь ход честно заканчивается выходом из `asyncio.run`, мусор собирается, а
потомок обязан дописать до конца. Окно дочитывания хвоста сжато до 0,2 с, иначе
быстрый потомок успевал бы выписать всё внутри тула и путь передачи потоку не
исполнялся бы вовсе — ровно так холостыми бывают сторожа дренажа.
"""
import asyncio
import gc
import shlex
import sys
import time

from gc_core.backends.local import LocalFsBackend


def test_background_writer_survives_the_end_of_the_turn(tmp_path, monkeypatch):
    monkeypatch.setitem(LocalFsBackend.aexec.__globals__, "_DRAIN_JOIN_TIMEOUT_SEC", 0.2)
    log = tmp_path / "child.log"
    child = (
        "import sys, time\n"
        f"with open({str(log)!r}, 'a') as p:\n"
        "    for i in range(20):\n"
        "        try:\n"
        "            print('tick', i, flush=True)\n"
        "            p.write(f'ok {i}\\n'); p.flush()\n"
        "        except BaseException as e:\n"
        "            p.write(f'FAIL {i} {type(e).__name__}\\n'); p.flush()\n"
        "            sys.exit(1)\n"
        "        time.sleep(0.1)\n"
    )
    cmd = f"{shlex.quote(sys.executable)} -c {shlex.quote(child)} &"

    async def turn():
        res = await LocalFsBackend().aexec(cmd, timeout=10)
        assert res.return_code == 0
        await asyncio.sleep(0.3)

    asyncio.run(turn())        # конец хода: loop закрыт, его задачи отменены
    gc.collect()               # транспорты хода собраны — их трубы закрыты

    deadline = time.monotonic() + 10
    text = ""
    while time.monotonic() < deadline:
        text = log.read_text(encoding="utf-8") if log.exists() else ""
        if "FAIL" in text or text.count("ok ") >= 20:
            break
        time.sleep(0.1)
    assert "FAIL" not in text, (
        f"фоновый процесс умер после конца хода: {text.splitlines()[-1]}"
    )
    assert text.count("ok ") == 20, f"дописано {text.count('ok ')} из 20"
