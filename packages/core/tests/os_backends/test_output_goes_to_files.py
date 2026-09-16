"""Вывод команды идёт в файлы, а не в наши трубы — и потому переживает нас.

Труба живёт, пока открыт читающий конец. В TUI каждый ход — свой `asyncio.run`,
и на выходе трубы закрывались: фоновый `npm run dev` умирал на первой же записи
в лог. В gc-cli и в трайле Harbor хуже — там завершается весь процесс агента, и
держать читателя некому в принципе, никакой поток не помог бы.

Файл этой связи не имеет: потомок и его внуки наследуют дескриптор файла, и им
безразлично, кто и когда закрылся. Вместе с переездом снят весь блок дренажа —
`_release_drain`, передача копии дескриптора потоку и платформенная развилка для
Windows.
"""
import ast
import os
import pathlib
import shlex
import subprocess
import sys
import time

from gc_core.backends import local as local_backend

CHILD_TICKS = 20


def _runner_node() -> ast.AsyncFunctionDef:
    tree = ast.parse(pathlib.Path(local_backend.__file__).read_text(encoding="utf-8"))
    return next(n for n in ast.walk(tree)
                if isinstance(n, ast.AsyncFunctionDef) and n.name == "_arun_bounded_process")


def test_stdout_and_stderr_are_files_not_pipes():
    """Структурно: запуск не просит у asyncio ни одной трубы под вывод."""
    node = _runner_node()

    pipes = [
        kw.lineno for call in ast.walk(node) if isinstance(call, ast.Call)
        for kw in call.keywords
        if kw.arg in ("stdout", "stderr")
        and getattr(kw.value, "attr", None) == "PIPE"
    ]
    assert not pipes, f"вывод снова уходит в трубу (строки {pipes})"

    opened = [n for n in ast.walk(node) if isinstance(n, ast.Call)
              and getattr(n.func, "id", None) == "_open_capture_file"]
    assert len(opened) == 2, "ожидались два файла захвата: stdout и stderr раздельно"


def test_capture_file_is_anonymous_on_posix(tmp_path):
    """На POSIX файл захвата не оставляет имени в файловой системе."""
    if os.name == "nt":
        import pytest

        pytest.skip("на Windows открытый файл удалить нельзя")

    write_fd, read_fd = local_backend._open_capture_file()
    try:
        os.write(write_fd, b"hello")
        assert os.read(read_fd, 64) == b"hello"
    finally:
        for fd in (write_fd, read_fd):
            os.close(fd)


def test_background_child_survives_the_whole_agent_process(tmp_path):
    """Главное свойство: внук переживает ВЫХОД процесса агента.

    Прежняя схема этого не умела ни на одной платформе: трубы закрывает ядро
    при выходе процесса, и дочитывать их некому. Это ровно сценарий gc-cli и
    трайла Harbor — агент ответил и завершился, а сервер должен остаться.
    """
    log = tmp_path / "child.log"
    child = (
        "import sys, time\n"
        f"with open({str(log)!r}, 'a') as p:\n"
        f"    for i in range({CHILD_TICKS}):\n"
        "        try:\n"
        "            print('tick', i, flush=True)\n"
        "            p.write(f'ok {i}\\n'); p.flush()\n"
        "        except BaseException as e:\n"
        "            p.write(f'FAIL {i} {type(e).__name__}\\n'); p.flush()\n"
        "            sys.exit(1)\n"
        "        time.sleep(0.1)\n"
    )
    cmd = f"{shlex.quote(sys.executable)} -c {shlex.quote(child)} &"

    agent = (
        "import asyncio, sys\n"
        # parents: backends → gc_core → src. Именно `src` кладём на путь, иначе
        # подпроцесс не найдёт ядро.
        "sys.path.insert(0, %r)\n" % str(pathlib.Path(local_backend.__file__).parents[2])
        + "from gc_core.backends.local import LocalFsBackend\n"
        f"res = asyncio.run(LocalFsBackend().aexec({cmd!r}, timeout=10))\n"
        "assert res.return_code == 0, res.return_code\n"
    )
    # Процесс агента отрабатывает ход и ЗАВЕРШАЕТСЯ целиком.
    done = subprocess.run([sys.executable, "-c", agent], capture_output=True, timeout=60)
    assert done.returncode == 0, done.stderr.decode()[-400:]

    deadline = time.monotonic() + 15
    text = ""
    while time.monotonic() < deadline:
        text = log.read_text(encoding="utf-8") if log.exists() else ""
        if "FAIL" in text or text.count("ok ") >= CHILD_TICKS:
            break
        time.sleep(0.1)

    assert "FAIL" not in text, (
        f"фоновый процесс умер после выхода агента: {text.splitlines()[-1]}"
    )
    assert text.count("ok ") == CHILD_TICKS, f"дописано {text.count('ok ')} из {CHILD_TICKS}"
