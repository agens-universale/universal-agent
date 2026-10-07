"""Сквозная нумерация плашек ``[Изображение #N]`` в треде.

Счётчик ``image_seq`` живёт в ``meta["runtime"]`` треда (ключ ``runtime`` —
ядра, ``persistence/meta_ownership.py``). Модуль только читает счётчик и
считает новое значение; записывает его тот, кто сохраняет сообщения.
Компакция счётчик не уменьшает: записи уходят в архив, meta остаётся.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from gc_core.attachments.model import ORIGIN_USER, attachments_of, label_number

IMAGE_SEQ_KEY = "image_seq"


def label_numbers(messages: Iterable[Mapping[str, Any]]) -> list[int]:
    """Номера пользовательских картинок в сообщениях — в порядке появления."""
    numbers: list[int] = []
    for message in messages:
        for att in attachments_of(message):
            if att.origin != ORIGIN_USER:
                continue
            number = label_number(att.label)
            if number is not None:
                numbers.append(number)
    return numbers


def read_image_seq(meta: Mapping[str, Any] | None) -> int:
    """Последний выданный номер треда; нет счётчика или он битый — 0."""
    # Импорт внутри функции: пакет ``persistence`` при загрузке тянет states,
    # реестр тулов и security, а пакет ``attachments`` обязан грузиться лёгким —
    # его импортируют тулы и ``security/media_roots.py`` (иначе цикл импорта).
    from gc_core.persistence.meta_ownership import runtime_section

    value = runtime_section(meta).get(IMAGE_SEQ_KEY)
    return value if type(value) is int and value > 0 else 0


def bumped_image_seq(
    meta: Mapping[str, Any] | None, messages: Iterable[Mapping[str, Any]]
) -> int:
    """Счётчик после сохранения ``messages``: ``max(image_seq, max N в батче)``."""
    return max([read_image_seq(meta), *label_numbers(messages)])
