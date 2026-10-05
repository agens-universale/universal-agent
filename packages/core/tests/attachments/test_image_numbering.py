"""Сквозная нумерация картинок треда: номера из подписей и счётчик в meta.runtime."""

from gc_core.attachments.model import (
    ORIGIN_TOOL,
    ORIGIN_USER,
    TOOL_LABEL,
    Attachment,
    user_label,
)
from gc_core.attachments.numbering import (
    IMAGE_SEQ_KEY,
    bumped_image_seq,
    label_numbers,
    read_image_seq,
)


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


def test_read_image_seq():
    assert IMAGE_SEQ_KEY == "image_seq"
    assert read_image_seq(None) == 0
    assert read_image_seq({}) == 0
    assert read_image_seq({"runtime": {"image_seq": 12}}) == 12
    assert read_image_seq({"runtime": {"image_seq": "12"}}) == 0
    assert read_image_seq({"runtime": {"image_seq": True}}) == 0
    assert read_image_seq({"runtime": "broken"}) == 0


def test_bumped_image_seq_takes_the_max_and_never_decreases():
    meta = {"runtime": {"image_seq": 9}}
    older = [
        {
            "role": "user",
            "content": "x",
            "attachments": [_record(user_label(4))],
        }
    ]
    newer = [
        {
            "role": "user",
            "content": "x",
            "attachments": [_record(user_label(11))],
        }
    ]
    assert bumped_image_seq(meta, older) == 9
    assert bumped_image_seq(meta, newer) == 11
    assert bumped_image_seq(None, newer) == 11
    assert bumped_image_seq(None, []) == 0
