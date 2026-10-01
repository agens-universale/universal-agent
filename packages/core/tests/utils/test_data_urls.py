"""Формат data-URL картинок (utils/data_urls): сборка для запроса, маскировка для логов."""

import copy

from gc_core.utils.data_urls import data_url, mask_data_urls


def test_data_url_is_mime_and_base64_payload():
    assert data_url("image/png", b"\x00\x01") == "data:image/png;base64,AAE="


def test_mask_replaces_the_data_with_its_length():
    value = {
        "messages": [
            {
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64,QUJD"},
                    },
                    {"type": "text", "text": "что тут?"},
                ]
            }
        ]
    }
    assert mask_data_urls(value) == {
        "messages": [
            {
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": "data:image/png;base64,<4 bytes>"
                        },
                    },
                    {"type": "text", "text": "что тут?"},
                ]
            }
        ]
    }


def test_mask_finds_a_data_url_inside_text():
    assert (
        mask_data_urls("см. data:image/jpeg;base64,/9j/AAAA= конец")
        == "см. data:image/jpeg;base64,<9 bytes> конец"
    )


def test_mask_does_not_mutate_the_input():
    value = {
        "url": "data:image/png;base64,QUJD",
        "items": ["data:image/png;base64,QUJD"],
    }
    snapshot = copy.deepcopy(value)
    mask_data_urls(value)
    assert value == snapshot


def test_values_without_data_urls_pass_through():
    assert mask_data_urls("просто текст") == "просто текст"
    assert mask_data_urls(42) == 42
    assert mask_data_urls(None) is None
