"""Правила живого статуса хода (``RuntimeStatus``) — без meta-словаря.

Каждое правило возвращает новый ``RuntimeStatus``. Явно заданные поля
(``model_fields_set``) ведут себя как ключи секции ``runtime`` meta develop:
поле задано ⇔ ключ есть; пустая секция без единого ключа — статус по
умолчанию (файловое хранилище её не пишет, как не писал develop).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from gc_core.attachments.numbering import label_numbers
from gc_core.threads.models import RuntimeStatus

_RUN_FLAGS = ("summarizing", "finalizing", "finish_gate_checking")


def _keys(rt: RuntimeStatus) -> set[str]:
    """Ключи секции ``runtime``, которые у статуса есть."""
    keys = set(rt.model_fields_set) - {"flags"}
    keys |= set(rt.flags)
    keys |= set(rt.model_extra or {})
    return keys


def _written(rt: RuntimeStatus) -> RuntimeStatus:
    """Статус после записи секции: метки пустой секции нет (``flags`` задан
    только с флагами), без единого ключа — статус по умолчанию."""
    if not rt.flags and "flags" in rt.model_fields_set:
        rt = rt.model_copy()
        rt.__pydantic_fields_set__.discard("flags")
    return rt if _keys(rt) else RuntimeStatus()


def _set(rt: RuntimeStatus, **values: Any) -> RuntimeStatus:
    return _written(rt.model_copy(update=values))


def _with_flags(rt: RuntimeStatus, flags: frozenset[str]) -> RuntimeStatus:
    return _set(rt, flags=flags)


def _unset(rt: RuntimeStatus, name: str) -> RuntimeStatus:
    if name not in rt.model_fields_set:
        return rt
    default = RuntimeStatus.model_fields[name].default
    out = rt.model_copy(update={name: default})
    out.__pydantic_fields_set__.discard(name)
    return _written(out)


def with_turn(rt: RuntimeStatus, *, running: bool) -> RuntimeStatus:
    """Отметка хода для восстановления: ``running`` — ход идёт; снятие —
    штатный конец, статус возвращается к прежнему виду."""
    if not running:
        return _unset(rt, "turn")
    if rt.turn == "running" and "turn" in rt.model_fields_set:
        return rt
    return _set(rt, turn="running")


def with_flag(rt: RuntimeStatus, flag: str, *, active: bool) -> RuntimeStatus:
    """Взвести или снять флаг хода (``summarizing``, ``finalizing``,
    ``finish_gate_checking``); неизвестное имя — ``ValueError``."""
    # Внутри функции: пакет ``persistence`` при загрузке тянет states и
    # реестр тулов, а ``threads`` обязан грузиться лёгким.
    from gc_core.persistence.meta_ownership import resolve_runtime_flag_name

    name = resolve_runtime_flag_name(flag)
    flags = rt.flags | {name} if active else rt.flags - {name}
    return _with_flags(rt, frozenset(flags))


def reset_run_flags(rt: RuntimeStatus) -> RuntimeStatus:
    """Сбросить флаги хода при старте (залипшие после падения)."""
    return _with_flags(rt, frozenset(rt.flags - set(_RUN_FLAGS)))


def _volume(messages: Sequence[Mapping[str, Any]], tags, strip_think):
    from gc_core.persistence.live_volume import live_volume_from_messages

    return live_volume_from_messages(
        list(messages), tags=tags, strip_think=strip_think
    )


def with_live_volume(
    rt: RuntimeStatus,
    messages: Sequence[Mapping[str, Any]],
    *,
    tags: Sequence[str] = (),
    strip_think: bool = True,
) -> RuntimeStatus:
    """Объём живой истории — пересчёт целиком (после сжатия)."""
    return _set(rt, **_volume(messages, tags, strip_think))


def bumped_live_volume(
    rt: RuntimeStatus,
    fresh: Sequence[Mapping[str, Any]],
    *,
    tags: Sequence[str] = (),
    strip_think: bool = True,
) -> RuntimeStatus:
    """Объём живой истории — прирост на только что дописанные сообщения."""
    delta = _volume(fresh, tags, strip_think)
    chars = (rt.live_history_chars or 0) + delta["live_history_chars"]
    count = (rt.live_msg_count or 0) + delta["live_msg_count"]
    return _set(
        rt, live_history_chars=max(0, chars), live_msg_count=max(0, count)
    )


def bumped_image_seq(
    rt: RuntimeStatus, fresh: Iterable[Mapping[str, Any]]
) -> RuntimeStatus:
    """Сквозной номер картинок треда только растёт: появился номер больше —
    он и записывается; иначе статус прежний."""
    current = rt.image_seq if type(rt.image_seq) is int else 0
    current = max(current, 0)
    image_seq = max([current, *label_numbers(fresh)])
    if image_seq <= current:
        return rt
    return _set(rt, image_seq=image_seq)


def with_trace_id(rt: RuntimeStatus, trace_id: str) -> RuntimeStatus:
    """Дописать trace_id прогона (без дубля)."""
    ids = [str(x) for x in rt.trace_ids or []]
    if trace_id in ids:
        return rt
    return _set(rt, trace_ids=[*ids, trace_id])


def with_mcp_tools(rt: RuntimeStatus, snapshot: Any) -> RuntimeStatus:
    """Снимок «сервер MCP → тулы» для экрана ``/mcp``."""
    return _set(rt, mcp_tools=snapshot)


def with_last_turn(
    rt: RuntimeStatus, last_turn: Mapping[str, Any]
) -> RuntimeStatus:
    """Короткий итог последнего закрытого хода (``status``, ``reason``)."""
    return _set(rt, last_turn=dict(last_turn))
