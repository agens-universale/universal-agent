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

from pathlib import Path

from recommended_mcp.process_supervisor import is_pid_alive

from voice_runtime.paths import voice_leases_dir, voice_pids_dir


def _lease_path(pid: int):
    return voice_leases_dir() / f"{pid}.lease"


def acquire(pid: int) -> None:
    path = _lease_path(pid)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(pid), encoding="utf-8")


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
        if alive(pid):
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
