"""Автозапуск MCP-daemon'ов по конфигу агента и манифесту recommended_mcp."""

from __future__ import annotations

import socket
import sys
import time
from typing import Any
from urllib.parse import urlparse

from recommended_mcp.manifest import load_manifest
from recommended_mcp.process_supervisor import RunSupervisor, SupervisorError
from recommended_mcp.run import run_entry
from recommended_mcp.runtime.manifest_resolve import recipe_root, resolve_agent_manifest_path

# Транспорты, при которых агент ходит к уже запущенному серверу (не stdio-spawn).
_DAEMON_TRANSPORTS = frozenset({"http", "sse", "stream_http", "stream_sse"})

_DEFAULT_READY_TIMEOUT_SEC = 15.0
_READY_POLL_SEC = 0.05


def _daemon_endpoint(url: str) -> tuple[str, int]:
    parsed = urlparse(url)
    host = parsed.hostname or "127.0.0.1"
    if parsed.port is not None:
        return host, parsed.port
    if parsed.scheme == "https":
        return host, 443
    return host, 80


def wait_daemon_url_ready(
    url: str,
    *,
    timeout: float = _DEFAULT_READY_TIMEOUT_SEC,
    poll: float = _READY_POLL_SEC,
) -> bool:
    """Дождаться, пока TCP-порт MCP-daemon'а начнёт принимать соединения."""
    host, port = _daemon_endpoint(url)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(poll)
    return False


def _warn(text: str, *, code: str, highlights: tuple[str, ...] = ()) -> None:
    """Предупреждение автозапуска — в stderr И в канал уведомлений ядра.

    Одного stderr мало: `gc-ua` зовёт автозапуск непосредственно перед
    `app.run()`, а тот переключает терминал в alternate screen. Подсказка
    «`gc-mcp install <рецепт>`» мелькала долю секунды и уезжала под экран TUI —
    человек видел в `/notices` только следствие («нет соединения с
    http://127.0.0.1:…») без единого слова о том, что делать. Смысл правки, из-за
    которой предупреждение вообще появилось (сбой автозапуска не должен ронять
    фронтенд), от этого терялся: фронтенд поднимался, а диагностика — нет.

    В `gc-cli` alternate screen нет, поэтому stderr остаётся: там он и есть
    основной канал. Уведомление ядра его не заменяет, а дополняет.

    Импорт локальный: `gc_core` — зависимость gc-mcp, но модуль зовут и из
    контекстов без TUI; тянуть его на импорте пакета незачем. Сбой канала
    уведомлений не должен превращаться в падение автозапуска — ради этого весь
    этот код и написан.
    """
    print(f"[warn] {text}", file=sys.stderr)
    try:
        from gc_core.runtime_notices import push_runtime_notice
        from gc_core.startup_notices import Notice

        push_runtime_notice(Notice("warning", code, text, highlights))
    except Exception:  # канал уведомлений не обязателен
        pass


#: Что советовать, если фронтенд не назвал своего способа выключить автозапуск.
#: Нейтрально по формулировке: имени флага тут знать неоткуда.
_DEFAULT_DISABLE_HINT = "отключить автозапуск MCP — см. справку вашей команды"


def ensure_daemons_for_agent(
    config_dict: dict[str, Any],
    *,
    manifest_path=None,
    disable_hint: str = _DEFAULT_DISABLE_HINT,
) -> RunSupervisor:
    """
    Поднять command-daemon'ы из манифеста, если они нужны включённым MCP в config агента.

    Идемпотентно: уже живые процессы не дублируются.
    Возвращает супервизор — в shutdown_managed() гасятся только процессы,
    запущенные в этом вызове.
    """
    path = manifest_path or resolve_agent_manifest_path(config_dict)
    if path is None:
        return RunSupervisor()

    recommended_root = recipe_root()
    manifest = load_manifest(path)
    servers = config_dict.get("mcp-servers") or {}
    supervisor = RunSupervisor()

    for entry in manifest:
        if entry.name not in servers:
            continue
        if entry.run is None or entry.run.type != "command":
            continue
        server_cfg = servers[entry.name]
        transport = str(server_cfg.get("transport", "")).strip()
        if transport not in _DAEMON_TRANSPORTS:
            continue
        try:
            started = run_entry(entry, recommended_root, supervisor)
        except SupervisorError as exc:
            # Рецепт ПОДКЛЮЧЁН, но не УСТАНОВЛЕН — обычное состояние: сервер
            # деинсталлировали, снесли venv рецептов, перенесли конфиг на другую
            # машину. Проводка при этом остаётся в config.local.yml, потому что
            # это две независимые операции.
            #
            # Ронять на этом весь фронтенд нельзя. В TUI автозапуск включён по
            # умолчанию (`set_defaults(auto_mcp=True)`), поэтому непоставленный
            # daemon-рецепт убивал `gc-ua` ДО первого экрана — трейсбеком
            # FileNotFoundError, из которого не читается ни имя рецепта, ни что
            # с этим делать. Остальные серверы при этом исправны и должны
            # подняться.
            # Имя флага приходит от фронтенда: модуль общий, а способ
            # выключить автозапуск у команд РАЗНЫЙ. `--no-auto-mcp` есть только
            # у gc-ua (в TUI автозапуск включён по умолчанию); у gc-cli он
            # opt-in через `-a`, и этого флага там нет вовсе — argparse отдал бы
            # его в apply_cli_overrides как перекрытие неизвестного ключа
            # конфига, а автозапуск остался бы включён.
            text = (
                f"{entry.name}: демон не запущен — {exc}. "
                f"Рецепт подключён, но не установлен: `gc-mcp install {entry.name}`. "
                f"{disable_hint}."
            )
            _warn(text, code="mcp_autostart_recipe", highlights=(entry.name,))
            continue
        if not started:
            continue
        url = server_cfg.get("url")
        if not url:
            continue
        print(f"[wait] {entry.name}: {url}")
        if wait_daemon_url_ready(str(url)):
            print(f"[ready] {entry.name}: {url}")
        else:
            print(
                f"[warn] {entry.name}: daemon not accepting connections at {url} "
                f"after {_DEFAULT_READY_TIMEOUT_SEC:.0f}s"
            )

    return supervisor
