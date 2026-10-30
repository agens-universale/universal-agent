from typing import NamedTuple

from gc_core.execution.contracts import ModelProviderError

FINISH_KIND_STOP = "stop"
FINISH_KIND_TOOL_CALLS = "tool_calls"
FINISH_KIND_LENGTH = "length"
FINISH_KIND_BLOCKED = "blocked"  # content_filter / refusal / safety
FINISH_KIND_UNKNOWN = "unknown"

# Известные значения OpenAI-совместимых API → kind. Ключи — lower-case.
_KNOWN_FINISH_KINDS = {
    "stop": FINISH_KIND_STOP,
    "tool_calls": FINISH_KIND_TOOL_CALLS,
    "length": FINISH_KIND_LENGTH,
    "content_filter": FINISH_KIND_BLOCKED,
    "refusal": FINISH_KIND_BLOCKED,
    "safety": FINISH_KIND_BLOCKED,
}


class FinishDecision(NamedTuple):
    accept: (
        bool  # принять payload в историю (не крашиться/не ретраить транспорт)
    )
    unknown: bool  # finish_reason незнакомый → залогировать warning
    kind: str  # stop | tool_calls | length | blocked | unknown


def _finish_reason_key(finish_reason) -> str | None:
    if not isinstance(finish_reason, str):
        return None
    key = finish_reason.strip().lower()
    return key or None


def classify_finish_kind(finish_reason) -> str:
    """Одна классификация finish_reason для цикла агента и bounce-сообщений."""
    key = _finish_reason_key(finish_reason)
    if key is None:
        return FINISH_KIND_UNKNOWN
    return _KNOWN_FINISH_KINDS.get(key, FINISH_KIND_UNKNOWN)


def finish_blocks_final(finish_reason) -> bool:
    """Отказ провайдера: не принимать как финальный text answer (даже в allow)."""
    return classify_finish_kind(finish_reason) == FINISH_KIND_BLOCKED


def finish_prevents_text_final(finish_reason) -> bool:
    """Не принимать prose как финал хода: blocked (отказ) или length (усечение).

    Транспортный accept и запись в историю от этого не зависят — только
    решение text_response в run_agent (даже при text_answer_mode=allow).
    """
    kind = classify_finish_kind(finish_reason)
    return kind in (FINISH_KIND_BLOCKED, FINISH_KIND_LENGTH)


def normalize_finish_reason(
    finish_reason,
    *,
    has_content: bool,
    has_tool_calls: bool,
    has_reasoning: bool = False,
) -> FinishDecision:
    """Permissive-но-честная трактовка finish_reason (провайдер-агностично).

    Известный stop/tool_calls/length/blocked → принять, unknown=False.
    Незнакомый/null + есть content/tool_calls/reasoning → принять как payload,
    но пометить unknown (warning). Незнакомый/null + пусто → не принять
    (генуинно непригодно; конвертер отдаёт пустой батч → bounce, не
    транспортный ретрай).

    ``kind`` отделяет транспортный accept от политики цикла: blocked/length
    всё равно попадают в историю (если есть текст), но не становятся финалом
    (см. ``finish_prevents_text_final``).
    """
    key = _finish_reason_key(finish_reason)
    if key is not None and key in _KNOWN_FINISH_KINDS:
        return FinishDecision(
            accept=True, unknown=False, kind=_KNOWN_FINISH_KINDS[key]
        )
    if has_content or has_tool_calls or has_reasoning:
        return FinishDecision(
            accept=True, unknown=True, kind=FINISH_KIND_UNKNOWN
        )
    return FinishDecision(accept=False, unknown=True, kind=FINISH_KIND_UNKNOWN)


_CONTEXT_OVERFLOW_MARKERS = (
    "context length",
    "context window",
    "reduce the number of tokens",
    "please reduce the length",
    "maximum model length",  # vLLM: «The decoder prompt (length N) is longer than the maximum model length of M»
)


def is_context_overflow_error(exc: BaseException) -> bool:
    """True, если ошибка бэкенда — переполнение контекстного окна модели.

    Восстановление в цикле: force-compaction истории + повтор запроса
    (см. run_agent). Проверяем текст исключения, т.к. тип различается
    (stream → BackendStreamError, non-stream → HTTPError/ValueError)."""
    if isinstance(exc, ModelProviderError):
        return exc.failure.context_overflow
    text = str(exc).lower()
    return any(marker in text for marker in _CONTEXT_OVERFLOW_MARKERS)


def nonempty_reasoning_text(msg) -> str | None:
    """Return the first nonempty reasoning alias without changing its text."""
    if not isinstance(msg, dict):
        return None
    for key in ("reasoning_content", "reasoning"):
        value = msg.get(key)
        if isinstance(value, str) and value:
            return value
    return None
