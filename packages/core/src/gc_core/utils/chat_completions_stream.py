"""SSE parsing + reassembly for streamed chat/completions.

Совместимостный фасад. Сама сборка живёт в ``utils.stream_assembler``
(push-парсер: состояние в объекте, источник строк — не его забота), потому что
асинхронному транспорту нужен тот же разбор, а второй копии быть не должно.

Здесь остались имена, которыми пользуется остальной код: ``BackendStreamError``
(его ловит политика ретраев), ``iter_sse_data_lines`` и
``assemble_streamed_completion``.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, Optional

from gc_core.utils.stream_assembler import (  # публичные имена фасада
    BackendStreamError,
    OnDelta,
    StreamAssembler,
)


def without_answer_deltas(on_delta: OnDelta) -> OnDelta:
    """Тот же приёмник без канала `answer`: `reset`, `think` и `tool` проходят как
    есть. Кому глушить текст ответа, решает вызывающий — здесь только фильтр по
    виду дельты."""

    def emit(kind: str, text: str) -> None:
        if kind != "answer":
            on_delta(kind, text)

    return emit


def iter_sse_data_lines(raw_lines: Iterable) -> Iterable[str]:
    """Yield JSON payload strings from SSE `data:` lines; stop at `[DONE]`."""
    for line in raw_lines:
        if isinstance(line, (bytes, bytearray)):
            line = line.decode("utf-8", errors="replace")
        line = (line or "").strip()
        if not line or not line.startswith("data:"):
            continue
        data = line[len("data:") :].strip()
        if data == "[DONE]":
            return
        yield data


def assemble_streamed_completion(
    data_payloads: Iterable[str],
    on_delta: Optional[OnDelta] = None,
) -> Dict[str, Any]:
    """Reassemble a chat.completion object from SSE chunk JSON payloads.

    Emits on_delta("think"|"answer"|"tool", text): reasoning and content inside
    inline `<think>...</think>` -> think; content outside -> answer.
    `message.content` is preserved verbatim (raw `<think>` included; downstream
    strips it). `finish_reason` may be `None` if the stream ends without one
    (e.g. a truncated/interrupted stream); this is intentional and surfaces
    downstream as an error rather than being masked by a fake default.
    """
    assembler = StreamAssembler(on_delta=on_delta)
    for payload in data_payloads:
        assembler.feed_payload(payload)
    assembler.flush()
    return assembler.result()


__all__ = [
    "BackendStreamError",
    "OnDelta",
    "StreamAssembler",
    "assemble_streamed_completion",
    "iter_sse_data_lines",
    "without_answer_deltas",
]
