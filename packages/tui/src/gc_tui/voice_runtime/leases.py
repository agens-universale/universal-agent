"""Light-switch voice-сервиса: lease-файлы живых клиентов (TUI).

Каждый клиент кладёт <pid>.lease при старте и снимает при выходе; выходящий
вычищает мёртвые lease (клиент убит — файл остался) и, если живых не осталось,
получает True — «ты последний, выключай свет». Гонки не критичны: в худшем
случае сервис погаснет и поднимется заново следующим TUI (идемпотентно).

Второй признак того же выключателя — ЧЕЙ это свет. Гасить можно только сервис,
поднятый самими агентами: форграундный `gc-voice serve` человек запускает для
отладки (агент спавнит сервис с выводом в никуда, и это единственный способ
увидеть его логи), и переживать выход из TUI он обязан. Пометку ставит тот, кто
спавнил; её отсутствие значит «не наше» — при сомнении НЕ гасим: лишний живой
процесс дешевле убитого чужого.
"""

from __future__ import annotations

import os
from pathlib import Path

from recommended_mcp.process_supervisor import is_pid_alive

from gc_tui.voice_runtime.paths import voice_leases_dir, voice_pids_dir


def _lease_path(pid: int):
    return voice_leases_dir() / f"{pid}.lease"


def _posix_start_key(pid: int) -> str | None:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return None
    # `comm` стоит в скобках и может содержать пробелы — режем по ПОСЛЕДНЕЙ ')'. Дальше
    # поля идут с третьего (state), поэтому starttime (22-е) оказывается на индексе 19.
    tail = stat.rpartition(")")[2].split()
    return tail[19] if len(tail) > 19 else None


def _windows_start_key(pid: int) -> str | None:
    import ctypes
    from ctypes import wintypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    k32 = ctypes.windll.kernel32
    handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
        if not k32.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                   ctypes.byref(kernel), ctypes.byref(user)):
            return None
        return f"{created.dwHighDateTime}:{created.dwLowDateTime}"
    finally:
        k32.CloseHandle(handle)


def process_start_key(pid: int) -> str | None:
    """Отпечаток «это тот самый процесс» — время его старта. None — узнать не удалось.

    Номера PID система переиспользует, поэтому «процесс с таким номером жив» ничего не
    доказывает: после жёсткой смерти TUI (kill -9, падение, ребут) lease остаётся на
    диске, а номер рано или поздно достаётся постороннему процессу. Время старта
    отличает их надёжно и без новых зависимостей: на Linux — `starttime` из
    `/proc/<pid>/stat`, на Windows — `GetProcessTimes` через ctypes (psutil ради одной
    проверки не тянем; он и так лишь опциональная зависимость swarm)."""
    if os.name == "posix":
        return _posix_start_key(pid)
    try:
        return _windows_start_key(pid)
    except OSError:
        return None


def acquire(pid: int) -> None:
    path = _lease_path(pid)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Вторая строка — отпечаток процесса: по нему `live_leases` отличает наш TUI от
    # чужака, которому система выдала тот же номер.
    path.write_text(f"{pid}\n{process_start_key(pid) or ''}", encoding="utf-8")


def _lease_is_ours(entry: Path, pid: int) -> bool:
    """Файл написан ЭТИМ процессом, а не тёзкой по номеру.

    Отпечатка нет (lease от прежней версии агента либо система его не отдала) — верим
    как раньше: иначе обновление агента погасило бы сервис под живыми окнами."""
    try:
        saved = entry.read_text(encoding="utf-8").splitlines()
    except OSError:
        return True
    stamp = saved[1].strip() if len(saved) > 1 else ""
    if not stamp:
        return True
    current = process_start_key(pid)
    return current is None or current == stamp


def live_leases(*, alive=is_pid_alive) -> list[int]:
    """Живые клиенты; мёртвые lease-файлы попутно удаляются."""
    root = voice_leases_dir()
    if not root.is_dir():
        return []
    result: list[int] = []
    for entry in root.glob("*.lease"):
        try:
            pid = int(entry.stem)
        except ValueError:
            continue   # мусор — не наш файл, не трогаем содержимое каталога
        if alive(pid) and _lease_is_ours(entry, pid):
            result.append(pid)
        else:
            try:
                entry.unlink()
            except OSError:
                pass
    return result


def release(pid: int, *, alive=is_pid_alive) -> bool:
    """Снять свой lease. True — живых клиентов не осталось (гаси сервис)."""
    try:
        _lease_path(pid).unlink()
    except OSError:
        pass
    return not any(p != pid for p in live_leases(alive=alive))


def _owner_path(name: str) -> Path:
    """Пометка «сервис подняли агенты» — рядом с pid-файлом, к которому она и относится."""
    return voice_pids_dir() / f"{name}.agent-owned"


def mark_agent_owned(name: str) -> None:
    """Сервис спавнили мы: light-switch вправе его погасить."""
    path = _owner_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("agent", encoding="utf-8")


def clear_agent_owned(name: str) -> None:
    """Снять пометку. Зовётся не только при гашении, но и при ЗАХВАТЕ pid-файла
    человеком (`gc-voice serve`): пометка от прошлой, уже мёртвой агентской жизни
    сервиса иначе досталась бы форграунду в наследство — и следующий TUI убил бы
    его на своём выходе, полностью законно с точки зрения выключателя."""
    try:
        _owner_path(name).unlink()
    except OSError:
        pass


def is_agent_owned(name: str) -> bool:
    """Сервис под этим именем подняли агенты, а не человек."""
    return _owner_path(name).is_file()
