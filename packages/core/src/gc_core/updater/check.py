"""Проверка обновлений: git ls-remote против установленного коммита (A2).
Чистый факт; любая ошибка/таймаут → 'unknown' (никогда не блокирует старт)."""
from __future__ import annotations

import subprocess
from typing import Callable, NamedTuple, Optional

from gc_core.updater.source import UpdateSource

_LS_TIMEOUT = 8


class CheckResult(NamedTuple):
    status: str                 # "update" | "current" | "unknown"
    remote_commit: Optional[str]
    detail: str


def _git_ls_remote(url: str, ref: str) -> Optional[str]:
    from gc_core.utils.win_console import preserve_console_mode
    try:
        with preserve_console_mode():   # subprocess на Windows сбрасывает режим
            out = subprocess.run(       # консоли — иначе Ctrl+C после проверки
                ["git", "ls-remote", "--exit-code", url, ref],  # обновлений рвёт приложение
                capture_output=True, text=True, timeout=_LS_TIMEOUT, check=False,
            )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0 or not out.stdout.strip():
        return None
    return out.stdout.split()[0] or None


def check_for_update(
    source: UpdateSource,
    *,
    ls_remote: Callable[[str, str], Optional[str]] = _git_ls_remote,
) -> CheckResult:
    ref = source.ref or "HEAD"
    remote = ls_remote(source.url, ref)
    if remote is None:
        return CheckResult("unknown", None, "не удалось связаться с источником")
    if source.commit and remote == source.commit:
        return CheckResult("current", remote, "установлена последняя версия")
    return CheckResult("update", remote, "доступна новая версия")



def record_check_result(now: float, remote_commit, installed_commit=None, path=None) -> None:
    """Стамп времени (троттл) + последний известный remote-коммит + установленный НА
    МОМЕНТ проверки (для валидности вердикта). remote/installed пишутся, только если
    remote известен — при unknown/ошибке (None) прошлый вердикт сохраняется (транзиентная
    сеть не гасит «доступна новая версия»)."""
    from gc_core import upgrade_gate
    updates = {"last_update_check_ts": now}
    if remote_commit:
        updates["last_update_remote_commit"] = remote_commit
        updates["last_update_checked_installed"] = installed_commit
    upgrade_gate.write_state(updates, path)


def update_available_from_state(state: dict, installed_commit) -> bool:
    """Персистентный вердикт: доступно ли обновление по СОХРАНЁННОМУ результату последней
    проверки. Валиден ТОЛЬКО пока install не сменился с момента проверки
    (`last_update_checked_installed == текущий`) — иначе (ты обновился) старый remote
    может быть ПОЗАДИ установленного, и `remote != installed` ложно бы показывал
    «доступно»; в этом случае считаем «нет», пока свежий чек не переоценит. Оффлайн
    git-родословная (впереди/позади) не нужна. Оценивается на каждом старте — уведомление
    держится до обновления, а не только в день чека."""
    remote = state.get("last_update_remote_commit")
    checked = state.get("last_update_checked_installed")
    return (bool(remote) and bool(installed_commit)
            and checked == installed_commit and remote != installed_commit)


def _update_cfg(config: dict) -> dict:
    return (config or {}).get("update") or {}


def auto_check_enabled(config: dict) -> bool:
    # YAML `check: off` → bool False; трактуем «выкл» и для "off", и для False.
    return _update_cfg(config).get("check", "auto") not in ("off", False)


def extra_args(config: dict) -> list:
    val = _update_cfg(config).get("extra_args") or []
    return list(val) if isinstance(val, list) else []
