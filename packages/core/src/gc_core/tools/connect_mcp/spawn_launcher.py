"""Пускатель процесса MCP-сервера: запись в журнал, затем ``exec`` команды.

Запуск: ``python -I -S spawn_launcher.py <каталог журнала> <ключ сервера>
-- <команда> [аргументы…]``. ``-I`` отрезает ``PYTHONPATH`` и
``PYTHONHOME`` из окружения сервера и каталог скрипта из ``sys.path``,
``-S`` — ``site`` (``.pth`` окружения не исполняются на каждом запуске
сервера); поэтому файл живёт на одной стандартной библиотеке, без
импортов ``gc_core``.

Пускатель пишет ``<каталог>/<pid>.json`` = ``{"pid", "pgid", "mark",
"server"}`` (файл ``0600``, временный файл и переименование: читатель не
видит недописанного) и заменяет себя командой — pid при этом тот же.
Окружение и аргументы в файл не попадают: в них бывают секреты. Не
удалось записать файл — сервер всё равно запускается, только без учёта.
Сбой ``exec`` дописывается в файл (``error``: ``command_not_found`` или
``exec_failed``, ``detail`` — текст ОС без путей), код выхода 127.

stdin и stdout — протокол MCP: пускатель в них ничего не пишет и не
читает. SIGPIPE и SIGXFSZ, которые интерпретатор при старте ставит в
«игнорировать», перед ``exec`` возвращаются к действию по умолчанию —
сервер получает их таким же, как при прямом запуске.

Метку личности пускатель читает сам, тем же способом, что
``gc_core.utils.process_identity.process_start_mark``: значения обязаны
совпадать.
"""

import json
import os
import signal
import subprocess
import sys

_EXIT_EXEC_FAILED = 127
_EXIT_USAGE = 2


def _start_mark() -> str | None:
    try:
        if sys.platform.startswith("linux"):
            with open("/proc/self/stat", encoding="utf-8") as fh:
                text = fh.read()
            return text[text.rindex(")") + 1 :].split()[19]
        argv = ["/bin/ps", "-o", "lstart=", "-p", str(os.getpid())]
        out = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=5,
            env={**os.environ, "LC_ALL": "C"},
        ).stdout
        return " ".join(out.split()) or None
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return None


def _write_record(directory: str, record: dict) -> None:
    path = os.path.join(directory, f"{record['pid']}.json")
    staged = os.path.join(directory, f".{record['pid']}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    try:
        with os.fdopen(
            os.open(staged, flags, 0o600), "w", encoding="utf-8"
        ) as fh:
            json.dump(record, fh)
        os.replace(staged, path)
    except OSError:
        pass


def _restore_signals() -> None:
    for name in ("SIGPIPE", "SIGXFSZ"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), signal.SIG_DFL)


def main(argv: list[str]) -> int:
    if len(argv) < 5 or argv[3] != "--":
        sys.stderr.write("usage: <journal dir> <server key> -- <command>\n")
        return _EXIT_USAGE
    directory, server, command, args = argv[1], argv[2], argv[4], argv[5:]
    record = {
        "pid": os.getpid(),
        "pgid": os.getpgid(0),
        "mark": _start_mark(),
        "server": server,
    }
    _write_record(directory, record)
    _restore_signals()
    try:
        os.execvp(command, [command, *args])
    except OSError as exc:
        missing = isinstance(exc, FileNotFoundError)
        record["error"] = "command_not_found" if missing else "exec_failed"
        record["detail"] = exc.strerror or type(exc).__name__
        _write_record(directory, record)
    return _EXIT_EXEC_FAILED


if __name__ == "__main__":
    sys.exit(main(sys.argv))
