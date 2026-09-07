"""Head+tail preview truncation for exec tool output.

Спека: docs_md/actual/2026-07-24-exec-output-head-tail-truncate.md (D3).

Бюджет ``max_chars`` — на голову + хвост (~поровну); между ними маркер
``\\n...[omitted N chars]...\\n``. Длина маркера в бюджет не вжимается.
"""

from __future__ import annotations

from dataclasses import dataclass

MARKER_TEMPLATE = "\n...[omitted {omitted} chars]...\n"


@dataclass(frozen=True)
class TruncationResult:
    text: str
    truncated: bool
    original_chars: int
    kept_head_chars: int
    kept_tail_chars: int
    omitted_chars: int


def truncate_head_tail(text: str, max_chars: int) -> TruncationResult:
    """Вернуть preview: начало + хвост + маркер omitted.

    ``max_chars`` делится примерно пополам на голову и хвост; маркер вставки
    между ними в этот бюджет не входит (плюс-минус длина маркера не важна).
    """
    original_chars = len(text)

    if max_chars is None or max_chars < 0:
        max_chars = 0

    if original_chars == 0:
        return TruncationResult(
            text="",
            truncated=False,
            original_chars=0,
            kept_head_chars=0,
            kept_tail_chars=0,
            omitted_chars=0,
        )

    if original_chars <= max_chars:
        return TruncationResult(
            text=text,
            truncated=False,
            original_chars=original_chars,
            kept_head_chars=original_chars,
            kept_tail_chars=0,
            omitted_chars=0,
        )

    if max_chars == 0:
        return TruncationResult(
            text="",
            truncated=True,
            original_chars=original_chars,
            kept_head_chars=0,
            kept_tail_chars=0,
            omitted_chars=original_chars,
        )

    head_len = max_chars // 2
    tail_len = max_chars - head_len
    omitted = original_chars - head_len - tail_len
    result_text = text[:head_len] + MARKER_TEMPLATE.format(omitted=omitted) + text[-tail_len:]

    return TruncationResult(
        text=result_text,
        truncated=True,
        original_chars=original_chars,
        kept_head_chars=head_len,
        kept_tail_chars=tail_len,
        omitted_chars=omitted,
    )
