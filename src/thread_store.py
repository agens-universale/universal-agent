import json
import os
import re
from datetime import datetime, timezone
from typing import Dict, List, Tuple, Any, Set, Optional

from message_policy.user_message_policy import (
    TECHNICAL_TYPE_DIALOG_SUMMARY,
    wrap_technical_user_content,
)
from utils.generate_message_id import generate_msg_id_sha256 as generate_message_id_func


_THREAD_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
_SYSTEM_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
_META_SCHEMA_VERSION = 1


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _utc_now_suffix() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def make_message_key(message: Dict[str, Any]) -> str:
    msg_id = message.get("id")
    if isinstance(msg_id, str) and len(msg_id) > 0:
        return f"id:{msg_id}"
    payload = json.dumps(message, ensure_ascii=False, sort_keys=True)
    return f"payload:{payload}"


def ensure_message_id(message: Dict[str, Any], prefix: str = "m") -> Dict[str, Any]:
    if not isinstance(message, dict):
        return message
    msg_id = message.get("id")
    if isinstance(msg_id, str) and msg_id:
        return message
    patched = dict(message)
    patched["id"] = generate_message_id_func(prefix)
    return patched


def validate_thread_id(thread_id: str) -> str:
    if not isinstance(thread_id, str) or len(thread_id.strip()) == 0:
        raise ValueError("thread_id должен быть непустой строкой")
    normalized = thread_id.strip()
    if not _THREAD_ID_PATTERN.match(normalized):
        raise ValueError(
            "thread_id может содержать только латинские буквы, цифры, '_' и '-'"
        )
    return normalized


def sanitize_system_id(system_id: str) -> str:
    """
    Санитизировать идентификатор внешней системы для использования в имени файла.

    Разрешаем только латинские буквы, цифры, '_' и '-'.
    Остальные символы заменяем на '_'.
    """
    if not isinstance(system_id, str):
        raise ValueError("system_id должен быть строкой")

    s = system_id.strip().lower()
    if not s:
        raise ValueError("system_id должен быть непустой строкой")

    s = re.sub(r"[^A-Za-z0-9_-]+", "_", s)
    s = s.strip("_")
    if not s:
        raise ValueError("system_id после санитизации стал пустым")
    if not _SYSTEM_ID_PATTERN.match(s):
        # На случай логической ошибки при замене символов.
        raise ValueError("system_id может содержать только латинские буквы, цифры, '_' и '-'")
    return s


def resolve_threads_dir(config_dict: Dict[str, Any]) -> str:
    """
    Определить директорию для хранения тредов.

    Контракт: config_dict["threads_data_path"] должен быть АБСОЛЮТНЫМ
    путём, заполненным config_modules.root.resolve_paths(). Пустое значение
    означает, что resolve_paths() не вызывался — это ошибка интеграции.
    """
    path_cfg = config_dict.get("threads_data_path")
    if not isinstance(path_cfg, str) or len(path_cfg.strip()) == 0:
        raise ValueError(
            "threads_data_path пуст; "
            "config_modules.root.resolve_paths() должен был его заполнить"
        )

    path_cfg = path_cfg.strip()
    if not os.path.isabs(path_cfg):
        raise ValueError(
            f"threads_data_path должен быть абсолютным путём, получено: {path_cfg!r}"
        )
    return path_cfg


def _thread_paths_legacy(threads_dir: str, thread_id: str) -> Tuple[str, str]:
    """Старый формат файлов без system в имени."""
    normalized = validate_thread_id(thread_id)
    data_path = os.path.join(threads_dir, f"data_{normalized}.jsonl")
    meta_path = os.path.join(threads_dir, f"meta_{normalized}.json")
    return data_path, meta_path


def _thread_paths(threads_dir: str, system_id: str, thread_id: str) -> Tuple[str, str]:
    """Новый формат файлов с system в имени."""
    sys_norm = sanitize_system_id(system_id)
    normalized_thread_id = validate_thread_id(thread_id)
    data_path = os.path.join(
        threads_dir, f"data_{sys_norm}_{normalized_thread_id}.jsonl"
    )
    meta_path = os.path.join(
        threads_dir, f"meta_{sys_norm}_{normalized_thread_id}.json"
    )
    return data_path, meta_path


def ensure_threads_dir(threads_dir: str) -> None:
    os.makedirs(threads_dir, exist_ok=True)


def reset_thread_files(threads_dir: str, system_id: str, thread_id: str) -> List[str]:
    """
    Удалить файлы конкретного треда (data/meta), включая legacy-формат.
    Возвращает список удалённых путей.
    """
    sys_norm = sanitize_system_id(system_id)
    normalized_thread_id = validate_thread_id(thread_id)
    paths = []
    paths.extend(_thread_paths(threads_dir, sys_norm, normalized_thread_id))
    paths.extend(_thread_paths_legacy(threads_dir, normalized_thread_id))

    deleted: List[str] = []
    for path in paths:
        if os.path.exists(path):
            os.remove(path)
            deleted.append(path)
    return deleted


def load_thread_messages(
    threads_dir: str, system_id: str, thread_id: str
) -> Tuple[List[Dict[str, Any]], Set[str], int]:
    data_path, _ = _thread_paths(threads_dir, system_id, thread_id)
    if not os.path.exists(data_path):
        # fallback: старые файлы без system в имени
        data_path, _ = _thread_paths_legacy(threads_dir, thread_id)
    if not os.path.exists(data_path):
        return [], set(), 0

    messages: List[Dict[str, Any]] = []
    message_keys: Set[str] = set()
    max_seq = 0
    with open(data_path, "r", encoding="utf-8") as f:
        for line in f:
            row = line.strip()
            if len(row) == 0:
                continue
            try:
                record = json.loads(row)
            except json.JSONDecodeError:
                continue
            # Допускаем строковую обёртку (двойной json.dumps строки записи).
            if isinstance(record, str):
                try:
                    record = json.loads(record)
                except json.JSONDecodeError:
                    continue
            if not isinstance(record, dict) or record.get("type") != "message":
                continue
            seq = record.get("seq")
            if isinstance(seq, int) and seq > max_seq:
                max_seq = seq
            payload = record.get("payload")
            if not isinstance(payload, dict):
                continue
            payload = ensure_message_id(payload)
            messages.append(payload)
            message_keys.add(make_message_key(payload))

    return messages, message_keys, max_seq


def load_thread_meta(
    threads_dir: str, system_id: str, thread_id: str
) -> Dict[str, Any]:
    _, meta_path = _thread_paths(threads_dir, system_id, thread_id)
    if not os.path.exists(meta_path):
        # fallback: старые файлы без system в имени
        _, meta_path = _thread_paths_legacy(threads_dir, thread_id)

    if not os.path.exists(meta_path):
        return {}

    with open(meta_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        return {}
    return data


def save_thread_meta(
    threads_dir: str, system_id: str, thread_id: str, meta: Dict[str, Any]
) -> None:
    sys_norm = sanitize_system_id(system_id)
    _, meta_path = _thread_paths(threads_dir, sys_norm, thread_id)
    ensure_threads_dir(threads_dir)

    to_save = dict(meta)
    to_save["thread_id"] = validate_thread_id(thread_id)
    to_save["system"] = sys_norm
    to_save["schema_version"] = _META_SCHEMA_VERSION
    to_save["updated_at"] = _utc_now_iso()
    if not to_save.get("created_at"):
        to_save["created_at"] = to_save["updated_at"]

    tmp_path = f"{meta_path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(to_save, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, meta_path)


def append_new_thread_messages(
    threads_dir: str,
    system_id: str,
    thread_id: str,
    messages: List[Dict[str, Any]],
    known_message_keys: Set[str],
    *,
    start_seq: int = 0,
) -> int:
    if len(messages) == 0:
        return 0

    ensure_threads_dir(threads_dir)
    data_path, _ = _thread_paths(threads_dir, system_id, thread_id)

    seq = start_seq
    wrote = 0
    with open(data_path, "a", encoding="utf-8") as f:
        for message in messages:
            key = make_message_key(message)
            if key in known_message_keys:
                continue
            seq += 1
            record = {
                "type": "message",
                "thread_id": thread_id,
                "system": sanitize_system_id(system_id),
                "seq": seq,
                "ts": _utc_now_iso(),
                "payload": message,
            }
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
            known_message_keys.add(key)
            wrote += 1
    return wrote


class ThreadSession:
    def __init__(
        self,
        *,
        threads_dir: str,
        system_id: str,
        thread_id: str,
        history_messages: List[Dict[str, Any]],
        known_message_keys: Set[str],
        persisted_messages_count: int,
        meta: Dict[str, Any],
        previous_response_id: Optional[str],
        backend_api_mode: str,
    ):
        self.threads_dir = threads_dir
        self.system_id = sanitize_system_id(system_id)
        self.thread_id = thread_id
        self.history_messages = history_messages
        self.known_message_keys = known_message_keys
        self.persisted_messages_count = persisted_messages_count
        self.meta = meta
        self.previous_response_id = previous_response_id
        self.backend_api_mode = backend_api_mode
        self.runtime_messages: List[Dict[str, Any]] = []

    @classmethod
    def from_config(
        cls, *, config_dict: Dict[str, Any], system_id: str, thread_id: str
    ) -> "ThreadSession":
        sys_norm = sanitize_system_id(system_id)
        normalized_thread_id = validate_thread_id(thread_id)
        threads_dir = resolve_threads_dir(config_dict)
        ensure_threads_dir(threads_dir)

        meta = load_thread_meta(threads_dir, sys_norm, normalized_thread_id)
        history_messages, known_message_keys, max_seq = load_thread_messages(
            threads_dir, sys_norm, normalized_thread_id
        )
        previous_response_id = meta.get("previous_response_id")
        if not isinstance(previous_response_id, str) or len(previous_response_id) == 0:
            previous_response_id = None
        backend_api_mode_raw = meta.get("backend_api_mode", "responses")
        backend_api_mode = str(backend_api_mode_raw).strip().lower()
        if backend_api_mode not in {"responses", "chat_completions"}:
            backend_api_mode = "responses"

        return cls(
            threads_dir=threads_dir,
            system_id=sys_norm,
            thread_id=normalized_thread_id,
            history_messages=history_messages,
            known_message_keys=known_message_keys,
            persisted_messages_count=max_seq,
            meta=meta,
            previous_response_id=previous_response_id,
            backend_api_mode=backend_api_mode,
        )

    def build_initial_messages(self, new_user_message: Dict[str, Any]) -> List[Dict[str, Any]]:
        if self.previous_response_id is None:
            return [*self.history_messages, new_user_message]
        return [new_user_message]

    def set_runtime_messages(self, runtime_messages: List[Dict[str, Any]]) -> None:
        self.runtime_messages = runtime_messages

    def get_full_history_messages(self) -> List[Dict[str, Any]]:
        combined: List[Dict[str, Any]] = []
        seen_keys: Set[str] = set()
        for item in [*self.history_messages, *self.runtime_messages]:
            key = make_message_key(item)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            combined.append(item)
        return combined

    def persist_batch(
        self,
        step_messages: List[Dict[str, Any]],
        *,
        previous_response_id: Optional[str],
    ) -> None:
        known_before = set(self.known_message_keys)
        written = append_new_thread_messages(
            self.threads_dir,
            self.system_id,
            self.thread_id,
            step_messages,
            self.known_message_keys,
            start_seq=self.persisted_messages_count,
        )
        self.persisted_messages_count += written
        known_seen = set(known_before)
        for message in step_messages:
            key = make_message_key(message)
            if key in known_seen:
                continue
            self.history_messages.append(message)
            known_seen.add(key)
        self.previous_response_id = previous_response_id
        # Preserve arbitrary meta fields (e.g. memory_files), otherwise tools that
        # update meta would get their data wiped on the next persist_batch().
        base_meta = (
            load_thread_meta(self.threads_dir, self.system_id, self.thread_id) or {}
        )
        base_meta["created_at"] = self.meta.get("created_at") or base_meta.get("created_at")
        base_meta["previous_response_id"] = self.previous_response_id
        base_meta["backend_api_mode"] = self.backend_api_mode
        base_meta["messages_count"] = self.persisted_messages_count
        if isinstance(self.meta.get("shared"), dict):
            base_meta["shared"] = self.meta["shared"]
        if isinstance(self.meta.get("last_backend_response_id"), str):
            base_meta["last_backend_response_id"] = self.meta.get("last_backend_response_id")
        if isinstance(self.meta.get("user_messages_log"), list):
            base_meta["user_messages_log"] = [
                str(x).strip()
                for x in self.meta.get("user_messages_log", [])
                if str(x).strip()
            ]
        if isinstance(self.meta.get("current_user_messages"), list):
            base_meta["current_user_messages"] = [
                str(x).strip()
                for x in self.meta.get("current_user_messages", [])
                if str(x).strip()
            ]
        # Persist rendered chat/completions system prompt for transparency/debugging.
        # Must stay identical to актуальным данным: when we are in `responses`
        # mode, there is no system prompt in messages, so it must not be present in meta.
        if self.backend_api_mode == "chat_completions":
            prompt = self.meta.get("chat_completions_system_prompt_rendered")
            if isinstance(prompt, str) and prompt.strip():
                base_meta["chat_completions_system_prompt_rendered"] = prompt
        else:
            base_meta.pop("chat_completions_system_prompt_rendered", None)
        save_thread_meta(self.threads_dir, self.system_id, self.thread_id, base_meta)

    def archive_and_compact_summary_payload(
        self,
        summary_payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        summary_text = str(summary_payload.get("summary_message_content") or "").strip()
        user_messages_log = summary_payload.get("user_messages_log")
        dialog_steps_offset = summary_payload.get("dialog_steps_offset", 0)
        data_path, meta_path = _thread_paths(self.threads_dir, self.system_id, self.thread_id)
        ts_suffix = _utc_now_suffix()
        archive_data_path = data_path.replace(".jsonl", f"__pre_summary__{ts_suffix}.jsonl")
        archive_meta_path = meta_path.replace(".json", f"__pre_summary__{ts_suffix}.json")

        data_exists = os.path.exists(data_path)
        meta_exists = os.path.exists(meta_path)
        moved_data = False
        moved_meta = False
        try:
            if data_exists:
                os.replace(data_path, archive_data_path)
                moved_data = True
            if meta_exists:
                os.replace(meta_path, archive_meta_path)
                moved_meta = True
        except Exception:
            if moved_meta and not os.path.exists(meta_path):
                os.replace(archive_meta_path, meta_path)
            if moved_data and not os.path.exists(data_path):
                os.replace(archive_data_path, data_path)
            raise

        summary_message = {
            "role": "user",
            "content": wrap_technical_user_content(
                (
                    "Summarized context from previous dialogue.\n"
                    f"{summary_text}"
                ),
                technical_type=TECHNICAL_TYPE_DIALOG_SUMMARY,
            ),
            "id": generate_message_id_func("m_sum"),
        }
        compact_messages: List[Dict[str, Any]] = [summary_message]

        known_message_keys: Set[str] = set()
        seq = 0
        with open(data_path, "w", encoding="utf-8") as fd:
            for message in compact_messages:
                seq += 1
                record = {
                    "type": "message",
                    "thread_id": self.thread_id,
                    "system": self.system_id,
                    "seq": seq,
                    "ts": _utc_now_iso(),
                    "payload": message,
                }
                fd.write(json.dumps(record, ensure_ascii=False) + "\n")
                known_message_keys.add(make_message_key(message))

        existing_meta = dict(self.meta) if isinstance(self.meta, dict) else {}
        shared_data = summary_payload.get("shared")
        now_iso = _utc_now_iso()
        new_meta = dict(existing_meta)
        new_meta.update(
            {
                "created_at": existing_meta.get("created_at") or now_iso,
                "previous_response_id": None,
                "backend_api_mode": self.backend_api_mode,
                "messages_count": len(compact_messages),
                "last_summarized_at": now_iso,
                "last_summary_archive_suffix": ts_suffix,
            }
        )
        if isinstance(shared_data, dict):
            new_meta["shared"] = shared_data
        try:
            effective_steps_offset = int(dialog_steps_offset)
        except (TypeError, ValueError):
            effective_steps_offset = int(existing_meta.get("dialog_steps_offset", 0) or 0)
        if effective_steps_offset < 0:
            effective_steps_offset = 0
        new_meta["dialog_steps_offset"] = effective_steps_offset
        if isinstance(user_messages_log, list):
            new_meta["user_messages_log"] = [
                str(x).strip() for x in user_messages_log if str(x).strip()
            ]
        elif isinstance(existing_meta.get("user_messages_log"), list):
            new_meta["user_messages_log"] = [
                str(x).strip()
                for x in existing_meta.get("user_messages_log", [])
                if str(x).strip()
            ]
        if isinstance(existing_meta.get("current_user_messages"), list):
            new_meta["current_user_messages"] = [
                str(x).strip()
                for x in existing_meta.get("current_user_messages", [])
                if str(x).strip()
            ]
        prompt = existing_meta.get("chat_completions_system_prompt_rendered")
        if isinstance(prompt, str) and prompt.strip():
            new_meta["chat_completions_system_prompt_rendered"] = prompt
        last_backend_response_id = existing_meta.get("last_backend_response_id")
        if isinstance(last_backend_response_id, str) and last_backend_response_id.strip():
            new_meta["last_backend_response_id"] = last_backend_response_id
        save_thread_meta(self.threads_dir, self.system_id, self.thread_id, new_meta)

        self.history_messages = compact_messages
        self.runtime_messages = []
        self.known_message_keys = known_message_keys
        self.persisted_messages_count = len(compact_messages)
        self.previous_response_id = None
        self.meta = new_meta
        return {
            "archive_suffix": ts_suffix,
            "archive_data_path": archive_data_path if data_exists else None,
            "archive_meta_path": archive_meta_path if meta_exists else None,
            "new_messages_count": len(compact_messages),
            "newly_summarized_message_id": summary_message.get("id"),
        }
