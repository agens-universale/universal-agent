"""Окно payload-retention трейса: tools вне segments[-keep:] без тяжёлого payload.

Диск и agent history не трогаем. User / assistant / turn-finish остаются
полными во всех сегментах. Nested субагентов монтируется только для parent'ов
из окна.
"""

from __future__ import annotations

from typing import Any

from gc_tui.services.tool_identity import tool_id

RETENTION_DETAIL_HINT = "детали вне окна retention; /export или архив"

_LIGHT_KINDS = frozenset({"call", "result", "error"})
_IDENTITY_KEYS = ("id", "type", "role", "name", "call_id")


def retention_window_start_index(n: int, keep: int = 2) -> int:
    """Индекс первого сегмента окна `segments[-keep:]`.

    n <= keep → 0 (всё окно, в том числе тред без компакции из одного сегмента).
    keep <= 0 → n (окно пустое).
    """
    if n <= 0:
        return 0
    if keep <= 0:
        return n
    return max(0, n - keep)


def message_ids_in_retention_window(segments: list, *, keep: int = 2) -> set:
    """id сообщений из `segments[-keep:]`.

    Дедуп рисует первое вхождение. Preserved-копия call/output несёт тот же
    id в более новом сегменте: пока она в окне, первая строка остаётся full.
    """
    start = retention_window_start_index(len(segments), keep)
    ids: set = set()
    for seg in segments[start:]:
        messages = seg.get("messages") if isinstance(seg, dict) else None
        if not messages:
            continue
        for rec in messages:
            payload = rec.get("payload") if isinstance(rec, dict) else None
            if not isinstance(payload, dict):
                continue
            mid = payload.get("id")
            if mid is not None:
                ids.add(mid)
    return ids


def events_leaving_window(
    events: list,
    segment_starts: list[int],
    window_ids: set | frozenset,
    keep: int = 2,
) -> list:
    """События сегментов вне окна `segments[-keep:]`, кроме id окна.

    Берём все сегменты до окна, а не один выпавший: первая строка могла
    остаться full из-за preserved-копии, а теперь копия сама вышла из окна.
    Облегчение идемпотентно. Нет сегментов вне окна → [].
    """
    outside = len(segment_starts) - keep
    if outside <= 0:
        return []
    end = segment_starts[outside]
    return [
        ev
        for ev in events[:end]
        if getattr(ev, "message_id", None) not in window_ids
    ]


def payload_mode(kind: str, seg_in_window: bool) -> str:
    """`full` | `light`. Light — только tool call/result/error вне окна."""
    if seg_in_window or kind not in _LIGHT_KINDS:
        return "full"
    return "light"


def lighten_tool_payload(payload: Any) -> dict:
    """Идентификаторы вызова без arguments/output. Маркер `_retention=light`."""
    light: dict = {"_retention": "light"}
    if isinstance(payload, dict):
        for key in _IDENTITY_KEYS:
            if key not in payload:
                continue
            value = payload[key]
            if isinstance(value, (str, int, float, bool)) or value is None:
                light[key] = value
    return light


def is_light_payload(event: Any) -> bool:
    payload = getattr(event, "full_payload", None)
    return isinstance(payload, dict) and payload.get("_retention") == "light"


def apply_payload_retention(event: Any, *, seg_in_window: bool) -> Any:
    """Post-strip tool payload. Summary строки уже посчитан map_message."""
    if payload_mode(event.kind, seg_in_window) == "light":
        event.full_payload = lighten_tool_payload(event.full_payload)
    return event


def subagent_parent_ids_in_window(
    segments: list, *, keep: int = 2
) -> set[str]:
    """call_id parent `subagent_*` в сегментах окна. Для preview до отбрасывания сырья."""
    start = retention_window_start_index(len(segments), keep)
    ids: set[str] = set()
    for seg in segments[start:]:
        messages = seg.get("messages") if isinstance(seg, dict) else None
        if not messages:
            continue
        for rec in messages:
            payload = rec.get("payload") if isinstance(rec, dict) else None
            if not isinstance(payload, dict):
                continue
            if payload.get("type") != "function_call":
                continue
            name = payload.get("name")
            canon = tool_id(name) if isinstance(name, str) else None
            if not isinstance(canon, str) or not canon.startswith("subagent_"):
                continue
            call_id = payload.get("call_id")
            if isinstance(call_id, str) and call_id:
                ids.add(call_id)
    return ids


def subagent_parent_ids_from_events(events) -> set[str]:
    """call_id `subagent_*`, чей call в ленте не light (окно или тред без компакции)."""
    ids: set[str] = set()
    for ev in events:
        if getattr(ev, "kind", None) != "call":
            continue
        name = getattr(ev, "tool_name", None) or ""
        if not str(name).startswith("subagent_"):
            continue
        if is_light_payload(ev):
            continue
        call_id = getattr(ev, "call_id", None)
        if isinstance(call_id, str) and call_id:
            ids.add(call_id)
    return ids


def parent_outside_retention(events, parent_call_id: str) -> bool:
    """True, если call parent'а есть в ленте и его payload light.

    Нет call-события — не считаем parent вне окна (live-путь ещё без сегментов).
    """
    if not parent_call_id:
        return False
    for ev in events:
        if getattr(ev, "kind", None) != "call":
            continue
        if getattr(ev, "call_id", None) != parent_call_id:
            continue
        name = getattr(ev, "tool_name", None) or ""
        if not str(name).startswith("subagent_"):
            continue
        return is_light_payload(ev)
    return False


def nested_call_parent_id(records, call_id: str) -> str | None:
    """parent_call_id sidecar-записи с этим child call_id."""
    if not call_id:
        return None
    for rec in records:
        if not isinstance(rec, dict):
            continue
        payload = rec.get("payload")
        if not isinstance(payload, dict) or payload.get("call_id") != call_id:
            continue
        parent = rec.get("parent_call_id")
        if isinstance(parent, str) and parent:
            return parent
    return None


def filter_sidecar_to_parents(records, allowed_parent_ids: set[str]) -> list:
    """Оставить sidecar-записи parent'ов из окна. Полный список не кэшировать."""
    kept = []
    for rec in records:
        if not isinstance(rec, dict):
            continue
        parent = rec.get("parent_call_id")
        if isinstance(parent, str) and parent in allowed_parent_ids:
            kept.append(rec)
    return kept
