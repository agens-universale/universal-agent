"""Список диалогов Responses и каталог хода.

Файлы — один уровень ``cwd`` последнего хода, не разбор тулов.
Новая папка и удаление пишут только внутрь этого ``cwd``.
Корень каталога хода удалить нельзя.
Тот же разбор каталога принимает корень нового хода без треда.

Читает уже лежащие индекс и jsonl. Новый архив не заводит.
После компакции промпты в живом jsonl пропадают: имя тогда берётся
из ``user_intent_log`` и ``current_user_intent`` в meta треда.
Треды субагентов не обходит.
"""

from __future__ import annotations

import json
import os

from gc_core.constants.tool_names import (
    ASK_HUMAN_TOOL_NAME,
    FINISH_TOOL_NAME,
    INTERMEDIATE_REPLY_TOOL_NAME,
    PROJECT_MEMORY_TOOL_NAME,
    READ_STORED_TOOL_OUTPUT_NAME,
    READ_USER_INTENT_LOG_NAME,
    SESSION_MEMORY_TOOL_NAME,
    SESSION_THINK_TOOL_NAME,
)
from gc_core.message_policy.user_message_policy import (
    is_technical_user_message_text,
)
from gc_core.thread_store import validate_thread_id

from gc_chat.responses.errors import ApiError
from gc_chat.responses.index_store import ResponseIndex, ResponseRecord

# Те же пороги, что у авто-имени TUI (session_title.py): первый промпт сеет
# любое имя, следующий заменяет его только если длиннее минимума.
_PROMPT_MIN = 30
_NAME_MAX = 200
_TEXT_PROBE = 8192
_ENVELOPE = "message"
_RESPONSES = "responses"
_CALL = "function_call"
# Не «работа хода»: финализатор обрабатывается отдельно, эти вызовы имя не двигают.
_NOT_WORK = frozenset(
    {
        ASK_HUMAN_TOOL_NAME,
        INTERMEDIATE_REPLY_TOOL_NAME,
        SESSION_THINK_TOOL_NAME,
        SESSION_MEMORY_TOOL_NAME,
        PROJECT_MEMORY_TOOL_NAME,
        READ_USER_INTENT_LOG_NAME,
        READ_STORED_TOOL_OUTPUT_NAME,
    }
)


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
        title = (
            _dialog_title(
                scan.records, _intent_asks(index.thread_path(thread_id))
            )
            if scan.intact
            else ""
        )
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
    return list_folder(cwd, relative)


def list_folder(cwd: str, relative: str) -> dict | None:
    """Один уровень каталога. Нет пути — ``None``."""
    located = _locate(cwd, relative, directory=True)
    if located is None:
        return None
    folder, resolved = located
    entries = _entries(cwd, folder, resolved)
    if entries is None:
        return None
    return {"path": folder, "entries": entries}


def make_directory(
    index: ResponseIndex, thread_id: str, parent: str, name: str
) -> str | None:
    """Каталог ``parent/name`` внутри ``cwd``. Нет треда или места — ``None``."""
    cwd = _cwd(index, thread_id)
    if cwd is None:
        return None
    return make_folder(cwd, parent, name)


def make_folder(cwd: str, parent: str, name: str) -> str | None:
    """Каталог ``parent/name`` внутри ``cwd``. Нет места — ``None``."""
    if not isinstance(name, str):
        raise ApiError(400, "invalid_request", "invalid name")
    cleaned = name.strip()
    if not _valid_name(cleaned):
        raise ApiError(400, "invalid_request", "invalid name")
    located = _locate(cwd, parent, directory=True)
    if located is None:
        return None
    folder, resolved = located
    dest_rel = cleaned if folder == "" else folder + "/" + cleaned
    dest = os.path.join(resolved, cleaned)
    if os.path.lexists(dest):
        raise ApiError(409, "conflict", "already exists")
    try:
        os.mkdir(dest)
    except FileExistsError:
        raise ApiError(409, "conflict", "already exists") from None
    except OSError:
        return None
    return dest_rel


def remove_entry(index: ResponseIndex, thread_id: str, relative: str) -> bool:
    """Удаляет файл или каталог внутри ``cwd``. Корень и чужой путь — ``False``."""
    cwd = _cwd(index, thread_id)
    if cwd is None:
        return False
    return delete_path(cwd, relative)


def delete_path(cwd: str, relative: str) -> bool:
    """Удаляет путь внутри ``cwd``. Корень и чужой путь — ``False``."""
    placed = _place(cwd, relative)
    if placed is None:
        return False
    _relative, lexical, resolved = placed
    cwd_real = os.path.realpath(os.path.abspath(cwd))
    try:
        if os.path.islink(lexical) or os.path.isfile(lexical):
            os.unlink(lexical)
        elif os.path.isdir(lexical) and resolved != cwd_real:
            _remove_tree(lexical)
        else:
            return False
    except OSError:
        return False
    return True


def file_bytes(
    index: ResponseIndex, thread_id: str, relative: str
) -> tuple[str, bytes] | None:
    """Байты обычного файла внутри ``cwd``. Иначе ``None``."""
    cwd = _cwd(index, thread_id)
    if cwd is None:
        return None
    return read_file(cwd, relative)


def read_file(cwd: str, relative: str) -> tuple[str, bytes] | None:
    """Байты обычного файла внутри ``cwd``. Иначе ``None``."""
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


class _Name:
    """Черновик имени: ``task`` важнее авто-имени из промптов."""

    def __init__(self) -> None:
        self.last_actual = ""
        self.task = ""
        self.tools = 0


def _dialog_title(records: list[dict], asks: list[str] | None = None) -> str:
    """Имя как в TUI: ``task`` хода, иначе авто-имя из промптов.

    ``asks`` — ``user_ask`` из meta. Они переживают компакцию, живой jsonl нет.
    Сначала они, потом промпты живого файла: короткий хвост длинное имя не сбивает.
    """
    name = _Name()
    for text in asks or []:
        name.last_actual = _next_prompt(name.last_actual, text)
    for record in records:
        _fold_name(name, _message(record))
    if name.task:
        return name.task
    return name.last_actual


def _intent_asks(data_path: str) -> list[str]:
    """Тексты ``user_ask`` из meta: закрытый лог, затем открытый ход."""
    meta = _read_meta(_meta_path(data_path))
    if meta is None:
        return []
    texts: list[str] = []
    for key in ("user_intent_log", "current_user_intent"):
        texts.extend(_user_asks(meta.get(key)))
    return texts


def _meta_path(data_path: str) -> str:
    directory, name = os.path.split(data_path)
    if not name.startswith("data_") or not name.endswith(".jsonl"):
        return ""
    stem = name[len("data_") : -len(".jsonl")]
    return os.path.join(directory, f"meta_{stem}.json")


def _read_meta(path: str) -> dict | None:
    if path == "" or not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            meta = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(meta, dict):
        return None
    return meta


def _user_asks(raw: object) -> list[str]:
    if not isinstance(raw, list):
        return []
    texts: list[str] = []
    for item in raw:
        text = _ask_text(item)
        if text is None:
            continue
        texts.append(text)
    return texts


def _ask_text(item: object) -> str | None:
    if isinstance(item, str):
        text = item.strip()
    elif isinstance(item, dict) and item.get("action") == "user_ask":
        content = item.get("content")
        text = content.strip() if isinstance(content, str) else ""
    else:
        return None
    if text == "" or is_technical_user_message_text(text):
        return None
    return text


def _fold_name(name: _Name, message: dict) -> None:
    user = _user_text(message)
    if user is not None:
        name.tools = 0
        name.last_actual = _next_prompt(name.last_actual, user)
        return
    tool = _call_name(message)
    if tool is None:
        return
    if tool == FINISH_TOOL_NAME:
        _fold_task(name, message)
        return
    if tool not in _NOT_WORK:
        name.tools += 1


def _next_prompt(current: str, text: str) -> str:
    norm = _clip(text)
    if norm == "":
        return current
    if current == "" or len(norm) > _PROMPT_MIN:
        return norm
    return current


def _fold_task(name: _Name, message: dict) -> None:
    task = _finish_task(message)
    if task == "":
        return
    if name.task == "" or name.tools > 0:
        name.task = task


def _finish_task(message: dict) -> str:
    raw = message.get("arguments")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return ""
    if not isinstance(raw, dict):
        return ""
    task = raw.get("task")
    if not isinstance(task, str):
        return ""
    return _clip(task)


def _call_name(message: dict) -> str | None:
    if message.get("type") != _CALL:
        return None
    name = message.get("name")
    if not isinstance(name, str) or name == "":
        return None
    return name


def _clip(text: str) -> str:
    return " ".join(text.split())[:_NAME_MAX]


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


def _valid_name(name: str) -> bool:
    if name == "" or name in {".", ".."} or name.startswith("."):
        return False
    return "/" not in name and "\\" not in name and "\x00" not in name


def _place(cwd: str, raw: str) -> tuple[str, str, str] | None:
    """Относительный путь, запись в каталоге и её реальный путь.

    Пустая строка — корень ``cwd``, его здесь нет.
    """
    if not isinstance(raw, str) or raw in {"", "."}:
        return None
    if "\x00" in raw or "\\" in raw or _has_dotdot(raw) or os.path.isabs(raw):
        return None
    cwd_abs = os.path.abspath(cwd)
    joined = os.path.abspath(os.path.join(cwd_abs, raw))
    relative = os.path.relpath(joined, cwd_abs).replace(os.sep, "/")
    if relative in {"", ".", ".."} or relative.startswith("../"):
        return None
    if any(part.startswith(".") for part in relative.split("/")):
        return None
    lexical = os.path.join(cwd_abs, *relative.split("/"))
    if not os.path.lexists(lexical):
        return None
    cwd_real = os.path.realpath(cwd_abs)
    resolved = os.path.realpath(lexical)
    if not _is_inside(cwd_real, resolved):
        return None
    return relative, lexical, resolved


def _remove_tree(path: str) -> None:
    for dirpath, dirnames, filenames in os.walk(
        path, topdown=False, followlinks=False
    ):
        for filename in filenames:
            os.unlink(os.path.join(dirpath, filename))
        for dirname in dirnames:
            child = os.path.join(dirpath, dirname)
            if os.path.islink(child):
                os.unlink(child)
            else:
                os.rmdir(child)
    os.rmdir(path)


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
        placed = _checked(cwd_abs, child, directory=False)
        if placed is not None:
            found.append(_file_entry(name, child, placed[1]))
    found.sort(
        key=lambda item: (
            item["kind"] != "dir",
            item["name"].casefold(),
            item["name"],
        )
    )
    return found


def _file_entry(name: str, child: str, resolved: str) -> dict:
    return {
        "name": name,
        "path": child,
        "kind": "file",
        "text": _file_is_text(resolved),
    }


def _file_is_text(path: str) -> bool:
    """Начало файла — UTF-8 без нулевого байта. Пустой файл — текст."""
    try:
        with open(path, "rb") as handle:
            sample = handle.read(_TEXT_PROBE)
            rest = handle.read(1)
    except OSError:
        return False
    if b"\0" in sample:
        return False
    return _utf8_sample(sample, cut=rest != b"")


def _utf8_sample(sample: bytes, *, cut: bool) -> bool:
    try:
        sample.decode("utf-8")
    except UnicodeDecodeError as exc:
        if not cut or exc.start < len(sample) - 3:
            return False
        try:
            sample[: exc.start].decode("utf-8")
        except UnicodeDecodeError:
            return False
        return True
    return True


def _has_dotdot(raw: str) -> bool:
    normalized = raw.replace("\\", "/")
    return any(part == ".." for part in normalized.split("/"))


def _is_inside(root: str, path: str) -> bool:
    try:
        common = os.path.commonpath([root, path])
    except ValueError:
        return False
    return common == root
