"""Список диалогов Responses и каталог хода.

Файлы — один уровень ``cwd`` последнего хода, не разбор тулов.
Новая папка и удаление пишут только внутрь этого ``cwd``.
Корень каталога хода удалить нельзя.
Тот же разбор каталога принимает корень нового хода без треда.

Читает ответы и историю тредов через хранилище. Новый архив не заводит.
После компакции промпты в живой истории пропадают: имя тогда берётся
из журнала намерений треда (``user_intent_log``, ``current_user_intent``).
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
from gc_core.threads.models import ThreadKey, ThreadRecord

from gc_chat.responses.errors import ApiError
from gc_chat.responses.index_store import (
    SYSTEM,
    ResponseIndex,
    ResponseRecord,
    chain,
    thread_key,
)

# Те же пороги, что у авто-имени TUI (session_title.py): первый промпт сеет
# любое имя, следующий заменяет его только если длиннее минимума.
_PROMPT_MIN = 30
_NAME_MAX = 200
_TEXT_PROBE = 8192
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


async def dialog_list(index: ResponseIndex) -> list[dict]:
    """Диалоги по ``thread_id``, сначала недавно обновлённые."""
    items: list[dict] = []
    covered: set[str] = set()
    async for thread in index.threads.iter_threads(system=SYSTEM):
        summary = index.summary(thread)
        if summary is None:
            continue
        covered.add(thread.key.thread)
        title = await _title(index, thread)
        items.append(_listed(thread.key.thread, summary, title))
    # Диалоги develop: сводки нет, только файлы ответов.
    for thread_id, records in index.legacy_dialogs().items():
        if thread_id in covered:
            continue
        try:
            thread = await index.threads.get(thread_key(thread_id))
        except (OSError, ValueError):
            # Битый id или тред, чья запись не доигрывается
            # (JournalRecoveryError), — не повод прятать остальные диалоги.
            continue
        title = "" if thread is None else await _title(index, thread)
        items.append(_summary(thread_id, chain(records), title))
    items.sort(key=lambda item: (-item["updated_at"], item["thread_id"]))
    return items


async def dialog_turns(
    index: ResponseIndex, thread_id: str
) -> list[dict] | None:
    """Ходы от первого к последнему. Неизвестный тред — ``None``."""
    records = await _thread_records(index, thread_id)
    if records is None:
        return None
    texts = _user_texts(await _messages(index, thread_key(thread_id)))
    turns: list[dict] = []
    for offset, record in enumerate(chain(records)):
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


async def dialog_folder(
    index: ResponseIndex, thread_id: str, relative: str
) -> dict | None:
    """Один уровень каталога хода. Нет треда или пути — ``None``."""
    cwd = await _cwd(index, thread_id)
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


async def make_directory(
    index: ResponseIndex, thread_id: str, parent: str, name: str
) -> str | None:
    """Каталог ``parent/name`` внутри ``cwd``. Нет треда или места — ``None``."""
    cwd = await _cwd(index, thread_id)
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


async def remove_entry(
    index: ResponseIndex, thread_id: str, relative: str
) -> bool:
    """Удаляет файл или каталог внутри ``cwd``. Корень и чужой путь — ``False``."""
    cwd = await _cwd(index, thread_id)
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


async def file_bytes(
    index: ResponseIndex, thread_id: str, relative: str
) -> tuple[str, bytes] | None:
    """Байты обычного файла внутри ``cwd``. Иначе ``None``."""
    cwd = await _cwd(index, thread_id)
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


async def _thread_records(
    index: ResponseIndex, thread_id: str
) -> list[ResponseRecord] | None:
    try:
        records = await index.thread_records(thread_id)
    except ValueError:
        return None
    return records or None


async def _messages(index: ResponseIndex, key: ThreadKey) -> list[dict]:
    return [entry.data for entry in await index.threads.history(key)]


async def _title(index: ResponseIndex, thread: ThreadRecord) -> str:
    asks = [
        *_user_asks(thread.state.intent_log),
        *_user_asks(thread.state.current_intent),
    ]
    return _dialog_title(await _messages(index, thread.key), asks)


def _listed(thread_id: str, summary: dict, title: str) -> dict:
    return {
        "thread_id": thread_id,
        "latest_response_id": summary.get("latest_response_id"),
        "cwd": summary.get("cwd"),
        "created_at": summary.get("created_at"),
        "updated_at": summary.get("updated_at"),
        "status": summary.get("status"),
        "title": title,
    }


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


def _user_texts(messages: list[dict]) -> list[str]:
    texts: list[str] = []
    for message in messages:
        text = _user_text(message)
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


def _dialog_title(messages: list[dict], asks: list[str] | None = None) -> str:
    """Имя как в TUI: ``task`` хода, иначе авто-имя из промптов.

    ``asks`` — ``user_ask`` журнала намерений. Он переживает компакцию, живая
    история нет. Сначала он, потом промпты истории: короткий хвост длинное
    имя не сбивает.
    """
    name = _Name()
    for text in asks or []:
        name.last_actual = _next_prompt(name.last_actual, text)
    for message in messages:
        _fold_name(name, message)
    if name.task:
        return name.task
    return name.last_actual


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


async def _cwd(index: ResponseIndex, thread_id: str) -> str | None:
    records = await _thread_records(index, thread_id)
    if records is None:
        return None
    return chain(records)[-1].cwd


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
