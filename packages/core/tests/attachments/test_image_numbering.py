"""Сквозная нумерация картинок треда: номера из подписей. Счётчик —
``threads/test_runtime_rules.py``."""

from gc_core.attachments.model import (
    ORIGIN_TOOL,
    ORIGIN_USER,
    TOOL_LABEL,
    Attachment,
    user_label,
)
from gc_core.attachments.numbering import label_numbers


def _record(label: str, origin: str = ORIGIN_USER) -> dict:
    return Attachment(
        id="0123456789abcdef",
        kind="image",
        mime="image/png",
        width=40,
        height=40,
        size=100,
        path="/t/x.png",
        origin=origin,
        label=label,
        source="clipboard",
        orig_width=40,
        orig_height=40,
    ).to_dict()


def test_label_numbers_reads_user_images_in_order():
    messages = [
        {
            "role": "user",
            "content": "[Изображение #3] [Изображение #5]",
            "attachments": [_record(user_label(3)), _record(user_label(5))],
        },
        {"role": "assistant", "content": "ok"},
        {
            "role": "user",
            "content": "<technical>…</technical>",
            "attachments": [
                _record(TOOL_LABEL, ORIGIN_TOOL),
                _record(user_label(7)),
            ],
        },
    ]
    assert label_numbers(messages) == [3, 5, 7]


def test_label_numbers_ignores_foreign_labels():
    messages = [
        {
            "role": "user",
            "content": "x",
            "attachments": [
                _record("Изображение #007"),
                _record("Скриншот 12"),
                _record(user_label(2), ORIGIN_TOOL),
            ],
        }
    ]
    assert label_numbers(messages) == []
