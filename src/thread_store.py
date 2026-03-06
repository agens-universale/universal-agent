import json
import os
import re
from datetime import datetime, timezone
from typing import Dict, List, Tuple, Any, Set, Optional


_THREAD_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
_META_SCHEMA_VERSION = 1


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def make_message_key(message: Dict[str, Any]) -> str:
    msg_id = message.get("id")
    if isinstance(msg_id, str) and len(msg_id) > 0:
        return f"id:{msg_id}"
    payload = json.dumps(message, ensure_ascii=False, sort_keys=True)
    return f"payload:{payload}"


def validate_thread_id(thread_id: str) -> str:
    if not isinstance(thread_id, str) or len(thread_id.strip()) == 0:
        raise ValueError("thread_id должен быть непустой строкой")
    normalized = thread_id.strip()
    if not _THREAD_ID_PATTERN.match(normalized):
        raise ValueError(
            "thread_id может содержать только латинские буквы, цифры, '_' и '-'"
        )
    return normalized


def resolve_threads_dir(config_dict: Dict[str, Any]) -> str:
    """
    Определить директорию для хранения тредов.

    - Если threads_data_path не задан или пуст — использовать <project_root>/threads.
    - Если задан threads_data_path — он ДОЛЖЕН быть абсолютным путём и используется как есть.
    """
    # Корень проекта: ../ относительно этого файла (src/thread_store.py -> project_root)
    base_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.abspath(os.path.join(base_dir, ".."))

    path_cfg = config_dict.get("threads_data_path")
    if not isinstance(path_cfg, str) or len(path_cfg.strip()) == 0:
        return os.path.join(project_root, "threads")

    path_cfg = path_cfg.strip()
    if not os.path.isabs(path_cfg):
        raise ValueError(
            f"threads_data_path должен быть абсолютным путём, получено: {path_cfg!r}"
        )
    return path_cfg


def _thread_paths(threads_dir: str, thread_id: str) -> Tuple[str, str]:
    normalized = validate_thread_id(thread_id)
    data_path = os.path.join(threads_dir, f"data_{normalized}.jsonl")
    meta_path = os.path.join(threads_dir, f"meta_{normalized}.json")
    return data_path, meta_path


def ensure_threads_dir(threads_dir: str) -> None:
    os.makedirs(threads_dir, exist_ok=True)


def load_thread_messages(
    threads_dir: str, thread_id: str
) -> Tuple[List[Dict[str, Any]], Set[str], int]:
    data_path, _ = _thread_paths(threads_dir, thread_id)
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
            if record.get("type") != "message":
                continue
            seq = record.get("seq")
            if isinstance(seq, int) and seq > max_seq:
                max_seq = seq
            payload = record.get("payload")
            if not isinstance(payload, dict):
                continue
            messages.append(payload)
            message_keys.add(make_message_key(payload))

    return messages, message_keys, max_seq


def load_thread_meta(threads_dir: str, thread_id: str) -> Dict[str, Any]:
    _, meta_path = _thread_paths(threads_dir, thread_id)
    if not os.path.exists(meta_path):
        return {}
    with open(meta_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        return {}
    return data


def save_thread_meta(threads_dir: str, thread_id: str, meta: Dict[str, Any]) -> None:
    _, meta_path = _thread_paths(threads_dir, thread_id)
    ensure_threads_dir(threads_dir)

    to_save = dict(meta)
    to_save["thread_id"] = validate_thread_id(thread_id)
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
    thread_id: str,
    messages: List[Dict[str, Any]],
    known_message_keys: Set[str],
    *,
    start_seq: int = 0,
) -> int:
    if len(messages) == 0:
        return 0

    ensure_threads_dir(threads_dir)
    data_path, _ = _thread_paths(threads_dir, thread_id)

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
        thread_id: str,
        history_messages: List[Dict[str, Any]],
        known_message_keys: Set[str],
        persisted_messages_count: int,
        meta: Dict[str, Any],
        previous_response_id: Optional[str],
    ):
        self.threads_dir = threads_dir
        self.thread_id = thread_id
        self.history_messages = history_messages
        self.known_message_keys = known_message_keys
        self.persisted_messages_count = persisted_messages_count
        self.meta = meta
        self.previous_response_id = previous_response_id
        self.runtime_messages: List[Dict[str, Any]] = []

    @classmethod
    def from_config(
        cls, *, config_dict: Dict[str, Any], thread_id: str
    ) -> "ThreadSession":
        normalized_thread_id = validate_thread_id(thread_id)
        threads_dir = resolve_threads_dir(config_dict)
        ensure_threads_dir(threads_dir)

        meta = load_thread_meta(threads_dir, normalized_thread_id)
        history_messages, known_message_keys, max_seq = load_thread_messages(
            threads_dir, normalized_thread_id
        )
        previous_response_id = meta.get("previous_response_id")
        if not isinstance(previous_response_id, str) or len(previous_response_id) == 0:
            previous_response_id = None

        return cls(
            threads_dir=threads_dir,
            thread_id=normalized_thread_id,
            history_messages=history_messages,
            known_message_keys=known_message_keys,
            persisted_messages_count=max_seq,
            meta=meta,
            previous_response_id=previous_response_id,
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
        batch_messages: List[Dict[str, Any]],
        *,
        previous_response_id: Optional[str],
    ) -> None:
        written = append_new_thread_messages(
            self.threads_dir,
            self.thread_id,
            batch_messages,
            self.known_message_keys,
            start_seq=self.persisted_messages_count,
        )
        self.persisted_messages_count += written
        self.previous_response_id = previous_response_id
        save_thread_meta(
            self.threads_dir,
            self.thread_id,
            {
                "created_at": self.meta.get("created_at"),
                "previous_response_id": self.previous_response_id,
                "messages_count": self.persisted_messages_count,
            },
        )
