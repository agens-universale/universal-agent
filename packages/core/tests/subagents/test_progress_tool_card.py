"""Заголовок карточки тула и её call_id — общие у ACP и server.

Оба фронтенда показывают вызов тула одной карточкой и раньше держали эти две
функции копиями; копия расходится молча, поэтому дом у них один — ядро.
"""

from __future__ import annotations

import pytest
from gc_core.execution.progress import tool_call_id, tool_card_title


def test_title_is_name_with_brief_arguments():
    title = tool_card_title("fs_read_file", '{"path": "a.py"}')

    assert title == "fs_read_file(a.py)"


def test_title_displays_path_relative_to_root(tmp_path):
    title = tool_card_title(
        "fs_read_file", {"path": str(tmp_path / "a.py")}, root=str(tmp_path)
    )

    assert title == "fs_read_file(a.py)"


@pytest.mark.parametrize("arguments", [None, "", "{}", {}])
def test_title_without_arguments_is_bare_name(arguments):
    assert tool_card_title("ls", arguments) == "ls"


@pytest.mark.parametrize(
    "message, expected",
    [
        ({"call_id": "c1", "id": "i1"}, "c1"),
        ({"call_id": "", "id": "i1"}, "i1"),
        ({"call_id": 7, "id": "i1"}, "i1"),
        ({"id": "i1"}, "i1"),
        ({"call_id": "", "id": ""}, None),
        ({}, None),
    ],
)
def test_call_id_prefers_call_id_then_id(message, expected):
    assert tool_call_id(message) == expected
