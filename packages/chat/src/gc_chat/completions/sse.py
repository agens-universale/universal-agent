"""Чанк Chat Completions и одна запись SSE ``data:``."""

from __future__ import annotations

import json

_DONE = b"data: [DONE]\n\n"


def completion_object(
    *,
    completion_id: str,
    created: int,
    model: str,
    content: str,
    reason: str,
) -> dict:
    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": reason,
            }
        ],
        "usage": None,
    }


class ChunkEncoder:
    """Первый чанк с текстом несёт ``delta.role``."""

    def __init__(
        self, *, completion_id: str, created: int, model: str
    ) -> None:
        self._id = completion_id
        self._created = created
        self._model = model
        self._role_sent = False

    def content(self, text: str) -> dict:
        delta: dict = {"content": text}
        if not self._role_sent:
            delta["role"] = "assistant"
            self._role_sent = True
        return self._chunk(delta, None)

    def finish(self, reason: str) -> dict:
        return self._chunk({}, reason)

    def _chunk(self, delta: dict, reason: str | None) -> dict:
        return {
            "id": self._id,
            "object": "chat.completion.chunk",
            "created": self._created,
            "model": self._model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": reason}],
        }


def encode_data(payload: dict) -> bytes:
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return ("data: " + raw + "\n\n").encode("utf-8")


def encode_done() -> bytes:
    return _DONE
