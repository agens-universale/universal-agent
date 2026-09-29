"""``kind`` ядра → события SSE и элементы ``output``."""

from __future__ import annotations

import copy
import uuid
from dataclasses import dataclass
from typing import Any

_ANSWER = "answer"
_THINK = "think"
_MESSAGE = "message"
_REASONING = "reasoning"
_TERMINAL = {
    "completed": "response.completed",
    "incomplete": "response.incomplete",
    "cancelled": "response.cancelled",
    "failed": "response.failed",
}


def unseen_answer(sent: str, final: Any) -> str:
    if not isinstance(final, str) or final == "":
        return ""
    if final.startswith(sent):
        return final[len(sent) :]
    if final in sent:
        return ""
    return final


@dataclass
class _OpenText:
    kind: str
    index: int
    item_id: str
    text: str


class StreamMap:
    """Один элемент на непрерывную серию answer или think."""

    def __init__(
        self,
        *,
        response_id: str,
        model: str,
        previous_response_id: str | None,
        metadata: dict,
        created_at: int,
    ) -> None:
        self.response_id = response_id
        self._model = model
        self._previous = previous_response_id
        self._metadata = metadata
        self._created_at = created_at
        self._seq: list[dict] = []
        self._open: _OpenText | None = None
        self._answer_since_reset = ""

    def opening_events(self) -> list[dict]:
        return [
            {"type": "response.created", "response": self.snapshot()},
            {"type": "response.in_progress", "response": self.snapshot()},
        ]

    def feed(self, kind: str, text: str) -> list[dict]:
        if kind == "reset":
            return self._reset()
        if kind not in (_ANSWER, _THINK):
            return []
        if not isinstance(text, str) or text == "":
            return []
        target = _MESSAGE if kind == _ANSWER else _REASONING
        events = self._switch_to(target)
        events.extend(self._delta(target, text))
        return events

    def apply_tool(self, report: dict) -> list[dict]:
        if report.get("op") == "open":
            return self._open_tool(report)
        if report.get("op") == "close":
            return self._close_tool(report)
        return []

    def flush_unseen(self, last_answer: Any) -> list[dict]:
        extra = unseen_answer(self._answer_since_reset, last_answer)
        if extra == "":
            return []
        return self.feed(_ANSWER, extra)

    def seal_events(
        self,
        status: str,
        *,
        error: dict | None,
        incomplete_details: dict | None,
    ) -> list[dict]:
        events = self._close_text()
        events.extend(self._finish_open_tools())
        body = self.snapshot(
            status, error=error, incomplete_details=incomplete_details
        )
        events.append({"type": _TERMINAL[status], "response": body})
        return events

    def snapshot(
        self,
        status: str = "in_progress",
        *,
        error: dict | None = None,
        incomplete_details: dict | None = None,
    ) -> dict:
        final = status != "in_progress"
        return {
            "id": self.response_id,
            "object": "response",
            "created_at": self._created_at,
            "status": status,
            "model": self._model,
            "previous_response_id": self._previous,
            "output": self._output(final=final),
            "error": error if status == "failed" else None,
            "incomplete_details": (
                incomplete_details if status == "incomplete" else None
            ),
            "metadata": dict(self._metadata),
            "usage": None,
        }

    def _switch_to(self, kind: str) -> list[dict]:
        if self._open is not None and self._open.kind == kind:
            return []
        events = self._close_text()
        if kind == _MESSAGE:
            events.extend(self._open_message())
        else:
            events.extend(self._open_reasoning())
        return events

    def _delta(self, kind: str, text: str) -> list[dict]:
        open_item = self._open
        if open_item is None:
            return []
        open_item.text += text
        self._write_open_text(open_item)
        if kind == _MESSAGE:
            self._answer_since_reset += text
            return [_text_delta(open_item, text)]
        return [_reasoning_delta(open_item, text)]

    def _reset(self) -> list[dict]:
        self._answer_since_reset = ""
        open_item = self._open
        if open_item is None:
            return []
        del self._seq[open_item.index]
        self._open = None
        return [
            {
                "type": "response.gc_stream_reset",
                "output_index": open_item.index,
                "item_id": open_item.item_id,
            }
        ]

    def _open_message(self) -> list[dict]:
        item_id = "msg_" + str(uuid.uuid4())
        item = _message_item(item_id, "", "in_progress")
        index = self._append(item)
        self._open = _OpenText(_MESSAGE, index, item_id, "")
        return [
            _item_added(index, item),
            _content_added(item_id, index),
        ]

    def _open_reasoning(self) -> list[dict]:
        item_id = "rs_" + str(uuid.uuid4())
        item = _reasoning_item(item_id, "")
        index = self._append(item)
        self._open = _OpenText(_REASONING, index, item_id, "")
        return [
            _item_added(index, item),
            _summary_added(item_id, index),
        ]

    def _close_text(self) -> list[dict]:
        open_item = self._open
        if open_item is None:
            return []
        self._open = None
        item = self._seq[open_item.index]
        if open_item.kind == _MESSAGE:
            item["status"] = "completed"
            return _message_done(open_item, item)
        return _reasoning_done(open_item, item)

    def _open_tool(self, report: dict) -> list[dict]:
        events = self._close_text()
        call_id = str(report["call_id"])
        item = {
            "type": "gc_tool_call",
            "id": "gtc_" + call_id,
            "call_id": call_id,
            "name": report["name"],
            "title": report["title"],
            "status": "in_progress",
        }
        events.append(_item_added(self._append(item), item))
        return events

    def _close_tool(self, report: dict) -> list[dict]:
        found = _find_tool(self._seq, str(report["call_id"]))
        if found is None:
            return []
        index, item = found
        item["status"] = report["status"]
        return [_item_done(index, item)]

    def _finish_open_tools(self) -> list[dict]:
        events: list[dict] = []
        for index, item in enumerate(self._seq):
            if item.get("type") != "gc_tool_call":
                continue
            if item.get("status") != "in_progress":
                continue
            item["status"] = "completed"
            events.append(_item_done(index, item))
        return events

    def _append(self, item: dict) -> int:
        self._seq.append(item)
        return len(self._seq) - 1

    def _write_open_text(self, open_item: _OpenText) -> None:
        item = self._seq[open_item.index]
        if open_item.kind == _MESSAGE:
            item["content"][0]["text"] = open_item.text
            return
        item["summary"][0]["text"] = open_item.text

    def _output(self, *, final: bool) -> list[dict]:
        if final:
            return [copy.deepcopy(item) for item in self._seq]
        visible: list[dict] = []
        open_index = None if self._open is None else self._open.index
        for index, item in enumerate(self._seq):
            if index == open_index:
                continue
            if item.get("status") == "in_progress":
                continue
            visible.append(copy.deepcopy(item))
        return visible


def _message_item(item_id: str, text: str, status: str) -> dict:
    return {
        "type": "message",
        "id": item_id,
        "status": status,
        "role": "assistant",
        "content": [
            {"type": "output_text", "text": text, "annotations": []},
        ],
    }


def _reasoning_item(item_id: str, text: str) -> dict:
    return {
        "id": item_id,
        "type": "reasoning",
        "summary": [{"type": "summary_text", "text": text}],
    }


def _item_added(index: int, item: dict) -> dict:
    return {
        "type": "response.output_item.added",
        "output_index": index,
        "item": copy.deepcopy(item),
    }


def _item_done(index: int, item: dict) -> dict:
    return {
        "type": "response.output_item.done",
        "output_index": index,
        "item": copy.deepcopy(item),
    }


def _content_added(item_id: str, index: int) -> dict:
    return {
        "type": "response.content_part.added",
        "item_id": item_id,
        "output_index": index,
        "content_index": 0,
        "part": {"type": "output_text", "text": "", "annotations": []},
    }


def _summary_added(item_id: str, index: int) -> dict:
    return {
        "type": "response.reasoning_summary_part.added",
        "item_id": item_id,
        "output_index": index,
        "summary_index": 0,
        "part": {"type": "summary_text", "text": ""},
    }


def _text_delta(open_item: _OpenText, text: str) -> dict:
    return {
        "type": "response.output_text.delta",
        "item_id": open_item.item_id,
        "output_index": open_item.index,
        "content_index": 0,
        "delta": text,
    }


def _reasoning_delta(open_item: _OpenText, text: str) -> dict:
    return {
        "type": "response.reasoning_summary_text.delta",
        "item_id": open_item.item_id,
        "output_index": open_item.index,
        "summary_index": 0,
        "delta": text,
    }


def _message_done(open_item: _OpenText, item: dict) -> list[dict]:
    part = copy.deepcopy(item["content"][0])
    return [
        {
            "type": "response.output_text.done",
            "item_id": open_item.item_id,
            "output_index": open_item.index,
            "content_index": 0,
            "text": open_item.text,
        },
        {
            "type": "response.content_part.done",
            "item_id": open_item.item_id,
            "output_index": open_item.index,
            "content_index": 0,
            "part": part,
        },
        _item_done(open_item.index, item),
    ]


def _reasoning_done(open_item: _OpenText, item: dict) -> list[dict]:
    part = copy.deepcopy(item["summary"][0])
    return [
        {
            "type": "response.reasoning_summary_text.done",
            "item_id": open_item.item_id,
            "output_index": open_item.index,
            "summary_index": 0,
            "text": open_item.text,
        },
        {
            "type": "response.reasoning_summary_part.done",
            "item_id": open_item.item_id,
            "output_index": open_item.index,
            "summary_index": 0,
            "part": part,
        },
        _item_done(open_item.index, item),
    ]


def _find_tool(seq: list[dict], call_id: str) -> tuple[int, dict] | None:
    for index, item in enumerate(seq):
        if item.get("type") != "gc_tool_call":
            continue
        if item.get("call_id") == call_id:
            return index, item
    return None
