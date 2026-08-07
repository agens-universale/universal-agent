"""Автостарт voice-сервиса из TUI: гейт по state+конфигу, light-switch, прогрев."""

from __future__ import annotations

import os
import threading
from typing import Any
from urllib.parse import urlsplit

from recommended_mcp.autostart import wait_daemon_url_ready
from recommended_mcp.process_supervisor import (
    RunSupervisor, is_pid_alive, read_pid, remove_pid, terminate_pid,
)

from voice_runtime.leases import (
    acquire, clear_agent_owned, is_agent_owned, mark_agent_owned, release,
)
from voice_runtime.paths import voice_pids_dir
from voice_runtime.service import build_run_spec, port_alive, preheat, service_url
from voice_runtime.state import load_state

SERVICE_NAME = "speaches"

# Дефолт `wait_daemon_url_ready` (15 с) рассчитан на MCP-демоны — те стартуют мгновенно.
# Холодный старт speaches другой: `uv run` поднимает окружение и импортирует
# ctranslate2/faster-whisper, на Windows это суммарно укладывается не всегда даже в
# минуту (поймано живой приёмкой — вердикт «не запустился» пришёл раньше, чем сервис
# успел ответить). Тёплый повтор (порт уже слушает) в это ожидание вообще не попадает —
# ждём только на первом холодном подъёме. Берём запас 90 с.
_VOICE_READY_TIMEOUT_SEC = 90.0


def url_is_ours(config_dict: dict[str, Any], port: int) -> bool:
    """Публичный гейт автостарта: voice.url в конфиге указывает на наш локальный
    сервис. Единственный владелец этой проверки — использовать и из TUI.

    Адрес РАЗБИРАЕТСЯ, а не сканируется на подстроки. Поиск `":{port}/"` плюс
    `"localhost" in url` промахивался в обе стороны: адрес без пути и IPv6-форма
    считались чужими (автостарт молчал, а /settings показывал «облако» для своей же
    машины), а `http://localhost.чужой-домен.com:8735/…` — своим, и агент брался
    поднимать локальный сервис под удалённый хост."""
    url = str((config_dict.get("voice") or {}).get("url") or "")
    # Лениво: `net` тянет requests, а autostart импортируется на старте TUI.
    from voice_runtime.net import is_loopback

    if not is_loopback(url):
        return False
    try:
        return urlsplit(url).port == port
    except ValueError:          # мусор вместо номера порта — точно не наш адрес
        return False


def _preheat_bg(state) -> None:
    threading.Thread(target=preheat, args=(state,), daemon=True).start()


def ensure_voice_service(config_dict: dict[str, Any], *,
                         supervisor_factory=RunSupervisor,
                         preheat_async: bool = True) -> bool:
    """Поднять локальный voice-сервис, если он установлен и сконфигурирован.

    True — сервис доступен (жил или поднят), lease взят, прогрев запущен.
    False — гейт не прошёл (не наша конфигурация) или подъём не удался."""
    state = load_state()
    if state is None or not url_is_ours(config_dict, state.port):
        return False
    if not port_alive(state):
        supervisor = supervisor_factory(pids_dir=voice_pids_dir())
        spec = build_run_spec(state)
        if supervisor.start_if_needed(SERVICE_NAME, spec, cwd=spec.cwd):
            # Спавнили МЫ (True — «стартовали сейчас») → сервис наш, гасить его можно.
            # Пометка идёт ДО ожидания готовности: «опоздавший» сервис, поднявшийся уже
            # после таймаута, тоже наш, и прибрать его на выходе обязано быть можно.
            # False здесь — порт молчит, но pid-файл принадлежит живому процессу: это
            # чужой (форграундный `gc-voice serve`) сервис на взлёте, и он не наш.
            mark_agent_owned(SERVICE_NAME)
        if not wait_daemon_url_ready(service_url(state), timeout=_VOICE_READY_TIMEOUT_SEC):
            # Таймаут — не то же самое, что «не поднялся»: возможна гонка, когда порт
            # ожил секундой позже последнего опроса. Перепроверяем факт перед вердиктом,
            # иначе плашка соврёт про реально живой сервис (поймано живой приёмкой:
            # строка режима — по факту — уже показывала «запущен», а плашка — «не
            # запустился»).
            if not port_alive(state):
                return False
    acquire(os.getpid())
    if preheat_async:
        _preheat_bg(state)
    else:
        preheat(state)
    return True


def release_voice_service() -> None:
    """Снять свой lease; последний живой клиент гасит сервис — но только СВОЙ.

    Гейт по пометке владельца обязателен, а не «на всякий случай»: этот вызов идёт
    из `finally` в tui/__init__ БЕЗУСЛОВНО, в том числе у TUI, который голоса не
    касался и lease не клал. `release` у такого возвращает True («живых клиентов
    нет») — и без проверки он убил бы форграундный `gc-voice serve` человека,
    запущенный в соседнем окне. Пометки нет → сервис не наш → уходим молча."""
    if not release(os.getpid()):
        return
    if not is_agent_owned(SERVICE_NAME):
        return
    pid = read_pid(SERVICE_NAME, voice_pids_dir())
    if pid is not None and is_pid_alive(pid):
        terminate_pid(pid)
        remove_pid(SERVICE_NAME, voice_pids_dir())
    clear_agent_owned(SERVICE_NAME)
