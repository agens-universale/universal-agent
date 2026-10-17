"""Bounded text views preserving the opening and the conclusion."""


def head_tail_preview(text: str, limit: int) -> str:
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    marker = "\n…[середина опущена]…\n"
    if limit <= len(marker):
        return text[-limit:]
    remaining = limit - len(marker)
    head = remaining // 2
    tail = remaining - head
    return text[:head] + marker + text[-tail:]
