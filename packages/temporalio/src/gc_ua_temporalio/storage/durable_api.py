"""Обычный ``ThreadStore`` поверх durable-операций хранилища.

Каждая запись — операция ``commit_operation`` с новым ``operation_id``
(``_legacy_commit``): у хранилища одна точка атомарной записи и одни правила
владельца/ревизии для durable-исполнителя и для обычных вызовов ядра.
Реализации дают ``_legacy_commit``, ``load_thread_state``, ``load_meta`` и
``read_subagent_trace``.
"""

from __future__ import annotations

from copy import deepcopy

from gc_core.thread_store_base import ThreadStoreBase


def _copy_sections(receipt, session_meta, sections):
    if session_meta is None:
        return
    for key in sections:
        if key in receipt.meta:
            session_meta[key] = deepcopy(receipt.meta[key])
        else:
            session_meta.pop(key, None)


class DurableThreadStoreAPI(ThreadStoreBase):
    def load_messages(self, system_id, thread_id):
        return self.load_thread_state(system_id, thread_id).messages

    def append_messages(
        self, system_id, thread_id, messages, *, known_keys=None, start_seq=0
    ):
        receipt = self._legacy_commit(
            system_id,
            thread_id,
            "append_messages",
            {
                "messages": list(messages),
                "known_keys": sorted(known_keys)
                if known_keys is not None
                else None,
                "start_seq": start_seq,
            },
        )
        if known_keys is not None:
            known_keys.update(receipt.added_known_keys)
        return receipt.written

    def save_meta(self, system_id, thread_id, meta):
        self._legacy_commit(system_id, thread_id, "save_meta", {"meta": meta})

    def patch_runtime_flag(
        self, system_id, thread_id, flag, *, active, session_meta=None
    ):
        receipt = self._legacy_commit(
            system_id,
            thread_id,
            "patch_runtime_flag",
            {"flag": flag, "active": active},
        )
        _copy_sections(receipt, session_meta, ("runtime", "tui"))

    def reset_run_flags(self, system_id, thread_id, *, session_meta=None):
        runtime = (session_meta or {}).get("runtime")
        runtime = runtime if isinstance(runtime, dict) else {}
        receipt = self._legacy_commit(
            system_id,
            thread_id,
            "reset_run_flags",
            {"stale_summarizing": bool(runtime.get("summarizing"))},
        )
        _copy_sections(receipt, session_meta, ("runtime", "tui"))

    def update_runtime_meta(self, system_id, thread_id, patch):
        self._legacy_commit(
            system_id, thread_id, "update_runtime_meta", {"patch": patch}
        )

    def discard_message(self, system_id, thread_id, message_id):
        return self._legacy_commit(
            system_id, thread_id, "discard_message", {"message_id": message_id}
        ).result

    def reset(self, system_id, thread_id):
        return self._legacy_commit(system_id, thread_id, "reset", {}).result

    def replace_messages(self, system_id, thread_id, messages):
        self._legacy_commit(
            system_id,
            thread_id,
            "replace_messages",
            {"messages": list(messages)},
        )

    def archive_and_replace(self, system_id, thread_id, messages):
        return self._legacy_commit(
            system_id,
            thread_id,
            "archive_and_replace",
            {"messages": list(messages)},
        ).archive

    def append_subagent_trace(
        self,
        system_id,
        thread_id,
        *,
        parent_call_id,
        child_run_id,
        kind,
        payload,
    ):
        return self._legacy_commit(
            system_id,
            thread_id,
            "append_subagent_trace",
            {
                "parent_call_id": parent_call_id,
                "child_run_id": child_run_id,
                "kind": kind,
                "payload": payload,
            },
        ).result
