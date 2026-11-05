"""Вызов тула в треде → короткая карточка между прозой.

Живой ход дописывает историю треда, пока stream() молчит на исполнении
тула. Хвост истории читается отдельно (``Threads.tail``) и не повторяет
записи, которые уже были до этого prompt.

Вызовы внутри субагента лежат не в истории, а в потоке ``subagent_events``.
Для клиента ACP это те же действия агента: та же карточка ``tool_call``, без
отдельной вложенности.
"""

from __future__ import annotations

import json
import os
from typing import Any

from gc_core.constants.tool_names import (
    FS_APPEND_FILE_NAME,
    FS_APPLY_FILE_EDITS_NAME,
    FS_READ_FILE_NAME,
    FS_SAVE_BASE64_FILE_NAME,
    FS_SEARCH_AND_REPLACE_NAME,
    FS_WRITE_FILE_NAME,
    INTERMEDIATE_REPLY_TOOL_NAME,
    SESSION_THINK_TOOL_NAME,
    TURN_FINALIZING_TOOLS,
)
from gc_core.execution.progress import (
    brief_args_summary,
    parse_tool_result_status,
)
from gc_core.message_policy.nudges import output_for_card
from gc_core.threads.facade import Cursor, Threads
from gc_core.threads.models import StreamEntry, ThreadKey

_CALL = "function_call"
_CALL_OUTPUT = "function_call_output"
_HISTORY = "messages"
_SUBAGENTS = "subagent_events"
_SIDECAR_CALL = "call"
_SIDECAR_DONE = frozenset({"result", "error"})
_IN_PROGRESS = "in_progress"
_COMPLETED = "completed"
_FAILED = "failed"
# Завершающие ход тулы (финал, вопрос, план) и служебные: их текст клиенту
# показывает фасад сам, карточкой они только дублировали бы его. Новый
# завершающий тул скрывается здесь без правки списка.
_HIDDEN = TURN_FINALIZING_TOOLS | {
    INTERMEDIATE_REPLY_TOOL_NAME,
    SESSION_THINK_TOOL_NAME,
}
# Потолок строки на карточке: и аргумент, и превью результата.
_CARD_LIMIT = 5000


def report_from_record(
    message: dict, seen: set[str], finished: set[str] | None = None
) -> dict | None:
    """Один function_call или его результат. Чужое и служебное — None."""
    kind = message.get("type")
    if kind == _CALL:
        return _call_report(message, seen)
    if kind == _CALL_OUTPUT:
        return _output_report(message, seen, finished)
    return None


def report_from_sidecar(
    record: dict, seen: set[str], finished: set[str] | None = None
) -> dict | None:
    """Запись sidecar → та же карточка, что у тула родителя.

    ``call_id`` внутри субагента свой, и он может совпасть с id родителя.
    В карточке id составной: родитель и вызов через двоеточие.
    """
    kind = record.get("kind")
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return None
    message = _scoped_payload(payload, record.get("parent_call_id"))
    if kind == _SIDECAR_CALL:
        return _call_report(message, seen)
    if kind not in _SIDECAR_DONE:
        return None
    update = _output_report(message, seen, finished)
    if update is not None and kind == "error":
        return {**update, "status": _FAILED}
    return update


class ToolFlow:
    """Хвост треда с момента :meth:`start`. Повторный poll дочитывает новое.

    История и вызовы субагента за один poll сливаются по ``ts``, чтобы тул
    внутри субагента встал между стартом субагента и его результатом, а не
    после. Сжатие истории посреди хода начинает её чтение заново; уже
    показанные карточки не повторяются.
    """

    def __init__(self, threads: Threads, key: ThreadKey) -> None:
        self._threads = threads
        self._key = key
        self._cursor: Cursor | None = None
        # Срез поиска текущего вызова — с создания хода.
        self._origin: Cursor | None = None
        self._seen: set[str] = set()
        self._finished: set[str] = set()

    @classmethod
    async def start(cls, threads: Threads, key: ThreadKey) -> ToolFlow:
        flow = cls(threads, key)
        flow._cursor = (
            await threads.tail(key, (_HISTORY, _SUBAGENTS), None)
        ).cursor
        flow._origin = flow._cursor
        return flow

    async def poll(self) -> list[dict]:
        batch = await self._threads.tail(
            self._key, (_HISTORY, _SUBAGENTS), self._cursor
        )
        self._cursor = batch.cursor
        ordered = sorted(
            (entry.ts, order, stream, entry)
            for order, (stream, entry) in enumerate(batch.entries)
        )
        updates: list[dict] = []
        for _ts, _order, stream, entry in ordered:
            update = _decode(stream, entry, self._seen, self._finished)
            if update is not None:
                updates.append(update)
        return updates

    async def current_call_id(self) -> str | None:
        """Вызов, который исполняется сейчас. Id тот же, что у карточки.

        Срез — с создания хода: незаконченный вызов прошлого prompt не
        прилипает. Родительский батч уже лежит в истории и идёт с начала,
        поэтому текущий — первый без output. Вложенный тул дописывается
        в поток субагента в момент старта, поэтому текущий — последний
        открытый там. Сжатие истории читает её заново с начала: курсор
        среза помнит версию истории на старте хода.
        """
        if self._origin is None:
            return None
        batch = await self._threads.tail(
            self._key, (_HISTORY, _SUBAGENTS), self._origin
        )
        nested = _open_call_ids(batch.entries, _SUBAGENTS)
        if nested:
            return nested[-1]
        parent = _open_call_ids(batch.entries, _HISTORY)
        if parent:
            return parent[0]
        return None


def _open_call_ids(
    entries: list[tuple[str, StreamEntry]], wanted: str
) -> list[str]:
    seen: set[str] = set()
    finished: set[str] = set()
    order: list[str] = []
    for stream, entry in entries:
        if stream != wanted:
            continue
        update = _decode(stream, entry, seen, finished)
        if update is None or update.get("sessionUpdate") != "tool_call":
            continue
        call_id = update.get("toolCallId")
        if isinstance(call_id, str) and call_id != "":
            order.append(call_id)
    return [item for item in order if item not in finished]


def _call_report(message: dict, seen: set[str]) -> dict | None:
    name = message.get("name")
    call_id = _call_id(message)
    if not isinstance(name, str) or name == "" or name in _HIDDEN:
        return None
    if call_id is None or call_id in seen:
        return None
    seen.add(call_id)
    arguments = message.get("arguments")
    report: dict[str, Any] = {
        "sessionUpdate": "tool_call",
        "toolCallId": call_id,
        "title": _title(name, arguments),
        "status": _IN_PROGRESS,
        "name": name,
    }
    parsed = _argument_object(arguments)
    if parsed is None:
        return report
    shown = _arguments_for_card(name, parsed)
    report["title"] = _title(name, shown)
    report["rawInput"] = _truncate_values(shown)
    locations = _locations(name, parsed)
    if locations is not None:
        report["locations"] = locations
    return report


def _output_report(
    message: dict, seen: set[str], finished: set[str] | None = None
) -> dict | None:
    call_id = _call_id(message)
    if call_id is None or call_id not in seen:
        return None
    if finished is not None and call_id in finished:
        return None
    if finished is not None:
        finished.add(call_id)
    output = message.get("output")
    ok, _brief = parse_tool_result_status(output)
    status = _FAILED if ok is False else _COMPLETED
    report: dict[str, Any] = {
        "sessionUpdate": "tool_call_update",
        "toolCallId": call_id,
        "status": status,
    }
    preview = _preview_text(output)
    if preview is not None:
        report["content"] = [
            {
                "type": "content",
                "content": {"type": "text", "text": preview},
            }
        ]
    return report


def _scoped_payload(payload: dict, parent: Any) -> dict:
    call_id = payload.get("call_id")
    if (
        not isinstance(parent, str)
        or parent == ""
        or not isinstance(call_id, str)
        or call_id == ""
    ):
        return payload
    scoped = dict(payload)
    scoped["call_id"] = f"{parent}:{call_id}"
    return scoped


def _argument_object(arguments: Any) -> dict | None:
    """Объект аргументов. Необъект и неразборная строка — None."""
    if isinstance(arguments, dict):
        return arguments
    if not isinstance(arguments, str) or arguments == "":
        return None
    try:
        parsed = json.loads(arguments)
    except json.JSONDecodeError:
        return None
    if isinstance(parsed, dict):
        return parsed
    return None


def _truncate_values(value: Any) -> Any:
    """Копия для карточки. Исходные аргументы вызова не меняются."""
    if isinstance(value, str):
        return value[:_CARD_LIMIT]
    if isinstance(value, dict):
        return {key: _truncate_values(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_truncate_values(item) for item in value]
    return value


def _locations(name: str, arguments: dict) -> list[dict] | None:
    path = arguments.get("path")
    if not isinstance(path, str) or path == "":
        return None
    item: dict[str, Any] = {"path": os.path.abspath(path)}
    offset = arguments.get("offset")
    if (
        name == FS_READ_FILE_NAME
        and isinstance(offset, int)
        and not isinstance(offset, bool)
        and offset >= 1
    ):
        item["line"] = offset
    return [item]


# Текст файла в аргументах. В карточке он рисуется как markdown и раздувает
# вёрстку так же, как тело чтения. Сам вызов эти поля по-прежнему получает.
_FILE_TEXT_KEYS = {
    FS_WRITE_FILE_NAME: frozenset({"content"}),
    FS_APPEND_FILE_NAME: frozenset({"content"}),
    FS_SAVE_BASE64_FILE_NAME: frozenset({"data"}),
    FS_SEARCH_AND_REPLACE_NAME: frozenset({"search", "replace"}),
}
_EDIT_TEXT_KEYS = frozenset({"search", "replace"})


def _arguments_for_card(name: str, arguments: dict) -> dict:
    """Аргументы карточки без текста, который тул пишет в файл."""
    hidden = _FILE_TEXT_KEYS.get(name)
    if hidden is not None:
        return {
            key: value for key, value in arguments.items() if key not in hidden
        }
    if name != FS_APPLY_FILE_EDITS_NAME:
        return arguments
    shown = dict(arguments)
    edits = shown.get("edits")
    if not isinstance(edits, list):
        return shown
    shown["edits"] = [_edit_for_card(item) for item in edits]
    return shown


def _edit_for_card(item: Any) -> Any:
    if not isinstance(item, dict):
        return item
    return {
        key: value for key, value in item.items() if key not in _EDIT_TEXT_KEYS
    }


def _without_read_body(value: Any) -> Any:
    """Карточка чтения показывает метаданные, не текст файла.

    Плагин гоняет тело карточки через markdown. Исходник с ``$`` и
    обратными слэшами на этом пути зацикливает вёрстку. Модели файл
    по-прежнему приходит целиком, из карточки уходит только ``content``.
    """
    if not isinstance(value, dict) or value.get("tool") != FS_READ_FILE_NAME:
        return value
    if "content" not in value:
        return value
    shown = dict(value)
    shown.pop("content", None)
    return shown


def _preview_text(output: Any) -> str | None:
    """Начало результата, не длиннее потолка карточки."""
    if output is None:
        return None
    value = output
    if isinstance(output, str):
        if output == "":
            return None
        output = output_for_card(output)
        try:
            value = json.loads(output)
        except json.JSONDecodeError:
            value = output
    value = _without_read_body(value)
    if value is None or value == "":
        return None
    if isinstance(value, str):
        text = value
    elif isinstance(value, (dict, list, int, float, bool)):
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    else:
        text = str(value)
    if text == "":
        return None
    return text[:_CARD_LIMIT]


def _title(name: str, arguments: Any) -> str:
    preview = brief_args_summary(arguments, tool_name=name)
    if preview == "":
        return name
    return f"{name}({preview})"


def _call_id(message: dict) -> str | None:
    call_id = message.get("call_id")
    if isinstance(call_id, str) and call_id != "":
        return call_id
    fallback = message.get("id")
    if isinstance(fallback, str) and fallback != "":
        return fallback
    return None


def _decode(
    stream: str, entry: StreamEntry, seen: set[str], finished: set[str]
) -> dict | None:
    if stream == _SUBAGENTS:
        record = {"ts": entry.ts, "seq": entry.seq, **entry.data}
        return report_from_sidecar(record, seen, finished)
    return report_from_record(entry.data, seen, finished)
