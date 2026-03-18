import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def _swarm_paths(threads_dir: str, swarm_id: str) -> Tuple[str, str, str, str]:
    """
    Получить пути к messages.jsonl, meta.json, stdout.log и stderr.log
    для указанного swarm_id.
    """
    base_dir = os.path.join(threads_dir, swarm_id)
    data_path = os.path.join(base_dir, "messages.jsonl")
    meta_path = os.path.join(base_dir, "meta.json")
    stdout_path = os.path.join(base_dir, "stdout.log")
    stderr_path = os.path.join(base_dir, "stderr.log")
    return data_path, meta_path, stdout_path, stderr_path


def load_swarm_meta(threads_dir: str, swarm_id: str) -> Dict[str, Any]:
    _, meta_path, _, _ = _swarm_paths(threads_dir, swarm_id)
    if not os.path.exists(meta_path):
        return {}
    with open(meta_path, "r", encoding="utf-8") as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError:
            return {}
    return data if isinstance(data, dict) else {}


def save_swarm_meta(
    threads_dir: str,
    swarm_id: str,
    *,
    base_meta: Dict[str, Any],
    swarm_meta_patch: Dict[str, Any],
) -> None:
    """
    Сохранить метадату сворма.

    - base_meta: то, что совместимо с обычной meta.json (CLI/вебхук).
    - swarm_meta_patch: патч для вложенного ключа swarm_meta (дополнительные поля сворма).
    """
    data_path, meta_path, _, _ = _swarm_paths(threads_dir, swarm_id)
    base_dir = os.path.dirname(meta_path)
    _ensure_dir(base_dir)

    current = load_swarm_meta(threads_dir, swarm_id)

    # Обновляем "обычную" часть метадаты.
    merged: Dict[str, Any] = dict(current)
    merged.update(base_meta)

    # Обновляем swarm-специфичные поля под ключом "swarm_meta".
    existing_swarm_meta = merged.get("swarm_meta") or {}
    if not isinstance(existing_swarm_meta, dict):
        existing_swarm_meta = {}
    updated_swarm_meta = dict(existing_swarm_meta)
    updated_swarm_meta.update(swarm_meta_patch)
    merged["swarm_meta"] = updated_swarm_meta

    # Обновляем служебные поля.
    merged.setdefault("created_at", _utc_now_iso())
    merged["updated_at"] = _utc_now_iso()

    tmp_path = f"{meta_path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, meta_path)

    # Убеждаемся, что директория для messages тоже существует.
    _ensure_dir(os.path.dirname(data_path))


def append_swarm_message(
    threads_dir: str,
    swarm_id: str,
    *,
    stream: str,
    message: str,
) -> None:
    """
    Добавить одну запись в messages.jsonl.
    """
    data_path, _, _, _ = _swarm_paths(threads_dir, swarm_id)
    _ensure_dir(os.path.dirname(data_path))

    record = {
        "ts": _utc_now_iso(),
        "stream": stream,
        "message": message,
    }
    with open(data_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def load_swarm_messages(threads_dir: str, swarm_id: str) -> List[Dict[str, Any]]:
    data_path, _, _, _ = _swarm_paths(threads_dir, swarm_id)
    if not os.path.exists(data_path):
        return []

    result: List[Dict[str, Any]] = []
    with open(data_path, "r", encoding="utf-8") as f:
        for line in f:
            row = line.strip()
            if not row:
                continue
            try:
                record = json.loads(row)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                result.append(record)
    return result


def load_swarm_stdout_stderr(
    threads_dir: str, swarm_id: str
) -> Tuple[List[str], List[str]]:
    """
    Прочитать stdout.log и stderr.log, если они есть.
    """
    _, _, stdout_path, stderr_path = _swarm_paths(threads_dir, swarm_id)

    def _read_lines(path: str) -> List[str]:
        if not os.path.exists(path):
            return []
        with open(path, "r", encoding="utf-8") as f:
            return [line.rstrip("\n") for line in f]

    return _read_lines(stdout_path), _read_lines(stderr_path)


__all__ = [
    "load_swarm_meta",
    "save_swarm_meta",
    "append_swarm_message",
    "load_swarm_messages",
    "load_swarm_stdout_stderr",
]

