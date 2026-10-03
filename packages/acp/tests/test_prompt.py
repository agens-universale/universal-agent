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
