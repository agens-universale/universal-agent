"""Идентичность определения MCP-сервера: умолчания и отпечатки.

Чистые функции над словарём определения сервера без trace
(``plain_dump(model, by_alias=True)`` из ``effective_mcp_servers``).
Отпечаток — ключ общих записей серверов, каталога тулов на диске и
сессий: презентационные поля его не меняют, всё остальное — меняет.
"""

import hashlib
import json
from typing import Any

SESSION_KEY_FIELD = "_gc_session_key"
PROCESS_TRANSPORTS = frozenset({"stdio", "stream_stdio"})

# Поля, которые не влияют ни на соединение, ни на ответ tools/list:
# whitelist отбирает тулы из уже полученного списка.
_PRESENTATION_FIELDS = frozenset(
    {
        "description",
        "server-name",
        "server_name",
        "whitelist",
        "loop_control_tools",
        SESSION_KEY_FIELD,
    }
)
_KEY_ONLY_FIELDS = ("headers", "env")


def is_process_transport(server_cfg: dict[str, Any]) -> bool:
    return server_cfg.get("transport") in PROCESS_TRANSPORTS


def with_service_defaults(server_cfg: dict[str, Any]) -> dict[str, Any]:
    """Копия определения с раскрытым умолчанием ``persistent_session``.

    Не заданный флаг (``None``) — постоянная сессия у процессных
    транспортов и разовая у сетевых; явное значение не трогаем.
    """
    cfg = dict(server_cfg)
    if cfg.get("persistent_session") is None:
        cfg["persistent_session"] = is_process_transport(cfg)
    return cfg


def _digest(cfg: dict[str, Any]) -> str:
    payload = json.dumps(cfg, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def _connection_part(server_cfg: dict[str, Any]) -> dict[str, Any]:
    cfg = with_service_defaults(server_cfg)
    return {k: v for k, v in cfg.items() if k not in _PRESENTATION_FIELDS}


def connection_fingerprint(server_cfg: dict[str, Any]) -> str:
    """Всё, что меняет соединение с сервером, включая секреты."""
    return _digest(_connection_part(server_cfg))


def catalog_fingerprint(server_cfg: dict[str, Any]) -> str:
    """Отпечаток без секретов: годится в имя каталога тулов на диске.

    Смена значения ``token``, ``headers`` или ``env`` его не меняет;
    наличие токена и набор имён ключей — меняют.
    """
    cfg = _connection_part(server_cfg)
    if "token" in cfg:
        cfg["token"] = bool(cfg["token"])
    for field in _KEY_ONLY_FIELDS:
        if field in cfg:
            cfg[field] = sorted(cfg[field] or {})
    return _digest(cfg)


def session_key(server_key: str, fingerprint: str) -> str:
    return f"{server_key}@{fingerprint}"
