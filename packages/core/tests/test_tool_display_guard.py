"""Страж: таблица отображения тулов и реестр встроенных не расходятся."""

from helpers.tool_registry_modes import builtin_tools_union

from gc_core.constants.tool_display import (
    LEGACY_TOOL_NAMES,
    TOOL_DISPLAY,
    DisplayCategory,
    tool_display,
)


async def test_every_builtin_tool_has_display():
    names = await builtin_tools_union()
    missing = [
        name
        for name in names
        if tool_display(name).category is DisplayCategory.CALLED
    ]
    assert missing == []


async def test_table_has_no_stale_names():
    names = set(await builtin_tools_union())
    assert sorted(set(TOOL_DISPLAY) - names) == []
    assert sorted(set(LEGACY_TOOL_NAMES.values()) - names) == []
