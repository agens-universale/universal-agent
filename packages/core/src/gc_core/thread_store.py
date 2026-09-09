import glob
import json
import os
import re
import threading
from datetime import datetime, timezone
from typing import Dict, List, Tuple, Any, Set, Optional

from gc_core.message_policy.tool_usage_notice.state import merge_tool_usage_notice_meta_to_base
from gc_core.intent_log.serialize import deserialize_list, serialize_for_meta
from gc_core.persistence.live_volume import bump_live_volume_for_messages, live_volume_from_messages, merge_tui_into_meta
from gc_core.persistence.meta_ownership import resolve_runtime_flag_name
from gc_core.persistence.thread_meta_mapper import ThreadMetaMapper
from gc_core.message_policy.user_message_policy import TECHNICAL_TYPE_DIALOG_SUMMARY, wrap_technical_user_content
from gc_core.utils.generate_message_id import generate_msg_id_sha256 as generate_message_id_func
from gc_core.utils.atomic_io import atomic_replace
from gc_core.utils.aio_compat import await_callback


_THREAD_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
_SYSTEM_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
_META_SCHEMA_VERSION = 2

# Process-local счётчик последнего выданного seq sidecar (SPEC 2026-07-30-sidecar-append-o1-seq).
# Ключ — нормализованный путь jsonl. Cold init на первый append для path; далее O(1).
_sidecar_last_seq: Dict[str, int] = {}
_sidecar_seq_lock = threading.Lock()


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


def subagent_trace_path(threads_dir: str, system_id: str, thread_id: str) -> str:
    """Путь sidecar nested-трейса: data_subagents_{system}_{thread_id}.jsonl."""
    sys_norm = sanitize_system_id(system_id)
    tid = validate_thread_id(thread_id)
    return os.path.join(threads_dir, f"data_subagents_{sys_norm}_{tid}.jsonl")


def touched_journal_path(threads_dir: str, system_id: str, thread_id: str) -> str:
    """Путь per-trace Diff journal: touched_<system>_<thread_id>.json."""
    sys_norm = sanitize_system_id(system_id)
    tid = validate_thread_id(thread_id)
    return os.path.join(threads_dir, f"touched_{sys_norm}_{tid}.json")


def ensure_threads_dir(threads_dir: str) -> None:
    os.makedirs(threads_dir, exist_ok=True)
    os.makedirs(os.path.join(threads_dir, "locks"), exist_ok=True)


def _lock_path(threads_dir: str, system_id: str, thread_id: str) -> str:
    """Путь lease-файла TUI session lock в подпапке locks/ (без legacy)."""
    sys_norm = sanitize_system_id(system_id)
    tid = validate_thread_id(thread_id)
    return os.path.join(threads_dir, "locks", f"lock_{sys_norm}_{tid}.json")


def reset_thread_files(threads_dir: str, system_id: str, thread_id: str) -> List[str]:
    """
    Удалить файлы конкретного треда (data/meta/lock/sidecar), включая legacy data/meta.
    Возвращает список удалённых путей.
    """
    sys_norm = sanitize_system_id(system_id)
    normalized_thread_id = validate_thread_id(thread_id)
    paths = []
    paths.extend(_thread_paths(threads_dir, sys_norm, normalized_thread_id))
    paths.extend(_thread_paths_legacy(threads_dir, normalized_thread_id))
    sidecar = subagent_trace_path(threads_dir, sys_norm, normalized_thread_id)
    paths.append(sidecar)
    paths.append(touched_journal_path(threads_dir, sys_norm, normalized_thread_id))
    paths.append(_lock_path(threads_dir, sys_norm, normalized_thread_id))

    deleted: List[str] = []
    for path in paths:
        if os.path.exists(path):
            os.remove(path)
            deleted.append(path)
    # Сброс process-local счётчика sidecar seq, иначе recreate получит «дырявый» seq (S5).
    with _sidecar_seq_lock:
        _sidecar_last_seq.pop(sidecar, None)
    return deleted


def _thread_archive_paths(threads_dir: str, system_id: str, thread_id: str) -> List[str]:
    """Пути __pre_summary__-архивов треда (data+meta), созданных суммаризацией.

    Legacy ``touched_*__pre_summary__*`` тоже подхватываем при delete — раньше
    journal ошибочно архивировали вместе с data/meta.
    """
    sys_norm = sanitize_system_id(system_id)
    tid = validate_thread_id(thread_id)
    data_glob = os.path.join(threads_dir, f"data_{sys_norm}_{tid}__pre_summary__*.jsonl")
    meta_glob = os.path.join(threads_dir, f"meta_{sys_norm}_{tid}__pre_summary__*.json")
    touched_glob = os.path.join(
        threads_dir, f"touched_{sys_norm}_{tid}__pre_summary__*.json"
    )
    return (
        sorted(glob.glob(data_glob))
        + sorted(glob.glob(meta_glob))
        + sorted(glob.glob(touched_glob))
    )


def delete_thread(threads_dir: str, system_id: str, thread_id: str) -> List[str]:
    """Полностью удалить тред: canonical + legacy + lock + sidecar + touched + __pre_summary__.

    Возвращает список удалённых путей. Отсутствие файла — no-op.
    """
    paths: List[str] = []
    paths.extend(_thread_paths(threads_dir, system_id, thread_id))
    paths.extend(_thread_paths_legacy(threads_dir, thread_id))
    paths.extend(_thread_archive_paths(threads_dir, system_id, thread_id))
    sidecar = subagent_trace_path(threads_dir, system_id, thread_id)
    paths.append(sidecar)
    paths.append(touched_journal_path(threads_dir, system_id, thread_id))
    paths.append(_lock_path(threads_dir, system_id, thread_id))

    deleted: List[str] = []
    for path in paths:
        if os.path.exists(path):
            os.remove(path)
            deleted.append(path)
    # Сброс process-local счётчика sidecar seq, иначе recreate получит «дырявый» seq (S5).
    with _sidecar_seq_lock:
        _sidecar_last_seq.pop(sidecar, None)
    return deleted


def _max_subagent_trace_seq(path: str) -> int:
    """Максимальный seq в sidecar; битые строки пропускаем."""
    if not os.path.exists(path):
        return 0
    max_seq = 0
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            row = line.strip()
            if not row:
                continue
            try:
                record = json.loads(row)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            seq = record.get("seq")
            if isinstance(seq, int) and seq > max_seq:
                max_seq = seq
    return max_seq


def append_subagent_trace_record(
    threads_dir: str,
    system_id: str,
    thread_id: str,
    *,
    parent_call_id: str,
    child_run_id: str,
    kind: str,
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    """Append-only запись в sidecar nested-трейса (flat envelope, монотонный seq).

    Hot path O(1): cold init (один скан max seq) только на первый append для path
    в жизни процесса, далее seq = last_seq + 1 без чтения файла. Сериализация writers
    одним модулем lock-ом, чтобы параллельные child-субагенты не получили дубль seq.
    (SPEC 2026-07-30-sidecar-append-o1-seq, S2-S4).
    """
    ensure_threads_dir(threads_dir)
    path = subagent_trace_path(threads_dir, system_id, thread_id)
    with _sidecar_seq_lock:
        if path not in _sidecar_last_seq:
            _sidecar_last_seq[path] = _max_subagent_trace_seq(path)  # cold init: 0 если файла нет
        seq = _sidecar_last_seq[path] + 1
        _sidecar_last_seq[path] = seq  # bump ДО write: упавший write даёт дырку, не дубль seq
        record = {
            "ts": _utc_now_iso(),
            "seq": seq,
            "parent_call_id": parent_call_id,
            "child_run_id": child_run_id,
            "kind": kind,
            "payload": payload,
        }
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


def read_subagent_trace(
    threads_dir: str, system_id: str, thread_id: str
) -> List[Dict[str, Any]]:
    """Прочитать все записи sidecar в порядке файла. Нет файла → []."""
    path = subagent_trace_path(threads_dir, system_id, thread_id)
    if not os.path.exists(path):
        return []
    records: List[Dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            row = line.strip()
            if not row:
                continue
            try:
                record = json.loads(row)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                records.append(record)
    return records


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

    to_save = ThreadMetaMapper.ensure_v2_meta(dict(meta))
    to_save["thread_id"] = validate_thread_id(thread_id)
    to_save["system"] = sys_norm
    to_save["schema_version"] = _META_SCHEMA_VERSION
    to_save["updated_at"] = _utc_now_iso()
    if not to_save.get("created_at"):
        to_save["created_at"] = to_save["updated_at"]

    tmp_path = f"{meta_path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(to_save, f, ensure_ascii=False, indent=2)
    atomic_replace(tmp_path, meta_path)


def update_tui_meta(
    threads_dir: str, system_id: str, thread_id: str, patch: Dict[str, Any]
) -> None:
    """Слить patch в meta.tui (read-merge-write). Только TUI-owned ключи."""
    sys_norm = sanitize_system_id(system_id)
    meta = load_thread_meta(threads_dir, sys_norm, thread_id) or {}
    tui_meta = dict(meta.get("tui") or {})
    tui_meta.update(patch)
    meta["tui"] = tui_meta
    save_thread_meta(threads_dir, sys_norm, thread_id, meta)


def update_runtime_meta(
    threads_dir: str, system_id: str, thread_id: str, patch: Dict[str, Any]
) -> None:
    """Слить patch в meta.runtime (read-merge-write). Только runtime-owned ключи."""
    from gc_core.persistence.meta_ownership import merge_runtime_into_meta

    sys_norm = sanitize_system_id(system_id)
    meta = load_thread_meta(threads_dir, sys_norm, thread_id) or {}
    merge_runtime_into_meta(meta, patch)
    save_thread_meta(threads_dir, sys_norm, thread_id, meta)


def patch_runtime_flag(
    threads_dir: str,
    system_id: str,
    thread_id: str,
    flag: str,
    *,
    active: bool,
    session_meta: Optional[Dict[str, Any]] = None,
) -> None:
    """Точечный patch ``meta.runtime.<flag>`` без clobber ``meta.tui`` (I1).

    ``flag``: ``summarizing``|``finalizing``|``finish_gate_checking``.
    """
    sys_norm = sanitize_system_id(system_id)
    name = resolve_runtime_flag_name(flag)
    disk = load_thread_meta(threads_dir, sys_norm, thread_id) or {}
    runtime = dict(disk.get("runtime") or {})
    if active:
        runtime[name] = True
    else:
        runtime.pop(name, None)
    if runtime:
        disk["runtime"] = runtime
    else:
        disk.pop("runtime", None)

    if session_meta is not None:
        if runtime:
            session_meta["runtime"] = dict(runtime)
        else:
            session_meta.pop("runtime", None)
        disk_tui = disk.get("tui")
        if isinstance(disk_tui, dict):
            session_meta["tui"] = dict(disk_tui)
        elif "tui" in session_meta:
            session_meta.pop("tui", None)
            disk.pop("tui", None)

    save_thread_meta(threads_dir, sys_norm, thread_id, disk)


def discard_message_by_id(
    threads_dir: str, system_id: str, thread_id: str, message_id: str
) -> bool:
    """Удалить message-запись с указанным ``payload.id`` из data_*.jsonl.

    Для отката осиротевшей user-реплики (быстрая отмена до вывода агента).
    Вызывать только когда на тред нет активного run'а (нет конкурентной
    дозаписи) — перезапись атомарна (tmp + os.replace). Декрементит
    ``messages_count`` в meta. Возвращает True, если что-то удалено.
    """
    if not message_id:
        return False
    sys_norm = sanitize_system_id(system_id)
    data_path, _ = _thread_paths(threads_dir, sys_norm, thread_id)
    if not os.path.exists(data_path):
        return False

    kept: List[str] = []
    removed = 0
    with open(data_path, "r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                kept.append(line.rstrip("\n"))
                continue
            payload = rec.get("payload") if isinstance(rec, dict) else None
            if (
                isinstance(rec, dict)
                and rec.get("type") == "message"
                and isinstance(payload, dict)
                and payload.get("id") == message_id
            ):
                removed += 1
                continue
            kept.append(line.rstrip("\n"))

    if removed == 0:
        return False

    tmp_path = f"{data_path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        for row in kept:
            f.write(row + "\n")
    atomic_replace(tmp_path, data_path)

    meta = load_thread_meta(threads_dir, sys_norm, thread_id) or {}
    try:
        meta["messages_count"] = max(0, int(meta.get("messages_count", 0)) - removed)
    except (TypeError, ValueError):
        pass
    live_msgs, _, _ = load_thread_messages(threads_dir, sys_norm, thread_id)
    merge_tui_into_meta(meta, live_volume_from_messages(live_msgs))
    save_thread_meta(threads_dir, sys_norm, thread_id, meta)
    return True


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
    written_payloads: List[Dict[str, Any]] = []
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
            written_payloads.append(message)
            wrote += 1
    # Счётчики живого объёма ЗДЕСЬ НЕ БАМПАЮТСЯ: это делает `ThreadSession`,
    # одинаково для любого хранилища. Пока бампал файловый слой, у хранилища
    # в базе ключей `live_*` не появлялось вовсе, а TUI их читает.
    return wrote


def archive_and_replace_thread_files(
    threads_dir: str,
    system_id: str,
    thread_id: str,
    messages: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Отложить файлы треда в архив и записать вместо них ``messages``.

    Файловая часть компакции. Жила прямо в ``ThreadSession`` и работала с
    ``self.threads_dir`` мимо контракта: у хранилища без файлов эта строка
    пустая, и запись уходила в относительный путь от текущего каталога — молча,
    без ошибки. Здесь она стала необязательной СПОСОБНОСТЬЮ файлового
    хранилища, а сессия, не найдя её, замещает историю через ``reset`` +
    ``append_messages``.

    Перенос файлов — с откатом: если переехал только meta, он возвращается на
    место, иначе тред остался бы без метаданных при целой истории.
    """
    data_path, meta_path = _thread_paths(threads_dir, system_id, thread_id)
    ts_suffix = _utc_now_suffix()
    archive_data_path = data_path.replace(".jsonl", f"__pre_summary__{ts_suffix}.jsonl")
    archive_meta_path = meta_path.replace(".json", f"__pre_summary__{ts_suffix}.json")

    data_exists = os.path.exists(data_path)
    meta_exists = os.path.exists(meta_path)
    moved_data = False
    moved_meta = False
    try:
        if data_exists:
            atomic_replace(data_path, archive_data_path)
            moved_data = True
        if meta_exists:
            atomic_replace(meta_path, archive_meta_path)
            moved_meta = True
    except Exception:
        if moved_meta and not os.path.exists(meta_path):
            atomic_replace(archive_meta_path, meta_path)
        if moved_data and not os.path.exists(data_path):
            atomic_replace(archive_data_path, data_path)
        raise

    seq = 0
    with open(data_path, "w", encoding="utf-8") as fd:
        for message in messages:
            seq += 1
            record = {
                "type": "message",
                "thread_id": thread_id,
                "system": sanitize_system_id(system_id),
                "seq": seq,
                "ts": _utc_now_iso(),
                "payload": message,
            }
            fd.write(json.dumps(record, ensure_ascii=False) + "\n")

    return {
        "archive_suffix": ts_suffix,
        "archive_data_path": archive_data_path if data_exists else None,
        "archive_meta_path": archive_meta_path if meta_exists else None,
    }


class ThreadSession:
    def __init__(
        self,
        *,
        threads_dir: str,
        system_id: str,
        store: Any = None,
        thread_id: str,
        history_messages: List[Dict[str, Any]],
        known_message_keys: Set[str],
        persisted_messages_count: int,
        meta: Dict[str, Any],
        previous_response_id: Optional[str],
        backend_api_mode: str,
    ):
        self.threads_dir = threads_dir
        # Хранилище сессии. None — файловое по умолчанию: сессию собирают и
        # напрямую (тесты, ручные сценарии), и ломать их не за что.
        if store is None:
            from gc_core.thread_store_file import FileThreadStore

            store = FileThreadStore(threads_dir)
        self.store = store
        self.system_id = sanitize_system_id(system_id)
        self.thread_id = thread_id
        self.history_messages = history_messages
        self.known_message_keys = known_message_keys
        self.persisted_messages_count = persisted_messages_count
        self.meta = meta
        self.previous_response_id = previous_response_id
        self.backend_api_mode = backend_api_mode
        self.runtime_messages: List[Dict[str, Any]] = []
        # Снимок known_message_keys до записи последнего step_messages (для compaction user_ask).
        self.known_keys_snapshot_before_last_batch: Optional[frozenset[str]] = None
        self._port: Any = None

    @property
    def port(self) -> Any:
        """Асинхронный порт хранилища. Резолвится ОДИН раз, при первом обращении.

        Лениво, а не в ``__init__``, намеренно: синхронные вызывающие (тесты,
        ручные сценарии, TUI) не должны платить за проверку, которая им не
        нужна. Громкий отказ на негодном хранилище стоит в
        ``create_run_context`` — это и есть точка проводки, где встраивающий
        передаёт своё хранилище.
        """
        if self._port is None:
            from gc_core.thread_store_async import athread_store

            self._port = athread_store(self.store)
        return self._port

    @classmethod
    def from_config(
        cls,
        *,
        config_dict: Dict[str, Any],
        system_id: str,
        thread_id: str,
        store: Any = None,
    ) -> "ThreadSession":
        """Загрузить тред. ``store`` не передан — файловое хранилище, как раньше.

        Параметр — точка подмены для встраивающего: своя реализация
        Хранилище (например поверх базы) передаётся сюда, и сессия больше
        нигде диска не касается. Дефолт сохраняет прежнее поведение полностью.
        """
        sys_norm = sanitize_system_id(system_id)
        normalized_thread_id = validate_thread_id(thread_id)

        store, threads_dir = cls._resolve_store(config_dict, store)

        meta = store.load_meta(sys_norm, normalized_thread_id)
        state = store.load_thread_state(sys_norm, normalized_thread_id)
        return cls._build(
            threads_dir=threads_dir, store=store, sys_norm=sys_norm,
            thread_id=normalized_thread_id, meta=meta, state=state,
        )

    @classmethod
    async def afrom_config(
        cls,
        *,
        config_dict: Dict[str, Any],
        system_id: str,
        thread_id: str,
        store: Any = None,
    ) -> "ThreadSession":
        """То же, что ``from_config``, но хранилище ждётся нативно.

        Этим путём тред читает ЦИКЛ АГЕНТА. Синхронный двойник остаётся для
        прямых вызывающих вне цикла.
        """
        from gc_core.thread_store_async import athread_store

        sys_norm = sanitize_system_id(system_id)
        normalized_thread_id = validate_thread_id(thread_id)
        store, threads_dir = cls._resolve_store(config_dict, store)

        port = athread_store(store)
        meta = await port.aload_meta(sys_norm, normalized_thread_id)
        state = await port.aload_thread_state(sys_norm, normalized_thread_id)
        session = cls._build(
            threads_dir=threads_dir, store=store, sys_norm=sys_norm,
            thread_id=normalized_thread_id, meta=meta, state=state,
        )
        session._port = port          # порт уже разрешён — не резолвить второй раз
        return session

    @staticmethod
    def _resolve_store(config_dict: Dict[str, Any], store: Any) -> "tuple[Any, str]":
        """Хранилище и каталог. Чистая часть, общая для sync и async."""
        if store is not None:
            return store, getattr(store, "threads_dir", "")
        # Путь к каталогу нужен ТОЛЬКО файловому хранилищу. Резолвить его
        # безусловно значило бы требовать `threads_data_path` от того, кто
        # держит историю в базе и файлов не заводит вовсе — а он и не
        # обязан его настраивать. Поймано гейтом на in-memory хранилище.
        from gc_core.thread_store_file import FileThreadStore

        threads_dir = resolve_threads_dir(config_dict)
        store = FileThreadStore(threads_dir)
        store.ensure_ready()
        return store, threads_dir

    @classmethod
    def _build(
        cls, *, threads_dir: str, store: Any, sys_norm: str, thread_id: str,
        meta: Dict[str, Any], state: Any,
    ) -> "ThreadSession":
        """Собрать сессию из прочитанного. Чистая часть, общая для sync и async."""
        history_messages, known_message_keys, max_seq = state
        previous_response_id = meta.get("previous_response_id")
        if not isinstance(previous_response_id, str) or len(previous_response_id) == 0:
            previous_response_id = None
        backend_api_mode_raw = meta.get("backend_api_mode", "responses")
        backend_api_mode = str(backend_api_mode_raw).strip().lower()
        if backend_api_mode not in {"responses", "chat_completions"}:
            backend_api_mode = "responses"

        return cls(
            threads_dir=threads_dir,
            store=store,
            system_id=sys_norm,
            thread_id=thread_id,
            history_messages=history_messages,
            known_message_keys=known_message_keys,
            persisted_messages_count=max_seq,
            meta=meta,
            previous_response_id=previous_response_id,
            backend_api_mode=backend_api_mode,
        )


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

    def _persist_batch_begin(self, step_messages: List[Dict[str, Any]]) -> Set[str]:
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
                m for m in step_messages
                if make_message_key(m) not in known_before
            ]
            if fresh:
                bump_live_volume_for_messages(base_meta, fresh)
        base_meta["created_at"] = self.meta.get("created_at") or base_meta.get("created_at")
        base_meta["previous_response_id"] = self.previous_response_id
        base_meta["backend_api_mode"] = self.backend_api_mode
        base_meta["messages_count"] = self.persisted_messages_count
        if isinstance(self.meta.get("shared"), dict):
            base_meta["shared"] = self.meta["shared"]
        if isinstance(self.meta.get("last_backend_response_id"), str):
            base_meta["last_backend_response_id"] = self.meta.get("last_backend_response_id")
        if isinstance(self.meta.get("user_intent_log"), list):
            base_meta["user_intent_log"] = serialize_for_meta(
                deserialize_list(self.meta.get("user_intent_log"))
            )
        if isinstance(self.meta.get("current_user_intent"), list):
            base_meta["current_user_intent"] = serialize_for_meta(
                deserialize_list(self.meta.get("current_user_intent"))
            )
        if "user_turn_seq" in self.meta:
            base_meta["user_turn_seq"] = int(self.meta.get("user_turn_seq") or 0)
        if "dialog_steps_offset" in self.meta:
            base_meta["dialog_steps_offset"] = int(self.meta.get("dialog_steps_offset") or 0)
        if isinstance(self.meta.get("token_usage"), dict):
            base_meta["token_usage"] = self.meta["token_usage"]
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

    def persist_batch(
        self,
        step_messages: List[Dict[str, Any]],
        *,
        previous_response_id: Optional[str],
    ) -> None:
        known_before = self._persist_batch_begin(step_messages)
        written = self.store.append_messages(
            self.system_id,
            self.thread_id,
            step_messages,
            known_keys=self.known_message_keys,
            start_seq=self.persisted_messages_count,
        )
        self._persist_batch_apply(
            step_messages, known_before, written, previous_response_id
        )
        # Preserve arbitrary meta fields (e.g. shared.working_memory), otherwise tools that
        # update meta would get their data wiped on the next persist_batch().
        base_meta = self.store.load_meta(self.system_id, self.thread_id) or {}
        self.store.save_meta(
            self.system_id,
            self.thread_id,
            self._persist_batch_meta(base_meta, step_messages, known_before),
        )

    async def apersist_batch(
        self,
        step_messages: List[Dict[str, Any]],
        *,
        previous_response_id: Optional[str],
    ) -> None:
        """То же, что ``persist_batch``, но хранилище ждётся нативно.

        Этим путём ходит ЦИКЛ АГЕНТА — самый горячий шов контракта: раз за
        итерацию ``while True`` и раз за тул-колл. Синхронный двойник остаётся
        для прямых вызывающих вне цикла (TUI, тесты, ручные сценарии).
        """
        known_before = self._persist_batch_begin(step_messages)
        written = await self.port.aappend_messages(
            self.system_id,
            self.thread_id,
            step_messages,
            known_keys=self.known_message_keys,
            start_seq=self.persisted_messages_count,
        )
        self._persist_batch_apply(
            step_messages, known_before, written, previous_response_id
        )
        base_meta = await self.port.aload_meta(self.system_id, self.thread_id) or {}
        await self.port.asave_meta(
            self.system_id,
            self.thread_id,
            self._persist_batch_meta(base_meta, step_messages, known_before),
        )

    def _compact_summary_message(self, summary_payload: Dict[str, Any]) -> Dict[str, Any]:
        """Сообщение-сводка, которым замещается история. Чистая часть."""
        summary_text = str(summary_payload.get("summary_message_content") or "").strip()
        return {
            "role": "user",
            "content": wrap_technical_user_content(
                f"Summarized context from previous dialogue.\n{summary_text}",
                technical_type=TECHNICAL_TYPE_DIALOG_SUMMARY,
            ),
            "id": generate_message_id_func("m_sum"),
        }

    def _compact_meta(
        self,
        summary_payload: Dict[str, Any],
        compact_messages: List[Dict[str, Any]],
        disk_meta: Dict[str, Any],
        ts_suffix: str,
    ) -> Dict[str, Any]:
        """Собрать meta после компакции. Чистая часть, общая для sync и async."""
        existing_meta = dict(self.meta) if isinstance(self.meta, dict) else {}
        user_intent_log = summary_payload.get("user_intent_log")
        dialog_steps_offset = summary_payload.get("dialog_steps_offset", 0)
        disk_tui = dict(disk_meta.get("tui") or {})
        disk_runtime = dict(disk_meta.get("runtime") or {})
        shared_data = summary_payload.get("shared")
        now_iso = _utc_now_iso()
        # created_at — birth of the thread; never reset on summary.
        # Prefer in-memory, then disk snapshot taken before archive move
        # (self.meta can lag when save_thread_meta minted created_at on disk only).
        created_at = (
            existing_meta.get("created_at")
            or disk_meta.get("created_at")
            or now_iso
        )
        new_meta = dict(existing_meta)
        new_meta.update(
            {
                "created_at": created_at,
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
        if isinstance(user_intent_log, list):
            new_meta["user_intent_log"] = serialize_for_meta(
                deserialize_list(user_intent_log)
            )
        elif isinstance(existing_meta.get("user_intent_log"), list):
            new_meta["user_intent_log"] = serialize_for_meta(
                deserialize_list(existing_meta.get("user_intent_log"))
            )
        if isinstance(existing_meta.get("current_user_intent"), list):
            new_meta["current_user_intent"] = serialize_for_meta(
                deserialize_list(existing_meta.get("current_user_intent"))
            )
        user_turn_seq = summary_payload.get("user_turn_seq")
        if user_turn_seq is not None:
            new_meta["user_turn_seq"] = int(user_turn_seq or 0)
        elif "user_turn_seq" in existing_meta:
            new_meta["user_turn_seq"] = int(existing_meta.get("user_turn_seq") or 0)
        prompt = existing_meta.get("chat_completions_system_prompt_rendered")
        if isinstance(prompt, str) and prompt.strip():
            new_meta["chat_completions_system_prompt_rendered"] = prompt
        last_backend_response_id = existing_meta.get("last_backend_response_id")
        if isinstance(last_backend_response_id, str) and last_backend_response_id.strip():
            new_meta["last_backend_response_id"] = last_backend_response_id
        # meta.tui: disk wins (TUI-owned). meta.runtime: merge + live_* пересчёт.
        session_tui = dict(existing_meta.get("tui") or {})
        new_meta["tui"] = {**session_tui, **disk_tui}
        session_runtime = dict(existing_meta.get("runtime") or {})
        merged_runtime = {**session_runtime, **disk_runtime}
        merged_runtime.update(live_volume_from_messages(compact_messages))
        new_meta["runtime"] = merged_runtime
        return new_meta

    def _compact_apply(
        self,
        compact_messages: List[Dict[str, Any]],
        known_message_keys: Set[str],
        new_meta: Dict[str, Any],
    ) -> None:
        """Перевести сессию в состояние после компакции. Чистая часть."""
        self.history_messages = compact_messages
        self.runtime_messages = []
        self.known_message_keys = known_message_keys
        self.persisted_messages_count = len(compact_messages)
        self.previous_response_id = None
        self.meta = new_meta

    def _archive_via_store(
        self, compact_messages: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Замещение истории без архива — для хранилищ без файлов.

        Резервный путь, когда хранилище не объявляет способность
        ``archive_and_replace``. Архива нет, и это честно: «отложить прежнюю
        историю рядом» осмысленно для файлов, а у базы это либо отдельная
        таблица, либо не нужно вовсе. Раньше на этом месте была прямая запись в
        файлы мимо контракта — у хранилища без ``threads_dir`` она уходила в
        относительный путь от текущего каталога, молча.

        Через ``replace_messages``, а не ``reset`` + ``append_messages``:
        ``reset`` по контракту сносит ВЕСЬ тред, включая ``subagent_trace`` —
        каждая компакция теряла историю вызовов субагентов, — и между ним и
        последующим ``save_meta`` треда не существовало вовсе. Хранилищу без
        этого метода (структурная реализация мимо базы) остаётся прежняя пара.
        """
        replace = getattr(self.store, "replace_messages", None)
        if callable(replace):
            replace(self.system_id, self.thread_id, compact_messages)
        else:
            self.store.reset(self.system_id, self.thread_id)
            self.store.append_messages(
                self.system_id, self.thread_id, compact_messages
            )
        return {
            "archive_suffix": _utc_now_suffix(),
            "archive_data_path": None,
            "archive_meta_path": None,
        }

    async def _aarchive_via_store(
        self, compact_messages: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """См. синхронного двойника: то же замещение без потери трассы."""
        replace = getattr(self.port, "areplace_messages", None)
        if callable(replace):
            await replace(self.system_id, self.thread_id, compact_messages)
        else:
            await self.port.areset(self.system_id, self.thread_id)
            await self.port.aappend_messages(
                self.system_id, self.thread_id, compact_messages
            )
        return {
            "archive_suffix": _utc_now_suffix(),
            "archive_data_path": None,
            "archive_meta_path": None,
        }

    def _compact_result(
        self,
        archived: Dict[str, Any],
        compact_messages: List[Dict[str, Any]],
        summary_message: Dict[str, Any],
    ) -> Dict[str, Any]:
        return {
            "archive_suffix": archived.get("archive_suffix"),
            "archive_data_path": archived.get("archive_data_path"),
            "archive_meta_path": archived.get("archive_meta_path"),
            "new_messages_count": len(compact_messages),
            "newly_summarized_message_id": summary_message.get("id"),
        }

    def archive_and_compact_summary_payload(
        self,
        summary_payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        # Diff touched-journal не трогаем: это operational state сессии (как
        # data_subagents_*), не история диалога. Удаляется только с тредом.
        # До переноса meta в архив: снимок disk tui/runtime (TUI-ключи не клобберить).
        disk_meta = self.store.load_meta(self.system_id, self.thread_id) or {}
        summary_message = self._compact_summary_message(summary_payload)
        compact_messages = [summary_message]

        archiver = getattr(self.store, "archive_and_replace", None)
        if callable(archiver):
            archived = archiver(self.system_id, self.thread_id, compact_messages)
        else:
            archived = self._archive_via_store(compact_messages)

        known_message_keys = {make_message_key(m) for m in compact_messages}
        new_meta = self._compact_meta(
            summary_payload, compact_messages, disk_meta,
            archived.get("archive_suffix") or "",
        )
        self.store.save_meta(self.system_id, self.thread_id, new_meta)
        self._compact_apply(compact_messages, known_message_keys, new_meta)
        return self._compact_result(archived, compact_messages, summary_message)

    async def aarchive_and_compact_summary_payload(
        self,
        summary_payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        """То же, что ``archive_and_compact_summary_payload``, но не блокируя цикл."""
        disk_meta = await self.port.aload_meta(self.system_id, self.thread_id) or {}
        summary_message = self._compact_summary_message(summary_payload)
        compact_messages = [summary_message]

        # Асинхронное имя ИЩЕМ ПЕРВЫМ. Раньше здесь искали только синхронное и
        # звали его прямо на loop: у `AsyncThreadStore` с корутинным
        # `aarchive_and_replace` синхронного имени нет вовсе, а если бы кто-то
        # объявил `async def archive_and_replace`, вызов вернул бы корутину и
        # `archived.get` упал бы уже ПОСЛЕ того, как патч применён к
        # `state.messages` — память и хранилище разошлись бы.
        aarchiver = getattr(self.port, "aarchive_and_replace", None)
        archiver = getattr(self.store, "archive_and_replace", None)
        if callable(aarchiver):
            archived = await aarchiver(
                self.system_id, self.thread_id, compact_messages
            )
        elif callable(archiver):
            archived = await await_callback(
                archiver(self.system_id, self.thread_id, compact_messages)
            )
        else:
            archived = await self._aarchive_via_store(compact_messages)

        known_message_keys = {make_message_key(m) for m in compact_messages}
        new_meta = self._compact_meta(
            summary_payload, compact_messages, disk_meta,
            archived.get("archive_suffix") or "",
        )
        await self.port.asave_meta(self.system_id, self.thread_id, new_meta)
        self._compact_apply(compact_messages, known_message_keys, new_meta)
        return self._compact_result(archived, compact_messages, summary_message)
