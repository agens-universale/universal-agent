"""Страж: P1a не изменил схемы встроенных тулов, видимые модели."""

import json
from pathlib import Path

from helpers.tool_registry_modes import builtin_tools_by_mode

SNAPSHOT = Path(__file__).parent / "fixtures" / "tool_specs_snapshot.json"


async def test_builtin_tool_specs_match_snapshot():
    expected = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    current = json.loads(json.dumps(await builtin_tools_by_mode()))
    assert current == expected
