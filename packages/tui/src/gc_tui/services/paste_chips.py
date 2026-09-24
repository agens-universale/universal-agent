"""Сворачивание объёмной вставки в плашку: пороги, подпись, стор, склейка.

Технический слой — без Textual, без UI, без бизнес-логики. Чистые функции +
модель данных ChipStore. Подпись плашки всегда однострочна (нет \\n) — это
гарантирует, что спан плашки не пересекает строки (см. token_spans)."""

from __future__ import annotations

COLLAPSE_MIN_LINES = 5
COLLAPSE_MIN_CHARS = 500


def should_collapse(text: str) -> bool:
    """True — если вставку нужно свернуть в плашку (по строкам ИЛИ символам)."""
    if not text or not text.strip():
        return False
    lines = text.count("\n") + 1
    return lines >= COLLAPSE_MIN_LINES or len(text) >= COLLAPSE_MIN_CHARS


def _plural_lines(n: int) -> str:
    if 11 <= (n % 100) <= 14:
        return "строк"
    d = n % 10
    if d == 1:
        return "строка"
    if 2 <= d <= 4:
        return "строки"
    return "строк"


def chip_label(n: int, text: str) -> str:
    """Подпись плашки: многострочная → `[Вставка #N · K строк]`,
    одна длинная строка → `[Вставка #N · K симв.]`."""
    lines = text.count("\n") + 1
    if lines >= 2:
        return f"[Вставка #{n} · {lines} {_plural_lines(lines)}]"
    return f"[Вставка #{n} · {len(text)} симв.]"


class ChipStore:
    """Содержимое плашек текущего ввода: id → полный текст. Нумерация монотонная
    и стабильная при удалении; reset() обнуляет счётчик (после отправки/очистки)."""

    def __init__(self) -> None:
        self._chips: dict[int, str] = {}
        self._counter: int = 0

    def add(self, text: str) -> tuple[int, str]:
        self._counter += 1
        cid = self._counter
        self._chips[cid] = text
        return cid, chip_label(cid, text)

    def get(self, cid: int) -> str | None:
        return self._chips.get(cid)

    def remove(self, cid: int) -> None:
        self._chips.pop(cid, None)

    def reset(self) -> None:
        self._chips.clear()
        self._counter = 0

    def labels(self) -> dict[int, str]:
        """Актуальные id → подписи (детерминированы по (id, текст))."""
        return {cid: chip_label(cid, t) for cid, t in self._chips.items()}


def token_spans(
    text: str, store: ChipStore
) -> list[tuple[int, int, int, int]]:
    """Границы плашек в тексте: список (chip_id, row, col_start, col_end).
    Подпись однострочна → спан в пределах одной строки."""
    spans: list[tuple[int, int, int, int]] = []
    labels = store.labels()
    for row, line in enumerate(text.split("\n")):
        for cid, label in labels.items():
            start = line.find(label)
            while start != -1:
                spans.append((cid, row, start, start + len(label)))
                start = line.find(label, start + len(label))
    return spans


def expand(text: str, store: ChipStore) -> str:
    """Заменить каждую плашку на её полный текст по найденным границам.

    Границы берём из token_spans ОДИН раз по исходному буферу и сплайсим
    точные участки (справа налево, чтобы офсеты оставались валидными). Так
    подставленный текст плашки не пере-сканируется — даже если он сам содержит
    подпись другой плашки, повторного разворота не будет."""
    spans = token_spans(text, store)
    if not spans:
        return text
    lines = text.split("\n")
    by_row: dict[int, list[tuple[int, int, int]]] = {}
    for cid, row, c0, c1 in spans:
        by_row.setdefault(row, []).append((c0, c1, cid))
    for row, items in by_row.items():
        line = lines[row]
        for c0, c1, cid in sorted(items, reverse=True):
            full = store.get(cid)
            if full is not None:
                line = line[:c0] + full + line[c1:]
        lines[row] = line
    return "\n".join(lines)
