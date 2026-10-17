"""Склейка промпта ACP в текст хода."""

import pytest

from gc_acp.prompt import PromptRejected, flatten_prompt


def test_text_and_resource_link_join_with_newline():
    text = flatten_prompt(
        [
            {"type": "text", "text": "see"},
            {
                "type": "resource_link",
                "name": "readme",
                "uri": "file:///tmp/readme",
            },
        ]
    )
    assert text == "see\nreadme (file:///tmp/readme)"


@pytest.mark.parametrize("kind", ["image", "audio", "resource"])
def test_unsupported_block_rejects_the_whole_prompt(kind):
    with pytest.raises(PromptRejected):
        flatten_prompt(
            [
                {"type": "text", "text": "ok"},
                {"type": kind},
            ]
        )


def test_empty_prompt_is_rejected():
    with pytest.raises(PromptRejected):
        flatten_prompt([])


def test_whitespace_prompt_is_rejected():
    with pytest.raises(PromptRejected):
        flatten_prompt([{"type": "text", "text": "  \n  "}])


def test_text_resource_and_link_keep_prompt_order():
    text = flatten_prompt(
        [
            {"type": "text", "text": "see"},
            {
                "type": "resource",
                "resource": {
                    "uri": "file:///tmp/a.py",
                    "mimeType": "text/x-python",
                    "text": "x = 1",
                },
            },
            {
                "type": "resource_link",
                "name": "readme",
                "uri": "file:///tmp/readme",
            },
        ]
    )
    assert text == "see\nfile:///tmp/a.py\nx = 1\nreadme (file:///tmp/readme)"


def test_resource_only_prompt_is_valid():
    text = flatten_prompt(
        [
            {
                "type": "resource",
                "resource": {"uri": "file:///tmp/a.py", "text": "x = 1"},
            },
            {
                "type": "resource",
                "resource": {"uri": "file:///tmp/b.py", "text": "y = 2"},
            },
        ]
    )
    assert text == "file:///tmp/a.py\nx = 1\nfile:///tmp/b.py\ny = 2"


def test_resource_text_is_kept_when_blob_is_also_present():
    text = flatten_prompt(
        [
            {
                "type": "resource",
                "resource": {
                    "uri": "file:///tmp/a.py",
                    "text": "x = 1",
                    "blob": "aaaa",
                },
            }
        ]
    )
    assert text == "file:///tmp/a.py\nx = 1"


@pytest.mark.parametrize(
    "resource",
    [
        {},
        {"text": "x = 1"},
        {"uri": "", "text": "x = 1"},
        {"uri": "file:///tmp/a.py", "text": ""},
        {"uri": "file:///tmp/a.py", "text": "  \n"},
        {"uri": "file:///tmp/a.py", "mimeType": "text/plain"},
    ],
)
def test_resource_without_uri_or_text_rejects_the_whole_prompt(resource):
    with pytest.raises(PromptRejected):
        flatten_prompt(
            [
                {"type": "text", "text": "ok"},
                {"type": "resource", "resource": resource},
            ]
        )


def test_blob_only_resource_names_the_reason():
    with pytest.raises(PromptRejected, match="embedded blob not supported"):
        flatten_prompt(
            [
                {"type": "text", "text": "ok"},
                {
                    "type": "resource",
                    "resource": {
                        "uri": "file:///tmp/a.png",
                        "blob": "aaaa",
                    },
                },
            ]
        )


@pytest.mark.parametrize(
    ("kind", "message"),
    [("image", "image not supported"), ("audio", "audio not supported")],
)
def test_image_and_audio_name_the_reason(kind, message):
    with pytest.raises(PromptRejected, match=message):
        flatten_prompt(
            [{"type": "text", "text": "ok"}, {"type": kind, "data": "qq"}]
        )


def test_overlong_resource_text_is_cut_to_256_kib():
    # 262143 байта ASCII и «я» (2 байта): срез приходится на середину символа.
    body = ("a" * (256 * 1024 - 1)) + "я"
    text = flatten_prompt(
        [
            {
                "type": "resource",
                "resource": {"uri": "file:///tmp/a.py", "text": body},
            }
        ]
    )
    assert text == (
        "file:///tmp/a.py\n" + ("a" * (256 * 1024 - 1)) + "\n[truncated]"
    )


def test_resource_text_at_the_ceiling_is_not_marked():
    body = "a" * (256 * 1024)
    text = flatten_prompt(
        [
            {
                "type": "resource",
                "resource": {"uri": "file:///tmp/a.py", "text": body},
            }
        ]
    )
    assert text == f"file:///tmp/a.py\n{body}"
