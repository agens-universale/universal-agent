"""Подготовка длинного текста для лога."""


def clip_middle(text: str, max_chars: int) -> str:
    """Длинный текст — началом и концом с полной длиной посередине: по
    началу видно, что это, по концу — где оборвалось."""
    if len(text) <= max_chars:
        return text
    half = max_chars // 2
    return (
        f"{text[:half]} ... [truncated, total_chars={len(text)}] ... "
        f"{text[len(text) - half :]}"
    )
