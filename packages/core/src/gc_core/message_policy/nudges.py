"""Batch-наджи в tool output: буфер, hold носителя, fill, карточка.

Цикл агента решает момент flush / clear / accept; политики дают текст;
сюда — контракт ``technical_system_messages`` и механика hold/publish.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from typing import Any

from gc_core.auto_routing.policy import LOOP
from gc_core.execution.contracts import ExecutionFailure
from gc_core.loop_detector.detector import clear as clear_loop_detector
from gc_core.loop_detector.detector import flush as flush_loop_detector
from gc_core.message_policy.mode_messages import mode_boundary_messages
from gc_core.message_policy.tool_usage_notice.apply import (
    notice_texts_before_llm,
)
from gc_core.message_policy.tool_usage_notice.state import (
    persist_tool_usage_notice_state,
    set_project_memory_check_cnt,
)
from gc_core.message_policy.user_message_policy import (
    TECHNICAL_TYPE_CURRENT_CONTEXT,
    TECHNICAL_TYPE_ERROR_MESSAGE,
    wrap_technical_user_content,
)
from gc_core.run_summary_coordinator import filter_messages_not_in_state
from gc_core.security.permission_mode import offers_plan_tool, resolve_mode
from gc_core.utils.generate_message_id import generate_msg_id_sha256

HARNESS_KEY = "technical_system_messages"
_HARNESS_KEY_RE = re.compile(r"^technical_system_messages(?:_\d+)?$")


def _object_payload(output: str) -> dict[str, Any] | None:
    if not isinstance(output, str):
        return None
    try:
        parsed = json.loads(output)
    except (TypeError, ValueError):
        return None
    if isinstance(parsed, dict):
        return parsed
    return None


def _free_harness_key(payload: dict[str, Any]) -> str:
    if HARNESS_KEY not in payload:
        return HARNESS_KEY
    index = 1
    while f"{HARNESS_KEY}_{index}" in payload:
        index += 1
    return f"{HARNESS_KEY}_{index}"


def _visible_harness_texts(texts: list[str]) -> list[str]:
    """Те же теги, что у служебного user: ``current_context``."""
    visible: list[str] = []
    for text in texts:
        if not isinstance(text, str) or text.strip() == "":
            continue
        visible.append(
            wrap_technical_user_content(
                text.strip(),
                technical_type=TECHNICAL_TYPE_CURRENT_CONTEXT,
            )
        )
    return visible


def _payload_with_harness_first(
    payload: dict[str, Any], key: str, messages: list[str]
) -> dict[str, Any]:
    ordered = {key: messages}
    for existing_key, value in payload.items():
        if existing_key == key:
            continue
        ordered[existing_key] = value
    return ordered


def attach_technical_system_messages(output: str, texts: list[str]) -> str:
    """Дописать непустые строки harness в JSON output.

    Список пишется первым ключом объекта, чтобы модель видела его до
    ``stdout``. Строки оборачиваются в ``<technical type="current_context">``,
    как служебный user. Объект: первый свободный ключ
    ``technical_system_messages``, затем ``_1``, ``_2``. Остальные ключи не
    меняются, ``ok`` / ``tool`` / ``error`` не добавляются. Не-объект при
    непустом списке оборачивается в объект с этим ключом и ``result``.
    Пустой список оставляет строку как была.
    """
    messages = _visible_harness_texts(texts)
    if not messages:
        return output
    payload = _object_payload(output)
    if payload is None:
        return json.dumps(
            {HARNESS_KEY: messages, "result": output},
            ensure_ascii=False,
        )
    key = _free_harness_key(payload)
    return json.dumps(
        _payload_with_harness_first(payload, key, messages),
        ensure_ascii=False,
    )


def output_for_card(output: str) -> str:
    """Тело карточки: ключи harness скрыты, прежние поля ответа на месте.

    Обёртка не-объекта (только ``result`` и ключ harness) показывается
    исходной строкой. Успех по-прежнему читается из ``ok`` до вызова.
    """
    payload = _object_payload(output)
    if payload is None:
        return output
    stripped = {
        key: value
        for key, value in payload.items()
        if not _HARNESS_KEY_RE.match(str(key))
    }
    if stripped.keys() == payload.keys():
        return output
    if set(stripped) == {"result"} and isinstance(stripped.get("result"), str):
        return stripped["result"]
    return json.dumps(stripped, ensure_ascii=False)


class NudgeBuffer:
    """Буфер наджей: эмиттеры пишут сюда; цикл один раз flush или clear."""

    __slots__ = ("texts",)

    def __init__(self) -> None:
        self.texts: list[str] = []

    def __bool__(self) -> bool:
        return bool(self.texts)

    def emit(self, text: str) -> None:
        if isinstance(text, str) and text.strip():
            self.texts.append(text.strip())

    def extend(self, items: list[str]) -> None:
        for text in items:
            self.emit(text)

    def clear(self) -> None:
        self.texts.clear()

    def take(self) -> list[str]:
        out = list(self.texts)
        self.texts.clear()
        return out

    def flush_into(self, carrier: dict[str, Any]) -> None:
        texts = self.take()
        if not texts:
            return
        carrier["output"] = attach_technical_system_messages(
            str(carrier.get("output") or ""), texts
        )


async def aproject_memory_hash(ctx: Any) -> str | None:
    """Версия памяти проекта для подсказок — только когда они включены."""
    from gc_core.message_policy.project_memory_notice.policy import (
        project_memory_hints_enabled,
    )

    try:
        enabled = project_memory_hints_enabled(ctx)
    except TypeError:
        return None
    if not enabled:
        return None
    from gc_core.project_memory import aget_hash

    return await aget_hash(ctx)


async def afill_nudge_buffer(
    buffer: NudgeBuffer,
    state: Any,
    ctx: Any,
    *,
    session_meta: dict[str, Any] | None,
    step_messages: list[dict[str, Any]],
    backend_mode: str,
    logger: Any | None = None,
) -> None:
    """Эмиттеры → буфер. Счётчики тикают здесь один раз за fill."""
    notice = flush_loop_detector(state)
    if notice:
        state.tick_signals.add(LOOP)
        buffer.emit(notice)
    if backend_mode == "chat_completions" and isinstance(session_meta, dict):
        try:
            buffer.extend(
                notice_texts_before_llm(
                    state,
                    ctx,
                    session_meta,
                    step_messages=step_messages,
                    logger=logger,
                    project_memory_hash=await aproject_memory_hash(ctx),
                )
            )
        except ExecutionFailure:
            raise
        except Exception as mem_exc:
            if logger is not None:
                logger.warning(
                    "Memory messages hook warning: %s",
                    mem_exc,
                    exc_info=True,
                )
    texts, state.mode_announced = mode_boundary_messages(
        current=resolve_mode(ctx).value,
        announced=state.mode_announced,
        can_propose_plan=offers_plan_tool(ctx, state),
    )
    buffer.extend(texts)


def clear_nudge_counters_after_summary(
    state: Any,
    *,
    session_meta: dict[str, Any] | None,
    buffer: NudgeBuffer | None = None,
) -> None:
    """После саммари — новый трейс: счётчики в цикле флоу, не в компакции.

    ``mode_announced`` и baseline hash project memory не трогаем.
    """
    if buffer is not None:
        buffer.clear()
    clear_loop_detector(state)
    state.tool_usage_notice_cnt = {}
    state.todo_reminder_cnt = 0
    if isinstance(session_meta, dict):
        set_project_memory_check_cnt(session_meta, 0)
        persist_tool_usage_notice_state(state, session_meta)


def _technical_user_note(text: str) -> dict[str, Any]:
    return {
        "role": "user",
        "content": wrap_technical_user_content(
            text, technical_type=TECHNICAL_TYPE_ERROR_MESSAGE
        ),
        "id": generate_msg_id_sha256("m"),
    }


def _published_in_state(state: Any, message: dict[str, Any]) -> dict[str, Any]:
    message_id = message.get("id")
    if isinstance(message_id, str) and message_id:
        for current in getattr(state, "messages", []) or []:
            if isinstance(current, dict) and current.get("id") == message_id:
                return current
    return message


def _insert_carrier(
    messages: list[dict[str, Any]], state: Any, carrier: dict[str, Any]
) -> None:
    if any(message is carrier for message in state.messages):
        return
    if getattr(state, "turn_messages_start", None) is None and hasattr(
        state, "mark_turn_start"
    ):
        state.mark_turn_start()
    try:
        index = messages.index(carrier)
    except ValueError:
        state.messages.append(carrier)
        return
    following = {
        message.get("id")
        for message in messages[index + 1 :]
        if isinstance(message, dict) and isinstance(message.get("id"), str)
    }
    if not following:
        state.messages.append(carrier)
        return
    inserted = False
    rebuilt: list[dict[str, Any]] = []
    for message in state.messages:
        if (
            not inserted
            and isinstance(message, dict)
            and message.get("id") in following
        ):
            rebuilt.append(carrier)
            inserted = True
        rebuilt.append(message)
    if not inserted:
        rebuilt.append(carrier)
    state.messages = rebuilt


class NudgeHold:
    """Hold носителя батча + буфер: publish до/после flush, clear на stop."""

    __slots__ = (
        "buffer",
        "carrier",
        "compacted",
        "followers",
        "notices_sent",
        "skip_fill",
        "trace",
    )

    def __init__(self) -> None:
        self.buffer = NudgeBuffer()
        self.trace: dict[str, Any] | None = None
        self.compacted = False
        self.carrier: dict[str, Any] | None = None
        self.followers: list[dict[str, Any]] = []
        # Подсказки этого шага уже ушли в сообщения. Повтор после
        # переполнения окна не собирает их заново. Снимается, когда
        # запрос к модели завершился и следующий шаг — новый.
        self.notices_sent = False
        # Ближайший запрос после сводки не собирает подсказки заново.
        self.skip_fill = False

    def clear(self, state: Any) -> None:
        """Accept / stop / compact: буфер и loop без доставки модели."""
        self.buffer.clear()
        self.notices_sent = False
        self.skip_fill = False
        clear_loop_detector(state)

    def reset_after_summary(
        self, state: Any, *, session_meta: dict[str, Any] | None
    ) -> None:
        clear_nudge_counters_after_summary(
            state, session_meta=session_meta, buffer=self.buffer
        )

    def begin_tool_batch(self) -> dict[str, Any]:
        self.trace = {
            "executed": [],
            "persisted": set(),
            "outputs": [],
        }
        self.compacted = False
        self.carrier = None
        self.followers = []
        return self.trace

    def mark_compacted(self) -> None:
        """Окно после session_compact: до следующего батча тулов наджи не шлём."""
        self.compacted = True

    def note_persisted(self, messages: list[dict[str, Any]] | None) -> None:
        trace = self.trace
        if not isinstance(trace, dict):
            return
        persisted = trace.setdefault("persisted", set())
        for message in messages or []:
            message_id = (
                message.get("id") if isinstance(message, dict) else None
            )
            if isinstance(message_id, str) and message_id:
                persisted.add(message_id)

    def wrap_persist(self, callback: Any) -> Any:
        """Пометить id как записанные, затем вызвать внешний persist."""

        def tracked(messages: list[dict[str, Any]]):
            self.note_persisted(messages)
            if callback is None:
                return None
            return callback(messages)

        return tracked

    def find_carrier(
        self, messages: list[dict[str, Any]]
    ) -> dict[str, Any] | None:
        """Последний ещё не записанный output реально исполненного вызова."""
        if self.compacted:
            return None
        trace = self.trace
        if not isinstance(trace, dict):
            return None
        persisted = trace.get("persisted") or set()
        by_call: dict[Any, dict[str, Any]] = {}
        for message in messages:
            if (
                isinstance(message, dict)
                and message.get("type") == "function_call_output"
            ):
                by_call[message.get("call_id")] = message
        for call_id in reversed(list(trace.get("executed") or [])):
            message = by_call.get(call_id)
            if message is None or message.get("id") in persisted:
                continue
            return message
        return None

    def arm_prepare(
        self, messages: list[dict[str, Any]], state: Any
    ) -> list[dict[str, Any]]:
        """Последний ответ и соседи за ним — на диск после решения про сводку.

        В историю они попадают сразу. ``state`` не режется: вынимать нечего.
        → что записать на диск этим шагом, без носителя и соседей.
        """
        del state
        carrier = self.find_carrier(messages)
        self.carrier = carrier
        self.followers = []
        if carrier is None:
            return messages
        try:
            carrier_at = messages.index(carrier)
        except ValueError:
            return messages
        self.followers = [
            message
            for message in messages[carrier_at + 1 :]
            if isinstance(message, dict)
        ]
        held_ids = {
            id(carrier),
            *(id(message) for message in self.followers),
        }
        return [message for message in messages if id(message) not in held_ids]

    async def _apersist_one(
        self,
        message: dict[str, Any],
        *,
        state: Any,
        apersist: Callable[[list[dict[str, Any]]], Awaitable[None]],
    ) -> None:
        published = _published_in_state(state, message)
        await apersist([published])
        message_id = published.get("id")
        if (
            isinstance(self.trace, dict)
            and isinstance(message_id, str)
            and message_id
        ):
            self.trace.setdefault("persisted", set()).add(message_id)

    def _flush_or_clear_buffer(
        self, carrier: dict[str, Any], *, with_nudges: bool
    ) -> None:
        if with_nudges and self.buffer:
            self.buffer.flush_into(carrier)
            return
        if not with_nudges:
            self.buffer.clear()

    def _place_followers(self, state: Any, carrier: dict[str, Any]) -> None:
        followers = [
            message
            for message in self.followers
            if not any(current is message for current in state.messages)
        ]
        if not followers:
            return
        rebuilt: list[dict[str, Any]] = []
        placed = False
        for message in state.messages:
            rebuilt.append(message)
            if message is carrier:
                rebuilt.extend(followers)
                placed = True
        if not placed:
            rebuilt.extend(followers)
        state.messages = rebuilt

    async def apublish(
        self,
        messages: list[dict[str, Any]],
        *,
        state: Any,
        apersist: Callable[[list[dict[str, Any]]], Awaitable[None]],
        with_nudges: bool,
    ) -> None:
        """Записать отложенный носитель (и соседей). with_nudges=False — clear."""
        carrier = self.carrier
        if carrier is None:
            if not with_nudges:
                self.buffer.clear()
            return
        self._flush_or_clear_buffer(carrier, with_nudges=with_nudges)
        _insert_carrier(messages, state, carrier)
        self._place_followers(state, carrier)
        await self._apersist_one(carrier, state=state, apersist=apersist)
        for follower in self.followers:
            await self._apersist_one(follower, state=state, apersist=apersist)
        self.followers = []
        self.carrier = None

    async def aflush_before_model(
        self,
        messages: list[dict[str, Any]],
        *,
        state: Any,
        ctx: Any,
        session_meta: dict[str, Any] | None,
        backend_mode: str,
        logger: Any | None,
        apersist: Callable[[list[dict[str, Any]]], Awaitable[None]],
    ) -> tuple[list[dict[str, Any]], bool]:
        """Один flush перед моделью.

        → ``(messages, done)``. ``done=True`` — носитель/compact закрыли путь;
        ``False`` — нужен редкий user-хвост / notices на границе хода.
        """
        if self.compacted:
            await self.apublish(
                messages, state=state, apersist=apersist, with_nudges=False
            )
            return messages, True

        carrier = self.carrier
        has_carrier = carrier is not None and any(
            message is carrier for message in messages
        )
        if has_carrier:
            await afill_nudge_buffer(
                self.buffer,
                state,
                ctx,
                session_meta=session_meta,
                step_messages=messages,
                backend_mode=backend_mode,
                logger=logger,
            )
            self.buffer.flush_into(carrier)
            await self.apublish(
                messages, state=state, apersist=apersist, with_nudges=False
            )
            return messages, True

        await self.apublish(
            messages, state=state, apersist=apersist, with_nudges=False
        )
        if self.buffer:
            notes = [_technical_user_note(text) for text in self.buffer.take()]
            messages = [*notes, *messages]
            to_append = filter_messages_not_in_state(
                list(getattr(state, "messages", []) or []), notes
            )
            if to_append:
                state.messages += to_append
                await apersist(to_append)
        return messages, False
