"""Список диалогов Responses и каталог хода.

Файлы — один уровень ``cwd`` последнего хода, не разбор тулов.

Читает уже лежащие индекс и jsonl. Новый архив не заводит.
Треды субагентов не обходит.
"""

from __future__ import annotations

import json
import os

from gc_core.message_policy.user_message_policy import (
    is_technical_user_message_text,
)
from gc_core.thread_store import validate_thread_id

from gc_chat.responses.index_store import ResponseIndex, ResponseRecord

_TITLE_LIMIT = 80
_ENVELOPE = "message"
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


def dialog_folder(
    index: ResponseIndex, thread_id: str, relative: str
) -> dict | None:
    """Один уровень каталога хода. Нет треда или пути — ``None``."""
    cwd = _cwd(index, thread_id)
    if cwd is None:
        return None
    located = _locate(cwd, relative, directory=True)
    if located is None:
        return None
    folder, resolved = located
    entries = _entries(cwd, folder, resolved)
    if entries is None:
        return None
    return {"path": folder, "entries": entries}


def file_bytes(
    index: ResponseIndex, thread_id: str, relative: str
) -> tuple[str, bytes] | None:
    """Байты обычного файла внутри ``cwd``. Иначе ``None``."""
    cwd = _cwd(index, thread_id)
    if cwd is None:
        return None
    located = _locate(cwd, relative, directory=False)
    if located is None:
        return None
    rel, resolved = located
    try:
        with open(resolved, "rb") as handle:
            data = handle.read()
    except OSError:
        return None
    return rel.rsplit("/", 1)[-1], data


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


def _cwd(index: ResponseIndex, thread_id: str) -> str | None:
    records = _thread_records(index, thread_id)
    if records is None:
        return None
    return _chain(records)[-1].cwd


def _locate(cwd: str, raw: str, *, directory: bool) -> tuple[str, str] | None:
    """Относительный путь и реальный файл или каталог внутри ``cwd``."""
    if not isinstance(raw, str):
        return None
    if "\x00" in raw or "\\" in raw or _has_dotdot(raw) or os.path.isabs(raw):
        return None
    cwd_abs = os.path.abspath(cwd)
    if raw in ["", "."]:
        if not directory:
            return None
        return _checked(cwd_abs, "", directory=True)
    joined = os.path.abspath(os.path.join(cwd_abs, raw))
    relative = os.path.relpath(joined, cwd_abs)
    if relative == ".":
        relative = ""
    if relative == ".." or relative.startswith(".." + os.sep):
        return None
    relative = relative.replace(os.sep, "/")
    if any(part.startswith(".") for part in relative.split("/")):
        return None
    return _checked(cwd_abs, relative, directory=directory)


def _checked(
    cwd_abs: str, relative: str, *, directory: bool
) -> tuple[str, str] | None:
    if relative == "":
        joined = cwd_abs
    else:
        joined = os.path.join(cwd_abs, *relative.split("/"))
    cwd_real = os.path.realpath(cwd_abs)
    resolved = os.path.realpath(joined)
    if not _is_inside(cwd_real, resolved):
        return None
    if directory:
        if not os.path.isdir(resolved):
            return None
    elif not os.path.isfile(resolved):
        return None
    return relative, resolved


def _entries(cwd: str, relative: str, resolved: str) -> list[dict] | None:
    try:
        names = os.listdir(resolved)
    except OSError:
        return None
    cwd_abs = os.path.abspath(cwd)
    found: list[dict] = []
    for name in names:
        if (
            name == ""
            or name.startswith(".")
            or "/" in name
            or "\\" in name
            or "\x00" in name
        ):
            continue
        child = name if relative == "" else relative + "/" + name
        if _checked(cwd_abs, child, directory=True) is not None:
            found.append({"name": name, "path": child, "kind": "dir"})
            continue
        if _checked(cwd_abs, child, directory=False) is not None:
            found.append({"name": name, "path": child, "kind": "file"})
    found.sort(
        key=lambda item: (
            item["kind"] != "dir",
            item["name"].casefold(),
            item["name"],
        )
    )
    return found


def _has_dotdot(raw: str) -> bool:
    normalized = raw.replace("\\", "/")
    return any(part == ".." for part in normalized.split("/"))


def _is_inside(root: str, path: str) -> bool:
    try:
        common = os.path.commonpath([root, path])
    except ValueError:
        return False
    return common == root
