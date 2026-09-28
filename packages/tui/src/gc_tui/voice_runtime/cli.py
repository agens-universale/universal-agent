"""gc-voice — CLI voice-батарейки: install / status / stop / serve / remove."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional, Sequence

from recommended_mcp.process_supervisor import (
    build_command_argv,
    is_pid_alive,
    read_pid,
    remove_pid,
    terminate_pid,
    write_pid,
)

from gc_tui.voice_runtime.fs import force_rmtree
from gc_tui.voice_runtime.install import VoiceInstallError, install
from gc_tui.voice_runtime.leases import clear_agent_owned, live_leases
from gc_tui.voice_runtime.paths import voice_pids_dir
from gc_tui.voice_runtime.service import build_run_spec, port_alive
from gc_tui.voice_runtime.state import (
    DEFAULT_MODEL,
    DEFAULT_PORT,
    available_update,
    clear_state,
    load_state,
)
from gc_tui.voice_runtime.wire import (
    base_config_raw,
    managed_voice,
    unwire_voice,
)

SERVICE_NAME = "speaches"


def _tolerant_console() -> None:
    """Не-UTF8 консоль (cp1251/cp866 на русской Windows): символы вне кодировки
    (например '→') не должны ронять CLI — печатаем с заменой."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")


def _cmd_install(args) -> int:
    try:
        install(port=args.port, model=args.model, base_raw=base_config_raw())
    except VoiceInstallError as exc:
        print(f"[voice] ошибка установки: {exc}", file=sys.stderr)
        return 1
    return 0


def _cmd_status() -> int:
    state = load_state()
    if state is None:
        print("[voice] не установлен. Установка: gc-voice install")
    else:
        # Устаревание называем прямо здесь: раньше status печатал версию, но ни с чем её не
        # сравнивал — узнать, что движок отстал, было неоткуда, кроме экрана после апгрейда.
        upd = available_update(state)
        print(
            f"[voice] установлен: speaches {state.version} → {state.root}"
            + (f"  · доступно {upd}" if upd else "")
        )
        if upd:
            print(
                "[voice] обновить: /settings → Голосовой ввод → Ctrl+U (или gc-voice install)"
            )
        print(f"[voice] порт {state.port}, модель {state.model}")
        pid = read_pid(SERVICE_NAME, voice_pids_dir())
        running = pid is not None and is_pid_alive(pid)
        if running:
            print(
                f"[voice] сервис: жив (pid {pid}){', порт отвечает' if port_alive(state) else ''}"
            )
        elif port_alive(state):
            # pid-файла нет, а порт занят: сервис подняли снаружи (gc-voice serve в другом
            # окне) либо его pid-файл потерян. Сказать «не запущен» — соврать в глаза.
            print(
                f"[voice] сервис: порт {state.port} отвечает, но pid-файла нет — "
                "поднят снаружи; остановите его там, где запускали (Ctrl+C)"
            )
        else:
            print("[voice] сервис: не запущен")
        clients = live_leases()
        print(f"[voice] клиенты (lease): {clients if clients else 'нет'}")
        wired = managed_voice()
        print(
            f"[voice] конфиг (managed): {wired['url'] if wired else 'не прописан'}"
        )
    return 0


def _stop_service() -> bool:
    pid = read_pid(SERVICE_NAME, voice_pids_dir())
    if pid is None or not is_pid_alive(pid):
        return False
    terminate_pid(pid)
    remove_pid(SERVICE_NAME, voice_pids_dir())
    clear_agent_owned(
        SERVICE_NAME
    )  # погашен — пометке владельца не о чем лежать
    return True


def _cmd_stop() -> int:
    if _stop_service():
        print("[voice] stopped")
        return 0
    state = load_state()
    if state is not None and port_alive(state):
        # Погасить нечем: pid неизвестен, а искать процесс по порту значит разбирать
        # вывод netstat/lsof на трёх системах. Честнее сказать, где его выключить.
        print(
            f"[voice] порт {state.port} отвечает, но pid-файла нет — сервис поднят "
            "снаружи; остановите его там, где запускали (Ctrl+C)"
        )
        return 1
    print("[voice] not running")
    return 0


def _cmd_serve() -> int:
    state = load_state()
    if state is None:
        print(
            "[voice] не установлен. Установка: gc-voice install",
            file=sys.stderr,
        )
        return 1
    spec = build_run_spec(state)
    argv = build_command_argv(spec)
    env_pairs = dict(spec.env)

    env = {**os.environ, **env_pairs}
    print(f"[voice] serve (foreground): {' '.join(argv)}")
    # pid-файл пишем и здесь: без него stop/status/remove слепы к этому процессу —
    # командой его не погасить, а на Windows он держит gc-voice.exe и ломает
    # обновление агента. Чужой (живой) pid-файл не трогаем: он принадлежит сервису,
    # который уже работает, и снести его значит сделать ТОТ невидимым для stop.
    owned = _claim_pid_file()
    try:
        return subprocess.run(argv, env=env, cwd=state.root).returncode
    finally:
        if owned:
            _release_pid_file()


def _claim_pid_file() -> bool:
    """Занять pid-файл сервиса своим PID. False — файл принадлежит живому процессу."""
    pid = read_pid(SERVICE_NAME, voice_pids_dir())
    if pid is not None and is_pid_alive(pid):
        return False
    write_pid(SERVICE_NAME, os.getpid(), voice_pids_dir())
    # Сервис теперь наш, человеческий: снимаем пометку «подняли агенты», если она
    # осталась от прошлой (уже мёртвой) агентской жизни сервиса. Без этого следующий
    # выходящий TUI прочитал бы чужую пометку и погасил бы этот форграунд.
    clear_agent_owned(SERVICE_NAME)
    return True


def _release_pid_file() -> None:
    """Снять pid-файл, если в нём всё ещё наш PID (чужой мог перезаписать)."""
    if read_pid(SERVICE_NAME, voice_pids_dir()) == os.getpid():
        remove_pid(SERVICE_NAME, voice_pids_dir())


def _cmd_remove() -> int:
    state = load_state()
    if state is None:
        print("[voice] не установлен — нечего удалять.")
        return 0
    if _stop_service():
        print("[voice] сервис остановлен")
    if unwire_voice():
        print("[voice] конфиг отключён (config.local.yml)")
    clear_state()
    root = Path(state.root)
    if root.is_dir():
        # force_rmtree: снимает read-only с git-объектов (иначе на Windows
        # rmtree молча оставляет огрызок). Не удалилось — файлы держит процесс
        # (например, форграунд serve: он без pid-файла, _stop_service его не видит).
        if not force_rmtree(root):
            print(
                f"[voice] окружение {root} удалено НЕ полностью — файлы заняты. "
                "Остановите сервис (gc-voice stop; форграунд serve — Ctrl+C) "
                "и повторите gc-voice remove."
            )
            print("[voice] модели в HF-кэше не тронуты (общий кэш машины).")
            # Ненулевой код — единственное, по чему остаток видят снаружи: состояние уже
            # очищено, и «не установлен» скроет от человека занятое на диске место.
            return 1
        print(f"[voice] удалено окружение {root}")
    print("[voice] модели в HF-кэше не тронуты (общий кэш машины).")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    _tolerant_console()
    parser = argparse.ArgumentParser(
        prog="gc-voice", description="Локальный STT-сервис (батарейка)"
    )
    sub = parser.add_subparsers(dest="command")
    p_inst = sub.add_parser(
        "install", help="поставить speaches + модель + конфиг"
    )
    p_inst.add_argument("--model", default=DEFAULT_MODEL)
    p_inst.add_argument("--port", type=int, default=DEFAULT_PORT)
    sub.add_parser("status", help="состояние установки/сервиса/клиентов")
    sub.add_parser("stop", help="погасить сервис (lease игнорируются)")
    sub.add_parser("serve", help="запустить в форграунде (отладка)")
    sub.add_parser("remove", help="снести сервис: stop + unwire + удалить env")

    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 2)

    if args.command == "install":
        return _cmd_install(args)
    if args.command == "status":
        return _cmd_status()
    if args.command == "stop":
        return _cmd_stop()
    if args.command == "serve":
        return _cmd_serve()
    if args.command == "remove":
        return _cmd_remove()
    parser.print_help()
    return 2
