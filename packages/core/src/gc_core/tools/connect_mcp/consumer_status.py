"""Статус сервера у потребителя сервиса MCP (спека §5.5) и правило 30 с.

Чистый расчёт: на вход — запись сервера и снимок состояния потребителя по
этому серверу (сессия, идущая попытка, свой отказ, номер повтора) и часы
сервиса; на выход — статус. Снимок потребитель снимает под своим замком.
"""

from dataclasses import dataclass

from gc_core.tools.connect_mcp.service_records import Failure, ServerEntry

#: Правило 30 с: не раньше этого после последнего отказа — новая попытка.
ATTEMPT_INTERVAL_SEC = 30
STATE_CACHED = "cached"
STATE_CONNECTING = "connecting"
STATE_CONNECTED = "connected"
STATE_FAILED = "failed"
#: Причина своего отказа, когда попытку отменили (код ``other``).
INTERRUPTED_REASON = "подключение прервано"


@dataclass(frozen=True)
class ServerStatus:
    """Статус сервера у потребителя (спека §5.5).

    ``from_catalog`` — тулы взяты из записи, а не из своей сессии
    (``connecting`` с известным списком и ``cached``); ``retry`` — номер
    идущего повтора серии; ``tools`` — сырой список записи, у ``failed``
    пуст.
    """

    state: str
    reason_code: str = ""
    reason: str = ""
    retry: int = 0
    from_catalog: bool = False
    checked_at: float | None = None
    tools: tuple = ()


def _is_interrupted(failure: Failure) -> bool:
    return failure.code == "other" and failure.reason == INTERRUPTED_REASON


def shared_failure(entry: ServerEntry) -> Failure | None:
    """Последний отказ записи, если он позже её последней удачи.

    Удача другого потребителя после отказа значит, что сервер уже
    поднимается: такой отказ правило 30 с не держит.
    """
    failure = entry.last_failure
    if failure is None:
        return None
    if entry.checked_at is not None and failure.at <= entry.checked_at:
        return None
    return failure


def shared_hold_since(entry: ServerEntry) -> float | None:
    """С какого времени правило 30 с держит потребителей записи.

    Только у записи в отказе (отказ позже последней удачи): от более
    позднего из отказа и попытки, прерванной закрытием потребителя.
    Иначе отметка не держит: обрыв запроса здоровый сервер не блокирует,
    а запрос на потребителя у отказавшего сервера не запускает его каждый
    раз.
    """
    failure = shared_failure(entry)
    if failure is None:
        return None
    if entry.interrupted_at is None:
        return failure.at
    return max(failure.at, entry.interrupted_at)


def _failed(failure: Failure, entry: ServerEntry, retry: int = 0):
    return ServerStatus(
        STATE_FAILED,
        reason_code=failure.code,
        reason=failure.reason,
        retry=retry,
        checked_at=entry.checked_at,
    )


def server_status(
    entry: ServerEntry,
    *,
    connected: bool,
    attempting: bool,
    own: Failure | None,
    retry: int,
    now: float,
    closed: bool = False,
) -> ServerStatus:
    """Статус сервера у потребителя по снимку его состояния.

    ``connected`` — сессия есть или удача без постоянной сессии;
    ``attempting`` — идёт своя попытка; ``own`` — своя последняя неудачная
    или прерванная попытка; ``retry`` — номер повтора серии; ``now`` —
    часы сервиса; ``closed`` — потребитель закрыт.
    """
    if connected:
        return ServerStatus(
            STATE_CONNECTED, checked_at=entry.checked_at, tools=entry.tools
        )
    if attempting:
        return ServerStatus(
            STATE_CONNECTING,
            from_catalog=entry.tools_known,
            checked_at=entry.checked_at,
            tools=entry.tools,
        )
    if own is not None and not (entry.tools_known and _is_interrupted(own)):
        return _failed(own, entry, retry)
    return _idle_status(entry, now, closed)


def _idle_status(entry: ServerEntry, now: float, closed: bool) -> ServerStatus:
    """Своей попытки и своего отказа нет (или попытка прервана при
    известных тулах).

    Тулы известны — ``cached``; иначе пока правило 30 с держит запись
    (``shared_hold_since``) — ``failed`` с кодом и текстом отказа записи;
    иначе у закрытого потребителя — «подключение прервано» (он больше не
    подключается), у открытого — ``connecting``.
    """
    if entry.tools_known:
        return ServerStatus(
            STATE_CACHED,
            from_catalog=True,
            checked_at=entry.checked_at,
            tools=entry.tools,
        )
    held = shared_hold_since(entry)
    if held is not None and now - held < ATTEMPT_INTERVAL_SEC:
        return _failed(shared_failure(entry), entry)
    if closed:
        return _failed(Failure(now, "other", INTERRUPTED_REASON), entry)
    # Первая попытка начнётся в той же сверке.
    return ServerStatus(STATE_CONNECTING, checked_at=entry.checked_at)


__all__ = [
    "ATTEMPT_INTERVAL_SEC",
    "INTERRUPTED_REASON",
    "STATE_CACHED",
    "STATE_CONNECTED",
    "STATE_CONNECTING",
    "STATE_FAILED",
    "ServerStatus",
    "server_status",
    "shared_failure",
    "shared_hold_since",
]
