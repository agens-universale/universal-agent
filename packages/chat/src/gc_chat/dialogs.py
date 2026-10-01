"""Список диалогов Responses и файлы хода.

Файлы — успешные тулы записи и существующие пути из ``handoff.artifacts``.

Читает уже лежащие индекс и jsonl. Новый архив не заводит.
Треды субагентов не обходит.
"""

from __future__ import annotations

import json
import os
from typing import Any

from gc_core.constants.tool_names import FINISH_TOOL_NAME
from gc_core.message_policy.user_message_policy import (
    is_technical_user_message_text,
)
from gc_core.thread_store import validate_thread_id

from gc_chat.responses.index_store import ResponseIndex, ResponseRecord

_TITLE_LIMIT = 80
_WRITE_TOOLS = frozenset(
    {
        "fs_write_file",
        "fs_append_file",
        "fs_apply_file_edits",
        "fs_search_and_replace",
        "fs_save_base64_file",
    }
)
_DRY_RUN_TOOL = "fs_search_and_replace"
_ENVELOPE = "message"
_CALL = "function_call"
_CALL_OUTPUT = "function_call_output"
_RESPONSES = "responses"


def dialog_list(index: ResponseIndex) -> list[dict]:
    """Диалоги по ``thread_id``, сначала недавно обновлённые."""
    grouped: dict[str, list[ResponseRecord]] = {}
    for record in index.load_all():
        grouped.setdefault(record.thread_id, []).append(record)
    items: list[dict] = []
    for thread_id, records in grouped.items():
        scan = _scan(index.thread_path(thread_id))
        if scan.foreign:
            continue
        chain = _chain(records)
        title = ""
        if scan.intact:
            texts = _user_texts(scan.records)
            if texts:
                title = _title(texts[0])
        items.append(_summary(thread_id, chain, title))
    items.sort(key=lambda item: (-item["updated_at"], item["thread_id"]))
    return items


def dialog_turns(index: ResponseIndex, thread_id: str) -> list[dict] | None:
    """Ходы от первого к последнему. Неизвестный тред — ``None``."""
    records = _thread_records(index, thread_id)
    if records is None:
        return None
    scan = _scan(index.thread_path(thread_id))
    texts = _user_texts(scan.records) if scan.intact else []
    turns: list[dict] = []
    for offset, record in enumerate(_chain(records)):
        output = record.response.get("output")
        turns.append(
            {
                "response_id": record.response_id,
                "previous_response_id": record.previous_response_id,
                "created_at": record.created_at,
                "status": record.status,
                "text": texts[offset] if offset < len(texts) else "",
                "output": output if isinstance(output, list) else [],
            }
        )
    return turns


def dialog_artifacts(
    index: ResponseIndex, thread_id: str
) -> list[dict] | None:
    """Успешные файлы записи внутри ``cwd`` последнего хода."""
    records = _thread_records(index, thread_id)
    if records is None:
        return None
    scan = _scan(index.thread_path(thread_id))
    if not scan.intact:
        return []
    cwd = _chain(records)[-1].cwd
    return _artifacts(scan.records, cwd)


def artifact_bytes(
    index: ResponseIndex, thread_id: str, relative: str
) -> tuple[str, bytes] | None:
    """Байты файла из списка артефактов. Иначе ``None``."""
    if not isinstance(relative, str) or relative == "":
        return None
    artifacts = dialog_artifacts(index, thread_id)
    if artifacts is None:
        return None
    match = next(
        (item for item in artifacts if item["path"] == relative), None
    )
    if match is None:
        return None
    records = _thread_records(index, thread_id)
    if records is None:
        return None
    cwd = _chain(records)[-1].cwd
    if relative_artifact(cwd, relative) != relative:
        return None
    full = os.path.realpath(
        os.path.join(os.path.abspath(cwd), *relative.split("/"))
    )
    if not os.path.isfile(full):
        return None
    try:
        with open(full, "rb") as handle:
            data = handle.read()
    except OSError:
        return None
    return match["name"], data


def relative_artifact(cwd: str, raw: str) -> str | None:
    """Относительный путь внутри ``cwd`` или ``None``.

    ``..``, каталог, отсутствующий файл и симлинк наружу отбрасываются.
    """
    if not isinstance(raw, str) or raw.strip() == "":
        return None
    if "\x00" in raw or _has_dotdot(raw):
        return None
    cwd_abs = os.path.abspath(cwd)
    if os.path.isabs(raw):
        joined = os.path.abspath(raw)
    else:
        joined = os.path.abspath(os.path.join(cwd_abs, raw))
    relative = os.path.relpath(joined, cwd_abs)
    if relative == ".." or relative.startswith(".." + os.sep):
        return None
    cwd_real = os.path.realpath(cwd_abs)
    resolved = os.path.realpath(joined)
    if not _is_inside(cwd_real, resolved):
        return None
    if not os.path.isfile(resolved):
        return None
    return relative.replace(os.sep, "/")


class _Scan:
    def __init__(self, kind: str, records: list[dict]) -> None:
        self.kind = kind
        self.records = records

    @property
    def intact(self) -> bool:
        return self.kind == "ok"

    @property
    def foreign(self) -> bool:
        return self.kind == "foreign"


def _thread_records(
    index: ResponseIndex, thread_id: str
) -> list[ResponseRecord] | None:
    try:
        validate_thread_id(thread_id)
    except ValueError:
        return None
    records = [
        record for record in index.load_all() if record.thread_id == thread_id
    ]
    if not records:
        return None
    if _scan(index.thread_path(thread_id)).foreign:
        return None
    return records


def _summary(thread_id: str, chain: list[ResponseRecord], title: str) -> dict:
    head = chain[-1]
    first = chain[0]
    return {
        "thread_id": thread_id,
        "latest_response_id": head.response_id,
        "cwd": head.cwd,
        "created_at": first.created_at,
        "updated_at": head.created_at,
        "status": head.status,
        "title": title,
    }


def _chain(records: list[ResponseRecord]) -> list[ResponseRecord]:
    by_id = {record.response_id: record for record in records}
    pointed = {
        record.previous_response_id
        for record in records
        if record.previous_response_id
    }
    heads = [record for record in records if record.response_id not in pointed]
    if heads:
        head = max(heads, key=lambda item: (item.created_at, item.response_id))
    else:
        head = max(
            records, key=lambda item: (item.created_at, item.response_id)
        )
    ordered: list[ResponseRecord] = []
    seen: set[str] = set()
    current: ResponseRecord | None = head
    while current is not None and current.response_id not in seen:
        seen.add(current.response_id)
        ordered.append(current)
        previous = current.previous_response_id
        current = by_id.get(previous) if isinstance(previous, str) else None
    ordered.reverse()
    return ordered


def _scan(path: str) -> _Scan:
    if not os.path.isfile(path):
        return _Scan("missing", [])
    try:
        with open(path, encoding="utf-8") as handle:
            lines = handle.readlines()
    except OSError:
        return _Scan("broken", [])
    records: list[dict] = []
    for line in lines:
        if line.strip() == "":
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            return _Scan("broken", [])
        if not isinstance(item, dict):
            return _Scan("broken", [])
        system = item.get("system")
        if isinstance(system, str) and system != _RESPONSES:
            return _Scan("foreign", [])
        records.append(item)
    return _Scan("ok", records)


def _user_texts(records: list[dict]) -> list[str]:
    texts: list[str] = []
    for record in records:
        text = _user_text(_message(record))
        if text is not None:
            texts.append(text)
    return texts


def _user_text(message: dict) -> str | None:
    if message.get("role") != "user":
        return None
    kind = message.get("type")
    if kind not in (None, "message"):
        return None
    content = message.get("content")
    if not isinstance(content, str):
        return None
    text = content.strip()
    if text == "" or is_technical_user_message_text(text):
        return None
    return text


def _title(text: str) -> str:
    flat = " ".join(text.split())
    return flat[:_TITLE_LIMIT]


def _message(record: dict) -> dict:
    payload = record.get("payload")
    if record.get("type") == _ENVELOPE and isinstance(payload, dict):
        return payload
    return record


def _artifacts(records: list[dict], cwd: str) -> list[dict]:
    pending: dict[str, dict] = {}
    found: list[dict] = []
    seen: set[str] = set()
    for record in records:
        message = _message(record)
        kind = message.get("type")
        if kind == _CALL:
            call_id = _call_id(message)
            if call_id is not None:
                pending[call_id] = message
            _collect_handoff(message, cwd, found, seen)
            continue
        if kind != _CALL_OUTPUT:
            continue
        call_id = _call_id(message)
        if call_id is None:
            continue
        call = pending.pop(call_id, None)
        if call is None:
            continue
        item = _accepted(call, message.get("output"), cwd)
        if item is None or item["path"] in seen:
            continue
        seen.add(item["path"])
        found.append(item)
    return found


def _collect_handoff(
    call: dict, cwd: str, found: list[dict], seen: set[str]
) -> None:
    """Файлы из ``handoff.artifacts`` у ``turn_finish``, если они есть."""
    for raw in _handoff_paths(call):
        item = _artifact_item(cwd, raw)
        if item is None or item["path"] in seen:
            continue
        seen.add(item["path"])
        found.append(item)


def _handoff_paths(call: dict) -> list[str]:
    if call.get("name") != FINISH_TOOL_NAME:
        return []
    arguments = _arguments(call.get("arguments"))
    handoff = _arguments(arguments.get("handoff"))
    artifacts = handoff.get("artifacts")
    if not isinstance(artifacts, list):
        return []
    return [item for item in artifacts if isinstance(item, str) and item != ""]


def _artifact_item(cwd: str, raw: str) -> dict | None:
    relative = relative_artifact(cwd, raw)
    if relative is None:
        return None
    return {"path": relative, "name": relative.rsplit("/", 1)[-1]}


def _accepted(call: dict, output: Any, cwd: str) -> dict | None:
    name = call.get("name")
    if not isinstance(name, str) or name not in _WRITE_TOOLS:
        return None
    arguments = _arguments(call.get("arguments"))
    if name == _DRY_RUN_TOOL and arguments.get("dry_run") is True:
        return None
    if _marked_error(output):
        return None
    raw = arguments.get("path")
    if not isinstance(raw, str):
        return None
    return _artifact_item(cwd, raw)


def _arguments(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if isinstance(data, dict):
        return data
    return {}


def _marked_error(output: Any) -> bool:
    data: Any = output
    if isinstance(output, str):
        try:
            data = json.loads(output)
        except json.JSONDecodeError:
            return False
    if not isinstance(data, dict):
        return False
    return data.get("ok") is False


def _call_id(message: dict) -> str | None:
    call_id = message.get("call_id")
    if isinstance(call_id, str) and call_id != "":
        return call_id
    return None


def _has_dotdot(raw: str) -> bool:
    normalized = raw.replace("\\", "/")
    return any(part == ".." for part in normalized.split("/"))


def _is_inside(root: str, path: str) -> bool:
    try:
        common = os.path.commonpath([root, path])
    except ValueError:
        return False
    return common == root
