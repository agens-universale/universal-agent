"""Аргумент команды — остаток строки дословно (бэкслэши/пробелы целы)."""

from gc_testkit.tui_support import tui_settings
import pytest


@pytest.mark.asyncio
async def test_arg_preserves_backslashes_and_spaces(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        delivered = []

        async def _spy(skill_id, prompt, *, loaded=True):
            delivered.append((skill_id, prompt, loaded))

        scr._deliver_skill_with_prompt = _spy
        # Прямой вызов диспетчера — фокус на парсинге остатка строки,
        # без UI skill-picker'а (он открывается на "/skill <текст>").
        # Имя скилла — первый токен; его нет в каталоге, поэтому остаток
        # уходит текстом без скилла — дословно.
        await scr._handle_command(r"skill my skill\with spaces")
        await pilot.pause(0.1)
        assert delivered == [("my", r"skill\with spaces", False)]
