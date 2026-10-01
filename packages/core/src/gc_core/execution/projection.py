"""Pure ThreadSession projection: prepare first, apply only after a durable ack."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict, List, Optional, Protocol, Set

from gc_core.execution.contracts import (
    ExecutionFailure,
    ProjectionAck,
    ProjectionBatch,
)
from gc_core.execution.message_keys import make_message_key
from gc_core.intent_log.serialize import deserialize_list, serialize_for_meta
from gc_core.message_policy.tool_usage_notice.state import (
    merge_tool_usage_notice_meta_to_base,
)
from gc_core.persistence.live_volume import bump_live_volume_for_messages
from gc_core.persistence.thread_meta_mapper import read_optional_nonneg_int


class AtomicProjectionStore(Protocol):
    def commit_batch(self, batch: ProjectionBatch) -> ProjectionAck: ...


class ThreadProjection:
    def __init__(self, *, system_id, thread_id, meta, messages=(), max_seq=0):
        self.system_id = system_id
        self.thread_id = thread_id
        self.meta = deepcopy(meta)
        self.history_messages = deepcopy(list(messages))
        self.known_message_keys = {make_message_key(m) for m in messages}
        self.persisted_messages_count = max_seq
        self.previous_response_id = meta.get("previous_response_id")
        self.backend_api_mode = meta.get(
            "backend_api_mode", "chat_completions"
        )
        self._proj_tags = ()
        self._proj_strip_think = True

    def prepare_projection(
        self, *, owner, epoch, event_id, sequence, messages, meta
    ):
        staged = ThreadProjection(
            system_id=self.system_id,
            thread_id=self.thread_id,
            meta=meta,
            messages=self.history_messages,
            max_seq=self.persisted_messages_count,
        )
        staged._proj_tags = self._proj_tags
        staged._proj_strip_think = self._proj_strip_think
        known = staged._persist_batch_begin(messages)
        fresh = {make_message_key(m) for m in messages} - known
        staged._persist_batch_apply(
            messages, known, len(fresh), meta.get("previous_response_id")
        )
        return ProjectionBatch(
            self.system_id,
            self.thread_id,
            owner,
            epoch,
            event_id,
            sequence,
            deepcopy(messages),
            staged._persist_batch_meta(meta, messages, known),
        )

    def apply_projection_ack(self, batch: ProjectionBatch, ack: ProjectionAck):
        if (ack.event_id, ack.sequence) != (batch.event_id, batch.sequence):
            raise ExecutionFailure("Projection ack does not match the batch")
        known = self._persist_batch_begin(batch.messages)
        self._persist_batch_apply(
            batch.messages,
            known,
            ack.written,
            batch.meta.get("previous_response_id"),
        )
        self.known_message_keys.update(
            make_message_key(m) for m in batch.messages
        )
        self.persisted_messages_count = ack.max_seq
        self.meta = deepcopy(batch.meta)

    def _persist_batch_begin(
        self, step_messages: List[Dict[str, Any]]
    ) -> Set[str]:
        """Снимок ключей ДО записи. Чистая часть — общая для sync и async."""
        known_before = set(self.known_message_keys)
        self.known_keys_snapshot_before_last_batch = frozenset(known_before)
        return known_before

    def _persist_batch_apply(
        self,
        step_messages: List[Dict[str, Any]],
        known_before: Set[str],
        written: int,
        previous_response_id: Optional[str],
    ) -> None:
        """Обновить состояние сессии по итогам записи. Чистая часть."""
        self.persisted_messages_count += written
        known_seen = set(known_before)
        for message in step_messages:
            key = make_message_key(message)
            if key in known_seen:
                continue
            self.history_messages.append(message)
            known_seen.add(key)
        self.previous_response_id = previous_response_id

    def _persist_batch_meta(
        self,
        base_meta: Dict[str, Any],
        step_messages: Optional[List[Dict[str, Any]]] = None,
        known_before: Optional[Set[str]] = None,
    ) -> Dict[str, Any]:
        """Собрать meta для записи. Чистая часть.

        Вынесено, чтобы синхронный и асинхронный пути не могли разойтись: у них
        различается ТОЛЬКО последовательность обращений к хранилищу, а вся
        логика — здесь, в одном экземпляре.

        Счётчики живого объёма (``live_history_chars`` / ``live_msg_count``)
        считаются ЗДЕСЬ, а не в файловом слое. Раньше их бампал
        ``append_new_thread_messages``, то есть только файловая реализация:
        у хранилища в базе этих ключей не появлялось вовсе, а TUI их читает
        (индикатор объёма контекста). Это производная от сообщений, а не
        деталь хранения, и место ей в сессии.
        """
        base_meta = dict(base_meta or {})
        if step_messages is not None and known_before is not None:
            fresh = [
                m
                for m in step_messages
                if make_message_key(m) not in known_before
            ]
            if fresh:
                # Параметры проекции — с develop: без них счётчик считал бы
                # служебные теги и рассуждения, которых модель не видит.
                bump_live_volume_for_messages(
                    base_meta,
                    fresh,
                    tags=self._proj_tags,
                    strip_think=self._proj_strip_think,
                )
        base_meta["created_at"] = self.meta.get("created_at") or base_meta.get(
            "created_at"
        )
        base_meta["previous_response_id"] = self.previous_response_id
        base_meta["backend_api_mode"] = self.backend_api_mode
        base_meta["messages_count"] = self.persisted_messages_count
        self._copy_session_meta_fields(base_meta)
        # Persist rendered chat/completions system prompt for transparency/debugging.
        # Must stay identical to актуальным данным: when we are in `responses`
        # mode, there is no system prompt in messages, so it must not be present in meta.
        if self.backend_api_mode == "chat_completions":
            prompt = self.meta.get("chat_completions_system_prompt_rendered")
            if isinstance(prompt, str) and prompt.strip():
                base_meta["chat_completions_system_prompt_rendered"] = prompt
        else:
            base_meta.pop("chat_completions_system_prompt_rendered", None)
        merge_tool_usage_notice_meta_to_base(base_meta, self.meta)
        return base_meta

    def _copy_session_meta_fields(self, base_meta: Dict[str, Any]) -> None:
        """Перенести в base_meta поля сессии, которые есть в self.meta в валидной форме."""
        if isinstance(self.meta.get("shared"), dict):
            base_meta["shared"] = self.meta["shared"]
        if isinstance(self.meta.get("last_backend_response_id"), str):
            base_meta["last_backend_response_id"] = self.meta.get(
                "last_backend_response_id"
            )
        if isinstance(self.meta.get("user_intent_log"), list):
            base_meta["user_intent_log"] = serialize_for_meta(
                deserialize_list(self.meta.get("user_intent_log"))
            )
        if isinstance(self.meta.get("current_user_intent"), list):
            base_meta["current_user_intent"] = serialize_for_meta(
                deserialize_list(self.meta.get("current_user_intent"))
            )
        if "user_turn_seq" in self.meta:
            base_meta["user_turn_seq"] = int(
                self.meta.get("user_turn_seq") or 0
            )
        if "dialog_steps_offset" in self.meta:
            base_meta["dialog_steps_offset"] = int(
                self.meta.get("dialog_steps_offset") or 0
            )
        for prompt_key in (
            "last_main_prompt_tokens",
            "last_main_prompt_message_count",
        ):
            if prompt_key in self.meta:
                base_meta[prompt_key] = read_optional_nonneg_int(
                    self.meta.get(prompt_key)
                )
        if isinstance(self.meta.get("token_usage"), dict):
            base_meta["token_usage"] = self.meta["token_usage"]
