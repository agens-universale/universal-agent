"""Плашки картинок в отправленном тексте: поиск, вырезание, сцепка по подписи.

Технический слой TUI без Textual. Плашка вложения в тексте — `[<label>]`; для
пользовательской картинки это `chip_text(N)` ядра (`label == user_label(N)`).
Поле ввода держит плашки в `ChipStore` (`paste_chips`); здесь — текст, который
уже отправлен: лента, история ↑, имя сессии, откат отправки."""

from __future__ import annotations

from typing import Mapping, Sequence

from gc_core.attachments.model import Attachment, chip_of, label_number


def chip_number(att: Attachment) -> int:
    """N пользовательской плашки по подписи вложения (`user_label(N)`); чужая
    подпись — `ValueError`."""
    number = label_number(att.label)
    if number is None:
        raise ValueError(f"not a user image label: {att.label!r}")
    return number


def chip_spans(
    line: str, images: Sequence[Attachment]
) -> list[tuple[int, int, Attachment]]:
    """Все вхождения плашек вложений в строке: (начало, конец, вложение) по порядку.

    Подпись заканчивается `]`, поэтому `#1` не находится внутри `#10`."""
    spans: list[tuple[int, int, Attachment]] = []
    for chip, att in {chip_of(a): a for a in images}.items():
        start = line.find(chip)
        while start != -1:
            spans.append((start, start + len(chip), att))
            start = line.find(chip, start + len(chip))
    return sorted(spans, key=lambda span: span[0])


def strip_image_chips(text: str, images: Sequence[Attachment]) -> str:
    """Текст без плашек вложений (история ↑, имя сессии): плашка уходит вместе с
    одним пробелом после неё; сообщение из одних картинок даёт пустую строку."""
    for chip in {chip_of(a) for a in images}:
        text = text.replace(chip + " ", "").replace(chip, "")
    return text.strip()


def images_mentioned(
    text: str, index: Mapping[str, Attachment]
) -> tuple[Attachment, ...]:
    """Вложения индекса, чьи плашки есть в тексте, — в порядке первого вхождения.

    Сцепка ответа на turn_ask_human с картинками из следующего технического
    сообщения (дизайн картинок §20.3): номер сквозной по треду, подпись уникальна."""
    found = [
        (text.find(chip_of(att)), att)
        for att in index.values()
        if chip_of(att) in text
    ]
    return tuple(att for _pos, att in sorted(found, key=lambda pair: pair[0]))
