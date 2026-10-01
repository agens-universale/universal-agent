"""Плашки поля ввода: объёмная вставка (свёрнутый текст) и изображения — пороги,
подписи, стор, склейка.

Технический слой — без Textual, без UI, без бизнес-логики. Чистые функции +
модель данных ChipStore. Подпись плашки всегда однострочна (нет \\n) — это
гарантирует, что спан плашки не пересекает строки (см. token_spans)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

from gc_core.attachments.model import Attachment, chip_text, user_label

COLLAPSE_MIN_LINES = 5
COLLAPSE_MIN_CHARS = 500

#: Вид плашки — первая часть ключа записи `(вид, номер)`: у каждого вида своя нумерация.
CHIP_TEXT = "text"
CHIP_IMAGE = "image"


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
    """Содержимое плашек текущего ввода по ключу `(вид, номер)`: текстовая — полный
    текст, image — `Attachment` из кармана процесса.

    Текстовые номера выдаёт сам стор (монотонно, стабильно при удалении); номер
    image-плашки сквозной по треду — его выдаёт экран (`add_image`). Удаление плашки
    из текста запись НЕ удаляет: Ctrl+Z возвращает подпись, и она снова плашка (класс
    Ctrl+Z, спека §12). Сброс — только `reset()`: программная очистка поля (отправка,
    двойной Esc)."""

    def __init__(self) -> None:
        self._chips: dict[tuple[str, int], str | Attachment] = {}
        self._counter: int = 0

    def add(self, text: str) -> tuple[int, str]:
        self._counter += 1
        cid = self._counter
        self._chips[(CHIP_TEXT, cid)] = text
        return cid, chip_label(cid, text)

    def get(self, cid: int) -> str | None:
        """Полный текст текстовой плашки #cid."""
        return self._chips.get((CHIP_TEXT, cid))

    def add_image(self, att: Attachment, n: int) -> str:
        """Image-плашка #n. Подпись вложения выставляется по номеру: номер может
        смениться, пока шло чтение (смена сессии), — стор единственный владелец пары
        «номер плашки ↔ подпись вложения»."""
        self._chips[(CHIP_IMAGE, n)] = replace(att, label=user_label(n))
        return chip_text(n)

    def add_images(
        self, images: Sequence[Attachment], first_n: int
    ) -> tuple[list[str], int]:
        """Image-плашки вставки по порядку. Картинка, уже записанная в сторе (тот же `id`
        — ту же картинку вставили снова, в том числе после удаления её плашки), получает
        свою прежнюю плашку, и номер не тратится; новые — #first_n, #first_n+1, …
        Возвращает подписи и сколько номеров занято."""
        known = {
            att.id: n
            for (kind, n), att in self._chips.items()
            if kind == CHIP_IMAGE
        }
        labels: list[str] = []
        used = 0
        for att in images:
            n = known.get(att.id)
            if n is None:
                n = known[att.id] = first_n + used
                used += 1
                self.add_image(att, n)
            labels.append(chip_text(n))
        return labels, used

    def images_in_text(self, text: str) -> list[Attachment]:
        """Вложения сообщения: image-плашки текста в порядке текста, первое вхождение
        каждого `id` (одна картинка двумя плашками — одно вложение, спека §4)."""
        seen: set[str] = set()
        found: list[Attachment] = []
        for key, *_ in sorted(
            token_spans(text, self), key=lambda span: (span[1], span[2])
        ):
            if key[0] != CHIP_IMAGE:
                continue
            att = self._chips[key]
            if att.id not in seen:
                seen.add(att.id)
                found.append(att)
        return found

    def renumber_images(self, first_n: int, text: str) -> dict[str, str]:
        """Перенумеровать image-плашки текста подряд с first_n по возрастанию старых
        номеров (смена сессии: сквозная нумерация нового треда). Записи плашек, которых
        в тексте нет (стёрты, хранились для Ctrl+Z), уходят: номер в новом треде им не
        положен. Возвращает «старая подпись → новая». Текст правят по спанам, снятым ДО
        перенумерации: последовательный str.replace сцепил бы замены (#1→#3, #3→#5)."""
        present = {
            key for key, *_ in token_spans(text, self) if key[0] == CHIP_IMAGE
        }
        old = sorted(
            (
                (n, att)
                for (kind, n), att in self._chips.items()
                if kind == CHIP_IMAGE
            ),
            key=lambda item: item[0],
        )
        for n, _ in old:
            del self._chips[(CHIP_IMAGE, n)]
        mapping: dict[str, str] = {}
        kept = [(n, att) for n, att in old if (CHIP_IMAGE, n) in present]
        for new_n, (n, att) in enumerate(kept, start=first_n):
            self.add_image(att, new_n)
            mapping[chip_text(n)] = chip_text(new_n)
        return mapping

    def reset(self) -> None:
        self._chips.clear()
        self._counter = 0

    def labels(self) -> dict[tuple[str, int], str]:
        """Ключи → подписи (детерминированы по записи)."""
        return {
            key: chip_label(key[1], value)
            if key[0] == CHIP_TEXT
            else chip_text(key[1])
            for key, value in self._chips.items()
        }


def token_spans(
    text: str, store: ChipStore
) -> list[tuple[tuple[str, int], int, int, int]]:
    """Границы плашек в тексте: список ((вид, номер), row, col_start, col_end).
    Подпись однострочна → спан в пределах одной строки. Подпись кончается «]», поэтому
    `[Изображение #1]` не находится внутри `[Изображение #10]`."""
    spans: list[tuple[tuple[str, int], int, int, int]] = []
    labels = store.labels()
    for row, line in enumerate(text.split("\n")):
        for key, label in labels.items():
            start = line.find(label)
            while start != -1:
                spans.append((key, row, start, start + len(label)))
                start = line.find(label, start + len(label))
    return spans


def expand(text: str, store: ChipStore) -> str:
    """Заменить каждую текстовую плашку на её полный текст по найденным границам.
    Image-плашка остаётся подписью: картинка едет вложением, подпись — в тексте.

    Границы берём из token_spans ОДИН раз по исходному буферу и сплайсим
    точные участки (справа налево, чтобы офсеты оставались валидными). Так
    подставленный текст плашки не пере-сканируется — даже если он сам содержит
    подпись другой плашки, повторного разворота не будет."""
    spans = token_spans(text, store)
    if not spans:
        return text
    lines = text.split("\n")
    by_row: dict[int, list[tuple[int, int, int]]] = {}
    for (kind, cid), row, c0, c1 in spans:
        if kind == CHIP_TEXT:
            by_row.setdefault(row, []).append((c0, c1, cid))
    for row, items in by_row.items():
        line = lines[row]
        for c0, c1, cid in sorted(items, reverse=True):
            full = store.get(cid)
            if full is not None:
                line = line[:c0] + full + line[c1:]
        lines[row] = line
    return "\n".join(lines)
