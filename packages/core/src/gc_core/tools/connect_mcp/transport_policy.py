"""Какой транспорт использовать на самом деле, и что об этом сказать.

Раньше выбор дублировался в двух местах — ``api.call_mcp_tool`` и
``registry._discover_one``, — и правило ``persistent_session`` приходилось
держать синхронно в обоих. Теперь оно одно.

Здесь же живёт депрекейт ``sse``.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

_logger = logging.getLogger(__name__)

#: Уже предупредили про этот сервер — не шумим на каждый вызов тула.
_warned: set[str] = set()

DEPRECATED_TRANSPORTS = {"sse": "stream_sse"}


def resolve_effective_transport(
    server_cfg: Dict[str, Any],
    *,
    server_name: str = "",
    logger: Any = None,
) -> str:
    """Транспорт, которым реально пойдёт запрос.

    Два правила, оба переписывают заявленный в конфиге:

    1. ``persistent_session: true`` — у легаси ``sse``/``http`` (one-shot через
       ``transport.py``) своего пула соединений нет, поэтому роутим через
       SDK-клиент, где живёт ``_PersistentSessionManager`` с длинной
       ``ClientSession``.

    2. ``sse`` ДЕПРЕКЕЙТНУТ и всегда роутится в ``stream_sse``. Своя
       реализация не соответствует спеке: она делает один POST на ``/sse`` и
       ждёт ответ там же, а по протоколу SSE-транспорт двухэндпойнтовый —
       ``GET /sse`` отдаёт ``event: endpoint`` с адресом ``/messages/?session_id=…``,
       туда уходит POST (ответ ``202 Accepted``), а сам JSON-RPC ответ
       приходит в открытый GET-поток. Одноразовым запросом это не выражается:
       нужна сессия с постоянным потоком — ровно то, что уже корректно делает
       SDK. Проверено на FastMCP: наш POST получает ``405 Method Not Allowed``.
    """
    transport = server_cfg.get("transport")

    replacement = DEPRECATED_TRANSPORTS.get(transport)
    if replacement:
        key = server_name or str(transport)
        if key not in _warned:
            _warned.add(key)
            (logger or _logger).warning(
                "mcp_transport_deprecated server=%s transport=%s → %s: своя "
                "реализация sse не соответствует спеке (POST на /sse вместо "
                "двухэндпойнтового рукопожатия) и не работает со "
                "спек-совместимыми серверами. Запрос отправлен через %s; "
                "поправьте transport в конфиге.",
                server_name or "?",
                transport,
                replacement,
                replacement,
                extra={
                    "event": "mcp_transport_deprecated",
                    "server": server_name,
                    "transport": transport,
                    "effective_transport": replacement,
                },
            )
        return replacement

    if server_cfg.get("persistent_session"):
        if transport == "sse":
            return "stream_sse"
        if transport == "http":
            return "stream_http"

    return transport


__all__ = ["DEPRECATED_TRANSPORTS", "resolve_effective_transport"]
