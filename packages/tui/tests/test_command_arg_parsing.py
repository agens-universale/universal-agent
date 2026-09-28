"""Аргумент команды — остаток строки дословно (бэкслэши/пробелы целы)."""

import pytest


@pytest.mark.asyncio
async def test_arg_preserves_backslashes_and_spaces(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", config={"model": "M"}
    )
    async with app.run_test() as pilot:
        # Прямой вызов диспетчера — фокус на парсинге остатка строки,
        # без UI skill-picker'а (он открывается на "/skill <текст>").
        await pilot.app.screen._handle_command(r"skill my skill\with spaces")
        await pilot.pause(0.1)
        assert app.tui_state.pending_skill_id == r"my skill\with spaces"
