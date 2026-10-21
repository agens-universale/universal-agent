"""Сквозная нумерация плашек ``[Изображение #N]`` в треде.

Счётчик — ``RuntimeStatus.image_seq`` записи треда; поднимает его сессия
при записи шага (``threads.runtime_rules.bumped_image_seq``) по номерам
из подписей картинок. Компакция счётчик не уменьшает: записи уходят в
архив, живой статус остаётся.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from gc_core.attachments.model import ORIGIN_USER, attachments_of, label_number


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
