"""Модель вложения: сериализация без байтов, подписи плашек, разбор сообщения."""

import json

import pytest

from gc_core.attachments.model import (
    ORIGIN_TOOL,
    ORIGIN_USER,
    TOOL_LABEL,
    Attachment,
    attachments_of,
    chip_of,
    chip_text,
    label_number,
    user_label,
    wh_text,
)


def _att(**overrides) -> Attachment:
    values = dict(
        id="3f9a1c27b0e4d5a6",
        kind="image",
        mime="image/png",
        width=1456,
        height=819,
        size=214880,
        path="/threads/images/cli_t1_3f9a1c27b0e4d5a6.png",
        origin=ORIGIN_USER,
        label="Изображение #8",
        source="clipboard",
        orig_width=2912,
        orig_height=1638,
    )
    values.update(overrides)
    return Attachment(**values)


def test_to_dict_is_a_flat_json_record():
    record = _att().to_dict()
    assert record == {
        "id": "3f9a1c27b0e4d5a6",
        "kind": "image",
        "mime": "image/png",
        "width": 1456,
        "height": 819,
        "size": 214880,
        "path": "/threads/images/cli_t1_3f9a1c27b0e4d5a6.png",
        "origin": "user",
        "label": "Изображение #8",
        "source": "clipboard",
        "orig_width": 2912,
        "orig_height": 1638,
    }
    assert json.loads(json.dumps(record, ensure_ascii=False)) == record


def test_from_dict_round_trips_and_ignores_unknown_keys():
    att = _att(origin=ORIGIN_TOOL, label=TOOL_LABEL)
    assert Attachment.from_dict({**att.to_dict(), "future_field": 1}) == att


def test_from_dict_rejects_a_missing_field():
    record = _att().to_dict()
    del record["path"]
    with pytest.raises(KeyError):
        Attachment.from_dict(record)


@pytest.mark.parametrize(
    "field, value", [("width", "1456"), ("width", True), ("path", None)]
)
def test_from_dict_rejects_a_wrong_type(field, value):
    with pytest.raises(TypeError):
        Attachment.from_dict({**_att().to_dict(), field: value})


def test_labels_and_chip():
    assert user_label(8) == "Изображение #8"
    assert chip_text(8) == "[Изображение #8]"
    assert TOOL_LABEL == "Image from fs_view_image"


def test_chip_of_wraps_the_label():
    assert chip_of(_att(label=user_label(8))) == chip_text(8)
    assert chip_of(_att(label=TOOL_LABEL)) == "[Image from fs_view_image]"


@pytest.mark.parametrize(
    "label, number",
    [
        (user_label(8), 8),
        (user_label(12), 12),
        (TOOL_LABEL, None),
        (
            "Изображение #012",
            None,
        ),  # не подпись user_label: номер без ведущих нулей
        ("Картинка #3", None),
    ],
)
def test_label_number_reads_only_user_labels(label, number):
    assert label_number(label) == number


def test_wh_text_drops_the_mime_prefix():
    assert wh_text(_att()) == "png 1456×819"
    assert (
        wh_text(_att(mime="image/jpeg", width=800, height=600))
        == "jpeg 800×600"
    )


def test_attachments_of_skips_broken_records():
    good = _att()
    message = {
        "role": "user",
        "content": "[Изображение #8] почему кнопка съехала?",
        "attachments": [
            good.to_dict(),
            {"id": "x"},
            "not-a-dict",
            {**good.to_dict(), "width": "1"},
        ],
    }
    assert attachments_of(message) == [good]


@pytest.mark.parametrize(
    "message",
    [
        {"role": "user", "content": "текст"},
        {"role": "user", "content": "текст", "attachments": None},
        {"role": "user", "content": "текст", "attachments": {"id": "x"}},
    ],
)
def test_attachments_of_without_a_list_is_empty(message):
    assert attachments_of(message) == []
